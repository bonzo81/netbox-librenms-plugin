"""Browser checks for the server-side sync tab filters: the selection store, repaint marks and navigation."""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
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


def _selection_page(scope, port_ids):
    rows = "".join(
        f'<tr data-port-id="{port_id}"><td data-col="selection">'
        f'<input type="checkbox" name="select" value="{port_id}" id="cb-{port_id}"></td></tr>'
        for port_id in port_ids
    )
    return f"""<!doctype html><html><body>
        <input type="hidden" name="server_key" value="default">
        <form method="post"><table id="{TABLE}" data-selection-scope="{scope}"><tbody>{rows}</tbody></table></form>
        </body></html>"""


def test_the_selection_follows_the_user_across_filters_and_a_new_name_field_starts_an_empty_one(page):
    # The server renders one scope for every filter of the tab: only the name field and the ignored toggle change it.
    _serve(page, _selection_page("interface_name_field=ifName", ["101", "102"]))
    page.evaluate("initializeCheckboxes()")
    page.check("#cb-101")

    # Another filter shows other rows: the checked row stays selected, and the notice counts it.
    _serve(page, _selection_page("interface_name_field=ifName", ["201"]))
    page.evaluate("initializeCheckboxes()")
    assert page.evaluate(f"offPageSelectionKeys(document.getElementById('{TABLE}'))") == ["101"]
    expect(page.locator(f"#{TABLE}-offpage-selection")).to_be_visible()

    # A filter that shows the row again shows it checked.
    _serve(page, _selection_page("interface_name_field=ifName", ["101", "201"]))
    page.evaluate("initializeCheckboxes()")
    expect(page.locator("#cb-101")).to_be_checked()

    # Another name field names other rows, so it starts an empty selection.
    _serve(page, _selection_page("interface_name_field=ifDescr", ["101"]))
    page.evaluate("initializeCheckboxes()")
    expect(page.locator("#cb-101")).not_to_be_checked()
    assert page.locator(f"#{TABLE}-offpage-selection").count() == 0


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
    # A tab swap renders the radios again, enabled and unbound, and the initializer runs on them.
    page.evaluate(
        """() => {
            const selector = document.getElementById('interface-name-field-selector');
            selector.outerHTML = selector.outerHTML.replaceAll(/ (disabled|data-name-field-bound)="[^"]*"/g, '');
            updateInterfaceNameField();
        }"""
    )
    # The swapped radios show the page's name field again. A click on a disabled radio changes nothing.
    page.evaluate("document.getElementById('if-descr').click()")
    page.wait_for_timeout(300)

    # Only the first choice saves and navigates; a later save could otherwise finish first.
    assert len(held) == 1
    assert page.locator("#if-descr").is_disabled() and page.locator("#if-name").is_disabled()
    with page.expect_navigation() as navigation:
        held[0].fulfill(status=200, content_type="application/json", body="{}")
    assert parse_qs(urlsplit(navigation.value.url).query)["interface_name_field"] == ["ifDescr"]
    assert len(held) == 1


def test_a_back_forward_cache_restore_shows_the_name_field_of_the_restored_rows(page):
    held = []
    page.route(SAVE_PREF_URL, lambda route: held.append(route))
    _serve(page, _name_field_page(), f"{PAGE_URL}?tab=interfaces")
    page.evaluate("updateInterfaceNameField()")
    page.check("#if-descr")
    page.wait_for_timeout(300)
    assert len(held) == 1

    # The page as the back-forward cache froze it before its navigation. Headless Chromium restores
    # no page from that cache, so the test sends the event that the restore sends.
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")

    assert page.locator("#if-name").is_checked()
    assert page.locator("#if-name").is_enabled() and page.locator("#if-descr").is_enabled()


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
          <table id="{IP_TABLE}" data-mgmt-rows="[]" data-selection-scope="{signature}"><tbody>{rows}</tbody></table>
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
          <table id="librenms-cable-table" data-selection-scope="{signature}"><tbody>{rows}</tbody></table>
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


