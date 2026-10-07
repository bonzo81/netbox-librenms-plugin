"""The interface "Sync options" menu is a per-user preference: saved through the real endpoint, rendered on every tab render."""

import json
import re
from copy import deepcopy

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory
from django.urls import reverse
from netbox.config import get_config

from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device
from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

PREFERENCE = "plugins.netbox_librenms_plugin.interface_sync_options"
SERVER_KEY = "default"
PORT_ID = 7100
FACTORY_STATE = {"auto_select_lag_members": True, "exclude_columns": []}
ALL_COLUMNS = ["name", "type", "speed", "vlans", "mac_address", "mtu", "enabled", "description"]


def _user(username):
    return get_user_model().objects.create(username=username, is_superuser=True, is_active=True)


def _stored(user):
    return get_user_model().objects.get(pk=user.pk).config.get(PREFERENCE)


def _save(client, value):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:save_user_pref"),
        data=json.dumps({"key": "interface_sync_options", "value": value}),
        content_type="application/json",
    )


def _store_raw(user, value):
    user.config.data = deepcopy(user.config.data)
    user.config.set(PREFERENCE, value, commit=True)


def _seed(device):
    port = {
        "port_id": PORT_ID,
        "ifName": "eth0",
        "ifDescr": "eth0",
        "ifType": "ethernetCsmacd",
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": "",
    }
    payload = {"ports": [port], "port_stack_relationships": {}}
    cache.set(SyncInterfacesView().get_cache_key(device, "ports", SERVER_KEY), payload, timeout=300)


def _tab(client, device):
    """Render the interfaces tab through the fragment the sync page loads."""
    response = client.get(
        reverse(
            "plugins:netbox_librenms_plugin:sync_cache_fragment",
            kwargs={"object_type": "device", "pk": device.pk, "tab": "interfaces"},
        ),
        {"server_key": SERVER_KEY, "interface_name_field": "ifName"},
    )
    assert response.status_code == 200
    return response.content.decode()


def _menu_inputs(html):
    start = html.index('id="interface-sync-options"')
    end = html.index('id="reset-interface-sync-options"', start)
    tags = re.findall(r"<input[^>]*\binterface-sync-option\b[^>]*>", html[start:end])
    assert len(tags) == 1 + len(ALL_COLUMNS)
    return [
        {
            "name": re.search(r'name="([^"]+)"', tag).group(1),
            "value": re.search(r'value="([^"]+)"', tag).group(1),
            "checked": re.search(r"\schecked(?=[\s>])", tag) is not None,
            "default": re.search(r'data-default-checked="(true|false)"', tag).group(1) == "true",
        }
        for tag in tags
    ]


def _menu_state(html):
    """Read the rendered menu back in the shape of the stored preference."""
    inputs = _menu_inputs(html)
    (switch,) = [tag for tag in inputs if tag["name"] == "auto_select_lag_members"]
    return {
        "auto_select_lag_members": switch["checked"],
        "exclude_columns": [tag["value"] for tag in inputs if tag["name"] == "exclude_columns" and tag["checked"]],
    }


def _factory_defaults(html):
    inputs = _menu_inputs(html)
    return {
        "auto_select_lag_members": next(tag["default"] for tag in inputs if tag["name"] == "auto_select_lag_members"),
        "exclude_columns": [tag["value"] for tag in inputs if tag["name"] == "exclude_columns" and tag["default"]],
    }


