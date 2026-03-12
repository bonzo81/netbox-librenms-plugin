"""
Regression tests for SingleIPAddressVerifyView.post().

Covers:
- Cache key uses CacheMixin.get_cache_key() (server-aware) instead of
  the old private _get_cache_key() that produced a different format.
- server_key from POST body is threaded into the cache lookup so
  non-default servers hit the correct cache entry.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_view():
    """Create a SingleIPAddressVerifyView instance without database access."""
    from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

    view = object.__new__(SingleIPAddressVerifyView)
    return view


def _make_request(body_dict):
    """Create a mock POST request with JSON body."""
    request = MagicMock()
    request.method = "POST"
    request.body = json.dumps(body_dict).encode()
    return request


def _mock_device(pk=1):
    """Create a mock Device with _meta for cache key generation."""
    device = MagicMock()
    device.pk = pk
    device._meta.model_name = "device"
    device.name = "test-device"
    device.get_absolute_url.return_value = f"/dcim/devices/{pk}/"
    device.interfaces.first.return_value = None
    return device


class TestCacheKeyFormat:
    """SingleIPAddressVerifyView must use CacheMixin.get_cache_key()."""

    def test_no_private_get_cache_key_method(self):
        """The old _get_cache_key method must not exist on SingleIPAddressVerifyView."""
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        assert not hasattr(SingleIPAddressVerifyView, "_get_cache_key"), (
            "SingleIPAddressVerifyView still has _get_cache_key; it should use CacheMixin.get_cache_key() instead"
        )

    def test_cache_key_matches_writer_format(self):
        """The cache key used by post() must match the format used by _prepare_context()."""
        view = _make_view()
        device = _mock_device(pk=42)

        # CacheMixin.get_cache_key produces this format
        expected_key = "librenms_ip_addresses_device_42_prod"

        assert view.get_cache_key(device, "ip_addresses", "prod") == expected_key

    def test_cache_key_default_server(self):
        """Default server key produces the expected cache key format."""
        view = _make_view()
        device = _mock_device(pk=7)

        expected_key = "librenms_ip_addresses_device_7_default"
        assert view.get_cache_key(device, "ip_addresses", "default") == expected_key


class TestServerKeyFromPost:
    """server_key from POST body must be used for cache lookup."""

    @pytest.fixture(autouse=True)
    def _patch_ip_models(self):
        """Patch IPAddress.objects to avoid DB access."""
        with patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddress") as mock_ip:
            mock_ip.objects.filter.return_value.first.return_value = None
            yield

    def _run_post(self, body, device=None):
        """Execute view.post() with mocks and return the cache key used."""
        view = _make_view()
        if device is None:
            device = _mock_device()

        request = _make_request(body)
        captured_cache_key = {}

        def fake_cache_get(key):
            captured_cache_key["key"] = key
            return {"ip_addresses": []}

        with (
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
        ):
            mock_cache.get.side_effect = fake_cache_get
            view.post(request)

        return captured_cache_key.get("key")

    def test_server_key_threaded_to_cache_lookup(self):
        """post() must include server_key in the cache key."""
        device = _mock_device(pk=5)
        key = self._run_post(
            {"device_id": 5, "ip_address": "10.0.0.1/24", "server_key": "prod", "object_type": "device"},
            device=device,
        )

        assert key == "librenms_ip_addresses_device_5_prod"

    def test_default_server_key_when_missing(self):
        """When server_key is absent from POST, default to 'default'."""
        device = _mock_device(pk=5)
        key = self._run_post(
            {"device_id": 5, "ip_address": "10.0.0.1/24", "object_type": "device"},
            device=device,
        )

        assert key == "librenms_ip_addresses_device_5_default"

    def test_null_server_key_falls_back_to_default(self):
        """When server_key is explicitly null, fall back to 'default'."""
        device = _mock_device(pk=5)
        key = self._run_post(
            {"device_id": 5, "ip_address": "10.0.0.1/24", "server_key": None, "object_type": "device"},
            device=device,
        )

        assert key == "librenms_ip_addresses_device_5_default"
