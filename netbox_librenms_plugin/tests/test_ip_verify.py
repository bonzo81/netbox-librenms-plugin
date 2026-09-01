"""
Regression tests for SingleIPAddressVerifyView.post().

Covers:
- Cache key uses CacheMixin.get_cache_key() (server-aware) instead of
  the old private _get_cache_key() that produced a different format.
- server_key from POST body is threaded into the cache lookup so
  non-default servers hit the correct cache entry.
"""

import json

import pytest
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser


pytestmark = pytest.mark.django_db


def _make_view(request=None):
    """Create a SingleIPAddressVerifyView bound to a real request and user."""
    from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

    view = SingleIPAddressVerifyView()
    view.setup(request if request is not None else _make_request({}))
    return view


def _make_request(body_dict):
    """Create a JSON POST request with a real permitted user."""
    request = RequestFactory().post("/", data=json.dumps(body_dict), content_type="application/json")
    request.user = make_superuser()
    return request


def _make_device(tag=1):
    """Create a real Device for cache key generation."""
    return make_device(f"ip-verify-cache-{tag}")


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
        device = _make_device(tag=42)

        # CacheMixin.get_cache_key produces this format
        expected_key = f"librenms_ip_addresses_device_{device.pk}_prod"

        assert view.get_cache_key(device, "ip_addresses", "prod") == expected_key

    def test_cache_key_default_server(self):
        """Default server key produces the expected cache key format."""
        view = _make_view()
        device = _make_device(tag=7)

        expected_key = f"librenms_ip_addresses_device_{device.pk}_default"
        assert view.get_cache_key(device, "ip_addresses", "default") == expected_key


class TestVerifyPostRejectsNonObjectBody:
    """A non-object JSON body must 400, not 500 on .get()."""

    def test_non_dict_json_returns_400(self):
        """A JSON array body returns 400 before any .get(), instead of raising AttributeError."""
        view = _make_view()
        request = _make_request([1, 2, 3])  # valid JSON, but an array — not an object

        response = view.post(request)

        assert response.status_code == 400
        assert json.loads(response.content)["message"] == "JSON payload must be an object"


class TestVerifyPostServerCacheNamespace:
    """The endpoint reads the selected configured namespace or the default namespace."""

    @pytest.mark.parametrize(
        ("server_key", "include_server_key", "selected_vrf"),
        [("production", True, 20), (None, True, 10), (None, False, 10)],
    )
    def test_configured_missing_and_null_server_keys_select_the_expected_cache(
        self,
        configure_librenms,
        server_key,
        include_server_key,
        selected_vrf,
    ):
        from django.core.cache import cache

        configure_librenms(
            {
                "default": {
                    "librenms_url": "https://default.example.test",
                    "api_token": "test-token",
                },
                "production": {
                    "librenms_url": "https://production.example.test",
                    "api_token": "test-token",
                },
            }
        )
        device = make_device(f"ip-verify-namespace-{selected_vrf}-{include_server_key}")
        view = _make_view()
        address = "198.18.40.1/32"
        for namespace, vrf_id in (("default", 10), ("production", 20)):
            cache.set(
                view.get_cache_key(device, "ip_addresses", namespace),
                {"ip_addresses": [{"ip_with_mask": address, "vrf_id": vrf_id}]},
                timeout=300,
            )

        body = {
            "device_id": device.pk,
            "object_type": "device",
            "ip_address": address,
            "vrf_id": selected_vrf,
        }
        if include_server_key:
            body["server_key"] = server_key
        request = _make_request(body)

        response = _make_view(request).post(request)

        assert response.status_code == 200
        assert "Synced" in json.loads(response.content)["formatted_row"]["status"]


@pytest.mark.django_db
class TestVerifyPostRejectsMalformedVrfId:
    """A non-numeric vrf_id must 400 before the VRF filter, not 500 via the broad handler.

    The `vrf__id` filter in `_find_existing_ip` is only reached when an IPAddress at the posted address
    already exists, so the real IP is created first — otherwise the guard is never exercised and the
    pre-fix code wouldn't 500 either. Only the cache read is stubbed; `_find_existing_ip` runs for real.
    """

    def _real_device(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        mfr, _ = Manufacturer.objects.get_or_create(name="VRFSK-Mfr", slug="vrfsk-mfr")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="VRFSK-DT", slug="vrfsk-dt")
        role, _ = DeviceRole.objects.get_or_create(name="VRFSK-Role", slug="vrfsk-role")
        site, _ = Site.objects.get_or_create(name="VRFSK-Site", slug="vrfsk-site")
        return Device.objects.create(name="vrfsk-dev", device_type=dt, role=role, site=site, status="active")

    def _post(self, vrf_id):
        from django.core.cache import cache
        from ipam.models import IPAddress

        device = self._real_device()
        IPAddress.objects.get_or_create(address="10.0.0.1/24")  # make the vrf__id filter reachable

        request = _make_request(
            {"ip_address": "10.0.0.1/24", "vrf_id": vrf_id, "device_id": device.pk, "object_type": "device"}
        )
        view = _make_view(request)
        cache.set(view.get_cache_key(device, "ip_addresses", "default"), {"ip_addresses": []}, timeout=300)

        return view.post(request)

    def test_non_numeric_vrf_id_returns_400(self):
        response = self._post("abc")
        assert response.status_code == 400
        assert json.loads(response.content)["message"] == "Invalid VRF ID"

    def test_list_vrf_id_returns_400(self):
        assert self._post([1, 2]).status_code == 400

    def test_boolean_vrf_id_returns_400(self):
        # bool is an int subclass; a JSON `true` must not silently coerce to vrf__id=1.
        assert self._post(True).status_code == 400

    def test_numeric_string_vrf_id_is_accepted(self):
        # A digit string coerces to int and flows through the real filter without 400/500.
        assert self._post("999999").status_code == 200


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
        cached = {
            "ip_addresses": [
                {
                    "ip_address": "10.0.0.1",
                    "ip_with_mask": "10.0.0.1/32",
                    "prefix_length": 32,
                    "vrf_id": 7,
                    "port_id": 5,
                }
            ]
        }
        entry, vrf_id, port_id = view._find_in_cache(cached, "10.0.0.1", 32)
        assert entry is not None and vrf_id == 7 and port_id == 5

    def test_malformed_row_among_ip_addresses_is_skipped(self):
        """A non-dict row inside ip_addresses must be skipped (not TypeError'd) so a later good row still matches."""
        view = self._make_view()
        cached = {
            "ip_addresses": [
                "not-a-dict",
                ["also", "bad"],
                {
                    "ip_address": "10.0.0.1",
                    "ip_with_mask": "10.0.0.1/32",
                    "prefix_length": 32,
                    "vrf_id": 7,
                    "port_id": 5,
                },
            ]
        }
        entry, vrf_id, port_id = view._find_in_cache(cached, "10.0.0.1", 32)
        assert entry is not None and vrf_id == 7 and port_id == 5

    def test_all_rows_malformed_returns_empty_triple(self):
        """When every row is malformed, the lookup is a clean miss rather than a crash."""
        view = self._make_view()
        assert view._find_in_cache({"ip_addresses": ["x", 5, None]}, "10.0.0.1", 32) == (None, None, None)


class TestNumericIDValidation:
    """post() must reject a non-numeric device_id/vrf_id with a clean 400 rather than let the value reach the ORM and surface as a generic 500."""

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
