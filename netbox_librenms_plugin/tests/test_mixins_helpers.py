"""Real tests for the shared mixin helpers consolidated during the develop-hardening pass.

- ``extract_cached_ports`` now reuses ``is_list_of_dicts`` for its ports-shape check (B7).
- ``LibreNMSAPIMixin.resolve_requested_server_key`` centralises the "configured-string-key-or
  -fallback" rule the device verify/render views each open-coded (B12).

These exercise the real functions against the real Django cache / real plugin config — no mocks.
"""

from copy import deepcopy

import pytest
from django.conf import settings
from django.core.cache import cache as real_cache
from django.test import RequestFactory, override_settings


def _configured_servers(*keys):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        key: {"librenms_url": f"https://{key}.example.test", "api_token": "token"} for key in keys
    }
    return override_settings(PLUGINS_CONFIG=plugin_config)


class TestExtractCachedPortsShapeCheck:
    """extract_cached_ports must accept a valid ports payload and treat any malformed shape as a miss."""

    def _fn(self):
        from netbox_librenms_plugin.views.mixins import extract_cached_ports

        return extract_cached_ports

    def test_valid_payload_returned_unchanged(self):
        payload = {"ports": [{"port_id": 1, "ifName": "eth0"}]}
        assert self._fn()(payload) is payload

    def test_empty_ports_list_is_valid(self):
        # An empty list is a device that legitimately has no ports — still a valid payload.
        payload = {"ports": []}
        assert self._fn()(payload) is payload

    def test_non_dict_cached_value_is_miss(self):
        assert self._fn()(["legacy", "bare", "list"]) is None

    def test_non_list_ports_is_miss(self):
        assert self._fn()({"ports": "not-a-list"}) is None

    def test_non_dict_port_row_is_miss_and_purges_cache(self):
        key = "test-b7-extract-cached-ports"
        bad = {"ports": [{"port_id": 1}, "not-a-dict"]}
        real_cache.set(key, bad, timeout=60)
        try:
            assert self._fn()(bad, cache_key=key) is None
            assert real_cache.get(key) is None  # the corrupt entry is purged, not re-served
        finally:
            real_cache.delete(key)


@pytest.mark.django_db
class TestResolveRequestedServerKey:
    """resolve_requested_server_key honours only a configured string key, else degrades to _render_server_key.

    The configured-server set (LibreNMSAPI.get_available_servers) is the external plugin-config
    boundary; it's pinned per test so the assertion is deterministic (a session-wide autouse fixture
    in the API-test helpers otherwise leaves get_plugin_config mocked to a default-only config). The
    helper's own logic — isinstance guard, membership check, degrading fallback — runs for real.
    """

    def _view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        view = DeviceInterfaceTableView()
        view.request = RequestFactory().get("/")
        return view

    def test_configured_string_key_is_honoured(self):
        view = self._view()
        with _configured_servers("prod", "default"):
            assert view.resolve_requested_server_key({"server_key": "prod"}) == "prod"

    def test_unconfigured_key_falls_back_to_render_resolver(self):
        view = self._view()
        with _configured_servers("default"):
            # "prod" is not among the configured servers → degrade, don't address its namespace.
            assert view.resolve_requested_server_key({"server_key": "prod"}) == view._render_server_key()

    def test_non_string_key_falls_back_without_crashing(self):
        view = self._view()
        with _configured_servers("prod"):
            # A JSON list is unhashable; the membership check must be skipped, not TypeError.
            assert view.resolve_requested_server_key({"server_key": ["forged"]}) == view._render_server_key()

    def test_missing_key_falls_back(self):
        view = self._view()
        assert view.resolve_requested_server_key({}) == view._render_server_key()


class TestResolvePostedServerKey:
    """resolve_posted_server_key honours only a configured key, else falls back to the ACTIVE server.

    Unlike resolve_requested_server_key (which degrades to _render_server_key()/None for GET renders),
    the module install/bind ACTION paths fall back to the active client server so the port-bind still
    runs — but a forged non-blank key must still be rejected so a binding is never written under an
    unconfigured namespace.
    """

    def _view(self, active="active-server"):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        view = DeviceInterfaceTableView()
        view._librenms_api = LibreNMSAPI(server_key=active)
        return view

    def test_configured_key_is_honoured(self):
        with _configured_servers("active-server", "prod", "default"):
            view = self._view()
            assert view.resolve_posted_server_key({"server_key": "prod"}) == "prod"

    def test_forged_nonblank_key_falls_back_to_active(self):
        with _configured_servers("active-server", "default"):
            view = self._view()
            # 'evil' names no configured server → must NOT be honoured (would scope a bind under a
            # bogus namespace); fall back to the active server instead.
            assert view.resolve_posted_server_key({"server_key": "evil"}) == "active-server"

    def test_blank_key_falls_back_to_active(self):
        with _configured_servers("active-server", "default"):
            view = self._view()
            assert view.resolve_posted_server_key({"server_key": "   "}) == "active-server"

    def test_missing_key_falls_back_to_active(self):
        with _configured_servers("active-server", "default"):
            view = self._view()
            assert view.resolve_posted_server_key({}) == "active-server"


class TestGetLiveDeviceInfo:
    """get_live_device_info centralizes the write-path rule: always fetch uncached (use_cache=False)."""

    def test_delegates_to_get_device_info_with_cache_bypassed(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        calls = []

        class _RecordingAPI:
            # Default use_cache=True so a helper that forgot to pass the flag would record True.
            def get_device_info(self, librenms_id, use_cache=True):
                calls.append((librenms_id, use_cache))
                return (True, {"sysName": "sw1", "device_id": librenms_id})

        view = DeviceInterfaceTableView()
        view._librenms_api = _RecordingAPI()

        result = view.get_live_device_info(42)

        # The whole point of the helper: every write path bypasses the possibly-stale render cache.
        assert calls == [(42, False)]
        assert result == (True, {"sysName": "sw1", "device_id": 42})
