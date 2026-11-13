from netbox_librenms_plugin.serial_identity import SerialRules


def unshared_batch(rows, api):
    for row in rows:
        # ruleid: batch-validation-shares-serial-rules
        validate_device_for_import(row, api=api)


def shared_batch(rows, api):
    serial_rules = SerialRules.load()
    for row in rows:
        # ok: batch-validation-shares-serial-rules
        validate_device_for_import(row, api=api, serial_rules=serial_rules)


def one_device(row, api):
    # ok: batch-validation-shares-serial-rules
    validate_device_for_import(row, api=api)


def polling_batch(queue, api):
    while queue:
        # ruleid: batch-validation-shares-serial-rules
        validate_device_for_import(queue.pop(), api=api)
