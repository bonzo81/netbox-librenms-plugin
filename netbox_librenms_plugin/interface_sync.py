"""Shared LibreNMS port to NetBox interface synchronization."""

import logging
from copy import deepcopy

from dcim.choices import InterfaceTypeChoices
from dcim.models import Device, Interface, MACAddress
from django.db import transaction
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.constants import INTERFACE_NAME_KEY, INTERFACE_SYNC_FIELD_PAIRS
from netbox_librenms_plugin.interface_diff import (
    interface_enabled_from_port,
    syncable_mac_address,
    synced_description,
)
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.utils import (
    AmbiguousLibreNMSIdError,
    coerce_interface_mtu,
    convert_speed_to_kbps,
    find_by_librenms_id,
    get_librenms_device_id,
    interface_name_fallback_matches_port,
    interface_name_rejection_reason,
    normalize_librenms_port_id,
    select_interface_type_mapping,
    set_librenms_device_id,
)

logger = logging.getLogger(__name__)


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


def get_netbox_interface_type(librenms_interface, *, speed_converter=convert_speed_to_kbps):
    """Return the NetBox interface type for one LibreNMS port, or None when nothing maps it."""
    speed = speed_converter(librenms_interface.get("ifSpeed"))
    # One type's rows are a handful at most, and resolving them in Python keeps this and the
    # interface table on the same rule.
    mappings = InterfaceTypeMapping.objects.filter(librenms_type=librenms_interface.get("ifType"))
    mapping = select_interface_type_mapping(mappings, speed)
    return mapping.netbox_type if mapping else None


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
    existing_mac = interface.mac_addresses.filter(mac_address=mac_address).first()
    mac_obj = existing_mac or MACAddress.objects.create(mac_address=mac_address)
    # The lookup above is scoped to this interface, so a miss means add() attaches it.
    changed = existing_mac is None
    interface.mac_addresses.add(mac_obj)
    if hasattr(interface, "primary_mac_address"):
        changed = changed or interface.primary_mac_address_id != mac_obj.pk
        interface.primary_mac_address = mac_obj
    return changed


def update_interface_from_port(  # noqa: C901
    interface,
    librenms_interface,
    *,
    synced_name,
    server_key,
    interface_name_field,
    exclude_columns=(),
    netbox_type=None,
    speed_converter=convert_speed_to_kbps,
):
    """Update one Interface or VMInterface and return whether NetBox changed."""
    is_device_interface = isinstance(interface, Interface)
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
            if is_device_interface and hasattr(interface, netbox_key):
                # No mapping means no opinion: only an interface with no type yet takes the
                # default, so an unmapped ifType can never flatten a correct type (a LAG
                # aggregate above all, which NetBox needs typed before it accepts members).
                if netbox_type is not None:
                    setattr(interface, netbox_key, netbox_type)
                elif not getattr(interface, netbox_key, None):
                    setattr(interface, netbox_key, InterfaceTypeChoices.TYPE_OTHER)
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
            existing_owner = find_by_librenms_id(type(interface), port_id, server_key)
        except AmbiguousLibreNMSIdError:
            logger.warning("Not setting port_id %s because it matches multiple interfaces.", port_id)
        else:
            if existing_owner is None or existing_owner.pk == interface.pk:
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
    server_key,
    interface_name_field,
    changeable_queryset,
    viewable_queryset,
    speed_converter=convert_speed_to_kbps,
):
    """Resolve or create one interface from an unambiguous LibreNMS port row."""
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

    rejection = interface_name_rejection_reason(librenms_interface, interface_name_field, model)
    if rejection is not None:
        raise ValueError(f"The LibreNMS {rejection}.")
    interface_name = librenms_interface[interface_name_field]
    port_id = normalize_librenms_port_id(librenms_interface.get("port_id"))
    if port_id is None:
        raise ValueError("The LibreNMS port ID is missing or invalid.")

    try:
        by_id = find_by_librenms_id(model, port_id, server_key)
    except AmbiguousLibreNMSIdError:
        raise ValueError("The LibreNMS port ID matches multiple NetBox interfaces.") from None

    owner_id = owner.pk
    if by_id is not None:
        if getattr(by_id, owner_id_field) != owner_id:
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
                raise ValueError("The interface name is already bound to another LibreNMS port.")
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

    netbox_type = get_netbox_interface_type(librenms_interface, speed_converter=speed_converter)
    update_interface_from_port(
        interface,
        librenms_interface,
        synced_name=interface_name,
        server_key=server_key,
        interface_name_field=interface_name_field,
        netbox_type=netbox_type,
        speed_converter=speed_converter,
    )
    if not viewable_queryset.filter(pk=interface.pk).exists():
        raise ValueError("The synchronized NetBox interface is outside your view scope.")
    return interface
