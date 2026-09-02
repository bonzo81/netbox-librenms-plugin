"""Browser-level checks for the sync tabs: the cache state machine and table selection."""

import json
from html import escape
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
STYLE_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "css" / "librenms_sync.css"


# ===========================================================================
# Interface selection: the requirement cascade and cross-page state
# ===========================================================================
SELECTION_PAGE_URL = "https://plugin.example.com/device/1/sync"

# Verbatim relationship shape of a live Junos MX304 (LibreNMS device 20): et-0/0/6.0 is a unit of
# et-0/0/6, et-0/0/6 is a member of ae2, and ae2.0 is a unit of ae2.
JUNOS_ROWS = [
    {"port_id": "4303", "name": "ae2"},
    {"port_id": "4304", "name": "ae2.0", "parent": "4303"},
    {"port_id": "4301", "name": "et-0/0/6", "lag": "4303"},
    {"port_id": "4302", "name": "et-0/0/6.0", "parent": "4301", "parent_name": "et-0/0/6"},
]


def _selection_row_markup(row):
    esc = escape
    attrs = [f'data-port-id="{esc(row["port_id"])}"']
    if row.get("parent"):
        attrs.append(f'data-parent-port-id="{esc(row["parent"])}"')
    if row.get("parent_name"):
        attrs.append(f'data-parent-name="{esc(row["parent_name"])}"')
    if row.get("lag"):
        attrs.append(f'data-member-of-lag="{esc(row["lag"])}"')
    if row.get("lag_name"):
        attrs.append(f'data-lag-name="{esc(row["lag_name"])}"')
    companion = (
        f'<select name="device_selection_{esc(row["port_id"])}"><option value="7">m7</option></select>'
        if row.get("companion")
        else ""
    )
    # A port id is not always usable as a DOM id, so a row can name its own checkbox.
    dom_id = row.get("dom_id", row["port_id"])
    return (
        f"<tr {' '.join(attrs)}>"
        f'<td data-col="selection"><input type="checkbox" name="select" value="{esc(row["port_id"])}"'
        f' id="cb-{esc(dom_id)}"></td>'
        f"<td>{row['name']}{companion}</td></tr>"
    )


def _selection_page_html(rows, *, auto_select=True):
    checked = "checked" if auto_select else ""
    body = "".join(_selection_row_markup(row) for row in rows)
    return f"""<!doctype html><html><body>
        <input type="checkbox" id="autoSelectLagMembers" {checked}>
        <form id="sync-form" method="post" action="{SELECTION_PAGE_URL}/submit">
          <input type="hidden" name="server_key" value="production">
          <table id="librenms-interface-table">
            <thead><tr><th><input type="checkbox" class="toggle"></th><th>Name</th></tr></thead>
            <tbody>{body}</tbody>
          </table>
          <button type="submit" id="do-sync">Sync</button>
        </form>
        </body></html>"""


