"""
End-to-end Playwright tests for LibreNMS plugin module sync workflow.

These tests exercise the full import → modules → install flow against a
live NetBox + LibreNMS instance inside the devcontainer.

Prerequisites:
    - NetBox running at NETBOX_URL (default http://172.22.0.4:8000)
    - LibreNMS server configured in plugin settings
    - A device linked to LibreNMS that has inventory modules
    - Playwright installed: pip install playwright && playwright install chromium

Configuration (environment variables):
    E2E_TESTS_ENABLED=1          Required to run these tests
    E2E_DEVICE_ID=<id>           NetBox device PK to test against (auto-detected if omitted)
    NETBOX_URL=<url>             NetBox base URL (default http://172.22.0.4:8000)
    NETBOX_USER=<user>           Login username (default admin)
    NETBOX_PASS=<pass>           Login password (default admin)
    NETBOX_CONTAINER=<name>      Docker container name (auto-detected if omitted)

Run:
    cd /home/mzieba/workspace/netbox-librenms-plugin
    HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= \
    no_proxy=localhost,127.0.0.1,172.22.0.4 \
    E2E_TESTS_ENABLED=1 python -m pytest tests/e2e/test_module_install.py -v -s
"""

import os
import subprocess

import pytest

NETBOX_URL = os.environ.get("NETBOX_URL", "http://172.22.0.4:8000")
NETBOX_USER = os.environ.get("NETBOX_USER", "admin")
NETBOX_PASS = os.environ.get("NETBOX_PASS", "admin")
CONTAINER_NAME = None

E2E_ENABLED = os.environ.get("E2E_TESTS_ENABLED", "0") == "1"

if not E2E_ENABLED:
    pytest.skip(
        "E2E tests skipped — set E2E_TESTS_ENABLED=1 to run against a live instance",
        allow_module_level=True,
    )


def _get_container():
    """Find the devcontainer name."""
    global CONTAINER_NAME
    if CONTAINER_NAME:
        return CONTAINER_NAME
    override = os.environ.get("NETBOX_CONTAINER")
    if override:
        CONTAINER_NAME = override
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
        CONTAINER_NAME = matches[0]
        return CONTAINER_NAME
    if len(matches) > 1:
        raise RuntimeError(f"Multiple candidate devcontainers found: {matches}. Set NETBOX_CONTAINER.")
    pytest.skip("No devcontainer found")


def _netbox_shell(code):
    """Run Python code in NetBox's Django shell."""
    import shlex

    container = _get_container()
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


def _detect_device_id():
    """Find a device linked to LibreNMS that has inventory modules.

    Returns the NetBox device PK, or skips the test session if none found.
    """
    output = _netbox_shell(
        "from dcim.models import Device; "
        "devs = Device.objects.exclude(custom_field_data__librenms_id=None)"
        ".exclude(custom_field_data__librenms_id={}).order_by('pk'); "
        "print(devs.first().pk if devs.exists() else '')"
    )
    if not output.strip():
        pytest.skip("No device with librenms_id found in NetBox")
    return int(output.strip())


def _delete_device_modules(device_id):
    """Remove all modules from a device."""
    _netbox_shell(
        f"from dcim.models import Module; "
        f"deleted = Module.objects.filter(device_id={device_id}).delete(); "
        f"print(f'Deleted {{deleted}}')"
    )


def _get_interfaces(device_id):
    """Get interface names for a device."""
    output = _netbox_shell(
        f"from dcim.models import Interface; "
        f'[print(f\'{{i.name}}|{{i.module.module_type.model if i.module else "-"}}|'
        f'{{i.module.module_bay.name if i.module else "-"}}\')'
        f" for i in Interface.objects.filter(device_id={device_id}).order_by('name')]"
    )
    results = []
    for line in output.split("\n"):
        if "|" in line:
            name, mod_type, bay = line.split("|")
            results.append({"name": name, "module_type": mod_type, "bay": bay})
    return results


@pytest.fixture(scope="module")
def device_id():
    """Resolve the device PK to test against."""
    env_id = os.environ.get("E2E_DEVICE_ID")
    if env_id:
        return int(env_id)
    return _detect_device_id()