TEMPLATES_ROOT = Path(__file__).parents[2] / "templates"
# The rest of the page query but the server key, which the form sends in hx-vals.
OTHER_STATE = [("tab", "interfaces"), ("cables_q", "keep")]
FILTER_ENDPOINT = "https://plugin.example.com/plugins/librenms_plugin/device/1/sync-tab-filter/interfaces/"


def _filter_form_class():
    from django import forms

    class InterfaceFilterForm(forms.Form):
        """The shape of SyncFilterForm for the interfaces tab, which the browser suite cannot import."""

        q = forms.CharField(label="Search", required=False, max_length=256)
        sync_state = forms.ChoiceField(
            label="Sync state", required=False, choices=[("", "All sync states"), ("mismatched", "Mismatched")]
        )
        filter_name = forms.CharField(label="Name", required=False)

        def add_prefix(self, field_name):
            return f"{self.prefix}{field_name}"

        @property
        def column_fields(self):
            return [self["filter_name"]]

        @property
        def columns_have_input(self):
            return bool(self["filter_name"].value())

    return InterfaceFilterForm


def _filter_tab(query):
    """Render the interfaces tab head with the real filter templates for *query*, a dict of the filter values."""
    from django.conf import settings
    from django.template import Context, Engine, Library

    if not settings.configured:
        settings.configure(USE_I18N=False, USE_TZ=True)
    sync_query = Library()
    sync_query.simple_tag(lambda *args, **kwargs: OTHER_STATE, name="sync_tab_query_items")
    sync_query.simple_tag(lambda *args, **kwargs: "?tab=interfaces", name="sync_tab_url")
    # The browser suite has no URLconf and no NetBox, so the filter endpoint URL and ``meta`` are stubs.
    stubs = Library()
    stubs.simple_tag(lambda *args, **kwargs: FILTER_ENDPOINT, name="url")
    helpers = Library()
    helpers.filter("meta", lambda owner, name: "device")
    engine = Engine(dirs=[str(TEMPLATES_ROOT)])
    engine.template_builtins.append(stubs)
    engine.template_libraries["librenms_sync_query"] = sync_query
    engine.template_libraries["helpers"] = helpers

    form = _filter_form_class()(query, prefix="interfaces_")
    form.is_valid()
    filters = SimpleNamespace(
        form=form,
        active=any(value for key, value in query.items() if key.startswith("interfaces_")),
        in_use=any(query.values()),
        active_count=sum(bool(value) for key, value in query.items() if key.startswith("interfaces_")),
    )
    context = Context(
        {
            "filters": filters,
            "prefix": "interfaces_",
            "tab": "interfaces",
            "section_id": "interfaceFilterSection",
            "label": "Filter interfaces",
            "owner": SimpleNamespace(pk=1),
            "content_id": "interface-sync-content",
            "server_key": "primary",
        }
    )

    def render(name):
        return engine.get_template(f"netbox_librenms_plugin/inc/{name}.html").render(context)

    return f"""{render("_sync_filter_form")}
        <div class="sync-tab-primary-toolbar">{render("_sync_filter_controls")}</div>
        {render("_sync_filter_panel")}
        <div id="interface-sync-content-results"><div data-filter-search="{query.get("interfaces_q", "")}" data-filter-state="{query.get("interfaces_sync_state", "")}">
        <form method="post" id="sync-form" action="/sync/">
          <input type="hidden" name="csrfmiddlewaretoken" value="token">
          <div class="sync-tab-primary-toolbar"><button type="submit" id="sync">Sync</button>
            </div>
          <table id="{TABLE}" data-rendered-q="{query.get("interfaces_q", "")}"
            data-rendered-sync_state="{query.get("interfaces_sync_state", "")}"
            data-rendered-filter_name="{query.get("interfaces_filter_name", "")}"></table>
        </form></div></div>"""


