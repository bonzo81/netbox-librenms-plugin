"""Browser checks for module grants: a pending member verify locks row writes, and a verified override travels."""

import json
from urllib.parse import parse_qsl

import pytest
from playwright.sync_api import expect

from test_sync_cache_browser import _add_page_scripts

PAGE_URL = "https://plugin.example.com/dcim/devices/1/librenms-sync/"
SUBMIT_URL = "https://plugin.example.com/install-selected/"
VERIFY_ROUTE = "**/plugins/librenms_plugin/verify-module/"


def _member_cell(index, member, grant="", binding=None):
    options = "".join(
        f'<option value="{value}"{" selected" if value == member else ""}>m{value}</option>' for value in ("1", "2")
    )
    return (
        f'<select name="device_selection_{index}" id="device_selection_{index}" class="form-select vc-member-select"'
        f' data-module="{index}" data-row-id="{index}" data-verify-binding="{binding or f"verify-{index}-{member}"}">'
        f'{options}</select><input type="hidden" name="selection_grant_{index}" value="{grant}">'
    )


def _actions_cell(index, member):
    return (
        f'<form method="post" action="/install-module/"><input type="hidden" name="selected_device_id" value="{member}">'
        f'<input type="hidden" name="inventory_binding" value="install-{index}-{member}">'
        f'<button type="submit" data-action="install" id="install-{index}">Install</button></form>'
    )


def _row(index, *, member="1", grant="", checked=False):
    checkbox = f'<input type="checkbox" name="select" value="{index}" id="cb-{index}"{" checked" if checked else ""}>'
    cells = "".join(
        f'<td data-col="{col}">{col}-{index}</td>'
        for col in ("name", "model", "serial", "description", "item_class", "module_bay", "module_type", "status")
    )
    return (
        f'<tr data-ent-index="{index}" data-depth="0"><td data-col="selection">{checkbox}</td>'
        f'<td data-col="device_selection">{_member_cell(index, member, grant)}</td>{cells}'
        f'<td data-col="actions">{_actions_cell(index, member)}</td></tr>'
    )


def _module_page(rows, htmx=False):
    htmx_attrs = f' hx-post="{SUBMIT_URL}" hx-swap="none"' if htmx else ""
    return f"""<!doctype html><html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">
        <form id="install-selected-form" method="post" action="{SUBMIT_URL}"{htmx_attrs}>
          <input type="hidden" name="server_key" value="production">
          <input type="hidden" name="inventory_binding" value="manifest-token">
          <button type="submit" id="install-selected-btn">Install Selected</button>
        </form>
        <table id="librenms-module-table" data-page-device="1" data-selection-snapshot="snapshot:policy">
          <tbody>{"".join(rows)}</tbody>
        </table>
        </body></html>"""


def _load(page, rows, url=PAGE_URL, htmx=False):
    html = _module_page(rows, htmx)
    page.route(
        lambda candidate: candidate.split("?")[0] == PAGE_URL,
        lambda route: route.fulfill(body=html, content_type="text/html"),
    )
    page.goto(url)
    _add_page_scripts(page)
    # The page script wires its htmx:configRequest injector on DOMContentLoaded, which already fired here.
    page.evaluate("document.dispatchEvent(new Event('DOMContentLoaded'))")
    page.wait_for_function(
        "document.getElementById('device_selection_' + %s).dataset.moduleSelectInitialized"
        % json.dumps(str(_first_index(rows)))
    )


def _first_index(rows):
    return rows[0].split('data-ent-index="', 1)[1].split('"', 1)[0]


def _verified_row(index, member, grant):
    """The formatted row the verify view answers with for *member*."""
    return {
        "selection": f'<input type="checkbox" name="select" value="{index}" id="cb-{index}">',
        "device_selection": _member_cell(index, member, grant, binding=f"verify-{index}-{member}-pinned"),
        "name": f"name-{index}-m{member}",
        "model": "model",
        "serial": "serial",
        "description": "description",
        "item_class": "module",
        "module_bay": "bay",
        "module_type": "type",
        "status": "Matched",
        "actions": _actions_cell(index, member),
    }


def _change_member(page, index, member):
    page.select_option(f"#device_selection_{index}", member)


def test_a_pending_member_verify_locks_the_row_writes_and_install_selected(page):
    held = []
    page.route(VERIFY_ROUTE, lambda route: held.append(route))
    _load(page, [_row(30, checked=True)])

    with page.expect_request(VERIFY_ROUTE) as verify:
        _change_member(page, 30, "2")
    body = json.loads(verify.value.post_data)

    assert (body["page_device_id"], body["verify_binding"], body["device_id"]) == ("1", "verify-30-1", "2")
    expect(page.locator("#install-30")).to_be_disabled()
    expect(page.locator("#cb-30")).to_be_disabled()
    expect(page.locator("#install-selected-btn")).to_be_disabled()
    assert page.locator('[name="selection_grant_30"]').input_value() == ""

    held[0].fulfill(json={"status": "success", "formatted_row": _verified_row(30, "2", "override-30")})

    expect(page.locator('td[data-col="name"]')).to_have_text("name-30-m2")
    expect(page.locator("#install-30")).to_be_enabled()
    expect(page.locator("#install-selected-btn")).to_be_enabled()
    expect(page.locator("#cb-30")).to_be_checked()
    assert page.locator('[name="selection_grant_30"]').input_value() == "override-30"


def test_a_refused_member_verify_restores_the_member_and_its_grant(page):
    page.route(
        VERIFY_ROUTE, lambda route: route.fulfill(status=404, json={"status": "error", "message": "not available"})
    )
    _load(page, [_row(30, grant="earlier-override", member="1")])

    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")

    expect(page.locator("#install-30")).to_be_enabled()
    expect(page.locator("#install-selected-btn")).to_be_enabled()
    assert page.locator("#device_selection_30").input_value() == "1"
    assert page.locator('[name="selection_grant_30"]').input_value() == "earlier-override"


@pytest.mark.parametrize("htmx", [False, True], ids=["native-submit", "htmx-submit"])
def test_a_verified_override_travels_with_its_row_across_pages(page, htmx):
    page.route(
        VERIFY_ROUTE,
        lambda route: route.fulfill(json={"status": "success", "formatted_row": _verified_row(30, "2", "override-30")}),
    )
    page.route(SUBMIT_URL, lambda route: route.fulfill(body="", content_type="text/html"))
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1", htmx=htmx)
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    expect(page.locator("#install-selected-btn")).to_be_enabled()
    page.check("#cb-30")

    _load(page, [_row(40)], url=f"{PAGE_URL}?page=2", htmx=htmx)
    page.check("#cb-40")
    with page.expect_request(SUBMIT_URL) as submitted:
        page.click("#install-selected-btn")
    pairs = parse_qsl(submitted.value.post_data)

    assert ("select", "30") in pairs and ("select", "40") in pairs
    assert ("device_selection_30", "2") in pairs
    assert ("selection_grant_30", "override-30") in pairs
    assert ("inventory_binding", "manifest-token") in pairs


def test_a_restored_row_brings_back_its_member_and_override(page):
    page.route(
        VERIFY_ROUTE,
        lambda route: route.fulfill(json={"status": "success", "formatted_row": _verified_row(30, "2", "override-30")}),
    )
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    page.check("#cb-30")

    _load(page, [_row(40)], url=f"{PAGE_URL}?page=2")
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")

    expect(page.locator("#cb-30")).to_be_checked()
    assert page.locator("#device_selection_30").input_value() == "2"
    assert page.locator('[name="selection_grant_30"]').input_value() == "override-30"
