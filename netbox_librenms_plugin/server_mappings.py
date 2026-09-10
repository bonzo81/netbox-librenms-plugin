"""Shared structure for server identities stored in ``librenms_id``."""

from django.db.models import JSONField

PREFERRED_SERVER_FIELD = "_preferred_server"
RESERVED_SERVER_KEYS = frozenset({PREFERRED_SERVER_FIELD})
JSON_FIELD_LOOKUP_NAMES = frozenset(JSONField.get_lookups())


def require_server_key(server_key: str) -> str:
    """Return a safe server identity key or reject invalid metadata paths."""
    if not isinstance(server_key, str) or not server_key.strip():
        raise ValueError("LibreNMS server key must be a non-empty string.")
    if server_key != server_key.strip():
        raise ValueError("LibreNMS server key must not contain leading or trailing whitespace.")
    if "__" in server_key:
        raise ValueError("LibreNMS server key must not contain '__'.")
    if server_key in JSON_FIELD_LOOKUP_NAMES:
        raise ValueError(f"LibreNMS server key {server_key!r} conflicts with a Django JSON lookup.")
    if server_key in RESERVED_SERVER_KEYS:
        raise ValueError(f"LibreNMS server key {server_key!r} is reserved for object metadata.")
    return server_key


def is_server_key(value) -> bool:
    """Return whether *value* is usable as a server identity key."""
    try:
        require_server_key(value)
    except ValueError:
        return False
    return True


def iter_server_mapping_entries(value):
    """Yield only server identity entries from a ``librenms_id`` mapping."""
    if not isinstance(value, dict):
        return
    for server_key, entry in value.items():
        # Hand-edited custom-field data can hold a key the validator rejects; every reader below
        # would raise on it, so drop it here instead of breaking server discovery.
        if is_server_key(server_key):
            yield server_key, entry


def with_preferred_server(value, server_key: str) -> dict:
    """Return a copy of a mapping with only its preference metadata changed."""
    server_key = require_server_key(server_key)
    if not isinstance(value, dict):
        raise ValueError("Object does not have a server-scoped LibreNMS mapping.")
    updated = dict(value)
    updated[PREFERRED_SERVER_FIELD] = server_key
    return updated


def without_server_mapping(value, server_key: str) -> dict:
    """Return a copy without one server identity and its matching preference."""
    server_key = require_server_key(server_key)
    if not isinstance(value, dict):
        return {}
    updated = dict(value)
    updated.pop(server_key, None)
    if updated.get(PREFERRED_SERVER_FIELD) == server_key:
        updated.pop(PREFERRED_SERVER_FIELD, None)
    return updated


class SameServerIdentityConflict(ValueError):
    """The active server already maps this object to a different LibreNMS host ID."""

    def __init__(self, server_key: str, current_host_id: int, proposed_host_id: int):
        self.server_key = server_key
        self.current_host_id = current_host_id
        self.proposed_host_id = proposed_host_id
        super().__init__(
            f"LibreNMS server '{server_key}' is already mapped to host ID {current_host_id}. "
            f"Replacing it with {proposed_host_id} requires the separate replacement confirmation."
        )


class StaleIdentityReplacement(ValueError):
    """A confirmed replacement no longer matches the object's current mapping."""

    def __init__(self, server_key: str, current_host_id, expected_host_id: int):
        self.server_key = server_key
        self.current_host_id = current_host_id
        self.expected_host_id = expected_host_id
        current = "no host ID" if current_host_id is None else f"host ID {current_host_id}"
        super().__init__(
            f"The replacement confirmation no longer matches the current mapping: server "
            f"'{server_key}' now has {current}, not host ID {expected_host_id}. "
            "Re-run the action to get a fresh confirmation."
        )
