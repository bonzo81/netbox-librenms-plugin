"""
End-to-end Playwright test for the Device Type mapping workflow in the import modal.

Tests that after adding a DeviceTypeMapping from the validation modal:
  1. The modal stays open and no longer shows "No matching type".
  2. The background table row updates to enable the Import button — no page refresh needed.

Configuration (env vars):
    E2E_TESTS_ENABLED=1           Required to run
    NETBOX_URL                    (default http://127.0.0.1:8000)
    NETBOX_USER / NETBOX_PASS     (default admin/admin)
    E2E_IMPORT_URL                Full import page URL to use
    E2E_HEADLESS=0                Set to 0 to watch browser (default 1)

Run:
    HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= \\
    no_proxy=localhost,127.0.0.1 \\
    E2E_TESTS_ENABLED=1 \\
    E2E_IMPORT_URL="http://127.0.0.1:8000/plugins/librenms_plugin/librenms-import/?apply_filters=1&librenms_location=1" \\
    /opt/python-venv/bin/python -m pytest tests/e2e/test_device_type_mapping.py -v -s -p no:django
"""

import os
import subprocess

import pytest

NETBOX_URL = os.environ.get("NETBOX_URL", "http://127.0.0.1:8000")
NETBOX_USER = os.environ.get("NETBOX_USER", "admin")
NETBOX_PASS = os.environ.get("NETBOX_PASS", "admin")
IMPORT_URL = os.environ.get(
    "E2E_IMPORT_URL",
    f"{NETBOX_URL}/plugins/librenms_plugin/librenms-import/",
)
HEADLESS = os.environ.get("E2E_HEADLESS", "1") != "0"

E2E_ENABLED = os.environ.get("E2E_TESTS_ENABLED", "0") == "1"

if not E2E_ENABLED:
    pytest.skip("E2E tests skipped — set E2E_TESTS_ENABLED=1 to run", allow_module_level=True)


def _get_container():
    result = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    matches = [n for n in result.stdout.strip().split("\n") if "devcontainer-devcontainer" in n]
    if len(matches) == 1:
        return matches[0]
    pytest.skip("No devcontainer found")


def _netbox_shell(code):
    import shlex

    container = _get_container()
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "bash",
            "-c",
            f"cd /opt/netbox/netbox && python3 manage.py shell -c {shlex.quote(code)}",
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
        raise RuntimeError(f"shell failed: {result.stderr}")
    return "\n".join(lines).strip()


def _delete_mapping(pk: int):
    _netbox_shell(
        f"from netbox_librenms_plugin.models import DeviceTypeMapping; "
        f"from django.core.cache import cache; "
        f"DeviceTypeMapping.objects.filter(pk={pk}).delete(); "
        f"cache.clear()"
    )


def _restore_mapping(hardware: str, device_type_id: int):
    _netbox_shell(
        f"from netbox_librenms_plugin.models import DeviceTypeMapping; "
        f"from dcim.models import DeviceType; "
        f"dt = DeviceType.objects.get(pk={device_type_id}); "
        f"DeviceTypeMapping.objects.get_or_create("
        f"  librenms_hardware={hardware!r}, defaults={{'netbox_device_type': dt}})"
    )


def _get_existing_mapping():
    output = _netbox_shell(
        "from netbox_librenms_plugin.models import DeviceTypeMapping; "
        "m = DeviceTypeMapping.objects.first(); "
        "print(f'{m.pk}|{m.librenms_hardware}|{m.netbox_device_type_id}') if m else print('')"
    )
    if not output.strip():
        return None
    parts = output.strip().split("|")
    return {"pk": int(parts[0]), "hardware": parts[1], "device_type_id": int(parts[2])} if len(parts) == 3 else None


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=HEADLESS)
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(ignore_https_errors=True)
    pg = ctx.new_page()
    pg.goto(f"{NETBOX_URL}/login/", timeout=15000)
    pg.fill("#id_username", NETBOX_USER)
    pg.fill("#id_password", NETBOX_PASS)
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")
    yield pg
    ctx.close()


def _close_modal(page):
    """Close the HTMX modal if open (use the X btn-close button)."""
    close_btn = page.locator("#htmx-modal button.btn-close[data-bs-dismiss='modal']").first
    if close_btn.count() > 0 and close_btn.is_visible():
        close_btn.click()
        page.wait_for_timeout(400)


def _find_no_match_modal(page):
    """
    Open detail buttons one by one until a modal shows 'No matching type'.
    Returns (modal_locator, row_locator) or (None, None).
    """
    btns = page.locator("button.btn-outline-danger, button.btn-warning").all()
    for btn in btns[:10]:  # Try up to 10 buttons
        if not btn.is_visible():
            continue
        row = btn.locator("xpath=ancestor::tr[1]")
        btn.click()
        try:
            page.wait_for_selector("#htmx-modal-content .modal-header", timeout=8000)
        except Exception:
            continue
        page.wait_for_timeout(500)
        modal = page.locator("#htmx-modal-content")
        if "No matching type" in modal.inner_text():
            return modal, row
        _close_modal(page)
        page.wait_for_timeout(200)
    return None, None


