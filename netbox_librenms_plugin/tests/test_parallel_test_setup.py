"""Tests for isolated parallel test workers."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from netbox_librenms_plugin.tests.isolated_settings import TEST_DB_NAME_PREFIX
from netbox_librenms_plugin.tests.parallel import (
    MAX_PARALLEL_WORKERS,
    isolated_redis_databases,
    isolated_test_database_name,
)


REPOSITORY_ROOT = Path(__file__).parents[2]


def _pytest_plugins_lines(source):
    """Return the line of every module-level ``pytest_plugins`` assignment in *source*."""
    import ast

    lines = []
    for node in ast.parse(source).body:
        # An annotated assignment registers the plugin just the same, and has one target.
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "pytest_plugins" for target in targets):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(
    "source",
    ['pytest_plugins = ["helpers"]', 'pytest_plugins: list[str] = ["helpers"]'],
    ids=["plain", "annotated"],
)
def test_the_plugin_scan_sees_both_assignment_forms(source):
    """pytest honours the annotated form too, and the scan below only knew the plain one."""
    assert _pytest_plugins_lines(source) == [1]


def test_no_test_module_registers_a_session_wide_plugin():
    """``pytest_plugins`` in a test module registers that plugin for the whole session.

    Any autouse fixture it carries then applies to every test file collected after it. A
    helper's config mock reached the virtual-chassis tests that way and pinned
    PLUGINS_CONFIG to a default-only server map, which only failed in a full-suite run.
    """
    tests_directory = Path(__file__).parent
    offenders = []
    for path in sorted(tests_directory.rglob("test_*.py")):
        offenders.extend(
            f"{path.relative_to(REPOSITORY_ROOT)}:{line}" for line in _pytest_plugins_lines(path.read_text())
        )

    assert offenders == [], (
        "pytest_plugins registers a plugin session-wide. Bind the fixture into the module "
        "instead, e.g. `mock_librenms_config = test_librenms_api_helpers.mock_librenms_config`. "
        f"Found: {', '.join(offenders)}"
    )


def test_xdist_worker_gets_private_postgresql_and_redis_databases():
    """Assign one PostgreSQL database and two Redis databases to a worker."""
    assert isolated_test_database_name("test_netbox_librenms", "gw3") == "test_netbox_librenms_gw3"
    assert isolated_redis_databases("gw3") == (3, MAX_PARALLEL_WORKERS + 3)


def test_serial_run_keeps_default_database_targets():
    """Keep the caller's targets when pytest does not use xdist."""
    assert isolated_test_database_name("test_netbox_librenms", None) == "test_netbox_librenms"
    assert isolated_redis_databases(None) == (0, 1)


def test_database_name_stays_within_postgresql_limit():
    """Keep a worker suffix when the base name reaches PostgreSQL's limit."""
    database_name = isolated_test_database_name(f"test_{'x' * 70}", "gw7")

    assert len(database_name) == 63
    assert database_name.endswith("_gw7")


def test_more_than_the_supported_workers_is_rejected():
    """Reject workers that cannot receive a private Redis database pair."""
    # Derived from the cap: a hardcoded id becomes a valid worker the moment the cap is raised.
    first_unsupported = f"gw{MAX_PARALLEL_WORKERS}"
    with pytest.raises(ValueError, match=f"At most {MAX_PARALLEL_WORKERS} pytest workers are supported"):
        isolated_redis_databases(first_unsupported)


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


def test_local_and_ci_commands_use_the_supported_worker_count():
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


def test_settings_module_exports_the_stripped_redis_host():
    """A padded host must reach the Redis client cleaned, not fail later at connect time."""
    script = (
        "import os, importlib; "
        "os.environ['DJANGO_SETTINGS_MODULE'] = 'netbox_librenms_plugin.tests.isolated_settings'; "
        "importlib.import_module('netbox_librenms_plugin.tests.isolated_settings'); "
        "print('HOST=' + repr(os.environ['REDIS_HOST'])); "
        "print('CACHE_HOST=' + repr(os.environ['REDIS_CACHE_HOST']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            # pytest injects the NetBox source path from pyproject; a bare subprocess does not.
            "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
            "TEST_DB_NAME": "test_netbox_librenms",
            "TEST_REDIS_HOST": "  redis  ",
        },
        cwd=REPOSITORY_ROOT,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "HOST='redis'" in result.stdout, result.stdout
    assert "CACHE_HOST='redis'" in result.stdout, result.stdout


