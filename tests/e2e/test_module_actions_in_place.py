"""
End-to-end Playwright tests for the in-place module row actions.

Every module action on the device sync page (Install, Install Selected, and the
mismatch modal's Update Serial Only) answers the HTMX post with the module tab
fragment. ``#module-sync-content`` is swapped in place, the toasts arrive out of
band, the modal closes through ``HX-Trigger: closeModal`` and the browser never
navigates. These tests drive that through the real UI against a live NetBox and
the LibreNMS stub server.

Prerequisites:
    - NetBox running at NETBOX_URL
    - The LibreNMS stub registered under the plugin server key in E2E_STUB_SERVER_KEY
      and reachable from the NetBox process
    - Playwright installed: pip install playwright && playwright install chromium

Configuration (environment variables): see ``tests/e2e/conftest.py``, plus
    E2E_STUB_SERVER_KEY=<key>    Plugin server key of the stub (default "stub")
    E2E_STUB_DEVICE_ID=<id>      LibreNMS device id in the stub (default 1)

Run (from a host that has Playwright, where NetBox itself is not importable, so the
repo-root conftest and the coverage addopts have to stay out of the way):
    cd /home/mzieba/workspace/netbox-librenms-plugin
    E2E_TESTS_ENABLED=1 NETBOX_URL=... python -m pytest \\
        tests/e2e/test_module_actions_in_place.py -v -s \\
        -p no:django -o 'addopts=' --confcutdir=tests/e2e
"""

import json
import os

import pytest

from .conftest import NETBOX_URL, netbox_shell

E2E_ENABLED = os.environ.get("E2E_TESTS_ENABLED", "0") == "1"

if not E2E_ENABLED:
    pytest.skip(
        "E2E tests skipped: set E2E_TESTS_ENABLED=1 to run against a live instance",
        allow_module_level=True,
    )

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402  (after the opt-in skip)
from playwright.sync_api import expect  # noqa: E402  (import after the opt-in skip)

SERVER_KEY = os.environ.get("E2E_STUB_SERVER_KEY", "stub")
STUB_DEVICE_ID = int(os.environ.get("E2E_STUB_DEVICE_ID", "1"))

DEVICE_NAME = "e2e-modules-stub"
MANUFACTURER_SLUG = "e2e-modules-mfg"
DEVICE_TYPE_MODEL = "E2E-MODULES-DT"
SITE_SLUG = "e2e-modules-site"
ROLE_SLUG = "e2e-modules-role"

# Port names of the stub inventory recording. The module bays must carry the same
# names, because the bay matcher pairs an inventory item with a bay of that name.
BAY_NAMES = [f"sfp{index}" for index in range(1, 19)] + ["sfp20", "sfp34"]
# entPhysicalModelName values of the same recording, one ModuleType each.
LIBRENMS_MODELS = [
    "T1-QSFP28-LR4",
    "T1-QDD-400G-FR4",
    "T1-QDD-400G-LR4",
    "180-3530-900",
    "LGI-QSFP28LR431",
]

SETUP_CODE = f"""
import json

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Manufacturer,
    ModuleBayTemplate,
    ModuleType,
    Site,
)

from netbox_librenms_plugin.models import ModuleTypeMapping
from netbox_librenms_plugin.utils import set_librenms_device_id

created = {{}}

manufacturer, was_created = Manufacturer.objects.get_or_create(
    slug="{MANUFACTURER_SLUG}", defaults={{"name": "E2E Modules Manufacturer"}}
)
created["manufacturer"] = [manufacturer.pk, was_created]

device_type, was_created = DeviceType.objects.get_or_create(
    manufacturer=manufacturer, model="{DEVICE_TYPE_MODEL}", defaults={{"slug": "e2e-modules-dt"}}
)
created["device_type"] = [device_type.pk, was_created]
for bay_name in {BAY_NAMES!r}:
    ModuleBayTemplate.objects.get_or_create(device_type=device_type, name=bay_name)

created["module_types"] = []
created["module_type_mappings"] = []
for model in {LIBRENMS_MODELS!r}:
    module_type, was_created = ModuleType.objects.get_or_create(manufacturer=manufacturer, model=model)
    created["module_types"].append([module_type.pk, was_created])
    # Scope the mapping to this manufacturer so it beats any global mapping the
    # instance already has for the same LibreNMS model name.
    mapping, was_created = ModuleTypeMapping.objects.get_or_create(
        librenms_model=model,
        manufacturer=manufacturer,
        defaults={{"netbox_module_type": module_type}},
    )
    created["module_type_mappings"].append([mapping.pk, was_created])

site, was_created = Site.objects.get_or_create(slug="{SITE_SLUG}", defaults={{"name": "E2E Modules Site"}})
created["site"] = [site.pk, was_created]

role, was_created = DeviceRole.objects.get_or_create(slug="{ROLE_SLUG}", defaults={{"name": "E2E Modules Role"}})
created["role"] = [role.pk, was_created]

device, was_created = Device.objects.get_or_create(
    name="{DEVICE_NAME}",
    defaults={{"device_type": device_type, "role": role, "site": site, "status": "active"}},
)
created["device"] = [device.pk, was_created]

set_librenms_device_id(device, {STUB_DEVICE_ID}, "{SERVER_KEY}")
device.save()
print("E2E_SETUP " + json.dumps(created))
"""

