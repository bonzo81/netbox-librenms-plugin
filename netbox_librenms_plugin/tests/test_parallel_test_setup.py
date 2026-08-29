"""Tests for isolated parallel test workers."""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from django.test import override_settings

from netbox_librenms_plugin.tests.conftest import (
    _seeded_model_rows,
    restore_seeded_state,
)
from netbox_librenms_plugin.tests.isolated_settings import TEST_DB_NAME_PREFIX
from netbox_librenms_plugin.tests.parallel import (
    MAX_PARALLEL_WORKERS,
    isolated_redis_databases,
    isolated_test_database_name,
    pytest_xdist_auto_num_workers,
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


def test_location_mapping_bulk_import_url_resolves():
    """The explicit import route must reach the location mapping form."""
    from django.urls import resolve, reverse

    from netbox_librenms_plugin.forms import LocationMappingImportForm
    from netbox_librenms_plugin.views.mapping_views import LocationMappingBulkImportView

    match = resolve(reverse("plugins:netbox_librenms_plugin:locationmapping_bulk_import"))
    assert match.func.view_class is LocationMappingBulkImportView
    assert match.func.view_class.model_form is LocationMappingImportForm


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


# Import root -> the distribution name that provides it.
THIRD_PARTY_TEST_IMPORT_ROOTS = {
    "django": "django",
    "packaging": "packaging",
    "playwright": "playwright",
    "pytest": "pytest",
    "requests": "requests",
    "xdist": "pytest-xdist",
    "yaml": "pyyaml",
}

# Gating NetBox ref -> the Django version its requirements.txt pins.
NETBOX_REF_DJANGO_PINS = {
    "v4.4.0": "5.2.5",
    "v4.6.5": "6.0.7",
}


def _third_party_roots_the_test_suite_imports():
    """Return the third-party import roots the test modules use directly, at any indent."""
    pattern = re.compile(r"^[ \t]*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
    roots = set()
    for module in (REPOSITORY_ROOT / "netbox_librenms_plugin" / "tests").rglob("*.py"):
        roots.update(pattern.findall(module.read_text()))
    return roots & THIRD_PARTY_TEST_IMPORT_ROOTS.keys()


def test_packages_the_test_suite_imports_directly_are_declared():
    """Declare packages imported directly by the test suite."""
    requirements = (REPOSITORY_ROOT / "requirements_dev.txt").read_text().splitlines()
    names = {re.split(r"[<>=!~;\s]", requirement, maxsplit=1)[0].lower() for requirement in requirements}
    imported = _third_party_roots_the_test_suite_imports()
    assert imported, "the scan found no third-party import, so every name below would pass vacuously"

    undeclared = sorted(
        THIRD_PARTY_TEST_IMPORT_ROOTS[root] for root in imported if THIRD_PARTY_TEST_IMPORT_ROOTS[root] not in names
    )

    assert not undeclared, f"imported by the test suite but not declared in requirements_dev.txt: {undeclared}"


def test_declared_django_range_covers_every_gating_netbox_release():
    """The dev requirements are installed next to NetBox, so a narrower range downgrades its Django."""
    from packaging.requirements import Requirement

    workflow = yaml.safe_load((REPOSITORY_ROOT / ".github/workflows/test.yaml").read_text())
    matrix = workflow["jobs"]["test-netbox"]["strategy"]["matrix"]["include"]
    gating_refs = sorted({entry["netbox-ref"] for entry in matrix if not entry["experimental"]})
    assert gating_refs, "the matrix scan found no gating NetBox ref, so the checks below would pass vacuously"

    unmapped = [ref for ref in gating_refs if ref not in NETBOX_REF_DJANGO_PINS]
    assert not unmapped, f"record the Django pin of each gating NetBox ref in NETBOX_REF_DJANGO_PINS: {unmapped}"

    declared = (REPOSITORY_ROOT / "requirements_dev.txt").read_text().splitlines()
    requirements = [Requirement(line) for line in declared if line and not line.startswith(("#", "-"))]
    django = next(requirement for requirement in requirements if requirement.name.lower() == "django")

    excluded = sorted(ref for ref in gating_refs if not django.specifier.contains(NETBOX_REF_DJANGO_PINS[ref]))
    assert not excluded, f"{django} excludes the Django pin of {excluded}"


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


def test_local_and_ci_commands_request_isolated_workers():
    """Keep the local entry point on the supported count and let CI size itself to the runner."""
    aliases = (REPOSITORY_ROOT / ".devcontainer/scripts/load-aliases.sh").read_text()
    workflow = (REPOSITORY_ROOT / ".github/workflows/test.yaml").read_text()

    assert 'parallel_args=(-n "$workers" --maxschedchunk=1)' in aliases
    assert "pytest -n auto --maxschedchunk=1" in workflow


def test_lint_workflow_python_meets_the_project_minimum():
    """Run the lint workflow on a Python version supported by the project."""
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    requires_python = pyproject["project"]["requires-python"]
    floor_match = re.search(r"(?:^|,)\s*>=\s*(\d+(?:\.\d+)*)", requires_python)
    assert floor_match is not None, f"requires-python has no inclusive floor: {requires_python!r}"

    workflow = yaml.safe_load((REPOSITORY_ROOT / ".github/workflows/lint-format.yaml").read_text())
    setup_python_steps = [
        step
        for step in workflow["jobs"]["format-and-lint"]["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert len(setup_python_steps) == 1

    floor = tuple(int(part) for part in floor_match.group(1).split("."))
    workflow_version = tuple(int(part) for part in str(setup_python_steps[0]["with"]["python-version"]).split("."))
    width = max(len(floor), len(workflow_version))
    assert workflow_version + (0,) * (width - len(workflow_version)) >= floor + (0,) * (width - len(floor))


@pytest.mark.parametrize(
    ("detected_workers", "expected"),
    [("2", 2), (str(MAX_PARALLEL_WORKERS), MAX_PARALLEL_WORKERS), ("32", MAX_PARALLEL_WORKERS)],
)
def test_auto_worker_count_never_exceeds_the_isolated_worker_ceiling(
    pytestconfig, monkeypatch, detected_workers, expected
):
    """`-n auto` on a big machine must stop at the last worker with private Redis databases."""
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", detected_workers)

    assert pytest_xdist_auto_num_workers(pytestconfig) == expected
    isolated_redis_databases(f"gw{expected - 1}")  # the highest worker this count starts


def test_auto_worker_count_stays_capped_without_the_environment_override(pytestconfig, monkeypatch):
    """Without the environment override the hook must still cap the detected CPU count."""
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)

    assert 1 <= pytest_xdist_auto_num_workers(pytestconfig) <= MAX_PARALLEL_WORKERS


@pytest.fixture
def xdist_without_auto_worker_hook(monkeypatch):
    """Remove xdist's optional hook from the real plugin module."""
    import xdist.plugin

    monkeypatch.delattr(xdist.plugin, "pytest_xdist_auto_num_workers", raising=False)


def test_fallback_auto_worker_count_preserves_a_valid_override_below_the_cap(
    pytestconfig,
    monkeypatch,
    xdist_without_auto_worker_hook,
):
    expected = MAX_PARALLEL_WORKERS - 1
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", str(expected))
    monkeypatch.setattr(os, "cpu_count", lambda: MAX_PARALLEL_WORKERS * 2)

    assert pytest_xdist_auto_num_workers(pytestconfig) == expected


@pytest.mark.parametrize("override", [str(MAX_PARALLEL_WORKERS * 2), None, "not-a-number"])
def test_fallback_auto_worker_count_caps_an_override_or_cpu_count(
    pytestconfig,
    monkeypatch,
    xdist_without_auto_worker_hook,
    override,
):
    if override is None:
        monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    else:
        monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", override)
    monkeypatch.setattr(os, "cpu_count", lambda: MAX_PARALLEL_WORKERS * 2)

    assert pytest_xdist_auto_num_workers(pytestconfig) == MAX_PARALLEL_WORKERS


def test_fallback_auto_worker_count_uses_one_when_cpu_count_is_unknown(
    pytestconfig,
    monkeypatch,
    xdist_without_auto_worker_hook,
):
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)

    assert pytest_xdist_auto_num_workers(pytestconfig) == 1


def test_fallback_highest_auto_worker_has_private_redis_databases(
    pytestconfig,
    monkeypatch,
    xdist_without_auto_worker_hook,
):
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", str(MAX_PARALLEL_WORKERS * 2))

    workers = pytest_xdist_auto_num_workers(pytestconfig)

    assert isolated_redis_databases(f"gw{workers - 1}") == (
        MAX_PARALLEL_WORKERS - 1,
        MAX_PARALLEL_WORKERS * 2 - 1,
    )


def test_auto_worker_count_propagates_type_error_from_xdist(pytestconfig, monkeypatch):
    import xdist.plugin

    def raise_type_error(config):
        raise TypeError("xdist hook failed")

    monkeypatch.setattr(xdist.plugin, "pytest_xdist_auto_num_workers", raise_type_error)

    with pytest.raises(TypeError, match="xdist hook failed"):
        pytest_xdist_auto_num_workers(pytestconfig)


def test_auto_worker_count_propagates_import_error_from_xdist(pytestconfig, monkeypatch):
    import xdist.plugin

    def raise_import_error(config):
        raise ImportError("xdist hook import failed")

    monkeypatch.setattr(xdist.plugin, "pytest_xdist_auto_num_workers", raise_import_error)

    with pytest.raises(ImportError, match="xdist hook import failed"):
        pytest_xdist_auto_num_workers(pytestconfig)


def _resolve_auto_worker_count(tmp_path, *path_args):
    """Run `pytest -n auto` over the given paths and report the worker count it resolved to."""
    resolved = tmp_path / "resolved-workers.txt"
    (tmp_path / "auto_worker_probe.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        "def pytest_configure(config):\n"
        '    Path(os.environ["AUTO_WORKER_PROBE_OUTPUT"]).write_text(str(config.option.numprocesses))\n'
        '    pytest.exit("worker count probed")\n'
    )
    environment = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (str(tmp_path), os.environ.get("PYTHONPATH", ""))))
    environment["AUTO_WORKER_PROBE_OUTPUT"] = str(resolved)
    # Report more workers than the cap allows, so the assertion holds on a small CI runner too.
    environment["PYTEST_XDIST_AUTO_NUM_WORKERS"] = str(MAX_PARALLEL_WORKERS * 4)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *path_args, "-n", "auto", "--no-cov", "-p", "auto_worker_probe"],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
    )

    assert resolved.exists(), f"stdout={result.stdout}\nstderr={result.stderr}"
    return int(resolved.read_text())


