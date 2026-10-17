import json

from netbox_librenms_plugin.data_shapes.envelope import unwrap_response


def legacy_pair_reader(value):
    # ruleid: recorded-response-needs-envelope
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        return value[1]
    return value


def legacy_list_reader(path, route):
    body = json.loads(path.read_text())["responses"][f"GET {route}"]
    # ruleid: recorded-response-needs-envelope
    return body[1] if isinstance(body, list) else body


def legacy_loop_reader(recording):
    for _key, value in recording.get("responses", {}).items():
        # ruleid: recorded-response-needs-envelope
        yield value[1] if isinstance(value, list) else value


def legacy_values_reader(recording):
    for value in recording["responses"].values():
        # ruleid: recorded-response-needs-envelope
        yield value[1] if isinstance(value, list) else value


def envelope_reader(value):
    # ok: recorded-response-needs-envelope
    return unwrap_response(value)[1]


def list_rows(rows):
    # ok: recorded-response-needs-envelope
    return rows if isinstance(rows, list) else []


def first_of_pair(pair):
    # ok: recorded-response-needs-envelope
    return pair[0] if isinstance(pair, list) and len(pair) == 2 else None


def second_or_scalar(value):
    # ok: recorded-response-needs-envelope
    return value[1] if isinstance(value, list) else value