def _load_selection_page(page, rows, *, url=SELECTION_PAGE_URL, auto_select=True):
    """Serve the fixture page from a real origin so sessionStorage behaves as it does in NetBox."""
    html = _selection_page_html(rows, auto_select=auto_select)
    page.route(
        f"{SELECTION_PAGE_URL}**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto(url)
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeCheckboxes()")


def _checked_values(page):
    return set(page.evaluate("Array.from(document.querySelectorAll('input[name=select]:checked')).map(cb => cb.value)"))


class TestRequirementCascade:
    """Selecting a row must select everything that row depends on, all the way up."""

    def test_sub_interface_pulls_in_its_parent_and_that_parent_s_aggregate(self, page):
        _load_selection_page(page, JUNOS_ROWS)

        page.check("#cb-4302")

        # et-0/0/6.0 needs et-0/0/6, which needs ae2. Stopping at the parent leaves the
        # aggregate unsynced, and the LAG assignment is then silently dropped.
        assert _checked_values(page) == {"4302", "4301", "4303"}

    def test_aggregate_unit_pulls_in_its_aggregate(self, page):
        _load_selection_page(page, JUNOS_ROWS)

        page.check("#cb-4304")

        assert _checked_values(page) == {"4304", "4303"}

    def test_clearing_the_last_dependent_releases_the_whole_chain(self, page):
        _load_selection_page(page, JUNOS_ROWS)

        page.check("#cb-4302")
        page.uncheck("#cb-4302")

        assert _checked_values(page) == set()

    def test_a_chain_held_by_another_row_is_not_released(self, page):
        _load_selection_page(page, JUNOS_ROWS)

        page.check("#cb-4302")
        page.check("#cb-4304")
        page.uncheck("#cb-4302")

        # ae2 is still required by ae2.0; only et-0/0/6 goes.
        assert _checked_values(page) == {"4304", "4303"}

    def test_an_aggregate_the_user_chose_survives_its_dependents(self, page):
        _load_selection_page(page, JUNOS_ROWS)

        page.check("#cb-4303")
        page.check("#cb-4302")
        page.uncheck("#cb-4302")

        # ae2 pulled et-0/0/6 in as a member, and ae2 was the user's own choice, so both stay.
        assert _checked_values(page) == {"4303", "4301"}

    def test_a_member_the_user_clears_stays_cleared(self, page):
        _load_selection_page(page, JUNOS_ROWS)

        page.check("#cb-4303")
        page.uncheck("#cb-4301")

        assert _checked_values(page) == {"4303"}

    def test_a_required_row_the_user_clears_is_not_re_added(self, page):
        _load_selection_page(page, JUNOS_ROWS)
        page.check("#cb-4302")

        page.uncheck("#cb-4303")

        # ae2 was pulled in for et-0/0/6.0, but a missing aggregate only leaves the LAG unset,
        # so the box must stay where the user put it instead of snapping back.
        assert _checked_values(page) == {"4302", "4301"}

    def test_a_cleared_row_is_offered_again_once_nothing_needs_it(self, page):
        _load_selection_page(page, JUNOS_ROWS)
        page.check("#cb-4302")
        page.uncheck("#cb-4303")
        page.uncheck("#cb-4302")

        page.check("#cb-4302")

        assert _checked_values(page) == {"4302", "4301", "4303"}

    def test_the_cascade_is_off_when_the_toggle_is_off(self, page):
        _load_selection_page(page, JUNOS_ROWS, auto_select=False)

        page.check("#cb-4302")

        assert _checked_values(page) == {"4302"}

    def test_turning_the_toggle_off_releases_only_what_the_cascade_added(self, page):
        _load_selection_page(page, JUNOS_ROWS)
        # ae2 first: clicking it once et-0/0/6.0 has already pulled it in would be a no-op,
        # so it would stay a row the cascade owns rather than one the user chose.
        page.check("#cb-4303")
        page.check("#cb-4302")

        page.uncheck("#autoSelectLagMembers")

        # ae2 pulled et-0/0/6 in and et-0/0/6.0 required both; only the two rows the user
        # clicked survive turning the cascade off.
        assert _checked_values(page) == {"4302", "4303"}

    def test_a_parent_on_another_page_raises_a_notice(self, page):
        # et-0/0/6.0 is rendered without its parent row, as it is when they straddle a page break.
        _load_selection_page(page, [JUNOS_ROWS[3]])

        page.check("#cb-4302")

        notice = page.locator("#parent-cross-page-notices").inner_text()
        assert "Parent interface" in notice
        assert "et-0/0/6" in notice

    def test_an_aggregate_on_another_page_is_named_as_a_lag(self, page):
        # et-0/0/6 without its ae2 row: the notice has to say LAG, not Parent.
        rows = [dict(JUNOS_ROWS[2], lag_name="ae2")]
        _load_selection_page(page, rows)

        page.check("#cb-4301")

        notice = page.locator("#parent-cross-page-notices").inner_text()
        assert "LAG interface" in notice
        assert "ae2" in notice

    def test_turning_the_toggle_back_on_re_derives_the_chain(self, page):
        _load_selection_page(page, JUNOS_ROWS)
        page.check("#cb-4302")
        page.uncheck("#autoSelectLagMembers")

        page.check("#autoSelectLagMembers")

        assert _checked_values(page) == {"4302", "4301", "4303"}

    def test_a_port_id_with_selector_metacharacters_still_cascades(self, page):
        # port_id values are integers today, so this only guards the escaping: an unescaped
        # quote would abort the member selector and silently select nothing.
        rows = [
            {"port_id": 'a"b', "name": "ae9", "dom_id": "ae9"},
            {"port_id": "5001", "name": "et-0/0/9", "lag": 'a"b'},
        ]
        _load_selection_page(page, rows)

        page.check("#cb-ae9")

        assert _checked_values(page) == {'a"b', "5001"}

    def test_select_all_pulls_in_requirements_once(self, page):
        _load_selection_page(page, JUNOS_ROWS)

        page.check("input.toggle")

        assert _checked_values(page) == {"4301", "4302", "4303", "4304"}


class TestCrossPageSelection:
    """A selection must survive paging, and reach the server when it is submitted."""

    def test_selection_is_restored_when_the_row_comes_back(self, page):
        _load_selection_page(page, JUNOS_ROWS, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-4304")

        page.goto(f"{SELECTION_PAGE_URL}?page=2")
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeCheckboxes()")

        assert "4304" in _checked_values(page)

    def test_rows_selected_on_another_page_are_counted_on_this_one(self, page):
        _load_selection_page(page, JUNOS_ROWS, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-4304")

        # Page 2 renders a different slice, so the ae2.0 row is not in the DOM any more.
        _load_selection_page(page, [JUNOS_ROWS[2]], url=f"{SELECTION_PAGE_URL}?page=2")

        notice = page.locator("#librenms-interface-table-offpage-selection")
        assert "2 more rows are selected on other pages." in notice.inner_text()

    def test_rows_selected_on_another_page_are_submitted(self, page):
        _load_selection_page(page, JUNOS_ROWS, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-4304")
        _load_selection_page(page, [JUNOS_ROWS[2]], url=f"{SELECTION_PAGE_URL}?page=2")
        page.check("#cb-4301")

        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")
        posted = request_info.value.post_data

        # The visible row serializes itself; ae2.0 and the ae2 it required come from the store.
        assert sorted(value for key, value in _selection_form_pairs(posted) if key == "select") == [
            "4301",
            "4303",
            "4304",
        ]

    def test_a_restored_row_keeps_the_standing_it_had(self, page):
        _load_selection_page(page, JUNOS_ROWS, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-4302")

        # Page away and back: et-0/0/6 and ae2 were pulled in by the cascade, not chosen, so
        # clearing et-0/0/6.0 must still release them.
        _load_selection_page(page, [JUNOS_ROWS[2]], url=f"{SELECTION_PAGE_URL}?page=2")
        _load_selection_page(page, JUNOS_ROWS, url=f"{SELECTION_PAGE_URL}?page=1")
        assert _checked_values(page) == {"4302", "4301", "4303"}

        page.uncheck("#cb-4302")

        assert _checked_values(page) == set()

    def test_a_row_key_that_collides_with_an_object_property_survives(self, page):
        # port_id values are integers today; this pins that the store holds row keys as data,
        # so a key like __proto__ is kept rather than swallowed by the prototype setter.
        rows = [{"port_id": "__proto__", "name": "ae9", "dom_id": "proto"}, JUNOS_ROWS[2]]
        _load_selection_page(page, rows, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-proto")

        _load_selection_page(page, [JUNOS_ROWS[1]], url=f"{SELECTION_PAGE_URL}?page=2")
        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")

        assert ("select", "__proto__") in _selection_form_pairs(request_info.value.post_data)

    def test_clearing_the_notice_drops_only_the_off_page_selection(self, page):
        """The notice counts other pages, so Clear must not take this page's rows with it."""
        _load_selection_page(page, JUNOS_ROWS, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-4304")
        _load_selection_page(page, [JUNOS_ROWS[2]], url=f"{SELECTION_PAGE_URL}?page=2")
        page.check("#cb-4301")
        visible = _checked_values(page)
        assert visible

        page.click("#librenms-interface-table-offpage-selection button")

        assert page.locator("#librenms-interface-table-offpage-selection").count() == 0
        assert _checked_values(page) == visible

        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")
        pairs = _selection_form_pairs(request_info.value.post_data)
        assert ("select", "4304") not in pairs
        assert ("select", "4301") in pairs

    def test_a_companion_input_travels_with_its_row(self, page):
        rows = [dict(JUNOS_ROWS[0], companion=True), JUNOS_ROWS[2]]
        _load_selection_page(page, rows, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-4303")

        _load_selection_page(page, [JUNOS_ROWS[1]], url=f"{SELECTION_PAGE_URL}?page=2")
        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")

        assert ("device_selection_4303", "7") in _selection_form_pairs(request_info.value.post_data)


def _selection_form_pairs(post_data):
    """Parse an application/x-www-form-urlencoded body into (name, value) pairs."""
    from urllib.parse import parse_qsl

    return parse_qsl(post_data or "")


def _replace_fixture_markup(html, old, new):
    """Replace fixture markup, failing when _page_html no longer emits the source markup."""
    assert old in html, f"fixture markup is gone, so the replacement would be a no-op: {old}"
    return html.replace(old, new)


def _page_html(initial_state, contract=None, active_tab="interfaces", valid_states=None):
    state = json.dumps(initial_state)
    contract = contract or {
        "interfaces": {"content_id": "interface-sync-content", "label": "Interface"},
        "ipaddresses": {"content_id": "ipaddress-sync-content", "label": "IP address"},
    }
    states = (
        ["ready", "invalidated", "refresh_failed", "locally_changed", "missing"]
        if valid_states is None
        else valid_states
    )
    serialized_contract = {
        "tabs": contract,
        "states": states,
    }
    contract_json = json.dumps(serialized_contract)
    interface_content_id = contract["interfaces"]["content_id"]
    ip_content_id = contract["ipaddresses"]["content_id"]
    return f"""
        <div id="librenms-sync-cache-state"
             data-status-url="https://plugin.example.com/status"
             data-server-key="primary"></div>
        <script id="librenms-sync-cache-initial" type="application/json">{state}</script>
        <script id="librenms-sync-cache-contract" type="application/json">{contract_json}</script>
        <div id="librenms-sync-tabs" data-active-tab="{active_tab}">
          <ul id="librenmsSync">
            <li><a id="interfaces-tab" class="nav-link {"active" if active_tab == "interfaces" else ""}"
                   data-tab="interfaces" href="https://plugin.example.com/page?tab=interfaces"
                   data-tab-label="Interfaces"
                   aria-controls="interfaces">Interfaces</a></li>
            <li><a id="ipaddresses-tab" class="nav-link {"active" if active_tab == "ipaddresses" else ""}"
                   data-tab="ipaddresses" href="https://plugin.example.com/page?tab=ipaddresses"
                   data-tab-label="IP Addresses"
                   aria-controls="ipaddresses">IP Addresses</a></li>
          </ul>
        <div id="interfaces" class="tab-pane{" active" if active_tab == "interfaces" else ""}"
             data-tab-id="interfaces"
             data-fragment-url="https://plugin.example.com/fragment/interfaces">
          <div id="{interface_content_id}"><button id="interface-action">Sync</button></div>
        </div>
        <div id="ipaddresses" class="tab-pane{" active" if active_tab == "ipaddresses" else ""}"
             data-tab-id="ipaddresses"
             data-fragment-url="https://plugin.example.com/fragment/ipaddresses">
          <div id="{ip_content_id}"><button id="ip-action">Sync</button></div>
        </div>
        </div>
        <div id="htmx-modal-content">
          <form><button id="modal-force-action" type="submit">Force</button></form>
        </div>
    """


def test_server_contract_drives_cache_content_replacement(page):
    """The browser must use the tab metadata supplied by the server."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        **initial,
        "interfaces": _state(
            "after-interfaces",
            state="invalidated",
            source_tab="ipaddresses",
            available=False,
        ),
    }
    contract = {
        "interfaces": {"content_id": "custom-interface-content", "label": "Network port"},
        "ipaddresses": {"content_id": "ipaddress-sync-content", "label": "IP address"},
    }

    page.set_content(_page_html(initial, contract))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
        current,
    )

    assert page.locator("#interface-action").count() == 0
    assert "Network port data" in page.locator("#custom-interface-content").inner_text()


def test_server_contract_defines_valid_wire_states(page):
    """The browser must validate status states against the server contract."""
    initial = {
        "interfaces": _state("before-interfaces", state="source_ready"),
        "ipaddresses": _state("before-ip", state="source_ready"),
    }

    page.set_content(_page_html(initial, valid_states=["source_ready"]))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": initial}),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().checking === null")

    assert page.evaluate("syncCacheController().lastCheckFailed") is False
    assert page.locator("#interface-action").count() == 1
    assert not page.locator("#modal-force-action").is_disabled()


def _state(
    revision,
    state="ready",
    *,
    source_tab=None,
    same_user=False,
    available=True,
    reason=None,
    refresh_error=None,
    refresh_error_timestamp=None,
    attention_required=None,
):
    state = {
        "revision": revision,
        "state": state,
        "source_tab": source_tab,
        "timestamp": "2026-08-14T00:00:00+00:00",
        "reason": reason,
        "refresh_error": refresh_error,
        "refresh_error_timestamp": refresh_error_timestamp,
        "same_user": same_user,
        "snapshot_available": available,
    }
    if attention_required is not None:
        state["attention_required"] = attention_required
    return state


def test_outer_tab_navigation_uses_the_rendered_status_as_the_next_baseline(page):
    """A status response started before navigation must not overwrite server-rendered state."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ipaddresses"),
    }
    rendered = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("rendered-ipaddresses"),
    }
    stale = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state(
            "stale-ipaddresses",
            state="locally_changed",
            source_tab="ipaddresses",
        ),
    }
    fragment_requests = []

    page.set_content(_page_html(initial))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("document.dispatchEvent(new Event('DOMContentLoaded')); initializeScripts();")
    page.evaluate(
        """() => {
            const realFetch = window.fetch;
            window.statusRequestStarted = false;
            window.releaseStatus = null;
            window.fetch = (input, init) => {
                if (String(input).includes('/status?')) {
                    window.statusRequestStarted = true;
                    return new Promise(resolve => {
                        window.releaseStatus = () => resolve(new Response(
                            JSON.stringify({tabs: window.staleStatus}),
                            {status: 200, headers: {'Content-Type': 'application/json'}}
                        ));
                    });
                }
                return realFetch(input, init);
            };
        }"""
    )
    page.route(
        "https://plugin.example.com/fragment/ipaddresses?*",
        lambda route: (fragment_requests.append(route.request.url), route.fulfill(body="<p>Reloaded</p>")),
    )
    page.evaluate(
        """() => {
            window.inFlightStatus = checkSyncCacheStatus();
            window.statusRequestSettled = false;
            window.inFlightStatus.then(() => { window.statusRequestSettled = true; });
        }"""
    )
    page.wait_for_function("window.statusRequestStarted === true")

    page.evaluate(
        """status => {
            const region = document.querySelector('#librenms-sync-tabs');
            region.outerHTML = '<div id="librenms-sync-tabs" data-active-tab="ipaddresses">'
                + '<script id="librenms-sync-rendered-status" type="application/json">'
                + JSON.stringify(status)
                + '</script>'
                + '<div id="interfaces" class="tab-pane" data-tab-id="interfaces" '
                + 'data-fragment-url="https://plugin.example.com/fragment/interfaces"></div>'
                + '<div id="ipaddresses" class="tab-pane active" data-tab-id="ipaddresses" '
                + 'data-fragment-url="https://plugin.example.com/fragment/ipaddresses">'
                + '<div id="ipaddress-sync-content"><p>Server-rendered IP rows</p></div>'
                + '</div></div>';
            document.querySelector('#librenms-sync-tabs').dispatchEvent(
                new CustomEvent('htmx:afterSwap', {bubbles: true})
            );
        }""",
        rendered,
    )
    assert page.evaluate("syncCacheController().status.ipaddresses.revision") == "rendered-ipaddresses"

    page.evaluate("status => { window.staleStatus = status; window.releaseStatus(); }", stale)
    page.wait_for_function("window.statusRequestSettled === true")

    assert fragment_requests == []
    assert page.locator("#ipaddress-sync-content").inner_text() == "Server-rendered IP rows"
    assert page.evaluate("syncCacheController().status.ipaddresses.revision") == "rendered-ipaddresses"


def test_cold_tab_does_not_show_stale_state_before_first_refresh(page):
    """A tab with no snapshot history must retain its initial refresh state."""
    initial = {
        "interfaces": _state(None, state="missing", available=False),
        "ipaddresses": _state(None, state="missing", available=False),
    }
    initial["interfaces"]["timestamp"] = None
    initial["ipaddresses"]["timestamp"] = None
    html = _replace_fixture_markup(
        _page_html(initial),
        '<button id="interface-action">Sync</button>',
        '<button id="interface-refresh">Refresh Interfaces</button>',
    )

    page.set_content(html)
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency()")

    assert page.locator("#interface-refresh").count() == 1
    assert not page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")
    assert "cache is unavailable" not in page.locator("#interface-sync-content").inner_text()


def test_cache_reads_carry_the_csrf_token(page):
    """The frontend rule is that every fetch() sends the token, so both cache reads must."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
    }
    seen = {}

    def record(name, fulfil):
        def handler(route):
            seen[name] = route.request.headers.get("x-csrftoken")
            fulfil(route)

        return handler

    page.route(
        "https://plugin.example.com/status?*",
        record("status", lambda route: route.fulfill(json={"tabs": initial})),
    )
    page.route(
        "https://plugin.example.com/fragment/interfaces*",
        record(
            "fragment",
            lambda route: route.fulfill(
                body='<div id="interface-sync-content">refreshed</div>', content_type="text/html"
            ),
        ),
    )
    page.set_content('<input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">' + _page_html(initial))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        """
        async () => {
            initializeSyncCacheConsistency();
            await checkSyncCacheStatus();
            await loadSyncCacheFragment('interfaces');
        }
        """
    )

    assert seen == {"status": "test-csrf-token", "fragment": "test-csrf-token"}


def test_cold_tab_does_not_flash_stale_during_tab_navigation(page):
    """Checking a never-refreshed tab must not briefly mark it stale."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state(None, state="missing", available=False),
    }
    initial["ipaddresses"]["timestamp"] = None

    page.route(
        "https://plugin.example.com/page*",
        lambda route: route.fulfill(body=_page_html(initial), content_type="text/html"),
    )
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": initial}),
    )
    page.goto("https://plugin.example.com/page")
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        """
        () => {
            initializeSyncCacheConsistency();
            const tab = document.querySelector('#ipaddresses-tab');
            new MutationObserver(() => {
                if (tab.classList.contains('sync-cache-unavailable')) {
                    sessionStorage.setItem('ip-state-flashed', 'true');
                }
            }).observe(tab, { attributes: true, attributeFilter: ['class'] });
        }
        """
    )
    page.locator("#ipaddresses-tab").click()
    page.wait_for_url("**/page?tab=ipaddresses")

    assert page.evaluate("sessionStorage.getItem('ip-state-flashed')") is None
    assert not page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")


def test_status_check_queues_one_follow_up_while_a_request_is_in_flight(page):
    """A second trigger must observe state that changed during the first request."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
    }
    changed = {
        **initial,
        "ipaddresses": _state(
            "ipaddresses-invalidated",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }
    payloads = [initial, changed]
    requests = []

    def serve_status(route):
        requests.append(route.request.url)
        route.fulfill(json={"tabs": payloads[min(len(requests) - 1, 1)]})

    page.set_content(_page_html(initial))
    page.route("https://plugin.example.com/status?*", serve_status)
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        """
        () => {
            initializeSyncCacheConsistency();
            checkSyncCacheStatus();
            checkSyncCacheStatus();
        }
        """
    )
    page.wait_for_function("document.querySelector('#ipaddresses-tab').classList.contains('sync-cache-unavailable')")

    assert len(requests) == 2
    assert page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")


def test_healthy_tab_click_navigates_without_bootstrap_global(page):
    """A ready tab must load when NetBox does not expose Bootstrap as a global."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
    }

    def serve_page(route):
        active_tab = "ipaddresses" if "tab=ipaddresses" in route.request.url else "interfaces"
        route.fulfill(body=_page_html(initial, active_tab=active_tab), content_type="text/html")

    page.route("https://plugin.example.com/page*", serve_page)
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": initial}),
    )
    page.goto("https://plugin.example.com/page")
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency()")

    assert page.evaluate("typeof bootstrap") == "undefined"
    page.locator("#ipaddresses-tab").click()
    page.wait_for_url("**/page?tab=ipaddresses")

    assert page.locator("#ipaddresses").evaluate("node => node.classList.contains('active')")