def _filter_page(query):
    tab = f'<div id="librenms-sync-tabs"><div id="interface-sync-content">{_filter_tab(query)}</div></div>'
    return f"<!doctype html><html><body>{tab}</body></html>"


def _serve_filter_tab(page):
    """Serve the tab, and hold each filter request until the test answers it with the tab for its query."""
    held = []
    page.route(
        f"{PAGE_URL}**", lambda route: route.fulfill(status=200, content_type="text/html", body=_filter_page({}))
    )
    page.route(f"{FILTER_ENDPOINT}**", lambda route: held.append(route))
    page.goto(f"{PAGE_URL}?tab=interfaces&server_key=primary&cables_q=keep")
    _add_page_scripts(page)
    return held


def _next_request(page, held, count):
    for _ in range(100):
        if len(held) >= count:
            return held[count - 1]
        page.wait_for_timeout(50)
    raise AssertionError(f"no filter request {count}: {[route.request.url for route in held]}")


def _answer(route):
    """Answer like the filter endpoint: the tab content, and the page URL with the request query to push."""
    split = urlsplit(route.request.url)
    query = {key: values[0] for key, values in parse_qs(split.query).items()}
    route.fulfill(
        status=200,
        content_type="text/html",
        headers={"HX-Push-Url": f"{PAGE_URL}?{split.query}"},
        body=_filter_tab(query),
    )


def test_typing_in_the_search_filters_the_tab_without_a_submit(page):
    held = _serve_filter_tab(page)
    search = page.locator("#id_interfaces_q")
    search.press_sequentially("et")

    route = _next_request(page, held, 1)
    # The request goes to the tab filter endpoint, with the rest of the page query, no page number and no empty field.
    assert route.request.url == f"{FILTER_ENDPOINT}?tab=interfaces&cables_q=keep&interfaces_q=et&server_key=primary"
    assert route.request.headers["hx-request"] == "true"

    # A character typed while the request runs stays in the search and filters again.
    search.press_sequentially("h")
    _answer(route)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "et")
    expect(search).to_have_value("eth")
    expect(search).to_be_focused()
    _answer(_next_request(page, held, 2))
    assert parse_qs(urlsplit(held[1].request.url).query)["interfaces_q"] == ["eth"]
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    assert parse_qs(urlsplit(page.url).query)["interfaces_q"] == ["eth"]
    expect(search).to_have_value("eth")


def test_a_sync_state_choice_filters_the_tab_at_once(page):
    held = _serve_filter_tab(page)
    page.select_option("#id_interfaces_sync_state", "mismatched")

    route = _next_request(page, held, 1)
    assert parse_qs(urlsplit(route.request.url).query)["interfaces_sync_state"] == ["mismatched"]
    _answer(route)
    expect(page.locator("#id_interfaces_sync_state")).to_have_value("mismatched")
    assert parse_qs(urlsplit(page.url).query)["interfaces_sync_state"] == ["mismatched"]


@pytest.mark.parametrize("control", ["sync_state", "filter_name"])
def test_filter_changes_during_a_search_are_sent_once_after_the_response(page, control):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "eth")
    first = _next_request(page, held, 1)

    if control == "sync_state":
        page.focus("#id_interfaces_sync_state")
        page.select_option("#id_interfaces_sync_state", "mismatched")
        value = "mismatched"
    else:
        page.fill("#id_interfaces_filter_name", "xe")
        page.click("#interfaceFilterSection button[type=submit]")
        value = "xe"
    page.wait_for_timeout(700)
    assert len(held) == 1

    _answer(first)
    second = _next_request(page, held, 2)
    query = parse_qs(urlsplit(second.request.url).query)
    assert query[f"interfaces_{control}"] == [value]
    assert query["interfaces_q"] == ["eth"]
    _answer(second)

    expect(page.locator(f"#id_interfaces_{control}")).to_have_value(value)
    expect(page.locator("#id_interfaces_q")).to_have_value("eth")
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    expect(page.locator(f"#{TABLE}")).to_have_attribute(f"data-rendered-{control}", value)
    assert parse_qs(urlsplit(page.url).query)[f"interfaces_{control}"] == [value]
    page.wait_for_timeout(1000)
    assert len(held) == 2
    assert errors == []


