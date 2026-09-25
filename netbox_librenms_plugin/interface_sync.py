"""Shared LibreNMS port to NetBox interface synchronization."""

import logging
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy, deepcopy
from typing import NamedTuple

from dcim.models import Device, Interface, MACAddress
from django.db import transaction
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.constants import INTERFACE_NAME_KEY, INTERFACE_SYNC_FIELD_PAIRS
from netbox_librenms_plugin.interface_diff import (
    interface_enabled_from_port,
    planned_interface_type,
    syncable_mac_address,
    synced_description,
)
from netbox_librenms_plugin.utils import (
    AmbiguousLibreNMSIdError,
    LibreNMSPortBindingConflict,
    claim_librenms_port_binding,
    coerce_interface_mtu,
    convert_speed_to_kbps,
    find_interface_by_librenms_port_id,
    get_librenms_device_id,
    interface_name_fallback_matches_port,
    interface_name_rejection_reason,
    normalize_librenms_port_id,
    set_librenms_device_id,
)
from netbox_librenms_plugin.transactions import first_at_version, row_changed, save_at_version

logger = logging.getLogger(__name__)


class InterfaceWrite(NamedTuple):
    """The result of a write of one interface row."""

    # The instance that holds the row as written; the caller continues with it.
    interface: Interface | VMInterface
    # Whether NetBox changed: the row, or a MAC address that the write attached.
    changed: bool


class NewInterfaceOutsideScope(ValueError):
    """A row that the sync created is outside the user's add scope or change scope, as the sync wrote it."""

    def __init__(self, actions):
        super().__init__(f"The new NetBox interface is outside your {' and '.join(actions)} scope.")


def interface_rows_outside_scope(written, created, *, addable_queryset, changeable_queryset):
    """
    Return the rows that a sync wrote and that are outside the user's scopes as the rows are now.

    NetBox's edit views save an object and then check that the saved object is in the user's
    scope. The sync writes a row that it created as a change too, so each written row must be in
    the change scope, and each created row also in the add scope. Call it after the last write,
    inside the transaction of the writes, so that a refusal can roll the writes back.

    Args:
        written (set[int]): The pks of the rows of one model that the sync created or changed.
        created (set[int]): The pks of the rows in *written* that the sync created.
        addable_queryset (QuerySet): The rows of the model that the user may add.
        changeable_queryset (QuerySet): The rows of the model that the user may change.

    Returns:
        dict[int, tuple[str, ...]]: The actions (``add``, ``change``) whose scope each refused row is outside, by pk.

    """
    refused = {}
    for action, pks, queryset in (("add", created, addable_queryset), ("change", written, changeable_queryset)):
        if pks:
            for pk in pks - set(queryset.filter(pk__in=pks).values_list("pk", flat=True)):
                refused[pk] = (*refused.get(pk, ()), action)
    return refused


class InterfaceWrites:
    """
    The Interface and VMInterface rows that one sync of *user* created or changed while ``collect_interface_writes`` runs.

    The receivers ``record_interface_save`` (``post_save``) and ``record_tagged_vlan_change``
    (``m2m_changed``) record the rows, so every save and every tagged-VLAN change is recorded, also
    one that NetBox makes. Each write path gives each row that it writes a name for a refusal
    (``name_interface_row``): the name that it read while the user could view the row, or None.
    """

    def __init__(self, user):
        self.user = user
        self.written = defaultdict(set)
        self.created = defaultdict(set)
        self.names = {}

    def outside_scope(self):
        """
        Return the recorded rows that are outside the user's scopes now, with one query for each model and action.

        Returns:
            tuple[list[tuple[str, tuple[str, ...]]], int]: The name of each refused row that the
                user may view and the actions whose scope it is outside, sorted; and the number of
                refused rows that the user may not view.

        Raises:
            RuntimeError: A refused row has no name entry: a write path of the caller did not name it.

        """
        named, hidden = [], 0
        for model, written in self.written.items():
            outside = interface_rows_outside_scope(
                written,
                self.created[model],
                addable_queryset=model.objects.restrict(self.user, "add"),
                changeable_queryset=model.objects.restrict(self.user, "change"),
            )
            for pk, actions in outside.items():
                if (model, pk) not in self.names:
                    raise RuntimeError(f"The sync wrote {model.__name__} {pk}, but gave it no name.")
                if (name := self.names[(model, pk)]) is None:
                    hidden += 1
                else:
                    named.append((name, actions))
        return sorted(named), hidden