def test_refreshing_one_tab_does_not_mark_never_refreshed_tabs_stale(page):
    """A successful refresh must not change untouched cold-tab affordances."""
    initial = {
        "interfaces": _state(None, state="missing", available=False),
        "ipaddresses": _state(None, state="missing", available=False),
    }
    initial["interfaces"]["timestamp"] = None
    initial["ipaddresses"]["timestamp"] = None
    refreshed = {
        "interfaces": _state("interfaces-refresh"),
        "ipaddresses": initial["ipaddresses"],
    }

    page.set_content(_page_html(initial))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
        refreshed,
    )

    assert not page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")


def test_cache_state_uses_theme_rails_without_resizing(page):
    """Available and unavailable states must not add text or change tab dimensions."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
    }
    invalidated = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }

    page.set_content(_page_html(initial))
    page.add_style_tag(path=str(STYLE_PATH))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        """
        () => {
            document.documentElement.style.setProperty('--tblr-warning', 'rgb(245, 159, 0)');
            document.documentElement.style.setProperty('--tblr-success', 'rgb(47, 179, 68)');
            document.documentElement.style.setProperty('--tblr-border-radius', '4px');
            initializeSyncCacheConsistency();
        }
        """
    )
    width_before = page.locator("#ipaddresses-tab").evaluate("node => node.getBoundingClientRect().width")
    tab = page.locator("#ipaddresses-tab")

    assert tab.evaluate("node => node.classList.contains('sync-cache-ready')")
    assert tab.evaluate("node => getComputedStyle(node, '::before').backgroundColor") == "rgb(47, 179, 68)"

    page.evaluate("status => reconcileSyncCacheStatus(status)", invalidated)
    width_after = tab.evaluate("node => node.getBoundingClientRect().width")

    assert width_after == width_before
    assert tab.inner_text() == "IP Addresses"
    assert tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")
    assert tab.get_attribute("title") == "Cached data is unavailable. Refresh this tab."
    assert tab.get_attribute("aria-label") == "IP Addresses. Cached data is unavailable."
    assert tab.evaluate("node => getComputedStyle(node, '::before').height") == "3px"
    assert tab.evaluate("node => getComputedStyle(node, '::before').backgroundColor") == "rgb(245, 159, 0)"

    page.evaluate("document.documentElement.style.setProperty('--tblr-warning', 'rgb(255, 193, 7)')")
    assert tab.evaluate("node => getComputedStyle(node, '::before').backgroundColor") == "rgb(255, 193, 7)"


def test_selecting_an_unavailable_tab_acknowledges_its_attention_state(page):
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }

    page.route(
        "https://plugin.example.com/page*",
        lambda route: route.fulfill(
            body=_page_html(
                initial,
                active_tab="ipaddresses" if "tab=ipaddresses" in route.request.url else "interfaces",
            ),
            content_type="text/html",
        ),
    )
    page.goto("https://plugin.example.com/page")
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency()")
    tab = page.locator("#ipaddresses-tab")
    assert tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")

    tab.click()
    page.wait_for_url("**/page?tab=ipaddresses")
    selected_tab = page.locator("#ipaddresses-tab")

    assert not selected_tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")
    assert selected_tab.get_attribute("title") is None


def test_acknowledged_status_does_not_restore_unavailable_attention(page):
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
            attention_required=False,
        ),
    }

    page.set_content(_page_html(initial))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency()")

    assert not page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")


def test_focus_check_clears_invalidated_rows_and_reports_anonymous_actor(page):
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("mutation", state="locally_changed", source_tab="interfaces"),
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }

    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); window.dispatchEvent(new Event('blur'))")
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.wait_for_function("document.querySelector('#ipaddress-sync-content').dataset.cacheEmpty === 'true'")

    assert page.locator("#ip-action").count() == 0
    assert page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")
    notice = page.locator("#librenms-sync-cache-notices").inner_text()
    assert "Some sync data was cleared because another user synchronized data from LibreNMS." in notice
    assert "Interface data" not in notice
    assert "another user synchronized Interface data" in page.locator("#ipaddress-sync-content").inner_text()


def test_cache_status_failure_disables_every_loaded_sync_control(page):
    """A failed status check must fail closed across tabs and an open modal."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }

    console_errors = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(status=503),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().lastCheckFailed === true")

    assert page.locator("#interface-action").count() == 0
    assert page.locator("#ip-action").count() == 0
    assert page.locator("#modal-force-action").is_disabled()
    assert "could not be verified" in page.locator("#interface-sync-content").inner_text()
    assert "could not be verified" in page.locator("#ipaddress-sync-content").inner_text()
    assert any("HTTP 503" in message for message in console_errors), console_errors


