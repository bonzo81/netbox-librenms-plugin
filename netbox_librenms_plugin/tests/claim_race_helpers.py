"""Shared helpers for real database LibreNMS identity claim races."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TypeVar

from django.db import close_old_connections, connection


_Result = TypeVar("_Result")


def run_librenms_id_claim_race(*operations: Callable[[], _Result]) -> tuple[list[_Result], list[int]]:
    """Run claim operations concurrently and return their results and lock keys."""
    claim_barrier = Barrier(len(operations))
    claim_keys = []

    def wait_for_competing_claim(execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql:
            claim_keys.append(params[0])
            claim_barrier.wait(timeout=5)
        return execute(sql, params, many, context)

    def run(operation):
        close_old_connections()
        try:
            with connection.execute_wrapper(wait_for_competing_claim):
                return operation()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(run, operation) for operation in operations]
        outcomes = [future.result(timeout=30) for future in futures]

    return outcomes, claim_keys