TEARDOWN_CODE = """
import json

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, ModuleType, Site

from netbox_librenms_plugin.models import ModuleTypeMapping

created = json.loads({payload!r})


def own_pks(key):
    entries = created.get(key) or []
    if entries and not isinstance(entries[0], list):
        entries = [entries]
    return [pk for pk, was_created in entries if was_created]


for model, key in (
    (Device, "device"),
    (ModuleTypeMapping, "module_type_mappings"),
    (ModuleType, "module_types"),
    (DeviceType, "device_type"),
    (Manufacturer, "manufacturer"),
    (Site, "site"),
    (DeviceRole, "role"),
):
    pks = own_pks(key)
    if pks:
        model.objects.filter(pk__in=pks).delete()
print("E2E_TEARDOWN done")
"""


def _shell_json(code, marker):
    """Run shell code and return the JSON object printed after a marker."""
    output = netbox_shell(code)
    for line in output.split("\n"):
        if line.startswith(marker):
            return json.loads(line[len(marker) :].strip())
    raise AssertionError(f"marker {marker!r} not found in shell output: {output}")


@pytest.fixture(scope="module")
def stub_device():
    """Create the device, bays and module types the stub inventory matches."""
    created = _shell_json(SETUP_CODE, "E2E_SETUP")
    yield {"pk": created["device"][0], "created": created}
    netbox_shell(TEARDOWN_CODE.format(payload=json.dumps(created)))


@pytest.fixture(autouse=True)
def clean_modules(stub_device):
    """Leave the device with no installed modules before each test."""
    _delete_modules(stub_device["pk"])


def _delete_modules(device_pk):
    """Remove every module of the device."""
    netbox_shell(f"from dcim.models import Module; Module.objects.filter(device_id={device_pk}).delete()")


def _installed_modules(device_pk):
    """Return {bay name: (module type model, serial)} for the device's modules."""
    return _shell_json(
        "import json\n"
        "from dcim.models import Module\n"
        f"rows = Module.objects.filter(device_id={device_pk}).select_related('module_bay', 'module_type')\n"
        'print("E2E_MODULES " + json.dumps({m.module_bay.name: [m.module_type.model, m.serial] for m in rows}))',
        "E2E_MODULES",
    )


def _set_module_serial(device_pk, bay_name, serial):
    """Write a serial straight to the installed module of one bay."""
    netbox_shell(
        "from dcim.models import Module\n"
        f"module = Module.objects.get(device_id={device_pk}, module_bay__name='{bay_name}')\n"
        f"module.serial = '{serial}'\n"
        "module.save()"
    )


def _sync_url(device_pk):
    """Return the module sync tab URL for the stub server."""
    return f"{NETBOX_URL}/dcim/devices/{device_pk}/librenms-sync/?tab=modules&server_key={SERVER_KEY}"


def _dom_click(page, selector):
    """Click through the DOM, because NetBox's fixed toast container can cover a control."""
    page.eval_on_selector(selector, "element => element.click()")


def _row_selector(name):
    """Return the CSS selector of one inventory item's table row."""
    return f"#librenms-module-table tbody tr:has(td[data-col=name]:text-is('{name}'))"


def _row(page, name):
    """Return the table row locator of one inventory item."""
    return page.locator(_row_selector(name))


def _toasts(page):
    """Return the toast container locator; every sync-tab partial repeats that id."""
    return page.locator("#django-messages").first


def _row_forms_bound(page, selector="#librenms-module-table"):
    """Report whether every hx-post form under a selector carries its HTMX binding."""
    return page.eval_on_selector_all(
        f"{selector} form[hx-post]",
        "forms => forms.length > 0 && forms.every(form => !!form['htmx-internal-data'])",
    )


