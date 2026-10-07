"""The interface and IP "Sync options" menus are per-user preferences: saved through the real endpoint, rendered on every tab render."""

import json
import re
from copy import deepcopy

import pytest
from dcim.models import Device, Interface
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory
from django.urls import reverse
from netbox.config import get_config

from netbox_librenms_plugin.tests._html_helpers import open_tags
from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device
from netbox_librenms_plugin.tests.test_ip_address_sync_safety import _configure_test_server, _serve_librenms_ip_rows
from netbox_librenms_plugin.utils import resolve_create_missing_interfaces, resolve_set_primary_ip
from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
from netbox_librenms_plugin.views.sync.ip_addresses import SyncIPAddressesView

PREFERENCE = "plugins.netbox_librenms_plugin.interface_sync_options"
SERVER_KEY = "default"
PORT_ID = 7100
FACTORY_STATE = {"auto_select_lag_members": True, "exclude_columns": []}
ALL_COLUMNS = ["name", "type", "speed", "vlans", "mac_address", "mtu", "enabled", "description"]


def _user(username):
    return get_user_model().objects.create(username=username, is_superuser=True, is_active=True)


def _stored(user, path=PREFERENCE):
    return get_user_model().objects.get(pk=user.pk).config.get(path)


def _save(client, value, key="interface_sync_options"):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:save_user_pref"),
        data=json.dumps({"key": key, "value": value}),
        content_type="application/json",
    )


def _store_raw(user, value, path=PREFERENCE):
    user.config.data = deepcopy(user.config.data)
    user.config.set(path, value, commit=True)


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


# =============================================================================
# IP tab "Sync options": Set Primary IP and Create missing interfaces
# =============================================================================
IP_PREFERENCES = {
    "set_primary_ip": "plugins.netbox_librenms_plugin.set_primary_ip",
    "create_missing_interfaces": "plugins.netbox_librenms_plugin.create_missing_interfaces",
}
IP_TOGGLES = {
    "set_primary_ip": ("set-primary-ip-toggle", resolve_set_primary_ip),
    "create_missing_interfaces": ("create-missing-interfaces-toggle", resolve_create_missing_interfaces),
}


def _request(user, data=None):
    request = RequestFactory().post("/sync/", data or {})
    request.user = get_user_model().objects.get(pk=user.pk)
    return request


@pytest.mark.django_db
class TestIPOptionPreferences:
    @pytest.mark.parametrize("key", IP_PREFERENCES)
    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_is_stored(self, client, key, value):
        user = _user(f"ip-option-save-{key}-{value}")
        client.force_login(user)

        assert _save(client, value, key).status_code == 200
        assert _stored(user, IP_PREFERENCES[key]) is value

    @pytest.mark.parametrize("key", IP_PREFERENCES)
    @pytest.mark.parametrize("value", [None, "true", "on", 1, 0, [], {}])
    def test_a_non_boolean_is_rejected_and_nothing_is_stored(self, client, key, value):
        user = _user(f"ip-option-reject-{key}")
        client.force_login(user)

        assert _save(client, value, key).status_code == 400
        assert _stored(user, IP_PREFERENCES[key]) is None

    @pytest.mark.parametrize("key", IP_PREFERENCES)
    @pytest.mark.parametrize(("saved", "posted", "expected"), [(True, "off", False), (False, "on", True)])
    def test_the_request_value_beats_the_saved_choice(self, key, saved, posted, expected):
        user = _user(f"ip-option-request-{key}")
        _store_raw(user, saved, IP_PREFERENCES[key])
        field, resolve = IP_TOGGLES[key]

        assert resolve(_request(user, {field: posted})) is expected

    @pytest.mark.parametrize("key", IP_PREFERENCES)
    @pytest.mark.parametrize("saved", [True, False])
    def test_the_saved_choice_beats_the_default(self, key, saved):
        user = _user(f"ip-option-saved-{key}")
        _store_raw(user, saved, IP_PREFERENCES[key])

        assert IP_TOGGLES[key][1](_request(user)) is saved

    @pytest.mark.parametrize("key", IP_PREFERENCES)
    @pytest.mark.parametrize("corrupt", ["true", "on", 1, ["x"], {"x": True}])
    def test_a_corrupt_saved_value_resolves_to_the_default(self, key, corrupt):
        user = _user(f"ip-option-corrupt-{key}")
        _store_raw(user, corrupt, IP_PREFERENCES[key])

        assert IP_TOGGLES[key][1](_request(user)) is False

    @pytest.mark.parametrize(("saved", "posted", "owner_action"), [(True, None, "change"), (True, "off", "view")])
    def test_a_saved_primary_ip_choice_sets_the_owner_permission_only_without_a_posted_toggle(
        self, saved, posted, owner_action
    ):
        user = _user("ip-option-permission")
        _store_raw(user, saved, IP_PREFERENCES["set_primary_ip"])
        data = {} if posted is None else {"set-primary-ip-toggle": posted}
        view = SyncIPAddressesView()
        view.request = _request(user, data)

        assert SyncIPAddressesView._owner_action(view.request) == owner_action
        assert (("change", Device) in view._required_permissions("device")["POST"]) is (owner_action == "change")

    @pytest.mark.parametrize(("saved", "posted", "required"), [(True, None, True), (True, "off", False)])
    def test_a_saved_create_missing_choice_sets_the_interface_permissions_only_without_a_posted_toggle(
        self, saved, posted, required
    ):
        user = _user("ip-option-create-permission")
        _store_raw(user, saved, IP_PREFERENCES["create_missing_interfaces"])
        data = {} if posted is None else {"create-missing-interfaces-toggle": posted}
        view = SyncIPAddressesView()
        view.request = _request(user, data)

        assert (("add", Interface) in view._required_permissions("device")["POST"]) is required