# The writes of the sync that runs now; None outside ``collect_interface_writes``.
_active_writes = ContextVar("librenms_interface_writes", default=None)


@contextmanager
def collect_interface_writes(user):
    """Record the Interface and VMInterface rows that the block writes for *user*, and yield the ``InterfaceWrites``."""
    writes = InterfaceWrites(user)
    token = _active_writes.set(writes)
    try:
        yield writes
    finally:
        _active_writes.reset(token)


def name_interface_row(interface, name):
    """
    Give *interface* its name for a refusal. The first name of a row stays.

    *name* is the name that the caller read while the user could view the row, or None for a row
    that the user may not view: a refusal counts that row and does not name it. Outside
    ``collect_interface_writes`` it does nothing.
    """
    if (writes := _active_writes.get()) is not None:
        writes.names.setdefault((type(interface), interface.pk), name)


def record_interface_save(sender, instance, created, **kwargs):
    """``post_save`` receiver: record a saved Interface or VMInterface row."""
    if (writes := _active_writes.get()) is not None:
        writes.written[sender].add(instance.pk)
        if created:
            writes.created[sender].add(instance.pk)


def record_tagged_vlan_change(sender, instance, action, reverse, model, pk_set, using, **kwargs):
    """``m2m_changed`` receiver of the tagged VLANs: record each row whose tagged VLANs change."""
    if (writes := _active_writes.get()) is None:
        return
    if not reverse:
        if action in ("post_add", "post_remove", "post_clear"):
            writes.written[type(instance)].add(instance.pk)
    elif action in ("post_add", "post_remove"):
        writes.written[model].update(pk_set)
    elif action == "pre_clear":
        # A clear from the VLAN side names no rows, so read them before the clear.
        writes.written[model].update(
            model.objects.using(using).filter(tagged_vlans=instance).values_list("pk", flat=True)
        )


def _owner_filter(interface):
    """Return the lookup of the owner of *interface*: its Device or its VirtualMachine."""
    if isinstance(interface, Interface):
        return {"device_id": interface.device_id}
    return {"virtual_machine_id": interface.virtual_machine_id}


def copy_before_change(instance):
    """
    Return a copy of *instance* that keeps the field values that *instance* has now.

    The copy shares no field value with *instance*, so a later change of *instance* leaves the copy
    as it was, also a change inside a value such as the custom field data. It sends no query.
    """
    before = copy(instance)
    for field in instance._meta.concrete_fields:
        setattr(before, field.attname, deepcopy(getattr(instance, field.attname)))
    return before


def changed_fields(instance, before):
    """Return the concrete fields of *instance* whose values are not the values of *before*, its earlier copy."""
    fields = instance._meta.concrete_fields
    return [field for field in fields if getattr(instance, field.attname) != getattr(before, field.attname)]


def keep_change_log_before_state(instance, before):
    """
    Give the change log record of the next save of *instance* the state of *before*.

    *before* is an earlier copy of *instance*, or its row as stored before the caller changed *instance*.

    Call it only for a row that the caller saves: the snapshot serializes *before*, and it reads
    the tags and the many-to-many values of the row.
    """
    before.snapshot()
    # snapshot() stores the before-state as _prechange_snapshot, where the change log and the events read it.
    instance._prechange_snapshot = before._prechange_snapshot


