"""Shared LibreNMS port to NetBox interface synchronization."""

import logging
from copy import deepcopy

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
    coerce_interface_mtu,
    convert_speed_to_kbps,
    find_interface_by_librenms_port_id,
    get_librenms_device_id,
    interface_name_fallback_matches_port,
    interface_name_rejection_reason,
    normalize_librenms_port_id,
    set_librenms_device_id,
)

logger = logging.getLogger(__name__)

# Default for update_interface_from_port(port_owner=...): look the port's owner up here.
LOOK_UP_PORT_OWNER = object()


def _bound_interface_name_is_occupied(interface, synced_name, port_id, server_key):
    """Return whether a bound interface must keep its current name."""
    if synced_name == interface.name:
        return False
    stored_port_id = normalize_librenms_port_id(get_librenms_device_id(interface, server_key, auto_save=False))
    if port_id is None or stored_port_id != port_id:
        return False
    owner_filter = (
        {"device_id": interface.device_id}
        if isinstance(interface, Interface)
        else {"virtual_machine_id": interface.virtual_machine_id}
    )
    return type(interface).objects.filter(**owner_filter, name=synced_name).exclude(pk=interface.pk).exists()


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
    if hasattr(interface, "primary_mac_address"):
        changed = changed or interface.primary_mac_address_id != mac_obj.pk
        interface.primary_mac_address = mac_obj
    return changed


def update_interface_from_port(  # noqa: C901
    interface,
    librenms_interface,
    *,
    rules,
    synced_name,
    server_key,
    interface_name_field,
    created,
    exclude_columns=(),
    speed_converter=convert_speed_to_kbps,
    port_owner=LOOK_UP_PORT_OWNER,
):
    """
    Update one Interface or VMInterface and return whether NetBox changed.

    ``rules`` decides the port for the interface's owner before anything is written, so an
    ignored or ambiguous port, or an incomplete port record, raises ``PortSyncBlocked``.
    ``created`` says whether the caller's resolver just created the interface, which is the
    one case where the planned type is written without a check against its links.

    Pass ``port_owner`` only when the caller already looked the port up with
    ``find_interface_by_librenms_port_id`` for this row, so the row reads its owner once.
    """
    decision = rules.decide_interface_write(librenms_interface, platform_id=interface_owner_platform_id(interface))
    planned_type = None
    # Planned before any field changes, from the same state the row diff reads.
    if isinstance(interface, Interface) and "type" not in exclude_columns:
        planned_type = planned_interface_type(interface, decision, created=created)
        if planned_type.kept_reason is not None:
            logger.warning("Interface %s (%s): %s", interface.pk, interface.name, planned_type.kept_reason)
    tracked_fields = (
        "name",
        "type",
        "speed",
        "description",
        "mtu",
        "enabled",
        "primary_mac_address_id",
    )
    before_fields = {
        field_name: getattr(interface, field_name) for field_name in tracked_fields if hasattr(interface, field_name)
    }
    before_custom_fields = deepcopy(interface.custom_field_data)
    # Built from the shared schema the row diff reads, so the table cannot paint a field this
    # loop leaves alone, or miss one it writes.
    field_mapping = {
        (interface_name_field if librenms_key == INTERFACE_NAME_KEY else librenms_key): netbox_field
        for librenms_key, netbox_field in INTERFACE_SYNC_FIELD_PAIRS
    }
    port_id = normalize_librenms_port_id(librenms_interface.get("port_id"))

    if "name" not in exclude_columns:
        rejection = interface_name_rejection_reason(
            {interface_name_field: synced_name}, interface_name_field, type(interface)
        )
        if rejection is not None:
            raise ValueError(f"The LibreNMS {rejection}.")

    for librenms_key, netbox_key in field_mapping.items():
        if netbox_key in exclude_columns:
            continue
        if librenms_key == "ifSpeed":
            setattr(interface, netbox_key, speed_converter(librenms_interface.get(librenms_key)))
        elif librenms_key == "ifType":
            if planned_type is not None:
                interface.type = planned_type.value
        elif librenms_key == "ifAlias":
            # Same rule the interface table renders: an alias echoing either canonical name is
            # not a description. Writing "" rather than skipping keeps the row and the table
            # agreeing after a sync.
            setattr(interface, netbox_key, synced_description(librenms_interface, type(interface)))
        elif librenms_key == "ifMtu":
            setattr(interface, netbox_key, coerce_interface_mtu(librenms_interface.get(librenms_key)))
        elif netbox_key == "name" and _bound_interface_name_is_occupied(
            interface,
            synced_name,
            port_id,
            server_key,
        ):
            continue
        else:
            value = synced_name if netbox_key == "name" else librenms_interface.get(librenms_key)
            setattr(interface, netbox_key, value)

    if port_id is not None:
        try:
            existing_owner = (
                find_interface_by_librenms_port_id(port_id, server_key)
                if port_owner is LOOK_UP_PORT_OWNER
                else port_owner
            )
        except AmbiguousLibreNMSIdError:
            logger.warning("Not setting port_id %s because it matches multiple interfaces.", port_id)
        else:
            # Model equality compares the concrete model and the pk, so a VMInterface never matches an Interface.
            if existing_owner is None or existing_owner == interface:
                set_librenms_device_id(interface, port_id, server_key)
            else:
                logger.warning("Not reassigning port_id %s from %s to %s.", port_id, existing_owner, interface)

    if "enabled" not in exclude_columns:
        interface.enabled = interface_enabled_from_port(librenms_interface)

    mac_changed = False
    if "mac_address" not in exclude_columns:
        mac_changed = assign_interface_mac(interface, librenms_interface.get("ifPhysAddress"))

    fields_changed = before_custom_fields != interface.custom_field_data or any(
        getattr(interface, field_name) != value for field_name, value in before_fields.items()
    )
    if fields_changed:
        interface.save()
    return fields_changed or mac_changed


@transaction.atomic
def resolve_or_create_interface_from_port(  # noqa: C901
    owner,
    librenms_interface,
    *,
    rules,
    server_key,
    interface_name_field,
    changeable_queryset,
    viewable_queryset,
    speed_converter=convert_speed_to_kbps,
):
    """
    Resolve or create one interface from an unambiguous LibreNMS port row.

    Raises:
        PortSyncBlocked: Before any lookup or write, when ``rules`` block the port for ``owner``.
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
            # A model-level add grant does not imply a constrained change grant, so the row
            # this call just created still has to fall inside the caller's change scope
            # before update_interface_from_port() populates it.
            elif not changeable_queryset.filter(pk=interface.pk).exists():
                raise ValueError("The new NetBox interface is outside your change scope.")

    update_interface_from_port(
        interface,
        librenms_interface,
        rules=rules,
        synced_name=interface_name,
        server_key=server_key,
        interface_name_field=interface_name_field,
        created=created,
        speed_converter=speed_converter,
        port_owner=by_id,
    )
    if not viewable_queryset.filter(pk=interface.pk).exists():
        raise ValueError("The synchronized NetBox interface is outside your view scope.")
    return interface
