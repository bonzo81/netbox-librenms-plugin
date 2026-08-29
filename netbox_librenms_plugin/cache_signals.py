"""
Drive sync-tab cache invalidation from the NetBox writes themselves.

A view used to name the devices whose snapshots a mutation had invalidated, which meant a view
that changed a second device could silently leave that device's tabs claiming their cached data
was current. The decision now follows the ORM writes, so a caller cannot forget an object it
changed.

The page the user acted on is excluded while it holds a claim: only the request knows which tab
is the source of the change and only the response can report the cleanup, so
``schedule_request_cache_mutation`` keeps that half. Forgetting to claim over-invalidates, which
is the safe direction to fail.
"""

import logging
from typing import NamedTuple

from django.db import DatabaseError, transaction
from django.db.models.signals import m2m_changed, post_delete, post_init, post_save, pre_delete, pre_save

from netbox_librenms_plugin.sync_cache import (
    SyncCacheConsistency,
    active_sync_subject_keys,
    sync_subject_key,
)

logger = logging.getLogger(__name__)

DISPATCH_PREFIX = "netbox_librenms_plugin_cache_invalidation"
PREVIOUS_OWNER_ATTRIBUTE = "_librenms_previous_owner"
M2M_CLEAR_SUBJECTS_ATTRIBUTE = "_librenms_m2m_clear_subjects"

DEVICE_LABEL = "dcim.device"
VIRTUAL_MACHINE_LABEL = "virtualization.virtualmachine"
ASSIGNED_OBJECT_LABELS = ("dcim.macaddress", "ipam.ipaddress")

# The columns that decide whose snapshots a row belongs to. A change to any of them moves the
# row between owners, so the owner it left has to be invalidated as well as the one it joined.
OWNER_COLUMNS = {
    "dcim.module": ("device_id",),
    "dcim.modulebay": ("device_id",),
    "dcim.interface": ("device_id",),
    "virtualization.vminterface": ("virtual_machine_id",),
    "dcim.macaddress": ("assigned_object_type_id", "assigned_object_id"),
    "ipam.ipaddress": ("assigned_object_type_id", "assigned_object_id"),
    "dcim.cabletermination": ("_device_id",),
}


class _AssignmentReference(NamedTuple):
    """Identify the interface-like object behind an assigned IP address or MAC address."""

    content_type_id: int
    object_id: int


class _Batch:
    """One transaction's worth of objects to invalidate, flushed once on commit."""

    def __init__(self, using):
        self.using = using
        self.owner_keys = {}
        self.assignment_claims = {}
        # Bound once: attribute access builds a new bound method each time, so the queued hook
        # could never be recognised by identity if it were re-read on every lookup.
        self.hook = self.flush

    def add(self, key):
        """Record an owner identity to clean up, keeping insertion order."""
        self.owner_keys[key] = None

    def add_assignment(self, reference, claimed_subject_keys):
        """Record a generic assignment and the claims active for every occurrence."""
        claims = set(claimed_subject_keys)
        if reference in self.assignment_claims:
            self.assignment_claims[reference].intersection_update(claims)
        else:
            self.assignment_claims[reference] = claims

    def _resolve_assignments(self):
        """Resolve every assigned object in bulk, once per concrete model."""
        from django.contrib.contenttypes.models import ContentType

        content_type_ids = {reference.content_type_id for reference in self.assignment_claims}
        content_types = ContentType.objects.using(self.using).in_bulk(content_type_ids)
        references_by_model = {}
        for reference in self.assignment_claims:
            content_type = content_types.get(reference.content_type_id)
            model = content_type.model_class() if content_type is not None else None
            if model is None:
                continue
            label = model._meta.label_lower
            if label in ASSIGNED_OBJECT_LABELS:
                logger.warning("Ignored unsupported assigned-object model %s after a NetBox change", label)
                continue
            if label not in OWNER_COLUMNS:
                continue
            references_by_model.setdefault(model, []).append(reference)

        for model, references in references_by_model.items():
            label = model._meta.label_lower
            object_ids = {reference.object_id for reference in references}
            rows = {
                row[0]: row[1:]
                for row in model.objects.using(self.using)
                .filter(pk__in=object_ids)
                .values_list("pk", *OWNER_COLUMNS[label])
            }
            for reference in references:
                values = rows.get(reference.object_id)
                key = _owner_key_from_columns(label, values) if values is not None else None
                if key is not None and key not in self.assignment_claims[reference]:
                    self.add(key)

    def flush(self):
        """Clear the caches of every object recorded in this batch."""
        from django.apps import apps

        try:
            self._resolve_assignments()
        except DatabaseError:
            logger.exception("Could not resolve assigned-object owners after a NetBox change")
        for label, pk in self.owner_keys:
            try:
                owner = apps.get_model(label).objects.using(self.using).filter(pk=pk).first()
                if owner is not None:
                    SyncCacheConsistency(owner).invalidate_every_tab()
            except Exception:
                logger.exception("Could not clear sync caches for %s %s after a NetBox change", label, pk)