@pytest.mark.parametrize(
    "path_args",
    [(), ("netbox_librenms_plugin",), ("netbox_librenms_plugin/tests",), (".",)],
    ids=["testpaths", "package", "test-package", "repository-root"],
)
def test_the_auto_worker_cap_applies_to_every_repository_anchor(tmp_path, path_args):
    """`-n auto` must stay capped whichever path the invocation points pytest at."""
    assert _resolve_auto_worker_count(tmp_path, *path_args) == MAX_PARALLEL_WORKERS


@pytest.mark.django_db
def test_a_detected_flush_restores_the_custom_field_with_the_seeded_rows():
    """The probe path must restore everything a flush removes, not only the seeded rows."""
    from extras.models import CustomField

    seeded_models = [model for model, _lookup, _value, _rows in _seeded_model_rows()]
    for model in seeded_models:
        model.objects.all().delete()
    CustomField.objects.filter(name="librenms_id").delete()

    assert restore_seeded_state(force=False) is True

    for model in seeded_models:
        assert model.objects.exists()
    assert CustomField.objects.filter(name="librenms_id").exists()


@pytest.mark.django_db
def test_intact_seeds_are_left_alone_when_no_flush_is_detected():
    """A probe that finds the seeds intact must not rewrite them."""
    assert restore_seeded_state(force=False) is False