def test_successful_cache_status_check_restores_only_fail_closed_modal_controls(page):
    """A transient status failure must not leave an open modal permanently disabled."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    attempts = 0

    def status_response(route):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            route.fulfill(status=503)
        else:
            route.fulfill(json={"tabs": initial})

    page.set_content(_page_html(initial))
    page.evaluate(
        """() => {
            const control = document.createElement('button');
            control.id = 'modal-already-disabled';
            control.disabled = true;
            document.querySelector('#htmx-modal-content form').append(control);
        }"""
    )
    page.route("https://plugin.example.com/status?*", status_response)
    page.route(
        "https://plugin.example.com/fragment/interfaces?*",
        lambda route: route.fulfill(body='<button id="restored-interface-action">Sync</button>'),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().lastCheckFailed === true && syncCacheController().checking === null")

    assert page.locator("#modal-force-action").is_disabled()
    assert page.locator("#modal-already-disabled").is_disabled()

    page.evaluate("checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().lastCheckFailed === false && syncCacheController().checking === null")

    assert not page.locator("#modal-force-action").is_disabled()
    assert page.locator("#modal-already-disabled").is_disabled()
    assert page.locator("#restored-interface-action").count() == 1


def test_available_status_without_a_usable_fragment_keeps_modal_controls_disabled(page):
    """A status response alone must not restore controls after fail-closed content loss."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    attempts = 0

    def status_response(route):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            route.fulfill(status=503)
        else:
            route.fulfill(json={"tabs": initial})

    page.set_content(_page_html(initial))
    page.route("https://plugin.example.com/status?*", status_response)
    page.route(
        "https://plugin.example.com/fragment/interfaces?*",
        lambda route: route.fulfill(status=503),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().lastCheckFailed === true && syncCacheController().checking === null")

    page.evaluate("checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().lastCheckFailed === false && syncCacheController().checking === null")

    assert page.locator("#modal-force-action").is_disabled()
    assert "could not be restored" in page.locator("#interface-sync-content").inner_text()


def test_hung_cache_status_request_times_out_and_fails_closed(page):
    """A stalled status request must release the controller and remove stale controls."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }

    page.clock.install()
    page.set_content(_page_html(initial))
    stalled_routes = []
    page.route("https://plugin.example.com/status?*", lambda route: stalled_routes.append(route))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("() => { initializeSyncCacheConsistency(); checkSyncCacheStatus(); }")
    assert page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")

    page.clock.fast_forward(20_000)
    page.clock.resume()
    page.wait_for_function("syncCacheController().lastCheckFailed === true", timeout=1_000)

    assert page.evaluate("syncCacheController().checking") is None
    assert page.locator("#interface-action").count() == 0
    assert not page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")
    for route in stalled_routes:
        route.abort()


def test_hung_cache_fragment_request_times_out_and_fails_closed(page):
    """A stalled fragment request must release the controller and remove stale controls."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        **initial,
        "interfaces": _state("after-interfaces"),
    }

    page.clock.install()
    page.set_content(_page_html(initial))
    stalled_routes = []
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.route("https://plugin.example.com/fragment/interfaces?*", lambda route: stalled_routes.append(route))
    page.add_script_tag(path=str(SCRIPT_PATH))
    with page.expect_request("https://plugin.example.com/fragment/interfaces?*"):
        page.evaluate("() => { initializeSyncCacheConsistency(); checkSyncCacheStatus(); }")
    page.wait_for_function("syncCacheController().status.interfaces.revision === 'after-interfaces'")
    assert page.evaluate("syncCacheController().checking !== null")
    assert page.evaluate("syncCacheController().lastCheckFailed") is False
    assert page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")

    page.clock.fast_forward(20_000)
    page.clock.resume()
    page.wait_for_function("syncCacheController().lastCheckFailed === true", timeout=1_000)

    assert page.evaluate("syncCacheController().checking") is None
    assert page.locator("#interface-action").count() == 0
    assert not page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")
    assert stalled_routes
    for route in stalled_routes:
        route.abort()