def write_interface_row(interface, apply, *, fresh_read_queryset, created=False):
    """
    Write the values that *apply* sets to the current row of *interface*, and only when a column changes.

    A row that this sync did not create is read again, with its row version, from its pk and its
    owner, and only from *fresh_read_queryset*. Then ``apply(row)`` sets the values on the fresh
    instance. With no changed column, nothing is locked, written or serialized. With a changed
    column, the change log's before-state is the state of the fresh read, and the row is saved only
    when no other operation changed it since the fresh read (``save_at_version``). A row that this
    sync created is private to its transaction, so it is written without a fresh read.

    Args:
        interface (Interface | VMInterface): The interface as the caller read it.
        apply (Callable[[Interface | VMInterface], bool]): Sets the values on the row to write, and
            returns whether it changed NetBox outside the row's columns.
        fresh_read_queryset (QuerySet): The rows that the fresh read may find. The IP tab passes
            its change scope. The interface sync passes all rows of the model, because its final
            check reads the scope after the last write.
        created (bool): Whether this sync created the interface.

    Returns:
        InterfaceWrite: The instance that holds the row as written, and whether NetBox changed.

    Raises:
        ConcurrentRowChange: The row left its owner or *fresh_read_queryset*, or another operation
            changed it after the fresh read.

    """
    if created:
        row, version = interface, None
    else:
        row, version = first_at_version(fresh_read_queryset.filter(pk=interface.pk, **_owner_filter(interface)))
        if row is None:
            raise row_changed(interface.name)
    # A copy now, and a snapshot only for a changed row: a snapshot reads the database.
    fresh = copy_before_change(row)
    changed_elsewhere = apply(row)
    changed_columns = {field.column for field in changed_fields(row, fresh)}
    if changed_columns and created:
        row.save()
    elif changed_columns:
        keep_change_log_before_state(row, fresh)
        save_at_version(row, version=version, changed_columns=changed_columns, name=interface.name)
    return InterfaceWrite(row, bool(changed_columns) or changed_elsewhere)


def _bound_interface_name_is_occupied(interface, synced_name, port_id, server_key):
    """Return whether a bound interface must keep its current name."""
    if synced_name == interface.name:
        return False
    stored_port_id = normalize_librenms_port_id(get_librenms_device_id(interface, server_key, auto_save=False))
    if port_id is None or stored_port_id != port_id:
        return False
    return (
        type(interface).objects.filter(**_owner_filter(interface), name=synced_name).exclude(pk=interface.pk).exists()
    )


def interface_owner_platform_id(interface):
    """Return the platform of the Device or VirtualMachine that owns an interface."""
    owner = interface.device if isinstance(interface, Interface) else interface.virtual_machine
    return owner.platform_id


def assign_interface_mac(interface, mac_address):
    """
    Assign one MAC address to an interface when LibreNMS supplies it.

    Args:
        interface: The Interface or VMInterface being written.
        mac_address: The MAC LibreNMS reported, if any.

    Returns:
        bool: Whether the assignment changed anything, so the caller does not have to read
            the relation back to find out.
    """
    # One gate, shared with the row diff: a MAC the column refuses is neither written nor
    # reported as a difference.
    mac_address = syncable_mac_address(mac_address)
    if mac_address is None:
        # Name the interface, never the value: a MAC is private data to py/clear-text-logging.
        logger.debug("LibreNMS reported no usable MAC for interface %s; skipping only the MAC.", interface.pk)
        return False
    mac_obj = interface.mac_addresses.filter(mac_address=mac_address).first()
    # The lookup above is scoped to this interface, so a miss means add() attaches it.
    changed = mac_obj is None
    if changed:
        mac_obj = MACAddress.objects.create(mac_address=mac_address)
        interface.mac_addresses.add(mac_obj)
    changed = changed or interface.primary_mac_address_id != mac_obj.pk
    interface.primary_mac_address = mac_obj
    return changed


