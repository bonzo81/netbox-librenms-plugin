"""Browser checks for the server-side sync tab filters: the selection store, repaint marks and navigation."""

import json
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

from test_sync_cache_browser import _add_page_scripts, _page_html, _state

PAGE_URL = "https://plugin.example.com/dcim/devices/1/librenms-sync/"
VERIFY_ROUTE = "**/plugins/librenms_plugin/verify-interface/*"
SAVE_PREF_URL = "https://plugin.example.com/plugins/librenms_plugin/save-user-pref/"
TABLE = "librenms-interface-table"


def _serve(page, html, url=PAGE_URL):
    """Serve *html* at *url* from a real origin, so sessionStorage and the URL behave as on NetBox."""
    page.route(f"{PAGE_URL}**", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    page.goto(url)
    _add_page_scripts(page)


def _selection_page(signature, port_ids):
    rows = "".join(
        f'<tr data-port-id="{port_id}"><td data-col="selection">'
        f'<input type="checkbox" name="select" value="{port_id}" id="cb-{port_id}"></td></tr>'
        for port_id in port_ids
    )
    return f"""<!doctype html><html><body>
        <input type="hidden" name="server_key" value="default">
        <form method="post"><table id="{TABLE}" data-filter-signature="{signature}"><tbody>{rows}</tbody></table></form>
        </body></html>"""


def test_a_new_filter_starts_an_empty_selection_and_the_old_filter_keeps_its_own(page):
    _serve(page, _selection_page("interfaces_q=eth", ["101", "102"]))
    page.evaluate("initializeCheckboxes()")
    page.check("#cb-101")

    # Another filter renders other rows; the row checked under the first filter is not carried over.
    _serve(page, _selection_page("interfaces_q=po", ["201"]))
    page.evaluate("initializeCheckboxes()")
    assert page.evaluate(f"offPageSelectionKeys(document.getElementById('{TABLE}'))") == []
    assert page.locator(f"#{TABLE}-offpage-selection").count() == 0

    # The first filter on another page still holds its row, and the notice counts it.
    _serve(page, _selection_page("interfaces_q=eth", ["103"]))
    page.evaluate("initializeCheckboxes()")
    assert page.evaluate(f"offPageSelectionKeys(document.getElementById('{TABLE}'))") == ["101"]
    expect(page.locator(f"#{TABLE}-offpage-selection")).to_be_visible()


CELLS = ("name", "type", "speed", "mac_address", "mtu", "enabled", "description", "vlans", "librenms_id")


def _member_row_page(filter_active):
    cells = "".join(f'<td data-col="{column}">old</td>' for column in CELLS)
    return f"""<!doctype html><html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="token">
        <input type="hidden" name="server_key" value="default">
        <table id="{TABLE}" data-filter-active="{"true" if filter_active else "false"}"><tbody>
          <tr data-port-id="101" data-interface="eth0">
            <td data-col="selection"><input type="checkbox" name="select" value="101"></td>
            <td data-col="device_selection"><select class="vc-member-select" data-interface="eth0">
              <option value="1" selected>m1</option><option value="2">m2</option></select></td>
            {cells}<td data-col="parent"></td><td data-col="actions"></td>
          </tr>
        </tbody></table></body></html>"""


def _formatted_row():
    row = {column: f"<span>new {column}</span>" for column in CELLS}
    row.update(selection='<input type="checkbox" name="select" value="101">', parent="", actions="", rule_state="")
    return json.dumps({"status": "success", "formatted_row": row})


def _verify_member(page):
    page.evaluate("handleInterfaceChange(document.querySelector('.vc-member-select'), '2')")


def test_a_member_repaint_of_a_filtered_row_marks_it_stale(page):
    requests = []

    def answer(route):
        requests.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=_formatted_row())

    page.route(VERIFY_ROUTE, answer)
    _serve(page, _member_row_page(filter_active=True), f"{PAGE_URL}?tab=interfaces&interfaces_q=eth")

    _verify_member(page)

    expect(page.locator('tr[data-port-id="101"][data-filter-stale="true"]')).to_have_count(1)
    expect(page.locator('td[data-col="name"] .filter-stale-badge')).to_have_count(1)
    notice = page.locator(f'#{TABLE}-filter-stale-notice[role="status"]')
    expect(notice).to_have_count(1)
    # The verify request carries the page query, so a repainted Rebind posts back to the same view.
    assert parse_qs(urlsplit(requests[0]).query) == {"tab": ["interfaces"], "interfaces_q": ["eth"]}

    # A second repaint keeps one badge and one notice.
    _verify_member(page)
    page.wait_for_function("document.querySelectorAll('.filter-stale-badge').length === 1")
    expect(notice).to_have_count(1)


