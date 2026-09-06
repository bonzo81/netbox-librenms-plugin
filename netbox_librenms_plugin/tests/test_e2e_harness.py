"""Harness checks for the ``tests/e2e`` suite, which sits outside ``testpaths``."""

import os
import re
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parents[2] / "tests" / "e2e"


def _e2e_conftest():
    """Import ``tests/e2e/conftest.py`` under a name pytest does not collect."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("e2e_conftest_under_test", E2E_DIR / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
