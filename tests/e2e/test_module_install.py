"""
End-to-end Playwright tests for LibreNMS plugin module sync workflow.

These tests exercise the full import → modules → install flow against a
live NetBox + LibreNMS instance inside the devcontainer.

Prerequisites:
    - NetBox running at NETBOX_URL (default http://172.22.0.4:8000)
    - LibreNMS server configured in plugin settings
    - Device 15 (WS-C4900M) exists and is linked to LibreNMS
    - Playwright installed: pip install playwright && playwright install chromium

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
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    for name in result.stdout.strip().split("\n"):
        if "devcontainer-devcontainer" in name:
            CONTAINER_NAME = name
            return name
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
        env={"PATH": "/usr/bin:/bin", "HOME": "/root"},
    )
    # Filter out config loading lines
    lines = [line for line in result.stdout.strip().split("\n") if not line.startswith(("🧬", "156 objects"))]
    if result.returncode != 0:
        raise RuntimeError(f"netbox shell command failed (rc={result.returncode}): {result.stderr}")
    return "\n".join(lines).strip()


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
    """Test the full module sync and install workflow on device 15 (WS-C4900M)."""

    DEVICE_ID = 15

    def _goto_modules_tab(self, page):
        """Navigate to the modules sync tab and refresh data."""
        page.goto(f"{NETBOX_URL}/dcim/devices/{self.DEVICE_ID}/librenms-sync/?tab=modules")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector('button:has-text("Refresh Modules")', timeout=10000)

        # Click Refresh Modules
        btn = page.query_selector('button:has-text("Refresh Modules")')
        assert btn is not None, "Refresh Modules button not found"
        btn.click()
        # Wait for the HTMX-refreshed table to render (spinner gone, rows present)
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
                }
            )
        return rows

    def test_clean_state_shows_install_buttons(self, page):
        """After deleting all modules, table shows Install buttons."""
        _delete_device_modules(self.DEVICE_ID)
        self._goto_modules_tab(page)

        rows = self._get_table_rows(page)
        assert len(rows) > 0, "No rows in module sync table"

        # Top-level items with matched bays should show Matched status
        supervisor = [r for r in rows if "Supervisor(slot 1)" in r["name"]]
        assert len(supervisor) == 1, f"Expected 1 Supervisor row, got {len(supervisor)}"
        assert supervisor[0]["status"] == "Matched", f"Expected Matched, got {supervisor[0]['status']}"

    def test_single_install(self, page):
        """Installing a single top-level module works."""
        _delete_device_modules(self.DEVICE_ID)
        self._goto_modules_tab(page)

        # Install FanTray 1
        pane = page.query_selector("#modules")
        for tr in pane.query_selector_all("table tr"):
            name_cell = tr.query_selector('td[data-col="name"]')
            if name_cell and "FanTray 1" in name_cell.inner_text():
                btn = tr.query_selector('button:has-text("Install"):not(:has-text("Branch"))')
                if btn:
                    btn.click()
                    page.wait_for_load_state("networkidle")
                    break

        # Verify via DB
        output = _netbox_shell(
            f"from dcim.models import Module; "
            f"m = Module.objects.filter(device_id={self.DEVICE_ID}, module_bay__name='Fan Tray 1').first(); "
            f"print(m.module_type.model if m else 'NONE')"
        )
        assert "WS-X4992" in output, f"FanTray not installed: {output}"

    def test_branch_install_supervisor(self, page):
        """Branch install creates supervisor + X2 transceivers with correct names."""
        _delete_device_modules(self.DEVICE_ID)
        self._goto_modules_tab(page)

        # Click Install Branch on Supervisor(slot 1)
        pane = page.query_selector("#modules")
        for tr in pane.query_selector_all("table tr"):
            name_cell = tr.query_selector('td[data-col="name"]')
            if name_cell and "Supervisor(slot 1)" in name_cell.inner_text():
                btn = tr.query_selector('button:has-text("Install Branch")')
                assert btn is not None, "Install Branch button not found for Supervisor"
                btn.click()
                break

        # Wait for branch install to complete (creates many modules + signals)
        page.wait_for_load_state("networkidle", timeout=60000)

        # Verify interfaces have correct names (not bare position numbers)
        interfaces = _get_interfaces(self.DEVICE_ID)
        x2_interfaces = [i for i in interfaces if i["module_type"] in ("X2-10GB-LR", "X2-10GB-SR")]

        assert len(x2_interfaces) > 0, "No X2 transceiver interfaces created"

        for iface in x2_interfaces:
            assert iface["name"].startswith("TenGigabitEthernet"), (
                f"Interface '{iface['name']}' in {iface['bay']} "
                f"should start with 'TenGigabitEthernet' (INR rule not applied?)"
            )

    def test_branch_install_no_duplicate_errors(self, page):
        """Branch install handles already-occupied bays gracefully."""
        # Seed state: ensure Supervisor(slot 1) branch is installed before testing
        # the duplicate-install path. If running in isolation no modules may exist.
        self._goto_modules_tab(page)
        pane = page.query_selector("#modules")
        seed_btn = None
        for tr in pane.query_selector_all("table tr"):
            name_cell = tr.query_selector('td[data-col="name"]')
            if name_cell and "Supervisor(slot 1)" in name_cell.inner_text():
                seed_btn = tr.query_selector('button:has-text("Install Branch")')
                break

        if seed_btn:
            # Not yet installed — do the initial install so bays become occupied
            seed_btn.click()
            page.wait_for_load_state("networkidle", timeout=60000)
            self._goto_modules_tab(page)
            pane = page.query_selector("#modules")

        # Click Install Branch on Supervisor(slot 1) again (bays now occupied)
        branch_btn = None
        for tr in pane.query_selector_all("table tr"):
            name_cell = tr.query_selector('td[data-col="name"]')
            if name_cell and "Supervisor(slot 1)" in name_cell.inner_text():
                branch_btn = tr.query_selector('button:has-text("Install Branch")')
                break

        assert branch_btn is not None, "Install Branch button not found for Supervisor(slot 1)"
        branch_btn.click()
        page.wait_for_load_state("networkidle", timeout=30000)

        # Check for error messages — should only have skips, no failures
        body_text = page.query_selector("body").inner_text()
        assert "Branch install failed" not in body_text, "Branch install crashed instead of handling errors gracefully"

    def test_child_bays_hidden_when_parent_not_installed(self, page):
        """Children show 'No Bay' when parent module is not installed."""
        _delete_device_modules(self.DEVICE_ID)
        self._goto_modules_tab(page)

        rows = self._get_table_rows(page)

        # Children of Supervisor(slot 1) should show "No matching bay"
        # since Supervisor isn't installed, its child bays don't exist yet
        children = [r for r in rows if r["name"].startswith("└─") and "TenGigabitEthernet1/" in r["name"]]
        for child in children:
            assert "No matching bay" in child["bay"], (
                f"Child '{child['name']}' should show 'No matching bay' when parent not installed, got '{child['bay']}'"
            )

    def test_full_workflow(self, page):
        """Full workflow: clean → install individuals → branch install → verify."""
        _delete_device_modules(self.DEVICE_ID)
        self._goto_modules_tab(page)

        # Step 1: Install PSUs and FanTray individually
        for label in ["FanTray 1", "Power Supply 1", "Power Supply 2"]:
            pane = page.query_selector("#modules")
            for tr in pane.query_selector_all("table tr"):
                name_cell = tr.query_selector('td[data-col="name"]')
                if name_cell and label in name_cell.inner_text():
                    btn = tr.query_selector('button:has-text("Install"):not(:has-text("Branch"))')
                    if btn:
                        btn.click()
                        page.wait_for_load_state("networkidle")
                        break

        # Step 2: Branch install Supervisor + transceivers
        pane = page.query_selector("#modules")
        for tr in pane.query_selector_all("table tr"):
            name_cell = tr.query_selector('td[data-col="name"]')
            if name_cell and "Supervisor(slot 1)" in name_cell.inner_text():
                btn = tr.query_selector('button:has-text("Install Branch")')
                if btn:
                    btn.click()
                    break

        page.wait_for_load_state("networkidle", timeout=60000)

        # Step 3: Branch install Linecard
        self._goto_modules_tab(page)
        pane = page.query_selector("#modules")
        for tr in pane.query_selector_all("table tr"):
            name_cell = tr.query_selector('td[data-col="name"]')
            if name_cell and "Linecard(slot 3)" in name_cell.inner_text():
                btn = tr.query_selector('button:has-text("Install Branch")')
                if btn:
                    btn.click()
                    break

        page.wait_for_load_state("networkidle", timeout=60000)

        # Verify: all installable modules should be installed
        self._goto_modules_tab(page)
        rows = self._get_table_rows(page)

        matched_but_not_installed = [r for r in rows if r["status"] == "Matched" and not r["name"].startswith("└─")]
        assert len(matched_but_not_installed) == 0, (
            f"Top-level items still 'Matched' after full workflow: {[r['name'] for r in matched_but_not_installed]}"
        )

        # Verify interface naming
        interfaces = _get_interfaces(self.DEVICE_ID)
        for iface in interfaces:
            assert iface["name"] != "1", "Interface with bare name '1' found — INR rule not applied"
