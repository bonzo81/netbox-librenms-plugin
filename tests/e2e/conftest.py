"""Shared harness for the end-to-end Playwright tests.

These tests are excluded from the default pytest discovery via ``testpaths``
in ``pyproject.toml`` and are intended to be invoked explicitly:

    python -m pytest tests/e2e/test_module_install.py -v -s

Note: ``pyproject.toml`` sets ``DJANGO_SETTINGS_MODULE = "netbox.settings"``
under ``[tool.pytest.ini_options]``, which pytest-django reads directly from
the config file (not from the environment).  Popping the env var here would
have no effect on pytest-django's initialisation.  The e2e tests do not
import or use Django models — they drive a running NetBox over HTTP — so
pytest-django's auto-loading is harmless and we leave it alone.  If you ever
need to skip pytest-django entirely for this suite, invoke pytest with
``-p no:django``.

Configuration (environment variables):
    E2E_TESTS_ENABLED=1          Required to run these tests
    NETBOX_URL=<url>             NetBox base URL (default http://172.22.0.4:8000)
    NETBOX_USER=<user>           Login username (default admin)
    NETBOX_PASS=<pass>           Login password (default admin)
    NETBOX_CONTAINER=<name>      Docker container name (auto-detected if omitted)
    E2E_HEADLESS=0               Set to 0 to watch the browser (default 1)
    E2E_CHROMIUM_EXECUTABLE      Chromium binary to launch instead of the bundled one
    E2E_BROWSER_ARGS             Space-separated extra Chromium arguments
"""

import os
import shlex
import subprocess

import pytest

NETBOX_URL = os.environ.get("NETBOX_URL", "http://172.22.0.4:8000")
NETBOX_USER = os.environ.get("NETBOX_USER", "admin")
NETBOX_PASS = os.environ.get("NETBOX_PASS", "admin")

_CONTAINER_NAME = None


def netbox_container():
    """Return the devcontainer name that runs NetBox."""
    global _CONTAINER_NAME
    if _CONTAINER_NAME:
        return _CONTAINER_NAME
    override = os.environ.get("NETBOX_CONTAINER")
    if override:
        _CONTAINER_NAME = override
        return override
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker ps failed (rc={result.returncode}): {result.stderr}")
    matches = [name for name in result.stdout.strip().split("\n") if "devcontainer-devcontainer" in name]
    if len(matches) == 1:
        _CONTAINER_NAME = matches[0]
        return _CONTAINER_NAME
    if len(matches) > 1:
        raise RuntimeError(f"Multiple candidate devcontainers found: {matches}. Set NETBOX_CONTAINER.")
    pytest.skip("No devcontainer found")


def netbox_shell(code):
    """Run Python code in NetBox's Django shell and return its output."""
    container = netbox_container()
    escaped = shlex.quote(code)
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "bash",
            "-c",
            f"cd /opt/netbox/netbox && python3 manage.py shell -c {escaped}",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": "/usr/bin:/bin", "HOME": "/root"},
    )
    lines = [
        line
        for line in result.stdout.strip().split("\n")
        if not line.startswith("🧬") and "objects imported automatically" not in line
    ]
    if result.returncode != 0:
        raise RuntimeError(f"netbox shell command failed (rc={result.returncode}): {result.stderr}")
    return "\n".join(lines).strip()


def browser_launch_options():
    """Return the Chromium launch options, including the optional host overrides."""
    options = {"headless": os.environ.get("E2E_HEADLESS", "1") != "0"}
    executable = os.environ.get("E2E_CHROMIUM_EXECUTABLE")
    if executable:
        options["executable_path"] = executable
    args = os.environ.get("E2E_BROWSER_ARGS", "").split()
    if args:
        options["args"] = args
    return options


@pytest.fixture(scope="module")
def browser():
    """Launch the browser for the test module."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    b = pw.chromium.launch(**browser_launch_options())
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(browser):
    """Create a new page and log in to NetBox."""
    ctx = browser.new_context(ignore_https_errors=True)
    pg = ctx.new_page()

    pg.goto(f"{NETBOX_URL}/login/", timeout=10000)
    pg.fill("#id_username", NETBOX_USER)
    pg.fill("#id_password", NETBOX_PASS)
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")
    yield pg
    ctx.close()