def _open_modules_tab(page, device_pk):
    """Open the module sync tab and mark the page so a reload is detectable."""
    page.goto(_sync_url(device_pk), timeout=30000)
    page.wait_for_selector('button:has-text("Refresh Modules")', timeout=20000)
    page.evaluate("window.__marker = 1")


def _refresh_modules(page):
    """Click Refresh Modules and return the cache-fragment request the status check made."""
    # The status check restores the tab through the loader, so wait for that response:
    # it is the swap the next action has to act on.
    with page.expect_response(
        lambda response: "/sync-cache-fragment/modules/" in response.url, timeout=60000
    ) as fragment:
        _dom_click(page, 'button:has-text("Refresh Modules")')
    page.wait_for_selector("#librenms-module-table tbody tr", timeout=60000)
    expect(_row(page, BAY_NAMES[0])).to_have_count(1)
    return fragment.value.request


def _mark_table(page):
    """Flag the current table node so the next swap is observable."""
    page.eval_on_selector("#librenms-module-table", "table => table.dataset.e2eStale = '1'")


def _wait_for_table_swap(page):
    """Wait until a table without the stale flag replaced the flagged one."""
    try:
        page.wait_for_selector("#librenms-module-table:not([data-e2e-stale])", timeout=30000)
    except PlaywrightTimeoutError as error:
        pane = page.eval_on_selector(
            "#module-sync-content", "pane => pane.innerText.replace(/\\s+/g, ' ').slice(0, 200)"
        )
        raise AssertionError(f"the module tab was not swapped in place. It now reads: {pane}") from error


def _assert_no_navigation(page, url_before):
    """Assert the page never reloaded: the marker survives and the URL is unchanged."""
    assert page.evaluate("window.__marker") == 1, "page reloaded: the in-page marker is gone"
    assert page.url == url_before, f"page navigated: {page.url} != {url_before}"


def _install_row(page, name):
    """Submit one row's Install form and wait for the in-place swap."""
    _mark_table(page)
    with page.expect_request(
        lambda request: "/install-module/" in request.url and request.method == "POST", timeout=30000
    ) as install:
        _dom_click(page, f"{_row_selector(name)} form[hx-post] button[type=submit]")
    _wait_for_table_swap(page)
    return install.value


def test_refresh_then_install_swaps_in_place(page, stub_device):
    """Installing a module swaps the module tab in place and reports it in a toast."""
    device_pk = stub_device["pk"]
    _open_modules_tab(page, device_pk)
    url_before = page.url
    _refresh_modules(page)

    assert _row_forms_bound(page), "row forms are not HTMX-bound after the refresh"
    expect(_row(page, "sfp1")).to_have_attribute("data-status", "Matched")
    module_type = _row(page, "sfp1").locator("td[data-col=module_type]").inner_text().strip()

    request = _install_row(page, "sfp1")

    assert request.headers.get("hx-request") == "true", "the install did not go out as an HTMX request"
    _assert_no_navigation(page, url_before)
    expect(_row(page, "sfp1")).to_have_attribute("data-status", "Installed")
    expect(_toasts(page)).to_contain_text(f"Installed {module_type} in sfp1")
    assert _row_forms_bound(page), "row forms lost their HTMX binding after the swap"
    assert _installed_modules(device_pk) == {"sfp1": [module_type, "SN-2a1946"]}


def test_second_action_in_a_row_also_swaps(page, stub_device):
    """A second row action right after the first one swaps in place as well."""
    device_pk = stub_device["pk"]
    _open_modules_tab(page, device_pk)
    url_before = page.url
    _refresh_modules(page)

    _install_row(page, "sfp1")
    expect(_row(page, "sfp1")).to_have_attribute("data-status", "Installed")

    request = _install_row(page, "sfp2")

    assert request.headers.get("hx-request") == "true", "the second install did not go out as an HTMX request"
    _assert_no_navigation(page, url_before)
    expect(_row(page, "sfp2")).to_have_attribute("data-status", "Installed")
    assert _row_forms_bound(page), "row forms lost their HTMX binding after the second swap"
    assert sorted(_installed_modules(device_pk)) == ["sfp1", "sfp2"]


