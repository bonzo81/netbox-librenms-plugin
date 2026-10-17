"""Shared helpers for real database LibreNMS identity claim races."""

from collections.abc import Callable
from contextlib import contextmanager
from functools import partial
from threading import Barrier
from typing import TypeVar

from django.db import close_old_connections, connection

from netbox_librenms_plugin.tests.conftest import run_in_threads
from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_timeout, second_connection

_Result = TypeVar("_Result")

# The device claim takes this lock without a wait; a claim that meets a held one refuses at once.
DEVICE_CLAIM_SQL = "pg_try_advisory_xact_lock"


def device_claim_key(server_key, librenms_id):
    """Return the advisory lock key of one device identity claim."""
    from netbox_librenms_plugin.utils import advisory_lock_key

    return advisory_lock_key(f"netbox-librenms-plugin:librenms-id:{server_key}:{librenms_id}")


@contextmanager
def held_device_claim(server_key, librenms_id):
    """
    Hold one device identity claim in a second connection until the block ends.

    A claim on the test's connection that waits for the held one fails after one second, so a
    claim that blocks fails the test and does not hang it.
    """
    with second_connection() as other, lock_timeout(1000):
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [device_claim_key(server_key, librenms_id)])
        yield other


def run_librenms_id_claim_race(*operations: Callable[[], _Result]) -> tuple[list[_Result], list[int]]:
    """Run claim operations concurrently and return their results and lock keys."""
    claim_barrier = Barrier(len(operations))
    claim_keys = []

    def run(operation):
        claim_observed = False

        def wait_for_competing_claim(execute, sql, params, many, context):
            nonlocal claim_observed
            if DEVICE_CLAIM_SQL in sql and not claim_observed:
                claim_observed = True
                claim_keys.append(params[0])
                claim_barrier.wait(timeout=5)
            return execute(sql, params, many, context)

        close_old_connections()
        try:
            with connection.execute_wrapper(wait_for_competing_claim):
                return operation()
        finally:
            connection.close()

    outcomes = run_in_threads(
        *(partial(run, operation) for operation in operations), barriers=(claim_barrier,), timeout=30
    )

    return outcomes, claim_keys
