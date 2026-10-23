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


def test_a_second_name_field_choice_waits_for_the_first_save(page):
    held = []
    page.route(SAVE_PREF_URL, lambda route: held.append(route))
    _serve(page, _name_field_page(), f"{PAGE_URL}?tab=interfaces")
    page.evaluate("updateInterfaceNameField()")

    page.check("#if-descr")
    # A click on a disabled radio changes nothing, so this is how a user tries the other choice.
    page.evaluate("document.getElementById('if-name').click()")
    page.wait_for_timeout(300)

    # Only the first choice saves and navigates; a later save could otherwise finish first.
    assert len(held) == 1
    assert page.locator("#if-name").is_disabled()
    with page.expect_navigation() as navigation:
        held[0].fulfill(status=200, content_type="application/json", body="{}")
    assert parse_qs(urlsplit(navigation.value.url).query)["interface_name_field"] == ["ifDescr"]
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


def _vlan_row_page(filter_active):
    return f"""<!doctype html><html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="token">
        <form><table id="librenms-vlan-table" data-filter-active="{"true" if filter_active else "false"}"><tbody>
          <tr data-vlan-id="501">
            <td data-col="vlan_id"><span class="text-danger">501</span></td>
            <td data-col="name"><span class="text-danger">Users</span></td>
            <td data-col="vlan_group_selection"><select class="vlan-sync-group-select" name="vlan_group_501"
                data-vlan-id="501" data-vlan-name="Users">
              <option value="" selected>Global</option><option value="1">Group A</option></select></td>
            <td data-col="status">Not in NetBox</td>
          </tr>
          <tr data-vlan-id="502"><td data-col="name"><span>Voice</span></td></tr>
        </tbody></table></form></body></html>"""


def _verify_vlan_group(page):
    page.route(
        "**/plugins/librenms_plugin/verify-vlan-sync-group/",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "status": "success",
                    "css_class": "text-success",
                    "exists_in_netbox": True,
                    "name_matches": True,
                    "status_html": '<span id="group-a-status">Synced</span>',
                }
            ),
        ),
    )
    page.evaluate("initializeVlanSyncGroupSelects()")
    page.locator(".vlan-sync-group-select").select_option("1")
    page.locator("#group-a-status").wait_for()


def test_a_vlan_group_verification_of_a_filtered_vlan_row_marks_it_stale(page):
    _serve(page, _vlan_row_page(filter_active=True))

    _verify_vlan_group(page)

    expect(page.locator('tr[data-vlan-id="501"][data-filter-stale="true"]')).to_have_count(1)
    # The badge follows the name text, so the repaint of the name colour still finds the name.
    expect(page.locator('td[data-col="name"] .filter-stale-badge')).to_have_count(1)
    expect(page.locator('td[data-col="name"] span').first).to_have_class("text-success")
    assert page.locator('tr[data-vlan-id="502"][data-filter-stale]').count() == 0
    expect(page.locator('#librenms-vlan-table-filter-stale-notice[role="status"]')).to_have_count(1)


def test_a_vlan_group_verification_without_a_filter_marks_nothing(page):
    _serve(page, _vlan_row_page(filter_active=False))

    _verify_vlan_group(page)

    assert page.locator("[data-filter-stale]").count() == 0
    assert page.locator("#librenms-vlan-table-filter-stale-notice").count() == 0