def _record_into_batch(connection, add):
    """
    Record one owner against this transaction's batch, registering its hook once.

    Batches are kept per savepoint scope. A savepoint rollback removes only the callbacks
    registered inside it, so an owner recorded in an inner block has to ride that block's own
    hook: adding it to an enclosing batch would clear its caches even though its write never
    committed. An empty commit queue is likewise no signal that a batch is finished, so a
    batch is reused only while its own hook is still queued.

    The owner is recorded before the hook is registered, because outside an atomic block
    ``on_commit`` runs the hook inline: registering first would flush an empty batch and lose
    the write entirely.

    Args:
        connection: The database connection the write went through.
        add: A callable that records one item on the selected batch.
    """
    batches = getattr(connection, "_librenms_invalidation_batches", None)
    if batches is None:
        batches = connection._librenms_invalidation_batches = {}

    queued = {id(func) for _sids, func, *_rest in connection.run_on_commit}
    # Drop batches whose hook has fired or been rolled back, so the map cannot grow unbounded.
    for scope in [scope for scope, batch in batches.items() if id(batch.hook) not in queued]:
        del batches[scope]

    scope = tuple(connection.savepoint_ids)
    batch = batches.get(scope)
    if batch is not None:
        add(batch)
        return

    batch = _Batch(connection.alias)
    add(batch)
    batches[scope] = batch
    transaction.on_commit(batch.hook, using=connection.alias)


def _record(connection, key):
    """Record one concrete owner against this transaction's batch."""
    _record_into_batch(connection, lambda batch: batch.add(key))


def _record_assignment(connection, reference, claimed_subject_keys):
    """Record one generic assignment for bulk owner resolution at commit."""
    _record_into_batch(connection, lambda batch: batch.add_assignment(reference, claimed_subject_keys))


def _owner_key_from_columns(label, values):
    """
    Return the owner identity a set of owner-column values points at.

    Args:
        label: The ``app_label.modelname`` of the row the values came from.
        values: The values of that model's :data:`OWNER_COLUMNS`, in order.

    Returns:
        The key from :func:`sync_subject_key`, or None when the row belongs to no synchronization subject.
    """
    if not values or values[0] is None:
        return None
    if label in ASSIGNED_OBJECT_LABELS:
        raise ValueError(f"Assigned object {label} must be resolved through the transaction batch.")
    if label == "virtualization.vminterface":
        return sync_subject_key(VIRTUAL_MACHINE_LABEL, values[0])
    return sync_subject_key(DEVICE_LABEL, values[0])


def _owner_key_of(instance):
    """
    Return the identity of the object whose snapshots *instance* belongs to.

    Reads the foreign-key columns rather than following the descriptors, so recording a write
    never loads the owner. The batch loads each distinct owner once, at commit.

    Args:
        instance: A model instance, or None.

    Returns:
        The key from :func:`sync_subject_key`, or None when there is no synchronization subject behind it.
    """
    if instance is None or instance.pk is None:
        return None
    label = instance._meta.label_lower
    if label in (DEVICE_LABEL, VIRTUAL_MACHINE_LABEL):
        return sync_subject_key(instance)
    if label in ASSIGNED_OBJECT_LABELS:
        raise ValueError(f"Assigned object {label} must be resolved through the transaction batch.")
    columns = OWNER_COLUMNS.get(label, ())
    return _owner_key_from_columns(label, tuple(getattr(instance, column, None) for column in columns))


