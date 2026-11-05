"""Browser checks for module grants: member verifies lock row writes, and a verified override travels with its row."""

import json
import re
from urllib.parse import parse_qsl

import pytest
from playwright.sync_api import expect

from test_sync_cache_browser import HTMX_SCRIPT, SCRIPT_PATH, TEMPLATE_DIR

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


def _checkbox(index, *, checked=False, disabled=False):
    return (
        f'<input type="checkbox" name="select" value="{index}" id="cb-{index}"'
        f"{' checked' if checked else ''}{' disabled' if disabled else ''}>"
    )


def _row(index, *, member="1", grant="", checked=False, marked=True, disabled=False):
    checkbox = _checkbox(index, checked=checked, disabled=disabled)
    cells = "".join(
        f'<td data-col="{col}">{col}-{index}</td>'
        for col in ("name", "model", "serial", "description", "item_class", "module_bay", "module_type", "status")
    )
    return (
        f'<tr data-ent-index="{index}" data-depth="0"><td data-col="selection">{checkbox}</td>'
        f'<td data-col="device_selection">{_member_cell(index, member, grant, marked=marked)}</td>{cells}'
        f'<td data-col="actions">{_actions_cell(index, member)}</td></tr>'
    )


def _module_content(rows):
    """The module tab fragment that a refresh or a table page swaps into #module-sync-content."""
    return f"""
        <input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">
        <form id="install-selected-form" {FORM_ATTRS}>
          <input type="hidden" name="server_key" value="production">
          <input type="hidden" name="inventory_binding" value="manifest-token">
          <button type="submit" id="install-selected-btn">Install Selected</button>
        </form>
        <table id="librenms-module-table" data-page-device="1" data-selection-snapshot="snapshot:policy">
          <tbody>{"".join(rows)}</tbody>
        </table>"""


def _module_page(rows, mode, extra=""):
    disable = " hx-disable" if mode == "htmx-disabled" else ""
    return f"""<!doctype html><html><body>
        <div id="django-messages"></div>
        <div id="module-sync-content"{disable}>{_module_content(rows)}
        </div>
        {extra}
        </body></html>"""


def _load(page, rows, url=PAGE_URL, mode="htmx", extra=""):
    """Serve one page of the module table; *mode* "htmx-absent" loads no htmx, as when its script fails."""
    html = _module_page(rows, mode, extra)
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


