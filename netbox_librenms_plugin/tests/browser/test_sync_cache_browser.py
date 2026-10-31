"""Browser-level checks for the sync tabs: the cache state machine and table selection."""

import json
import re
from html import escape
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect


SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
TEMPLATE_DIR = Path(__file__).parents[2] / "templates" / "netbox_librenms_plugin"
STYLE_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "css" / "librenms_sync.css"
# NetBox 4.6.5 and 4.7 bundle htmx 2.0.10 (4.4.0 bundles 2.0.6), so the vendored copy tracks NetBox.
HTMX_PATH = Path(__file__).parent / "vendor" / "htmx.min.js"
# NetBox imports HTMX as a module, so a NetBox page has no `htmx` global. The function wrapper keeps
# the same shape here and publishes the API under another name for the tests that need it.
HTMX_SCRIPT = f"window.htmxTest = (function () {{\n{HTMX_PATH.read_text()}\n; return htmx; }})();"


def _add_page_scripts(page):
    """Load HTMX and then the plugin script into the current page, the way a NetBox page does."""
    page.add_script_tag(content=HTMX_SCRIPT)
    page.add_script_tag(path=str(SCRIPT_PATH))


def test_relationship_diagnostics_modal_opens_without_bootstrap(page):
    from django.template import Context, Engine, Library

    urls = Library()

    @urls.simple_tag(name="url")
    def url(name):
        return "/patterns/"

    engine = Engine()
    engine.template_builtins.append(urls)
    template = TEMPLATE_DIR / "inc" / "_relationship_diagnostics.html"
    diagnostics = {
        "pairs_seen": 1,
        "pairs_usable": 1,
        "pairs_unclassified": 0,
        "ports_seen": 2,
        "name_field": "ifName",
        "verdict": "Every usable pair was classified.",
        "kinds": [],
        "patterns": {"lag": [], "bridge": [], "sap": []},
    }
    fragment = engine.from_string(template.read_text(encoding="utf-8")).render(
        Context({"diagnostics": diagnostics}, use_l10n=False)
    )
    page.set_content(f"<style>.modal {{ display: none; }} .modal.show {{ display: block; }}</style>{fragment}")
    _add_page_scripts(page)

    assert page.evaluate("typeof bootstrap") == "undefined"
    modal = page.locator("#relationshipDiagnosticsModal")
    assert not modal.is_visible()
    page.get_by_role("button", name=re.compile("Relationship data:")).click()
    assert modal.is_visible()
    assert modal.get_attribute("aria-modal") == "true"

    modal.locator(".modal-footer button[data-bs-dismiss='modal']").click()
    assert not modal.is_visible()
    assert page.locator(".modal-backdrop").count() == 0


def test_the_platform_row_opens_the_create_platform_modal_without_bootstrap(page):
    """The device and VM sync pages share the platform row; its Create & Sync button opens the real modal form."""
    from types import SimpleNamespace

    from django.template import Context, Engine

    engine = Engine(dirs=[str(TEMPLATE_DIR.parent)])
    assessment = SimpleNamespace(
        reason="os_platform_missing", state="proposed", operation="create_os_platform", tracks_version=False
    )
    row = engine.get_template("netbox_librenms_plugin/inc/_platform_sync_row.html").render(
        Context(
            {"object": SimpleNamespace(platform=None), "platform_info": {"assessment": assessment}, "csrf_token": "t"},
            use_l10n=False,
        )
    )
    form = engine.get_template("netbox_librenms_plugin/htmx/create_platform_modal.html").render(
        Context(
            {"librenms_os": "junos", "platform_name": "junos", "form_action": "/c/", "device_pk": 1, "csrf_token": "t"},
            use_l10n=False,
        )
    )
    page.set_content(
        "<style>.modal { display: none; } .modal.show { display: block; }</style>"
        f"<table><tbody>{row}</tbody></table>"
        '<div class="modal fade" id="create-platform-modal" tabindex="-1" aria-hidden="true">'
        f'<div class="modal-dialog"><div class="modal-content">{form}</div></div></div>'
    )
    _add_page_scripts(page)

    assert page.evaluate("typeof bootstrap") == "undefined"
    modal = page.locator("#create-platform-modal")
    assert not modal.is_visible()
    page.get_by_role("button", name="Create & Sync").click()
    assert modal.is_visible()
    assert modal.get_attribute("aria-modal") == "true"
    assert modal.locator("input[name=librenms_os]").get_attribute("value") == "junos"

    modal.get_by_role("button", name="Cancel").click()
    assert not modal.is_visible()
    assert page.locator(".modal-backdrop").count() == 0


# ===========================================================================
# Interface selection: the requirement cascade and cross-page state
# ===========================================================================
SELECTION_PAGE_URL = "https://plugin.example.com/device/1/sync"
MODULE_VERIFY_ROUTE = "**/plugins/librenms_plugin/verify-module/"

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
    if row.get("bridge"):
        attrs.append(f'data-bridge-port-id="{esc(row["bridge"])}"')
    if row.get("bridge_name"):
        attrs.append(f'data-bridge-name="{esc(row["bridge_name"])}"')
    companion = (
        f'<select name="device_selection_{esc(row["port_id"])}">'
        '<option value="7">m7</option><option value="9">m9</option></select>'
        if row.get("companion")
        else ""
    )
    hidden_fields = "".join(
        f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'
        for name, value in row.get("hidden_fields", {}).items()
    )
    # A port id is not always usable as a DOM id, so a row can name its own checkbox.
    dom_id = row.get("dom_id", row["port_id"])
    # The table renders an ignored row without a checkbox.
    checkbox = (
        ""
        if row.get("ignored")
        else f'<input type="checkbox" name="select" value="{esc(row["port_id"])}" id="cb-{esc(dom_id)}">'
    )
    if row.get("ignored"):
        attrs.append('data-rule-state="ignored"')
    return (
        f"<tr {' '.join(attrs)}>"
        f'<td data-col="selection">{checkbox}</td>'
        f"<td>{row['name']}{companion}{hidden_fields}</td></tr>"
    )


def _selection_page_html(
    rows,
    *,
    auto_select=True,
    table_id="librenms-interface-table",
    selection_snapshot="",
    blocked_port_ids=(),
):
    checked = "checked" if auto_select else ""
    body = "".join(_selection_row_markup(row) for row in rows)
    snapshot_attr = f' data-selection-snapshot="{escape(selection_snapshot)}"' if selection_snapshot else ""
    blocked_attr = escape(json.dumps(list(blocked_port_ids)))
    return f"""<!doctype html><html><body>
        <input type="checkbox" id="autoSelectLagMembers" {checked}>
        <input type="hidden" name="csrfmiddlewaretoken" value="selection-csrf-token">
        <form id="sync-form" method="post" action="{SELECTION_PAGE_URL}/submit" data-blocked-port-ids="{blocked_attr}">
          <input type="hidden" name="server_key" value="production">
          <table id="{escape(table_id)}"{snapshot_attr}>
            <thead><tr><th><input type="checkbox" class="toggle"></th><th>Name</th></tr></thead>
            <tbody>{body}</tbody>
          </table>
          <button type="submit" id="do-sync">Sync</button>
        </form>
        </body></html>"""


def _load_selection_page(
    page,
    rows,
    *,
    url=SELECTION_PAGE_URL,
    auto_select=True,
    table_id="librenms-interface-table",
    selection_snapshot="",
    blocked_port_ids=(),
):
    """Serve the fixture page from a real origin so sessionStorage behaves as it does in NetBox."""
    html = _selection_page_html(
        rows,
        auto_select=auto_select,
        table_id=table_id,
        selection_snapshot=selection_snapshot,
        blocked_port_ids=blocked_port_ids,
    )
    page.route(
        f"{SELECTION_PAGE_URL}**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto(url)
    _add_page_scripts(page)
    page.evaluate("initializeCheckboxes()")


def _checked_values(page):
    return set(page.evaluate("Array.from(document.querySelectorAll('input[name=select]:checked')).map(cb => cb.value)"))


VERIFY_CELLS = (
    "name",
    "type",
    "speed",
    "mac_address",
    "mtu",
    "enabled",
    "description",
    "vlans",
    "librenms_id",
    "parent",
)


def _checkbox_markup(port_id):
    """The selection cell's checkbox, as the table renders it for a row a sync may write."""
    return f'<input type="checkbox" name="select" value="{escape(port_id)}" id="cb-{escape(port_id)}">'


def _verify_row_markup(row):
    esc = escape
    rule_state = row.get("rule_state", "ignored" if row.get("ignored") else "")
    attrs = [f'data-port-id="{esc(row["port_id"])}"', f'data-rule-state="{rule_state}"']
    for key, data_attr in (("parent", "data-parent-port-id"), ("lag", "data-member-of-lag")):
        if row.get(key):
            attrs.append(f'{data_attr}="{esc(row[key])}"')
    options = "".join(
        f'<option value="{member}"{" selected" if member == row["member"] else ""}>m{member}</option>'
        for member in ("1", "2")
    )
    cells = "".join(f'<td data-col="{col}"></td>' for col in VERIFY_CELLS)
    # The table renders no checkbox on a row the write check refuses; an unresolved owner keeps one.
    checkbox = "" if rule_state in ("ignored", "ambiguous", "incomplete") else _checkbox_markup(row["port_id"])
    return (
        f"<tr {' '.join(attrs)}>"
        f'<td data-col="selection">{checkbox}</td>'
        f'<td><select name="device_selection_{esc(row["port_id"])}" class="vc-member-select"'
        f' data-interface="{esc(row["name"])}">{options}</select></td>{cells}</tr>'
    )


def _serve_verify(page, formatted_rows):
    """Answer the verify endpoint per posted port ID with that row's repaint."""

    def _answer(route):
        port_id = str(json.loads(route.request.post_data)["port_id"])
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "success", "formatted_row": formatted_rows[port_id]}),
        )

    page.route("**/plugins/librenms_plugin/verify-interface/", _answer)


def _verify_form_markup(rows):
    """Render the interface tab's sync form around *rows*, as the server renders it."""
    body = "".join(_verify_row_markup(row) for row in rows)
    blocked = escape(json.dumps([row["port_id"] for row in rows if row.get("ignored") or row.get("rule_state")]))
    return f"""<form id="sync-form" method="post" action="{SELECTION_PAGE_URL}/submit"
              data-interface-origin-device-id="1" data-blocked-port-ids="{blocked}">
          <input type="hidden" name="csrfmiddlewaretoken" value="test-token">
          <input type="hidden" name="server_key" value="production">
          <table id="librenms-interface-table"><tbody>{body}</tbody></table>
          <button type="submit" id="do-sync">Sync</button>
        </form>"""


def _load_verify_page(page, rows, *, url=SELECTION_PAGE_URL, verify=None, bulk=False):
    """Serve a chassis interface tab whose rows the verify endpoint can repaint."""
    bulk_markup = (
        '<select id="bulk-vc-member-select"><option value="2">m2</option></select>'
        '<button type="button" id="apply-bulk-vc-member">Apply</button>'
        if bulk
        else ""
    )
    html = f"""<!doctype html><html><body>
        <input type="checkbox" id="autoSelectLagMembers" checked>
        <input type="radio" name="interface_name_field" value="ifName" checked>
        {bulk_markup}
        <div id="interface-sync-content">{_verify_form_markup(rows)}</div>
        </body></html>"""
    page.route(
        f"{SELECTION_PAGE_URL}**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    if verify is not None:
        _serve_verify(page, verify)
    page.goto(url)
    _add_page_scripts(page)
    if bulk:
        # NetBox wraps the member selects in TomSelect; this stands in for its setValue contract.
        page.evaluate(
            """() => document.querySelectorAll('.vc-member-select').forEach((select) => {
                select.tomselect = {setValue(value, silent) {
                    select.value = value;
                    if (!silent) handleInterfaceChange(select, value);
                }};
            })"""
        )
        page.evaluate("initializeBulkEditApply()")
    page.evaluate("initializeCheckboxes()")


def _verified_row(*, rule_state, selection, lag=None, parent=None):
    """Return the verify endpoint's repaint for one row."""
    row = {col: "" for col in VERIFY_CELLS}
    row.update(
        {
            "selection": selection,
            "rule_state": rule_state,
            "librenms_lag_port_id": lag,
            "librenms_lag_name": None,
            "librenms_parent_port_id": parent,
            "librenms_parent_name": None,
            "librenms_bridge_port_id": None,
            "librenms_bridge_name": None,
        }
    )
    return row


def _wait_for_verified_member(page, port_id, member):
    """Wait until the row's verify has settled on *member*."""
    page.wait_for_function(
        """([portId, member]) => {
            const select = document.querySelector(`tr[data-port-id="${portId}"] .vc-member-select`);
            return select && select._lastVerifiedMember === member;
        }""",
        arg=[port_id, member],
    )


def _verify_row(page, port_id, member, formatted_row):
    """Switch one row to another chassis member and wait for the verify repaint."""
    _serve_verify(page, {port_id: formatted_row})
    page.evaluate(
        """([portId, member]) => {
            const select = document.querySelector(`tr[data-port-id="${portId}"] .vc-member-select`);
            select.value = member;
            handleInterfaceChange(select, member);
        }""",
        [port_id, member],
    )
    _wait_for_verified_member(page, port_id, member)


def _submitted_selects(page):
    """Submit the sync form and return the posted row selections."""
    with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
        page.click("#do-sync")
    return sorted(value for key, value in _selection_form_pairs(request_info.value.post_data) if key == "select")


# A (10) is an aggregate, I (20) is its member, and C (30) is a unit of I.
CHAIN_ROWS = [
    {"port_id": "10", "name": "ae0", "member": "1"},
    {"port_id": "20", "name": "et-0/0/0", "member": "1", "lag": "10"},
    {"port_id": "30", "name": "et-0/0/0.0", "member": "1", "parent": "20"},
]
OTHER_PAGE_ROWS = [{"port_id": "40", "name": "et-0/0/9", "member": "1"}]


class TestTheWalkSkipsEveryBlockedRow:
    """The browser walk reads one blocked set: every row the writer's check refuses."""

    @pytest.mark.parametrize("rule_state", ["ambiguous", "incomplete", "owner_unresolved"])
    def test_a_blocked_middle_row_is_neither_selected_nor_walked_through(self, page, rule_state):
        rows = [dict(row, rule_state=rule_state) if row["port_id"] == "20" else row for row in CHAIN_ROWS]
        _load_verify_page(page, rows)

        page.check("#cb-30")

        assert _checked_values(page) == {"30"}

    def test_an_aggregate_never_pulls_in_a_blocked_member(self, page):
        rows = [
            {"port_id": "10", "name": "ae0", "member": "1"},
            {"port_id": "20", "name": "et-0/0/0", "member": "1", "lag": "10", "rule_state": "owner_unresolved"},
            {"port_id": "30", "name": "et-0/0/1", "member": "1", "lag": "10"},
        ]
        _load_verify_page(page, rows)

        page.check("#cb-10")

        # The unresolved-owner member keeps its checkbox for the bulk member action, yet no cascade selects it.
        assert _checked_values(page) == {"10", "30"}

    def test_a_blocked_aggregate_never_pulls_in_its_members(self, page):
        rows = [
            {"port_id": "10", "name": "ae0", "member": "1", "rule_state": "owner_unresolved"},
            {"port_id": "20", "name": "Gi1/0/1", "member": "1", "lag": "10"},
        ]
        _load_verify_page(page, rows)
        errors = []
        page.on("pageerror", lambda error: errors.append(error))

        page.check("#cb-10")

        # The server refuses the aggregate and expands nothing from it, so the browser starts no cascade from it.
        assert _checked_values(page) == {"10"}
        assert "20" not in _stored_selection(page)

        page.uncheck("#cb-10")

        assert _checked_values(page) == set()
        assert _stored_selection(page) == {}
        assert errors == []

    def test_a_member_pulled_in_before_the_aggregate_was_blocked_is_released(self, page):
        rows = [
            {"port_id": "10", "name": "ae0", "member": "1"},
            {"port_id": "20", "name": "Gi1/0/1", "member": "1", "lag": "10"},
        ]
        _load_verify_page(page, rows)
        page.check("#cb-10")
        _verify_row(page, "10", "2", _verified_row(rule_state="owner_unresolved", selection=_checkbox_markup("10")))
        assert page.evaluate("() => _blockedPortIds().has('10')")

        page.uncheck("#cb-10")

        assert _checked_values(page) == set()
        assert _stored_selection(page) == {}

    def test_a_row_the_verify_makes_ambiguous_joins_the_blocked_set(self, page):
        _load_verify_page(page, CHAIN_ROWS)

        _verify_row(page, "20", "2", _verified_row(rule_state="ambiguous", selection="", lag="10"))
        page.check("#cb-30")

        assert _checked_values(page) == {"30"}


def _store_member_choice(page, port_id, member):
    """Leave a stored selection of one row on another member, as an earlier visit does."""
    page.evaluate(
        """([portId, member]) => writeStoredSelection(
            document.getElementById('librenms-interface-table'),
            {[portId]: {inputs: {[`device_selection_${portId}`]: member}, auto: ''}},
        )""",
        [port_id, member],
    )


def _hold_verify(page):
    """Hold every verify request until the test answers it; return the held routes."""
    held = []

    def _hold(route):
        held.append(route)

    page.route("**/plugins/librenms_plugin/verify-interface/", _hold)
    return held


def _answer(route, formatted_row):
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({"status": "success", "formatted_row": formatted_row}),
    )


def _stored_selection(page):
    """Return the interface table's stored selection."""
    return page.evaluate("() => readStoredSelection(document.getElementById('librenms-interface-table'))")


class TestAMovedMemberIsNotRestored:
    """A stored selection for another member than the page renders is cleared, never verified or restored."""

    ROW = [{"port_id": "10", "name": "et-0/0/0", "member": "1"}]

    def _restore(self, page, member):
        verify_requests = []
        page.on("request", lambda request: "verify-interface" in request.url and verify_requests.append(request))
        _load_verify_page(page, OTHER_PAGE_ROWS)
        _store_member_choice(page, "10", member)
        _load_verify_page(page, self.ROW)
        return verify_requests

    def test_a_differing_member_is_cleared_with_a_notice_and_no_verify(self, page):
        verify_requests = self._restore(page, "2")

        expect(page.locator("#librenms-interface-table-cleared-selections")).to_have_text(
            "1 saved selection(s) on another Virtual Chassis member were cleared; select them again."
        )
        assert verify_requests == []
        assert _checked_values(page) == set()
        assert page.locator("tr[data-port-id='10'] .vc-member-select").input_value() == "1"
        assert "10" not in _stored_selection(page)
        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")
        pairs = _selection_form_pairs(request_info.value.post_data)
        assert ("select", "10") not in pairs
        assert [value for key, value in pairs if key == "device_selection_10"] == ["1"]

    def test_an_equal_member_restores(self, page):
        verify_requests = self._restore(page, "1")

        assert verify_requests == []
        assert _checked_values(page) == {"10"}
        assert page.locator("#librenms-interface-table-cleared-selections").count() == 0

    def test_an_off_page_selection_still_submits_with_its_member(self, page):
        _load_verify_page(page, OTHER_PAGE_ROWS)
        _store_member_choice(page, "10", "2")
        _load_verify_page(page, OTHER_PAGE_ROWS, url=f"{SELECTION_PAGE_URL}?page=2")

        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")
        pairs = _selection_form_pairs(request_info.value.post_data)

        assert ("select", "10") in pairs
        assert ("device_selection_10", "2") in pairs