def test_a_member_repaint_without_a_filter_marks_nothing(page):
    page.route(
        VERIFY_ROUTE, lambda route: route.fulfill(status=200, content_type="application/json", body=_formatted_row())
    )
    _serve(page, _member_row_page(filter_active=False))

    _verify_member(page)

    expect(page.locator('td[data-col="name"]')).to_have_text("new name")
    assert page.locator("[data-filter-stale]").count() == 0
    assert page.locator(f"#{TABLE}-filter-stale-notice").count() == 0


def _vlan_modal_page():
    vlans = json.dumps([{"vid": 100, "type": "U", "group_id": "", "group_name": "Global", "css": "text-danger"}])
    groups = json.dumps([{"id": "", "name": "Global"}, {"id": "7", "name": "campus"}])
    return f"""<!doctype html><html><body>
        <table id="{TABLE}" data-filter-active="true"><tbody>
          <tr data-port-id="101"><td data-col="name">eth0</td><td data-col="vlans">
            <span title="VLAN 100(U)"><span class="text-danger">100(U)</span></span><button type="button"
              class="vlan-edit-btn" data-row-key="101" data-vlans='{vlans}' data-vlan-groups='{groups}'>Edit</button>
          </td></tr>
          <tr data-port-id="102"><td data-col="name">eth1</td></tr>
        </tbody></table>
        <div class="modal" id="vlanDetailModal" data-current-row-key="101">
          <table><tbody id="vlanDetailTableBody"><tr><td><select class="vlan-modal-group-select" data-vid="100">
            <option value="">Global</option><option value="7" selected>campus</option></select></td></tr></tbody></table>
          <input type="checkbox" id="applyVlanGroupToAll">
          <button type="button" id="saveVlanGroups">Save</button>
        </div></body></html>"""


def test_a_vlan_group_change_on_a_filtered_row_marks_it_stale(page):
    _serve(page, _vlan_modal_page())
    page.evaluate("initializeVlanModalSave()")

    page.click("#saveVlanGroups")

    expect(page.locator('tr[data-port-id="101"][data-filter-stale="true"] .filter-stale-badge')).to_have_count(1)
    assert page.locator('tr[data-port-id="102"][data-filter-stale]').count() == 0
    expect(page.locator(f'#{TABLE}-filter-stale-notice[role="status"]')).to_have_count(1)


def _name_field_page():
    return f"""<!doctype html><html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="token">
        <div id="interface-name-field-selector" data-platform-id="" data-save-pref-url="{SAVE_PREF_URL}">
          <input type="radio" class="interface-name-field" name="interface_name_field" value="ifDescr" id="if-descr">
          <input type="radio" class="interface-name-field" name="interface_name_field" value="ifName" id="if-name" checked>
        </div></body></html>"""


