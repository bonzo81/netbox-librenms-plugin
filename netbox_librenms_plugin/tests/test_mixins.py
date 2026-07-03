"""Tests for view mixins: LibreNMSAPIMixin and CacheMixin."""

from unittest.mock import MagicMock, patch


class TestLibreNMSAPIMixinLazyInit:
    """LibreNMSAPIMixin.librenms_api is lazy — not created until first access."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = None
        return mixin

    def test_starts_with_none(self):
        mixin = self._make_mixin()
        assert mixin._librenms_api is None

    def test_first_access_creates_instance(self):
        mixin = self._make_mixin()
        fake_api = MagicMock()

        with patch("netbox_librenms_plugin.views.mixins.LibreNMSAPI", return_value=fake_api):
            api = mixin.librenms_api

        assert api is fake_api

    def test_second_access_returns_same_instance(self):
        mixin = self._make_mixin()
        fake_api = MagicMock()

        with patch("netbox_librenms_plugin.views.mixins.LibreNMSAPI", return_value=fake_api) as mock_cls:
            api1 = mixin.librenms_api
            api2 = mixin.librenms_api

        assert api1 is api2
        mock_cls.assert_called_once()  # constructor called only once

    def test_librenms_api_is_property_descriptor(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        assert isinstance(LibreNMSAPIMixin.__dict__["librenms_api"], property)


class TestLibreNMSAPIMixinGetServerInfo:
    """get_server_info() returns correct structure for multi-server and legacy configs."""

    def _make_mixin_with_api(self, server_key="default"):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        mixin = object.__new__(LibreNMSAPIMixin)
        fake_api = MagicMock()
        fake_api.server_key = server_key
        mixin._librenms_api = fake_api
        return mixin

    def test_multi_server_returns_display_name_and_url(self, mock_multi_server_config):
        mixin = self._make_mixin_with_api("default")

        with patch("netbox.plugins.get_plugin_config") as mock_config:
            mock_config.side_effect = lambda _plugin, key: mock_multi_server_config if key == "servers" else None
            info = mixin.get_server_info()

        assert info["display_name"] == "default"  # falls back to server_key (no display_name in fixture)
        assert info["url"] == mock_multi_server_config["default"]["librenms_url"]
        assert info["is_legacy"] is False
        assert info["server_key"] == "default"

    def test_legacy_config_sets_is_legacy_true(self, mock_legacy_config):
        mixin = self._make_mixin_with_api("default")

        def mock_plugin_config(_plugin, key):
            if key == "servers":
                return None
            if key == "librenms_url":
                return mock_legacy_config["librenms_url"]
            return None

        with patch("netbox.plugins.get_plugin_config", side_effect=mock_plugin_config):
            info = mixin.get_server_info()

        assert info["is_legacy"] is True
        assert info["url"] == mock_legacy_config["librenms_url"]

    def test_returns_error_info_on_exception(self):
        mixin = self._make_mixin_with_api("default")

        with patch("netbox.plugins.get_plugin_config", side_effect=ImportError):
            info = mixin.get_server_info()

        assert "is_legacy" in info
        assert info["is_legacy"] is True


class TestCacheMixinKeyGeneration:
    """CacheMixin generates consistent, predictable cache keys."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import CacheMixin

        return object.__new__(CacheMixin)

    def test_get_cache_key_format(self):
        mixin = self._make_mixin()
        obj = MagicMock()
        obj._meta.model_name = "device"
        obj.pk = 5

        key = mixin.get_cache_key(obj, "ports")
        assert key == "librenms_ports_device_5"

    def test_get_cache_key_includes_server_key(self):
        """
        Cache keys must be namespaced per server so two servers' data never collide.

        Without server_key isolation a second server's stale ports list could be
        returned to the wrong sync session.
        """
        mixin = self._make_mixin()
        obj = MagicMock()
        obj._meta.model_name = "device"
        obj.pk = 5

        key = mixin.get_cache_key(obj, "ports", server_key="srv1")
        assert "srv1" in key
        assert key == "librenms_ports_device_5_srv1"

    def test_get_cache_key_includes_model_name(self):
        mixin = self._make_mixin()
        obj = MagicMock()
        obj._meta.model_name = "virtualmachine"
        obj.pk = 10

        key = mixin.get_cache_key(obj, "interfaces")
        assert "virtualmachine" in key
        assert "10" in key

    def test_get_cache_key_different_data_types(self):
        mixin = self._make_mixin()
        obj = MagicMock()
        obj._meta.model_name = "device"
        obj.pk = 1

        key_ports = mixin.get_cache_key(obj, "ports", server_key="prod")
        key_ips = mixin.get_cache_key(obj, "ips", server_key="prod")
        assert key_ports != key_ips

    def test_get_last_fetched_key_format(self):
        mixin = self._make_mixin()
        obj = MagicMock()
        obj._meta.model_name = "device"
        obj.pk = 3

        key = mixin.get_last_fetched_key(obj, "ports")
        assert key == "librenms_ports_last_fetched_device_3"  # exact string

    def test_get_last_fetched_key_includes_server_key(self):
        """
        The last-fetched timestamp key must also be server-scoped.

        If two servers share the same key the cache countdown would reflect the
        wrong server's fetch time.
        """
        mixin = self._make_mixin()
        obj = MagicMock()
        obj._meta.model_name = "device"
        obj.pk = 3

        key = mixin.get_last_fetched_key(obj, "ports", server_key="srv1")
        assert key == "librenms_ports_last_fetched_device_3_srv1"  # exact string

    def test_cache_key_different_pks_differ(self):
        mixin = self._make_mixin()
        obj1 = MagicMock()
        obj1._meta.model_name = "device"
        obj1.pk = 1

        obj2 = MagicMock()
        obj2._meta.model_name = "device"
        obj2.pk = 2

        assert mixin.get_cache_key(obj1, "ports") != mixin.get_cache_key(obj2, "ports")

    def test_get_vlan_overrides_key_exists_and_differs_from_data_key(self):
        """VLAN group overrides use a separate cache key from the VLAN data key."""
        mixin = self._make_mixin()
        obj = MagicMock()
        obj._meta.model_name = "device"
        obj.pk = 7

        vlan_key = mixin.get_vlan_overrides_key(obj)
        assert vlan_key == "librenms_vlan_group_overrides_device_7"
        data_key = mixin.get_cache_key(obj, "vlans")
        assert vlan_key != data_key

    def test_get_vlan_overrides_key_server_scoped(self):
        """VLAN overrides key includes server_key to avoid cross-server leakage."""
        mixin = self._make_mixin()
        obj = MagicMock()
        obj._meta.model_name = "device"
        obj.pk = 7

        key_no_server = mixin.get_vlan_overrides_key(obj)
        key_with_server = mixin.get_vlan_overrides_key(obj, server_key="prod")
        assert key_with_server == "librenms_vlan_group_overrides_device_7_prod"
        assert key_no_server != key_with_server