def test_a_refused_member_check_shows_the_server_reason_and_keeps_the_member(page):
    """The middleware answers a broken rule with a 409 JSON error, and the operator must see its text."""
    reason = 'Interface type mapping "Gi" (ID 7) stores an invalid name_pattern: (. Fix the rule, then try again.'
    _load_verify_page(page, OTHER_PAGE_ROWS)
    page.evaluate("document.body.insertAdjacentHTML('afterbegin', '<div id=\"django-messages\"></div>')")
    page.route(
        "**/plugins/librenms_plugin/verify-interface/",
        lambda route: route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": reason})),
    )

    page.evaluate(
        """() => {
            const select = document.querySelector('tr[data-port-id="40"] .vc-member-select');
            select.value = '2';
            handleInterfaceChange(select, '2');
        }"""
    )

    expect(page.locator("#django-messages .toast-body")).to_have_text(f"{reason} The row keeps its previous member.")
    expect(page.locator('tr[data-port-id="40"] .vc-member-select')).to_have_value("1")


CABLE_VERIFY_URL = "https://plugin.example.com/verify-cable/"


def _cable_member_row_markup(row_id, *, serial_owner=None):
    """Render one VC cable row, as the VC cable table renders it on member 1."""
    esc = escape
    options = '<option value="1" selected>m1</option><option value="2">m2</option>'
    if serial_owner is None:
        member = f'<select name="device_selection_{esc(row_id)}" id="device_selection_{esc(row_id)}">{options}</select>'
    else:
        # A console port row fixes its owner: the dropdown is shown disabled and a hidden field submits the owner.
        member = (
            f'<select id="device_selection_{esc(row_id)}" disabled>{options}</select>'
            f'<input type="hidden" name="device_selection_{esc(row_id)}" value="{esc(serial_owner)}">'
        )
    return (
        f'<tr data-interface="{esc(row_id)}" id="{esc(row_id)}">'
        f'<td data-col="selection"><input type="checkbox" name="select" value="{esc(row_id)}"'
        f' id="cb-{esc(row_id)}"></td>'
        f'<td data-col="device_selection">{member}</td>'
        '<td data-col="local_port">Ethernet1</td><td data-col="actions"></td></tr>'
    )


def _load_cable_member_page(page, row_ids, *, serial_owner=None, url=SELECTION_PAGE_URL):
    """Serve a chassis cable tab whose rows the cable verify endpoint could repaint."""
    body = "".join(_cable_member_row_markup(row_id, serial_owner=serial_owner) for row_id in row_ids)
    html = f"""<!doctype html><html><body>
        <form id="sync-form" method="post" action="{SELECTION_PAGE_URL}/submit">
          <input type="hidden" name="csrfmiddlewaretoken" value="test-token">
          <input type="hidden" name="server_key" value="production">
          <div data-cable-verify-url="{CABLE_VERIFY_URL}" data-cable-origin-device-id="1">
            <table id="librenms-cable-table-vc"><tbody>{body}</tbody></table>
          </div>
          <button type="submit" id="do-sync">Sync</button>
        </form>
        </body></html>"""
    page.route(
        f"{SELECTION_PAGE_URL}**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto(url)
    _add_page_scripts(page)
    page.evaluate("initializeCheckboxes()")


def _store_cable_member_choice(page, row_id, member):
    """Leave a stored cable selection of one row on another member, as an earlier visit does."""
    page.evaluate(
        """([rowId, member]) => writeStoredSelection(
            document.getElementById('librenms-cable-table-vc'),
            {[rowId]: {inputs: {[`device_selection_${rowId}`]: member}, auto: ''}},
        )""",
        [row_id, member],
    )


class TestAMovedCableMemberIsNotRestored:
    """The cable table clears a stored selection for another member by the same rule as the interface table."""

    def _restore(self, page, member, *, serial_owner=None):
        verify_requests = []
        page.on("request", lambda request: CABLE_VERIFY_URL in request.url and verify_requests.append(request))
        _load_cable_member_page(page, ["other-row"])
        _store_cable_member_choice(page, "cable-10", member)
        _load_cable_member_page(page, ["cable-10"], serial_owner=serial_owner)
        return verify_requests

    def _stored(self, page):
        return page.evaluate("() => readStoredSelection(document.getElementById('librenms-cable-table-vc'))")

    def test_a_differing_member_is_cleared_with_a_notice_and_no_verify(self, page):
        verify_requests = self._restore(page, "2")

        expect(page.locator("#librenms-cable-table-vc-cleared-selections")).to_have_text(
            "1 saved selection(s) on another Virtual Chassis member were cleared; select them again."
        )
        assert verify_requests == []
        assert _checked_values(page) == set()
        assert page.locator("#device_selection_cable-10").input_value() == "1"
        assert "cable-10" not in self._stored(page)
        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")
        pairs = _selection_form_pairs(request_info.value.post_data)
        assert ("select", "cable-10") not in pairs
        assert [value for key, value in pairs if key == "device_selection_cable-10"] == ["1"]

    def test_a_differing_fixed_owner_is_cleared_and_keeps_the_rendered_owner(self, page):
        verify_requests = self._restore(page, "2", serial_owner="1")

        expect(page.locator("#librenms-cable-table-vc-cleared-selections")).to_have_count(1)
        assert verify_requests == []
        assert _checked_values(page) == set()
        assert page.locator('input[type="hidden"][name="device_selection_cable-10"]').input_value() == "1"

    def test_an_equal_member_restores(self, page):
        verify_requests = self._restore(page, "1")

        assert verify_requests == []
        assert _checked_values(page) == {"cable-10"}
        assert page.locator("#librenms-cable-table-vc-cleared-selections").count() == 0

    def test_an_off_page_selection_still_submits_with_its_member(self, page):
        _load_cable_member_page(page, ["other-row"])
        _store_cable_member_choice(page, "cable-10", "2")
        _load_cable_member_page(page, ["other-row"], url=f"{SELECTION_PAGE_URL}?page=2")

        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")
        pairs = _selection_form_pairs(request_info.value.post_data)

        assert ("select", "cable-10") in pairs
        assert ("device_selection_cable-10", "2") in pairs


class TestTheCableVerifyAnswerSetsTheCheckbox:
    """After a member change, the cable row's checkbox follows can_create_cable of the verify answer."""

    @pytest.mark.parametrize("can_create", [True, False], ids=["writable", "not-writable"])
    def test_the_checkbox_follows_the_answer(self, page, can_create):
        _load_cable_member_page(page, ["cable-10"])
        formatted_row = {
            "local_port": "Ethernet1",
            "remote_port": "",
            "remote_device": "",
            "cable_status": "No Cable",
            "actions": "",
            "can_create_cable": can_create,
        }
        page.route(
            f"{CABLE_VERIFY_URL}**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "success", "formatted_row": formatted_row}),
            ),
        )
        with page.expect_response(f"{CABLE_VERIFY_URL}**"):
            page.evaluate(
                """() => {
                    const select = document.getElementById('device_selection_cable-10');
                    select.value = '2';
                    handleCableChange(select, '2');
                }"""
            )

        checkbox = page.locator("#cb-cable-10")
        if can_create:
            expect(checkbox).to_be_enabled()
        else:
            expect(checkbox).to_be_disabled()


class TestADetachedVerifyChangesNothing:
    """A verify answer for a row the tab swap replaced must not touch the new page or storage."""

    def test_an_old_answer_after_a_tab_swap_changes_neither_storage_nor_the_blocked_set(self, page, fetches):
        rows = [
            {"port_id": "10", "name": "et-0/0/0", "member": "1"},
            {"port_id": "20", "name": "et-0/0/1", "member": "1"},
        ]
        _load_verify_page(page, rows)
        fetches.install()
        page.check("#cb-20")
        held = _hold_verify(page)
        page.evaluate(
            """() => {
                const select = document.querySelector('tr[data-port-id="10"] .vc-member-select');
                select.value = '2';
                handleInterfaceChange(select, '2');
            }"""
        )
        expect(page.locator("tr[data-port-id='10'] .vc-member-select")).to_have_value("2")

        # The sync swaps the tab in, and the user then clears the row the swap restored.
        page.evaluate(
            """(markup) => {
                document.getElementById('interface-sync-content').innerHTML = markup;
                initializeCheckboxes();
            }""",
            _verify_form_markup(rows),
        )
        page.uncheck("#cb-20")
        _answer(held[0], _verified_row(rule_state="ignored", selection=""))
        fetches.wait_until_settled()

        assert _stored_selection(page) == {}
        assert page.evaluate("() => document.querySelector('[data-blocked-port-ids]').dataset.blockedPortIds") == "[]"


class TestTheOrderedSelectionCommit:
    """A member, eligibility or checkbox change ends in one order: eligibility, closure, then storage."""

    def test_a_row_the_verify_ignores_releases_its_chain_in_storage_too(self, page):
        _load_verify_page(page, CHAIN_ROWS)
        page.check("#cb-30")
        assert _checked_values(page) == {"10", "20", "30"}

        _verify_row(page, "20", "2", _verified_row(rule_state="ignored", selection="", lag="10"))
        assert _checked_values(page) == {"30"}
        _load_verify_page(page, OTHER_PAGE_ROWS, url=f"{SELECTION_PAGE_URL}?page=2")

        # A was held only for I, so once I is ignored the stored selection must not submit it.
        assert _submitted_selects(page) == ["30"]

    def test_a_member_switch_keeps_the_row_a_requirement(self, page):
        rows = [{"port_id": "10", "name": "et-0/0/0", "member": "1"}, CHAIN_ROWS[2] | {"parent": "10"}]
        _load_verify_page(page, rows)
        page.check("#cb-30")
        assert _checked_values(page) == {"10", "30"}

        _verify_row(page, "10", "2", _verified_row(rule_state="", selection=_checkbox_markup("10")))
        page.uncheck("#cb-30")

        # The row was held for C, so it is released with C, not kept as a choice of the user's.
        assert _checked_values(page) == set()

    def test_the_bulk_member_apply_commits_every_verified_row(self, page):
        verify = {
            "10": _verified_row(rule_state="", selection=_checkbox_markup("10")),
            "20": _verified_row(rule_state="ignored", selection="", lag="10"),
            "30": _verified_row(rule_state="", selection=_checkbox_markup("30"), parent="20"),
        }
        _load_verify_page(page, CHAIN_ROWS, verify=verify, bulk=True)
        page.check("#cb-30")

        page.click("#apply-bulk-vc-member")
        for port_id in ("10", "30"):
            _wait_for_verified_member(page, port_id, "2")
        expect(page.locator("tr[data-port-id='20']")).to_have_attribute("data-rule-state", "ignored")
        _load_verify_page(page, OTHER_PAGE_ROWS, url=f"{SELECTION_PAGE_URL}?page=2")

        assert _submitted_selects(page) == ["30"]


class TestRequirementCascade:
    """Selecting a row must select everything that row depends on, all the way up."""

    def test_sub_interface_pulls_in_its_parent_and_that_parent_s_aggregate(self, page):
        _load_selection_page(page, JUNOS_ROWS)

        page.check("#cb-4302")

        # et-0/0/6.0 needs et-0/0/6, which needs ae2. Stopping at the parent leaves the
        # aggregate unsynced, and the LAG assignment is then silently dropped.
        assert _checked_values(page) == {"4302", "4301", "4303"}

    def test_an_ignored_parent_is_neither_selected_nor_walked_through(self, page):
        rows = [dict(row, ignored=True) if row["port_id"] == "4301" else row for row in JUNOS_ROWS]
        _load_selection_page(page, rows, blocked_port_ids=["4301"])

        page.check("#cb-4302")

        # et-0/0/6 is ignored, so neither it nor ae2 behind it is selected.
        assert _checked_values(page) == {"4302"}

    def test_an_ignored_parent_on_another_page_raises_no_notice(self, page):
        _load_selection_page(page, [JUNOS_ROWS[3]], blocked_port_ids=["4301"])

        page.check("#cb-4302")

        assert _checked_values(page) == {"4302"}
        assert page.locator("#parent-cross-page-notices").count() == 0

    def test_a_row_the_verify_makes_ignored_leaves_the_selection_and_its_member_choice(self, page):
        _load_verify_page(page, [{"port_id": "10", "name": "Vlan10", "member": "1"}])
        page.check("#cb-10")

        _verify_row(page, "10", "2", _verified_row(rule_state="ignored", selection=""))
        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")
        pairs = _selection_form_pairs(request_info.value.post_data)

        # The ignored row is not submitted, and member 1 never follows member 2's visible choice.
        assert ("select", "10") not in pairs
        assert [value for key, value in pairs if key == "device_selection_10"] == ["2"]

    def test_a_parent_the_verify_makes_ignored_is_not_walked_through(self, page):
        # C (30) is a unit of I (20), and I is a member of A (10).
        rows = [
            {"port_id": "10", "name": "ae0", "member": "1"},
            {"port_id": "20", "name": "et-0/0/0", "member": "1", "lag": "10"},
            {"port_id": "30", "name": "et-0/0/0.0", "member": "1", "parent": "20"},
        ]
        _load_verify_page(page, rows)

        _verify_row(page, "20", "2", _verified_row(rule_state="ignored", selection="", lag="10"))
        page.check("#cb-30")

        assert _checked_values(page) == {"30"}

    def test_aggregate_unit_pulls_in_its_aggregate(self, page):
        _load_selection_page(page, JUNOS_ROWS)

        page.check("#cb-4304")

        assert _checked_values(page) == {"4304", "4303"}

    def test_bridge_member_pulls_in_its_bridge(self, page):
        rows = [
            {"port_id": "100", "name": "vmbr0"},
            {"port_id": "103", "name": "nic0", "bridge": "100"},
        ]
        _load_selection_page(page, rows)

        page.check("#cb-103")

        assert _checked_values(page) == {"100", "103"}

    def test_member_verification_replaces_relationship_metadata(self, page):
        """Use the selected VC member's dependencies after its row is verified."""
        html = """<!doctype html><html><body>
            <input name="csrfmiddlewaretoken" value="test-token">
            <input name="server_key" value="stub">
            <input type="radio" name="interface_name_field" value="ifName" checked>
            <input type="checkbox" id="autoSelectLagMembers" checked>
            <table id="librenms-interface-table" data-interface-origin-device-id="1"><tbody>
              <tr data-port-id="10" data-parent-port-id="20" data-parent-name="old-parent"
                  data-member-of-lag="30" data-lag-name="old-lag"
                  data-bridge-port-id="40" data-bridge-name="old-bridge">
                <td><input type="checkbox" name="select" value="10" checked></td>
                <td><select class="vc-member-select" data-interface="Ethernet1.100">
                  <option value="1" selected>member-1</option><option value="2">member-2</option>
                </select></td>
                <td data-col="name"></td><td data-col="type"></td><td data-col="speed"></td>
                <td data-col="mac_address"></td><td data-col="mtu"></td><td data-col="enabled"></td>
                <td data-col="description"></td><td data-col="vlans"></td>
                <td data-col="librenms_id"></td><td data-col="parent"></td>
              </tr>
              <tr data-port-id="20"><td><input type="checkbox" name="select" value="20"></td></tr>
              <tr data-port-id="21"><td><input type="checkbox" name="select" value="21"></td></tr>
              <tr data-port-id="30"><td><input type="checkbox" name="select" value="30"></td></tr>
              <tr data-port-id="40"><td><input type="checkbox" name="select" value="40"></td></tr>
              <tr data-port-id="41"><td><input type="checkbox" name="select" value="41"></td></tr>
            </tbody></table>
        </body></html>"""
        page.set_content(html)
        page.route(
            "**/plugins/librenms_plugin/verify-interface/",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "status": "success",
                        "formatted_row": {
                            "name": "Ethernet1.100",
                            "type": "virtual",
                            "speed": "1 Gbps",
                            "mac_address": "",
                            "mtu": "1500",
                            "enabled": "yes",
                            "description": "",
                            "vlans": "",
                            "librenms_id": "10",
                            "parent": "relationships",
                            "librenms_parent_port_id": 21,
                            "librenms_parent_name": "new-parent",
                            "librenms_lag_port_id": None,
                            "librenms_lag_name": None,
                            "librenms_bridge_port_id": 41,
                            "librenms_bridge_name": "new-bridge",
                        },
                    }
                ),
            ),
        )
        _add_page_scripts(page)
        page.evaluate("initializeCheckboxes()")
        page.evaluate("refreshRequiredSelections()")

        assert _checked_values(page) == {"10", "20", "30", "40"}

        with page.expect_response("**/plugins/librenms_plugin/verify-interface/"):
            page.evaluate(
                """() => {
                    const select = document.querySelector('.vc-member-select');
                    handleInterfaceChange(select, '2');
                }"""
            )

        row = page.locator("tr[data-port-id='10']")
        # The response event occurs before response.json() updates the row.
        expect(row).to_have_attribute("data-bridge-name", "new-bridge")
        relationship_data = row.evaluate(
            """row => ({
                parentPortId: row.dataset.parentPortId,
                parentName: row.dataset.parentName,
                memberOfLag: row.dataset.memberOfLag,
                lagName: row.dataset.lagName,
                bridgePortId: row.dataset.bridgePortId,
                bridgeName: row.dataset.bridgeName,
            })"""
        )
        assert relationship_data == {
            "parentPortId": "21",
            "parentName": "new-parent",
            "memberOfLag": "",
            "lagName": "",
            "bridgePortId": "41",
            "bridgeName": "new-bridge",
        }
        assert _checked_values(page) == {"10", "21", "41"}

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

    def test_a_bridge_on_another_page_is_named_as_a_bridge(self, page):
        rows = [{"port_id": "103", "name": "nic0", "bridge": "100", "bridge_name": "vmbr0"}]
        _load_selection_page(page, rows)

        page.check("#cb-103")

        notice = page.locator("#parent-cross-page-notices").inner_text()
        assert "Bridge interface" in notice
        assert "vmbr0" in notice

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

    def test_a_shift_range_pulls_in_the_members_of_an_aggregate_it_covers(self, page):
        # ae2 sits inside the range and its member et-0/0/6 outside it; a click on ae2 pulls
        # that member in, so the range has to as well.
        rows = [
            {"port_id": "4001", "name": "et-0/0/1"},
            JUNOS_ROWS[0],
            {"port_id": "4002", "name": "et-0/0/2"},
            JUNOS_ROWS[2],
        ]
        _load_selection_page(page, rows)
        page.click("#cb-4001")

        page.click("#cb-4002", modifiers=["Shift"])

        assert _checked_values(page) == {"4001", "4303", "4002", "4301"}

        page.uncheck("#autoSelectLagMembers")

        # The ranged rows are the user's own, so turning the cascade off gives back exactly the
        # member the range pulled in.
        assert _checked_values(page) == {"4001", "4303", "4002"}

    def test_a_shift_range_that_clears_an_aggregate_releases_its_members(self, page):
        # A click that clears ae2 gives et-0/0/6 back and keeps ae2 cleared, so a range must too.
        rows = [
            {"port_id": "4001", "name": "et-0/0/1"},
            JUNOS_ROWS[0],
            {"port_id": "4002", "name": "et-0/0/2"},
            JUNOS_ROWS[2],
            JUNOS_ROWS[1],
        ]
        _load_selection_page(page, rows)
        page.check("#cb-4303")
        page.check("#cb-4304")
        # Anchor on a row the user just cleared, so the range clears everything it covers.
        page.click("#cb-4001")
        page.click("#cb-4001")

        page.click("#cb-4002", modifiers=["Shift"])

        # et-0/0/6 is released with its aggregate, and ae2 stays cleared although ae2.0 requires it.
        assert _checked_values(page) == {"4304"}

    def test_a_shift_range_across_a_selected_aggregate_keeps_a_cleared_member_cleared(self, page):
        # ae2 does not change state here, so crossing it must not undo the user's clear.
        rows = [
            {"port_id": "4001", "name": "et-0/0/1"},
            JUNOS_ROWS[0],
            {"port_id": "4002", "name": "et-0/0/2"},
            JUNOS_ROWS[2],
        ]
        _load_selection_page(page, rows)
        page.check("#cb-4303")
        page.uncheck("#cb-4301")
        page.click("#cb-4001")

        page.click("#cb-4002", modifiers=["Shift"])

        assert _checked_values(page) == {"4001", "4303", "4002"}


