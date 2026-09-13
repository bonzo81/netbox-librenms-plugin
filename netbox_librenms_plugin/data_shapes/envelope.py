"""
One definition of how a recorded response carries its HTTP status.

A non-2xx response used to be stored as ``[status, body]``. A LibreNMS body can itself be a
two-element list whose first item is an integer, so that framing was ambiguous: the reader took
the first item as the status and dropped it, registering nonsense such as HTTP 1. The status now
travels in a tagged object, which no JSON body from LibreNMS can be mistaken for.

Every reader and writer of the format goes through here, so the two sides cannot drift.
"""

STATUS_KEY = "__http_status__"
BODY_KEY = "body"


def _is_int(value):
    """Return whether *value* is a real integer. bool is an int subclass and is not one."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_status_envelope(value):
    """
    Return whether *value* is a tagged status envelope.

    Both keys are required. A body would have to carry the reserved status key AND a "body" key
    to be mistaken for one, and an envelope written without its body is not silently read as an
    empty error.
    """
    return isinstance(value, dict) and _is_int(value.get(STATUS_KEY)) and BODY_KEY in value


def is_malformed_status_envelope(value):
    """
    Return whether *value* carries the reserved status key without a body.

    A complete envelope always has both keys. A half-written one would otherwise read as a
    successful body that happens to contain the reserved key.
    """
    return isinstance(value, dict) and _is_int(value.get(STATUS_KEY)) and BODY_KEY not in value


def wrap_response(status, body):
    """Return the value to store for a response, tagging the status only when it is not 2xx."""
    if 200 <= status < 300:
        return body
    return {STATUS_KEY: status, BODY_KEY: body}


def unwrap_response(value):
    """
    Return ``(status, body)`` for a stored response value.

    Anything that is not a tagged envelope is the body of a 2xx response, including a plain list.
    """
    if is_status_envelope(value):
        return value[STATUS_KEY], value.get(BODY_KEY)
    return 200, value
