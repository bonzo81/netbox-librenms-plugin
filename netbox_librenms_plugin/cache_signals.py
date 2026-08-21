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

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_init, post_save

from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, active_sync_page_keys, sync_page_key

logger = logging.getLogger(__name__)

DISPATCH_PREFIX = "netbox_librenms_plugin_cache_invalidation"
PREVIOUS_OWNER_ATTRIBUTE = "_librenms_previous_owner"

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


class _Batch:
    """One transaction's worth of objects to invalidate, flushed once on commit."""

    def __init__(self):
        self.owner_keys = {}
        # Bound once: attribute access builds a new bound method each time, so the queued hook
        # could never be recognised by identity if it were re-read on every lookup.
        self.hook = self.flush

    def add(self, key):
        """Record an owner identity to clean up, keeping insertion order."""
        self.owner_keys[key] = None

    def flush(self):
        """Clear the caches of every object recorded in this batch."""
        from django.apps import apps

        for label, pk in self.owner_keys:
            try:
                owner = apps.get_model(label).objects.filter(pk=pk).first()
                if owner is not None:
                    SyncCacheConsistency(owner).invalidate_every_tab()
            except Exception:
                logger.exception("Could not clear sync caches for %s %s after a NetBox change", label, pk)


def _record(connection, key):
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
        key: The owner identity to clean up.
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
        batch.add(key)
        return

    batch = _Batch()
    batch.add(key)
    batches[scope] = batch
    transaction.on_commit(batch.hook, using=connection.alias)


def _owner_key_from_columns(label, values):
    """
    Return the owner identity a set of owner-column values points at.

    Args:
        label: The ``app_label.modelname`` of the row the values came from.
        values: The values of that model's :data:`OWNER_COLUMNS`, in order.

    Returns:
        The key from :func:`sync_page_key`, or None when the row belongs to no sync page.
    """
    if not values or values[0] is None:
        return None
    if label in ASSIGNED_OBJECT_LABELS:
        return _owner_key_of_assignment(*values)
    if label == "virtualization.vminterface":
        return sync_page_key(VIRTUAL_MACHINE_LABEL, values[0])
    return sync_page_key(DEVICE_LABEL, values[0])


def _owner_key_of_assignment(content_type_id, object_id):
    """
    Return the identity behind a generic assignment, one hop up.

    An IP or a MAC reaches its device through the interface it is assigned to, so the hop is
    only taken for rows that carry an assignment.

    Args:
        content_type_id: The assigned object's content type.
        object_id: The assigned object's primary key.

    Returns:
        The key from :func:`sync_page_key`, or None when nothing owns it.
    """
    from django.contrib.contenttypes.models import ContentType

    if object_id is None or content_type_id is None:
        return None
    model = ContentType.objects.get_for_id(content_type_id).model_class()
    if model is None or model._meta.label_lower not in OWNER_COLUMNS:
        return None
    assigned = model.objects.filter(pk=object_id).values_list(*OWNER_COLUMNS[model._meta.label_lower]).first()
    return _owner_key_from_columns(model._meta.label_lower, assigned) if assigned else None


def _owner_key_of(instance):
    """
    Return the identity of the object whose snapshots *instance* belongs to.

    Reads the foreign-key columns rather than following the descriptors, so recording a write
    never loads the owner. The batch loads each distinct owner once, at commit.

    Args:
        instance: A model instance, or None.

    Returns:
        The key from :func:`sync_page_key`, or None when there is no sync page behind it.
    """
    if instance is None or instance.pk is None:
        return None
    label = instance._meta.label_lower
    if label in (DEVICE_LABEL, VIRTUAL_MACHINE_LABEL):
        return sync_page_key(instance)
    columns = OWNER_COLUMNS.get(label, ())
    return _owner_key_from_columns(label, tuple(getattr(instance, column, None) for column in columns))


def _schedule(key, using):
    """
    Queue one cleanup for an owner identity, at most once per transaction.

    Args:
        key: The owner identity from :func:`sync_page_key`.
        using: The database alias the write went through.
    """
    if key is None or key in active_sync_page_keys():
        # The acting page keeps its own transition, which preserves its source tab.
        return
    _record(transaction.get_connection(using), key)


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
    setattr(instance, PREVIOUS_OWNER_ATTRIBUTE, _owner_columns_now(instance))


def _handle_write(sender, instance, using=None, **kwargs):
    """Queue the owner of a created, changed or deleted row, and the one it moved away from."""
    if kwargs.get("raw"):
        # loaddata writes historical rows; nothing is serving them from a snapshot.
        return
    _schedule(_owner_key_of(instance), using)

    previous = getattr(instance, PREVIOUS_OWNER_ATTRIBUTE, None)
    if previous is None or previous == _owner_columns_now(instance):
        return
    # The row moved: the object it left still holds a snapshot that no longer matches NetBox.
    _schedule(_owner_key_from_columns(sender._meta.label_lower, previous), using)


def _handle_m2m(sender, instance, action, reverse, pk_set=None, using=None, **kwargs):
    """Queue the owners of an m2m change, which post_save never reports."""
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if not reverse:
        _schedule(_owner_key_of(instance), using)
        return
    # The reverse side hands us the VLAN, so the rows that changed are named by pk_set.
    if not pk_set:
        return
    interface_model = _m2m_interface_model(sender)
    if interface_model is None:
        return
    for changed in interface_model.objects.filter(pk__in=pk_set):
        _schedule(_owner_key_of(changed), using)


def _m2m_interface_model(through):
    """Return the interface model on a tagged-VLAN through table."""
    for field in through._meta.get_fields():
        related = getattr(field, "related_model", None)
        if related is not None and related._meta.label_lower in OWNER_COLUMNS:
            return related
    return None


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

    for model in models:
        label = model._meta.label_lower
        post_init.connect(_remember_owner, sender=model, dispatch_uid=f"{DISPATCH_PREFIX}_init_{label}")
        post_save.connect(_handle_write, sender=model, dispatch_uid=f"{DISPATCH_PREFIX}_save_{label}")
        post_delete.connect(_handle_write, sender=model, dispatch_uid=f"{DISPATCH_PREFIX}_delete_{label}")

    for through in (Interface.tagged_vlans.through, VMInterface.tagged_vlans.through):
        m2m_changed.connect(
            _handle_m2m,
            sender=through,
            dispatch_uid=f"{DISPATCH_PREFIX}_m2m_{through._meta.label_lower}",
        )