class TestCrossPageSelection:
    """A selection must survive paging, and reach the server when it is submitted."""

    def test_selection_is_restored_when_the_row_comes_back(self, page):
        _load_selection_page(page, JUNOS_ROWS, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-4304")

        page.goto(f"{SELECTION_PAGE_URL}?page=2")
        _add_page_scripts(page)
        page.evaluate("initializeCheckboxes()")

        assert "4304" in _checked_values(page)

    def test_rows_selected_on_another_page_are_counted_on_this_one(self, page):
        _load_selection_page(page, JUNOS_ROWS, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-4304")

        # Page 2 renders a different slice, so the ae2.0 row is not in the DOM any more.
        _load_selection_page(page, [JUNOS_ROWS[2]], url=f"{SELECTION_PAGE_URL}?page=2")

        notice = page.locator("#librenms-interface-table-offpage-selection")
        assert notice.inner_text().split()[0] == "2"

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

    def test_module_selection_is_discarded_when_the_inventory_snapshot_changes(self, page):
        """Discard an off-page selection when a module inventory snapshot changes."""
        options = {"table_id": "librenms-module-table", "auto_select": False}
        _load_selection_page(
            page,
            [JUNOS_ROWS[0]],
            url=f"{SELECTION_PAGE_URL}?page=1",
            selection_snapshot="snapshot-a",
            **options,
        )
        page.check("#cb-4303")

        _load_selection_page(
            page,
            [JUNOS_ROWS[2]],
            url=f"{SELECTION_PAGE_URL}?page=2",
            selection_snapshot="snapshot-b",
            **options,
        )

        assert page.locator("#librenms-module-table-offpage-selection").count() == 0
        assert page.evaluate("Object.keys(readStoredSelection(document.querySelector('table'))).length") == 0

    def test_verified_cable_fields_travel_with_their_off_page_row(self, page):
        expected_fields = {
            "expected_local_id_4303": "7401",
            "expected_local_device_id_4303": "6401",
            "expected_remote_id_4303": "7402",
            "expected_remote_device_id_4303": "6402",
        }
        rows = [dict(JUNOS_ROWS[0], hidden_fields=expected_fields), JUNOS_ROWS[2]]
        _load_selection_page(page, rows, url=f"{SELECTION_PAGE_URL}?page=1")
        page.check("#cb-4303")

        _load_selection_page(page, [JUNOS_ROWS[1]], url=f"{SELECTION_PAGE_URL}?page=2")
        with page.expect_request(f"{SELECTION_PAGE_URL}/submit") as request_info:
            page.click("#do-sync")

        submitted = dict(_selection_form_pairs(request_info.value.post_data))
        assert {name: submitted.get(name) for name in expected_fields} == expected_fields

    def test_a_restored_module_member_is_verified_again(self, page):
        # The module table verifies a restored member again; the member-verified tables clear it instead.
        page.route(MODULE_VERIFY_ROUTE, lambda route: None)
        rows = [dict(JUNOS_ROWS[0], companion=True), JUNOS_ROWS[2]]
        table = "librenms-module-table"
        _load_selection_page(page, rows, url=f"{SELECTION_PAGE_URL}?page=1", table_id=table)
        page.select_option('[name="device_selection_4303"]', "9")
        page.check("#cb-4303")

        _load_selection_page(page, [JUNOS_ROWS[1]], url=f"{SELECTION_PAGE_URL}?page=2", table_id=table)
        with page.expect_request(MODULE_VERIFY_ROUTE) as verify:
            _load_selection_page(page, rows, url=f"{SELECTION_PAGE_URL}?page=1", table_id=table)

        assert json.loads(verify.value.post_data)["device_id"] == "9"
        assert "4303" in _checked_values(page)
        assert page.locator('[name="device_selection_4303"]').input_value() == "9"

    def test_restoring_a_selection_does_not_restore_stale_action_fields(self, page):
        old_row = dict(
            JUNOS_ROWS[0],
            companion=True,
            hidden_fields={
                "csrfmiddlewaretoken": "old-csrf",
                "module_id": "41",
                "ent_index": "4303",
                "inventory_binding": "old-binding",
            },
        )
        table = "librenms-module-table"
        # The restored member is verified again; the request stays open so the row keeps what the restore set.
        page.route(MODULE_VERIFY_ROUTE, lambda route: None)
        _load_selection_page(page, [old_row, JUNOS_ROWS[2]], url=f"{SELECTION_PAGE_URL}?page=1", table_id=table)
        page.select_option('[name="device_selection_4303"]', "9")
        page.check("#cb-4303")

        _load_selection_page(page, [JUNOS_ROWS[1]], url=f"{SELECTION_PAGE_URL}?page=2", table_id=table)
        new_row = dict(
            old_row,
            hidden_fields={
                "csrfmiddlewaretoken": "new-csrf",
                "module_id": "42",
                "ent_index": "5303",
                "inventory_binding": "new-binding",
            },
        )
        _load_selection_page(page, [new_row, JUNOS_ROWS[2]], url=f"{SELECTION_PAGE_URL}?page=1", table_id=table)

        assert "4303" in _checked_values(page)
        assert page.locator('[name="device_selection_4303"]').input_value() == "9"
        row = page.locator('tr[data-port-id="4303"]')
        for name, expected in new_row["hidden_fields"].items():
            assert row.locator(f'[name="{name}"]').input_value() == expected


def test_vlan_row_submit_keeps_another_page_selection_and_group(page):
    """A per-row VLAN action must not consume the bulk selection stored on another page."""
    page_one = f"""
        <input name="server_key" value="production">
        <form id="sync-form" method="post" action="{SELECTION_PAGE_URL}/submit">
          <table id="librenms-vlan-table"><tbody><tr>
            <td><input id="vlan-101" type="checkbox" name="select" value="101"></td>
            <td><input type="hidden" name="expected_vlan_name_101" value="users">
              <select name="vlan_group_101">
              <option value="1">Group A</option><option value="2">Group B</option>
            </select></td>
          </tr></tbody></table>
        </form>
    """
    page_two = f"""
        <input name="server_key" value="production">
        <form id="sync-form" method="post" action="{SELECTION_PAGE_URL}/submit">
          <table id="librenms-vlan-table"><tbody><tr>
            <td><input type="checkbox" name="select" value="201"></td>
            <td><button id="sync-one" type="submit" name="sync_one" value="201">Create</button></td>
          </tr></tbody></table>
        </form>
    """

    def serve_page(route):
        body = page_two if "page=2" in route.request.url else page_one
        route.fulfill(status=200, content_type="text/html", body=body)

    page.route(f"{SELECTION_PAGE_URL}**", serve_page)
    page.goto(f"{SELECTION_PAGE_URL}?page=1")
    _add_page_scripts(page)
    page.select_option('[name="vlan_group_101"]', "2")
    page.check("#vlan-101")

    page.goto(f"{SELECTION_PAGE_URL}?page=2")
    _add_page_scripts(page)
    page.locator("#sync-form").evaluate("form => form.addEventListener('submit', event => event.preventDefault())")
    page.click("#sync-one")

    posted = page.locator("#sync-form").evaluate(
        """form => Array.from(new FormData(form, document.getElementById('sync-one')).entries())"""
    )
    stored = page.evaluate("readStoredSelection(document.getElementById('librenms-vlan-table'))")
    assert [pair for pair in posted if pair[0] == "sync_one"] == [["sync_one", "201"]]
    assert [pair for pair in posted if pair == ["select", "101"]] == []
    assert stored["101"]["inputs"]["vlan_group_101"] == "2"
    assert stored["101"]["inputs"]["expected_vlan_name_101"] == "users"


@pytest.mark.parametrize(
    ("table_id", "field", "selected_value", "shown_value"),
    [
        # A VLAN name rule changed the name.
        ("librenms-vlan-table", "expected_vlan_name_300", "web+300", "web"),
        # The far end of the cable now resolves to another NetBox interface.
        ("librenms-cable-table", "expected_remote_id_300", "100", "200"),
        ("librenms-cable-table-vc", "expected_remote_id_300", "100", "200"),
    ],
)
def test_a_restored_row_posts_the_value_it_shows_now(page, table_id, field, selected_value, shown_value):
    """A restored selection must post the value the row shows now, not the value it showed when it was selected."""

    def sync_page(value):
        return f"""
        <input name="server_key" value="production">
        <form id="sync-form" method="post" action="{SELECTION_PAGE_URL}/submit">
          <table id="{table_id}"><tbody><tr data-interface="300">
            <td><input id="row-300" type="checkbox" name="select" value="300"></td>
            <td><select name="device_selection_300"><option value="1" selected>m1</option></select>
              <input type="hidden" name="{field}" value="{value}"></td>
          </tr></tbody></table>
        </form>
        """

    shown = {"value": selected_value}
    page.route(
        f"{SELECTION_PAGE_URL}**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=sync_page(shown["value"])),
    )
    page.goto(SELECTION_PAGE_URL)
    _add_page_scripts(page)
    page.check("#row-300")

    shown["value"] = shown_value
    page.goto(SELECTION_PAGE_URL)
    _add_page_scripts(page)
    page.evaluate(f"restoreTableSelection(document.getElementById('{table_id}'))")

    assert page.locator("#row-300").is_checked()
    assert page.locator(f'[name="{field}"]').input_value() == shown_value
    stored = page.evaluate(f"readStoredSelection(document.getElementById('{table_id}'))")
    assert stored["300"]["inputs"][field] == shown_value


# Tabler's modal layout: the open modal covers the viewport, so a click beside the dialog hits the modal itself.
MODAL_CSS = (
    "<style>.modal { display: none; position: fixed; inset: 0; z-index: 1055; } .modal.show { display: block; }"
    " .modal-dialog { width: 400px; margin: 100px auto; background: #fff; }"
    " .modal-backdrop { position: fixed; inset: 0; z-index: 1050; background: rgba(0, 0, 0, .5); }</style>"
)
CONFIRM_MODAL = "#librenms-confirm-modal"


def _spy_on_window_confirm(page):
    """Record every window.confirm call; the plugin must ask through its own modal instead."""
    page.evaluate(
        "() => { window.confirmCalls = []; window.confirm = (m) => { window.confirmCalls.push(m); return true; }; }"
    )


def _rebind_form_page(page, message="Move the binding?"):
    page.set_content(
        MODAL_CSS
        + f"""
        <form id="sync-form" method="post" action="/sync">
          <table id="librenms-interface-table"><tbody><tr>
            <td><input type="checkbox" name="select" value="201"></td>
            <td><input type="hidden" name="rebind_expected_port_201" value="8679">
                <button id="rebind" type="submit" name="rebind_one" value="201"
                formaction="/rebind?interface_name_field=ifName" data-confirm="{escape(message)}">Rebind</button></td>
          </tr></tbody></table>
        </form>
        """
    )
    _add_page_scripts(page)
    _spy_on_window_confirm(page)
    page.evaluate(
        """() => {
            writeStoredSelection(document.querySelector('table'), {'101': {inputs: {}, auto: ''}});
            window.submitted = [];
            document.getElementById('sync-form').addEventListener('submit', event => {
                event.preventDefault();
                window.submitted.push([event.submitter.formAction, Array.from(new FormData(event.target, event.submitter).entries())]);
            });
        }"""
    )


@pytest.mark.parametrize("dismiss", ["cancel", "escape", "backdrop", "close"])
def test_rebind_asks_in_the_plugin_modal_and_a_dismissal_submits_nothing(page, dismiss):
    _rebind_form_page(page)

    page.click("#rebind")

    modal = page.locator(CONFIRM_MODAL)
    expect(modal).to_be_visible()
    expect(modal.locator("[data-confirm-message]")).to_have_text("Move the binding?")
    if dismiss == "cancel":
        modal.get_by_role("button", name="Cancel").click()
    elif dismiss == "escape":
        page.keyboard.press("Escape")
    elif dismiss == "close":
        modal.get_by_role("button", name="Close").click()
    else:
        page.mouse.click(5, 5)
    expect(modal).to_be_hidden()
    assert page.evaluate("window.submitted") == []
    assert page.evaluate("window.confirmCalls") == []
    assert list(page.evaluate("readStoredSelection(document.querySelector('table'))")) == ["101"]


def test_rebind_confirmed_in_the_modal_submits_once_with_its_submitter(page):
    """Rebind submits only after the confirmation, to its own action, and leaves the bulk selection alone."""
    _rebind_form_page(page)

    page.click("#rebind")
    page.locator(CONFIRM_MODAL).get_by_role("button", name="Confirm").click()

    expect(page.locator(CONFIRM_MODAL)).to_be_hidden()
    [(action, pairs)] = page.evaluate("window.submitted")
    assert action.endswith("/rebind?interface_name_field=ifName")
    assert ["rebind_one", "201"] in pairs
    assert ["rebind_expected_port_201", "8679"] in pairs
    assert ["select", "101"] not in pairs
    assert page.evaluate("window.confirmCalls") == []
    assert list(page.evaluate("readStoredSelection(document.querySelector('table'))")) == ["101"]


def test_the_confirm_modal_shows_markup_in_the_message_as_text(page):
    _rebind_form_page(page, message="Create NetBox VRF '<b>red</b>'?")

    page.click("#rebind")

    message = page.locator(f"{CONFIRM_MODAL} [data-confirm-message]")
    expect(message).to_have_text("Create NetBox VRF '<b>red</b>'?")
    assert message.locator("b").count() == 0


def test_a_confirmed_htmx_row_action_posts_its_submitter_and_formaction(page):
    """An htmx form reads the submitter it last saw, so the resubmit must keep the clicked button."""
    requests = []

    def answer(route):
        requests.append((route.request.url, route.request.post_data))
        route.fulfill(status=200, content_type="text/html", body='<span id="swapped">Done</span>')

    page.route(f"{HTMX_SYNC_URL}**", answer)
    page.set_content(
        MODAL_CSS
        + f"""
        <div id="tab-content">
          <form id="sync-form" method="post" action="{HTMX_SYNC_URL}" hx-post="{HTMX_SYNC_URL}"
                hx-target="#tab-content" hx-swap="innerHTML">
            <table id="librenms-ipaddress-table"><tbody><tr>
              <td><button id="create-vrf" type="submit" name="create_vrf" value="row-7"
                   formaction="{HTMX_SYNC_URL}/vrf" data-confirm="Create NetBox VRF 'red'?">VRF</button></td>
            </tr></tbody></table>
          </form>
        </div>
        """
    )
    _add_page_scripts(page)
    _spy_on_window_confirm(page)

    page.click("#create-vrf")
    expect(page.locator(f"{CONFIRM_MODAL} [data-confirm-message]")).to_have_text("Create NetBox VRF 'red'?")
    assert requests == []
    page.locator(CONFIRM_MODAL).get_by_role("button", name="Confirm").click()
    page.locator("#swapped").wait_for()

    [(url, body)] = requests
    assert url == f"{HTMX_SYNC_URL}/vrf"
    assert "create_vrf=row-7" in body
    assert page.evaluate("window.confirmCalls") == []


@pytest.mark.parametrize("accept", [False, True], ids=["cancelled", "confirmed"])
def test_an_hx_confirm_question_asks_in_the_plugin_modal(page, accept):
    requests = []

    def answer(route):
        requests.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body="")

    page.route(f"{HTMX_SYNC_URL}**", answer)
    page.set_content(
        MODAL_CSS
        + f"""
        <button id="transfer" type="button" hx-post="{HTMX_SYNC_URL}/transfer" hx-swap="none"
                hx-confirm="Transfer primary IPv4 (192.0.2.1/24) to <b>winner</b>?">Transfer</button>
        """
    )
    _add_page_scripts(page)
    _spy_on_window_confirm(page)

    page.click("#transfer")
    modal = page.locator(CONFIRM_MODAL)
    expect(modal.locator("[data-confirm-message]")).to_have_text(
        "Transfer primary IPv4 (192.0.2.1/24) to <b>winner</b>?"
    )
    if accept:
        with page.expect_response(f"{HTMX_SYNC_URL}/transfer"):
            modal.get_by_role("button", name="Confirm").click()
    else:
        modal.get_by_role("button", name="Cancel").click()
    expect(modal).to_be_hidden()

    assert requests == ([f"{HTMX_SYNC_URL}/transfer"] if accept else [])
    assert page.evaluate("window.confirmCalls") == []


def _render_remove_mapping_form(page, server_key):
    """Render the sync page's real remove-mapping form, cut out of its template, with the page script."""
    from django.template import Context, Engine, Library

    urls = Library()

    @urls.simple_tag(name="url")
    def url(name, **kwargs):
        return "/remove-mapping/"

    engine = Engine()
    engine.template_builtins.append(urls)
    source = (TEMPLATE_DIR / "librenms_sync_base.html").read_text(encoding="utf-8")
    form = re.search(r"<form[^>]*remove_server_mapping.*?</form>", source, flags=re.S).group(0)
    fragment = engine.from_string(form).render(
        Context(
            {
                "mapping": {"server_key": server_key},
                "server_key": "production",
                "lookup_device_pk": 7,
                "lookup_device_model_name": "device",
                "csrf_token": "token",
            },
            use_l10n=False,
        )
    )
    page.set_content(MODAL_CSS + fragment)
    _add_page_scripts(page)
    _spy_on_window_confirm(page)
    page.evaluate(
        """() => {
            window.submitted = [];
            document.querySelector('form').addEventListener('submit', event => {
                event.preventDefault();
                window.submitted.push(Array.from(new FormData(event.target, event.submitter).entries()));
            });
        }"""
    )


@pytest.mark.parametrize("dismiss", ["cancel", "escape", "backdrop"])
def test_remove_mapping_asks_in_the_plugin_modal_and_a_dismissal_submits_nothing(page, dismiss):
    _render_remove_mapping_form(page, "old-lab")

    page.get_by_role("button", name="Remove").click()

    modal = page.locator(CONFIRM_MODAL)
    expect(modal.locator("[data-confirm-message]")).to_have_text(
        "Remove mapping for server 'old-lab'? This cannot be undone."
    )
    if dismiss == "cancel":
        modal.get_by_role("button", name="Cancel").click()
    elif dismiss == "escape":
        page.keyboard.press("Escape")
    else:
        page.mouse.click(5, 5)
    expect(modal).to_be_hidden()
    assert page.evaluate("window.submitted") == []
    assert page.evaluate("window.confirmCalls") == []


def test_remove_mapping_confirmed_in_the_modal_submits_once_and_shows_the_key_as_text(page):
    _render_remove_mapping_form(page, "o'lab <b>x</b>")

    page.get_by_role("button", name="Remove").click()
    message = page.locator(f"{CONFIRM_MODAL} [data-confirm-message]")
    expect(message).to_have_text("Remove mapping for server 'o'lab <b>x</b>'? This cannot be undone.")
    assert message.locator("b").count() == 0
    page.locator(CONFIRM_MODAL).get_by_role("button", name="Confirm").click()

    [pairs] = page.evaluate("window.submitted")
    assert ["server_key", "o'lab <b>x</b>"] in pairs
    assert ["active_server_key", "production"] in pairs
    assert page.evaluate("window.confirmCalls") == []


def _modal_state(page):
    """Return which modals are open, how many backdrops exist, and whether the page scroll is locked."""
    return page.evaluate(
        """() => ({
            open: Array.from(document.querySelectorAll('.modal.show')).map((modal) => modal.id),
            backdrops: document.querySelectorAll('.modal-backdrop').length,
            locked: document.body.classList.contains('modal-open'),
        })"""
    )


def _nested_confirm_page(page, requests):
    def answer(route):
        requests.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body="")

    page.route(f"{HTMX_SYNC_URL}**", answer)
    page.set_content(
        MODAL_CSS
        + f"""
        <div class="modal" id="parent-modal" tabindex="-1"><div class="modal-dialog"><div class="modal-content">
          <div class="modal-body">
            <button id="move" type="button" hx-post="{HTMX_SYNC_URL}/move" hx-swap="none"
                    hx-confirm="Move interface 'eth0' to winner?">Move</button>
          </div>
          <div class="modal-footer"><button type="button" data-bs-dismiss="modal">Close parent</button></div>
        </div></div></div>
        """
    )
    _add_page_scripts(page)
    page.evaluate("showModal(document.getElementById('parent-modal'))")
    assert _modal_state(page) == {"open": ["parent-modal"], "backdrops": 1, "locked": True}
    page.click("#move")
    confirm = page.locator(CONFIRM_MODAL)
    expect(confirm).to_be_visible()
    # The confirmation sits on top: the point under its buttons belongs to the confirm modal.
    box = confirm.get_by_role("button", name="Confirm").bounding_box()
    on_top = page.evaluate(
        "([x, y]) => !!document.elementFromPoint(x, y)?.closest('#librenms-confirm-modal')",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
    )
    assert on_top
    return confirm