@pytest.mark.django_db
def test_a_surviving_row_does_not_hide_the_seeds_that_went_missing_with_it():
    """A partial row loss must still restore, because one row is not the whole seed."""
    for model, lookup_field, _value, rows in _seeded_model_rows():
        survivor = rows[0][0]
        removed, _details = model.objects.exclude(**{lookup_field: survivor}).delete()
        assert removed, f"{model.__name__} kept every row, so this test never made the seed partial"

    assert restore_seeded_state(force=False) is True

    for model, lookup_field, _value, rows in _seeded_model_rows():
        stored = set(model.objects.values_list(lookup_field, flat=True))
        assert stored >= {lookup for lookup, _value in rows}


@pytest.mark.django_db
def test_a_changed_seed_value_is_restored_even_though_its_lookup_key_survived():
    """A seeded row can keep its key and lose its value, so the value is part of the seeded state."""
    for model, lookup_field, value_field, rows in _seeded_model_rows():
        lookup, _value = rows[0]
        drifted = model.objects.filter(**{lookup_field: lookup}).update(**{value_field: r"^ZZZ\d+$"})
        assert drifted, f"{model.__name__} has no row for {lookup!r}, so nothing drifted"

    assert restore_seeded_state(force=False) is True

    for model, lookup_field, value_field, rows in _seeded_model_rows():
        stored = set(model.objects.values_list(lookup_field, value_field))
        assert stored.issuperset(rows)