@transaction.atomic
def update_interface_from_port(  # noqa: C901
    interface,
    librenms_interface,
    *,
    rules,
    synced_name,
    server_key,
    interface_name_field,
    created,
    fresh_read_queryset,
    exclude_columns=(),
    speed_converter=convert_speed_to_kbps,
):
    """
    Update one Interface or VMInterface from its LibreNMS port through ``write_interface_row``.

    ``rules`` decides the port for the interface's owner before anything is written, so an
    ignored or ambiguous port, or an incomplete port record, raises ``PortSyncBlocked``.
    ``created`` says whether the caller's resolver just created the interface, which is the
    one case where the planned type is written without a check against its links.
    ``fresh_read_queryset`` holds the rows that the write may read again, as in ``write_interface_row``.

    Claim and re-read the cross-model port identity before changing any field.

    Returns:
        InterfaceWrite: The instance that holds the row as written, and whether NetBox changed.
            The caller continues with that instance, not with *interface*.

    Raises:
        ConcurrentRowChange: The row left *fresh_read_queryset*, or another operation changed it
            after the write read it.

    """
    decision = rules.decide_interface_write(librenms_interface, platform_id=interface_owner_platform_id(interface))
    port_id = normalize_librenms_port_id(librenms_interface.get("port_id"))
    if port_id is not None:
        claim_librenms_port_binding(port_id, server_key)
        try:
            existing_owner = find_interface_by_librenms_port_id(port_id, server_key)
        except AmbiguousLibreNMSIdError:
            raise LibreNMSPortBindingConflict("The LibreNMS port ID matches multiple NetBox interfaces.") from None
        if existing_owner is not None and existing_owner != interface:
            raise LibreNMSPortBindingConflict("The LibreNMS port ID is already assigned to another NetBox interface.")
    if "name" not in exclude_columns:
        rejection = interface_name_rejection_reason(
            {interface_name_field: synced_name}, interface_name_field, type(interface)
        )
        if rejection is not None:
            raise ValueError(f"The LibreNMS {rejection}.")
    # Built from the shared schema the row diff reads, so the table cannot paint a field this
    # loop leaves alone, or miss one it writes.
    field_mapping = {
        (interface_name_field if librenms_key == INTERFACE_NAME_KEY else librenms_key): netbox_field
        for librenms_key, netbox_field in INTERFACE_SYNC_FIELD_PAIRS
    }
    port_id = normalize_librenms_port_id(librenms_interface.get("port_id"))

    def apply_port(row):
        planned_type = None
        # Planned from the fresh row before any field changes, the same state the row diff reads.
        if isinstance(row, Interface) and "type" not in exclude_columns:
            planned_type = planned_interface_type(row, decision, created=created)
            if planned_type.kept is not None:
                logger.warning("Interface %s (%s): %s", row.pk, row.name, planned_type.kept.log_note)

        for librenms_key, netbox_key in field_mapping.items():
            if netbox_key in exclude_columns:
                continue
            if librenms_key == "ifSpeed":
                setattr(row, netbox_key, speed_converter(librenms_interface.get(librenms_key)))
            elif librenms_key == "ifType":
                if planned_type is not None:
                    row.type = planned_type.value
            elif librenms_key == "ifAlias":
                # Same rule the interface table renders: an alias echoing either canonical name is
                # not a description. Writing "" rather than skipping keeps the row and the table
                # agreeing after a sync.
                setattr(row, netbox_key, synced_description(librenms_interface, type(row)))
            elif librenms_key == "ifMtu":
                setattr(row, netbox_key, coerce_interface_mtu(librenms_interface.get(librenms_key)))
            elif netbox_key == "name" and _bound_interface_name_is_occupied(row, synced_name, port_id, server_key):
                continue
            else:
                value = synced_name if netbox_key == "name" else librenms_interface.get(librenms_key)
                setattr(row, netbox_key, value)

        if port_id is not None:
            set_librenms_device_id(row, port_id, server_key)

        if "enabled" not in exclude_columns:
            row.enabled = interface_enabled_from_port(librenms_interface)

        if "mac_address" in exclude_columns:
            return False
        return assign_interface_mac(row, librenms_interface.get("ifPhysAddress"))

    return write_interface_row(interface, apply_port, fresh_read_queryset=fresh_read_queryset, created=created)