def test_valid_status_recovers_when_the_initial_state_is_malformed(page):
    """The stable tab contract must validate status when initial state parsing fails."""
    current = {
        "interfaces": _state("current-interfaces"),
        "ipaddresses": _state("current-ipaddresses"),
    }
    html = _replace_fixture_markup(
        _page_html(current),
        f'<script id="librenms-sync-cache-initial" type="application/json">{json.dumps(current)}</script>',
        '<script id="librenms-sync-cache-initial" type="application/json">{malformed</script>',
    )

    page.set_content(html)
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.route(
        "https://plugin.example.com/fragment/interfaces?*",
        lambda route: route.fulfill(body='<button id="interface-action">Sync</button>'),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().checking === null")

    assert page.evaluate("syncCacheController().lastCheckFailed") is False
    assert page.locator("#interface-action").count() == 1
    assert page.locator("#ip-action").count() == 1


@pytest.mark.parametrize(
    "contract_text",
    [
        pytest.param("{malformed", id="invalid-json"),
        pytest.param(
            json.dumps(
                {
                    "tabs": {"interfaces": None, "ipaddresses": None},
                    "states": ["ready", "invalidated"],
                }
            ),
            id="malformed-tab-specs",
        ),
    ],
)
def test_invalid_cache_contract_stops_status_requests_and_fails_closed(page, contract_text):
    """An immutable contract error must require reload instead of retrying requests."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    attempts = 0

    def status_response(route):
        nonlocal attempts
        attempts += 1
        route.fulfill(json={"tabs": initial})

    page.set_content(_page_html(initial))
    page.locator("#librenms-sync-cache-contract").evaluate(
        "(node, text) => { node.textContent = text; }",
        contract_text,
    )
    page.route("https://plugin.example.com/status?*", status_response)
    page.add_script_tag(path=str(SCRIPT_PATH))

    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().checking === null")
    page.evaluate("checkSyncCacheStatus()")

    assert attempts == 0
    assert page.evaluate("syncCacheController().lastCheckFailed") is True
    assert page.locator("#interface-action").is_disabled()
    assert page.locator("#ip-action").is_disabled()
    assert page.locator("#modal-force-action").is_disabled()


def test_fragment_failure_logs_the_http_status_and_clears_the_content(page):
    """A fragment failure must retain its diagnostic while failing closed."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        **initial,
        "interfaces": _state("after-interfaces", state="locally_changed", source_tab="interfaces"),
    }

    console_errors = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.route(
        "https://plugin.example.com/fragment/interfaces?*",
        lambda route: route.fulfill(status=503),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("document.querySelector('#interface-sync-content').dataset.cacheEmpty === 'true'")

    assert any("HTTP 503" in message for message in console_errors), console_errors
    assert "could not be restored" in page.locator("#interface-sync-content").inner_text()


def test_malformed_cache_status_disables_every_loaded_sync_control(page):
    """A successful response with an invalid schema must fail closed."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }

    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": None}),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().checking === null")

    assert page.locator("#interface-action").count() == 0
    assert page.locator("#ip-action").count() == 0
    assert page.locator("#modal-force-action").is_disabled()
    assert page.evaluate("syncCacheController().lastCheckFailed") is True


def test_null_cache_status_disables_every_loaded_sync_control(page):
    """A null JSON response must fail closed like every other invalid schema."""
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }

    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(body="null", content_type="application/json"),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().checking === null")

    assert page.locator("#interface-action").count() == 0
    assert page.locator("#ip-action").count() == 0
    assert page.locator("#modal-force-action").is_disabled()
    assert page.evaluate("syncCacheController().lastCheckFailed") is True


def test_changed_source_comparison_restores_from_cache_fragment(page):
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("after", state="locally_changed", source_tab="interfaces"),
        "ipaddresses": _state("before-ip"),
    }

    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.route(
        "https://plugin.example.com/fragment/interfaces?*",
        lambda route: route.fulfill(body='<div id="restored-comparison">Current comparison</div>'),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")

    assert page.locator("#restored-comparison").inner_text() == "Current comparison"


def test_countdown_expiry_removes_rows_and_sync_controls(page):
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }

    page.set_content(
        _replace_fixture_markup(
            _page_html(initial),
            '<button id="interface-action">Sync</button>',
            '<span id="countdown-timer" data-expiry="2000-01-01T00:00:00Z"></span>'
            '<button id="interface-action">Sync</button>',
        )
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); initializeCountdown('countdown-timer')")

    assert page.locator("#interface-action").count() == 0
    assert "cache expired" in page.locator("#interface-sync-content").inner_text()
    assert page.locator("#interfaces-tab").evaluate(
        "node => !node.classList.contains('sync-cache-ready') && !node.classList.contains('sync-cache-unavailable')"
    )
    assert page.locator("#interfaces-tab").get_attribute("title") is None


def test_hidden_tab_countdown_changes_available_rail_to_unavailable_without_interaction(page):
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }

    page.set_content(
        _replace_fixture_markup(
            _page_html(initial),
            '<button id="ip-action">Sync</button>',
            '<span id="ip-countdown-timer" data-expiry="2000-01-01T00:00:00Z"></span>'
            '<button id="ip-action">Sync</button>',
        )
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency(); initializeCountdown('ip-countdown-timer')")
    tab = page.locator("#ipaddresses-tab")

    assert not tab.evaluate("node => node.classList.contains('sync-cache-ready')")
    assert tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")
    assert page.locator("#ip-action").count() == 0


def test_failed_retry_updates_an_already_invalidated_tab_without_a_new_revision(page):
    initial = {
        "interfaces": _state("mutation", state="locally_changed", source_tab="interfaces"),
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }
    current = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
            refresh_error="The latest LibreNMS refresh failed.",
            refresh_error_timestamp="2026-08-14T00:01:00+00:00",
        ),
    }

    page.set_content(_page_html(initial))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
        current,
    )

    text = page.locator("#ipaddress-sync-content").inner_text()
    assert "synchronized Interface data" in text
    assert "latest LibreNMS refresh failed" in text


def test_initiating_mutation_reports_one_toast_even_when_only_another_server_was_cleared(page):
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("mutation", state="locally_changed", source_tab="interfaces", same_user=True),
        "ipaddresses": initial["ipaddresses"],
    }
    payload = {
        "transition_id": "mutation",
        "removed": True,
        "cleanup_failed": False,
        "tabs": ["ipaddresses"],
        "revisions": {"primary:interfaces": "mutation"},
    }

    page.route(
        "https://plugin.example.com/page",
        lambda route: route.fulfill(body=_page_html(initial), content_type="text/html"),
    )
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.goto("https://plugin.example.com/page")
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "payload => { initializeSyncCacheConsistency(); "
        "document.dispatchEvent(new CustomEvent('librenmsCacheChanged', { detail: payload })); }",
        payload,
    )
    page.wait_for_function("document.querySelector('#librenms-sync-cache-notices') !== null")

    notices = page.locator("#librenms-sync-cache-notices .alert")
    assert notices.count() == 1
    assert "Other sync tabs were cleared" in notices.inner_text()


def test_ready_refresh_restores_an_active_tab_after_local_invalidation(page):
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    invalidated = {
        "interfaces": _state(
            "mutation",
            state="invalidated",
            source_tab="ipaddresses",
            available=False,
        ),
        "ipaddresses": _state("mutation", state="locally_changed", source_tab="ipaddresses"),
    }
    refreshed = {
        "interfaces": _state("refresh", state="ready", source_tab="interfaces"),
        "ipaddresses": invalidated["ipaddresses"],
    }

    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/fragment/interfaces?*",
        lambda route: route.fulfill(body='<div id="ready-comparison">Refreshed comparison</div>'),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "async states => { initializeSyncCacheConsistency(); "
        "await reconcileSyncCacheStatus(states.invalidated); "
        "await reconcileSyncCacheStatus(states.refreshed); }",
        {"invalidated": invalidated, "refreshed": refreshed},
    )

    assert page.locator("#ready-comparison").inner_text() == "Refreshed comparison"


def test_ready_revision_restores_when_invalidation_and_refresh_collapse(page):
    """A focus check must reload data even when it misses the invalidated revision."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    refreshed = {
        "interfaces": _state("refresh", state="ready", source_tab="interfaces"),
        "ipaddresses": initial["ipaddresses"],
    }

    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/fragment/interfaces?*",
        lambda route: route.fulfill(body='<div id="collapsed-refresh">Refreshed comparison</div>'),
    )
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
        refreshed,
    )

    assert page.locator("#collapsed-refresh").inner_text() == "Refreshed comparison"


