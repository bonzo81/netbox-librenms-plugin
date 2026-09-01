"""Tests for LibreNMS API, cache, and redirect view mixins."""

from copy import deepcopy

import pytest
from django.conf import settings
from django.test import RequestFactory, override_settings

from netbox_librenms_plugin.tests.conftest import make_device, make_vm


pytestmark = pytest.mark.django_db


def _server_settings(servers, **legacy):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin = dict(plugin_config.get("netbox_librenms_plugin", {}))
    plugin["servers"] = servers
    plugin.update(legacy)
    plugin_config["netbox_librenms_plugin"] = plugin
    return override_settings(PLUGINS_CONFIG=plugin_config)


class TestLibreNMSAPIMixinLazyInit:
    @staticmethod
    def _mixin():
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = None
        return mixin

    def test_starts_with_none(self):
        assert self._mixin()._librenms_api is None

    def test_first_access_creates_real_instance(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        assert isinstance(self._mixin().librenms_api, LibreNMSAPI)

    def test_second_access_returns_same_instance(self):
        mixin = self._mixin()
        assert mixin.librenms_api is mixin.librenms_api

    def test_librenms_api_is_property_descriptor(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        assert isinstance(LibreNMSAPIMixin.__dict__["librenms_api"], property)


class TestLibreNMSAPIMixinGetServerInfo:
    @staticmethod
    def _mixin():
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        return object.__new__(LibreNMSAPIMixin)

    def test_multi_server_returns_display_name_and_url(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        servers = {"default": {"librenms_url": "https://librenms.example.test", "api_token": "token"}}
        with _server_settings(servers):
            mixin = self._mixin()
            mixin._librenms_api = LibreNMSAPI(server_key="default")
            info = mixin.get_server_info()

        assert info == {
            "display_name": "default",
            "url": "https://librenms.example.test",
            "is_legacy": False,
            "server_key": "default",
        }

    def test_legacy_config_sets_is_legacy_true(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with _server_settings({}, librenms_url="https://legacy.example.test", api_token="token"):
            mixin = self._mixin()
            mixin._librenms_api = LibreNMSAPI()
            info = mixin.get_server_info()

        assert info["is_legacy"] is True
        assert info["url"] == "https://legacy.example.test"

    def test_returns_error_info_for_unbuildable_config(self):
        with _server_settings({}, librenms_url="", api_token=""):
            mixin = self._mixin()
            mixin._librenms_api = None
            info = mixin.get_server_info()

        assert info["is_legacy"] is True
        assert info["url"] == "Configuration error"


class TestCacheMixinKeyGeneration:
    @staticmethod
    def _mixin():
        from netbox_librenms_plugin.views.mixins import CacheMixin

        return CacheMixin()

    def test_get_cache_key_format(self):
        device = make_device("cache-key-format")
        assert self._mixin().get_cache_key(device, "ports") == f"librenms_ports_device_{device.pk}"

    def test_get_cache_key_includes_server_key(self):
        device = make_device("cache-key-server")
        assert self._mixin().get_cache_key(device, "ports", "srv1") == f"librenms_ports_device_{device.pk}_srv1"

    def test_get_cache_key_includes_model_name(self):
        vm = make_vm("cache-key-vm")
        assert self._mixin().get_cache_key(vm, "interfaces") == f"librenms_interfaces_virtualmachine_{vm.pk}"

    def test_get_cache_key_different_data_types(self):
        device = make_device("cache-key-types")
        mixin = self._mixin()
        assert mixin.get_cache_key(device, "ports", "prod") != mixin.get_cache_key(device, "ips", "prod")

    def test_get_last_fetched_key_format_and_scope(self):
        device = make_device("cache-last-fetched")
        mixin = self._mixin()
        assert mixin.get_last_fetched_key(device, "ports") == f"librenms_ports_last_fetched_device_{device.pk}"
        assert mixin.get_last_fetched_key(device, "ports", "srv1") == (
            f"librenms_ports_last_fetched_device_{device.pk}_srv1"
        )

    def test_cache_key_different_pks_differ(self):
        first = make_device("cache-key-first")
        second = make_device("cache-key-second")
        mixin = self._mixin()
        assert mixin.get_cache_key(first, "ports") != mixin.get_cache_key(second, "ports")

    def test_vlan_overrides_key_is_distinct_and_server_scoped(self):
        device = make_device("cache-vlan-overrides")
        mixin = self._mixin()
        bare = mixin.get_vlan_overrides_key(device)
        scoped = mixin.get_vlan_overrides_key(device, "prod")
        assert bare == f"librenms_vlan_group_overrides_device_{device.pk}"
        assert scoped == f"{bare}_prod"
        assert bare != mixin.get_cache_key(device, "vlans")


class TestRedirectWithServerKey:
    @staticmethod
    def _request():
        return RequestFactory().get("/", HTTP_HOST="testserver")

    @pytest.mark.parametrize(
        ("url", "server_key", "expected"),
        [
            ("/sync/1/", "prod", "/sync/1/?server_key=prod"),
            ("/sync/1/?tab=ports", "prod", "/sync/1/?tab=ports&server_key=prod"),
            ("/sync/1/", None, "/sync/1/"),
            ("/sync/1/", "", "/sync/1/"),
        ],
    )
    def test_safe_redirects(self, url, server_key, expected):
        from netbox_librenms_plugin.views.mixins import redirect_with_server_key

        assert redirect_with_server_key(self._request(), url, server_key).url == expected

    def test_external_candidate_fails_open_redirect_barrier(self):
        from netbox_librenms_plugin.views.mixins import redirect_with_server_key

        response = redirect_with_server_key(self._request(), "https://attacker.invalid/path", "prod")

        assert response.url == "https://attacker.invalid/path"
        assert "server_key" not in response.url


class TestResolveConfiguredServerKey:
    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [("siteB", "siteB"), ("ghost", None), (["siteB"], None), (None, None), ("", None)],
    )
    def test_only_configured_string_key_is_returned(self, candidate, expected):
        from netbox_librenms_plugin.views.mixins import resolve_configured_server_key

        servers = {"siteB": {"librenms_url": "https://siteb.example.test", "api_token": "token"}}
        with _server_settings(servers):
            assert resolve_configured_server_key(candidate) == expected
