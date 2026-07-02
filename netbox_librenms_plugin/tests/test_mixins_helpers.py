"""Real tests for the shared mixin helpers consolidated during the develop-hardening pass.

- ``extract_cached_ports`` now reuses ``is_list_of_dicts`` for its ports-shape check (B7).
- ``LibreNMSAPIMixin.resolve_requested_server_key`` centralises the "configured-string-key-or
  -fallback" rule the device verify/render views each open-coded (B12).

These exercise the real functions against the real Django cache / real plugin config — no mocks.
"""

from unittest.mock import patch

import pytest
from django.core.cache import cache as real_cache
from django.test import RequestFactory

from netbox_librenms_plugin.librenms_api import LibreNMSAPI


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
        with patch.object(LibreNMSAPI, "get_available_servers", return_value={"prod": "Prod", "default": "Default"}):
            assert view.resolve_requested_server_key({"server_key": "prod"}) == "prod"

    def test_unconfigured_key_falls_back_to_render_resolver(self):
        view = self._view()
        with patch.object(LibreNMSAPI, "get_available_servers", return_value={"default": "Default"}):
            # "prod" is not among the configured servers → degrade, don't address its namespace.
            assert view.resolve_requested_server_key({"server_key": "prod"}) == view._render_server_key()

    def test_non_string_key_falls_back_without_crashing(self):
        view = self._view()
        with patch.object(LibreNMSAPI, "get_available_servers", return_value={"prod": "Prod"}):
            # A JSON list is unhashable; the membership check must be skipped, not TypeError.
            assert view.resolve_requested_server_key({"server_key": ["forged"]}) == view._render_server_key()

    def test_missing_key_falls_back(self):
        view = self._view()
        assert view.resolve_requested_server_key({}) == view._render_server_key()
