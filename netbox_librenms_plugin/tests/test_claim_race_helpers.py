"""Tests for the shared LibreNMS identity claim race helper."""

from contextlib import contextmanager
from threading import local

from netbox_librenms_plugin.tests import claim_race_helpers


class _ThreadLocalConnection:
    """Provide the connection methods used by the race helper."""

    def __init__(self):
        self.state = local()

    @contextmanager
    def execute_wrapper(self, wrapper):
        self.state.wrapper = wrapper
        try:
            yield
        finally:
            del self.state.wrapper

    def execute_claim(self, key):
        def execute(sql, params, many, context):
            return params[0]

        return self.state.wrapper(execute, "SELECT pg_advisory_xact_lock(%s)", [key], False, None)

    def close(self):
        pass


def test_claim_race_tracks_only_the_first_advisory_lock_per_operation(monkeypatch):
    """A later advisory lock must not enter the claim barrier again."""
    connection = _ThreadLocalConnection()
    monkeypatch.setattr(claim_race_helpers, "connection", connection)
    monkeypatch.setattr(claim_race_helpers, "close_old_connections", lambda: None)

    def take_two_locks():
        connection.execute_claim(0x4E42544C)
        connection.execute_claim(0x4E42544D)
        return True

    outcomes, claim_keys = claim_race_helpers.run_librenms_id_claim_race(take_two_locks, take_two_locks)

    assert outcomes == [True, True]
    assert claim_keys == [0x4E42544C, 0x4E42544C]
