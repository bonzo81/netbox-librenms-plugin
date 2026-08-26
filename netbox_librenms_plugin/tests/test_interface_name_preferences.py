"""Integration tests for interface-name preference behavior on sync pages."""

import json
from copy import deepcopy

import pytest
from dcim.models import Platform
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
from netbox_librenms_plugin.utils import get_interface_name_field, mark_librenms_migrated


GLOBAL_PREFERENCE = "plugins.netbox_librenms_plugin.interface_name_field"
PLATFORM_PREFERENCES = "plugins.netbox_librenms_plugin.interface_name_fields_by_platform"


def _selector_tag(html):
    """Return the opening selector tag from rendered HTML."""
    marker = 'id="interface-name-field-selector"'
    position = html.index(marker)
    return html[html.rfind("<div", 0, position) : html.index(">", position) + 1]


@pytest.mark.django_db
@pytest.mark.parametrize(("tab", "hidden"), [("interfaces", False), ("modules", True), ("vlans", True)])
def test_interface_name_selector_visibility_follows_the_active_sync_tab(client, settings, tab, hidden):
    """The full sync page shows the selector only where interface names affect matching."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "default": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"}
    }
    settings.PLUGINS_CONFIG = plugin_config
    platform = Platform.objects.create(name="Selector Platform", slug="selector-platform")
    device = make_device("selector-device")
    device.platform = platform
    device.save(update_fields=["platform"])
    winner = make_device("selector-winner")
    mark_librenms_migrated(device, winner.pk, "default")
    device.save(update_fields=["custom_field_data"])
    user = make_superuser("selector-visibility-user")
    client.force_login(user)

    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": tab},
    )

    assert response.status_code == 200
    selector = _selector_tag(response.content.decode())
    assert ("d-none" in selector.split('class="', 1)[1].split('"', 1)[0].split()) is hidden
    assert f'data-platform-id="{platform.pk}"' in selector


@pytest.mark.django_db
def test_sync_tab_links_replace_the_server_rendered_region(client, settings):
    """Tab links must work normally and enhance the same navigation through HTMX."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "default": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"}
    }
    settings.PLUGINS_CONFIG = plugin_config
    device = make_device("htmx-sync-tabs")
    winner = make_device("htmx-sync-tabs-winner")
    mark_librenms_migrated(device, winner.pk, "default")
    device.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser("htmx-sync-tabs-user"))

    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": "ipaddresses", "server_key": "default", "interface_name_field": "ifDescr"},
    )

    assert response.status_code == 200
    html = response.content.decode()
    marker = 'id="ipaddresses-tab"'
    position = html.index(marker)
    link = html[html.rfind("<a", 0, position) : html.index(">", position) + 1]
    expected_query = "?tab=ipaddresses&amp;server_key=default&amp;interface_name_field=ifDescr"
    assert f'href="{response.request["PATH_INFO"]}{expected_query}"' in link
    assert f'hx-get="{response.request["PATH_INFO"]}{expected_query}"' in link
    assert 'hx-target="#librenms-sync-tabs"' in link
    assert 'hx-select="#librenms-sync-tabs"' in link
    assert 'hx-swap="outerHTML"' in link
    assert 'hx-push-url="true"' in link
    assert 'hx-sync="#librenms-sync-tabs:replace"' in link
    assert 'hx-include="#interface-name-field-selector"' in link
    assert 'class="nav-link active"' in link
    assert 'aria-selected="true"' in link


@pytest.mark.django_db
@pytest.mark.parametrize("tab", ["interfaces", "ipaddresses"])
def test_the_swapped_tab_region_carries_the_active_tab_marker(client, settings, tab):
    """activeSyncTab() reads data-active-tab off the swapped container, so the swap must replace it."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "default": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"}
    }
    settings.PLUGINS_CONFIG = plugin_config
    device = make_device(f"active-tab-marker-{tab}")
    winner = make_device(f"active-tab-marker-winner-{tab}")
    mark_librenms_migrated(device, winner.pk, "default")
    device.save(update_fields=["custom_field_data"])
    client.force_login(make_superuser(f"active-tab-marker-user-{tab}"))

    response = client.get(
        reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
        {"tab": tab, "server_key": "default"},
    )

    assert response.status_code == 200
    html = response.content.decode()
    position = html.index('id="librenms-sync-tabs"')
    region = html[html.rfind("<div", 0, position) : html.index(">", position) + 1]

    # The marker sits on the element hx-select/hx-target name, so an innerHTML swap would
    # leave the previous tab's value behind and activeSyncTab() would report the wrong tab.
    assert f'data-active-tab="{tab}"' in region


def _post_preference(client, value, platform_id=None):
    """Save one interface-name preference through the public JSON endpoint."""
    payload = {"key": "interface_name_field", "value": value}
    if platform_id is not None:
        payload["platform_id"] = platform_id
    return client.post(
        reverse("plugins:netbox_librenms_plugin:save_user_pref"),
        data=json.dumps(payload),
        content_type="application/json",
    )


def _request_for(user):
    """Build a real request whose user configuration is read from the database."""
    request = RequestFactory().get("/")
    request.user = type(user).objects.get(pk=user.pk)
    return request


@pytest.mark.django_db
def test_interface_name_choice_is_saved_per_user_and_platform(client):
    """A platform choice overrides the user's global choice only on that platform."""
    LibreNMSSettings.objects.create(remember_interface_name_per_platform=True)
    cisco = Platform.objects.create(name="Vendor A", slug="vendor-a")
    nokia = Platform.objects.create(name="Vendor B", slug="vendor-b")
    cisco_device = make_device("platform-a-device")
    cisco_device.platform = cisco
    cisco_device.save(update_fields=["platform"])
    nokia_device = make_device("platform-b-device")
    nokia_device.platform = nokia
    nokia_device.save(update_fields=["platform"])
    user = make_superuser("platform-interface-name-user")
    client.force_login(user)

    assert _post_preference(client, "ifName").status_code == 200
    assert _post_preference(client, "ifDescr", cisco.pk).status_code == 200

    request = _request_for(user)
    assert get_interface_name_field(request, cisco_device) == "ifDescr"
    assert get_interface_name_field(request, nokia_device) == "ifName"
    assert request.user.config.get(GLOBAL_PREFERENCE) == "ifName"
    assert request.user.config.get(PLATFORM_PREFERENCES) == {str(cisco.pk): "ifDescr"}