@pytest.mark.parametrize("dismiss", ["cancel", "escape", "close", "backdrop"])
def test_a_confirmation_over_an_open_modal_leaves_the_parent_open_when_dismissed(page, dismiss):
    requests = []
    confirm = _nested_confirm_page(page, requests)

    if dismiss == "cancel":
        confirm.get_by_role("button", name="Cancel").click()
    elif dismiss == "escape":
        page.keyboard.press("Escape")
    elif dismiss == "close":
        confirm.get_by_role("button", name="Close").click()
    else:
        page.mouse.click(5, 5)
    expect(confirm).to_be_hidden()

    assert requests == []
    assert _modal_state(page) == {"open": ["parent-modal"], "backdrops": 1, "locked": True}
    # Focus returns into the parent, so its own Escape and focus trap keep working.
    assert page.evaluate("document.activeElement.id") == "move"


def test_a_confirmation_over_an_open_modal_sends_once_and_leaves_the_parent_open(page):
    requests = []
    confirm = _nested_confirm_page(page, requests)

    with page.expect_response(f"{HTMX_SYNC_URL}/move"):
        confirm.get_by_role("button", name="Confirm").click()
    expect(confirm).to_be_hidden()

    assert requests == [f"{HTMX_SYNC_URL}/move"]
    assert _modal_state(page) == {"open": ["parent-modal"], "backdrops": 1, "locked": True}
    # Closing the parent afterwards removes the last backdrop and the scroll lock.
    page.get_by_role("button", name="Close parent").click()
    assert _modal_state(page) == {"open": [], "backdrops": 0, "locked": False}


def test_a_standalone_confirmation_leaves_no_backdrop_or_scroll_lock(page):
    _rebind_form_page(page)

    page.click("#rebind")
    assert _modal_state(page) == {"open": ["librenms-confirm-modal"], "backdrops": 1, "locked": True}
    page.locator(CONFIRM_MODAL).get_by_role("button", name="Cancel").click()

    assert _modal_state(page) == {"open": [], "backdrops": 0, "locked": False}


@pytest.mark.parametrize(
    "table_id",
    ["librenms-interface-table", "librenms-ipaddress-table", "librenms-cable-table"],
)
def test_bulk_submit_still_consumes_off_page_selection_for_other_row_action_tabs(page, table_id):
    """Bulk actions still submit and clear off-page rows for every other row-action tab."""
    bulk_url = "https://plugin.example.com/bulk"
    page.route(bulk_url, lambda route: route.fulfill(body="", content_type="text/html"))
    page.set_content(
        f"""
        <input name="server_key" value="production">
        <form id="sync-form" method="post" action="{bulk_url}">
          <table id="{table_id}"><tbody><tr>
            <td><input type="checkbox" name="select" value="201"></td>
          </tr></tbody></table>
          <button id="bulk-submit" type="submit">Sync selected</button>
        </form>
        """
    )
    _add_page_scripts(page)
    storage_key = page.evaluate(
        """() => {
            const table = document.querySelector('table');
            writeStoredSelection(table, {'101': {inputs: {}, auto: ''}});
            return _selectionStorageKey(table);
        }"""
    )

    # A listener that cancels the submit would stand in for htmx, so the native submit really navigates.
    with page.expect_request(bulk_url) as submitted:
        page.click("#bulk-submit")

    assert ("select", "101") in _selection_form_pairs(submitted.value.post_data)
    assert page.evaluate("key => window.sessionStorage.getItem(key)", storage_key) is None


HTMX_SYNC_URL = "https://plugin.example.com/htmx-sync"


def _htmx_sync_form(table_id="librenms-interface-table"):
    return f"""
        <div id="tab-content">
          <form id="sync-form" method="post" action="{HTMX_SYNC_URL}"
                hx-post="{HTMX_SYNC_URL}" hx-target="#tab-content" hx-swap="innerHTML"
                hx-sync="#tab-content:drop">
            <table id="{table_id}"><tbody><tr>
              <td><input type="checkbox" name="select" value="201" checked></td>
              <td><button id="row-sync" type="submit" name="sync_one" value="201">Sync</button></td>
            </tr></tbody></table>
            <button id="bulk-submit" type="submit">
              <span class="spinner spinner-border d-none" id="sync-spinner"></span>Sync selected
            </button>
          </form>
        </div>
        """


@pytest.mark.parametrize("table_id", ["librenms-interface-table", "librenms-cable-table"])
def test_an_htmx_submit_carries_the_off_page_selection(page, table_id):
    """The off-page rows are in the form before htmx serializes it in the form's own submit listener."""
    bodies = []

    def answer(route):
        bodies.append(route.request.post_data)
        route.fulfill(status=200, content_type="text/html", body='<span id="swapped">Synced</span>')

    page.route(HTMX_SYNC_URL, answer)
    page.set_content(_htmx_sync_form(table_id))
    _add_page_scripts(page)
    page.evaluate("writeStoredSelection(document.querySelector('table'), {'101': {inputs: {}, auto: ''}})")

    page.click("#bulk-submit")
    page.locator("#swapped").wait_for()

    assert sorted(value for key, value in _selection_form_pairs(bodies[0]) if key == "select") == ["101", "201"]


# The shape of the lock-conflict middleware's answer: a 200 that swaps nothing, a toast, and the failure event.
TRY_AGAIN_TEXT = "Try again."
TRY_AGAIN_BODY = (
    '<div id="django-messages" hx-swap-oob="true"><div class="toast">'
    f'<div class="toast-body">{TRY_AGAIN_TEXT}</div></div></div>'
)


def _fail(route, failure):
    """Answer a sync submit with one kind of failure."""
    if failure == "status":
        route.fulfill(status=500, content_type="text/html", body="Server error")
    elif failure == "try-again":
        route.fulfill(
            status=200,
            content_type="text/html",
            headers={"HX-Reswap": "none", "HX-Trigger": '{"librenmsRequestFailed":null}'},
            body=TRY_AGAIN_BODY,
        )
    else:
        route.abort()


@pytest.mark.parametrize("failure", ["status", "transport", "try-again"])
def test_a_failed_htmx_submit_gives_the_button_back(page, failure):
    """A failed htmx submit swaps nothing, so a button left disabled would look dead."""
    page.route(HTMX_SYNC_URL, lambda route: _fail(route, failure))
    page.set_content('<div id="django-messages"></div>' + _htmx_sync_form())
    _add_page_scripts(page)
    page.evaluate("initializeSyncFormSpinners()")

    page.click("#bulk-submit")

    expect(page.locator("#bulk-submit")).to_be_enabled()
    expect(page.locator("#sync-spinner")).to_have_class(re.compile(r"\bd-none\b"))
    assert page.locator("#sync-form").count() == 1
    if failure == "try-again":
        expect(page.locator("#django-messages .toast-body")).to_have_text(TRY_AGAIN_TEXT)


@pytest.mark.parametrize("failure", ["status", "try-again"])
def test_a_retry_after_a_failed_htmx_submit_still_carries_the_off_page_selection(page, failure):
    """A failed submit gives the off-page rows back, so the notice shows them and a retry posts them."""
    bodies = []

    def answer(route):
        bodies.append(route.request.post_data)
        if len(bodies) == 1:
            _fail(route, failure)
        else:
            route.fulfill(status=200, content_type="text/html", body='<span id="swapped">Synced</span>')

    page.route(HTMX_SYNC_URL, answer)
    page.set_content('<div id="django-messages"></div>' + _htmx_sync_form())
    _add_page_scripts(page)
    page.evaluate("initializeSyncFormSpinners()")
    page.evaluate(
        "writeStoredSelection(document.querySelector('table'), {'101': {inputs: {expected_local_id_101: '7'}, auto: ''}})"
    )

    page.click("#bulk-submit")
    expect(page.locator("#librenms-interface-table-offpage-selection")).to_contain_text("1 more")
    page.click("#bulk-submit")
    page.locator("#swapped").wait_for()

    first, retry = (_selection_form_pairs(body) for body in bodies)
    assert sorted(first) == sorted(retry) == [("expected_local_id_101", "7"), ("select", "101"), ("select", "201")]


def test_a_dropped_row_submit_keeps_the_pending_bulk_submit_recoverable(page):
    """A row submit that htmx drops behind a pending bulk submit must not lose that submit's off-page rows."""
    held = []
    bodies = []

    def answer(route):
        bodies.append(route.request.post_data)
        if len(bodies) == 1:
            held.append(route)
        else:
            route.fulfill(status=200, content_type="text/html", body='<span id="swapped">Synced</span>')

    page.route(HTMX_SYNC_URL, answer)
    page.set_content(_htmx_sync_form())
    _add_page_scripts(page)
    page.evaluate("writeStoredSelection(document.querySelector('table'), {'101': {inputs: {}, auto: ''}})")

    with page.expect_request(HTMX_SYNC_URL):
        page.click("#bulk-submit")
    page.click("#row-sync")
    held[0].fulfill(status=500, content_type="text/html", body="Server error")
    expect(page.locator("#librenms-interface-table-offpage-selection")).to_contain_text("1 more")
    page.click("#bulk-submit")
    page.locator("#swapped").wait_for()

    first, retry = (_selection_form_pairs(body) for body in bodies)
    assert sorted(first) == sorted(retry) == [("select", "101"), ("select", "201")]


def test_a_successful_htmx_redirect_does_not_give_the_off_page_selection_back(page):
    """An HX-Redirect success leaves htmx's `successful` unset, and it must not read as a failure."""
    fixture_url = page.url
    page.route(
        HTMX_SYNC_URL,
        lambda route: route.fulfill(status=200, headers={"HX-Redirect": fixture_url}, body=""),
    )
    page.set_content(_htmx_sync_form())
    _add_page_scripts(page)
    storage_key = page.evaluate(
        """() => {
            const table = document.querySelector('table');
            writeStoredSelection(table, {'101': {inputs: {}, auto: ''}});
            return _selectionStorageKey(table);
        }"""
    )

    # The redirect reloads the same path, so the new document reads the same selection store.
    with page.expect_navigation():
        page.click("#bulk-submit")

    assert page.evaluate("key => window.sessionStorage.getItem(key)", storage_key) is None


def test_a_retry_after_a_failed_module_install_still_carries_the_off_page_selection(page):
    """The module table sits outside its form, and its off-page rows come back after a failure too."""
    bodies = []

    def answer(route):
        bodies.append(route.request.post_data)
        if len(bodies) == 1:
            route.fulfill(status=500, content_type="text/html", body="Server error")
        else:
            route.fulfill(status=200, content_type="text/html", body='<span id="swapped">Installed</span>')

    page.route(HTMX_SYNC_URL, answer)
    page.set_content(
        f"""
        <div id="module-sync-content">
          <form id="install-selected-form" method="post" action="{HTMX_SYNC_URL}"
                hx-post="{HTMX_SYNC_URL}" hx-target="#module-sync-content" hx-swap="innerHTML">
            <button id="install-submit" type="submit">Install Selected</button>
          </form>
          <table id="librenms-module-table"><tbody><tr>
            <td><input type="checkbox" name="select" value="201" checked>
                <select id="device_selection_201" name="device_selection_201"><option value="7" selected>m7</option></select></td>
          </tr></tbody></table>
        </div>
        """
    )
    _add_page_scripts(page)
    # The page script wires its htmx:configRequest injector on DOMContentLoaded, which already fired here.
    page.evaluate("document.dispatchEvent(new Event('DOMContentLoaded'))")
    page.evaluate(
        "writeStoredSelection(document.getElementById('librenms-module-table'),"
        " {'101': {inputs: {device_selection_101: '9'}, auto: ''}})"
    )

    page.click("#install-submit")
    expect(page.locator("#librenms-module-table-offpage-selection")).to_contain_text("1 more")
    page.click("#install-submit")
    page.locator("#swapped").wait_for()

    first, retry = (sorted(_selection_form_pairs(body)) for body in bodies)
    expected = [("device_selection_101", "9"), ("device_selection_201", "7"), ("select", "101"), ("select", "201")]
    assert first == retry == expected


def _selection_form_pairs(post_data):
    """Parse an application/x-www-form-urlencoded body into (name, value) pairs."""
    from urllib.parse import parse_qsl

    return parse_qsl(post_data or "")


def _replace_fixture_markup(html, old, new):
    """Replace fixture markup, failing when _page_html no longer emits the source markup."""
    assert old in html, f"fixture markup is gone, so the replacement would be a no-op: {old}"
    return html.replace(old, new)


def _fragment_loader(tab, content_id):
    """Mirror the hidden loader element librenms_sync_base.html puts in every tab pane."""
    return (
        f'<span class="d-none" data-fragment-loader hx-get="https://plugin.example.com/fragment/{tab}"'
        f' hx-trigger="librenms:load-fragment" hx-target="#{content_id}"'
        f' hx-sync="#{content_id}:abort" hx-swap="innerHTML"></span>'
    )


def _page_html(
    initial_state,
    contract=None,
    active_tab="interfaces",
    valid_states=None,
    action_form=False,
):
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
    interface_action = '<button id="interface-action">Sync</button>'
    if action_form:
        interface_action = (
            '<form id="interface-action-form" hx-post="https://plugin.example.com/action/interfaces"'
            ' hx-target="#interface-sync-content" hx-swap="innerHTML"'
            ' hx-sync="#interface-sync-content:drop">'
            '<button id="interface-action" type="submit">Sync</button></form>'
        )
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
             data-tab-id="interfaces">
          {_fragment_loader("interfaces", interface_content_id)}
          <div id="{interface_content_id}">{interface_action}</div>
        </div>
        <div id="ipaddresses" class="tab-pane{" active" if active_tab == "ipaddresses" else ""}"
             data-tab-id="ipaddresses">
          {_fragment_loader("ipaddresses", ip_content_id)}
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
        """payload => {
            const region = document.querySelector('#librenms-sync-tabs');
            region.outerHTML = '<div id="librenms-sync-tabs" data-active-tab="ipaddresses">'
                + '<script id="librenms-sync-rendered-status" type="application/json">'
                + JSON.stringify(payload.status)
                + '</script>'
                + '<div id="interfaces" class="tab-pane" data-tab-id="interfaces">'
                + payload.interfaceLoader
                + '<div id="interface-sync-content"><p>Server-rendered interface rows</p></div>'
                + '</div>'
                + '<div id="ipaddresses" class="tab-pane active" data-tab-id="ipaddresses">'
                + payload.ipLoader
                + '<div id="ipaddress-sync-content"><p>Server-rendered IP rows</p></div>'
                + '</div></div>';
            const swapped = document.querySelector('#librenms-sync-tabs');
            // In production this markup arrives through a real HTMX swap, which processes it.
            window.htmxTest.process(swapped);
            swapped.dispatchEvent(new CustomEvent('htmx:afterSwap', {bubbles: true}));
        }""",
        {
            "status": rendered,
            "interfaceLoader": _fragment_loader("interfaces", "interface-sync-content"),
            "ipLoader": _fragment_loader("ipaddresses", "ipaddress-sync-content"),
        },
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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


SYNC_PAGE_URL = "https://plugin.example.com/device/1/librenms-sync/?tab=modules&interface_name_field=ifDescr"


