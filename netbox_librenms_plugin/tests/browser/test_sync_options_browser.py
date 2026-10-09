"""The interface and IP "Sync options" menus save the user's choice as preferences and keep it across a stale tab swap."""

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
TEMPLATE_DIR = Path(__file__).parents[2] / "templates" / "netbox_librenms_plugin"
SAVE_PREF_PATH = "/plugins/librenms_plugin/save-user-pref/"
HTMX_PATH = Path(__file__).parent / "vendor" / "htmx.min.js"
# NetBox has no `htmx` global, so the test publishes the API under another name.
HTMX_SCRIPT = f"window.htmxTest = (function () {{\n{HTMX_PATH.read_text()}\n; return htmx; }})();"
TAB_PATH = "/browser-sync-tab/"
CSRF_TOKEN = "browser-csrf-token"
COLUMNS = [
    ("name", "Name"),
    ("type", "Type"),
    ("speed", "Speed"),
    ("vlans", "VLANs"),
    ("mac_address", "MAC"),
    ("mtu", "MTU"),
    ("enabled", "Enabled"),
    ("description", "Description"),
]


def _render(template_name, context):
    """Render a real plugin template with ``{% url %}`` pointed at the fixture save URL."""
    from django.template import Context, Engine, Library

    urls = Library()

    @urls.simple_tag(name="url")
    def url(name):
        assert name == "plugins:netbox_librenms_plugin:save_user_pref"
        return SAVE_PREF_PATH

    engine = Engine()
    engine.template_builtins.append(urls)
    template = engine.from_string((TEMPLATE_DIR / template_name).read_text(encoding="utf-8"))
    return template.render(Context(context, use_l10n=False))


def _menu_markup(auto_select=True, excluded=()):
    """Render the interface menu with the context shape the interfaces tab view builds."""
    sync_options = {
        "auto_select_lag_members": auto_select,
        "auto_select_lag_members_default": True,
        "exclude_columns": [
            {"value": value, "label": label, "checked": value in excluded, "default_checked": False}
            for value, label in COLUMNS
        ],
    }
    return _render("inc/_interface_sync_options.html", {"sync_options": sync_options})


def _ip_menu_markup(set_primary=False, create_missing=False):
    """Render the IP menu with the context the IP tab view builds."""
    ip_sync = {"set_primary_ip": set_primary, "create_missing_interfaces": create_missing}
    return _render("inc/_ip_sync_options.html", {"ip_sync": ip_sync})


