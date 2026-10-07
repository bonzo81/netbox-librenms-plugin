"""The interface "Sync options" menu saves its whole state as a user preference; the IP menu saves nothing."""

import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[2] / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
MENU_TEMPLATE = (
    Path(__file__).parents[2] / "templates" / "netbox_librenms_plugin" / "inc" / "_interface_sync_options.html"
)
SAVE_PREF_PATH = "/plugins/librenms_plugin/save-user-pref/"
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
