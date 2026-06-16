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


class TestNumericIDValidation:
    """post() must reject a non-numeric device_id/vrf_id with a clean 400 rather than let
    the value reach the ORM and surface as a generic 500."""

    def test_non_numeric_object_id_returns_400(self):
        view = _make_view()
        request = _make_request({"device_id": "abc", "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["status"] == "error"
        # Assert the specific message so the test can't pass on an unrelated 400 branch.
        assert payload["message"] == "Invalid object ID"

    def test_non_numeric_vrf_id_returns_400(self):
        view = _make_view()
        request = _make_request({"device_id": 5, "vrf_id": "xyz", "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["status"] == "error"
        assert payload["message"] == "Invalid VRF ID"

    def test_boolean_false_object_id_rejected_as_invalid(self):
        # bool is an int subclass; object_id=False must hit the explicit boolean guard
        # ("Invalid object ID"), not the falsy "No object ID provided" branch. The guard
        # therefore has to run before `if not object_id`.
        view = _make_view()
        request = _make_request({"device_id": False, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["message"] == "Invalid object ID"

    def test_boolean_true_object_id_rejected_as_invalid(self):
        # object_id=True would otherwise int() to 1 and validate as device #1.
        view = _make_view()
        request = _make_request({"device_id": True, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["message"] == "Invalid object ID"

    def test_boolean_vrf_id_rejected_as_invalid(self):
        # bool is an int subclass; vrf_id=True would otherwise int() to 1 and validate as VRF #1.
        # The boolean guard must reject it ("Invalid VRF ID"), mirroring the object_id guards —
        # so true/false can't silently regress to 1/0.
        view = _make_view()
        request = _make_request({"device_id": 5, "vrf_id": True, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["status"] == "error"
        assert payload["message"] == "Invalid VRF ID"

    def test_float_object_id_rejected_as_invalid(self):
        # A JSON float device_id=1.9 would otherwise int()-truncate to 1 and bind device #1.
        # The explicit float guard must reject it with a clean 400 instead.
        view = _make_view()
        request = _make_request({"device_id": 1.9, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["message"] == "Invalid object ID"

    def test_float_vrf_id_rejected_as_invalid(self):
        # vrf_id=2.5 would otherwise int()-truncate to 2 and validate as VRF #2.
        view = _make_view()
        request = _make_request({"device_id": 5, "vrf_id": 2.5, "ip_address": "10.0.0.1/24", "object_type": "device"})
        response = view.post(request)
        assert response.status_code == 400
        payload = json.loads(response.content)
        assert payload["status"] == "error"
        assert payload["message"] == "Invalid VRF ID"