def _serve_sync_page(page, html, url=SYNC_PAGE_URL):
    """Serve the fixture from the sync page's own URL so window.location is what NetBox shows."""
    page.route(
        lambda candidate: candidate.split("?")[0] == url.split("?")[0],
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto(url)
    _add_page_scripts(page)


def test_cache_reads_carry_the_browser_url(page):
    """The fragment is rendered with return_url links, so it must learn the page URL like an HTMX request."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
    }
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": initial}),
    )
    page.route(
        "https://plugin.example.com/fragment/interfaces*",
        lambda route: route.fulfill(body='<div id="interface-sync-content">refreshed</div>', content_type="text/html"),
    )
    _serve_sync_page(
        page, '<input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">' + _page_html(initial)
    )
    with page.expect_request("https://plugin.example.com/fragment/interfaces*") as fragment:
        page.evaluate(
            """
            async () => {
                initializeSyncCacheConsistency();
                await checkSyncCacheStatus();
                await loadSyncCacheFragment('interfaces');
            }
            """
        )

    assert fragment.value.headers.get("hx-current-url") == SYNC_PAGE_URL


SAVE_PREF_URL = "https://plugin.example.com/save-pref"


def _ipaddress_toggle_fragment():
    """Build an IP address fragment around the template's own "Sync options" menu."""
    from django.template import Context, Engine, Library

    urls = Library()

    @urls.simple_tag(name="url")
    def url(name):
        assert name == "plugins:netbox_librenms_plugin:save_user_pref"
        return SAVE_PREF_URL

    engine = Engine()
    engine.template_builtins.append(urls)
    menu = engine.from_string((TEMPLATE_DIR / "inc" / "_ip_sync_options.html").read_text(encoding="utf-8"))
    return (
        menu.render(Context({"ip_sync": {"set_primary_ip": True}}, use_l10n=False))
        + '<table id="librenms-ipaddress-table"><tbody>'
        '<tr data-mgmt-ip="true"><td><input type="checkbox" name="select" value="10.0.0.1"></td></tr>'
        '<tr><td><input type="checkbox" name="select" value="10.0.0.2"></td></tr>'
        "</tbody></table>"
    )


def test_a_cache_fragment_binds_the_primary_ip_toggle(page):
    """A restored fragment must bind its "Set Primary IP" toggle, or the toggle stays dead until a sync."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
    }
    saved_prefs = []

    def save_pref(route):
        saved_prefs.append(route.request.post_data)
        route.fulfill(json={"status": "ok"})

    page.route(
        "https://plugin.example.com/fragment/ipaddresses*",
        lambda route: route.fulfill(body=_ipaddress_toggle_fragment(), content_type="text/html"),
    )
    page.route(SAVE_PREF_URL, save_pref)
    _serve_sync_page(
        page, '<input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">' + _page_html(initial)
    )
    page.evaluate("async () => { initializeSyncCacheConsistency(); await loadSyncCacheFragment('ipaddresses'); }")

    mgmt_row = page.locator('tr[data-mgmt-ip="true"] input[name="select"]')
    plain_row = page.locator('tr:not([data-mgmt-ip]) input[name="select"]')
    assert mgmt_row.is_checked()
    assert not plain_row.is_checked()

    with page.expect_request(SAVE_PREF_URL):
        page.uncheck("#set-primary-ip-toggle-cb")

    assert not mgmt_row.is_checked()
    assert [{"key": body["key"], "value": body["value"]} for body in map(json.loads, saved_prefs)] == [
        {"key": "set_primary_ip", "value": False}
    ]

    page.check("#set-primary-ip-toggle-cb")

    assert mgmt_row.is_checked()


def test_a_cache_fragment_load_does_not_recheck_the_cache_status(page):
    """The loader runs as the outcome of a status check, so its swap must not start another one."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state("ipaddresses-ready"),
    }
    status_requests = []

    def serve_status(route):
        status_requests.append(route.request.url)
        route.fulfill(json={"tabs": initial})

    page.route("https://plugin.example.com/status?*", serve_status)
    page.route(
        "https://plugin.example.com/fragment/interfaces*",
        lambda route: route.fulfill(body='<span id="restored">restored</span>', content_type="text/html"),
    )
    page.set_content(_page_html(initial))
    _add_page_scripts(page)
    page.evaluate("async () => { initializeSyncCacheConsistency(); await loadSyncCacheFragment('interfaces'); }")

    assert page.locator("#restored").count() == 1
    assert page.evaluate("syncCacheController().checking") is None
    assert status_requests == []


def test_module_verify_carries_the_browser_url(page):
    """The verify response rebuilds the row's action links, so the POST must carry the page URL too."""
    page.route(
        "**/plugins/librenms_plugin/verify-module/",
        lambda route: route.fulfill(json={"status": "error"}),
    )
    _serve_sync_page(
        page,
        """
        <input type="hidden" name="csrfmiddlewaretoken" value="test-csrf-token">
        <input type="hidden" name="server_key" value="primary">
        <table><tbody>
          <tr data-ent-index="7" data-depth="0">
            <td data-col="device_selection">
              <select id="module-device" data-row-id="7" data-module="7"><option value="1">r01</option></select>
            </td>
          </tr>
        </tbody></table>
        """,
    )
    with page.expect_request("**/plugins/librenms_plugin/verify-module/") as verify:
        page.evaluate("handleModuleChange(document.getElementById('module-device'), '1')")

    assert verify.value.headers.get("hx-current-url") == SYNC_PAGE_URL


def test_cold_tab_does_not_flash_stale_during_tab_navigation(page):
    """Checking a never-refreshed tab must not briefly mark it stale."""
    initial = {
        "interfaces": _state("interfaces-ready"),
        "ipaddresses": _state(None, state="missing", available=False),
    }
    initial["ipaddresses"]["timestamp"] = None

    page.route(
        "https://plugin.example.com/page*",
        lambda route: route.fulfill(
            body=_page_html(
                initial, active_tab="ipaddresses" if "tab=ipaddresses" in route.request.url else "interfaces"
            ),
            content_type="text/html",
        ),
    )
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": initial}),
    )
    page.goto("https://plugin.example.com/page")
    _add_page_scripts(page)
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
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency()")

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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
    page.evaluate("initializeSyncCacheConsistency()")

    assert page.evaluate("typeof bootstrap") == "undefined"
    page.locator("#ipaddresses-tab").click()
    page.wait_for_url("**/page?tab=ipaddresses")
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency()")

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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
    page.evaluate("initializeSyncCacheConsistency()")
    tab = page.locator("#ipaddresses-tab")
    assert tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")

    tab.click()
    page.wait_for_url("**/page?tab=ipaddresses")
    page.add_script_tag(path=str(SCRIPT_PATH))
    page.evaluate("initializeSyncCacheConsistency()")
    assert page.evaluate("typeof initializeSyncCacheConsistency") == "function"
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().lastCheckFailed === true && syncCacheController().checking === null")

    assert page.locator("#modal-force-action").is_disabled()
    assert page.locator("#modal-already-disabled").is_disabled()

    page.evaluate("checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().lastCheckFailed === false && syncCacheController().checking === null")

    assert not page.locator("#modal-force-action").is_disabled()
    assert page.locator("#modal-already-disabled").is_disabled()
    assert page.locator("#restored-interface-action").count() == 1


def test_verified_missing_snapshot_restores_pane_controls_but_not_modal_controls(page):
    """A verified missing snapshot must restore pane controls but not modal controls."""
    initial = {
        "interfaces": _state("cold-interfaces", "missing", available=False),
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
            const button = document.createElement('button');
            button.id = 'interfaces-refresh';
            document.getElementById('interfaces').append(button);
        }"""
    )
    page.route("https://plugin.example.com/status?*", status_response)
    _add_page_scripts(page)
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().lastCheckFailed === true && syncCacheController().checking === null")

    assert page.locator("#interfaces-refresh").is_disabled()
    assert page.locator("#modal-force-action").is_disabled()

    page.evaluate("checkSyncCacheStatus()")
    page.wait_for_function("syncCacheController().lastCheckFailed === false && syncCacheController().checking === null")

    assert not page.locator("#interfaces-refresh").is_disabled()
    assert page.locator("#modal-force-action").is_disabled()
    assert "could not be verified" in page.locator("#interface-sync-content").inner_text()


@pytest.mark.parametrize("tab", ["interface", "ipaddress", "module", "vlan", "cable"])
def test_refresh_forms_and_countdowns_live_in_the_swapped_content(tab):
    wrapper = (TEMPLATE_DIR / f"_{tab}_sync.html").read_text()
    content = (TEMPLATE_DIR / f"_{tab}_sync_content.html").read_text()
    refresh = (TEMPLATE_DIR / "inc" / f"_{tab}_sync_refresh.html").read_text()

    assert f'id="{tab}-sync-content"' in wrapper
    assert f"_{tab}_sync_content.html" in wrapper
    assert "sync-tab-refresh-toolbar" not in wrapper
    assert "sync-tab-primary-toolbar" in content
    assert f"_{tab}_sync_refresh.html" in content
    assert "cache_oob" not in content
    assert refresh.index("</button>") < refresh.index("_sync_cache_countdown.html")
    assert "_hidden_server_key.html" in refresh


def test_the_search_line_shares_the_sync_row_and_clears_the_refresh_controls(page):
    """Rendered live-search controls share the toolbar and wrap without overflow."""
    from test_sync_toolbar_browser import load_tab

    load_tab(page, "interface", "interface_sync")
    page.set_viewport_size({"width": 1440, "height": 900})
    actions = page.locator(".sync-tab-actions").bounding_box()
    search = page.locator(".sync-filter-search").bounding_box()
    refresh = page.locator(".sync-tab-refresh-toolbar button").bounding_box()
    assert search["y"] == pytest.approx(actions["y"], abs=1)
    assert refresh["y"] == pytest.approx(actions["y"], abs=1)
    assert search["x"] + search["width"] < refresh["x"]
    diagnostics = page.locator(".sync-tab-lead-row").bounding_box()
    assert diagnostics["y"] >= refresh["y"] + refresh["height"]
    page.set_viewport_size({"width": 390, "height": 900})
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_vlan_group_verification_ignores_a_late_response_for_an_old_selection(page):
    """A late group response must not replace the selected group's row state."""
    page.set_content(
        """
        <input name="csrfmiddlewaretoken" value="test-token">
        <form>
          <table id="librenms-vlan-table"><tbody><tr>
            <td data-col="vlan_id"><span class="text-danger">501</span></td>
            <td data-col="name"><span class="text-danger">Users</span></td>
            <td data-col="vlan_group_selection">
              <select class="vlan-sync-group-select" name="vlan_group_501"
                      data-vlan-id="501" data-vlan-name="Users">
                <option value="" selected>Global</option>
                <option value="1">Group A</option>
                <option value="2">Group B</option>
              </select>
            </td>
            <td data-col="status">Not in NetBox</td>
          </tr></tbody></table>
        </form>
        """
    )
    held_routes = {}

    def hold_verification(route):
        group_id = json.loads(route.request.post_data)["vlan_group_id"]
        held_routes[str(group_id)] = route

    page.route("**/plugins/librenms_plugin/verify-vlan-sync-group/", hold_verification)
    _add_page_scripts(page)
    page.evaluate("initializeVlanSyncGroupSelects()")

    with page.expect_request("**/plugins/librenms_plugin/verify-vlan-sync-group/"):
        page.locator(".vlan-sync-group-select").select_option("1")
    with page.expect_request("**/plugins/librenms_plugin/verify-vlan-sync-group/"):
        page.locator(".vlan-sync-group-select").select_option("2")
    for _attempt in range(40):
        if held_routes.keys() >= {"1", "2"}:
            break
        page.wait_for_timeout(25)
    assert held_routes.keys() >= {"1", "2"}

    held_routes["2"].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "status": "success",
                "css_class": "text-warning",
                "exists_in_netbox": True,
                "name_matches": False,
                "netbox_vlan_name": "Group B name",
                "status_html": '<button id="group-b-action">Update B</button>',
            }
        ),
    )
    page.locator("#group-b-action").wait_for()
    held_routes["1"].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "status": "success",
                "css_class": "text-success",
                "exists_in_netbox": True,
                "name_matches": True,
                "netbox_vlan_name": "Users",
                "status_html": '<span id="group-a-status">Synced A</span>',
            }
        ),
    )
    page.wait_for_timeout(100)

    assert page.locator(".vlan-sync-group-select").input_value() == "2"
    assert page.locator("#group-a-status").count() == 0
    assert page.locator('td[data-col="status"]').inner_text() == "Update B"
    assert page.locator('td[data-col="vlan_id"] span').get_attribute("class") == "text-warning"


def test_vlan_modal_group_verification_ignores_a_late_response_for_an_old_selection(page):
    """A late modal response must not replace the selected group's saved row state."""
    page.set_content(
        """
        <input name="csrfmiddlewaretoken" value="test-token">
        <button id="saveVlanGroups">Save</button>
        <div id="vlanDetailModal" data-current-row-key="port-1">
          <table><tbody><tr id="modal-vlan-row">
            <td><span class="text-danger">501</span></td>
            <td><select id="modal-vlan-group" data-interface="Ethernet1">
              <option value="" selected>Global</option>
              <option value="1">Group A</option>
              <option value="2">Group B</option>
            </select></td>
          </tr></tbody></table>
        </div>
        <button class="vlan-edit-btn" data-row-key="port-1"
                data-vlans='[{"vid": 501, "css": "text-danger", "missing": true}]'>Edit</button>
        """
    )
    held_routes = {}

    def hold_verification(route):
        group_id = json.loads(route.request.post_data)["vlan_group_id"]
        held_routes[str(group_id)] = route

    page.route("**/plugins/librenms_plugin/verify-vlan-group/", hold_verification)
    _add_page_scripts(page)
    select = page.locator("#modal-vlan-group")

    select.select_option("1")
    page.evaluate("verifyVlanInGroup(document.getElementById('modal-vlan-group'), '7', 501, 'T', '1')")
    select.select_option("2")
    page.evaluate("verifyVlanInGroup(document.getElementById('modal-vlan-group'), '7', 501, 'T', '2')")
    for _attempt in range(40):
        if held_routes.keys() >= {"1", "2"}:
            break
        page.wait_for_timeout(25)
    assert held_routes.keys() >= {"1", "2"}

    held_routes["2"].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({"status": "success", "css_class": "text-warning", "is_missing": True}),
    )
    page.wait_for_function("document.getElementById('modal-vlan-row').dataset.resolvedCss === 'text-warning'")
    held_routes["1"].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps({"status": "success", "css_class": "text-success", "is_missing": False}),
    )
    page.wait_for_timeout(100)

    row = page.locator("#modal-vlan-row")
    button_vlans = json.loads(page.locator('.vlan-edit-btn[data-row-key="port-1"]').get_attribute("data-vlans"))
    assert select.input_value() == "2"
    assert row.get_attribute("data-resolved-css") == "text-warning"
    assert row.locator("td:first-child span").get_attribute("class") == "text-warning"
    assert button_vlans == [{"vid": 501, "css": "text-warning", "missing": True}]
    assert not page.locator("#saveVlanGroups").is_disabled()


def test_restored_vlan_group_selection_reverifies_only_a_changed_group(page):
    """A restored group must repaint its row without rechecking an unchanged row."""
    page.set_content(
        """
        <input name="csrfmiddlewaretoken" value="test-token">
        <input name="server_key" value="default">
        <form>
          <table id="librenms-vlan-table"><tbody>
            <tr>
              <td data-col="selection"><input type="checkbox" name="select" value="501"></td>
              <td data-col="vlan_id"><span class="text-success">501</span></td>
              <td data-col="name"><span class="text-success">Users</span></td>
              <td data-col="vlan_group_selection">
                <select class="vlan-sync-group-select" name="vlan_group_501"
                        data-vlan-id="501" data-vlan-name="Users">
                  <option value="1" selected>Group A</option>
                  <option value="2">Group B</option>
                </select>
              </td>
              <td data-col="status">Synced in A</td>
            </tr>
            <tr>
              <td data-col="selection"><input type="checkbox" name="select" value="502"></td>
              <td data-col="vlan_id"><span class="text-success">502</span></td>
              <td data-col="name"><span class="text-success">Servers</span></td>
              <td data-col="vlan_group_selection">
                <select class="vlan-sync-group-select" name="vlan_group_502"
                        data-vlan-id="502" data-vlan-name="Servers">
                  <option value="1" selected>Group A</option>
                  <option value="2">Group B</option>
                </select>
              </td>
              <td data-col="status">Synced in A</td>
            </tr>
          </tbody></table>
        </form>
        """
    )
    requests = []

    def answer_verification(route):
        payload = json.loads(route.request.post_data)
        requests.append((payload["vid"], str(payload["vlan_group_id"])))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "status": "success",
                    "css_class": "text-danger",
                    "exists_in_netbox": False,
                    "name_matches": False,
                    "status_html": '<button id="restored-group-action">Create in B</button>',
                }
            ),
        )

    page.route("**/plugins/librenms_plugin/verify-vlan-sync-group/", answer_verification)
    _add_page_scripts(page)
    page.evaluate(
        """() => {
            const table = document.getElementById('librenms-vlan-table');
            writeStoredSelection(table, {
                '501': {inputs: {vlan_group_501: '2'}, auto: ''},
                '502': {inputs: {vlan_group_502: '1'}, auto: ''}
            });
            restoreTableSelection(table);
        }"""
    )
    for _attempt in range(40):
        if requests:
            break
        page.wait_for_timeout(25)

    assert requests == [("501", "2")]
    page.locator("#restored-group-action").wait_for()
    assert page.locator('[name="vlan_group_501"]').input_value() == "2"
    assert page.locator('[name="vlan_group_502"]').input_value() == "1"
    assert page.locator('tr:has([name="vlan_group_501"]) td[data-col="status"]').inner_text() == "Create in B"
    assert page.locator('tr:has([name="vlan_group_502"]) td[data-col="status"]').inner_text() == "Synced in A"


def test_filter_disclosure_state_survives_fragment_replacement(page):
    """An expanded filter remains expanded and visibly active after its tab refreshes."""
    markup = """
      <div id="filter-host">
        <button class="btn btn-sm btn-secondary sync-filter-toggle" type="button"
                data-bs-target="#interfaceFilterSection" aria-expanded="false"
                aria-controls="interfaceFilterSection">
          <i class="mdi mdi-filter-outline"></i> Filters
        </button>
        <div class="collapse" id="interfaceFilterSection"></div>
      </div>
    """
    page.set_content(markup)
    _add_page_scripts(page)
    page.evaluate("initializeScripts()")

    page.locator("#interfaceFilterSection").evaluate(
        """section => {
            section.classList.add('show');
            section.dispatchEvent(new Event('shown.bs.collapse', { bubbles: true }));
        }"""
    )
    button = page.locator(".sync-filter-toggle")
    assert "active" in button.get_attribute("class").split()
    assert button.get_attribute("aria-expanded") == "true"
    assert button.locator("i").evaluate("icon => icon.classList.contains('mdi-filter-check')")

    page.locator("#filter-host").evaluate("(host, markup) => { host.innerHTML = markup; }", markup)
    page.evaluate("initializeScripts()")

    assert page.locator("#interfaceFilterSection").evaluate("section => section.classList.contains('show')")
    assert "active" in page.locator(".sync-filter-toggle").get_attribute("class").split()

    page.locator("#interfaceFilterSection").evaluate("section => section.classList.remove('show')")
    page.evaluate("document.body.dispatchEvent(new CustomEvent('htmx:afterSettle'))")

    assert page.locator("#interfaceFilterSection").evaluate("section => section.classList.contains('show')")
    assert "btn-primary" in page.locator(".sync-filter-toggle").get_attribute("class").split()

    page.locator("#interfaceFilterSection").evaluate(
        """section => {
            section.classList.remove('show');
            section.dispatchEvent(new Event('hidden.bs.collapse', { bubbles: true }));
        }"""
    )
    button = page.locator(".sync-filter-toggle")
    assert "active" not in button.get_attribute("class").split()
    assert "btn-secondary" in button.get_attribute("class").split()
    assert button.get_attribute("aria-expanded") == "false"
    assert button.locator("i").evaluate("icon => icon.classList.contains('mdi-filter-outline')")


def test_a_closed_filter_panel_stays_closed_when_the_server_opened_it(page):
    """The server opens a panel that holds a filter value once; a later settle keeps the user's close."""
    markup = """
      <div id="filter-host">
        <button class="btn btn-sm btn-secondary sync-filter-toggle" type="button"
                data-bs-target="#interfaceFilterSection" aria-expanded="false"
                aria-controls="interfaceFilterSection">
          <i class="mdi mdi-filter-outline"></i> Filters
        </button>
        <div class="collapse show" id="interfaceFilterSection" data-filter-open="true"></div>
      </div>
    """
    page.set_content(markup)
    _add_page_scripts(page)
    page.evaluate("initializeScripts()")
    section = page.locator("#interfaceFilterSection")
    assert section.evaluate("section => section.classList.contains('show')")

    section.evaluate(
        """section => {
            section.classList.remove('show');
            section.dispatchEvent(new Event('hidden.bs.collapse', { bubbles: true }));
        }"""
    )
    page.evaluate("document.body.dispatchEvent(new CustomEvent('htmx:afterSettle'))")

    assert not section.evaluate("section => section.classList.contains('show')")
    assert page.locator(".sync-filter-toggle").get_attribute("aria-expanded") == "false"

    # A new render with a filter value opens the panel again.
    page.locator("#filter-host").evaluate("(host, markup) => { host.innerHTML = markup; }", markup)
    page.evaluate("initializeScripts()")
    assert section.evaluate("section => section.classList.contains('show')")


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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
    page.evaluate("() => { initializeSyncCacheConsistency(); checkSyncCacheStatus(); }")
    assert page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")

    page.clock.fast_forward(20_000)
    page.clock.resume()
    page.wait_for_function("syncCacheController().lastCheckFailed === true")

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
    _add_page_scripts(page)
    with page.expect_request("https://plugin.example.com/fragment/interfaces?*"):
        page.evaluate("() => { initializeSyncCacheConsistency(); checkSyncCacheStatus(); }")
    page.wait_for_function("syncCacheController().status.interfaces.revision === 'after-interfaces'")
    assert page.evaluate("syncCacheController().checking !== null")
    assert page.evaluate("syncCacheController().lastCheckFailed") is False
    assert page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")

    page.clock.fast_forward(20_000)
    page.clock.resume()
    page.wait_for_function("syncCacheController().lastCheckFailed === true")

    assert page.evaluate("syncCacheController().checking") is None
    assert page.locator("#interface-action").count() == 0
    assert not page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")
    assert stalled_routes
    for route in stalled_routes:
        route.abort()


