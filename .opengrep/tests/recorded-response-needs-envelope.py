from netbox_librenms_plugin.data_shapes.envelope import unwrap_response


def legacy_pair_reader(value):
    # ruleid: recorded-response-needs-envelope
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        return value[1]
    return value


def legacy_list_reader(body):
    # ruleid: recorded-response-needs-envelope
    return body[1] if isinstance(body, list) else body


def envelope_reader(value):
    # ok: recorded-response-needs-envelope
    return unwrap_response(value)[1]


def list_rows(rows):
    # ok: recorded-response-needs-envelope
    return rows if isinstance(rows, list) else []


def first_of_pair(pair):
    # ok: recorded-response-needs-envelope
    return pair[0] if isinstance(pair, list) and len(pair) == 2 else None