@transaction.atomic
def resolve_or_create_interface_from_port(  # noqa: C901
    owner,
    librenms_interface,
    *,
    rules,
    server_key,
    interface_name_field,
    addable_queryset,
    changeable_queryset,
    viewable_queryset,
    speed_converter=convert_speed_to_kbps,
):
    """
    Resolve or create one interface from an unambiguous LibreNMS port row.

    Raises:
        PortSyncBlocked: Before any lookup or write, when ``rules`` block the port for ``owner``.
        NewInterfaceOutsideScope: The created row, as written, is outside the add or change scope.
        ValueError: When the port cannot be resolved to one interface safely.

    """
    if isinstance(owner, Device):
        model = Interface
        owner_filter = {"device": owner}
        owner_id_field = "device_id"
    elif isinstance(owner, VirtualMachine):
        model = VMInterface
        owner_filter = {"virtual_machine": owner}
        owner_id_field = "virtual_machine_id"
    else:
        raise ValueError("Unsupported interface owner type.")
    rules.decide_interface_write(librenms_interface, platform_id=owner.platform_id)

    rejection = interface_name_rejection_reason(librenms_interface, interface_name_field, model)
    if rejection is not None:
        raise ValueError(f"The LibreNMS {rejection}.")
    interface_name = librenms_interface[interface_name_field]
    port_id = normalize_librenms_port_id(librenms_interface.get("port_id"))
    if port_id is None:
        raise ValueError("The LibreNMS port ID is missing or invalid.")

    claim_librenms_port_binding(port_id, server_key)
    try:
        by_id = find_interface_by_librenms_port_id(port_id, server_key)
    except AmbiguousLibreNMSIdError:
        raise ValueError("The LibreNMS port ID matches multiple NetBox interfaces.") from None

    owner_id = owner.pk
    created = False
    if by_id is not None:
        if not isinstance(by_id, model) or getattr(by_id, owner_id_field) != owner_id:
            raise ValueError("The LibreNMS port ID is already assigned to another NetBox interface owner.")
        if not viewable_queryset.filter(pk=by_id.pk).exists():
            raise ValueError("The matching NetBox interface is outside your view scope.")
        if not changeable_queryset.filter(pk=by_id.pk).exists():
            raise ValueError("The matching NetBox interface is outside your change scope.")
        interface = by_id
    else:
        existing_by_name = model.objects.filter(**owner_filter, name=interface_name).first()
        if existing_by_name is not None:
            if not interface_name_fallback_matches_port(existing_by_name, port_id, server_key):
                # Name the holding port only to a caller who may view the interface.
                holder = (
                    normalize_librenms_port_id(get_librenms_device_id(existing_by_name, server_key, auto_save=False))
                    if viewable_queryset.filter(pk=existing_by_name.pk).exists()
                    else None
                )
                if holder is None:
                    raise ValueError("The interface name is already bound to another LibreNMS port.")
                raise ValueError(f"The interface name is already bound to LibreNMS port {holder}.")
            if not viewable_queryset.filter(pk=existing_by_name.pk).exists():
                raise ValueError("The matching NetBox interface is outside your view scope.")
            if not changeable_queryset.filter(pk=existing_by_name.pk).exists():
                raise ValueError("The matching NetBox interface is outside your change scope.")
            interface = existing_by_name
        else:
            interface, created = model.objects.get_or_create(**owner_filter, name=interface_name)
            if not created:
                if not interface_name_fallback_matches_port(interface, port_id, server_key):
                    raise ValueError("The interface name became bound to another LibreNMS port.")
                if not viewable_queryset.filter(pk=interface.pk).exists():
                    raise ValueError("The matching NetBox interface is outside your view scope.")
                if not changeable_queryset.filter(pk=interface.pk).exists():
                    raise ValueError("The matching NetBox interface is outside your change scope.")

    interface = update_interface_from_port(
        interface,
        librenms_interface,
        rules=rules,
        synced_name=interface_name,
        server_key=server_key,
        interface_name_field=interface_name_field,
        created=created,
        fresh_read_queryset=changeable_queryset,
        speed_converter=speed_converter,
    ).interface
    if created:
        refused = interface_rows_outside_scope(
            {interface.pk}, {interface.pk}, addable_queryset=addable_queryset, changeable_queryset=changeable_queryset
        )
        if refused:
            raise NewInterfaceOutsideScope(refused[interface.pk])
    if not viewable_queryset.filter(pk=interface.pk).exists():
        raise ValueError("The synchronized NetBox interface is outside your view scope.")
    return interface
