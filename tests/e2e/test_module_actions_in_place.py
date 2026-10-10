"""
In-place module row actions on the device sync page.

Every module action (Install, Install Selected, and the mismatch modal's Update Serial Only)
answers the HTMX post with the module tab fragment. ``#module-sync-content`` is swapped in place,
the toasts arrive out of band, the modal closes through ``HX-Trigger: closeModal`` and the
browser never navigates. The device is linked to the stub's ArcOS recording, whose optics are
flat ``port`` rows directly under the chassis.
"""

import re

import pytest
from playwright.sync_api import expect

from . import module_tab as tab
from .conftest import recorded

LIBRENMS_ID = 1
OPTICS = [
    item
    for item in recorded("arcos-lag-transceivers", f"/api/v0/inventory/{LIBRENMS_ID}/all")["inventory"]
    if item["entPhysicalClass"] == "port"
]
# The bay matcher pairs an inventory item with the bay of the same name.
NAMES = [item["entPhysicalName"] for item in OPTICS]
SERIAL = {item["entPhysicalName"]: item["entPhysicalSerialNum"] for item in OPTICS}
MODELS = sorted({item["entPhysicalModelName"] for item in OPTICS})


@pytest.fixture(scope="module")
def seeded_device(module_device) -> dict:
    """Create the device, bays and module types that the stub inventory matches."""
    return module_device(LIBRENMS_ID, NAMES, {model: [] for model in MODELS})


@pytest.fixture
def modules_page(logged_in_page, netbox_url, netbox_api, seeded_device):
    """Leave the device with no modules, and open its module tab with fresh inventory."""
    netbox_api.delete_modules(seeded_device["id"])
    tab.open_modules_tab(logged_in_page, netbox_url, seeded_device["id"])
    return logged_in_page


def test_refresh_then_install_swaps_in_place(modules_page, netbox_api, seeded_device):
    """Installing a module swaps the module tab in place and reports it in a toast."""
    page = modules_page
    url_before = page.url
    tab.refresh_modules(page, NAMES[0])

    assert tab.forms_bound(page), "row forms are not HTMX-bound after the refresh"
    expect(tab.row(page, NAMES[0])).to_have_attribute("data-status", "Matched")
    module_type = tab.row(page, NAMES[0]).locator("td[data-col=module_type]").inner_text().strip()

    request = tab.install_row(page, NAMES[0])

    assert request.headers.get("hx-request") == "true", "the install did not go out as an HTMX request"
    tab.assert_no_navigation(page, url_before)
    expect(tab.row(page, NAMES[0])).to_have_attribute("data-status", "Installed")
    expect(tab.toasts(page)).to_contain_text(f"Installed {module_type} in {NAMES[0]}")
    assert tab.forms_bound(page), "row forms lost their HTMX binding after the swap"
    assert netbox_api.installed_modules(seeded_device["id"]) == {NAMES[0]: (module_type, SERIAL[NAMES[0]])}


def test_install_toast_renders_once(modules_page):
    """The page keeps one toast container, so the install message shows once and is visible."""
    page = modules_page
    expect(tab.toasts(page)).to_have_count(1)
    tab.refresh_modules(page, NAMES[0])

    tab.install_row(page, NAMES[0])

    expect(tab.toasts(page)).to_have_count(1)
    toast = page.locator(".toast", has_text=re.compile(rf"Installed \S+ in {re.escape(NAMES[0])} "))
    expect(toast).to_have_count(1)
    expect(toast).to_be_visible()


def test_dismissed_toast_stays_hidden_after_a_later_swap(modules_page):
    """NetBox shows every toast that is not showing after each HTMX swap, so a dismissed toast must not come back."""
    page = modules_page
    tab.refresh_modules(page, NAMES[0])
    tab.install_row(page, NAMES[0])
    toasts = tab.toasts(page).locator(".toast")
    expect(toasts.first).to_be_visible()
    # One DOM pass: a toast that auto-hides leaves the page, so a list of buttons taken first can go stale.
    page.eval_on_selector_all(
        "#django-messages .toast [data-bs-dismiss=toast]", "buttons => buttons.forEach(b => b.click())"
    )
    expect(toasts.filter(visible=True)).to_have_count(0)

    page.get_by_role("button", name="Capture data shape").click()
    page.wait_for_selector("#htmx-modal.show")

    expect(toasts.filter(visible=True)).to_have_count(0)


def test_second_action_in_a_row_also_swaps(modules_page, netbox_api, seeded_device):
    """A second row action right after the first one swaps in place as well."""
    page = modules_page
    url_before = page.url
    tab.refresh_modules(page, NAMES[0])

    tab.install_row(page, NAMES[0])
    expect(tab.row(page, NAMES[0])).to_have_attribute("data-status", "Installed")

    request = tab.install_row(page, NAMES[1])

    assert request.headers.get("hx-request") == "true", "the second install did not go out as an HTMX request"
    tab.assert_no_navigation(page, url_before)
    expect(tab.row(page, NAMES[1])).to_have_attribute("data-status", "Installed")
    assert tab.forms_bound(page), "row forms lost their HTMX binding after the second swap"
    assert sorted(netbox_api.installed_modules(seeded_device["id"])) == sorted(NAMES[:2])