def _schedule(key, using):
    """
    Queue one cleanup for an owner identity, at most once per transaction.

    Args:
        key: The owner identity from :func:`sync_subject_key`.
        using: The database alias the write went through.
    """
    if key is None or key in active_sync_subject_keys():
        # The synchronization subject keeps its own transition, which preserves its source tab.
        return
    _record(transaction.get_connection(using), key)


def _schedule_columns(label, values, using):
    """Queue owner columns, deferring generic assignment resolution until commit."""
    if label in ASSIGNED_OBJECT_LABELS:
        if len(values) == 2 and all(value is not None for value in values):
            _record_assignment(
                transaction.get_connection(using),
                _AssignmentReference(*values),
                active_sync_subject_keys(),
            )
        return
    _schedule(_owner_key_from_columns(label, values), using)


def _owner_columns_now(instance):
    """Return the current values of the instance's owner columns."""
    return tuple(getattr(instance, column, None) for column in OWNER_COLUMNS.get(instance._meta.label_lower, ()))


def _remember_owner(sender, instance, **kwargs):
    """
    Record the owner a row was loaded with, so a reassignment can invalidate both sides.

    Tracked here rather than read from NetBox's own ``_original_device``, which this plugin
    deliberately re-seeds while moving components between virtual-chassis members.

    Args:
        sender: The model being instantiated.
        instance: The instance just loaded or built.
    """
    columns = OWNER_COLUMNS.get(instance._meta.label_lower, ())
    values = (
        None
        if any(column not in instance.__dict__ for column in columns)
        else tuple(instance.__dict__[column] for column in columns)
    )
    setattr(instance, PREVIOUS_OWNER_ATTRIBUTE, values)


def _load_deferred_subject_columns(sender, instance, using=None, **kwargs):
    """Load omitted subject columns only when the deferred row is written or deleted."""
    if getattr(instance, PREVIOUS_OWNER_ATTRIBUTE, None) is not None or instance.pk is None:
        return
    columns = OWNER_COLUMNS.get(sender._meta.label_lower, ())
    previous = sender.objects.using(using).filter(pk=instance.pk).values_list(*columns).first()
    setattr(instance, PREVIOUS_OWNER_ATTRIBUTE, previous)
    if previous is not None:
        for column, value in zip(columns, previous, strict=True):
            instance.__dict__.setdefault(column, value)


def _handle_write(sender, instance, using=None, **kwargs):
    """Queue the owner of a created, changed or deleted row, and the one it moved away from."""
    if kwargs.get("raw"):
        # loaddata writes historical rows; nothing is serving them from a snapshot.
        return
    label = sender._meta.label_lower
    current = _owner_columns_now(instance)
    _schedule_columns(label, current, using)

    previous = getattr(instance, PREVIOUS_OWNER_ATTRIBUTE, None)
    if previous is None or previous == current:
        return
    # The row moved: the object it left still holds a snapshot that no longer matches NetBox.
    _schedule_columns(label, previous, using)


def _handle_m2m(sender, instance, action, reverse, pk_set=None, using=None, **kwargs):
    """Queue the owners of an m2m change, which post_save never reports."""
    if reverse and action == "pre_clear":
        _remember_reverse_clear_subjects(sender, instance, using)
        return
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if not reverse:
        _schedule(_owner_key_of(instance), using)
        return
    # The reverse side hands us the VLAN. A clear names its interfaces only in pre_clear.
    if action == "post_clear":
        for key in _take_reverse_clear_subjects(sender, instance, using):
            _schedule(key, using)
        return
    if not pk_set:
        return
    interface_field, _vlan_field = _m2m_relation_fields(sender)
    interface_model = interface_field.related_model
    for changed in interface_model.objects.using(using).filter(pk__in=pk_set):
        _schedule(_owner_key_of(changed), using)