def test_location_mapping_bulk_import_url_resolves():
    """The explicit import route must reach the location mapping form."""
    from django.urls import resolve, reverse

    from netbox_librenms_plugin.forms import LocationMappingImportForm
    from netbox_librenms_plugin.views.mapping_views import LocationMappingBulkImportView

    match = resolve(reverse("plugins:netbox_librenms_plugin:locationmapping_bulk_import"))
    assert match.func.view_class is LocationMappingBulkImportView
    assert match.func.view_class.model_form is LocationMappingImportForm


def test_test_alias_preserves_the_calling_shell(tmp_path):
    """A test invocation must preserve its caller's directory and environment."""
    import venv

    virtual_environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(virtual_environment)
    script = "\n".join(
        (
            # Redirect only the container's activation path. Source the real alias script
            # in place so its repository discovery and shell function remain under test.
            "source() {",
            '  if [ "$1" = /opt/netbox/venv/bin/activate ]; then',
            '    builtin source "$ALIAS_TEST_VENV/bin/activate"',
            "  else",
            '    builtin source "$@"',
            "  fi",
            "}",
            f'source "{REPOSITORY_ROOT}/.devcontainer/scripts/load-aliases.sh"',
            "unset VIRTUAL_ENV",
            'original_path="$PATH"',
            'original_directory="$PWD"',
            "pytest() {",
            '  test "$PLUGIN_DIR" = "$ALIAS_TEST_REPOSITORY" &&',
            '    test "$PWD" = "$ALIAS_TEST_REPOSITORY" &&',
            '    test "$VIRTUAL_ENV" = "$ALIAS_TEST_VENV" && return 23',
            "}",
            "netbox-test",
            "result=$?",
            'test "$result" = 23 || exit 1',
            'test "$PWD" = "$original_directory" || exit 2',
            'test "$PATH" = "$original_path" || exit 3',
            'test -z "${VIRTUAL_ENV:-}" || exit 4',
        )
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "TEST_DB_NAME": "test_alias_contract",
            "TEST_REDIS_HOST": "redis-alias-contract",
            "ALIAS_TEST_VENV": str(virtual_environment),
            "ALIAS_TEST_REPOSITORY": str(REPOSITORY_ROOT),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.django_db
def test_reused_database_restores_inventory_and_serial_seed_rules():
    """A transactional flush must not remove the newer migration seeds permanently."""
    import importlib

    from netbox_librenms_plugin.models import InventoryIgnoreRule, NormalizationRule
    from netbox_librenms_plugin.tests.conftest import seed_migration_rows

    migration = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")
    inventory = {"name": migration.DEFAULT_RULE["name"]}
    serial = {key: migration.SERIAL_RULE[key] for key in ("scope", "match_pattern")}
    InventoryIgnoreRule.objects.filter(**inventory).delete()
    NormalizationRule.objects.filter(**serial).delete()
    seed_migration_rows()
    assert InventoryIgnoreRule.objects.filter(**inventory).exists()
    assert NormalizationRule.objects.filter(**serial).exists()

def test_isolated_settings_exclude_unrelated_installed_plugins(settings):
    """Do not import sibling worktrees while resolving URLs for this plugin's tests."""
    assert settings.PLUGINS == ["netbox_librenms_plugin"]
    assert set(settings.PLUGINS_CONFIG) == {"netbox_librenms_plugin"}


def test_librenms_config_mock_is_not_applied_to_unrelated_tests(settings):
    """A helper plugin must not replace the configured server catalog globally."""
    from copy import deepcopy

    from netbox.plugins import get_plugin_config

    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "isolated": {
            "librenms_url": "https://isolated.example.com",
            "api_token": "test-token",
        }
    }
    settings.PLUGINS_CONFIG = plugin_config

    assert get_plugin_config("netbox_librenms_plugin", "servers") == plugin_config["netbox_librenms_plugin"]["servers"]
