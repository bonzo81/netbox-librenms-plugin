import functools


def unmemoized_batch(rows, api):
    serial_key_for = functools.cache(vc_serial_key)
    for row in rows:
        # ruleid: batch-validation-needs-candidate-keys
        validate_device_for_import(row, api=api, serial_key_for=serial_key_for)


def memoized_batch(rows, api):
    serial_key_for = functools.cache(vc_serial_key)
    candidate_keys = functools.cache(candidate_vc_serial_keys)
    for row in rows:
        # ok: batch-validation-needs-candidate-keys
        validate_device_for_import(row, api=api, serial_key_for=serial_key_for, candidate_keys=candidate_keys)


def one_device(row, api):
    # ok: batch-validation-needs-candidate-keys
    validate_device_for_import(row, api=api)
