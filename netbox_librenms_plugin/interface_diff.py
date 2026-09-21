"""
One definition of how a LibreNMS port row differs from the NetBox interface it syncs onto.

The interface table paints its columns from this, and the per-row sync button is shown from it,
so "out of sync" is decided in a single place. Every comparison asks the same question the sync
writer answers: would ``update_interface_from_port`` (plus the VLAN assignment that follows it)
change NetBox? The rule set is therefore built from the same field schema the writer uses, and
reuses the writer's own normalisers, so a row cannot be painted green against a sync that would
still write, or amber against one that would not.
"""

from typing import NamedTuple

from dcim.fields import MACAddressField
from django.core.exceptions import ValidationError

from netbox_librenms_plugin.constants import INTERFACE_SYNC_EXTRA_FIELDS, INTERFACE_SYNC_FIELD_PAIRS
from netbox_librenms_plugin.utils import (
    bounded_interface_text,
    check_vlan_group_matches,
    coerce_interface_mtu,
    convert_speed_to_kbps,
    get_librenms_device_id,
    normalize_librenms_port_id,
)

# Per-field verdicts.
ABSENT = "absent"
DIFFERS = "differs"
MATCHES = "matches"

# Row states.
ROW_ABSENT = "absent"
ROW_DIFFERS = "differs"
ROW_IN_SYNC = "in_sync"

# Every field one sync writes, in the order the table shows them.
SYNC_FIELDS = tuple(netbox_field for _, netbox_field in INTERFACE_SYNC_FIELD_PAIRS) + INTERFACE_SYNC_EXTRA_FIELDS

# The model attribute each field needs. A model without it (VMInterface has no type or speed)
# cannot be changed by that part of the sync, so the field is reported as matching.
_REQUIRED_ATTRIBUTE = {
    "name": "name",
    "type": "type",
    "speed": "speed",
    "description": "description",
    "mtu": "mtu",
    "enabled": "enabled",
    "mac_address": "mac_addresses",
    "librenms_id": "custom_field_data",
    "vlans": "mode",
}


class DiffContext(NamedTuple):
    """Everything a row comparison needs beyond the row and the NetBox interface itself."""

    interface_name_field: str
    server_key: str
    netbox_type: str | None
    vlan_context: object | None


class RowSyncState(NamedTuple):
    """One row's sync verdict: the row state, and the verdict for each field a sync writes."""

    state: str
    fields: dict

    @property
    def differing_fields(self):
        """Return the fields a sync would change, in table order."""
        return tuple(name for name in SYNC_FIELDS if self.fields.get(name) == DIFFERS)

    def verdict(self, field):
        """Return one field's verdict, defaulting to absent for a field this row never compared."""
        return self.fields.get(field, ABSENT)


def interface_enabled_from_port(port):
    """
    Return the enabled state a sync writes from a LibreNMS port's ifAdminStatus.

    LibreNMS omits ifAdminStatus for a port it cannot poll administratively; the sync treats
    that as enabled, so the table has to read it the same way or an unpolled port renders as
    a difference that never resolves.

    Args:
        port (dict): The LibreNMS port row.

    Returns:
        bool: The enabled value a sync would write.

    """
    admin_status = port.get("ifAdminStatus")
    if admin_status is None:
        return True
    if isinstance(admin_status, str):
        return admin_status.lower() == "up"
    return bool(admin_status)


def synced_description(port, model=None):
    """
    Return the description a sync writes from a LibreNMS port's ifAlias.

    An alias echoing either canonical name is not a description, and the stored value is clipped
    to the NetBox column, so both rules are applied here as well as in the writer.

    Args:
        port (dict): The LibreNMS port row.
        model (type | None): Concrete interface model whose column bounds the text.

    Returns:
        str: The description value a sync would write.

    """
    alias = port.get("ifAlias")
    echoes_name = alias in (port.get("ifDescr"), port.get("ifName"))
    usable_alias = alias if isinstance(alias, str) and not echoes_name else ""
    return bounded_interface_text("description", usable_alias, model)


def syncable_mac_address(mac_address):
    """
    Return the MAC a sync would write, or None when LibreNMS reported nothing usable.

    NetBox's own field decides: the macaddr column rejects whatever netaddr cannot parse, so a
    value it refuses can never reach the interface and must not be reported as a difference.

    Args:
        mac_address (object): The raw LibreNMS ifPhysAddress value.

    Returns:
        str | None: The MAC to write, or None when there is nothing to write.

    """
    if not isinstance(mac_address, str) or not mac_address.strip():
        return None
    try:
        MACAddressField().to_python(mac_address)
    except ValidationError:
        return None
    return mac_address


def parse_vlan_group_id(group_id_str):
    """Normalise a VLAN group ID from the row's selection map to int or None."""
    return int(group_id_str) if group_id_str else None


def _name_differs(port, interface, context):
    return port.get(context.interface_name_field) != interface.name


def _type_differs(port, interface, context):
    # The writer's rule: an unmapped ifType is no opinion, so it only fills a type-less interface.
    current = getattr(interface, "type", None)
    if context.netbox_type is not None:
        return current != context.netbox_type
    return not current