def test_unapplied_column_edits_survive_a_search_response_without_submitting(page):
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "eth")
    first = _next_request(page, held, 1)
    page.fill("#id_interfaces_filter_name", "xe")

    _answer(first)

    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    expect(page.locator("#id_interfaces_filter_name")).to_have_value("xe")
    page.wait_for_timeout(1000)
    assert len(held) == 1
    page.click("#interfaceFilterSection button[type=submit]")
    second = _next_request(page, held, 2)
    assert parse_qs(urlsplit(second.request.url).query)["interfaces_filter_name"] == ["xe"]
    _answer(second)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-filter_name", "xe")


def test_a_delayed_search_keeps_all_latest_fields_after_focus_moves(page):
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "et")
    first = _next_request(page, held, 1)
    page.fill("#id_interfaces_q", "eth")
    page.fill("#id_interfaces_filter_name", "xe")

    # The response arrives before the search delay, with focus on a column field.
    _answer(first)
    second = _next_request(page, held, 2)
    query = parse_qs(urlsplit(second.request.url).query)
    assert query["interfaces_q"] == ["eth"]
    assert query["interfaces_filter_name"] == ["xe"]
    _answer(second)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-filter_name", "xe")
    expect(page.locator("#id_interfaces_filter_name")).to_be_focused()
    page.wait_for_timeout(1000)
    assert len(held) == 2


def test_repeated_changes_and_clearing_during_each_request_send_only_the_latest_form(page):
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "eth")
    first = _next_request(page, held, 1)
    page.select_option("#id_interfaces_sync_state", "mismatched")
    page.fill("#id_interfaces_filter_name", "xe")
    page.click("#interfaceFilterSection button[type=submit]")
    page.fill("#id_interfaces_filter_name", "ge")
    page.click("#interfaceFilterSection button[type=submit]")
    _answer(first)

    second = _next_request(page, held, 2)
    query = parse_qs(urlsplit(second.request.url).query)
    assert query["interfaces_sync_state"] == ["mismatched"]
    assert query["interfaces_filter_name"] == ["ge"]
    page.select_option("#id_interfaces_sync_state", "")
    page.fill("#id_interfaces_filter_name", "")
    page.click("#interfaceFilterSection button[type=submit]")
    _answer(second)

    third = _next_request(page, held, 3)
    query = parse_qs(urlsplit(third.request.url).query)
    assert "interfaces_sync_state" not in query
    assert "interfaces_filter_name" not in query
    assert query["interfaces_q"] == ["eth"]
    _answer(third)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-sync_state", "")
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-filter_name", "")
    page.wait_for_timeout(1000)
    assert len(held) == 3


def test_a_queued_choice_back_to_the_sent_value_does_not_apply_later_column_edits(page):
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "eth")
    first = _next_request(page, held, 1)
    page.select_option("#id_interfaces_sync_state", "mismatched")
    page.select_option("#id_interfaces_sync_state", "")
    _answer(first)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    page.wait_for_timeout(1000)
    assert len(held) == 1

    # A later cache fragment replacement must not turn that completed choice into an Apply.
    page.fill("#id_interfaces_filter_name", "xe")
    page.evaluate(
        "endpoint => { window.htmxTest.ajax('GET', endpoint + '?interfaces_q=eth', {target: '#interface-sync-content-results', select: '#interface-sync-content-results'}); }",
        FILTER_ENDPOINT,
    )
    _answer(_next_request(page, held, 2))
    expect(page.locator("#id_interfaces_filter_name")).to_have_value("xe")
    page.wait_for_timeout(1000)
    assert len(held) == 2