def _remember_reverse_clear_subjects(through, instance, using):
    """Capture synchronization subjects before a reverse clear removes its through rows."""
    from django.core.exceptions import ImproperlyConfigured

    interface_field, reverse_field = _m2m_relation_fields(through)
    if not isinstance(instance, reverse_field.related_model):
        raise ImproperlyConfigured(
            f"Sync cache invalidation received {type(instance)._meta.label_lower} for {through._meta.label_lower}."
        )

    interface_ids = (
        through.objects.using(using)
        .filter(**{reverse_field.attname: instance.pk})
        .values_list(interface_field.attname, flat=True)
    )
    interface_model = interface_field.related_model
    label = interface_model._meta.label_lower
    owner_rows = interface_model.objects.using(using).filter(pk__in=interface_ids).values_list(*OWNER_COLUMNS[label])
    keys = tuple(key for values in owner_rows if (key := _owner_key_from_columns(label, values)) is not None)
    remembered = getattr(instance, M2M_CLEAR_SUBJECTS_ATTRIBUTE, None)
    if remembered is None:
        remembered = {}
        setattr(instance, M2M_CLEAR_SUBJECTS_ATTRIBUTE, remembered)
    remembered[(through._meta.label_lower, using)] = keys


def _take_reverse_clear_subjects(through, instance, using):
    """Return and forget subjects captured for one reverse m2m clear."""
    remembered = getattr(instance, M2M_CLEAR_SUBJECTS_ATTRIBUTE, {})
    return remembered.pop((through._meta.label_lower, using), ())


def _m2m_relation_fields(through):
    """Return the interface and VLAN fields, or reject an unsupported through-model shape."""
    from django.core.exceptions import ImproperlyConfigured

    relation_fields = [field for field in through._meta.fields if getattr(field, "related_model", None) is not None]
    interface_fields = [field for field in relation_fields if field.related_model._meta.label_lower in OWNER_COLUMNS]
    vlan_fields = [field for field in relation_fields if field not in interface_fields]
    if len(interface_fields) != 1 or len(vlan_fields) != 1:
        raise ImproperlyConfigured(
            f"Sync cache invalidation does not recognize tagged-VLAN through model {through._meta.label_lower}."
        )
    return interface_fields[0], vlan_fields[0]


def _validate_owner_columns(models):
    """Fail startup when a supported NetBox model lacks a declared owner column."""
    from django.core.exceptions import ImproperlyConfigured

    for model in models:
        label = model._meta.label_lower
        available = {field.attname for field in model._meta.get_fields() if hasattr(field, "attname")}
        for column in OWNER_COLUMNS[label]:
            if column not in available:
                raise ImproperlyConfigured(
                    f"Sync cache invalidation model {label} does not provide owner column {column}."
                )


def connect():
    """Subscribe to the NetBox writes the sync tabs depend on."""
    from dcim.models import CableTermination, Interface, MACAddress, Module, ModuleBay
    from ipam.models import IPAddress
    from virtualization.models import VMInterface

    models = (Module, ModuleBay, Interface, VMInterface, MACAddress, IPAddress, CableTermination)
    _validate_owner_columns(models)

    through_models = (Interface.tagged_vlans.through, VMInterface.tagged_vlans.through)
    for through in through_models:
        _m2m_relation_fields(through)

    for model in models:
        label = model._meta.label_lower
        post_init.connect(_remember_owner, sender=model, dispatch_uid=f"{DISPATCH_PREFIX}_init_{label}")
        pre_save.connect(
            _load_deferred_subject_columns,
            sender=model,
            dispatch_uid=f"{DISPATCH_PREFIX}_pre_save_{label}",
        )
        pre_delete.connect(
            _load_deferred_subject_columns,
            sender=model,
            dispatch_uid=f"{DISPATCH_PREFIX}_pre_delete_{label}",
        )
        post_save.connect(_handle_write, sender=model, dispatch_uid=f"{DISPATCH_PREFIX}_save_{label}")
        post_delete.connect(_handle_write, sender=model, dispatch_uid=f"{DISPATCH_PREFIX}_delete_{label}")

    for through in through_models:
        m2m_changed.connect(
            _handle_m2m,
            sender=through,
            dispatch_uid=f"{DISPATCH_PREFIX}_m2m_{through._meta.label_lower}",
        )