FRAGMENT_URL_PREFIX = "https://plugin.example.com/fragment/interfaces"
ACTION_URL = "https://plugin.example.com/action/interfaces"
# Count the loader's htmx lifecycle events, so a test can tell a dropped restore from an issued one.
LOADER_EVENT_COUNTER = """() => {
    window.loaderEvents = { trigger: 0, request: 0 };
    const loader = document.querySelector('#interfaces [data-fragment-loader]');
    loader.addEventListener('htmx:trigger', () => { window.loaderEvents.trigger += 1; });
    loader.addEventListener('htmx:beforeRequest', () => { window.loaderEvents.request += 1; });
}"""


def _race_states():
    initial = {
        "interfaces": _state("before-interfaces"),
        "ipaddresses": _state("before-ip"),
    }
    return initial, {**initial, "interfaces": _state("after-interfaces")}


def test_action_supersedes_an_in_flight_cache_restore(page):
    """A module action must abort an in-flight cache restore so the older fragment cannot replace its response."""
    initial, current = _race_states()
    fragment_routes = []

    page.set_content(_page_html(initial, action_form=True))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.route(f"{FRAGMENT_URL_PREFIX}?*", lambda route: fragment_routes.append(route))
    page.route(
        ACTION_URL,
        lambda route: route.fulfill(body='<span id="after-action">After action</span>'),
    )
    _add_page_scripts(page)
    with page.expect_request(f"{FRAGMENT_URL_PREFIX}?*"):
        page.evaluate("() => { initializeSyncCacheConsistency(); checkSyncCacheStatus(); }")
    page.wait_for_function("document.querySelector('[data-fragment-loader]').classList.contains('htmx-request')")

    # The fixed loader aborts its request when the action is posted; the unfixed one keeps it in flight.
    try:
        with page.expect_event(
            "requestfailed", lambda request: request.url.startswith(FRAGMENT_URL_PREFIX), timeout=2_000
        ):
            page.locator("#interface-action").click()
        restore_aborted = True
    except PlaywrightTimeoutError:
        restore_aborted = False
    page.locator("#after-action").wait_for()
    if not restore_aborted:
        fragment_routes[0].fulfill(body='<span id="stale">Stale restore</span>')
    page.wait_for_function("syncCacheController().checking === null")

    assert page.locator("#stale").count() == 0
    assert page.locator("#after-action").count() == 1
    assert restore_aborted
    assert page.evaluate("syncCacheController().lastCheckFailed") is False
    assert "could not be restored" not in page.locator("#interface-sync-content").inner_text()
    assert page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")


def test_cache_restore_is_dropped_behind_an_in_flight_action(page):
    """A restore that arrives during a module action must not be issued, so it cannot replace the action's response."""
    initial, current = _race_states()
    action_routes = []
    fragment_routes = []

    page.set_content(_page_html(initial, action_form=True))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.route(ACTION_URL, lambda route: action_routes.append(route))
    page.route(f"{FRAGMENT_URL_PREFIX}?*", lambda route: fragment_routes.append(route))
    _add_page_scripts(page)
    page.evaluate("initializeSyncCacheConsistency()")
    page.evaluate(LOADER_EVENT_COUNTER)
    with page.expect_request(ACTION_URL):
        page.locator("#interface-action").click()
    page.evaluate("() => { checkSyncCacheStatus(); }")
    page.wait_for_function("window.loaderEvents.trigger === 1")

    assert page.evaluate("window.loaderEvents.request") == 0, "the restore was issued during the action"
    action_routes[0].fulfill(body='<span id="after-action">After action</span>')
    page.locator("#after-action").wait_for()
    page.wait_for_function("syncCacheController().checking === null")

    assert fragment_routes == []
    assert page.locator("#after-action").count() == 1
    assert "could not be restored" not in page.locator("#interface-sync-content").inner_text()
    assert page.evaluate("syncCacheController().lastCheckFailed") is False
    assert page.evaluate("syncCacheController().pendingRestores.size") == 0


def test_failed_action_reissues_a_dropped_cache_restore(page):
    """A restore dropped behind an action must run after the action fails, or the tab keeps stale rows."""
    initial, current = _race_states()
    action_routes = []
    fragment_requests = []

    def restore_fragment(route):
        fragment_requests.append(route.request.url)
        route.fulfill(body='<span id="restored">Restored</span>')

    page.set_content(_page_html(initial, action_form=True))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.route(ACTION_URL, lambda route: action_routes.append(route))
    page.route(f"{FRAGMENT_URL_PREFIX}?*", restore_fragment)
    _add_page_scripts(page)
    page.evaluate("initializeSyncCacheConsistency()")
    page.evaluate(LOADER_EVENT_COUNTER)
    with page.expect_request(ACTION_URL):
        page.locator("#interface-action").click()
    page.evaluate("() => { checkSyncCacheStatus(); }")
    page.wait_for_function("window.loaderEvents.trigger === 1")

    assert page.evaluate("window.loaderEvents.request") == 0, "the restore was issued during the action"
    action_routes[0].fulfill(status=500)
    page.locator("#restored").wait_for()
    page.wait_for_function("syncCacheController().checking === null")

    assert fragment_requests == [f"{FRAGMENT_URL_PREFIX}?server_key=primary"]
    assert page.evaluate("window.loaderEvents") == {"trigger": 2, "request": 1}
    assert page.evaluate("syncCacheController().lastCheckFailed") is False
    assert page.evaluate("syncCacheController().pendingRestores.size") == 0


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
    _add_page_scripts(page)
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
    _add_page_scripts(page)

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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")

    assert page.locator("#restored-comparison").inner_text() == "Current comparison"


def test_restored_content_keeps_its_htmx_bindings(page):
    """Restored rows must act through HTMX, so the fragment has to arrive through an HTMX swap."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("after"),
        "ipaddresses": initial["ipaddresses"],
    }

    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.route(
        "https://plugin.example.com/fragment/interfaces?*",
        lambda route: route.fulfill(
            body='<form hx-post="https://plugin.example.com/action" hx-target="#interface-sync-content"'
            ' hx-swap="innerHTML"><button id="restored-action" type="submit">Go</button></form>'
        ),
    )
    page.route(
        "https://plugin.example.com/action",
        lambda route: route.fulfill(body='<p id="swapped">done</p>'),
    )
    _add_page_scripts(page)
    # NetBox imports HTMX as a module: with no htmx global, HTML the page inserts itself stays unbound.
    assert page.evaluate("typeof htmx") == "undefined"

    page.evaluate("initializeSyncCacheConsistency(); checkSyncCacheStatus()")
    page.wait_for_selector("#restored-action")
    with page.expect_request("https://plugin.example.com/action") as action:
        page.click("#restored-action")

    assert action.value.headers.get("hx-request") == "true"
    assert page.locator("#interface-sync-content #swapped").inner_text() == "done"


def test_tab_navigation_during_a_restore_drops_the_late_fragment(page):
    """A fragment answered after the tab region was swapped belongs to the previous page state."""
    initial = {
        "interfaces": _state("before"),
        "ipaddresses": _state("before-ip"),
    }
    current = {
        "interfaces": _state("after"),
        "ipaddresses": initial["ipaddresses"],
    }
    pending_routes = []

    page.set_content(_page_html(initial))
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: route.fulfill(json={"tabs": current}),
    )
    page.route("https://plugin.example.com/fragment/interfaces?*", lambda route: pending_routes.append(route))
    _add_page_scripts(page)
    with page.expect_request("https://plugin.example.com/fragment/interfaces?*"):
        page.evaluate("() => { initializeSyncCacheConsistency(); checkSyncCacheStatus(); }")

    page.evaluate(
        """() => document.querySelector('#librenms-sync-tabs').dispatchEvent(
            new CustomEvent('htmx:afterSwap', {bubbles: true})
        )"""
    )
    pending_routes[0].fulfill(body='<div id="late-comparison">Late comparison</div>')
    page.wait_for_function("syncCacheController().checking === null")

    assert page.locator("#late-comparison").count() == 0
    assert page.locator("#interface-action").count() == 1


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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
    page.evaluate("initializeSyncCacheConsistency(); initializeCountdown('ip-countdown-timer')")
    tab = page.locator("#ipaddresses-tab")

    assert not tab.evaluate("node => node.classList.contains('sync-cache-ready')")
    assert tab.evaluate("node => node.classList.contains('sync-cache-unavailable')")
    assert page.locator("#ip-action").count() == 0


def test_a_browser_clock_ahead_of_the_server_keeps_a_valid_cached_tab(page):
    """The countdown measures the cache lifetime on the server clock that rendered it."""
    from datetime import UTC, datetime, timedelta

    from django.conf import settings
    from django.template import Context, Engine, Library

    if not settings.configured:
        settings.configure(USE_I18N=False, USE_TZ=True)
    query = Library()
    query.simple_tag(lambda server_key: 600, name="sync_cache_timeout")
    engine = Engine()
    engine.template_libraries["librenms_sync_query"] = query
    countdown = engine.from_string((TEMPLATE_DIR / "inc" / "_sync_cache_countdown.html").read_text()).render(
        Context(
            {
                "cache_expiry": datetime.now(UTC) + timedelta(minutes=5),
                "cache_container_id": "cache-countdown",
                "cache_countdown_id": "countdown-timer",
                "cache_server_key": "primary",
            }
        )
    )
    initial = {"interfaces": _state("before"), "ipaddresses": _state("before-ip")}

    page.clock.install(time=datetime.now(UTC) + timedelta(days=1))
    page.set_content(
        _replace_fixture_markup(
            _page_html(initial),
            '<button id="interface-action">Sync</button>',
            '<table id="interface-rows"><tr><td>Ethernet1</td></tr></table>'
            + countdown
            + '<button id="interface-action">Sync</button>',
        )
    )
    _add_page_scripts(page)
    page.evaluate("initializeSyncCacheConsistency(); initializeCountdown('countdown-timer')")

    assert page.locator("#interface-rows").count() == 1
    assert page.locator("#interface-action").count() == 1
    assert page.evaluate("syncCacheController().invalidatedLocally.size") == 0
    assert page.locator("#interfaces-tab").evaluate("node => node.classList.contains('sync-cache-ready')")
    assert not page.locator("#countdown-timer").evaluate("node => node.classList.contains('is-expired')")


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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
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
    _add_page_scripts(page)
    page.evaluate(
        "status => { initializeSyncCacheConsistency(); return reconcileSyncCacheStatus(status); }",
        current,
    )

    assert "ago" in page.locator("#ipaddress-sync-content").inner_text()


def _modal_script(marker):
    """Return the modal's inline script containing *marker*, which must carry no template syntax."""
    template = (TEMPLATE_DIR / "htmx" / "add_bay_template_modal.html").read_text()
    blocks = [block.split("</script>", 1)[0] for block in template.split("<script>")[1:]]
    matching = [block for block in blocks if marker in block]
    assert len(matching) == 1, f"expected one script containing {marker!r}, found {len(matching)}"
    # Rendering an expression against an empty context blanks it, so the browser would assert
    # against silently mangled JS. Refuse the block instead of guessing what it should hold.
    templated = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", matching[0])
    assert not templated, f"{marker!r} needs a rendering context for {templated}"
    return matching[0]


def test_the_bay_chooser_blocks_next_until_a_bay_is_selected(page):
    """
    Step one must not open the review step with no bay chosen.

    The mapping kind is derived by the server from the bay picked here, so an empty selection
    would review a mapping onto nothing. The kind itself is no longer decided client-side: the
    modal asks for the bay first, then renders the proposal for that bay.
    """
    page.set_content("""<select id="add-bay-name">
        <option value="" selected>Choose a bay</option>
        <option value="RE0">RE0</option></select>
        <button id="add-bay-next" disabled>Next</button>""")
    page.add_script_tag(content=_modal_script("add-bay-next"))

    assert page.locator("#add-bay-next").is_disabled()
    page.locator("#add-bay-name").select_option("RE0")
    assert page.locator("#add-bay-next").is_enabled()
    page.locator("#add-bay-name").select_option("")
    assert page.locator("#add-bay-next").is_disabled(), "an empty selection must close the step again"


def test_a_single_offered_bay_leaves_next_enabled(page):
    """One offered bay is preselected, so it must not need a pointless re-selection."""
    page.set_content("""<select id="add-bay-name">
        <option value="RE0" selected>RE0</option></select>
        <button id="add-bay-next" disabled>Next</button>""")
    page.add_script_tag(content=_modal_script("add-bay-next"))

    assert page.locator("#add-bay-next").is_enabled()


def test_failed_cable_verify_restores_controls_without_a_member_baseline(page):
    """A failed first verification must not leave the row controls disabled."""
    html = """
        <input type="hidden" name="csrfmiddlewaretoken" value="test-csrf">
        <div data-cable-verify-url="https://plugin.example.com/verify-cable/">
          <table>
            <tr data-interface="row-1">
              <td data-col="selection"><input type="checkbox" name="select"></td>
              <td data-col="device_selection">
                <select id="member" data-row-id="row-1" data-interface="row-1">
                  <option value="1">Member one</option>
                  <option value="2" selected>Member two</option>
                </select>
              </td>
              <td data-col="actions"><button type="button">Sync Cable</button></td>
            </tr>
          </table>
        </div>
    """

    pending_route = None

    def hold_verify_route(route):
        nonlocal pending_route
        pending_route = route

    page.route("https://plugin.example.com/verify-cable/", hold_verify_route)
    page.set_content(html)
    _add_page_scripts(page)
    # handleCableChange disables the row before it calls fetch, so waiting on the disabled
    # controls alone can outrun the route handler that captures pending_route.
    with page.expect_request("https://plugin.example.com/verify-cable/"):
        page.evaluate(
            """
            () => {
                const select = document.querySelector('#member');
                handleCableChange(select, select.value);
            }
            """
        )
    page.wait_for_function(
        """
        () => {
            const row = document.querySelector('#member')?.closest('tr');
            const selection = row?.querySelector('input[name="select"]');
            const action = row?.querySelector('td[data-col="actions"] button');
            return selection?.disabled && action?.disabled;
        }
        """
    )

    assert pending_route is not None
    pending_route.fulfill(status=503, body="verification unavailable")

    page.wait_for_function(
        """
        () => {
            const row = document.querySelector('#member')?.closest('tr');
            const selection = row?.querySelector('input[name="select"]');
            const action = row?.querySelector('td[data-col="actions"] button');
            return selection && action && !selection.disabled && !action.disabled;
        }
        """
    )

    row = page.locator("#member").locator("xpath=ancestor::tr[1]")
    assert not row.locator('input[name="select"]').is_disabled()
    assert not row.locator('td[data-col="actions"] button').is_disabled()


def _cable_row_sharing_its_identity_html():
    """Return one cable row whose row identity is also carried by an earlier loaded table."""
    return """
        <input type="hidden" name="csrfmiddlewaretoken" value="token">
        <table id="librenms-interface-table"><tbody>
          <tr data-interface="7018">
            <td data-col="selection"><input type="checkbox" name="select"></td>
            <td data-col="cable_status">interface row</td>
          </tr>
        </tbody></table>
        <div data-cable-verify-url="https://plugin.example.com/verify" data-cable-origin-device-id="7">
          <table id="librenms-cable-table-vc"><tbody>
            <tr data-interface="7018">
              <td data-col="selection"><input type="checkbox" name="select"></td>
              <td data-col="device_selection">
                <select id="device_selection_7018" data-row-id="7018" data-interface="7018">
                  <option value="7" selected>Member 7</option>
                </select>
              </td>
              <td data-col="local_port">Ethernet1</td>
              <td data-col="remote_port">Ethernet2</td>
              <td data-col="remote_device">remote</td>
              <td data-col="cable_status">Not connected</td>
              <td data-col="actions"><button id="row-action">Sync</button></td>
            </tr>
          </tbody></table>
        </div>
    """


def test_cable_verify_updates_the_row_that_owns_the_changed_select(page):
    """A row identity another loaded table also carries must not divert the verify."""
    page.set_content(_cable_row_sharing_its_identity_html())
    _add_page_scripts(page)
    page.evaluate(
        """() => {
            window.fetch = () => Promise.resolve({
                ok: true,
                json: () => Promise.resolve({
                    status: 'success',
                    formatted_row: {
                        local_port: 'Ethernet1',
                        remote_port: 'Ethernet2',
                        remote_device: 'remote',
                        cable_status: 'Connected',
                        actions: '<button id="new-action">Resync</button>',
                        can_create_cable: true
                    }
                })
            });
            handleCableChange(document.getElementById('device_selection_7018'), '7');
        }"""
    )

    page.wait_for_selector("#new-action")
    cable_row = page.locator("#librenms-cable-table-vc tr")
    assert cable_row.locator('td[data-col="cable_status"]').inner_text() == "Connected"
    interface_row = page.locator("#librenms-interface-table tr")
    assert interface_row.locator('td[data-col="cable_status"]').inner_text() == "interface row"


def _cable_row_html(*, with_actions_cell):
    """Return one cable row, optionally rendered without its actions cell."""
    actions_cell = '<td data-col="actions"><button id="row-action">Sync</button></td>' if with_actions_cell else ""
    return f"""
        <input type="hidden" name="csrfmiddlewaretoken" value="token">
        <div data-cable-verify-url="https://plugin.example.com/verify" data-cable-origin-device-id="7">
          <table id="librenms-cable-table"><tbody>
            <tr data-interface="row-1">
              <td data-col="selection"><input type="checkbox" name="select"></td>
              <td data-col="device_selection">
                <select id="member-select" data-row-id="row-1" data-interface="row-1">
                  <option value="7" selected>Member 7</option>
                </select>
              </td>
              <td data-col="local_port">Ethernet1</td>
              <td data-col="remote_port">Ethernet2</td>
              <td data-col="remote_device">remote</td>
              <td data-col="cable_status">Not connected</td>
              {actions_cell}
            </tr>
          </tbody></table>
        </div>
    """


