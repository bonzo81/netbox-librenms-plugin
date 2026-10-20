"""Tests for the shared LibreNMS identity claim race helper."""

from contextlib import contextmanager
from threading import local

import pytest

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

        return self.state.wrapper(execute, "SELECT pg_try_advisory_xact_lock(%s)", [key], False, None)

    def close(self):
        pass


@pytest.mark.django_db
def test_the_observer_watches_the_statement_of_the_real_device_claim():
    """The helper's SQL and key match what a real claim runs, so a race test cannot miss the claim."""
    from django.db import connection, transaction
    from django.test.utils import CaptureQueriesContext

    from netbox_librenms_plugin.server_mappings import lock_librenms_id_assignment

    with CaptureQueriesContext(connection) as queries, transaction.atomic():
        lock_librenms_id_assignment(61901, "default")

    claims = [query["sql"] for query in queries.captured_queries if claim_race_helpers.DEVICE_CLAIM_SQL in query["sql"]]
    assert claims == [f"SELECT pg_try_advisory_xact_lock({claim_race_helpers.device_claim_key('default', 61901)})"]


def test_claim_race_tracks_only_the_first_advisory_lock_per_operation(monkeypatch):
    """A later claim lock must not enter the claim barrier again."""
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