def test_missing_snapshot_without_reason_shows_generic_warning(page):
    """An unexplained cache disappearance must not blame another user."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    missing = {
        "interfaces": _state(None, state="missing", available=False),
        "ipaddresses": initial["ipaddresses"],
    }

    page.set_content(_page_html(initial))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
        missing,
    )

    warning = page.locator("#interface-sync-content").inner_text()
    assert "cache is unavailable" in warning
    assert "another user" not in warning


def test_cross_user_refresh_failure_shows_shared_failure_reason(page):
    """A refresh failure must explain the shared state without naming the actor."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    failed = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state(
            "failure",
            state="refresh_failed",
            available=False,
            reason=(
                "Cached IP address data is unavailable because another user attempted "
                "to refresh it from LibreNMS and the refresh failed."
            ),
        ),
    }

    page.set_content(_page_html(initial))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
        failed,
    )

    warning = page.locator("#ipaddress-sync-content").inner_text()
    assert "another user attempted to refresh" in warning
    assert "refresh failed" in warning


def test_refreshed_hidden_tab_stays_marked_until_server_rendered_navigation(page):
    """A hidden tab with replaced rows must retain its unavailable state."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    invalidated = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }
    refreshed = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state("refresh", state="ready", source_tab="ipaddresses"),
    }

    page.set_content(_page_html(initial))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "async states => { initializeSyncCacheConsistency(); "
        "await reconcileSyncCacheStatus(states.invalidated); "
        "await reconcileSyncCacheStatus(states.refreshed); }",
        {"invalidated": invalidated, "refreshed": refreshed},
    )

    assert page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")


def test_initiating_inline_mutation_rebuilds_its_source_fragment(page):
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("mutation", state="locally_changed", source_tab="interfaces", same_user=True),
        "ipaddresses": initial["ipaddresses"],
    }
    payload = {
        "transition_id": "mutation",
        "removed": False,
        "cleanup_failed": False,
        "tabs": [],
        "cleanup_tabs": ["ipaddresses"],
        "source_tab": "interfaces",
        "source_fragment_required": True,
        "revisions": {"primary:interfaces": "mutation"},
    }

    page.route(
        "https://plugin.example.com/page",
        lambda route: route.fulfill(body=_page_html(initial), content_type="text/html"),
    )
    page.route("https://plugin.example.com/status?*", lambda route: route.fulfill(json={"tabs": current}))
    page.route(
        "https://plugin.example.com/fragment/interfaces?*",
        lambda route: route.fulfill(body='<div id="inline-comparison">Updated inline comparison</div>'),
    )
    page.goto("https://plugin.example.com/page")
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "payload => { initializeSyncCacheConsistency(); "
        "document.dispatchEvent(new CustomEvent('librenmsCacheChanged', { detail: payload })); }",
        payload,
    )
    page.wait_for_selector("#inline-comparison")

    assert page.locator("#inline-comparison").inner_text() == "Updated inline comparison"


def test_cleanup_failure_removes_controls_from_every_planned_tab(page):
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    payload = {
        "transition_id": "failed-cleanup",
        "removed": False,
        "cleanup_failed": True,
        "tabs": [],
        "cleanup_tabs": ["ipaddresses"],
        "source_tab": "interfaces",
        "source_fragment_required": False,
        "revisions": {},
    }

    page.set_content(_page_html(initial))
    page.route("https://plugin.example.com/status?*", lambda route: route.fulfill(json={"tabs": initial}))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "payload => { initializeSyncCacheConsistency(); "
        "document.dispatchEvent(new CustomEvent('librenmsCacheChanged', { detail: payload })); }",
        payload,
    )

    assert page.locator("#ip-action").count() == 0
    assert "cleanup could not be verified" in page.locator("#ipaddress-sync-content").inner_text()
    assert page.locator("#ipaddresses-tab").evaluate("node => node.classList.contains('sync-cache-unavailable')")


def test_cleanup_failure_notice_survives_a_later_informational_event(page):
    """A later success must not hide the reload instruction from a failed cleanup."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    failed = {
        "transition_id": "failed-cleanup",
        "removed": False,
        "cleanup_failed": True,
        "cleanup_tabs": ["ipaddresses"],
        "revisions": {},
    }
    succeeded = {
        "transition_id": "later-success",
        "removed": True,
        "cleanup_failed": False,
        "tabs": ["ipaddresses"],
        "revisions": {},
    }

    page.set_content(_page_html(initial))
    page.route("https://plugin.example.com/status?*", lambda route: route.fulfill(json={"tabs": initial}))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "payloads => { initializeSyncCacheConsistency(); "
        "payloads.forEach(payload => document.dispatchEvent("
        "new CustomEvent('librenmsCacheChanged', { detail: payload }))); }",
        [failed, succeeded],
    )

    notice = page.locator("#librenms-sync-cache-notices .alert")
    assert notice.count() == 1
    assert notice.evaluate("node => node.classList.contains('alert-danger')")
    assert "related cache cleanup failed" in notice.inner_text()
    assert "Other sync tabs were cleared" not in notice.inner_text()