def test_apply_to_all_on_a_filtered_table_saves_the_groups_for_the_whole_device(page):
    saved = []

    def save(route):
        saved.append(json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/plugins/librenms_plugin/save-vlan-group-overrides/", save)
    html = _vlan_modal_page().replace(
        'id="vlanDetailModal" data-current-row-key="101"',
        'id="vlanDetailModal" data-current-row-key="101" data-current-device-id="1"',
    )
    _serve(page, html.replace("<body>", '<body><input type="hidden" name="csrfmiddlewaretoken" value="token">'))
    page.evaluate("initializeVlanModalSave()")

    page.check("#applyVlanGroupToAll")
    page.click("#saveVlanGroups")

    # The option covers every page and filter: the server stores the groups for the device, not for the shown rows.
    page.wait_for_function("document.querySelectorAll('.filter-stale-badge').length === 1")
    assert saved == [{"device_id": "1", "vid_group_map": {"100": "7"}, "server_key": None}]
    expect(page.locator('tr[data-port-id="101"][data-filter-stale="true"]')).to_have_count(1)


IP_TABLE = "librenms-ipaddress-table"
IP_SUBMIT_PATH = "https://plugin.example.com/plugins/librenms_plugin/sync-ip/"


def _ip_selection_page(signature, row_ids):
    rows = "".join(
        f'<tr data-interface="{row_id}"><td data-col="selection">'
        f'<input type="checkbox" name="select" value="{row_id}" id="cb-{row_id.replace(".", "-").replace("/", "-")}"></td></tr>'
        for row_id in row_ids
    )
    return f"""<!doctype html><html><body>
        <input type="hidden" name="server_key" value="default">
        <form method="post" action="{IP_SUBMIT_PATH}">
          <table id="{IP_TABLE}" data-mgmt-rows="[]" data-filter-signature="{signature}"><tbody>{rows}</tbody></table>
          <button type="submit" id="sync">Sync</button>
        </form></body></html>"""


def _submitted(page):
    posted = []
    page.route(
        IP_SUBMIT_PATH,
        lambda route: (posted.append(route.request.post_data), route.fulfill(status=200, body="ok")),
    )
    page.click("#sync")
    page.wait_for_function("document.body.textContent.trim() === 'ok'")
    return sorted(parse_qs(posted[0]).get("select", []))


def test_a_name_field_change_drops_the_ip_selection_from_the_next_bulk_sync(page):
    # The server signs the IP table with its filter and name field (views/base/ip_addresses_view.py).
    _serve(page, _ip_selection_page("interface_name_field=ifName&ipaddresses_q=eth", ["198.18.0.1/24"]))
    page.evaluate("initializeCheckboxes()")
    page.check("#cb-198-18-0-1-24")

    # Under ifDescr the row's interface no longer matches the filter, so another row shows.
    _serve(page, _ip_selection_page("interface_name_field=ifDescr&ipaddresses_q=eth", ["198.18.0.2/24"]))
    page.evaluate("initializeCheckboxes()")
    page.check("#cb-198-18-0-2-24")

    assert _submitted(page) == ["198.18.0.2/24"]


CABLE_TABLE = "librenms-cable-table-vc"


def _cable_row_page(filter_active):
    return f"""<!doctype html><html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="token">
        <input type="hidden" name="server_key" value="default">
        <div data-cable-verify-url="https://plugin.example.com/verify" data-cable-origin-device-id="7">
          <table id="{CABLE_TABLE}" data-filter-active="{"true" if filter_active else "false"}"><tbody>
            <tr data-interface="row-1">
              <td data-col="selection"><input type="checkbox" name="select" value="row-1"></td>
              <td data-col="device_selection"><select id="member-select" data-interface="row-1">
                <option value="7" selected>m1</option><option value="8">m2</option></select></td>
              <td data-col="local_port">Ethernet1/1</td>
              <td data-col="remote_port">xe-1</td>
              <td data-col="remote_device">peer</td>
              <td data-col="cable_status">No Cable</td>
              <td data-col="actions"></td>
            </tr>
            <tr data-interface="row-2"><td data-col="local_port">Ethernet1/2</td></tr>
          </tbody></table>
        </div></body></html>"""


def _verify_cable_member(page):
    formatted_row = {
        "local_port": "<a href='/dcim/interfaces/9/'>Ethernet2/1</a>",
        "remote_port": "xe-1",
        "remote_device": "peer",
        "cable_status": "Cable Mismatch",
        "actions": "",
        "can_create_cable": True,
    }
    page.route(
        "https://plugin.example.com/verify",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "success", "formatted_row": formatted_row}),
        ),
    )
    page.evaluate("handleCableChange(document.getElementById('member-select'), '8')")
    page.locator('td[data-col="cable_status"]', has_text="Cable Mismatch").wait_for()


def test_a_cable_member_verification_of_a_filtered_row_marks_it_stale(page):
    _serve(page, _cable_row_page(filter_active=True), f"{PAGE_URL}?tab=cables&cables_q=Ethernet1")

    _verify_cable_member(page)

    expect(page.locator('tr[data-interface="row-1"][data-filter-stale="true"]')).to_have_count(1)
    # The repaint replaced the local port cell, and the badge follows in the new cell.
    expect(page.locator('td[data-col="local_port"] a')).to_have_text("Ethernet2/1")
    expect(page.locator('td[data-col="local_port"] .filter-stale-badge')).to_have_count(1)
    assert page.locator('tr[data-interface="row-2"][data-filter-stale]').count() == 0
    expect(page.locator(f'#{CABLE_TABLE}-filter-stale-notice[role="status"]')).to_have_count(1)

    # A second repaint draws the cell again and keeps one badge and one notice.
    _verify_cable_member(page)
    page.wait_for_function("document.querySelectorAll('.filter-stale-badge').length === 1")
    expect(page.locator(f"#{CABLE_TABLE}-filter-stale-notice")).to_have_count(1)


def test_a_cable_member_verification_without_a_filter_marks_nothing(page):
    _serve(page, _cable_row_page(filter_active=False))

    _verify_cable_member(page)

    assert page.locator("[data-filter-stale]").count() == 0
    assert page.locator(f"#{CABLE_TABLE}-filter-stale-notice").count() == 0