def _ip_toggles(html):
    """Return the rendered checked state of the IP menu switches."""
    switches = {
        attributes["id"]: "checked" in attributes
        for attributes in open_tags(html, "input")
        if attributes.get("id") in ("set-primary-ip-toggle-cb", "create-missing-interfaces-toggle-cb")
    }
    assert len(switches) == 2
    return {
        "set_primary_ip": switches["set-primary-ip-toggle-cb"],
        "create_missing_interfaces": switches["create-missing-interfaces-toggle-cb"],
    }


@pytest.mark.django_db
class TestRenderingTheIPMenu:
    """The refresh and the fragment load both render the saved switches; a corrupt value renders off."""

    def _refresh(self, client, device, live_librenms, data=None):
        rows = [{"address": "198.18.41.10", "prefix_length": 24, "port_id": 7041, "interface": "Ethernet1"}]
        _serve_librenms_ip_rows(live_librenms.server, rows, device_name=device.name)
        response = client.post(
            reverse("plugins:netbox_librenms_plugin:device_ipaddress_sync", args=[device.pk]),
            {"server_key": SERVER_KEY, "interface_name_field": "ifName", **(data or {})},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        return response.content.decode()

    def _fragment(self, client, device):
        response = client.get(
            reverse(
                "plugins:netbox_librenms_plugin:sync_cache_fragment",
                kwargs={"object_type": "device", "pk": device.pk, "tab": "ipaddresses"},
            ),
            {"server_key": SERVER_KEY},
        )
        assert response.status_code == 200
        return response.content.decode()

    @pytest.mark.parametrize(
        "saved",
        [
            {"set_primary_ip": True, "create_missing_interfaces": True},
            {"set_primary_ip": True, "create_missing_interfaces": False},
            {"set_primary_ip": False, "create_missing_interfaces": True},
        ],
    )
    def test_the_saved_switches_render_on_refresh_and_on_the_fragment_load(
        self, client, settings, live_librenms, saved
    ):
        _configure_test_server(settings)
        device = make_device("ip-option-render", librenms_cf={SERVER_KEY: {"id": 42}})
        client.force_login(_user("ip-option-render-user"))
        for key, value in saved.items():
            assert _save(client, value, key).status_code == 200

        assert _ip_toggles(self._refresh(client, device, live_librenms)) == saved
        assert _ip_toggles(self._fragment(client, device)) == saved

    def test_without_a_choice_and_with_a_corrupt_one_the_switches_render_off(self, client, settings, live_librenms):
        _configure_test_server(settings)
        device = make_device("ip-option-render-off", librenms_cf={SERVER_KEY: {"id": 42}})
        user = _user("ip-option-render-off-user")
        client.force_login(user)
        off = {"set_primary_ip": False, "create_missing_interfaces": False}

        assert _ip_toggles(self._refresh(client, device, live_librenms)) == off

        for path in IP_PREFERENCES.values():
            _store_raw(user, "on", path)
        assert _ip_toggles(self._fragment(client, device)) == off

    def test_a_posted_toggle_beats_the_saved_choice_on_refresh(self, client, settings, live_librenms):
        _configure_test_server(settings)
        device = make_device("ip-option-render-posted", librenms_cf={SERVER_KEY: {"id": 42}})
        client.force_login(_user("ip-option-render-posted-user"))
        assert _save(client, True, "create_missing_interfaces").status_code == 200

        html = self._refresh(client, device, live_librenms, {"create-missing-interfaces-toggle": "off"})

        assert _ip_toggles(html)["create_missing_interfaces"] is False
