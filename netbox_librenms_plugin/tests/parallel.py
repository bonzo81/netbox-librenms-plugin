"""Isolation helpers for parallel pytest workers."""

import os
import re


MAX_PARALLEL_WORKERS = 8
_POSTGRES_NAME_LIMIT = 63
_WORKER_ID_PATTERN = re.compile(r"gw(?P<number>\d+)")


def pytest_xdist_auto_num_workers(config):
    """Cap `-n auto` at the worker count that still gets private Redis databases."""
    # Only the import is guarded: an error raised by a real hook must stay visible.
    try:
        from xdist.plugin import pytest_xdist_auto_num_workers as detected_num_workers
    except ImportError:
        workers = _detached_num_workers()
    else:
        workers = detected_num_workers(config)
    return min(workers, MAX_PARALLEL_WORKERS)


def _detached_num_workers():
    """Return the worker count to use when xdist no longer exposes its own default."""
    override = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS")
    if override is not None:
        try:
            parsed_override = int(override)
        except ValueError:
            pass
        else:
            if parsed_override > 0:
                return parsed_override
    return os.cpu_count() or 1


def isolated_test_database_name(base_name: str, worker_id: str | None) -> str:
    """Return a PostgreSQL-safe database name for one pytest worker."""
    suffix = f"_{worker_id}" if worker_id else ""
    return f"{base_name[: _POSTGRES_NAME_LIMIT - len(suffix)]}{suffix}"


def isolated_redis_databases(worker_id: str | None) -> tuple[int, int]:
    """Return private task and cache Redis databases for one pytest worker."""
    if worker_id is None:
        return 0, 1

    match = _WORKER_ID_PATTERN.fullmatch(worker_id)
    if match is None:
        raise ValueError(f"Unsupported pytest worker ID: {worker_id!r}.")

    worker_number = int(match.group("number"))
    if worker_number >= MAX_PARALLEL_WORKERS:
        raise ValueError(f"At most {MAX_PARALLEL_WORKERS} pytest workers are supported.")

    return worker_number, MAX_PARALLEL_WORKERS + worker_number