def test_the_column_filters_wait_for_apply(page):
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_filter_name", "xe")
    page.wait_for_timeout(700)
    assert held == []

    page.click("#interfaceFilterSection button[type=submit]")
    route = _next_request(page, held, 1)
    assert parse_qs(urlsplit(route.request.url).query)["interfaces_filter_name"] == ["xe"]


def test_the_sync_form_never_posts_the_filter_fields(page):
    _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "eth")
    page.fill("#id_interfaces_filter_name", "xe")

    posted = page.evaluate("[...new FormData(document.getElementById('sync-form')).keys()]")
    assert posted == ["csrfmiddlewaretoken"]


def test_text_typed_between_the_swap_and_the_settle_is_kept(page):
    held = _serve_filter_tab(page)
    search = page.locator("#id_interfaces_q")
    search.press_sequentially("et")
    route = _next_request(page, held, 1)
    # A key press after the swap and before htmx settles it, the moment the old restore used.
    page.evaluate(
        """() => document.addEventListener('htmx:afterSwap', () => setTimeout(() => {
            const search = document.getElementById('id_interfaces_q');
            search.value += 'x';
            search.dispatchEvent(new Event('input', { bubbles: true }));
        }, 0), { once: true })"""
    )

    _answer(route)

    expect(search).to_have_value("etx")
    assert parse_qs(urlsplit(_next_request(page, held, 2).request.url).query)["interfaces_q"] == ["etx"]


def test_a_search_waits_for_the_running_request_and_then_sends_only_the_latest_text(page):
    """The server cannot stop a request the browser drops, so the search never runs two at once."""
    held = _serve_filter_tab(page)
    search = page.locator("#id_interfaces_q")
    search.press_sequentially("et")
    first = _next_request(page, held, 1)

    # Slow typing: each pause is longer than the delay, and the first request still runs.
    for character in "-0/":
        search.press_sequentially(character)
        page.wait_for_timeout(700)
    assert len(held) == 1

    _answer(first)
    second = _next_request(page, held, 2)
    assert parse_qs(urlsplit(second.request.url).query)["interfaces_q"] == ["et-0/"]
    _answer(second)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "et-0/")
    page.wait_for_timeout(700)
    assert [parse_qs(urlsplit(route.request.url).query).get("interfaces_q") for route in held] == [["et"], ["et-0/"]]


def test_a_search_typed_before_the_response_keeps_its_full_delay(page):
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "et")
    first = _next_request(page, held, 1)
    page.fill("#id_interfaces_q", "eth")
    typed_at = page.evaluate("performance.now()")
    _answer(first)
    second = _next_request(page, held, 2)
    elapsed = page.evaluate("performance.now()") - typed_at
    assert elapsed >= 450, elapsed
    assert parse_qs(urlsplit(second.request.url).query)["interfaces_q"] == ["eth"]
    _answer(second)


def test_a_cancelled_choice_does_not_submit_a_later_unapplied_column(page):
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "eth")
    first = _next_request(page, held, 1)
    page.select_option("#id_interfaces_sync_state", "mismatched")
    page.select_option("#id_interfaces_sync_state", "")
    page.fill("#id_interfaces_filter_name", "xe")
    _answer(first)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    page.wait_for_timeout(1000)
    assert all("interfaces_filter_name" not in parse_qs(urlsplit(route.request.url).query) for route in held)
    expect(page.locator("#id_interfaces_filter_name")).to_have_value("xe")


