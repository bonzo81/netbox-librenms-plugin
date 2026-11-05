from netbox_librenms_plugin.import_utils import virtual_chassis
from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name, vc_serial_key


def _member_name(master_name, position, serial, pattern, serial_key):
    # ok: vc-member-name-through-helper
    return _generate_vc_member_name(master_name, position, serial=serial_key(serial), pattern=pattern)


def header_name(device, resolved_name):
    # ruleid: vc-member-name-through-helper
    return _generate_vc_member_name(resolved_name, device.vc_position, serial=device.serial)


def qualified_name(device, resolved_name):
    # ruleid: vc-member-name-through-helper
    return virtual_chassis._generate_vc_member_name(resolved_name, device.vc_position)


def keyed_name(device, resolved_name):
    # ok: vc-member-name-through-helper
    return _member_name(resolved_name, device.vc_position, device.serial, None, vc_serial_key())