def test_the_cable_picker_request_carries_the_page_query(page):
    html = """<!doctype html><html><body>
        <input type="hidden" name="csrfmiddlewaretoken" value="token">
        <div id="cables">
          <span class="d-none" data-cable-picker-loader hx-get="/plugins/librenms_plugin/picker/1/"
              hx-trigger="librenms:open-cable-picker" hx-target="#htmx-modal-content" hx-swap="innerHTML"></span>
          <button id="pick" type="button"
              data-cable-picker-url="/plugins/librenms_plugin/picker/1/?row_id=row-1&amp;server_key=default">Pick</button>
        </div>
        <div id="htmx-modal"><div id="htmx-modal-content"></div></div></body></html>"""
    query = "tab=cables&server_key=default&cables_q=a%26b&cables_q=again&cables_page=2&interfaces_q=eth"
    _serve(page, html, f"{PAGE_URL}?{query}")
    page.route(
        "https://plugin.example.com/plugins/librenms_plugin/picker/1/?*",
        lambda route: route.fulfill(status=200, content_type="text/html", body='<div class="modal-header"></div>'),
    )
    page.evaluate("document.dispatchEvent(new Event('DOMContentLoaded'))")

    with page.expect_request("**/plugins/librenms_plugin/picker/1/?*") as request_info:
        page.locator("#pick").click()

    # The row's own keys stay once; every page key is added, a repeated one with each value.
    assert parse_qs(urlsplit(request_info.value.url).query) == {
        "row_id": ["row-1"],
        "server_key": ["default"],
        "tab": ["cables"],
        "cables_q": ["a&b", "again"],
        "cables_page": ["2"],
        "interfaces_q": ["eth"],
    }


CABLE_SUBMIT_PATH = "https://plugin.example.com/plugins/librenms_plugin/device/1/cables/sync/"


def _cable_selection_page(signature, row_ids):
    rows = "".join(
        f'<tr data-interface="{row_id}"><td data-col="selection">'
        f'<input type="checkbox" name="select" value="{row_id}" id="cb-{row_id}"></td></tr>'
        for row_id in row_ids
    )
    return f"""<!doctype html><html><body>
        <input type="hidden" name="server_key" value="default">
        <form method="post" action="{CABLE_SUBMIT_PATH}">
          <table id="librenms-cable-table" data-filter-signature="{signature}"><tbody>{rows}</tbody></table>
          <button type="submit" id="sync">Sync</button>
        </form></body></html>"""


def test_a_name_field_change_drops_the_cable_selection_from_the_next_bulk_sync(page):
    # The server signs every tab with the name field (sync_filters.filter_sync_table).
    _serve(page, _cable_selection_page("cables_filter_local_port=uplink&interface_name_field=ifName", ["7301"]))
    page.evaluate("initializeCheckboxes()")
    page.check("#cb-7301")

    # Under ifDescr the row's local port no longer matches the filter, so another row shows.
    _serve(page, _cable_selection_page("cables_filter_local_port=uplink&interface_name_field=ifDescr", ["7302"]))
    page.evaluate("initializeCheckboxes()")
    page.check("#cb-7302")

    posted = []
    page.route(
        CABLE_SUBMIT_PATH,
        lambda route: (posted.append(route.request.post_data), route.fulfill(status=200, body="ok")),
    )
    page.click("#sync")
    page.wait_for_function("document.body.textContent.trim() === 'ok'")
    assert sorted(parse_qs(posted[0]).get("select", [])) == ["7302"]


def test_apply_clears_the_stale_marks_and_a_later_repaint_marks_again(page):
    page.route(
        VERIFY_ROUTE, lambda route: route.fulfill(status=200, content_type="application/json", body=_formatted_row())
    )
    apply_form = '<form method="get" class="sync-filter-form"><button type="submit" id="apply">Apply</button></form>'
    html = _member_row_page(filter_active=True).replace("<body>", f"<body>{apply_form}", 1)
    _serve(page, html, f"{PAGE_URL}?tab=interfaces&interfaces_q=eth")
    _verify_member(page)
    expect(page.locator(".filter-stale-badge")).to_have_count(1)
    expect(page.locator(f"#{TABLE}-filter-stale-notice")).to_have_count(1)

    # Apply loads the page again: the server chose the rows afresh, so no mark is left.
    with page.expect_navigation():
        page.click("#apply")
    _add_page_scripts(page)
    assert page.locator("[data-filter-stale]").count() == 0
    assert page.locator(".filter-stale-badge").count() == 0
    assert page.locator(f"#{TABLE}-filter-stale-notice").count() == 0

    _verify_member(page)
    expect(page.locator('tr[data-port-id="101"][data-filter-stale="true"]')).to_have_count(1)
    expect(page.locator(".filter-stale-badge")).to_have_count(1)
    expect(page.locator(f"#{TABLE}-filter-stale-notice")).to_have_count(1)
