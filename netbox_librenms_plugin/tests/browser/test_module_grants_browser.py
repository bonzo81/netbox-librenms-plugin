"""Browser checks for module grants: member verifies lock row writes, and a verified override travels with its row."""

import json
from urllib.parse import parse_qsl

import pytest
from playwright.sync_api import expect

from test_sync_cache_browser import HTMX_SCRIPT, SCRIPT_PATH

PAGE_URL = "https://plugin.example.com/dcim/devices/1/librenms-sync/"
SUBMIT_URL = "https://plugin.example.com/install-selected/"
VERIFY_ROUTE = "**/plugins/librenms_plugin/verify-module/"
POLICY_REFUSAL = "Inventory rules or device identity changed. Refresh Modules and try again."
# The attributes _module_sync_content.html renders on the Install Selected form.
FORM_ATTRS = (
    f'method="post" action="{SUBMIT_URL}" hx-post="{SUBMIT_URL}" hx-target="#module-sync-content"'
    ' hx-swap="innerHTML" hx-sync="#module-sync-content:drop" hx-disabled-elt="find button"'
)


def _member_cell(index, member, grant="", binding=None, marked=True):
    """*marked* False renders no option[selected], so the browser shows the first option."""
    options = "".join(
        f'<option value="{value}"{" selected" if marked and value == member else ""}>m{value}</option>'
        for value in ("1", "2")
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


def _row(index, *, member="1", grant="", checked=False, marked=True):
    checkbox = f'<input type="checkbox" name="select" value="{index}" id="cb-{index}"{" checked" if checked else ""}>'
    cells = "".join(
        f'<td data-col="{col}">{col}-{index}</td>'
        for col in ("name", "model", "serial", "description", "item_class", "module_bay", "module_type", "status")
    )
    return (
        f'<tr data-ent-index="{index}" data-depth="0"><td data-col="selection">{checkbox}</td>'
        f'<td data-col="device_selection">{_member_cell(index, member, grant, marked=marked)}</td>{cells}'
        f'<td data-col="actions">{_actions_cell(index, member)}</td></tr>'
    )


def _module_page(rows, mode):
    disable = " hx-disable" if mode == "htmx-disabled" else ""
    return f"""<!doctype html><html><body>
        <div id="django-messages"></div>
        <div id="module-sync-content"{disable}>
        <input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">
        <form id="install-selected-form" {FORM_ATTRS}>
          <input type="hidden" name="server_key" value="production">
          <input type="hidden" name="inventory_binding" value="manifest-token">
          <button type="submit" id="install-selected-btn">Install Selected</button>
        </form>
        <table id="librenms-module-table" data-page-device="1" data-selection-snapshot="snapshot:policy">
          <tbody>{"".join(rows)}</tbody>
        </table>
        </div>
        </body></html>"""


def _load(page, rows, url=PAGE_URL, mode="htmx"):
    """Serve one page of the module table; *mode* "htmx-absent" loads no htmx, as when its script fails."""
    html = _module_page(rows, mode)
    page.route(
        lambda candidate: candidate.split("?")[0] == PAGE_URL,
        lambda route: route.fulfill(body=html, content_type="text/html"),
    )
    page.goto(url)
    if mode != "htmx-absent":
        page.add_script_tag(content=HTMX_SCRIPT)
    page.add_script_tag(path=str(SCRIPT_PATH))
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


def _answers(page, *answers):
    """Answer successive verify requests in order; an answer of None holds that request for the test."""
    held, bodies = [], []
    pending = list(answers)

    def handler(route):
        bodies.append(json.loads(route.request.post_data))
        answer = pending.pop(0)
        if answer is None:
            held.append(route)
        else:
            route.fulfill(**answer)

    page.route(VERIFY_ROUTE, handler)
    return held, bodies


def _success(index, member, grant):
    return {"json": {"status": "success", "formatted_row": _verified_row(index, member, grant)}}


REFUSED = {"status": 409, "json": {"status": "error", "message": POLICY_REFUSAL}}


def _change_member(page, index, member):
    page.select_option(f"#device_selection_{index}", member)


def _toasts(page):
    return page.locator("#django-messages .toast-body")


def _row_target(page, index):
    return page.locator(f'tr[data-ent-index="{index}"] td[data-col="actions"] [name="selected_device_id"]')


def test_a_pending_member_verify_locks_the_row_writes_and_install_selected(page):
    held, _bodies = _answers(page, None)
    _load(page, [_row(30, checked=True)])

    with page.expect_request(VERIFY_ROUTE) as verify:
        _change_member(page, 30, "2")
    body = json.loads(verify.value.post_data)

    assert (body["page_device_id"], body["verify_binding"], body["device_id"]) == (
        "1",
        "verify-30-1",
        "2",
    )
    expect(page.locator("#install-30")).to_be_disabled()
    expect(page.locator("#cb-30")).to_be_disabled()
    expect(page.locator("#install-selected-btn")).to_be_disabled()
    assert page.locator('[name="selection_grant_30"]').input_value() == ""

    page.wait_for_function("document.querySelector('tr[data-ent-index=\"30\"]').dataset.verifyPending === 'true'")
    held[0].fulfill(**_success(30, "2", "override-30"))

    expect(page.locator('td[data-col="name"]')).to_have_text("name-30-m2")
    expect(page.locator("#install-30")).to_be_enabled()
    expect(page.locator("#install-selected-btn")).to_be_enabled()
    expect(page.locator("#cb-30")).to_be_checked()
    assert page.locator('[name="selection_grant_30"]').input_value() == "override-30"


def test_a_refused_member_verify_restores_the_member_and_its_grant_and_says_why(page):
    _answers(page, REFUSED)
    _load(page, [_row(30, grant="earlier-override", member="1")])

    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")

    expect(_toasts(page)).to_contain_text(POLICY_REFUSAL)
    expect(_toasts(page)).to_contain_text("The row keeps its previous member.")
    expect(page.locator("#install-30")).to_be_enabled()
    expect(page.locator("#install-selected-btn")).to_be_enabled()
    assert page.locator("#device_selection_30").input_value() == "1"
    assert page.locator('[name="selection_grant_30"]').input_value() == "earlier-override"


def test_a_refusal_without_a_full_stop_reads_as_its_own_sentence(page):
    """The verify view answers a dropped snapshot with "No cached inventory data", which has no full stop."""
    _answers(page, {"status": 404, "json": {"status": "error", "message": "No cached inventory data"}})
    _load(page, [_row(30)])

    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")

    expect(_toasts(page)).to_have_text("No cached inventory data. The row keeps its previous member.")


@pytest.mark.parametrize("mode", ["htmx", "htmx-absent", "htmx-disabled"])
def test_a_verified_override_travels_with_its_row_across_pages(page, mode):
    """The form keeps its production hx-post in every mode; without htmx the native submit carries the rows."""
    _answers(page, _success(30, "2", "override-30"))
    page.route(SUBMIT_URL, lambda route: route.fulfill(body="", content_type="text/html"))
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1", mode=mode)
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    expect(page.locator("#install-selected-btn")).to_be_enabled()
    page.check("#cb-30")

    _load(page, [_row(40)], url=f"{PAGE_URL}?page=2", mode=mode)
    page.check("#cb-40")
    with page.expect_request(SUBMIT_URL) as submitted:
        page.click("#install-selected-btn")
    pairs = parse_qsl(submitted.value.post_data)

    assert sorted(value for name, value in pairs if name == "select") == ["30", "40"]
    assert ("device_selection_30", "2") in pairs
    assert ("selection_grant_30", "override-30") in pairs
    assert ("inventory_binding", "manifest-token") in pairs


def test_a_restored_retarget_writes_nothing_until_its_member_is_verified_again(page):
    """The page renders the row's forms for the rendered member, so the restored member is verified before any write."""
    held, bodies = _answers(page, _success(30, "2", "override-30"), None)
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    page.check("#cb-30")
    _load(page, [_row(40)], url=f"{PAGE_URL}?page=2")

    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")

    page.wait_for_function("document.querySelector('tr[data-ent-index=\"30\"]').dataset.verifyPending === 'true'")
    assert (bodies[1]["device_id"], bodies[1]["verify_binding"]) == ("2", "verify-30-1")
    assert page.locator("#device_selection_30").input_value() == "2"
    # The stored grant is not reused, and the rendered forms of member 1 stay locked.
    assert page.locator('[name="selection_grant_30"]').input_value() == ""
    expect(_row_target(page, 30)).to_have_value("1")
    expect(page.locator("#install-30")).to_be_disabled()
    expect(page.locator("#install-selected-btn")).to_be_disabled()

    held[0].fulfill(**_success(30, "2", "override-30-again"))

    expect(page.locator("#install-30")).to_be_enabled()
    expect(_row_target(page, 30)).to_have_value("2")
    assert page.locator('[name="selection_grant_30"]').input_value() == "override-30-again"
    expect(page.locator("#cb-30")).to_be_checked()
    expect(page.locator("#install-selected-btn")).to_be_enabled()


def test_a_refused_reverify_clears_the_restored_selection_and_says_why(page):
    _answers(page, _success(30, "2", "override-30"), REFUSED)
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    page.check("#cb-30")
    _load(page, [_row(40)], url=f"{PAGE_URL}?page=2")

    with page.expect_response(VERIFY_ROUTE):
        _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")

    expect(_toasts(page)).to_contain_text(POLICY_REFUSAL)
    expect(_toasts(page)).to_contain_text("The saved selection of this row is cleared.")
    expect(page.locator("#cb-30")).not_to_be_checked()
    assert page.locator("#device_selection_30").input_value() == "1"
    stored = page.evaluate("Object.keys(readStoredSelection(document.getElementById('librenms-module-table')))")
    assert "30" not in stored


def test_a_refused_reverify_of_a_select_without_a_marked_option_releases_the_row(page):
    """The rendered member is the browser's default option, so the rollback goes back to it and unlocks the row."""
    _answers(page, _success(30, "2", "override-30"), REFUSED)
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    page.check("#cb-30")
    _load(page, [_row(40)], url=f"{PAGE_URL}?page=2")

    with page.expect_response(VERIFY_ROUTE):
        _load(page, [_row(30, grant="earlier-override", marked=False), _row(31)], url=f"{PAGE_URL}?page=1")

    expect(_toasts(page)).to_contain_text("The saved selection of this row is cleared.")
    expect(page.locator('tr[data-ent-index="30"][data-verify-pending]')).to_have_count(0)
    expect(page.locator("#install-30")).to_be_enabled()
    expect(page.locator("#install-selected-btn")).to_be_enabled()
    assert page.locator("#device_selection_30").input_value() == "1"
    assert page.locator('[name="selection_grant_30"]').input_value() == "earlier-override"