def test_mismatch_modal_updates_serial_in_place(modules_page, netbox_api, seeded_device):
    """Update Serial Only closes the mismatch modal and swaps the tab in place."""
    page = modules_page
    url_before = page.url
    tab.refresh_modules(page, NAMES[0])
    tab.install_row(page, NAMES[0])
    expect(tab.row(page, NAMES[0])).to_have_attribute("data-status", "Installed")

    (module,) = netbox_api.list("dcim/modules", device_id=seeded_device["id"])
    netbox_api.update(f"dcim/modules/{module['id']}", {"serial": "E2E-WRONG-SERIAL"})
    tab.refresh_modules(page, NAMES[0])
    expect(tab.row(page, NAMES[0])).to_have_attribute("data-status", "Serial Mismatch")

    tab.dom_click(page, f"{tab.row_selector(NAMES[0])} button[hx-get]")
    page.wait_for_selector("#htmx-modal.show #htmx-modal-content form[hx-post]")
    assert tab.forms_bound(page, "#htmx-modal-content"), "modal forms are not HTMX-bound"
    expect(page.locator("#htmx-modal-content .modal-title")).to_have_text("Module Mismatch")

    tab.mark_table(page)
    with page.expect_request(
        lambda request: "/update-module-serial/" in request.url and request.method == "POST"
    ) as update:
        tab.dom_click(page, "#htmx-modal-content form[hx-post] button:has-text('Update Serial Only')")
    tab.wait_for_table_swap(page)

    assert update.value.headers.get("hx-request") == "true", "the serial update did not go out as an HTMX request"
    tab.assert_no_navigation(page, url_before)
    # NetBox's base template ships a second element with that id, so count the open ones.
    expect(page.locator("#htmx-modal.show")).to_have_count(0)
    expect(tab.toasts(page)).to_contain_text("Updated serial for")
    expect(tab.row(page, NAMES[0])).to_have_attribute("data-status", "Installed")
    assert netbox_api.installed_modules(seeded_device["id"])[NAMES[0]][1] == SERIAL[NAMES[0]]


def test_restored_content_after_refresh_keeps_bindings(modules_page):
    """The status check restores the tab through the HTMX loader, so the forms stay bound."""
    page = modules_page
    request = tab.refresh_modules(page, NAMES[0])

    assert request.headers.get("hx-request") == "true", "the cache fragment was not fetched through HTMX"
    assert tab.forms_bound(page), "restored row forms are not HTMX-bound"
    assert page.eval_on_selector(
        "#modules [data-fragment-loader]",
        "loader => !!loader['htmx-internal-data']",
    ), "the fragment loader itself is not HTMX-bound"


def test_second_click_while_in_flight_is_dropped(modules_page, netbox_api, seeded_device):
    """hx-sync drops a second row action that starts while the first one is in flight."""
    page = modules_page
    url_before = page.url
    tab.refresh_modules(page, NAMES[0])

    posts = []
    page.on(
        "request",
        lambda request: (
            posts.append(request.url) if "/install-module/" in request.url and request.method == "POST" else None
        ),
    )

    tab.mark_table(page)
    # Both clicks run in one task, so the second one starts while the first POST is in flight.
    page.evaluate(
        """names => {
            const rowFor = name => Array.from(document.querySelectorAll('#librenms-module-table tbody tr'))
                .find(row => row.querySelector('td[data-col=name]')?.innerText.trim() === name);
            for (const name of names) rowFor(name).querySelector('form[hx-post] button[type=submit]').click();
        }""",
        NAMES[:2],
    )
    tab.wait_for_table_swap(page)

    assert len(posts) == 1, f"expected one install POST, got {len(posts)}"
    tab.assert_no_navigation(page, url_before)
    expect(tab.row(page, NAMES[0])).to_have_attribute("data-status", "Installed")
    expect(tab.row(page, NAMES[1])).to_have_attribute("data-status", "Matched")
    assert sorted(netbox_api.installed_modules(seeded_device["id"])) == [NAMES[0]]


def test_install_selected_swaps_in_place(modules_page, netbox_api, seeded_device):
    """Install Selected installs every checked row and swaps the tab in place."""
    page = modules_page
    url_before = page.url
    tab.refresh_modules(page, NAMES[0])

    page.evaluate(
        """names => {
            const rowFor = name => Array.from(document.querySelectorAll('#librenms-module-table tbody tr'))
                .find(row => row.querySelector('td[data-col=name]')?.innerText.trim() === name);
            for (const name of names) rowFor(name).querySelector('input[name=select]').checked = true;
        }""",
        NAMES[2:4],
    )
    tab.mark_table(page)
    with page.expect_request(
        lambda request: "/install-selected/" in request.url and request.method == "POST"
    ) as install:
        tab.dom_click(page, "#install-selected-form button[type=submit]")
    tab.wait_for_table_swap(page)

    assert install.value.headers.get("hx-request") == "true", "the bulk install did not go out as an HTMX request"
    tab.assert_no_navigation(page, url_before)
    expect(tab.row(page, NAMES[2])).to_have_attribute("data-status", "Installed")
    expect(tab.row(page, NAMES[3])).to_have_attribute("data-status", "Installed")
    expect(tab.toasts(page)).to_contain_text("Installed 2 module(s)")
    assert sorted(netbox_api.installed_modules(seeded_device["id"])) == sorted(NAMES[2:4])
