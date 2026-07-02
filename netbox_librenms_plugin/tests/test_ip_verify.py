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
    # Direct post() calls bypass dispatch() (which sets self.request), so null the object-perm
    # gate; the gate itself is covered by TestSingleIPAddressVerifyObjectPermissionGate (real DB).
    view.require_object_permissions_json = MagicMock(return_value=None)
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
        # Direct post() bypasses dispatch(); _get_object reads self.request.user to object-scope the
        # lookup, so supply the request the view would otherwise get from dispatch.
        view.request = request
        captured_cache_key = {}

        def fake_cache_get(key):
            captured_cache_key["key"] = key
            return {"ip_addresses": []}

        with (
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_object_or_404",
                return_value=device,
            ),
            # The verify gate validates server_key against configured servers before using it as a
            # cache namespace; configure "prod" so a posted "prod" threads through, while an absent
            # or unconfigured key still falls back to "default".
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"prod": "Prod"},
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


class TestVerifyPostRejectsNonObjectBody:
    """A non-object JSON body must 400, not 500 on .get()."""

    def test_non_dict_json_returns_400(self):
        """A JSON array body returns 400 before any .get(), instead of raising AttributeError."""
        view = _make_view()
        request = _make_request([1, 2, 3])  # valid JSON, but an array — not an object

        response = view.post(request)

        assert response.status_code == 400
        assert json.loads(response.content)["message"] == "JSON payload must be an object"


class TestFindInCacheFailsClosed:
    """SingleIPAddressVerifyView._find_in_cache treats a truthy non-dict entry as a miss, not a crash."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        return object.__new__(SingleIPAddressVerifyView)

    def test_non_dict_cache_entry_returns_empty_triple(self):
        """A list (legacy snapshot shape) must not raise AttributeError on .get()."""
        view = self._make_view()
        assert view._find_in_cache([{"ip_address": "10.0.0.1"}], "10.0.0.1", 32) == (None, None, None)

    def test_dict_cache_entry_still_matches(self):
        """Positive control: a well-formed dict entry still resolves normally."""
        view = self._make_view()
        cached = {"ip_addresses": [{"ip_address": "10.0.0.1", "prefix_length": 32, "vrf_id": 7, "port_id": 5}]}
        entry, vrf_id, port_id = view._find_in_cache(cached, "10.0.0.1", 32)
        assert entry is not None and vrf_id == 7 and port_id == 5

    def test_malformed_row_among_ip_addresses_is_skipped(self):
        """A non-dict row inside ip_addresses must be skipped (not TypeError'd) so a later good row still matches."""
        view = self._make_view()
        cached = {
            "ip_addresses": [
                "not-a-dict",
                ["also", "bad"],
                {"ip_address": "10.0.0.1", "prefix_length": 32, "vrf_id": 7, "port_id": 5},
            ]
        }
        entry, vrf_id, port_id = view._find_in_cache(cached, "10.0.0.1", 32)
        assert entry is not None and vrf_id == 7 and port_id == 5

    def test_all_rows_malformed_returns_empty_triple(self):
        """When every row is malformed, the lookup is a clean miss rather than a crash."""
        view = self._make_view()
        assert view._find_in_cache({"ip_addresses": ["x", 5, None]}, "10.0.0.1", 32) == (None, None, None)