class TestRedirectWithServerKey:
    """redirect_with_server_key appends a validated server_key, gated by the open-redirect barrier."""

    def _request(self):
        from django.test import RequestFactory

        return RequestFactory().get("/")

    def test_appends_server_key_to_safe_url(self):
        from netbox_librenms_plugin.views.mixins import redirect_with_server_key

        resp = redirect_with_server_key(self._request(), "/sync/1/", "prod")
        assert resp.url == "/sync/1/?server_key=prod"

    def test_uses_ampersand_when_url_already_has_query(self):
        from netbox_librenms_plugin.views.mixins import redirect_with_server_key

        resp = redirect_with_server_key(self._request(), "/sync/1/?tab=ports", "prod")
        assert resp.url == "/sync/1/?tab=ports&server_key=prod"

    def test_blank_or_none_server_key_redirects_to_bare_url(self):
        from netbox_librenms_plugin.views.mixins import redirect_with_server_key

        for key in (None, ""):
            resp = redirect_with_server_key(self._request(), "/sync/1/", key)
            assert resp.url == "/sync/1/"

    def test_server_key_dropped_when_candidate_fails_open_redirect_barrier(self):
        """A candidate the barrier rejects must drop the server_key and redirect to the bare url (the CodeQL py/url-redirection barrier, now shared in this helper)."""
        from netbox_librenms_plugin.views.mixins import redirect_with_server_key

        with patch("netbox_librenms_plugin.views.mixins.url_has_allowed_host_and_scheme", return_value=False):
            resp = redirect_with_server_key(self._request(), "/sync/1/", "prod")
        assert resp.url == "/sync/1/"
        assert "server_key" not in resp.url


class TestResolveConfiguredServerKey:
    """resolve_configured_server_key: the shared allowlist re-sourcing a key from trusted config."""

    def test_returns_key_when_it_names_a_configured_server(self):
        from netbox_librenms_plugin.views.mixins import resolve_configured_server_key

        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"siteB": "Site B"},
        ):
            assert resolve_configured_server_key("siteB") == "siteB"

    def test_returns_none_for_a_stale_or_tampered_key(self):
        from netbox_librenms_plugin.views.mixins import resolve_configured_server_key

        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"siteB": "Site B"},
        ):
            assert resolve_configured_server_key("ghost") is None

    def test_blank_or_none_short_circuits_before_touching_config(self):
        from netbox_librenms_plugin.views.mixins import resolve_configured_server_key

        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
        ) as servers:
            for key in (None, ""):
                assert resolve_configured_server_key(key) is None
            servers.assert_not_called()