def test_mismatch_modal_updates_serial_in_place(page, stub_device):
    """Update Serial Only closes the mismatch modal and swaps the tab in place."""
    device_pk = stub_device["pk"]
    _open_modules_tab(page, device_pk)
    url_before = page.url
    _refresh_modules(page)
    _install_row(page, "sfp1")
    expect(_row(page, "sfp1")).to_have_attribute("data-status", "Installed")

    _set_module_serial(device_pk, "sfp1", "E2E-WRONG-SERIAL")
    _refresh_modules(page)
    expect(_row(page, "sfp1")).to_have_attribute("data-status", "Serial Mismatch")

    _dom_click(page, f"{_row_selector('sfp1')} button[hx-get]")
    page.wait_for_selector("#htmx-modal.show #htmx-modal-content form[hx-post]", timeout=15000)
    assert _row_forms_bound(page, "#htmx-modal-content"), "modal forms are not HTMX-bound"
    expect(page.locator("#htmx-modal-content #htmx-modal-label")).to_have_text("Module Mismatch")

    _mark_table(page)
    with page.expect_request(
        lambda request: "/update-module-serial/" in request.url and request.method == "POST", timeout=30000
    ) as update:
        _dom_click(page, "#htmx-modal-content form[hx-post] button:has-text('Update Serial Only')")
    _wait_for_table_swap(page)

    assert update.value.headers.get("hx-request") == "true", "the serial update did not go out as an HTMX request"
    _assert_no_navigation(page, url_before)
    # NetBox's base template ships a second element with that id, so count the open ones.
    expect(page.locator("#htmx-modal.show")).to_have_count(0)
    expect(_toasts(page)).to_contain_text("Updated serial for")
    expect(_row(page, "sfp1")).to_have_attribute("data-status", "Installed")
    assert _installed_modules(device_pk)["sfp1"][1] == "SN-2a1946"


def test_restored_content_after_refresh_keeps_bindings(page, stub_device):
    """The status check restores the tab through the HTMX loader, so the forms stay bound."""
    _open_modules_tab(page, stub_device["pk"])
    request = _refresh_modules(page)

    assert request.headers.get("hx-request") == "true", "the cache fragment was not fetched through HTMX"
    assert _row_forms_bound(page), "restored row forms are not HTMX-bound"
    assert page.eval_on_selector(
        "#modules [data-fragment-loader]",
        "loader => !!loader['htmx-internal-data']",
    ), "the fragment loader itself is not HTMX-bound"


def test_second_click_while_in_flight_is_dropped(page, stub_device):
    """hx-sync drops a second row action that starts while the first one is in flight."""
    device_pk = stub_device["pk"]
    _open_modules_tab(page, device_pk)
    url_before = page.url
    _refresh_modules(page)

    posts = []
    page.on(
        "request",
        lambda request: (
            posts.append(request.url) if "/install-module/" in request.url and request.method == "POST" else None
        ),
    )

    _mark_table(page)
    # Both clicks run in one task, so the second one starts while the first POST is in flight.
    page.evaluate(
        """names => {
            const rowFor = name => Array.from(document.querySelectorAll('#librenms-module-table tbody tr'))
                .find(row => row.querySelector('td[data-col=name]')?.innerText.trim() === name);
            for (const name of names) rowFor(name).querySelector('form[hx-post] button[type=submit]').click();
        }""",
        ["sfp1", "sfp2"],
    )
    _wait_for_table_swap(page)

    assert len(posts) == 1, f"expected one install POST, got {len(posts)}"
    _assert_no_navigation(page, url_before)
    expect(_row(page, "sfp1")).to_have_attribute("data-status", "Installed")
    expect(_row(page, "sfp2")).to_have_attribute("data-status", "Matched")
    assert sorted(_installed_modules(device_pk)) == ["sfp1"]


def test_install_selected_swaps_in_place(page, stub_device):
    """Install Selected installs every checked row and swaps the tab in place."""
    device_pk = stub_device["pk"]
    _open_modules_tab(page, device_pk)
    url_before = page.url
    _refresh_modules(page)

    page.evaluate(
        """names => {
            const rowFor = name => Array.from(document.querySelectorAll('#librenms-module-table tbody tr'))
                .find(row => row.querySelector('td[data-col=name]')?.innerText.trim() === name);
            for (const name of names) rowFor(name).querySelector('input[name=select]').checked = true;
        }""",
        ["sfp3", "sfp4"],
    )
    _mark_table(page)
    with page.expect_request(
        lambda request: "/install-selected/" in request.url and request.method == "POST", timeout=30000
    ) as install:
        _dom_click(page, "#install-selected-form button[type=submit]")
    _wait_for_table_swap(page)

    assert install.value.headers.get("hx-request") == "true", "the bulk install did not go out as an HTMX request"
    _assert_no_navigation(page, url_before)
    expect(_row(page, "sfp3")).to_have_attribute("data-status", "Installed")
    expect(_row(page, "sfp4")).to_have_attribute("data-status", "Installed")
    expect(_toasts(page)).to_contain_text("Installed 2 module(s)")
    assert sorted(_installed_modules(device_pk)) == ["sfp3", "sfp4"]
