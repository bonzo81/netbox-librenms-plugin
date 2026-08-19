"""Shared LibreNMS port to NetBox interface synchronization."""

import logging

from dcim.models import Device, Interface, MACAddress
from django.db import transaction
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.utils import (
    coerce_interface_mtu,
    AmbiguousLibreNMSIdError,
    bounded_interface_text,
    interface_name_rejection_reason,
    convert_speed_to_kbps,
    find_by_librenms_id,
    interface_name_fallback_matches_port,
    normalize_librenms_port_id,
    set_librenms_device_id,
)

logger = logging.getLogger(__name__)


def get_netbox_interface_type(librenms_interface, *, speed_converter=convert_speed_to_kbps):
    """Return the NetBox interface type for one LibreNMS port."""
    speed = speed_converter(librenms_interface.get("ifSpeed"))
    mappings = InterfaceTypeMapping.objects.filter(librenms_type=librenms_interface.get("ifType"))

    if speed is not None:
        speed_mapping = mappings.filter(librenms_speed__lte=speed).order_by("-librenms_speed").first()
        mapping = speed_mapping or mappings.filter(librenms_speed__isnull=True).first()
    else:
        mapping = mappings.filter(librenms_speed__isnull=True).first()

    return mapping.netbox_type if mapping else "other"


def assign_interface_mac(interface, mac_address):
    """Assign one MAC address to an interface when LibreNMS supplies it."""
    if not mac_address:
        return
    existing_mac = interface.mac_addresses.filter(mac_address=mac_address).first()
    mac_obj = existing_mac or MACAddress.objects.create(mac_address=mac_address)
    interface.mac_addresses.add(mac_obj)
    if hasattr(interface, "primary_mac_address"):
        interface.primary_mac_address = mac_obj


def update_interface_from_port(
    interface,
    librenms_interface,
    *,
    server_key,
    interface_name_field,
    exclude_columns=(),
    netbox_type=None,
    speed_converter=convert_speed_to_kbps,
):
    """Update one Interface or VMInterface from a LibreNMS port row."""
    is_device_interface = isinstance(interface, Interface)
    field_mapping = {
        interface_name_field: "name",
        "ifType": "type",
        "ifSpeed": "speed",
        "ifAlias": "description",
        "ifMtu": "mtu",
    }

    if "name" not in exclude_columns:
        rejection = interface_name_rejection_reason(librenms_interface, interface_name_field, type(interface))
        if rejection is not None:
            raise ValueError(f"The LibreNMS {rejection}.")

    for librenms_key, netbox_key in field_mapping.items():
        if netbox_key in exclude_columns:
            continue
        if librenms_key == "ifSpeed":
            setattr(interface, netbox_key, speed_converter(librenms_interface.get(librenms_key)))
        elif librenms_key == "ifType":
            if is_device_interface and hasattr(interface, netbox_key):
                setattr(interface, netbox_key, netbox_type)
        elif librenms_key == "ifAlias":
            # Same rule the interface table renders: an alias echoing either canonical name is
            # not a description. Writing "" rather than skipping keeps the row and the table
            # agreeing after a sync.
            alias = librenms_interface.get("ifAlias")
            echoes_name = alias in (librenms_interface.get("ifDescr"), librenms_interface.get("ifName"))
            usable_alias = alias if isinstance(alias, str) and not echoes_name else ""
            setattr(interface, netbox_key, bounded_interface_text(netbox_key, usable_alias, type(interface)))
        elif librenms_key == "ifMtu":
            setattr(interface, netbox_key, coerce_interface_mtu(librenms_interface.get(librenms_key)))
        else:
            setattr(interface, netbox_key, librenms_interface.get(librenms_key))

    port_id = normalize_librenms_port_id(librenms_interface.get("port_id"))
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
        admin_status = librenms_interface.get("ifAdminStatus")
        interface.enabled = (
            True
            if admin_status is None
            else (admin_status.lower() == "up" if isinstance(admin_status, str) else bool(admin_status))
        )

    if "mac_address" not in exclude_columns:
        assign_interface_mac(interface, librenms_interface.get("ifPhysAddress"))

    interface.save()


@transaction.atomic
def resolve_or_create_interface_from_port(
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
        server_key=server_key,
        interface_name_field=interface_name_field,
        netbox_type=netbox_type,
        speed_converter=speed_converter,
    )
    if not viewable_queryset.filter(pk=interface.pk).exists():
        raise ValueError("The synchronized NetBox interface is outside your view scope.")
    return interface
