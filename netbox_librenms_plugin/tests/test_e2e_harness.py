"""Harness checks for the ``tests/e2e`` suite, which sits outside ``testpaths``."""

import os
import re
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parents[2] / "tests" / "e2e"


def _e2e_conftest():
    """Import ``tests/e2e/conftest.py`` under a name pytest does not collect."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("e2e_conftest_under_test", E2E_DIR / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _e2e_module(name, monkeypatch):
    """Import an opt-in ``tests/e2e`` module as part of a package that pytest does not collect."""
    import importlib
    import importlib.util
    import sys

    monkeypatch.setenv("E2E_TESTS_ENABLED", "1")
    package = "e2e_suite_under_test"
    spec = importlib.util.spec_from_file_location(
        package, E2E_DIR / "__init__.py", submodule_search_locations=[str(E2E_DIR)]
    )
    monkeypatch.setitem(sys.modules, package, importlib.util.module_from_spec(spec))
    spec.loader.exec_module(sys.modules[package])
    for module_name in (f"{package}.conftest", f"{package}.{name}"):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(f"{package}.{name}")
    monkeypatch.setitem(sys.modules, f"{package}.{name}", module)
    return module


@pytest.mark.django_db
def test_module_actions_setup_leaves_nothing_when_a_later_step_fails(monkeypatch):
    """The fixture runs no teardown when setup fails, so setup must roll back what it created."""
    pytest.importorskip("playwright.sync_api")
    from dcim.models import Device, DeviceType, Manufacturer, Site

    module = _e2e_module("test_module_actions_in_place", monkeypatch)

    def refuse_save(*args, **kwargs):
        raise RuntimeError("injected failure at the device save")

    monkeypatch.setattr(Device, "save", refuse_save)
    with pytest.raises(RuntimeError, match="injected failure"):
        exec(module.SETUP_CODE, {})

    assert not Manufacturer.objects.filter(slug=module.MANUFACTURER_SLUG).exists()
    assert not DeviceType.objects.filter(model=module.DEVICE_TYPE_MODEL).exists()
    assert not Site.objects.filter(slug=module.SITE_SLUG).exists()


def test_netbox_shell_runs_the_docker_on_the_inherited_path(tmp_path, monkeypatch):
    """Docker often sits outside /usr/bin, so the helper must not narrow PATH for it."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("#!/bin/sh\necho harness-marker\n")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("NETBOX_CONTAINER", "netbox-e2e-harness-probe")

    assert _e2e_conftest().netbox_shell("print('probe')") == "harness-marker"


def test_netbox_shell_failure_reports_the_shell_stdout(tmp_path, monkeypatch):
    """A failed shell command can print its useful output on stdout, so the error must carry it."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("#!/bin/sh\necho stdout-detail\necho stderr-detail >&2\nexit 1\n")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("NETBOX_CONTAINER", "netbox-e2e-harness-probe")

    with pytest.raises(RuntimeError) as failure:
        _e2e_conftest().netbox_shell("print('probe')")

    assert "stdout-detail" in str(failure.value)
    assert "stderr-detail" in str(failure.value)


def test_e2e_run_instructions_carry_no_developer_home_path():
    """Every contributor must be able to run the documented command."""
    sources = sorted(E2E_DIR.glob("*.py"))
    assert sources, "the scan found no e2e module, so the check below would pass vacuously"

    leaks = [
        f"{path.name}:{number}"
        for path in sources
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"/(?:home|Users)/[A-Za-z0-9._-]+/", line)
    ]

    assert not leaks, f"absolute developer path in the e2e sources: {leaks}"