@pytest.mark.django_db
def test_a_missing_custom_field_restores_even_though_the_seeded_rows_are_intact():
    """The custom field is seeded state too, so losing it alone must trigger a restore."""
    from extras.models import CustomField

    assert CustomField.objects.filter(name="librenms_id").exists(), "the custom field was already missing"
    CustomField.objects.filter(name="librenms_id").delete()

    assert restore_seeded_state(force=False) is True
    assert CustomField.objects.filter(name="librenms_id").exists()


@pytest.mark.parametrize("damage", ["legacy-type", "missing-object-type"])
@pytest.mark.django_db
def test_an_invalid_custom_field_schema_is_restored(damage):
    """The seed probe must reject a field that cannot store every supported object mapping."""
    from dcim.models import Device, Interface
    from django.contrib.contenttypes.models import ContentType
    from extras.models import CustomField
    from virtualization.models import VirtualMachine, VMInterface

    custom_field = CustomField.objects.get(name="librenms_id")
    required_types = set(
        ContentType.objects.get_for_models(
            Device,
            VirtualMachine,
            Interface,
            VMInterface,
        ).values()
    )

    if damage == "legacy-type":
        CustomField.objects.filter(pk=custom_field.pk).update(type="integer")
    else:
        custom_field.object_types.remove(ContentType.objects.get_for_model(VMInterface))

    assert restore_seeded_state(force=False) is True

    custom_field.refresh_from_db()
    assert custom_field.type == "json"
    assert set(custom_field.object_types.all()) >= required_types


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


def test_playwright_state_machine_has_a_required_separate_ci_job():
    """Run browser behavior independently from the NetBox test matrix."""
    workflow = (REPOSITORY_ROOT / ".github/workflows/test.yaml").read_text()
    makefile = (REPOSITORY_ROOT / "Makefile").read_text()
    requirements = (REPOSITORY_ROOT / "requirements_dev.txt").read_text()
    setup = (REPOSITORY_ROOT / ".devcontainer/scripts/setup.sh").read_text()
    testing_guide = (REPOSITORY_ROOT / "docs/development/testing.md").read_text()
    browser_tests = (REPOSITORY_ROOT / "netbox_librenms_plugin/tests/browser/test_sync_cache_browser.py").read_text()

    assert "playwright>=" in requirements
    assert "pytest.importorskip" not in browser_tests
    assert "browser-tests:" in workflow
    assert "pip install -r requirements_dev.txt" in workflow
    assert "python -m playwright install --with-deps chromium" in workflow
    assert "--ignore=netbox_librenms_plugin/tests/browser" in workflow
    assert "pytest -c netbox_librenms_plugin/tests/browser/pytest.ini" in workflow
    # Both callers take the directory: a new browser module must run without editing them.
    assert (
        "pytest -c netbox_librenms_plugin/tests/browser/pytest.ini netbox_librenms_plugin/tests/browser\n" in makefile
    )
    assert "test_sync_cache_browser.py" not in workflow
    assert "test_sync_cache_browser.py" not in makefile
    assert "python -m playwright install --with-deps chromium" in setup
    # A developer outside the devcontainer and CI installs the browser from the guide.
    assert "python -m playwright install chromium" in testing_guide
    assert (
        "pytest -c netbox_librenms_plugin/tests/browser/pytest.ini "
        "netbox_librenms_plugin/tests/browser --lf" in testing_guide
    )


def test_testing_guide_requires_a_test_database_only_for_database_backed_tests():
    """Describe the test database without claiming that the full suite has no database dependency."""
    testing_guide = (REPOSITORY_ROOT / "docs/development/testing.md").read_text()

    assert "The database-backed suite needs a test database" in testing_guide
    assert "No database connection required" not in testing_guide


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