def test_cache_expiry_discards_a_delayed_search_before_a_new_form_appears(page):
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "eth")
    # Use the same removal path as cache expiry, during the search delay.
    page.evaluate("""() => {
        document.body.insertAdjacentHTML('beforeend', '<div id="librenms-sync-cache-state"></div>');
        document.getElementById('librenms-sync-cache-state')._controller = {
            contract: {interfaces: {content_id: 'interface-sync-content'}}
        };
        clearSyncTabContent('interfaces');
    }""")
    page.wait_for_timeout(700)
    assert held == []
    page.evaluate(
        "endpoint => { htmxTest.ajax('GET', endpoint, {target: '#interface-sync-content'}); }", FILTER_ENDPOINT
    )
    _answer(_next_request(page, held, 1))
    expect(page.locator("#id_interfaces_q")).to_have_value("")
    page.fill("#id_interfaces_filter_name", "xe")
    page.evaluate(
        "endpoint => { htmxTest.ajax('GET', endpoint, {target: '#interface-sync-content'}); }", FILTER_ENDPOINT
    )
    _answer(_next_request(page, held, 2))
    page.wait_for_timeout(1000)
    assert len(held) == 2


def test_a_filter_response_keeps_the_same_form_and_controls(page):
    held = _serve_filter_tab(page)
    page.evaluate(
        "window.originalFilterForm = document.querySelector('.sync-filter-form'); window.originalSearch = document.querySelector('.sync-filter-search')"
    )
    page.fill("#id_interfaces_q", "eth")
    _answer(_next_request(page, held, 1))
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    assert page.evaluate(
        "originalFilterForm === document.querySelector('.sync-filter-form') && originalSearch === document.querySelector('.sync-filter-search')"
    )


def test_filter_status_and_validation_update_without_replacing_controls(page):
    held = _serve_filter_tab(page)
    search = page.locator("#id_interfaces_q")
    page.fill("#id_interfaces_q", "x" * 257)
    _answer(_next_request(page, held, 1))
    expect(page.locator("#interfaces_filter-validation [role=alert]")).to_be_visible()
    expect(search).to_have_attribute("aria-invalid", "true")
    expect(page.locator(".sync-filter-clear")).to_have_count(1)
    page.fill("#id_interfaces_q", "eth")
    _answer(_next_request(page, held, 2))
    expect(page.locator("#interfaces_filter-validation [role=alert]")).to_have_count(0)
    expect(search).not_to_have_attribute("aria-invalid", "true")
    expect(page.locator("#interfaces_filter-count .badge")).to_have_text("1")
    page.fill("#id_interfaces_q", "")
    _answer(_next_request(page, held, 3))
    expect(page.locator(".sync-filter-clear")).to_have_count(0)
    expect(page.locator("#interfaces_filter-count .badge")).to_have_count(0)


def test_an_applied_column_survives_a_cancelled_queued_state_change(page):
    held = _serve_filter_tab(page)
    page.fill("#id_interfaces_q", "eth")
    first = _next_request(page, held, 1)
    page.fill("#id_interfaces_filter_name", "xe")
    page.click("#interfaceFilterSection button[type=submit]")
    page.select_option("#id_interfaces_sync_state", "mismatched")
    page.select_option("#id_interfaces_sync_state", "")

    _answer(first)

    second = _next_request(page, held, 2)
    assert parse_qs(urlsplit(second.request.url).query)["interfaces_filter_name"] == ["xe"]
    _answer(second)
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-filter_name", "xe")
    page.wait_for_timeout(700)
    assert len(held) == 2


def test_history_restores_the_filter_controls_with_the_results(page):
    held = _serve_filter_tab(page)
    for count, value in enumerate(("eth", "xe"), start=1):
        page.fill("#id_interfaces_q", value)
        _answer(_next_request(page, held, count))
        expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", value)

    page.go_back()

    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "eth")
    expect(page.locator("#id_interfaces_q")).to_have_value("eth")
    assert parse_qs(urlsplit(page.url).query)["interfaces_q"] == ["eth"]
    page.go_forward()
    expect(page.locator(f"#{TABLE}")).to_have_attribute("data-rendered-q", "xe")
    expect(page.locator("#id_interfaces_q")).to_have_value("xe")