def test_selected_cable_verification_refreshes_off_page_companions(page):
    """A verified member and cable snapshot must replace the checked row's stored values."""
    first_page = f"""
        <input type="hidden" name="csrfmiddlewaretoken" value="token">
        <div data-cable-verify-url="https://plugin.example.com/verify"
             data-cable-origin-device-id="7">
          <form method="post" action="{SELECTION_PAGE_URL}/submit">
            <table id="librenms-cable-table"><tbody>
              <tr data-interface="row-1">
                <td data-col="selection">
                  <input id="selected-cable" type="checkbox" name="select" value="row-1">
                  <input type="hidden" name="expected_local_id_row-1" value="old-local">
                  <input type="hidden" name="expected_local_device_id_row-1" value="old-local-device">
                  <input type="hidden" name="expected_remote_id_row-1" value="old-remote">
                  <input type="hidden" name="expected_remote_device_id_row-1" value="old-remote-device">
                </td>
                <td data-col="device_selection">
                  <select id="member-select" name="device_selection_row-1"
                          data-row-id="row-1" data-interface="row-1">
                    <option value="7" selected>Member 7</option>
                    <option value="9">Member 9</option>
                  </select>
                </td>
                <td data-col="local_port">Ethernet1</td>
                <td data-col="remote_port">Ethernet2</td>
                <td data-col="remote_device">remote</td>
                <td data-col="cable_status">Not connected</td>
                <td data-col="actions"><button type="button">Sync</button></td>
              </tr>
            </tbody></table>
          </form>
        </div>
        """
    second_page = f"""
        <form method="post" action="{SELECTION_PAGE_URL}/submit">
          <table id="librenms-cable-table"><tbody>
            <tr data-interface="other-row">
              <td data-col="selection">
                <input type="checkbox" name="select" value="other-row">
              </td>
            </tr>
          </tbody></table>
        </form>
        """

    def serve_selection_page(route):
        body = second_page if "page=2" in route.request.url else first_page
        route.fulfill(status=200, content_type="text/html", body=body)

    page.route(f"{SELECTION_PAGE_URL}**", serve_selection_page)
    page.goto(f"{SELECTION_PAGE_URL}?page=1")
    _add_page_scripts(page)
    page.check("#selected-cable")
    stored_before_verify = page.evaluate(
        "readStoredSelection(document.getElementById('librenms-cable-table'))['row-1']"
    )
    assert stored_before_verify["inputs"]["device_selection_row-1"] == "7"
    assert stored_before_verify["inputs"]["expected_local_id_row-1"] == "old-local"
    page.evaluate(
        """() => {
            const select = document.getElementById('member-select');
            select.value = '9';
            window.fetch = () => Promise.resolve({
                ok: true,
                json: () => Promise.resolve({
                    status: 'success',
                    formatted_row: {
                        local_port: 'Ethernet9',
                        remote_port: 'Ethernet8',
                        remote_device: 'new-remote',
                        cable_status: 'Connected',
                        actions: '<button type="button">Resync</button>',
                        can_create_cable: true,
                        expected_local_id: 'new-local',
                        expected_local_device_id: 'new-local-device',
                        expected_remote_id: 'new-remote',
                        expected_remote_device_id: 'new-remote-device'
                    }
                })
            });
            handleCableChange(select, select.value);
        }"""
    )
    page.wait_for_function("document.getElementById('member-select')._lastVerifiedMember === '9'")

    page.goto(f"{SELECTION_PAGE_URL}?page=2")
    _add_page_scripts(page)
    page.evaluate("initializeCheckboxes()")
    submitted = dict(
        page.evaluate(
            """() => {
                const form = document.querySelector('form');
                form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
                return Array.from(new FormData(form).entries());
            }"""
        )
    )

    assert submitted == {
        "select": "row-1",
        "device_selection_row-1": "9",
        "expected_local_id_row-1": "new-local",
        "expected_local_device_id_row-1": "new-local-device",
        "expected_remote_id_row-1": "new-remote",
        "expected_remote_device_id_row-1": "new-remote-device",
    }


def test_cable_verify_completes_for_a_row_rendered_without_its_actions_cell(page):
    """A row missing the actions cell must still complete the verify instead of erroring out."""
    page.set_content(_cable_row_html(with_actions_cell=False))
    _add_page_scripts(page)
    page.evaluate(
        """() => {
            window.warnings = [];
            window.errors = [];
            console.warn = (message) => window.warnings.push(String(message));
            console.error = (message) => window.errors.push(String(message));
            window.fetch = () => Promise.resolve({
                ok: true,
                json: () => Promise.resolve({
                    status: 'success',
                    formatted_row: {
                        local_port: 'Ethernet1',
                        remote_port: 'Ethernet2',
                        remote_device: 'remote',
                        cable_status: 'Connected',
                        actions: '<button>Sync</button>',
                        can_create_cable: true
                    }
                })
            });
            handleCableChange(document.getElementById('member-select'), '7');
        }"""
    )

    page.wait_for_function("document.getElementById('member-select')._lastVerifiedMember !== undefined")
    assert page.evaluate("document.getElementById('member-select')._lastVerifiedMember") == "7"
    assert page.locator('td[data-col="cable_status"]').inner_text() == "Connected"
    assert page.evaluate("window.errors") == []
    assert any('data-col="actions"' in warning for warning in page.evaluate("window.warnings"))
    assert page.evaluate("document.querySelector('input[name=\"select\"]').disabled") is False


def test_cable_verify_updates_every_cell_of_a_complete_row(page):
    """The guarded update must still replace each cell a rendered row carries."""
    page.set_content(_cable_row_html(with_actions_cell=True))
    _add_page_scripts(page)
    page.evaluate(
        """() => {
            window.warnings = [];
            console.warn = (message) => window.warnings.push(String(message));
            window.fetch = () => Promise.resolve({
                ok: true,
                json: () => Promise.resolve({
                    status: 'success',
                    formatted_row: {
                        local_port: 'Ethernet9',
                        remote_port: 'Ethernet8',
                        remote_device: 'other-remote',
                        cable_status: 'Connected',
                        actions: '<button id="new-action">Resync</button>',
                        can_create_cable: true
                    }
                })
            });
            handleCableChange(document.getElementById('member-select'), '7');
        }"""
    )

    page.wait_for_selector("#new-action")
    assert page.locator('td[data-col="local_port"]').inner_text() == "Ethernet9"
    assert page.locator('td[data-col="remote_port"]').inner_text() == "Ethernet8"
    assert page.locator('td[data-col="remote_device"]').inner_text() == "other-remote"
    assert page.locator('td[data-col="cable_status"]').inner_text() == "Connected"
    assert page.evaluate("window.warnings") == []


def test_refreshed_cable_picker_uses_the_persistent_htmx_loader(page):
    """A picker inserted by verification must work without a global HTMX object."""
    picker_url = "https://plugin.example.com/picker?row_id=row-1&server_key=primary"
    page.set_content(
        f"""
        <div id="cables">
          <span class="d-none" data-cable-picker-loader
                hx-get="https://plugin.example.com/picker"
                hx-trigger="librenms:open-cable-picker"
                hx-target="#htmx-modal-content" hx-swap="innerHTML"></span>
          {_cable_row_html(with_actions_cell=True)}
        </div>
        <div id="htmx-modal" aria-labelledby="htmx-modal-label">
          <span id="htmx-modal-label">Loading</span>
          <div id="htmx-modal-content"></div>
        </div>
        """
    )
    page.route(
        "https://plugin.example.com/picker?*",
        lambda route: route.fulfill(
            body='<div class="modal-header"><h5 class="modal-title">Pick remote end</h5></div>',
            content_type="text/html",
        ),
    )
    _add_page_scripts(page)
    page.evaluate(
        """
        pickerUrl => {
            document.dispatchEvent(new Event('DOMContentLoaded'));
            window.fetch = () => Promise.resolve({
                ok: true,
                json: () => Promise.resolve({
                    status: 'success',
                    formatted_row: {
                        local_port: 'Ethernet1',
                        remote_port: 'Ethernet2',
                        remote_device: 'remote',
                        cable_status: 'Connected',
                        actions: `<button id="refreshed-picker" type="button"
                                  data-cable-picker-url="${pickerUrl}">Pick remote end</button>`,
                        can_create_cable: true
                    }
                })
            });
            handleCableChange(document.getElementById('member-select'), '7');
        }
        """,
        picker_url,
    )
    page.wait_for_selector("#refreshed-picker")

    assert page.evaluate("typeof window.htmx") == "undefined"
    with page.expect_request("https://plugin.example.com/picker?*") as request_info:
        page.locator("#refreshed-picker").click()

    request = request_info.value
    assert request.url == picker_url
    assert request.headers.get("x-csrftoken") == "token"
    page.wait_for_selector("#htmx-modal-content .modal-title")
    expect(page.locator("#htmx-modal")).to_have_class(re.compile(r"(?:^|\s)show(?:\s|$)"))


@pytest.mark.parametrize("action", ["sync_one", "rebind_one", "create_vrf"])
def test_programmatic_single_row_submit_keeps_off_page_selection(page, action):
    """A real HTMX request with a row action needs no SubmitEvent to preserve bulk rows."""
    bodies = []

    def answer(route):
        bodies.append(route.request.post_data)
        route.fulfill(status=204)

    page.route(HTMX_SYNC_URL, answer)
    page.set_content(_htmx_sync_form())
    _add_page_scripts(page)
    page.evaluate("writeStoredSelection(document.querySelector('table'), {'101': {inputs: {}, auto: ''}})")
    page.evaluate(
        """action => htmxTest.ajax('POST', document.getElementById('sync-form').action, {
            source: document.getElementById('sync-form'),
            target: '#tab-content',
            values: {[action]: '201'}
        })""",
        action,
    )

    assert (action, "201") in _selection_form_pairs(bodies[0])
    assert ("select", "101") not in _selection_form_pairs(bodies[0])
    assert list(page.evaluate("readStoredSelection(document.querySelector('table'))")) == ["101"]


# ===========================================================================
# Table fit: a sync table must fit its card without a scroll wrapper
# ===========================================================================
# The netbox.css rules that set how wide a sync table can shrink.
NETBOX_TABLE_CSS = """
body { margin: 0; font: 14px/1.4 sans-serif; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { padding: .5rem; vertical-align: top; }
.table thead th { font-size: .625rem; text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; }
.badge { display: inline-flex; gap: .25rem; padding: .25em .5em; font-size: .857em; white-space: nowrap; }
.btn { display: inline-flex; padding: .25rem .5rem; white-space: nowrap; }
.d-none { display: none !important; }
.d-inline-block { display: inline-block !important; }
.text-wrap { white-space: normal !important; }
.text-break { word-wrap: break-word !important; word-break: break-word !important; }
.text-start { text-align: left !important; }
"""

# Cell markup in the shape the interface and module tables render, with the widest values seen live.
FIT_TABLES = {
    "librenms-interface-table": {
        "headers": [
            ("selection", ""),
            ("device_selection", "Virtual Chassis member"),
            ("name", "Name"),
            ("type", "Interface Type"),
            ("speed", "Speed"),
            ("vlans", "VLANs"),
            ("mac_address", "MAC Address"),
            ("mtu", "MTU"),
            ("enabled", "Enabled"),
            ("description", "Description"),
            ("librenms_id", "LibreNMS ID"),
            ("parent", "Relationships"),
            ("actions", ""),
        ],
        "cells": {
            "selection": '<input type="checkbox" name="select">',
            "device_selection": '<select class="form-select" style="width: 100%"><option>Select a member</option></select>',
            "name": '<span class="text-danger">2/x1/1/c36/1</span>',
            "type": '<span>softwareLoopback <i class="mdi mdi-help-circle-outline"></i></span>',
            "speed": "<span>100 Gbps</span>",
            "vlans": "<span>2759(T) <i class='mdi mdi-alert'></i></span>",
            "mac_address": "<span>02:00:00:CD:9F:07</span>",
            "mtu": "<span>9208</span>",
            "enabled": "<span>Disabled</span>",
            "description": "<span>uplink to core router</span>",
            "librenms_id": "<span>2005606206</span>",
            "parent": (
                '<div class="lh-sm"><span class="badge text-wrap text-start">1 stacked port</span></div>'
                '<div class="lh-sm"><span class="badge text-wrap text-start">LAG lag-2</span>'
                ' <button type="button" class="btn btn-sm">Sync</button></div>'
            ),
            "actions": '<div class="lh-sm"><span class="badge text-wrap text-start">Select\u00a0member</span></div>',
        },
        "keep": ["device_selection", "name", "parent", "actions"],
    },
    "librenms-module-table": {
        "headers": [
            ("selection", ""),
            ("device_selection", "Virtual Chassis Member"),
            ("name", "Name"),
            ("model", "Model"),
            ("serial", "Serial"),
            ("description", "Description"),
            ("item_class", "Class"),
            ("module_bay", "Module Bay"),
            ("module_type", "Module Type"),
            ("status", "Status"),
            ("actions", "Actions"),
        ],
        "cells": {
            "selection": '<input type="checkbox" name="select">',
            "device_selection": '<select class="form-select" style="width: 100%"><option>nokia1</option></select>',
            "name": '<span class="d-inline-block" style="padding-left:40px">└─ entity-c0151e 2/x1/1</span>',
            "model": "CFP2/QSFP28",
            "serial": "SN-7890dd",
            "description": "CFP2/QSFP28",
            "item_class": '<i class="mdi mdi-power-plug me-1"></i> powerSupply',
            "module_bay": '<span class="text-danger">No matching bay</span>',
            "module_type": '<span class="text-warning">No matching type</span>',
            "status": '<span class="badge text-wrap text-start">Integrated in entity-6d234b 2/x1</span>',
            "actions": '<button type="button" class="btn btn-sm">Install</button>',
        },
        "keep": ["device_selection", "name", "status", "actions"],
    },
}


def _render_fit_table(page, table_id, card_width):
    """Render one sync table inside a card of the given width, with the page script initialized."""
    spec = FIT_TABLES[table_id]
    head = "".join(f'<th class="text-wrap" data-col="{col}">{label}</th>' for col, label in spec["headers"])
    # The VC member column opts into the shared width class from librenms_sync.css, as the tables do.
    row = "".join(
        f'<td data-col="{col}" class="{"device-selection-col" if col == "device_selection" else ""}">'
        f"{spec['cells'][col]}</td>"
        for col, _label in spec["headers"]
    )
    page.set_content(
        f"<style>{NETBOX_TABLE_CSS}</style>"
        f'<div id="frame" style="width: {card_width}px"><div class="card">'
        f'<table class="table" id="{table_id}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{f'<tr>{row}</tr>' * 3}</tbody></table></div></div>"
    )
    page.add_style_tag(path=str(STYLE_PATH))
    _add_page_scripts(page)
    page.evaluate("initializeScripts()")


def _fit_state(page, table_id):
    """Return the table's overflow, the visible columns, and the fit note text."""
    return page.evaluate(
        """(tableId) => {
            const table = document.getElementById(tableId);
            const visible = (cell) => getComputedStyle(cell).display !== 'none';
            const note = table.parentElement.querySelector('[data-table-fit-note]');
            return {
                scrollWidth: table.scrollWidth,
                clientWidth: table.parentElement.clientWidth,
                headers: Array.from(table.tHead.rows[0].cells).filter(visible).map((th) => th.dataset.col),
                cells: Array.from(table.tBodies[0].rows[0].cells).filter(visible).map((td) => td.dataset.col),
                note: note && !note.hidden ? note.textContent : '',
            };
        }""",
        table_id,
    )


@pytest.mark.parametrize("table_id", sorted(FIT_TABLES))
@pytest.mark.parametrize("card_width", [943, 750, 687])
def test_sync_table_fits_its_card_at_narrow_widths(page, table_id, card_width):
    """The card widths are the live ones at 1280 px and 1024 px with the sidebar, and at 768 px without it."""
    _render_fit_table(page, table_id, card_width)

    state = _fit_state(page, table_id)
    assert state["scrollWidth"] <= state["clientWidth"] + 1, state
    assert state["headers"] == state["cells"], "a hidden header must hide its cells too"
    assert set(FIT_TABLES[table_id]["keep"]) <= set(state["cells"])
    hidden = [col for col, _label in FIT_TABLES[table_id]["headers"] if col not in state["cells"]]
    labels = dict(FIT_TABLES[table_id]["headers"])
    noted = re.fullmatch(r"Hidden to fit the window: (.+)\. Widen the window to show them\.", state["note"])
    assert sorted(noted.group(1).split(", ") if noted else []) == sorted(labels[col] for col in hidden)


@pytest.mark.parametrize("table_id", sorted(FIT_TABLES))
def test_sync_table_keeps_one_fit_note_when_a_notice_lands_before_the_table(page, table_id):
    _render_fit_table(page, table_id, 687)
    # The off-page selection notice and the cross-page notices are inserted directly before the table.
    page.evaluate(
        "(id) => { const n = document.createElement('div'); n.className = 'off-page-notice';"
        " document.getElementById(id).before(n); }",
        table_id,
    )

    page.evaluate("document.getElementById('frame').style.width = '2400px'")
    page.wait_for_function(
        "(n) => document.querySelectorAll('#frame tbody tr:first-child td:not(.d-none)').length === n",
        arg=len(FIT_TABLES[table_id]["headers"]),
    )
    assert page.evaluate("document.querySelectorAll('#frame [data-table-fit-note]:not([hidden])').length") == 0

    page.evaluate("document.getElementById('frame').style.width = '687px'")
    page.wait_for_function("() => !!document.querySelector('#frame [data-table-fit-note]:not([hidden])')")
    assert page.evaluate("document.querySelectorAll('#frame [data-table-fit-note]').length") == 1


@pytest.mark.parametrize("table_id", sorted(FIT_TABLES))
def test_sync_table_shows_hidden_columns_again_when_the_card_widens(page, table_id):
    _render_fit_table(page, table_id, 687)
    narrow = _fit_state(page, table_id)
    assert len(narrow["cells"]) < len(FIT_TABLES[table_id]["headers"])

    page.evaluate("document.getElementById('frame').style.width = '2400px'")
    page.wait_for_function(
        "(n) => document.querySelectorAll('#frame tbody tr:first-child td:not(.d-none)').length === n",
        arg=len(FIT_TABLES[table_id]["headers"]),
    )
    assert _fit_state(page, table_id)["note"] == ""

    page.evaluate("document.getElementById('frame').style.width = '687px'")
    page.wait_for_function("() => !!document.querySelector('#frame [data-table-fit-note]:not([hidden])')")
    state = _fit_state(page, table_id)
    assert state["cells"] == narrow["cells"]
    assert state["scrollWidth"] <= state["clientWidth"] + 1


# ===========================================================================
# Phone width: the cable, IP, VLAN and device info tables wrap to fit 375 px
# ===========================================================================
PHONE_WIDTH = 375

# The Bootstrap utilities and TomSelect bootstrap5 rules that set how far the phone fixtures can shrink.
PHONE_UTILITY_CSS = """
*, ::before, ::after { box-sizing: border-box; }
.container-fluid { padding: 0 .5rem; }
.card { border: 1px solid #ccc; }
.d-flex { display: flex !important; }
.align-items-center { align-items: center !important; }
.justify-content-between { justify-content: space-between !important; }
.gap-1 { gap: .25rem !important; }
.gap-2 { gap: .5rem !important; }
.ms-1 { margin-left: .25rem !important; }
.ms-2 { margin-left: .5rem !important; }
.ms-3 { margin-left: 1rem !important; }
.m-2 { margin: .5rem !important; }
.text-nowrap { white-space: nowrap !important; }
.btn { padding: .375rem .75rem; border: 1px solid; }
.form-select { display: block; width: 100%; padding: .375rem 2.25rem .375rem .75rem; font: inherit; }
.form-select-sm { padding: .25rem 2rem .25rem .5rem; }
.ts-hidden-accessible { position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important; padding: 0 !important; border: 0 !important; }
.ts-wrapper { position: relative; display: flex; }
.ts-wrapper.form-select { padding: 0 !important; }
.ts-control { display: flex; flex-wrap: wrap; width: 100%; overflow: hidden; padding: .375rem 2.25rem .375rem .75rem; }
.ts-control > input { flex: 1 1 auto; min-width: 7rem; max-width: 100% !important; padding: 0 !important; border: 0 !important; }
"""