class TestDeviceTypeMappingModal:
    """Verify DeviceType mapping from import validation modal updates row in-place."""

    def test_mapping_updates_modal_and_row(self, page):
        """
        Setup: temporarily remove a DeviceTypeMapping so a device shows 'No matching type'.
        Assert: after adding the mapping back via the modal form:
          - Modal stays open and shows the match (no 'No matching type')
          - Background row updates to show enabled Import button (no page refresh)
        """
        mapping = _get_existing_mapping()
        if not mapping:
            pytest.skip("No DeviceTypeMapping in DB — cannot set up test scenario")

        _delete_mapping(mapping["pk"])
        try:
            self._run_test(page, mapping)
        finally:
            _restore_mapping(mapping["hardware"], mapping["device_type_id"])

    def _run_test(self, page, mapping):
        page.goto(IMPORT_URL, timeout=20000)
        page.wait_for_load_state("networkidle")

        modal, row = _find_no_match_modal(page)
        if modal is None:
            pytest.skip(
                f"No 'No matching type' device found at {IMPORT_URL} after removing mapping. "
                "Set E2E_IMPORT_URL to the filtered import page that shows the affected device."
            )

        # Ensure a role is selected so the device can become importable
        role_select = row.locator("select[name^='role_']")
        if role_select.count() > 0 and not role_select.input_value():
            role_select.select_option(index=1)
            page.wait_for_load_state("networkidle")
            # Re-open modal after role change
            _close_modal(page)
            modal, row = _find_no_match_modal(page)
            if modal is None:
                pytest.skip("No 'No matching type' device after role selection")

        # Locate the search input
        search = modal.locator("input[placeholder*='Search device types']")
        assert search.is_visible(), "DeviceType search input not visible in modal"

        # Diagnose: dump the search input's id and the dropdown div's id
        search_id = search.get_attribute("id")
        dropdown_div = modal.locator("[id^='dt-dropdown-']")
        dropdown_id = dropdown_div.get_attribute("id") if dropdown_div.count() > 0 else "(not found)"
        print(f"\nSearch input id: {search_id!r}, dropdown div id: {dropdown_id!r}")

        # Check whether the JS event listener is attached by inspecting via JS
        has_listener = (
            page.evaluate(f"() => {{ var el = document.getElementById({search_id!r}); return !!el; }}")
            if search_id
            else False
        )
        print(f"searchEl found via getElementById: {has_listener}")

        # Try dispatching input event directly via JS to bypass any event listener gaps
        page.evaluate(
            f"() => {{"
            f"  var el = document.getElementById({search_id!r});"
            f"  if (el) {{ el.value = 'a'; el.dispatchEvent(new Event('input', {{bubbles:true}})); }}"
            f"}}"
        )
        page.wait_for_timeout(1500)

        # If still no dropdown, try using the keyboard directly on the focused element
        search.click()
        page.keyboard.type("b", delay=100)
        page.wait_for_timeout(1500)

        # Final check
        dropdown_items = modal.locator("[id^='dt-dropdown-'] a")
        print(f"Dropdown item count after attempts: {dropdown_items.count()}")

        page.wait_for_selector(
            "#htmx-modal-content [id^='dt-dropdown-'] a",
            timeout=8000,
            state="attached",
        )
        page.wait_for_timeout(200)

        dropdown = modal.locator("[id^='dt-dropdown-']")
        first = dropdown.locator("a").first
        assert first.count() > 0, "No results in DeviceType dropdown"
        first.click()
        page.wait_for_timeout(200)

        hidden_val = modal.locator("input[name='device_type_id']").input_value()
        assert hidden_val, "Hidden device_type_id not filled after clicking a result"

        # Submit
        modal.locator("button:has-text('Add Mapping')").click()

        # Wait for OOB modal update + JS-triggered row refresh (50ms defer + network round-trip)
        page.wait_for_timeout(3000)
        page.wait_for_load_state("networkidle")

        # --- Assert 1: modal stays open and shows match ---
        assert modal.is_visible(), "Modal closed after adding mapping — expected to stay open"
        modal_after = modal.inner_text()
        assert "No matching type" not in modal_after, f"Modal still shows 'No matching type'.\nModal:\n{modal_after}"

        # --- Assert 2: row updated without page refresh ---
        import_btn = row.locator("button.btn-success.device-import-btn:not([disabled])")
        assert import_btn.count() > 0, (
            "No enabled Import button in row after mapping — "
            "JS deviceMappingAdded handler may not have triggered the row update.\n"
            f"Row: {row.inner_text()}"
        )