def test_blocked_cache_notice_can_render_after_danger_is_dismissed(page):
    """A danger notice must not consume the revision of a blocked later notice."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    failed = {
        "transition_id": "failed-cleanup",
        "removed": False,
        "cleanup_failed": True,
        "cleanup_tabs": ["ipaddresses"],
        "revisions": {},
    }
    succeeded = {
        "transition_id": "later-success",
        "removed": True,
        "cleanup_failed": False,
        "tabs": ["ipaddresses"],
        "revisions": {},
    }

    page.set_content(_page_html(initial))
    page.route("https://plugin.example.com/status?*", lambda route: route.fulfill(json={"tabs": initial}))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "payloads => { initializeSyncCacheConsistency(); "
        "payloads.forEach(payload => document.dispatchEvent("
        "new CustomEvent('librenmsCacheChanged', { detail: payload }))); }",
        [failed, succeeded],
    )

    page.locator("#librenms-sync-cache-notices .btn-close").click()
    page.evaluate(
        "payload => document.dispatchEvent(new CustomEvent('librenmsCacheChanged', { detail: payload }))",
        succeeded,
    )

    notice = page.locator("#librenms-sync-cache-notices .alert")
    assert notice.count() == 1
    assert notice.evaluate("node => node.classList.contains('alert-info')")
    assert "Other sync tabs were cleared" in notice.inner_text()


def test_invalidation_reason_includes_relative_time(page):
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": initial["interfaces"],
        "ipaddresses": _state(
            "mutation",
            state="invalidated",
            source_tab="interfaces",
            available=False,
        ),
    }

    page.set_content(_page_html(initial))
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate(
        "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
        current,
    )

    assert "ago" in page.locator("#ipaddress-sync-content").inner_text()
