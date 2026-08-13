"""Tests for isolated parallel test workers."""

import os
from pathlib import Path

import pytest

from netbox_librenms_plugin.tests.parallel import isolated_redis_databases, isolated_test_database_name


REPOSITORY_ROOT = Path(__file__).parents[2]


def test_xdist_worker_gets_private_postgresql_and_redis_databases():
    """Assign one PostgreSQL database and two Redis databases to a worker."""
    assert isolated_test_database_name("test_netbox_librenms", "gw3") == "test_netbox_librenms_gw3"
    assert isolated_redis_databases("gw3") == (3, 11)


def test_serial_run_keeps_default_database_targets():
    """Keep the caller's targets when pytest does not use xdist."""
    assert isolated_test_database_name("test_netbox_librenms", None) == "test_netbox_librenms"
    assert isolated_redis_databases(None) == (0, 1)


def test_database_name_stays_within_postgresql_limit():
    """Keep a worker suffix when the base name reaches PostgreSQL's limit."""
    database_name = isolated_test_database_name(f"test_{'x' * 70}", "gw7")

    assert len(database_name) == 63
    assert database_name.endswith("_gw7")


def test_more_than_eight_workers_is_rejected():
    """Reject workers that cannot receive a private Redis database pair."""
    with pytest.raises(ValueError, match="At most 8 pytest workers are supported"):
        isolated_redis_databases("gw8")


@pytest.mark.django_db
def test_active_worker_uses_its_private_database_targets(settings):
    """Apply the worker identity to the real Django database and Redis settings."""
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    tasks_database, cache_database = isolated_redis_databases(worker_id)

    assert settings.DATABASES["default"]["TEST"]["NAME"] == isolated_test_database_name(
        os.environ["TEST_DB_NAME"],
        worker_id,
    )
    assert settings.RQ_QUEUES["default"]["DB"] == tasks_database
    assert settings.CACHES["default"]["LOCATION"].endswith(f"/{cache_database}")


def test_local_and_ci_commands_use_eight_workers():
    """Keep local and CI test entry points on the supported worker count."""
    aliases = (REPOSITORY_ROOT / ".devcontainer/scripts/load-aliases.sh").read_text()
    workflow = (REPOSITORY_ROOT / ".github/workflows/test.yaml").read_text()

    assert 'local workers="${NETBOX_TEST_WORKERS:-8}"' in aliases
    assert 'parallel_args=(-n "$workers" --maxschedchunk=1)' in aliases
    assert "pytest -n 8 --maxschedchunk=1" in workflow