@pytest.mark.django_db
class TestSavingTheMenu:
    def test_a_valid_choice_is_stored_in_menu_order(self, client):
        user = _user("sync-options-save")
        client.force_login(user)

        response = _save(client, {"exclude_columns": ["mtu", "name"], "auto_select_lag_members": False})

        assert response.status_code == 200
        assert _stored(user) == {"auto_select_lag_members": False, "exclude_columns": ["name", "mtu"]}

    def test_every_column_can_be_excluded(self, client):
        user = _user("sync-options-save-all")
        client.force_login(user)

        assert _save(client, {"auto_select_lag_members": True, "exclude_columns": ALL_COLUMNS[::-1]}).status_code == 200
        assert _stored(user) == {"auto_select_lag_members": True, "exclude_columns": ALL_COLUMNS}

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "name",
            ["name"],
            {},
            {"exclude_columns": []},
            {"auto_select_lag_members": True},
            {"auto_select_lag_members": True, "exclude_columns": [], "extra": 1},
            {"auto_select_lag_members": "false", "exclude_columns": []},
            {"auto_select_lag_members": 1, "exclude_columns": []},
            {"auto_select_lag_members": None, "exclude_columns": []},
            {"auto_select_lag_members": True, "exclude_columns": "name"},
            {"auto_select_lag_members": True, "exclude_columns": {"name": True}},
            {"auto_select_lag_members": True, "exclude_columns": ["name", "name"]},
            {"auto_select_lag_members": True, "exclude_columns": ["ifAlias"]},
            {"auto_select_lag_members": True, "exclude_columns": [1]},
            {"auto_select_lag_members": True, "exclude_columns": [["name"]]},
        ],
    )
    def test_an_invalid_value_is_rejected_and_nothing_is_stored(self, client, value):
        user = _user("sync-options-reject")
        client.force_login(user)

        response = _save(client, value)

        assert response.status_code == 400
        assert _stored(user) is None

    def test_one_user_s_choice_does_not_reach_another_user(self):
        # A new user's in-memory config holds NetBox's shared DEFAULT_USER_PREFERENCES dict itself.
        chooser = _user("sync-options-chooser")
        defaults_before = deepcopy(get_config().DEFAULT_USER_PREFERENCES)
        request = RequestFactory().post(
            reverse("plugins:netbox_librenms_plugin:save_user_pref"),
            data=json.dumps(
                {"key": "interface_sync_options", "value": {**FACTORY_STATE, "exclude_columns": ["vlans"]}}
            ),
            content_type="application/json",
        )
        request.user = chooser

        assert SaveUserPrefView.as_view()(request).status_code == 200

        assert _stored(chooser) == {"auto_select_lag_members": True, "exclude_columns": ["vlans"]}
        assert get_config().DEFAULT_USER_PREFERENCES == defaults_before
        assert _stored(_user("sync-options-later")) is None


@pytest.mark.django_db
class TestRenderingTheMenu:
    @pytest.fixture(autouse=True)
    def _server(self, settings):
        configure_default_librenms_server(settings)

    def _device(self, name):
        device = make_device(name, librenms_cf={SERVER_KEY: {"id": 91}})
        _seed(device)
        return device

    def test_without_a_choice_the_menu_shows_the_factory_defaults(self, client):
        device = self._device("sync-options-none")
        client.force_login(_user("sync-options-none-user"))

        html = _tab(client, device)

        assert _menu_state(html) == FACTORY_STATE
        assert _factory_defaults(html) == FACTORY_STATE

    def test_the_tab_renders_the_saved_choice(self, client):
        device = self._device("sync-options-saved")
        client.force_login(_user("sync-options-saved-user"))
        saved = {"auto_select_lag_members": False, "exclude_columns": ["type", "vlans", "description"]}
        assert _save(client, saved).status_code == 200

        html = _tab(client, device)

        assert _menu_state(html) == saved
        # The badge and Reset still compare against the factory defaults.
        assert _factory_defaults(html) == FACTORY_STATE

    @pytest.mark.parametrize(
        "corrupt",
        [
            "name",
            {"auto_select_lag_members": "no", "exclude_columns": ["name"]},
            {"auto_select_lag_members": False, "exclude_columns": ["name", "gone"]},
            {"auto_select_lag_members": False},
        ],
    )
    def test_a_corrupt_stored_value_renders_the_factory_defaults(self, client, corrupt):
        device = self._device("sync-options-corrupt")
        user = _user("sync-options-corrupt-user")
        _store_raw(user, corrupt)
        client.force_login(user)

        assert _menu_state(_tab(client, device)) == FACTORY_STATE

    def test_the_tab_swapped_in_after_a_sync_keeps_the_saved_choice(self, client):
        device = self._device("sync-options-swap")
        client.force_login(_user("sync-options-swap-user"))
        saved = {"auto_select_lag_members": False, "exclude_columns": ["mac_address", "mtu"]}
        assert _save(client, saved).status_code == 200

        response = client.post(
            reverse(
                "plugins:netbox_librenms_plugin:sync_selected_interfaces",
                kwargs={"object_type": "device", "object_id": device.pk},
            )
            + "?interface_name_field=ifName",
            {"server_key": SERVER_KEY, "select": [str(PORT_ID)], "exclude_columns": ["mac_address", "mtu"]},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        html = response.content.decode()
        assert "Selected interfaces synced successfully." in html
        assert _menu_state(html) == saved

    def test_a_saved_choice_is_not_shown_to_another_user(self, client):
        device = self._device("sync-options-other")
        client.force_login(_user("sync-options-owner"))
        assert _save(client, {"auto_select_lag_members": False, "exclude_columns": ["name"]}).status_code == 200

        client.force_login(_user("sync-options-viewer"))

        assert _menu_state(_tab(client, device)) == FACTORY_STATE