def _tom_select(select_classes, item):
    """Return a select the way NetBox's TomSelect renders it: a wrapper with the select's classes."""
    return (
        f'<select class="{select_classes} tomselected ts-hidden-accessible"><option selected>{item}</option></select>'
        f'<div class="ts-wrapper {select_classes} single full has-items"><div class="ts-control">'
        f'<div class="item">{item}</div><input type="select-one" autocomplete="off" size="1"></div></div>'
    )


# The IP tab sets the VRF column widths in an inline style block; the phone rule must override it.
IP_VRF_CSS = re.search(r"<style>(.*?)</style>", (TEMPLATE_DIR / "_ipaddress_sync_content.html").read_text(), re.S)[1]

_CABLE_CELLS = (
    '<td data-col="selection"><input type="checkbox" name="select"></td>'
    '<td data-col="local_port"><a href="#">GigabitEthernet0/0/0/10.4094</a>'
    ' <span class="badge bg-purple text-white ms-1">OOB</span>'
    ' <span class="badge bg-teal text-dark ms-1">Serial</span></td>'
    '<td data-col="remote_port"><a href="#">TenGigabitEthernet1/1/48</a>'
    ' <i class="mdi mdi-gesture-tap-button text-muted"></i> <i class="mdi mdi-lan-connect text-muted"></i></td>'
    '<td data-col="remote_device"><a href="#">core-switch-02.dc-west.example.com</a></td>'
    '<td data-col="cable_status"><a href="#">Connected to a different interface</a></td>'
    '<td data-col="actions"><span class="badge bg-warning-lt fw-normal d-inline-flex align-items-center gap-1">'
    '<i class="mdi mdi-refresh"></i>Refresh needed</span>'
    '<button type="submit" class="btn btn-sm btn-primary" name="sync_one">Sync Cable</button>'
    '<button type="button" class="btn btn-sm btn-outline-primary"><i class="mdi mdi-plus-network"></i></button>'
    '<button type="button" class="btn btn-sm btn-outline-secondary"><i class="mdi mdi-connection"></i></button></td>'
)
_CABLE_HEAD = "<th></th><th>Local Port</th><th>Remote Port</th><th>Remote Device</th><th>Cable Status</th><th></th>"
_CABLE_TOOLBAR = (
    '<div class="noprint d-flex justify-content-between align-items-center mt-0 mb-3 sync-tab-primary-toolbar">'
    '<div><button type="submit" class="btn btn-primary"><span>Sync Selected Cables</span></button>'
    '<a href="#" class="m-2"><i class="mdi mdi-help-circle"></i> info</a></div></div>'
)

# Markup in the shape the real tables and templates render, with long values.
PHONE_TABLES = {
    "librenms-cable-table": {
        "toolbar": _CABLE_TOOLBAR,
        "table": '<table class="table table-hover object-list" id="librenms-cable-table">'
        f"<thead><tr>{_CABLE_HEAD}</tr></thead><tbody>{f'<tr>{_CABLE_CELLS}</tr>' * 3}</tbody></table>",
    },
    "librenms-cable-table-vc": {
        "toolbar": _CABLE_TOOLBAR,
        "table": '<table class="table table-hover object-list" id="librenms-cable-table-vc">'
        f"<thead><tr><th>Virtual Chassis Member</th>{_CABLE_HEAD}</tr></thead><tbody>"
        + (
            '<tr><td class="device-selection-col" data-col="device_selection">'
            f"{_tom_select('form-select', 'edge-router-01-member-2')}</td>"
            f"{_CABLE_CELLS}</tr>"
        )
        * 3
        + "</tbody></table>",
    },
    "librenms-ipaddress-table": {
        "toolbar": (
            '<div class="noprint d-flex align-items-center mt-0 mb-3 sync-tab-primary-toolbar">'
            '<div class="d-flex align-items-center gap-2">'
            '<button type="submit" class="btn btn-primary"><span>Sync Selected IP Addresses</span></button>'
            '<div class="dropdown" id="ip-sync-options"><button class="btn btn-outline-secondary dropdown-toggle"'
            ' type="button"><i class="mdi mdi-tune-variant"></i> Sync options'
            ' <span class="badge bg-secondary-lt ms-1">0</span></button></div></div></div>'
        ),
        "table": '<table class="table table-hover object-list" id="librenms-ipaddress-table">'
        "<thead><tr><th></th><th>IP Address</th><th>Prefix Length</th><th>Device</th><th>Interface</th>"
        "<th>VRF</th><th>Status</th></tr></thead><tbody>"
        + (
            '<tr><td data-col="selection"><input type="checkbox" name="select"></td>'
            '<td data-col="address"><a href="#">2001:db8:4100:ffff::1234</a></td>'
            '<td data-col="prefix">64</td>'
            '<td data-col="device"><a href="#">edge-router-01.dc-west.example.com</a></td>'
            '<td data-col="interface"><a href="#">GigabitEthernet0/0/0/10.4094</a></td>'
            '<td data-col="vrf"><div class="d-flex align-items-center gap-1">'
            f"{_tom_select('form-select vrf-select', 'customer-internet-transit')}"
            '<i class="mdi mdi-lightbulb-on-outline text-muted"></i>'
            '<button type="submit" class="btn btn-sm btn-outline-primary text-nowrap" name="create_vrf">'
            '<i class="mdi mdi-plus-thick" aria-hidden="true"></i> VRF</button></div></td>'
            '<td data-col="status"><button type="submit" class="btn btn-sm btn-primary" name="sync_one">'
            '<i class="mdi mdi-plus-thick" aria-hidden="true"></i> Create</button></td></tr>'
        )
        * 3
        + "</tbody></table>",
    },
    "librenms-vlan-table": {
        "toolbar": (
            '<div class="noprint d-flex align-items-center mt-0 mb-3 sync-tab-primary-toolbar">'
            '<div><button type="submit" class="btn btn-primary"><span>Sync Selected VLANs</span></button></div></div>'
        ),
        "table": '<table class="table table-hover object-list" id="librenms-vlan-table">'
        "<thead><tr><th></th><th>VLAN ID</th><th>Name</th><th>VLAN Group</th><th>Type</th><th>State</th>"
        "<th>Status</th></tr></thead><tbody>"
        + (
            '<tr data-vlan-id="4094"><td data-col="selection"><input type="checkbox" name="select"></td>'
            '<td data-col="vlan_id"><span class="text-danger">4094</span></td>'
            '<td data-col="name"><span class="text-danger">customer-internet-transit</span></td>'
            '<td data-col="vlan_group_selection">'
            f"{_tom_select('form-select form-select-sm vlan-sync-group-select', 'datacenter-west (dc-west)')}"
            '<i class="mdi mdi-alert text-warning ms-1"></i></td>'
            '<td data-col="type">ethernet</td>'
            '<td data-col="state"><span class="text-success">Active</span></td>'
            '<td data-col="status"><button type="submit" class="btn btn-sm btn-primary" name="sync_one">'
            '<i class="mdi mdi-plus-thick" aria-hidden="true"></i> Create</button></td></tr>'
        )
        * 3
        + "</tbody></table>",
    },
    "librenms-info-sync-table": {
        "toolbar": "",
        "table": '<table class="table table-sm table-hover mb-0 librenms-info-sync-table" id="librenms-info-sync-table">'
        '<thead><tr><th style="width: 20%; padding-left: 1rem;">Field</th><th style="width: 40%;">NetBox Value</th>'
        '<th style="width: 40%;">LibreNMS Value</th></tr></thead><tbody>'
        '<tr><td style="padding-left: 1rem;">Name <i class="mdi mdi-information-outline text-muted ms-1"></i></td>'
        '<td><div class="d-flex align-items-center"><div>edge-router-01.dc-west.example.com</div>'
        '<div class="ms-3"><form method="post" style="display: inline;">'
        '<button type="submit" class="btn btn-sm btn-outline-primary"><i class="mdi mdi-sync"></i> Sync to NetBox'
        "</button></form></div></div></td><td>edge-router-01.dc-west.example.com</td></tr>"
        '<tr><td style="padding-left: 1rem;">Device Type</td>'
        '<td><div class="d-flex align-items-center"><div><a href="#">Juniper Networks MX10003-PREMIUM</a></div>'
        '<div class="ms-3"><form method="post" style="display: inline;">'
        '<button type="submit" class="btn btn-sm btn-outline-danger"><i class="mdi mdi-sync"></i> Sync to NetBox'
        "</button></form></div></div></td>"
        '<td>MX10003-PREMIUM-CHASSIS <a href="#" class="btn btn-sm btn-outline-secondary ms-2">'
        '<i class="mdi mdi-open-in-new"></i> DeviceTypes</a></td></tr>'
        '<tr><td style="padding-left: 1rem;">Serial Number</td>'
        '<td><div class="d-flex align-items-center"><div>JN1234567890ABCDEF</div><div class="ms-3">'
        '<button type="button" class="btn btn-sm btn-outline-info"><i class="mdi mdi-server-network"></i>'
        " VC Serials (4)</button></div></div></td><td>JN1234567890ABCDEF</td></tr></tbody></table>",
    },
}


@pytest.mark.parametrize("table_id", sorted(PHONE_TABLES))
def test_sync_table_fits_the_page_at_phone_width(page, table_id):
    """The phone rules are a viewport media query, so the viewport itself is 375 px wide."""
    spec = PHONE_TABLES[table_id]
    page.set_viewport_size({"width": PHONE_WIDTH, "height": 800})
    page.set_content(
        f"<style>{NETBOX_TABLE_CSS}{PHONE_UTILITY_CSS}{IP_VRF_CSS}</style>"
        f'<div class="container-fluid"><div id="{table_id.removeprefix("librenms-")}-sync-content">'
        f'{spec["toolbar"]}<div class="card">{spec["table"]}</div></div></div>'
    )
    page.add_style_tag(path=str(STYLE_PATH))

    state = page.evaluate(
        """(tableId) => {
            const table = document.getElementById(tableId);
            const toolbar = document.querySelector('.sync-tab-primary-toolbar');
            return {
                page: document.documentElement.scrollWidth,
                table: table.scrollWidth,
                card: table.parentElement.clientWidth,
                toolbar: toolbar ? toolbar.scrollWidth : 0,
                toolbarBox: toolbar ? toolbar.clientWidth : 0,
            };
        }""",
        table_id,
    )
    assert state["page"] <= PHONE_WIDTH, state
    assert state["table"] <= state["card"] + 1, state
    assert state["toolbar"] <= state["toolbarBox"] + 1, state


def _load_netbox_only_controls(page):
    html = """<input name="csrfmiddlewaretoken" value="test-token">
      <input type="checkbox" id="select-all-netbox-interfaces">
      <div id="netbox-only-interfaces-rows">
        <input type="checkbox" class="netbox-interface-checkbox" value="7">
      </div>
      <button id="confirm-delete-interfaces">Delete</button>"""
    page.route(
        "https://plugin.example.com/dcim/devices/1/librenms-sync/",
        lambda route: route.fulfill(body=html, content_type="text/html"),
    )
    page.goto("https://plugin.example.com/dcim/devices/1/librenms-sync/")
    _add_page_scripts(page)
    page.evaluate("initializeScripts()")


def test_netbox_only_delete_sends_one_request_after_reinitialization(page):
    requests = []
    page.route(
        "**/delete-netbox-interfaces/",
        lambda route: (requests.append(route.request), route.fulfill(json={"status": "success"}))[-1],
    )
    _load_netbox_only_controls(page)
    page.route(
        "**/netbox-only-rows/",
        lambda route: route.fulfill(
            body='<input type="checkbox" class="netbox-interface-checkbox" value="7">',
            content_type="text/html",
        ),
    )
    page.evaluate("""async () => {
      await htmxTest.ajax('GET', '/netbox-only-rows/', {target: '#netbox-only-interfaces-rows'});
    }""")
    page.locator(".netbox-interface-checkbox").check()

    page.locator("#confirm-delete-interfaces").click()
    page.wait_for_load_state("networkidle")

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert 'name="interface_ids"\r\n\r\n7' in requests[0].post_data


def test_netbox_only_select_all_uses_only_the_current_rows_after_reinitialization(page):
    _load_netbox_only_controls(page)
    page.evaluate("""() => {
      window.oldNetboxCheckbox = document.querySelector('.netbox-interface-checkbox');
      document.getElementById('netbox-only-interfaces-rows').innerHTML =
        '<input type="checkbox" class="netbox-interface-checkbox" value="8">';
      initializeScripts();
    }""")

    page.locator("#select-all-netbox-interfaces").check()

    assert page.locator(".netbox-interface-checkbox").is_checked()
    assert page.evaluate("oldNetboxCheckbox.checked") is False


def test_vlan_modal_warning_icons_are_named_by_their_titles(page):
    """The missing-VLAN warnings carry their text only in the title, so it is also their accessible name."""
    vlans = [{"vid": 501, "type": "T", "missing": True}, {"vid": 502, "type": "T", "missing": False, "group_id": ""}]
    groups = [{"id": "", "name": "Global"}, {"id": 1, "name": "Group A"}]
    page.set_content(
        f"""
        <input name="csrfmiddlewaretoken" value="test-token">
        <span id="vlanModalInterfaceName"></span>
        <div id="vlanDetailModal"><table><tbody id="vlanDetailTableBody"></tbody></table></div>
        <button id="edit" data-interface="Ethernet1" data-row-key="port-1" data-device-id="7"
                data-vlans='{escape(json.dumps(vlans))}' data-vlan-groups='{escape(json.dumps(groups))}'>Edit</button>
        """
    )
    page.route(
        "**/plugins/librenms_plugin/verify-vlan-group/",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "success", "css_class": "text-danger", "is_missing": True}),
        ),
    )
    _add_page_scripts(page)

    page.evaluate("openVlanDetailModal(document.getElementById('edit'))")
    second = page.locator("#vlanDetailTableBody tr").nth(1)
    second.locator("select").select_option("1")
    expect(second.locator("i.mdi-alert")).to_have_count(1)

    icons = page.locator("#vlanDetailTableBody i[title]")
    expect(icons).to_have_count(2)
    for index in range(2):
        icon = icons.nth(index)
        assert icon.get_attribute("role") == "img"
        assert icon.get_attribute("aria-label") == icon.get_attribute("title")


NETBOX_ONLY_URL = "https://plugin.example.com/netbox-only-interfaces/?server_key=primary"


def _netbox_only_rows():
    from django.conf import settings
    from django.template import Context, Engine

    if not settings.configured:
        settings.configure(USE_I18N=False, USE_TZ=True)
    engine = Engine(dirs=[str(TEMPLATE_DIR.parent)])
    interfaces = [{"id": pk, "name": f"eth{pk}", "type": "Other", "enabled": True, "url": f"/i/{pk}/"} for pk in (1, 2)]
    return engine.get_template("netbox_librenms_plugin/inc/_netbox_only_interfaces.html").render(
        Context({"interface_sync": {"netbox_only_interfaces": interfaces}, "migrated_to_marker": None})
    )


def test_the_netbox_only_dialog_loads_its_rows_when_it_opens(page):
    requests = []
    page.route(
        NETBOX_ONLY_URL,
        lambda route: (requests.append(route.request.url), route.fulfill(body=_netbox_only_rows()))[-1],
    )
    page.set_content(
        f"""<div id="netboxOnlyInterfacesModal" style="display: none">
              <div id="netbox-only-interfaces-rows" hx-get="{NETBOX_ONLY_URL}" hx-trigger="intersect once"
                   hx-swap="innerHTML">Loading interfaces</div>
            </div>"""
    )
    _add_page_scripts(page)
    page.wait_for_timeout(300)
    # A closed dialog loads nothing: a filter render can leave thousands of rows out.
    assert requests == []

    page.locator("#netboxOnlyInterfacesModal").evaluate("dialog => { dialog.style.display = 'block'; }")

    expect(page.locator(".netbox-interface-checkbox")).to_have_count(2)
    page.check("#select-all-netbox-interfaces")
    expect(page.locator(".netbox-interface-checkbox:checked")).to_have_count(2)
    assert requests == [NETBOX_ONLY_URL]


@pytest.mark.parametrize("failure", ["not found", "network"])
def test_a_failed_load_of_the_netbox_only_rows_replaces_the_spinner(page, failure):
    # An expired snapshot answers 404, which htmx does not swap; a dropped connection answers nothing.
    page.route(
        NETBOX_ONLY_URL,
        lambda route: route.fulfill(status=404, body="gone") if failure == "not found" else route.abort(),
    )
    page.set_content(
        f"""<div id="netboxOnlyInterfacesModal" style="display: none">
              <div id="netbox-only-interfaces-rows" hx-get="{NETBOX_ONLY_URL}" hx-trigger="intersect once"
                   hx-swap="innerHTML"><div role="status"><span class="spinner-border"></span>Loading</div></div>
            </div>"""
    )
    _add_page_scripts(page)
    page.locator("#netboxOnlyInterfacesModal").evaluate("dialog => { dialog.style.display = 'block'; }")

    expect(page.locator("#netbox-only-interfaces-rows [role=alert]")).to_have_count(1)
    expect(page.locator("#netbox-only-interfaces-rows .spinner-border")).to_have_count(0)


def test_a_history_restore_keeps_one_set_of_page_listeners_on_the_restored_controller(page):
    """Back restores a new cache-state root; the page listeners must serve its controller, once."""
    initial = {"interfaces": _state("before-interfaces"), "ipaddresses": _state("before-ip")}
    link = (
        '<a id="go" hx-get="https://plugin.example.com/next" hx-push-url="https://plugin.example.com/page?next=1"'
        ' hx-target="#htmx-modal-content">Next</a>'
    )
    page.route(
        "https://plugin.example.com/page**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=f"<!doctype html><html><body>{_page_html(initial)}{link}</body></html>",
        ),
    )
    page.route(
        "https://plugin.example.com/next",
        lambda route: route.fulfill(status=200, content_type="text/html", body='<p id="next">next</p>'),
    )
    statuses = []
    page.route(
        "https://plugin.example.com/status?*",
        lambda route: (statuses.append(route.request.url), route.fulfill(json={"tabs": initial})),
    )
    page.goto("https://plugin.example.com/page?tab=interfaces")
    _add_page_scripts(page)
    page.evaluate("initializeScripts()")
    page.click("#go")
    page.wait_for_selector("#next")

    page.go_back()
    page.wait_for_selector("#go")
    # The restore can start its own status check; count only once no request has come for a while.
    seen = -1
    while seen != len(statuses):
        seen = len(statuses)
        page.wait_for_timeout(300)
        page.wait_for_function("syncCacheController() && syncCacheController().checking === null")
    statuses.clear()
    page.evaluate("window.dispatchEvent(new Event('blur')); window.dispatchEvent(new Event('focus'))")
    page.wait_for_timeout(500)
    assert len(statuses) == 1
    page.evaluate(
        "document.dispatchEvent(new CustomEvent('librenmsCacheChanged', {detail: {revisions: {interfaces: 'r-new'}}}))"
    )

    assert page.evaluate("syncCacheController().ownRevisions.has('r-new')")
