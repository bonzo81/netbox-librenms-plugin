"""Tests for isolated parallel test workers."""

import os
import subprocess
from pathlib import Path

import pytest

from netbox_librenms_plugin.tests.isolated_settings import TEST_DB_NAME_PREFIX
from netbox_librenms_plugin.tests.parallel import (
    MAX_PARALLEL_WORKERS,
    isolated_redis_databases,
    isolated_test_database_name,
)


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

    assert 'parallel_args=(-n "$workers" --maxschedchunk=1)' in aliases
    assert f"pytest -n {MAX_PARALLEL_WORKERS} --maxschedchunk=1" in workflow


def _run_netbox_test_alias(worker_value=None, *, db_name="test_alias_contract", redis_host="redis-alias-contract"):
    """Run the local test alias with pytest and the venv activation stubbed out."""
    script = "\n".join(
        (
            f'source "{REPOSITORY_ROOT}/.devcontainer/scripts/load-aliases.sh"',
            "source() { :; }",  # skip the venv activation
            "pytest() { printf 'PYTEST %s\\n' \"$*\"; }",
            "netbox-test",
            'printf "STATUS %s\\n" "$?"',
        )
    )
    environment = {
        **os.environ,
        "TEST_DB_NAME": db_name,
        "TEST_REDIS_HOST": redis_host,
    }
    if worker_value is None:
        environment.pop("NETBOX_TEST_WORKERS", None)
    else:
        environment["NETBOX_TEST_WORKERS"] = worker_value
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        cwd=REPOSITORY_ROOT,
        check=False,
    )


def test_test_alias_defaults_to_the_supported_worker_count():
    """The alias must request exactly the workers the isolation helper can serve."""
    result = _run_netbox_test_alias()

    assert "STATUS 0" in result.stdout
    assert f"-n {MAX_PARALLEL_WORKERS} --maxschedchunk=1" in result.stdout


@pytest.mark.parametrize("worker_value", [str(MAX_PARALLEL_WORKERS + 1), "0", "two"])
def test_test_alias_rejects_worker_counts_without_isolated_databases(worker_value):
    """Reject the value before xdist starts a worker that cannot get its own databases."""
    result = _run_netbox_test_alias(worker_value)

    assert "STATUS 2" in result.stdout
    assert "PYTEST" not in result.stdout
    assert f"NETBOX_TEST_WORKERS must be an integer from 1 through {MAX_PARALLEL_WORKERS}." in result.stderr


def test_test_alias_treats_an_empty_worker_value_as_unset():
    """An empty variable must select the default instead of failing the run."""
    result = _run_netbox_test_alias("")

    assert "STATUS 0" in result.stdout
    assert f"-n {MAX_PARALLEL_WORKERS} --maxschedchunk=1" in result.stdout


def test_test_alias_rejects_a_database_name_the_settings_module_refuses():
    """Reject the name here instead of failing later while the settings module loads."""
    result = _run_netbox_test_alias(db_name="netbox_alias_contract")

    assert "STATUS 1" in result.stdout
    assert "PYTEST" not in result.stdout
    assert f"TEST_DB_NAME must start with '{TEST_DB_NAME_PREFIX}'." in result.stderr


def test_test_alias_rejects_a_blank_redis_host():
    """A whitespace-only host reaches the settings module as no host at all."""
    result = _run_netbox_test_alias(redis_host="   ")

    assert "STATUS 1" in result.stdout
    assert "PYTEST" not in result.stdout
    assert "TEST_REDIS_HOST must not be empty." in result.stderr


@pytest.mark.django_db(transaction=True)
def test_custom_field_restore_drops_stale_content_type_cache(caplog):
    """Repair the custom field after a worker cached a ContentType from another DB state."""
    import logging

    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType

    from netbox_librenms_plugin import _ensure_librenms_id_custom_field

    db_alias = "default"
    _ensure_librenms_id_custom_field._executed_aliases.discard(db_alias)
    ContentType.objects.clear_cache()

    interface_type = ContentType.objects.db_manager(db_alias).get_for_model(Interface)
    stale_pk = (ContentType.objects.using(db_alias).order_by("-pk").values_list("pk", flat=True).first() or 0) + 10000
    stale_type = ContentType(
        pk=stale_pk,
        app_label=interface_type.app_label,
        model=interface_type.model,
    )
    # Couples to Django's private ContentTypeManager._cache layout ({alias: {(app_label, model)}}),
    # which the test matrix pins to Django 5.1 and 6.0. If a later release reshapes it, seed the
    # cache through ContentType.objects._add_to_cache(db_alias, stale_type) instead.
    ContentType.objects._cache.setdefault(db_alias, {})[(stale_type.app_label, stale_type.model)] = stale_type

    assert ContentType.objects.db_manager(db_alias).get_for_model(Interface) is stale_type

    with caplog.at_level(logging.ERROR, logger="netbox_librenms_plugin"):
        _ensure_librenms_id_custom_field(sender=None, using=db_alias)

    try:
        assert "Failed to auto-create 'librenms_id' custom field" not in caplog.text
        assert db_alias in _ensure_librenms_id_custom_field._executed_aliases
    finally:
        ContentType.objects.clear_cache()
