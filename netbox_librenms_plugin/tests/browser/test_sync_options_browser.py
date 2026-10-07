"""The interface "Sync options" menu saves its whole state as a user preference; the IP menu saves nothing."""

import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
MENU_TEMPLATE = (
    Path(__file__).parents[2] / "templates" / "netbox_librenms_plugin" / "inc" / "_interface_sync_options.html"
)
SAVE_PREF_PATH = "/plugins/librenms_plugin/save-user-pref/"
HTMX_PATH = Path(__file__).parent / "vendor" / "htmx.min.js"
# NetBox has no `htmx` global, so the test publishes the API under another name.
HTMX_SCRIPT = f"window.htmxTest = (function () {{\n{HTMX_PATH.read_text()}\n; return htmx; }})();"
TAB_PATH = "/browser-interface-tab/"
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

# The URL is present so only the script's wiring keeps the IP menu from saving.
IP_MENU = f"""
<div class="dropdown" id="ip-sync-options" data-save-pref-url="{SAVE_PREF_PATH}">
  <input class="ip-sync-option" type="checkbox" id="ip-option" data-default-checked="false">
  <span id="ip-sync-options-count">0</span>
  <button type="button" id="reset-ip-sync-options">Reset</button>
</div>
"""


def _menu_markup(auto_select=True, excluded=()):
    """Render the real menu include with the context shape the interfaces tab view builds."""
    from django.template import Context, Engine, Library

    urls = Library()

    @urls.simple_tag(name="url")
    def url(name):
        assert name == "plugins:netbox_librenms_plugin:save_user_pref"
        return SAVE_PREF_PATH

    engine = Engine()
    engine.template_builtins.append(urls)
    sync_options = {
        "auto_select_lag_members": auto_select,
        "auto_select_lag_members_default": True,
        "exclude_columns": [
            {"value": value, "label": label, "checked": value in excluded, "default_checked": False}
            for value, label in COLUMNS
        ],
    }
    template = engine.from_string(MENU_TEMPLATE.read_text(encoding="utf-8"))
    return template.render(Context({"sync_options": sync_options}, use_l10n=False))


@pytest.fixture
def menu_page(page):
    """Load the menu and the real plugin script, and record every save-pref request."""
    saved = []

    def _answer(route):
        saved.append(route.request)
        route.fulfill(status=200, content_type="application/json", body='{"status": "ok"}')

    page.route(f"**{SAVE_PREF_PATH}", _answer)

    def _load(**menu_state):
        page.set_content(
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{CSRF_TOKEN}">'
            f"{_menu_markup(**menu_state)}{IP_MENU}"
        )
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeSyncOptionMenus()")
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


def _posted_value(request):
    assert request.method == "POST"
    assert request.headers["x-csrftoken"] == CSRF_TOKEN
    payload = json.loads(request.post_data)
    assert payload["key"] == "interface_sync_options"
    return payload["value"]


def test_toggling_an_option_saves_the_whole_menu_once(page, menu_page):
    saved = menu_page()

    page.check("#exclude-mtu")
    _settle(page, saved, 1)

    assert [_posted_value(request) for request in saved] == [
        {"auto_select_lag_members": True, "exclude_columns": ["mtu"]}
    ]

    page.uncheck("#autoSelectLagMembers")
    _settle(page, saved, 2)

    assert [_posted_value(request) for request in saved][1:] == [
        {"auto_select_lag_members": False, "exclude_columns": ["mtu"]}
    ]
    assert page.locator("#interface-sync-options-count").inner_text() == "2"


def test_reset_saves_the_factory_defaults_once(page, menu_page):
    saved = menu_page(auto_select=False, excluded=("name", "vlans", "description"))
    assert page.locator("#interface-sync-options-count").inner_text() == "4"

    page.click("#reset-interface-sync-options")
    _settle(page, saved, 1)

    assert [_posted_value(request) for request in saved] == [{"auto_select_lag_members": True, "exclude_columns": []}]
    assert page.locator("#interface-sync-options-count").inner_text() == "0"


def test_the_ip_menu_saves_nothing(page, menu_page):
    saved = menu_page()

    page.check("#ip-option")
    page.click("#reset-ip-sync-options")
    _settle(page, saved, 0)

    assert saved == []
    assert page.locator("#ip-sync-options-count").inner_text() == "0"


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
    saved = menu_page(excluded=("name",))
    page.click("#reset-interface-sync-options")
    _settle(page, saved, 1)

    _swap_menu(page, excluded=("name",))
    _settle(page, saved, 1)

    assert not _checked(page, "#exclude-name")
    assert page.locator("#interface-sync-options-count").inner_text() == "0"
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


@pytest.fixture
def tab_page(page):
    """Serve the interface tab and let a real HTMX swap run the production initializer."""
    page.route(f"**{SAVE_PREF_PATH}", lambda route: route.fulfill(status=200, body='{"status": "ok"}'))
    stale = {}
    page.route(f"**{TAB_PATH}", lambda route: route.fulfill(status=200, content_type="text/html", body=stale["html"]))

    def _load(**menu_state):
        page.set_content(
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{CSRF_TOKEN}">'
            '<input type="hidden" name="server_key" value="production">'
            f'<div id="interface-sync-content">{_tab_markup(**menu_state)}</div>'
        )
        page.add_script_tag(content=HTMX_SCRIPT)
        page.add_script_tag(path=str(SCRIPT_PATH))
        page.evaluate("initializeScripts()")

    def _swap(**menu_state):
        stale["html"] = _tab_markup(**menu_state)
        page.evaluate(
            "url => htmxTest.ajax('GET', url, {target: '#interface-sync-content', swap: 'innerHTML'})", TAB_PATH
        )
        page.wait_for_function("document.getElementById('interface-sync-options').dataset.initialized === 'true'")

    return _load, _swap


def _selected(page):
    return set(page.evaluate("Array.from(document.querySelectorAll('input[name=select]:checked')).map(cb => cb.value)"))


def test_a_stale_swap_does_not_pull_in_a_parent_the_user_turned_auto_select_off_for(page, tab_page):
    load, swap = tab_page
    load()
    page.uncheck("#autoSelectLagMembers")
    page.check("#cb-4302")
    assert _selected(page) == {"4302"}

    # The swap renders the stored preference from before the save: auto-select on.
    swap()

    assert not _checked(page, "#autoSelectLagMembers")
    assert _selected(page) == {"4302"}


def test_a_stale_swap_keeps_the_parent_after_reset_turned_auto_select_on(page, tab_page):
    load, swap = tab_page
    load(auto_select=False)
    page.click("#reset-interface-sync-options")
    page.check("#cb-4302")
    assert _selected(page) == {"4301", "4302"}

    # The swap renders the stored preference from before the Reset: auto-select off.
    swap(auto_select=False)

    assert _checked(page, "#autoSelectLagMembers")
    assert _selected(page) == {"4301", "4302"}
