from dcim.models import Device, Module
from django.db.models import Q

from netbox_librenms_plugin.serial_identity import Comparison, SerialRules, compare, display, group
# ruleid: serial-identity-through-owner
from netbox_librenms_plugin.utils import normalize_stack_serial
# ruleid: serial-identity-through-owner
from netbox_librenms_plugin.import_utils.virtual_chassis import vc_serial_key as key_for


def stored_attribute(device, incoming):
    # ruleid: serial-identity-through-owner
    return device.serial == incoming


def incoming_first(device, incoming):
    # ruleid: serial-identity-through-owner
    return incoming != device.serial


def ancestor_walk(current, item_serial):
    """modules_view.py: the first ancestor serial compared with the item serial, both locals."""
    ancestor_serial = current.get("entPhysicalSerialNum")
    # ruleid: serial-identity-through-owner
    return ancestor_serial == item_serial


def lowered_inventory_field(item, serial):
    """modules_view.py: an inventory field, lowercased, compared with a serial."""
    # ruleid: serial-identity-through-owner
    return item.get("entPhysicalSerialNum").strip().lower() == serial


def grouped_rows(table_data):
    """modules_view.py: rows grouped by their serial text in a dict."""
    serial_rows = {}
    for row in table_data:
        serial = row.get("serial", "")
        # ruleid: serial-identity-through-owner
        serial_rows.setdefault(serial, []).append(row)
    return serial_rows


def keyed_members(members, by_serial):
    for member in members:
        # ruleid: serial-identity-through-owner
        by_serial[member.serial] = member


def stack_fingerprint(vc_data):
    """bulk_import.py: a stack keyed by its sorted member serials."""
    # ruleid: serial-identity-through-owner
    return sorted(serial for member in vc_data["members"] if (serial := normalize_stack_serial(member["serial"])))


def orm_lookups(serial, serials):
    # ruleid: serial-identity-through-owner
    Device.objects.filter(serial=serial)
    # ruleid: serial-identity-through-owner
    Module.objects.filter(device_id=1, serial__in=serials)
    # ruleid: serial-identity-through-owner
    Device.objects.exclude(serial__iexact=serial)
    # ruleid: serial-identity-through-owner
    return Device.objects.filter(Q(serial=serial) | Q(name="x"))


def rules_applied(value, manufacturer):
    # ruleid: serial-identity-through-owner
    apply_normalization_rules(value, "serial", manufacturer)
    # ruleid: serial-identity-through-owner
    return preload_normalization_rules("serial", manufacturer=manufacturer)


def owner_comparison(device, incoming):
    # ok: serial-identity-through-owner
    return compare(device.serial, incoming, SerialRules.load().for_device(device)) is Comparison.SAME


def owner_grouping(members, context):
    # ok: serial-identity-through-owner
    return group(members, context, serial=lambda member: member.serial)


def presentation(device):
    # ok: serial-identity-through-owner
    return {"serial": display(device.serial)}


def writes(device, module, serial):
    # ok: serial-identity-through-owner
    Module(device=device, serial=serial)
    # ok: serial-identity-through-owner
    device.serial = serial


def other_fields(result, source, match_type):
    # ok: serial-identity-through-owner
    serial_cable = source == "serial"
    # ok: serial-identity-through-owner
    serial_action = result["serial_action"] == "conflict"
    # ok: serial-identity-through-owner
    return serial_cable or serial_action or match_type == "serial"


def module_type_rules(name, manufacturer):
    # ok: serial-identity-through-owner
    return apply_normalization_rules(name, "module_type", manufacturer)