@pytest.mark.django_db
def test_disabling_platform_memory_keeps_using_the_global_user_preference(client):
    """The installation setting can opt out of platform-specific preference storage."""
    settings = LibreNMSSettings.objects.create(remember_interface_name_per_platform=True)
    platform = Platform.objects.create(name="Vendor C", slug="vendor-c")
    device = make_device("platform-c-device")
    device.platform = platform
    device.save(update_fields=["platform"])
    user = make_superuser("global-interface-name-user")
    client.force_login(user)

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:settings"),
        {
            "form_type": "import_settings",
            "vc_member_name_pattern": "-M{position}",
            "use_sysname_default": "on",
        },
    )

    assert response.status_code == 302
    settings.refresh_from_db()
    assert settings.remember_interface_name_per_platform is False
    assert _post_preference(client, "ifDescr", platform.pk).status_code == 200
    request = _request_for(user)
    assert get_interface_name_field(request, device) == "ifDescr"
    assert request.user.config.get(GLOBAL_PREFERENCE) == "ifDescr"
    assert request.user.config.get(PLATFORM_PREFERENCES) is None


@pytest.mark.django_db
def test_platform_memory_requires_installation_opt_in():
    """New installations use the existing global user preference until enabled."""
    settings = LibreNMSSettings.objects.create()

    assert settings.remember_interface_name_per_platform is False


@pytest.mark.django_db
def test_settings_page_tracks_platform_memory_changes(client):
    """Changing the platform-memory switch enables the import settings save button."""
    user = make_superuser("platform-memory-settings-user")
    client.force_login(user)

    response = client.get(reverse("plugins:netbox_librenms_plugin:settings"))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'name="remember_interface_name_per_platform"' in html
    assert 'id="id_remember_interface_name_per_platform"' in html
    assert 'id="save-import-btn"' in html
    # One narrow marker keeps the switch wired to the save-button handler.
    assert "rememberInterfaceNameCheckbox.addEventListener('change', updateImportSaveButton)" in html


@pytest.mark.django_db
def test_interface_name_preference_rejects_unknown_values(client):
    """The preference endpoint accepts only values rendered by the selector."""
    user = make_superuser("invalid-interface-name-user")
    client.force_login(user)

    response = _post_preference(client, "ifAlias")

    assert response.status_code == 400
    assert type(user).objects.get(pk=user.pk).config.get(GLOBAL_PREFERENCE) is None


@pytest.mark.django_db
@pytest.mark.parametrize("value", [["ifName"], {"field": "ifName"}])
def test_interface_name_preference_rejects_non_string_values(client, value):
    """The JSON endpoint must reject containers before set membership checks."""
    user = make_superuser("non-string-interface-name-user")
    client.force_login(user)

    response = _post_preference(client, value)

    assert response.status_code == 400
    assert type(user).objects.get(pk=user.pk).config.get(GLOBAL_PREFERENCE) is None


@pytest.mark.django_db
def test_malformed_stored_interface_name_preferences_fall_back_safely():
    """Malformed user JSON must not break the interface sync page preference reader."""
    LibreNMSSettings.objects.create(remember_interface_name_per_platform=True)
    platform = Platform.objects.create(name="Malformed Preference Platform", slug="malformed-preference-platform")
    device = make_device("malformed-preference-device")
    device.platform = platform
    device.save(update_fields=["platform"])
    user = make_superuser("malformed-interface-name-reader")
    user.config.set(GLOBAL_PREFERENCE, "ifName", commit=False)
    user.config.set(PLATFORM_PREFERENCES, {str(platform.pk): {"field": "ifDescr"}}, commit=True)

    assert get_interface_name_field(_request_for(user), device) == "ifName"