def _speed_differs(port, interface, context):
    return convert_speed_to_kbps(port.get("ifSpeed")) != interface.speed


def _description_differs(port, interface, context):
    return synced_description(port, type(interface)) != interface.description


def _mtu_differs(port, interface, context):
    return coerce_interface_mtu(port.get("ifMtu")) != interface.mtu


def _enabled_differs(port, interface, context):
    return interface_enabled_from_port(port) != interface.enabled


def _mac_address_differs(port, interface, context):
    mac_address = syncable_mac_address(port.get("ifPhysAddress"))
    if mac_address is None:
        # The writer skips a MAC it cannot store, so it cannot change anything.
        return False
    existing = next((mac for mac in interface.mac_addresses.all() if mac.mac_address == mac_address), None)
    if existing is None:
        return True
    if hasattr(interface, "primary_mac_address_id"):
        return interface.primary_mac_address_id != existing.pk
    return False


def _librenms_id_differs(port, interface, context):
    port_id = normalize_librenms_port_id(port.get("port_id"))
    if port_id is None:
        return False
    stored = get_librenms_device_id(interface, context.server_key, auto_save=False)
    return stored is None or str(stored) != str(port_id)


def _expected_vlan_mode(port, reported_tagged, reported_untagged):
    """Return the 802.1Q mode a sync writes, following ifTrunk before the VLAN lists."""
    if reported_tagged or port.get("mode") == "tagged":
        return "tagged"
    if reported_untagged:
        return "access"
    return None


def _vlan_group_mismatch(vlan_type, vid, context):
    """Return whether the group selected for one VLAN differs from the group NetBox assigned."""
    selected_group_id = parse_vlan_group_id(context.group_map.get(vid, {}).get("group_id", ""))
    return not check_vlan_group_matches(
        vlan_type,
        vid,
        selected_group_id,
        context.netbox_untagged_group_id,
        context.netbox_tagged_group_ids,
        context.netbox_untagged_vid,
        context.netbox_tagged_vids,
    )


def _vlans_differ(port, interface, context):
    vlan_context = context.vlan_context
    if vlan_context is None:
        return False
    reported_untagged = port.get("untagged_vlan")
    reported_tagged = list(port.get("tagged_vlans") or [])

    if (interface.mode or None) != _expected_vlan_mode(port, reported_tagged, reported_untagged):
        return True

    # A VLAN that resolves to no NetBox group is left out of the write, so it is compared as
    # absent rather than as the value the sync would store.
    expected_untagged = reported_untagged
    if not reported_untagged or reported_untagged in vlan_context.missing:
        expected_untagged = None
    if expected_untagged != vlan_context.netbox_untagged_vid:
        return True
    if expected_untagged is not None and _vlan_group_mismatch("U", expected_untagged, vlan_context):
        return True

    expected_tagged = {vid for vid in reported_tagged if vid not in vlan_context.missing}
    if expected_tagged != vlan_context.netbox_tagged_vids:
        return True
    return any(_vlan_group_mismatch("T", vid, vlan_context) for vid in expected_tagged)


_RULES = {
    "name": _name_differs,
    "type": _type_differs,
    "speed": _speed_differs,
    "description": _description_differs,
    "mtu": _mtu_differs,
    "enabled": _enabled_differs,
    "mac_address": _mac_address_differs,
    "librenms_id": _librenms_id_differs,
    "vlans": _vlans_differ,
}


def compute_row_sync_state(port, *, interface_name_field, server_key, netbox_type=None, vlan_context=None):
    """
    Return the sync state of one LibreNMS row against the NetBox interface it resolved to.

    Args:
        port (dict): The interface table row, carrying ``netbox_interface`` and ``exists_in_netbox``.
        interface_name_field (str): The LibreNMS port field currently acting as the interface name.
        server_key (str): The LibreNMS server the row's port_id belongs to.
        netbox_type (str | None): The NetBox type the sync would write, or None when the row's
            ifType has no mapping and the sync therefore holds no opinion.
        vlan_context: The row's VLAN evidence bundled with the NetBox assignment it is compared
            against, or None to leave VLANs out of the comparison.

    Returns:
        RowSyncState: The row state and each field's verdict.

    """
    interface = port.get("netbox_interface")
    if not port.get("exists_in_netbox") or interface is None:
        return RowSyncState(ROW_ABSENT, dict.fromkeys(SYNC_FIELDS, ABSENT))

    context = DiffContext(
        interface_name_field=interface_name_field,
        server_key=server_key,
        netbox_type=netbox_type,
        vlan_context=vlan_context,
    )
    fields = {}
    for field in SYNC_FIELDS:
        if not hasattr(interface, _REQUIRED_ATTRIBUTE[field]):
            fields[field] = MATCHES
            continue
        fields[field] = DIFFERS if _RULES[field](port, interface, context) else MATCHES
    state = ROW_DIFFERS if DIFFERS in fields.values() else ROW_IN_SYNC
    return RowSyncState(state, fields)