@pytest.fixture
def menu_page(page):
    """Load both menus and the real plugin script, and record every save-pref request."""
    saved = []

    def _answer(route):
        saved.append(route.request)
        route.fulfill(status=200, content_type="application/json", body='{"status": "ok"}')

    page.route(f"**{SAVE_PREF_PATH}", _answer)

    def _load(interface=None, ip=None):
        page.set_content(
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{CSRF_TOKEN}">'
            f"{_menu_markup(**(interface or {}))}{_ip_menu_markup(**(ip or {}))}"
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeScripts()")
        return saved

    return _load


def _settle(page, saved, count):
    """Wait until *count* saves arrived, then let any extra save arrive before the caller counts them."""
    for _ in range(250):
        if len(saved) >= count:
            break
        page.wait_for_timeout(20)
    # Saves run one after another, so an extra save would follow the last expected one.
    page.wait_for_timeout(200)


def _posted(saved):
    """Return each saved preference as ``(key, value)``, after checking it is a CSRF-protected POST."""
    pairs = []
    for request in saved:
        assert request.method == "POST"
        assert request.headers["x-csrftoken"] == CSRF_TOKEN
        payload = json.loads(request.post_data)
        pairs.append((payload["key"], payload["value"]))
    return pairs


def test_toggling_an_option_saves_the_whole_menu_once(page, menu_page):
    saved = menu_page()

    page.check("#exclude-mtu")
    _settle(page, saved, 1)

    assert _posted(saved) == [("interface_sync_options", {"auto_select_lag_members": True, "exclude_columns": ["mtu"]})]

    page.uncheck("#autoSelectLagMembers")
    _settle(page, saved, 2)

    assert _posted(saved)[1:] == [
        ("interface_sync_options", {"auto_select_lag_members": False, "exclude_columns": ["mtu"]})
    ]
    assert page.locator("#interface-sync-options-count").inner_text() == "2"


def test_reset_saves_the_factory_defaults_once(page, menu_page):
    saved = menu_page(interface={"auto_select": False, "excluded": ("name", "vlans", "description")})
    assert page.locator("#interface-sync-options-count").inner_text() == "4"

    page.click("#reset-interface-sync-options")
    _settle(page, saved, 1)

    assert _posted(saved) == [("interface_sync_options", {"auto_select_lag_members": True, "exclude_columns": []})]
    assert page.locator("#interface-sync-options-count").inner_text() == "0"


def test_an_ip_option_change_saves_only_its_own_key(page, menu_page):
    saved = menu_page()

    page.check("#create-missing-interfaces-toggle-cb")
    _settle(page, saved, 1)
    page.check("#set-primary-ip-toggle-cb")
    _settle(page, saved, 2)

    assert _posted(saved) == [("create_missing_interfaces", True), ("set_primary_ip", True)]
    assert page.locator("#ip-sync-options-count").inner_text() == "2"
    assert page.locator("#interface-sync-options-count").inner_text() == "0"


def test_ip_reset_saves_each_changed_key_once(page, menu_page):
    saved = menu_page(ip={"set_primary": True, "create_missing": True})

    page.click("#reset-ip-sync-options")
    _settle(page, saved, 2)

    assert _posted(saved) == [("set_primary_ip", False), ("create_missing_interfaces", False)]
    assert page.locator("#ip-sync-options-count").inner_text() == "0"


def test_ip_reset_does_not_save_an_unchanged_key(page, menu_page):
    saved = menu_page(ip={"create_missing": True})

    page.click("#reset-ip-sync-options")
    _settle(page, saved, 1)

    assert _posted(saved) == [("create_missing_interfaces", False)]


def _fail_saves(page, failure, count=1):
    """Make the first *count* save-pref requests fail with HTTP 500 or a network error, and record them."""
    failed = []

    def _fail(route):
        if len(failed) >= count:
            route.fallback()
            return
        failed.append(route.request)
        if failure == "abort":
            route.abort()
        else:
            route.fulfill(status=500, content_type="text/plain", body="Server Error")

    # Playwright runs the route registered last first, so this one sees each save before the fixture's.
    page.route(f"**{SAVE_PREF_PATH}", _fail)
    return failed


@pytest.mark.parametrize("failure", ["http_500", "abort"])
def test_a_failed_save_goes_again_with_the_next_change(page, menu_page, failure):
    saved = menu_page()
    failed = _fail_saves(page, failure)
    page.check("#create-missing-interfaces-toggle-cb")
    _settle(page, failed, 1)
    assert _posted(failed) == [("create_missing_interfaces", True)]

    page.check("#set-primary-ip-toggle-cb")
    _settle(page, saved, 2)

    assert _posted(saved) == [("set_primary_ip", True), ("create_missing_interfaces", True)]

    # The retry succeeded, so the next change does not send that key again.
    page.uncheck("#set-primary-ip-toggle-cb")
    _settle(page, saved, 3)

    assert _posted(saved)[2:] == [("set_primary_ip", False)]


def test_a_failed_save_that_the_user_replaced_is_not_sent_again(page, menu_page):
    saved = menu_page()
    failed = _fail_saves(page, "http_500")
    page.check("#create-missing-interfaces-toggle-cb")
    page.uncheck("#create-missing-interfaces-toggle-cb")
    _settle(page, saved, 1)

    page.check("#set-primary-ip-toggle-cb")
    _settle(page, saved, 2)

    assert _posted(failed) == [("create_missing_interfaces", True)]
    assert _posted(saved) == [("create_missing_interfaces", False), ("set_primary_ip", True)]


def _swap_menu(page, **menu_state):
    """Replace the menu as an HTMX tab swap does, with markup rendered from *menu_state*."""
    page.evaluate(
        """(html) => {
            document.getElementById('interface-sync-options').outerHTML = html;
            initializeScripts();
        }""",
        _menu_markup(**menu_state),
    )


def _checked(page, selector):
    return page.locator(selector).is_checked()


def test_a_swap_rendered_before_the_save_keeps_the_latest_choice(page, menu_page):
    saved = menu_page()
    page.check("#exclude-description")
    page.uncheck("#autoSelectLagMembers")
    _settle(page, saved, 2)

    # The sync response rendered the stored preference from before these saves.
    _swap_menu(page)
    _settle(page, saved, 2)

    assert _checked(page, "#exclude-description")
    assert not _checked(page, "#autoSelectLagMembers")
    assert page.locator("#interface-sync-options-count").inner_text() == "2"
    assert len(saved) == 2


def test_a_swap_rendered_before_a_reset_keeps_the_defaults(page, menu_page):
    saved = menu_page(interface={"excluded": ("name",)})
    page.click("#reset-interface-sync-options")
    _settle(page, saved, 1)

    _swap_menu(page, excluded=("name",))
    _settle(page, saved, 1)

    assert not _checked(page, "#exclude-name")
    assert page.locator("#interface-sync-options-count").inner_text() == "0"
    assert len(saved) == 1


@pytest.mark.parametrize("failure", ["http_500", "abort"])
def test_a_swap_sends_a_failed_save_again_with_the_latest_choice(page, menu_page, failure):
    saved = menu_page()
    failed = _fail_saves(page, failure, count=2)
    page.check("#exclude-mtu")
    page.check("#exclude-description")
    _settle(page, failed, 2)

    # The stale render predates both failed saves.
    _swap_menu(page)
    _settle(page, saved, 1)

    latest = {"auto_select_lag_members": True, "exclude_columns": ["mtu", "description"]}
    assert _posted(saved) == [("interface_sync_options", latest)]
    assert _checked(page, "#exclude-mtu")
    assert _checked(page, "#exclude-description")

    # The retry succeeded, so a second swap sends nothing.
    _swap_menu(page)
    _settle(page, saved, 2)

    assert len(saved) == 1


# A sub-interface (4302) and the parent it requires (4301).
TAB_ROWS = (
    '<tr data-port-id="4301"><td data-col="selection">'
    '<input type="checkbox" name="select" value="4301" id="cb-4301"></td><td>et-0/0/6</td></tr>'
    '<tr data-port-id="4302" data-parent-port-id="4301" data-parent-name="et-0/0/6"><td data-col="selection">'
    '<input type="checkbox" name="select" value="4302" id="cb-4302"></td><td>et-0/0/6.0</td></tr>'
)


def _tab_markup(**menu_state):
    """The swappable interface tab: the menu and a table whose rows the server renders unchecked."""
    return (
        f"{_menu_markup(**menu_state)}"
        '<table id="librenms-interface-table"><thead><tr><th><input type="checkbox" class="toggle"></th>'
        f"<th>Name</th></tr></thead><tbody>{TAB_ROWS}</tbody></table>"
    )


# The management IP row, which "Set Primary IP" ticks, and another row.
IP_ROWS = (
    '<tr data-mgmt-ip="true"><td data-col="selection">'
    '<input type="checkbox" name="select" value="198.18.0.1/24" id="ip-mgmt"></td><td>198.18.0.1/24</td></tr>'
    '<tr><td data-col="selection">'
    '<input type="checkbox" name="select" value="198.18.0.2/24" id="ip-other"></td><td>198.18.0.2/24</td></tr>'
)


def _ip_tab_markup(**menu_state):
    """The swappable IP tab: the menu and a table whose rows the server renders unchecked."""
    return (
        f"{_ip_menu_markup(**menu_state)}"
        '<table id="librenms-ipaddress-table"><thead><tr><th><input type="checkbox" class="toggle"></th>'
        f"<th>Address</th></tr></thead><tbody>{IP_ROWS}</tbody></table>"
    )


@pytest.fixture
def swap_page(page):
    """Serve a sync tab and let a real HTMX swap run the production initializer."""
    saved = []

    def _answer(route):
        saved.append(route.request)
        route.fulfill(status=200, content_type="application/json", body='{"status": "ok"}')

    page.route(f"**{SAVE_PREF_PATH}", _answer)
    stale = {}
    page.route(f"**{TAB_PATH}", lambda route: route.fulfill(status=200, content_type="text/html", body=stale["html"]))

    def _load(tab_html):
        page.set_content(
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{CSRF_TOKEN}">'
            '<input type="hidden" name="server_key" value="production">'
            f'<div id="tab-content">{tab_html}</div>'
        )
        page.add_script_tag(content=HTMX_SCRIPT)
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeScripts()")
        return saved

    def _swap(tab_html, menu_id):
        stale["html"] = tab_html
        page.evaluate("url => htmxTest.ajax('GET', url, {target: '#tab-content', swap: 'innerHTML'})", TAB_PATH)
        page.wait_for_function(f"document.getElementById('{menu_id}').dataset.initialized === 'true'")

    return _load, _swap


def _selected(page):
    return set(page.evaluate("Array.from(document.querySelectorAll('input[name=select]:checked')).map(cb => cb.value)"))


def test_a_stale_swap_does_not_pull_in_a_parent_the_user_turned_auto_select_off_for(page, swap_page):
    load, swap = swap_page
    load(_tab_markup())
    page.uncheck("#autoSelectLagMembers")
    page.check("#cb-4302")
    assert _selected(page) == {"4302"}

    # The swap renders the stored preference from before the save: auto-select on.
    swap(_tab_markup(), "interface-sync-options")

    assert not _checked(page, "#autoSelectLagMembers")
    assert _selected(page) == {"4302"}


def test_a_stale_swap_keeps_the_parent_after_reset_turned_auto_select_on(page, swap_page):
    load, swap = swap_page
    load(_tab_markup(auto_select=False))
    page.click("#reset-interface-sync-options")
    page.check("#cb-4302")
    assert _selected(page) == {"4301", "4302"}

    # The swap renders the stored preference from before the Reset: auto-select off.
    swap(_tab_markup(auto_select=False), "interface-sync-options")

    assert _checked(page, "#autoSelectLagMembers")
    assert _selected(page) == {"4301", "4302"}


def test_a_stale_ip_swap_keeps_the_switches_and_the_management_row(page, swap_page):
    load, swap = swap_page
    saved = load(_ip_tab_markup())
    page.check("#set-primary-ip-toggle-cb")
    page.check("#create-missing-interfaces-toggle-cb")
    _settle(page, saved, 2)
    assert _checked(page, "#ip-mgmt")

    # The swap renders the stored preferences from before these saves: both off.
    swap(_ip_tab_markup(), "ip-sync-options")
    _settle(page, saved, 2)

    assert _checked(page, "#set-primary-ip-toggle-cb")
    assert _checked(page, "#create-missing-interfaces-toggle-cb")
    assert _checked(page, "#ip-mgmt")
    assert not _checked(page, "#ip-other")
    assert page.locator("#ip-sync-options-count").inner_text() == "2"
    assert len(saved) == 2


def test_a_stale_ip_swap_after_reset_keeps_the_switches_off(page, swap_page):
    load, swap = swap_page
    saved = load(_ip_tab_markup(set_primary=True, create_missing=True))
    assert _checked(page, "#ip-mgmt")
    page.click("#reset-ip-sync-options")
    _settle(page, saved, 2)
    assert not _checked(page, "#ip-mgmt")

    # The swap renders the stored preferences from before the Reset: both on.
    swap(_ip_tab_markup(set_primary=True, create_missing=True), "ip-sync-options")
    _settle(page, saved, 2)

    assert not _checked(page, "#set-primary-ip-toggle-cb")
    assert not _checked(page, "#create-missing-interfaces-toggle-cb")
    assert not _checked(page, "#ip-mgmt")
    assert page.locator("#ip-sync-options-count").inner_text() == "0"
    assert len(saved) == 2


IP_SUBMIT_PATH = "/browser-sync-ip/"
# A second page of the IP table: no management row on it.
IP_PAGE_TWO_ROWS = (
    '<tr><td data-col="selection">'
    '<input type="checkbox" name="select" value="198.18.0.3/24" id="ip-third"></td><td>198.18.0.3/24</td></tr>'
)


def _ip_form_markup(rows, **menu_state):
    """One page of the IP tab, with the table inside the form that submits the selection."""
    return (
        f"{_ip_menu_markup(**menu_state)}"
        f'<form id="ip-sync-form" method="post" action="{IP_SUBMIT_PATH}">'
        # The server names the management row on every page, also where the row is not rendered.
        """<table id="librenms-ipaddress-table" data-mgmt-rows='["198.18.0.1/24"]'>"""
        '<thead><tr><th><input type="checkbox" class="toggle"></th>'
        f'<th>Address</th></tr></thead><tbody>{rows}</tbody></table><button type="submit" id="ip-submit">Sync</button>'
        "</form>"
    )


def _submitted_selection(page):
    """Submit the IP form and return the posted ``select`` values."""
    posted = []
    page.route(
        f"**{IP_SUBMIT_PATH}",
        lambda route: (posted.append(route.request.post_data), route.fulfill(status=200, body="ok")),
    )
    page.click("#ip-submit")
    page.wait_for_function("document.body.textContent.trim() === 'ok'")
    return sorted(parse_qs(posted[0])["select"])


def test_turning_set_primary_ip_off_drops_the_off_page_management_row(page, swap_page):
    load, swap = swap_page
    load(_ip_form_markup(IP_ROWS, set_primary=True))
    assert _checked(page, "#ip-mgmt")
    page.check("#ip-other")

    swap(_ip_form_markup(IP_PAGE_TWO_ROWS, set_primary=True), "ip-sync-options")
    page.check("#ip-third")
    page.uncheck("#set-primary-ip-toggle-cb")

    assert _submitted_selection(page) == ["198.18.0.2/24", "198.18.0.3/24"]


def test_turning_set_primary_ip_on_selects_the_off_page_management_row(page, swap_page):
    load, swap = swap_page
    load(_ip_form_markup(IP_ROWS))
    assert not _checked(page, "#ip-mgmt")

    swap(_ip_form_markup(IP_PAGE_TWO_ROWS), "ip-sync-options")
    page.check("#ip-third")
    page.check("#set-primary-ip-toggle-cb")

    assert page.locator("#librenms-ipaddress-table-offpage-selection span").inner_text() == (
        "1 more row is selected on another page."
    )
    assert _submitted_selection(page) == ["198.18.0.1/24", "198.18.0.3/24"]


def test_a_saved_set_primary_ip_selects_the_management_row_from_another_page(page, swap_page):
    load, _swap = swap_page
    load(_ip_form_markup(IP_PAGE_TWO_ROWS, set_primary=True))
    page.check("#ip-third")

    assert _submitted_selection(page) == ["198.18.0.1/24", "198.18.0.3/24"]
