"""Shared structure for server identities stored in ``librenms_id``."""

PREFERRED_SERVER_FIELD = "_preferred_server"
RESERVED_SERVER_KEYS = frozenset({PREFERRED_SERVER_FIELD})


def require_server_key(server_key: str) -> str:
    """Return a normalized server identity key or reject reserved metadata."""
    if not isinstance(server_key, str) or not server_key.strip():
        raise ValueError("LibreNMS server key must be a non-empty string.")
    normalized = server_key.strip()
    if normalized in RESERVED_SERVER_KEYS:
        raise ValueError(f"LibreNMS server key {normalized!r} is reserved for object metadata.")
    return normalized


def iter_server_mapping_entries(value):
    """Yield only server identity entries from a ``librenms_id`` mapping."""
    if not isinstance(value, dict):
        return
    for server_key, entry in value.items():
        if isinstance(server_key, str) and server_key not in RESERVED_SERVER_KEYS:
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
