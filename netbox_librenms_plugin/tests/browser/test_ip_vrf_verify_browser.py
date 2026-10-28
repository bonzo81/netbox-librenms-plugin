"""Browser checks for the IP table's VRF select: only the latest verify decides the row."""

import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
PAGE_URL = "https://plugin.example.com/dcim/devices/1/librenms-sync/"
VERIFY_ROUTE = "**/plugins/librenms_plugin/verify-ipaddress/"
ALLOWED, IGNORED = "1", "2"


def _row(row_id, *, disabled=False, vrf=""):
    return f"""<tr data-interface="{row_id}" data-name="fe80::1/64">
          <td data-col="selection"><input type="checkbox" name="select" value="{row_id}"{" disabled" if disabled else ""}></td>
          <td data-col="vrf"><select id="vrf_select_{row_id}" class="form-select vrf-select" data-ip="fe80::1"
              data-prefix="64" data-row-id="{row_id}" name="vrf_{row_id}">
            <option value="">Global</option><option value="{ALLOWED}"{" selected" if vrf == ALLOWED else ""}>allowed</option><option value="{IGNORED}"{" selected" if vrf == IGNORED else ""}>ignored</option>
          </select></td>
          <td data-col="status">rendered</td>
        </tr>"""


def _load(page, rows):
    html = f"""<!doctype html><html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="vrf-csrf-token">
        <form id="sync-form" method="post"><input type="hidden" name="server_key" value="default">
          <table id="librenms-ipaddress-table"><tbody>{"".join(rows)}</tbody></table>
        </form></body></html>"""
    page.route(PAGE_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    page.goto(PAGE_URL)
    page.add_script_tag(path=str(SCRIPT_PATH))


def _verdict(status, selectable):
    return json.dumps({"status": "success", "formatted_row": {"status": status, "selectable": selectable}})


def _wait_for(page, condition):
    for _ in range(200):
        if condition():
            return
        page.wait_for_timeout(10)
    raise AssertionError("the page did not send the expected verify requests")


def test_a_late_response_for_an_earlier_vrf_does_not_repaint_the_row(page, fetches):
    _load(page, [_row("row-1")])
    fetches.install()
    held = []
    page.route(VERIFY_ROUTE, lambda route: held.append(route))
    page.evaluate("initializeVRFSelects()")
    page.wait_for_selector("select[data-vrf-select-initialized]", state="attached")
    select, checkbox = page.locator("#vrf_select_row-1"), page.locator('input[name="select"]')

    select.select_option(ALLOWED)
    _wait_for(page, lambda: len(held) == 1)
    select.select_option(IGNORED)
    _wait_for(page, lambda: len(held) == 2)
    to_allowed, to_ignored = held
    assert [route.request.post_data_json["vrf_id"] for route in held] == [ALLOWED, IGNORED]

    to_ignored.fulfill(status=200, content_type="application/json", body=_verdict("Ignored", False))
    page.wait_for_function("document.querySelector('td[data-col=status]').textContent === 'Ignored'")
    to_allowed.fulfill(status=200, content_type="application/json", body=_verdict("Create", True))
    fetches.wait_until_settled()

    assert page.locator('td[data-col="status"]').text_content() == "Ignored"
    assert checkbox.is_disabled()


def test_a_restored_selection_shows_the_stored_vrf_and_verifies_it_again(page):
    _load(page, [_row("row-1", disabled=True), _row("row-2")])
    verified = []

    def verify(route):
        verified.append(route.request.post_data_json["vrf_id"])
        route.fulfill(status=200, content_type="application/json", body=_verdict("Ignored", False))

    page.route(VERIFY_ROUTE, verify)
    page.evaluate(
        f"""() => writeStoredSelection(document.getElementById('librenms-ipaddress-table'), {{
            'row-1': {{inputs: {{}}, auto: ''}},
            'row-2': {{inputs: {{'vrf_row-2': '{IGNORED}'}}, auto: '', chosen: ['vrf_row-2']}},
        }})"""
    )

    page.evaluate("restoreTableSelection(document.getElementById('librenms-ipaddress-table'))")

    assert not page.locator('input[value="row-1"]').is_checked()
    assert page.locator("#vrf_select_row-2").input_value() == IGNORED
    _wait_for(page, lambda: verified == [IGNORED])
    # The rendered status belonged to the rendered VRF; the verify of the stored VRF decides the row.
    page.wait_for_function("document.querySelectorAll('td[data-col=status]')[1].textContent === 'Ignored'")
    assert page.locator('input[value="row-2"]').is_disabled()


def test_a_selected_row_that_leaves_the_page_posts_its_vrf(page):
    _load(page, [_row("row-1"), _row("row-2")])
    page.evaluate(
        f"""() => {{
            const table = document.getElementById('librenms-ipaddress-table');
            document.getElementById('vrf_select_row-1').value = '{ALLOWED}';
            table.querySelector('input[value="row-1"]').checked = true;
            persistTableSelection(table);
            // A filter or another page leaves the selected row out of the rendered table.
            table.querySelector('tr[data-interface="row-1"]').remove();
            injectOffPageSelections(document.getElementById('sync-form'));
        }}"""
    )

    posted = page.evaluate("[...new FormData(document.getElementById('sync-form'))]")

    assert ["select", "row-1"] in posted
    assert ["vrf_row-1", ALLOWED] in posted


@pytest.mark.parametrize("allowed_on_return", [True, False], ids=["allowed", "newly-ignored"])
def test_a_selected_allowed_vrf_is_verified_before_restoring_an_ignored_row(page, allowed_on_return):
    verified = []
    held = []

    def verify(route):
        vrf = route.request.post_data_json["vrf_id"]
        verified.append(vrf)
        if len(verified) == 1:
            route.fulfill(status=200, content_type="application/json", body=_verdict("", vrf == ALLOWED))
        else:
            held.append(route)

    page.route(VERIFY_ROUTE, verify)
    ignored_row = _row("row-1", disabled=True, vrf=IGNORED)
    _load(page, [ignored_row])
    page.evaluate("initializeScripts()")
    page.wait_for_selector("select[data-vrf-select-initialized]", state="attached")
    assert page.locator('input[value="row-1"]').is_disabled()

    page.select_option("#vrf_select_row-1", ALLOWED)
    page.wait_for_function("!document.querySelector('input[name=select]').disabled")
    page.check('input[value="row-1"]')

    _load(page, [_row("row-2")])
    page.evaluate("initializeScripts()")
    _load(page, [ignored_row])
    page.evaluate("initializeScripts()")

    assert page.locator("#vrf_select_row-1").input_value() == ALLOWED
    _wait_for(page, lambda: verified == [ALLOWED, ALLOWED])
    assert page.locator('input[value="row-1"]').is_disabled()
    held[0].fulfill(status=200, content_type="application/json", body=_verdict("", allowed_on_return))
    if allowed_on_return:
        page.wait_for_function("!document.querySelector('input[name=select]').disabled")
        assert page.locator('input[value="row-1"]').is_checked()
    else:
        page.wait_for_function("!document.querySelector('input[name=select]').checked")
        assert page.locator('input[value="row-1"]').is_disabled()
        assert page.evaluate("Object.keys(readStoredSelection(document.querySelector('table')))") == []
    posted = page.evaluate("[...new FormData(document.getElementById('sync-form'))]")
    assert (["select", "row-1"] in posted) == allowed_on_return
    assert ["vrf_row-1", ALLOWED] in posted