def test_browser_tests_take_their_page_from_the_shared_fixture():
    """A hand-rolled launch skips the teardown that closes the browser after a failing test."""
    browser_directory = REPOSITORY_ROOT / "netbox_librenms_plugin/tests/browser"
    modules = sorted(browser_directory.glob("test_*.py"))

    assert modules, "the scan found no browser module, so the check below would pass vacuously"
    hand_rolled = sorted(path.name for path in modules if "chromium.launch(" in path.read_text())
    assert not hand_rolled, f"take the page from the shared `page` fixture in conftest.py instead: {hand_rolled}"


def test_root_pytest_config_excludes_the_separate_browser_suite():
    """A bare root pytest run must not collect tests owned by the browser config."""
    browser_path = "netbox_librenms_plugin/tests/browser"
    environment = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}
    browser_environment = {
        name: value
        for name, value in environment.items()
        if name not in {"DJANGO_SETTINGS_MODULE", "NETBOX_CONFIGURATION"}
    }
    root_collection = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-cov", "-n", "0"],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
    )
    browser_collection = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            f"{browser_path}/pytest.ini",
            browser_path,
            "--collect-only",
            "-q",
        ],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        env=browser_environment,
        check=False,
    )

    assert root_collection.returncode == 0, root_collection.stderr
    assert "test_sync_cache_browser.py" not in root_collection.stdout
    assert browser_collection.returncode == 0, browser_collection.stderr
    assert "test_sync_cache_browser.py" in browser_collection.stdout


def test_isolated_settings_exclude_unrelated_installed_plugins(settings):
    """Do not import sibling worktrees while resolving URLs for this plugin's tests."""
    assert settings.PLUGINS == ["netbox_librenms_plugin"]
    assert set(settings.PLUGINS_CONFIG) == {"netbox_librenms_plugin"}


def test_isolated_settings_use_the_cross_version_configuration_contract():
    """Load the configured module without requiring a newer NetBox helper."""
    source = (REPOSITORY_ROOT / "netbox_librenms_plugin/tests/isolated_settings.py").read_text()

    assert "netbox.settings_utils" not in source
    assert "importlib.import_module" in source


def test_librenms_config_mock_is_not_applied_to_unrelated_tests(settings):
    """A helper plugin must not replace the configured server catalog globally."""
    from copy import deepcopy

    from netbox.plugins import get_plugin_config

    original_servers = deepcopy(settings.PLUGINS_CONFIG["netbox_librenms_plugin"]["servers"])
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "isolated": {
            "librenms_url": "https://isolated.example.com",
            "api_token": "test-token",
        }
    }
    with override_settings(PLUGINS_CONFIG=plugin_config):
        assert (
            get_plugin_config("netbox_librenms_plugin", "servers") == plugin_config["netbox_librenms_plugin"]["servers"]
        )

    assert get_plugin_config("netbox_librenms_plugin", "servers") == original_servers


def test_configured_norecursedirs_still_skips_pytest_default_directories(tmp_path):
    """Setting norecursedirs replaces pytest's defaults, so ours must still cover them."""
    configured = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    patterns = configured["tool"]["pytest"]["ini_options"]["norecursedirs"]
    # Drive a throwaway project with the repository's own patterns, so this stays true by
    # behaviour rather than by a second copy of pytest's default list.
    rendered = ", ".join(f'"{pattern}"' for pattern in patterns)
    (tmp_path / "pyproject.toml").write_text(f"[tool.pytest.ini_options]\nnorecursedirs = [{rendered}]\n")
    for directory in ("venv", "build", "dist", "node_modules", ".hidden"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "test_skipped_by_default.py").write_text("def test_skipped(): pass\n")
    (tmp_path / "test_collected.py").write_text("def test_collected(): pass\n")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTEST_") and name not in {"DJANGO_SETTINGS_MODULE", "NETBOX_CONFIGURATION"}
    }

    collection = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-p", "no:randomly"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=environment,
        check=False,
    )

    assert "test_collected" in collection.stdout, collection.stdout + collection.stderr
    assert "test_skipped_by_default" not in collection.stdout, (
        "norecursedirs dropped pytest's default exclusions:\n" + collection.stdout
    )