def _verified_row(index, member, grant, disabled=False):
    """The formatted row the verify view answers with for *member*."""
    return {
        "selection": _checkbox(index, disabled=disabled),
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


def _success(index, member, grant, disabled=False):
    return {"json": {"status": "success", "formatted_row": _verified_row(index, member, grant, disabled)}}


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

    page.wait_for_function("document.querySelector('tr[data-ent-index=\"30\"]').dataset.verifyPending === 'change'")
    held[0].fulfill(**_success(30, "2", "override-30"))

    expect(page.locator('td[data-col="name"]')).to_have_text("name-30-m2")
    expect(page.locator("#install-30")).to_be_enabled()
    expect(page.locator("#install-selected-btn")).to_be_enabled()
    expect(page.locator("#cb-30")).to_be_checked()
    assert page.locator('[name="selection_grant_30"]').input_value() == "override-30"


def test_a_verified_row_checkbox_starts_and_ends_a_shift_click_range(page):
    """The verify writes a new checkbox, which takes part in a shift-click range like a rendered one."""
    _answers(page, _success(30, "2", "override-30"))
    _load(page, [_row(30), _row(31), _row(32)])
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    expect(page.locator("#cb-30")).to_be_enabled()

    page.click("#cb-30")
    page.click("#cb-32", modifiers=["Shift"])

    expect(page.locator("#cb-31")).to_be_checked()
    page.click("#cb-32")
    page.click("#cb-30", modifiers=["Shift"])
    expect(page.locator("#cb-31")).not_to_be_checked()


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

    page.wait_for_function("document.querySelector('tr[data-ent-index=\"30\"]').dataset.verifyPending === 'restore'")
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


def test_a_restored_member_left_before_its_verify_keeps_its_verified_grant(page):
    """The restore pass verifies the stored member again; a page change before the answer keeps the stored pair."""
    page.route(SUBMIT_URL, lambda route: route.fulfill(body="", content_type="text/html"))
    _held, _bodies = _answers(page, _success(30, "2", "override-30"), None)
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    page.check("#cb-30")
    _load(page, [_row(40)], url=f"{PAGE_URL}?page=2")
    with page.expect_request(VERIFY_ROUTE):
        _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")

    # The table pagination swaps page 2 in while the verify of the restored member is still in flight.
    pairs = _swap_page_2_and_install_selected(page)

    assert ("device_selection_30", "2") in pairs
    assert [value for name, value in pairs if name == "selection_grant_30"] == ["override-30"]


def _swap_page_2_and_install_selected(page):
    """Swap page 2 in through the table pagination, select its row and return the Install Selected fields."""
    page.route(f"{PAGE_URL}?page=2&swap=1", lambda route: route.fulfill(body=_module_content([_row(40)])))
    page.evaluate(
        "htmxTest.ajax('GET', %s, {target: '#module-sync-content', swap: 'innerHTML'})"
        % json.dumps(f"{PAGE_URL}?page=2&swap=1")
    )
    page.locator("#cb-40").wait_for()
    page.check("#cb-40")
    with page.expect_request(SUBMIT_URL) as submitted:
        page.click("#install-selected-btn")
    return parse_qsl(submitted.value.post_data, keep_blank_values=True)


def test_a_member_change_left_before_its_verify_never_installs_on_the_previous_member(page):
    """The user picked member 2, so a page change before the answer must not keep the row on member 1."""
    page.route(SUBMIT_URL, lambda route: route.fulfill(body="", content_type="text/html"))
    _answers(page, None)
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")
    page.check("#cb-30")
    with page.expect_request(VERIFY_ROUTE):
        _change_member(page, 30, "2")

    pairs = _swap_page_2_and_install_selected(page)

    assert ("device_selection_30", "1") not in pairs
    # The new member travels without a grant, so the writer refuses the batch instead of installing on member 1.
    assert ("device_selection_30", "2") in pairs
    assert [value for name, value in pairs if name == "selection_grant_30"] == [""]


def test_a_restored_row_on_its_rendered_member_submits_the_grant_of_its_render(page):
    """A change away and back stores a grant for the pinned row; the reloaded row shows the page render again."""
    page.route(SUBMIT_URL, lambda route: route.fulfill(body="", content_type="text/html"))
    _held, bodies = _answers(page, _success(30, "2", "override-30"), _success(30, "1", "pinned-30-m1"))
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")
    for member in ("2", "1"):
        with page.expect_response(VERIFY_ROUTE):
            _change_member(page, 30, member)
        # The verified cell brings a new select, which listens once the page script wires it.
        page.wait_for_function("document.getElementById('device_selection_30').dataset.moduleSelectInitialized")
    page.check("#cb-30")
    assert page.locator('[name="selection_grant_30"]').input_value() == "pinned-30-m1"

    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")

    expect(page.locator("#cb-30")).to_be_checked()
    assert page.locator("#device_selection_30").input_value() == "1"
    with page.expect_request(SUBMIT_URL) as submitted:
        page.click("#install-selected-btn")
    pairs = parse_qsl(submitted.value.post_data, keep_blank_values=True)

    # The reloaded row is the page render again, so it submits that render's empty grant and no verify runs.
    assert len(bodies) == 2
    assert ("device_selection_30", "1") in pairs
    assert [value for name, value in pairs if name == "selection_grant_30"] == [""]


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


@pytest.mark.parametrize("enabled_for_member", [True, False])
def test_a_restored_member_reverifies_a_row_that_its_rendered_member_disables(page, enabled_for_member):
    """The rendered member disables the row; the stored member decides it again, and only an enabled row posts."""
    page.route(SUBMIT_URL, lambda route: route.fulfill(body="", content_type="text/html"))
    restored = _success(30, "2", "override-30-again", disabled=not enabled_for_member)
    _held, bodies = _answers(page, _success(30, "2", "override-30"), restored)
    _load(page, [_row(30, disabled=True), _row(31)], url=f"{PAGE_URL}?page=1")
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    page.check("#cb-30")
    _load(page, [_row(40)], url=f"{PAGE_URL}?page=2")

    with page.expect_response(VERIFY_ROUTE):
        _load(page, [_row(30, disabled=True), _row(31)], url=f"{PAGE_URL}?page=1")

    expect(page.locator('tr[data-ent-index="30"][data-verify-pending]')).to_have_count(0)
    assert (bodies[1]["device_id"], bodies[1]["verify_binding"]) == ("2", "verify-30-1")
    expect(page.locator("#cb-30")).to_be_enabled(enabled=enabled_for_member)
    expect(page.locator("#cb-30")).to_be_checked(checked=enabled_for_member)
    stored = page.evaluate("Object.keys(readStoredSelection(document.getElementById('librenms-module-table')))")
    assert ("30" in stored) is enabled_for_member
    page.check("#cb-31")
    with page.expect_request(SUBMIT_URL) as submitted:
        page.click("#install-selected-btn")
    pairs = parse_qsl(submitted.value.post_data, keep_blank_values=True)

    expected = ["30", "31"] if enabled_for_member else ["31"]
    assert sorted(value for name, value in pairs if name == "select") == expected
    if enabled_for_member:
        assert ("device_selection_30", "2") in pairs
        assert ("selection_grant_30", "override-30-again") in pairs


PREVIEW_PATH = "/plugins/librenms_plugin/devices/1/module-mismatch-preview/"
PREVIEW_URL = f"{PREVIEW_PATH}?module_id=5&ent_index=30&inventory_binding=preview-30-2"


def _loader(marker="data-modal-loader"):
    """Cut a module tab loader out of librenms_sync_base.html, so the page carries its exact markup."""
    source = (TEMPLATE_DIR / "librenms_sync_base.html").read_text(encoding="utf-8")
    return re.search(rf"<span[^>]*{marker}.*?</span>", source, flags=re.S).group(0)


def test_a_verified_row_opens_its_replace_preview_in_the_modal(page):
    """The verify writes the actions cell with innerHTML, which htmx never binds, so the loader sends the request."""
    answer = _success(30, "2", "override-30")
    # The attributes module_action_control renders on the Replace button (tables/modules.py).
    answer["json"]["formatted_row"]["actions"] += (
        f'<button type="button" id="replace-30" hx-get="{PREVIEW_URL.replace("&", "&amp;")}"'
        ' hx-target="#htmx-modal-content" hx-swap="innerHTML" hx-sync="#htmx-modal-content:replace"'
        ' hx-disabled-elt="this">Replace</button>'
    )
    _answers(page, answer)
    page.route(
        f"**{PREVIEW_PATH}*",
        lambda route: route.fulfill(body='<p id="mismatch-preview">Preview</p>', content_type="text/html"),
    )
    shell = '<div id="htmx-modal"><div id="htmx-modal-content"></div></div>'
    _load(page, [_row(30)], extra=_loader() + shell)
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    expect(page.locator("#install-30")).to_be_enabled()

    with page.expect_request(f"**{PREVIEW_PATH}*", timeout=3000) as preview:
        page.click("#replace-30")

    assert (preview.value.method, preview.value.url) == ("GET", f"https://plugin.example.com{PREVIEW_URL}")
    expect(page.locator("#htmx-modal-content #mismatch-preview")).to_have_text("Preview")


INSTALL_PATH = "/plugins/librenms_plugin/devices/1/install-module/"


def _verified_install_form():
    """A verify answer whose actions cell holds the Install form with the attributes render_actions puts on it."""
    answer = _success(30, "2", "override-30")
    answer["json"]["formatted_row"]["actions"] = (
        f'<form method="post" action="{INSTALL_PATH}" hx-post="{INSTALL_PATH}" hx-target="#module-sync-content"'
        ' hx-swap="innerHTML" hx-sync="#module-sync-content:drop" hx-indicator="closest tr"'
        ' hx-disabled-elt="find button" style="display:inline">'
        '<input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">'
        '<input type="hidden" name="selected_device_id" value="2">'
        '<input type="hidden" name="inventory_binding" value="install-30-2">'
        '<button type="submit" data-action="install" id="install-30">Install</button></form>'
    )
    return answer


def test_a_verified_row_form_posts_through_htmx_and_swaps_the_tab(page):
    """The verify writes the actions cell with innerHTML, so the row form posts through the loader, not natively."""
    _answers(page, _verified_install_form())
    held = []
    page.route(f"**{INSTALL_PATH}", lambda route: held.append(route))
    _load(page, [_row(30)], extra=_loader("data-row-form-loader"))
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    expect(page.locator("#install-30")).to_be_enabled()

    with page.expect_request(f"**{INSTALL_PATH}", timeout=3000) as install:
        page.click("#install-30")

    request = install.value
    assert (request.method, request.headers.get("hx-request")) == ("POST", "true")
    pairs = parse_qsl(request.post_data)
    assert ("selected_device_id", "2") in pairs
    assert ("inventory_binding", "install-30-2") in pairs
    # The row shows its progress and locks its button, as hx-indicator and hx-disabled-elt do on a bound form.
    expect(page.locator('tr[data-ent-index="30"]')).to_have_class(re.compile(r"\bhtmx-request\b"))
    expect(page.locator("#install-30")).to_be_disabled()
    held[0].fulfill(body='<p id="installed">Installed</p>', content_type="text/html")

    expect(page.locator("#module-sync-content #installed")).to_have_text("Installed")
    assert page.url == PAGE_URL


def test_a_verified_row_form_posts_natively_when_htmx_did_not_load(page):
    """The table and the verify work without htmx, so an unbound loader leaves the form to its native post."""
    _answers(page, _verified_install_form())
    page.route(f"**{INSTALL_PATH}", lambda route: route.fulfill(body="<p>installed</p>", content_type="text/html"))
    _load(page, [_row(30)], mode="htmx-absent", extra=_loader("data-row-form-loader"))
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    expect(page.locator("#install-30")).to_be_enabled()

    with page.expect_request(f"**{INSTALL_PATH}", timeout=3000) as install:
        page.click("#install-30")

    request = install.value
    assert (request.method, request.headers.get("hx-request")) == ("POST", None)
    assert ("inventory_binding", "install-30-2") in parse_qsl(request.post_data)


def test_a_failed_row_post_keeps_the_buttons_locked_while_a_member_verify_runs(page):
    """The member changed during the post, so a failed post must not release the form of the previous member."""
    held_verify, _bodies = _answers(page, _verified_install_form(), None)
    held_post = []
    page.route(f"**{INSTALL_PATH}", lambda route: held_post.append(route))
    _load(page, [_row(30)], extra=_loader("data-row-form-loader"))
    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    page.wait_for_function("document.getElementById('device_selection_30').dataset.moduleSelectInitialized")
    with page.expect_request(f"**{INSTALL_PATH}", timeout=3000):
        page.click("#install-30")
    with page.expect_request(VERIFY_ROUTE):
        _change_member(page, 30, "1")

    with page.expect_response(f"**{INSTALL_PATH}"):
        held_post[0].fulfill(status=500, body="failed", content_type="text/html")

    expect(page.locator('tr[data-ent-index="30"]')).not_to_have_class(re.compile(r"\bhtmx-request\b"))
    expect(page.locator("#install-30")).to_be_disabled()
    # A refused verify puts the row back on member 2, whose form may post again.
    held_verify[0].fulfill(**REFUSED)
    expect(page.locator("#install-30")).to_be_enabled()


@pytest.mark.parametrize("content_type", ["text/html", "text/plain"])
@pytest.mark.parametrize("reverify", [False, True], ids=["member-change", "saved-selection"])
def test_a_non_json_member_refusal_keeps_the_response_body_out_of_the_toast(page, content_type, reverify):
    """An upstream error page must not become the user's refusal message."""
    refusal = {"status": 502, "content_type": content_type, "body": "private diagnostic content"}
    if reverify:
        _answers(page, _success(30, "2", "override-30"), refusal)
    else:
        _answers(page, refusal)
    _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")

    with page.expect_response(VERIFY_ROUTE):
        _change_member(page, 30, "2")
    if reverify:
        page.check("#cb-30")
        _load(page, [_row(40)], url=f"{PAGE_URL}?page=2")
        with page.expect_response(VERIFY_ROUTE):
            _load(page, [_row(30), _row(31)], url=f"{PAGE_URL}?page=1")

    expect(_toasts(page)).to_contain_text("502")
    expect(_toasts(page)).not_to_contain_text("private diagnostic content")
    expect(page.locator("#install-30")).to_be_enabled()
    assert page.locator("#device_selection_30").input_value() == "1"