@pytest.fixture(scope="module")
def browser():
    """Launch browser for the test module."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
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


class TestModuleInstallWorkflow:
    """Test the full module sync and install workflow."""

    def _goto_modules_tab(self, page, device_id):
        """Navigate to the modules sync tab and refresh data."""
        page.goto(f"{NETBOX_URL}/dcim/devices/{device_id}/librenms-sync/?tab=modules")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector('button:has-text("Refresh Modules")', timeout=10000)

        btn = page.query_selector('button:has-text("Refresh Modules")')
        assert btn is not None, "Refresh Modules button not found"
        btn.click()
        page.wait_for_selector("#modules table tr", timeout=30000)
        page.wait_for_load_state("networkidle")

    def _get_table_rows(self, page):
        """Parse the module sync table into dicts."""
        pane = page.query_selector("#modules")
        assert pane is not None, "Modules pane not found"

        def _text(tr, col):
            el = tr.query_selector(f'td[data-col="{col}"]')
            return el.inner_text().strip() if el else ""

        rows = []
        for tr in pane.query_selector_all("table tr"):
            if tr.query_selector('td[data-col="name"]') is None:
                continue
            rows.append(
                {
                    "name": _text(tr, "name"),
                    "model": _text(tr, "model"),
                    "serial": _text(tr, "serial"),
                    "bay": _text(tr, "module_bay"),
                    "type": _text(tr, "module_type"),
                    "status": _text(tr, "status"),
                    "tr": tr,
                }
            )
        return rows

    def _find_row_with_button(self, rows, button_text):
        """Find the first top-level row that has a button matching button_text."""
        for row in rows:
            if row["name"].startswith("└─"):
                continue
            btn = row["tr"].query_selector(f'button:has-text("{button_text}")')
            if btn:
                return row, btn
        return None, None

    def test_clean_state_shows_matched_rows(self, page, device_id):
        """After deleting all modules, table shows rows with Matched status."""
        _delete_device_modules(device_id)
        self._goto_modules_tab(page, device_id)

        rows = self._get_table_rows(page)
        assert len(rows) > 0, "No rows in module sync table"

        matched = [r for r in rows if r["status"] == "Matched"]
        assert len(matched) > 0, f"Expected at least one Matched row, got statuses: {set(r['status'] for r in rows)}"

    def test_single_install(self, page, device_id):
        """Installing a single top-level module works."""
        _delete_device_modules(device_id)
        self._goto_modules_tab(page, device_id)

        rows = self._get_table_rows(page)
        row, btn = self._find_row_with_button(rows, "Install")
        if not btn:
            pytest.skip("No installable module found in table")

        module_name = row["name"]
        btn.click()
        page.wait_for_load_state("networkidle")

        module_count = _netbox_shell(
            f"from dcim.models import Module; print(Module.objects.filter(device_id={device_id}).count())"
        )
        assert int(module_count.strip()) > 0, f"No modules in DB after installing '{module_name}'"

    def test_branch_install(self, page, device_id):
        """Branch install creates parent module + children."""
        _delete_device_modules(device_id)
        self._goto_modules_tab(page, device_id)

        rows = self._get_table_rows(page)
        row, btn = self._find_row_with_button(rows, "Install Branch")
        if not btn:
            pytest.skip("No branch-installable module found in table")

        module_name = row["name"]
        btn.click()
        page.wait_for_load_state("networkidle", timeout=60000)

        module_count = _netbox_shell(
            f"from dcim.models import Module; print(Module.objects.filter(device_id={device_id}).count())"
        )
        count = int(module_count.strip())
        assert count > 1, f"Branch install of '{module_name}' created {count} module(s), expected >1"

        interfaces = _get_interfaces(device_id)
        module_interfaces = [i for i in interfaces if i["module_type"] != "-"]
        assert len(module_interfaces) > 0, f"No interfaces linked to modules after branch install of '{module_name}'"
        for iface in module_interfaces:
            assert not iface["name"].isdigit(), (
                f"Interface '{iface['name']}' has bare numeric name — naming rule not applied"
            )

    def test_branch_install_no_duplicate_errors(self, page, device_id):
        """Branch install handles already-occupied bays gracefully."""
        self._goto_modules_tab(page, device_id)

        rows = self._get_table_rows(page)
        row, btn = self._find_row_with_button(rows, "Install Branch")
        if not btn:
            pytest.skip("No branch-installable module found in table")

        module_name = row["name"]

        # First install (may already be installed from prior test)
        btn.click()
        page.wait_for_load_state("networkidle", timeout=60000)

        # Navigate back and try again — bays should now be occupied
        self._goto_modules_tab(page, device_id)
        rows = self._get_table_rows(page)
        _, btn2 = self._find_row_with_button(rows, "Install Branch")
        if not btn2:
            pytest.skip(f"No Install Branch button after first install of '{module_name}'")

        btn2.click()
        page.wait_for_load_state("networkidle", timeout=30000)

        body_text = page.query_selector("body").inner_text()
        assert "Branch install failed" not in body_text, (
            "Branch install crashed instead of handling occupied bays gracefully"
        )

    def test_child_bays_hidden_when_parent_not_installed(self, page, device_id):
        """Children show 'No matching bay' when parent module is not installed."""
        _delete_device_modules(device_id)
        self._goto_modules_tab(page, device_id)

        rows = self._get_table_rows(page)
        children = [r for r in rows if r["name"].startswith("└─")]
        if not children:
            pytest.skip("No child module rows found in table")

        no_bay = [c for c in children if "No matching bay" in c["bay"]]
        assert len(no_bay) > 0, (
            "Expected some children to show 'No matching bay' when parent not installed, "
            f"got bays: {set(c['bay'] for c in children)}"
        )

    def test_full_workflow(self, page, device_id):
        """Full workflow: clean → install individuals → branch install → verify."""
        _delete_device_modules(device_id)
        self._goto_modules_tab(page, device_id)

        # Step 1: Install a few individual modules
        for _ in range(3):
            rows = self._get_table_rows(page)
            row, btn = self._find_row_with_button(rows, "Install")
            if not btn:
                break
            btn.click()
            page.wait_for_load_state("networkidle")

        # Step 2: Branch install all available branches
        while True:
            rows = self._get_table_rows(page)
            row, btn = self._find_row_with_button(rows, "Install Branch")
            if not btn:
                break
            btn.click()
            page.wait_for_load_state("networkidle", timeout=60000)
            self._goto_modules_tab(page, device_id)

        # Verify: no top-level "Matched" items remain uninstalled
        self._goto_modules_tab(page, device_id)
        rows = self._get_table_rows(page)
        top_level_matched = [r for r in rows if r["status"] == "Matched" and not r["name"].startswith("└─")]
        assert len(top_level_matched) == 0, (
            f"Top-level items still Matched after full workflow: {[r['name'] for r in top_level_matched]}"
        )

        # Verify interface naming — no bare numeric names
        interfaces = _get_interfaces(device_id)
        for iface in interfaces:
            assert not iface["name"].isdigit(), (
                f"Interface '{iface['name']}' has bare numeric name — naming rule not applied"
            )
