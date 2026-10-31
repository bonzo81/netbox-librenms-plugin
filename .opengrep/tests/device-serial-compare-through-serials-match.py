from netbox_librenms_plugin.import_utils.virtual_chassis import chassis_serial_key, serials_match


def stored_first(device, incoming):
    # ruleid: device-serial-compare-through-serials-match
    return device.serial == incoming


def incoming_first(device, incoming):
    # ruleid: device-serial-compare-through-serials-match
    return incoming == device.serial


def stored_first_differs(device, incoming):
    # ruleid: device-serial-compare-through-serials-match
    return device.serial != incoming


def incoming_first_differs(device, incoming):
    # ruleid: device-serial-compare-through-serials-match
    return incoming != device.serial


def keyed(device, incoming):
    # ok: device-serial-compare-through-serials-match
    return serials_match(device.serial, incoming, chassis_serial_key(device))