def test_a_name_field_change_saves_once_then_loads_the_page_with_the_filters_and_no_page(page):
    held = []
    page.route(SAVE_PREF_URL, lambda route: held.append(route))
    query = "tab=interfaces&interfaces_q=a%26b+c&interfaces_page=3&cables_page=2&interfaces_per_page=25"
    _serve(page, _name_field_page(), f"{PAGE_URL}?{query}")
    # The initializer runs again after every htmx swap; the radio must still save once.
    for _ in range(3):
        page.evaluate("updateInterfaceNameField()")

    page.check("#if-descr")
    page.wait_for_timeout(300)
    assert len(held) == 1
    assert json.loads(held[0].request.post_data)["value"] == "ifDescr"
    # The page waits for the save, so the stored preference and the next render agree.
    assert page.url == f"{PAGE_URL}?{query}"

    with page.expect_navigation() as navigation:
        held[0].fulfill(status=200, content_type="application/json", body="{}")

    target = parse_qs(urlsplit(navigation.value.url).query)
    assert target == {
        "tab": ["interfaces"],
        "interfaces_q": ["a&b c"],
        "interfaces_per_page": ["25"],
        "interface_name_field": ["ifDescr"],
    }
    assert len(held) == 1


def test_a_cache_restore_loads_the_fragment_for_the_page_query(page):
    initial = {"interfaces": _state("before"), "ipaddresses": _state("before-ip")}
    current = {"interfaces": _state("after"), "ipaddresses": initial["ipaddresses"]}
    fragments = []

    def fragment(route):
        fragments.append(route.request.url)
        route.fulfill(body='<div id="restored">Restored</div>')

    page.set_content(_page_html(initial))
    page.evaluate("history.replaceState(null, '', '?tab=interfaces&interfaces_q=a%26b+c&interfaces_page=2')")
    page.route("https://plugin.example.com/status?*", lambda route: route.fulfill(json={"tabs": current}))
    page.route("https://plugin.example.com/fragment/interfaces?*", fragment)
    _add_page_scripts(page)

    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.locator("#restored").wait_for()

    assert parse_qs(urlsplit(fragments[0]).query) == {
        "tab": ["interfaces"],
        "interfaces_q": ["a&b c"],
        "interfaces_page": ["2"],
        "server_key": ["primary"],
    }


def _ip_row_page(filter_active):
    return f"""<!doctype html><html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="token">
        <form method="post"><input type="hidden" name="server_key" value="default">
          <table id="librenms-ipaddress-table" data-filter-active="{"true" if filter_active else "false"}"><tbody>
            <tr data-interface="198.18.0.1/24">
              <td data-col="selection"><input type="checkbox" name="select" value="198.18.0.1/24"></td>
              <td data-col="address"><a href="/ipam/ip-addresses/1/">198.18.0.1</a></td>
              <td data-col="vrf"><select class="form-select vrf-select" data-ip="198.18.0.1" data-prefix="24"
                  data-row-id="198.18.0.1/24" name="vrf_198.18.0.1/24">
                <option value="" selected>Global</option><option value="1">blue</option></select></td>
              <td data-col="status">Synced</td>
            </tr>
            <tr data-interface="198.18.0.2/24"><td data-col="address">198.18.0.2</td></tr>
          </tbody></table>
        </form></body></html>"""


def _verify_vrf(page):
    page.route(
        "**/plugins/librenms_plugin/verify-ipaddress/",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"status": "success", "formatted_row": {"status": "<span>Update</span>", "selectable": True}}
            ),
        ),
    )
    page.evaluate("handleVRFChange(document.querySelector('.vrf-select'), '1')")
    page.locator('td[data-col="status"]', has_text="Update").wait_for()


def test_a_vrf_verification_of_a_filtered_ip_row_marks_it_stale(page):
    _serve(page, _ip_row_page(filter_active=True))

    _verify_vrf(page)

    expect(page.locator('tr[data-interface="198.18.0.1/24"][data-filter-stale="true"]')).to_have_count(1)
    expect(page.locator('td[data-col="address"] .filter-stale-badge')).to_have_count(1)
    assert page.locator('tr[data-interface="198.18.0.2/24"][data-filter-stale]').count() == 0
    expect(page.locator('#librenms-ipaddress-table-filter-stale-notice[role="status"]')).to_have_count(1)


def test_a_vrf_verification_without_a_filter_marks_nothing(page):
    _serve(page, _ip_row_page(filter_active=False))

    _verify_vrf(page)

    assert page.locator("[data-filter-stale]").count() == 0
    assert page.locator("#librenms-ipaddress-table-filter-stale-notice").count() == 0
