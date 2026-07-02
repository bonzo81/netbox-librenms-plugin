"""Tests for SingleCableVerifyView and SingleInterfaceVerifyView VC resolution.

Verifies that both views delegate VC device resolution to
get_librenms_sync_device() and handle the None return gracefully
(e.g. empty VC members or vc_position type errors).
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory


def _make_request(body: dict) -> MagicMock:
    """Create a mock POST request with JSON body."""
    request = MagicMock()
    request.body = json.dumps(body).encode()
    request.user.has_perm.return_value = True
    return request


def _make_vc_device(pk=1, name="vc-device"):
    """Create a mock Device that belongs to a virtual chassis."""
    device = MagicMock()
    device.pk = pk
    device.id = pk
    device.name = name
    device._meta.model_name = "device"
    device.virtual_chassis = MagicMock()
    device.interfaces.filter.return_value.first.return_value = None
    return device


# ---------------------------------------------------------------------------
# SingleCableVerifyView
# ---------------------------------------------------------------------------
class TestSingleCableVerifyView:
    """SingleCableVerifyView.post() VC resolution and None guard."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        # Direct post() calls bypass dispatch() (which sets self.request), so null the object-perm
        # gate; the gate itself is covered in test_coverage_base_views.py.
        view.require_object_permissions_json = MagicMock(return_value=None)
        return view

    @patch("netbox_librenms_plugin.views.base.cables_view.get_object_or_404")
    @patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.base.cables_view.cache")
    def test_vc_no_resolvable_sync_device_returns_empty_row(self, mock_cache, mock_sync, mock_get_obj):
        """VC where get_librenms_sync_device returns None → empty row, no crash."""
        device = _make_vc_device(pk=1)
        mock_get_obj.return_value = device
        mock_sync.return_value = None

        view = self._make_view()
        request = _make_request({"device_id": 1, "local_port_id": "42"})
        response = view.post(request)

        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["formatted_row"]["cable_status"] == "Missing Ports"
        mock_cache.get.assert_not_called()

    @patch("netbox_librenms_plugin.views.base.cables_view.get_object_or_404")
    @patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.base.cables_view.cache")
    def test_vc_resolved_sync_device_uses_cache(self, mock_cache, mock_sync, mock_get_obj):
        """VC with resolved sync device: cache is queried with that device's key."""
        device = _make_vc_device(pk=1)
        sync_device = _make_vc_device(pk=2, name="sync-device")
        mock_get_obj.return_value = device
        mock_sync.return_value = sync_device
        mock_cache.get.return_value = None  # No cached data

        view = self._make_view()
        request = _make_request({"device_id": 1, "local_port_id": "42"})
        view.post(request)

        mock_sync.assert_called_once_with(device, server_key=view._librenms_api.server_key)
        mock_cache.get.assert_called_once()
        cache_key = mock_cache.get.call_args[0][0]
        assert "device" in cache_key
        assert "2" in cache_key

    @patch("netbox_librenms_plugin.views.base.cables_view.get_object_or_404")
    @patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.base.cables_view.cache")
    def test_non_vc_device_skips_sync_device_lookup(self, mock_cache, mock_sync, mock_get_obj, mock_netbox_device):
        """Non-VC device: get_librenms_sync_device is NOT called."""
        mock_netbox_device.virtual_chassis = None
        mock_get_obj.return_value = mock_netbox_device
        mock_cache.get.return_value = None

        view = self._make_view()
        request = _make_request({"device_id": 5, "local_port_id": "10"})
        view.post(request)

        mock_sync.assert_not_called()
        mock_cache.get.assert_called_once()

    def test_no_device_id_returns_empty_row(self):
        """Missing device_id: returns default empty formatted_row."""
        view = self._make_view()
        request = _make_request({"local_port_id": "42"})
        response = view.post(request)

        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["formatted_row"]["cable_status"] == "Missing Ports"

    @patch("netbox_librenms_plugin.views.base.cables_view.get_object_or_404")
    @patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.base.cables_view.cache")
    def test_malformed_cached_links_fail_closed(self, mock_cache, mock_sync, mock_get_obj, mock_netbox_device):
        """A corrupt cached links entry must fail closed (empty row + purge), not 500 the verify path."""
        mock_netbox_device.virtual_chassis = None
        mock_get_obj.return_value = mock_netbox_device
        # A non-dict link row: the old code iterates it and calls str.get(...) → AttributeError.
        mock_cache.get.return_value = {"links": ["not-a-dict"]}

        view = self._make_view()
        request = _make_request({"device_id": 5, "local_port_id": "10"})
        response = view.post(request)

        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["formatted_row"]["cable_status"] == "Missing Ports"
        # The corrupt entry is purged so the next verify doesn't keep serving garbage.
        mock_cache.delete.assert_called_once()


def test_extract_cached_links_fails_closed_and_purges_malformed_entries():
    """_extract_cached_links (shared by the cached GET render and the verify path) must reject a corrupt entry and purge its key, using the REAL cache."""
    from django.core.cache import cache as real_cache

    from netbox_librenms_plugin.views.base.cables_view import _extract_cached_links

    key = "test-cables-malformed-links-key"
    for bad in (["junk"], "corrupt", {"links": None}, {"links": ["bad"]}, {"links": [{"ok": 1}, 5]}):
        real_cache.set(key, bad)
        assert _extract_cached_links(real_cache.get(key), key) is None
        assert real_cache.get(key) is None  # purged

    # A well-formed entry passes through unchanged (and is NOT purged).
    real_cache.set(key, {"links": [{"local_port_id": 1}]})
    try:
        assert _extract_cached_links(real_cache.get(key), key) == [{"local_port_id": 1}]
        assert real_cache.get(key) == {"links": [{"local_port_id": 1}]}
    finally:
        real_cache.delete(key)


# ---------------------------------------------------------------------------
# SingleInterfaceVerifyView
# ---------------------------------------------------------------------------
class TestSingleInterfaceVerifyView:
    """SingleInterfaceVerifyView.post() VC resolution and None guard."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        view = object.__new__(SingleInterfaceVerifyView)
        view._librenms_api = MagicMock()
        # Direct post() calls bypass dispatch() (which sets self.request), so null the object-perm
        # gate; permission ordering is covered in test_coverage_devices.py.
        view.require_object_permissions_json = MagicMock(return_value=None)
        return view

    @patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404")
    @patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.object_sync.devices.cache")
    def test_vc_no_resolvable_sync_device_returns_404(self, mock_cache, mock_sync, mock_get_obj):
        """VC where get_librenms_sync_device returns None → 404 JSON error, no crash."""
        device = _make_vc_device(pk=1)
        mock_get_obj.return_value = device
        mock_sync.return_value = None

        view = self._make_view()
        request = _make_request(
            {
                "device_id": 1,
                "interface_name": "eth0",
                "interface_name_field": "ifName",
            }
        )
        response = view.post(request)

        assert response.status_code == 404
        data = json.loads(response.content)
        assert data["status"] == "error"
        assert "sync device" in data["message"].lower()
        mock_cache.get.assert_not_called()

    @patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404")
    @patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.object_sync.devices.cache")
    def test_vc_resolved_sync_device_uses_cache(self, mock_cache, mock_sync, mock_get_obj):
        """VC with resolved sync device: cache is queried with that device's key."""
        device = _make_vc_device(pk=1)
        sync_device = _make_vc_device(pk=3, name="sync-member")
        mock_get_obj.return_value = device
        mock_sync.return_value = sync_device
        mock_cache.get.return_value = None

        view = self._make_view()
        request = _make_request(
            {
                "device_id": 1,
                "interface_name": "eth0",
                "interface_name_field": "ifName",
            }
        )
        view.post(request)

        mock_sync.assert_called_once_with(device, server_key=view._librenms_api.server_key)
        mock_cache.get.assert_called_once()
        cache_key = mock_cache.get.call_args[0][0]
        assert "3" in cache_key

    @patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404")
    @patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.object_sync.devices.cache")
    def test_non_vc_device_skips_sync_device_lookup(self, mock_cache, mock_sync, mock_get_obj, mock_netbox_device):
        """Non-VC device: get_librenms_sync_device is NOT called."""
        mock_netbox_device.virtual_chassis = None
        mock_get_obj.return_value = mock_netbox_device
        mock_cache.get.return_value = None

        view = self._make_view()
        request = _make_request(
            {
                "device_id": 5,
                "interface_name": "eth0",
                "interface_name_field": "ifName",
            }
        )
        view.post(request)

        mock_sync.assert_not_called()
        mock_cache.get.assert_called_once()

    def test_no_device_id_returns_400(self):
        """Missing device_id: returns 400 error."""
        view = self._make_view()
        request = _make_request({"interface_name": "eth0"})
        response = view.post(request)

        assert response.status_code == 400
        data = json.loads(response.content)
        assert data["status"] == "error"


# ---------------------------------------------------------------------------
# SingleIPAddressVerifyView — object-permission gate (real DB, real has_perm)
# ---------------------------------------------------------------------------
def _make_gate_device(name="ipgate-dev"):
    """Create a real Device for the IP-verify object-permission gate tests."""
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name="IPGate-Mfr", slug="ipgate-mfr")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="IPGate-DT", slug="ipgate-dt")
    role, _ = DeviceRole.objects.get_or_create(name="IPGate-Role", slug="ipgate-role")
    site, _ = Site.objects.get_or_create(name="IPGate-Site", slug="ipgate-site")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site, status="active")


def _make_gate_vm(name="ipgate-vm"):
    """Create a real VirtualMachine for the IP-verify object-permission gate tests."""
    from virtualization.models import Cluster, ClusterType, VirtualMachine

    ct, _ = ClusterType.objects.get_or_create(name="IPGate-CT", slug="ipgate-ct")
    cluster, _ = Cluster.objects.get_or_create(name="IPGate-Cluster", type=ct)
    return VirtualMachine.objects.create(name=name, cluster=cluster, status="active")


@pytest.mark.django_db
class TestSingleIPAddressVerifyObjectPermissionGate:
    """SingleIPAddressVerifyView must gate POST on dcim.view_device.

    The read-only verify endpoint resolves an arbitrary ``device_id`` from the
    JSON body and returns that object's name/url/cached rows. Without the gate a
    caller with only plugin-view rights could probe and read back objects they
    cannot see (mirrors the interface/module/cable verify views' hardening).

    These exercise the REAL gate end-to-end: a real Device, a real (non-super)
    user, real NetBox ObjectPermission grants and real ``has_perm`` — no mocks.
    """

    def _post(self, user, device_id, object_type=None):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        payload = {"device_id": device_id, "ip_address": "1.2.3.4/24"}
        if object_type is not None:
            payload["object_type"] = object_type
        request = RequestFactory().post(
            "/plugins/librenms_plugin/verify-ipaddress/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        request.user = user
        # Direct post() bypasses dispatch(); supply the request the gate reads.
        view = SingleIPAddressVerifyView()
        view.request = request
        view.kwargs = {}
        view.args = ()
        return view.post(request)

    def test_user_without_view_device_is_denied(self):
        """A user lacking dcim.view_device is refused (403) before any object data is read."""
        from django.contrib.auth import get_user_model

        device = _make_gate_device()
        user = get_user_model().objects.create_user(username="ipgate-noperm", password="x")

        response = self._post(user, device.pk)

        assert response.status_code == 403
        # The denied response must NOT leak the hidden device's name.
        assert device.name.encode() not in response.content

    def test_user_with_view_device_passes_gate(self):
        """A user granted dcim.view_device clears the gate (non-403, normal 200 path)."""
        from core.models import ObjectType
        from dcim.models import Device
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        device = _make_gate_device(name="ipgate-dev2")
        user = get_user_model().objects.create_user(username="ipgate-perm", password="x")
        perm = ObjectPermission.objects.create(name="ipgate-view-device", actions=["view"])
        perm.object_types.set([ObjectType.objects.get_for_model(Device)])
        perm.users.set([user])
        # Re-fetch to clear NetBox's per-request object-permission cache on the user.
        user = get_user_model().objects.get(pk=user.pk)

        response = self._post(user, device.pk)

        assert response.status_code != 403

    def test_device_view_only_user_denied_vm_data(self):
        """A user with only dcim.view_device must NOT read VirtualMachine data through this endpoint."""
        from core.models import ObjectType
        from dcim.models import Device
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        vm = _make_gate_vm()
        user = get_user_model().objects.create_user(username="ipgate-devonly", password="x")
        perm = ObjectPermission.objects.create(name="ipgate-view-dev-only", actions=["view"])
        perm.object_types.set([ObjectType.objects.get_for_model(Device)])
        perm.users.set([user])
        user = get_user_model().objects.get(pk=user.pk)

        # The static Device-only gate would have let this through (the user HAS dcim.view_device),
        # leaking VM data; the per-object gate must require virtualization.view_virtualmachine.
        response = self._post(user, vm.pk, object_type="virtualmachine")

        assert response.status_code == 403
        assert vm.name.encode() not in response.content

    def test_vm_view_user_passes_gate_for_vm(self):
        """A user granted virtualization.view_virtualmachine clears the gate for a VM target."""
        from core.models import ObjectType
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission
        from virtualization.models import VirtualMachine

        vm = _make_gate_vm(name="ipgate-vm2")
        user = get_user_model().objects.create_user(username="ipgate-vmperm", password="x")
        perm = ObjectPermission.objects.create(name="ipgate-view-vm", actions=["view"])
        perm.object_types.set([ObjectType.objects.get_for_model(VirtualMachine)])
        perm.users.set([user])
        user = get_user_model().objects.get(pk=user.pk)

        response = self._post(user, vm.pk, object_type="virtualmachine")

        assert response.status_code != 403

    def test_vm_target_without_object_type_resolves_model_and_denies_device_only_user(self):
        """Even with no object_type, a VM id resolves to its model so a Device-only user is denied."""
        from core.models import ObjectType
        from dcim.models import Device
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        vm = _make_gate_vm(name="ipgate-vm3")
        user = get_user_model().objects.create_user(username="ipgate-devonly2", password="x")
        perm = ObjectPermission.objects.create(name="ipgate-view-dev-only2", actions=["view"])
        perm.object_types.set([ObjectType.objects.get_for_model(Device)])
        perm.users.set([user])
        user = get_user_model().objects.get(pk=user.pk)

        response = self._post(user, vm.pk)  # no object_type — server resolves the id to a VM

        assert response.status_code == 403
        assert vm.name.encode() not in response.content


@pytest.mark.django_db
class TestSingleIPAddressVerifyServerKeyCacheNamespace:
    """The verify POST must validate server_key before using it as a cache namespace.

    A forged/unconfigured key must not let a caller address an arbitrary server-key cache
    namespace; it falls back to a configured server (mirrors the sync/cable hardening).
    """

    def test_forged_server_key_falls_back_to_configured_namespace(self):
        from unittest.mock import patch

        from django.contrib.auth import get_user_model

        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        device = _make_gate_device(name="ipgate-srvkey")
        user = get_user_model().objects.create_user(username="ipgate-srvkey-su", password="x", is_superuser=True)
        user = get_user_model().objects.get(pk=user.pk)

        request = RequestFactory().post(
            "/plugins/librenms_plugin/verify-ipaddress/",
            data=json.dumps(
                {
                    "device_id": device.pk,
                    "ip_address": "1.2.3.4/24",
                    "object_type": "device",
                    "server_key": "evil-namespace",  # not a configured server
                }
            ),
            content_type="application/json",
        )
        request.user = user
        view = SingleIPAddressVerifyView()
        view.request = request
        view.kwargs = {}
        view.args = ()

        captured = {}
        original_get_cache_key = view.get_cache_key

        def spy(obj, data_type="ports", server_key=None):
            captured["server_key"] = server_key
            return original_get_cache_key(obj, data_type, server_key)

        view.get_cache_key = spy

        servers = {
            "alpha": {"librenms_url": "https://a.example.com", "api_token": "t"},
            "beta": {"librenms_url": "https://b.example.com", "api_token": "t"},
        }
        with patch(
            "netbox_librenms_plugin.librenms_api.get_plugin_config",
            side_effect=lambda app, key, default=None: servers if key == "servers" else default,
        ):
            view.post(request)

        # The forged key never reaches the cache namespace: it falls back to the fixed "default"
        # (the only non-configured namespace a caller can ever address), never "evil-namespace".
        assert captured.get("server_key") == "default"
        assert captured.get("server_key") != "evil-namespace"


# ---------------------------------------------------------------------------
# server_key guards on the interface/module verify + VLAN overrides endpoints
# (real DB, only the plugin-config boundary patched)
# ---------------------------------------------------------------------------
_GUARD_SERVERS = {
    "alpha": {"librenms_url": "https://a.example.com", "api_token": "t"},
    "beta": {"librenms_url": "https://b.example.com", "api_token": "t"},
}


def _patch_servers_config():
    from unittest.mock import patch as _patch

    return _patch(
        "netbox_librenms_plugin.librenms_api.get_plugin_config",
        side_effect=lambda app, key, default=None: _GUARD_SERVERS if key == "servers" else default,
    )


def _make_vc_member_device(name="srvkey-vc-dev"):
    """A real VC-member Device: the unguarded views route its server_key into cf_dict.get()."""
    from dcim.models import VirtualChassis

    device = _make_gate_device(name=name)
    vc = VirtualChassis.objects.create(name=f"vc-{name}")
    device.virtual_chassis = vc
    device.vc_position = 1
    # A dict-form librenms_id mapping: get_librenms_sync_device reads this dict with the
    # caller's server_key, so an unhashable key actually reaches cf_dict.get(["a"]).
    device.custom_field_data["librenms_id"] = {"alpha": 4242}
    device.save()
    return device


def _superuser(username):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username=username, password="x", is_superuser=True)
    return get_user_model().objects.get(pk=user.pk)


def _json_post(url, body, user):
    request = RequestFactory().post(url, data=json.dumps(body), content_type="application/json")
    request.user = user
    return request


@pytest.mark.django_db
class TestVerifyEndpointsServerKeyGuard:
    """A forged/non-string server_key must fall back, not 500 (mirrors the cable/IP siblings).

    A JSON array server_key is unhashable: routed into get_librenms_sync_device it reaches
    cf_dict.get(["a"]) and raises TypeError, turning these endpoints into 500s where the
    hardened siblings degrade. A forged string key must also never scope cache access.
    """

    def test_interface_verify_unhashable_server_key_degrades(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        device = _make_vc_member_device(name="srvkey-if-dev")
        request = _json_post(
            "/plugins/librenms_plugin/verify-interface/",
            {"device_id": device.pk, "interface_name": "eth0", "server_key": ["a"]},
            _superuser("srvkey-if-su"),
        )
        view = SingleInterfaceVerifyView()
        view.request = request

        with _patch_servers_config():
            response = view.post(request)

        # JSON error/miss response — not a TypeError 500.
        assert response.status_code in (200, 404)

    def test_module_verify_unhashable_server_key_degrades(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

        device = _make_vc_member_device(name="srvkey-mod-dev")
        request = _json_post(
            "/plugins/librenms_plugin/verify-module/",
            {"device_id": device.pk, "ent_physical_index": 7, "server_key": ["a"]},
            _superuser("srvkey-mod-su"),
        )
        view = SingleModuleVerifyView()
        view.request = request

        with _patch_servers_config():
            response = view.post(request)

        assert response.status_code in (200, 404)

    def test_vlan_overrides_unhashable_server_key_degrades(self):
        from netbox_librenms_plugin.views.object_sync.devices import SaveVlanGroupOverridesView

        device = _make_vc_member_device(name="srvkey-vlan-dev")
        request = _json_post(
            "/plugins/librenms_plugin/vlan-group-overrides/",
            {"device_id": device.pk, "vid_group_map": {"10": 1}, "server_key": ["a"]},
            _superuser("srvkey-vlan-su"),
        )
        view = SaveVlanGroupOverridesView()
        view.request = request

        with _patch_servers_config():
            response = view.post(request)

        # Falls back and then 400s on the missing ports cache — never a TypeError 500.
        assert response.status_code == 400

    def test_interface_verify_forged_string_key_never_scopes_cache(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        device = _make_gate_device(name="srvkey-forged-dev")
        request = _json_post(
            "/plugins/librenms_plugin/verify-interface/",
            {"device_id": device.pk, "interface_name": "eth0", "server_key": "evil-namespace"},
            _superuser("srvkey-forged-su"),
        )
        view = SingleInterfaceVerifyView()
        view.request = request

        captured = {}
        original_get_cache_key = view.get_cache_key

        def spy(obj, data_type="ports", server_key=None):
            captured["server_key"] = server_key
            return original_get_cache_key(obj, data_type, server_key)

        view.get_cache_key = spy

        with _patch_servers_config():
            view.post(request)

        assert captured.get("server_key") != "evil-namespace"


@pytest.mark.django_db
class TestParseRequestJsonRejectsNonObject:
    """A valid-JSON non-object body must 400 centrally, not AttributeError-500 at data.get()."""

    def test_helper_rejects_list_str_and_number(self):
        from netbox_librenms_plugin.views.mixins import parse_request_json

        for body in (b"[1]", b'"x"', b"7"):
            request = MagicMock()
            request.body = body
            data, err = parse_request_json(request)
            assert data is None
            assert err is not None and err.status_code == 400

    def test_cable_verify_returns_400_for_array_body(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        request = RequestFactory().post(
            "/plugins/librenms_plugin/verify-cable/", data="[1]", content_type="application/json"
        )
        request.user = _superuser("nonobj-cable-su")
        view = SingleCableVerifyView()
        view.request = request

        with _patch_servers_config():
            response = view.post(request)

        assert response.status_code == 400
        assert json.loads(response.content)["message"] == "JSON payload must be an object"


@pytest.mark.django_db
class TestInterfaceVerifyMalformedPortsCache:
    """A truthy but malformed ports cache entry must degrade to the 404 miss path and be purged."""

    def test_corrupt_ports_cache_degrades_and_purges(self):
        from django.core.cache import cache as real_cache

        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        device = _make_gate_device(name="srvkey-corrupt-dev")
        request = _json_post(
            "/plugins/librenms_plugin/verify-interface/",
            {"device_id": device.pk, "interface_name": "eth0", "server_key": "alpha"},
            _superuser("srvkey-corrupt-su"),
        )
        view = SingleInterfaceVerifyView()
        view.request = request

        cache_key = view.get_cache_key(device, "ports", "alpha")
        real_cache.set(cache_key, ["corrupt", "legacy", "shape"], timeout=60)
        try:
            with _patch_servers_config():
                response = view.post(request)

            # Degrades to the not-found miss path instead of AttributeError-500...
            assert response.status_code == 404
            # ...and the poisoned entry is purged so it isn't served again.
            assert real_cache.get(cache_key) is None
        finally:
            real_cache.delete(cache_key)


@pytest.mark.django_db
class TestVerifyViewObjectScope:
    """A *constrained* view_device grant must not resolve out-of-scope devices in the verify views.

    require_object_permissions_json only checks the model-level view_device perm, so a pk/site-scoped
    grant clears the gate. The device lookup must then object-scope via restrict() so an out-of-scope
    pk 404s instead of leaking that device's cached verify payload — even when its cache is warm.
    """

    SERVER_KEY = "default"  # the only configured server in the test env

    @staticmethod
    def _constrained_user(in_scope_device):
        from core.models import ObjectType
        from dcim.models import Device
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        user = get_user_model().objects.create_user(username="scoped-verify", password="x")
        perm = ObjectPermission.objects.create(
            name="scoped-view-device", actions=["view"], constraints={"pk": in_scope_device.pk}
        )
        perm.object_types.set([ObjectType.objects.get_for_model(Device)])
        perm.users.set([user])
        # Re-fetch to clear NetBox's per-request object-permission cache on the user.
        return get_user_model().objects.get(pk=user.pk)

    def _interface_verify_view(self, user):
        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        view = SingleInterfaceVerifyView()
        request = RequestFactory().post("/plugins/librenms_plugin/verify-interface/")
        request.user = user
        view.request = request
        view.kwargs = {}
        view.args = ()
        return view

    def test_out_of_scope_device_verify_raises_404(self):
        """The verify endpoint 404s an out-of-scope device even with its ports cache warm (no data leak)."""
        from django.core.cache import cache as real_cache
        from django.http import Http404

        in_scope = _make_gate_device(name="scope-in")
        out_of_scope = _make_gate_device(name="scope-out")
        user = self._constrained_user(in_scope)
        view = self._interface_verify_view(user)

        # Warm the OUT-OF-SCOPE device's ports cache: with the old raw get_object_or_404 the device
        # resolves and the endpoint returns its cached row (the leak). restrict() drops it first.
        real_cache.set(
            view.get_cache_key(out_of_scope, "ports", self.SERVER_KEY),
            {"ports": [{"ifName": "Gi0/0", "port_id": 1}]},
        )
        request = RequestFactory().post(
            "/plugins/librenms_plugin/verify-interface/",
            data=json.dumps(
                {
                    "device_id": out_of_scope.pk,
                    "interface_name": "Gi0/0",
                    "interface_name_field": "ifName",
                    "server_key": self.SERVER_KEY,
                }
            ),
            content_type="application/json",
        )
        request.user = user
        view.request = request
        # An out-of-scope pk 404s at the lookup like a nonexistent one — never reaching the cache
        # read (Django's middleware renders Http404 as a 404 for a real request).
        with pytest.raises(Http404):
            view.post(request)

    def test_grant_scopes_lookup_to_covered_device(self):
        """The restricted queryset the endpoint uses covers the in-scope device and excludes others."""
        from dcim.models import Device

        in_scope = _make_gate_device(name="scope-in-2")
        out_of_scope = _make_gate_device(name="scope-out-2")
        user = self._constrained_user(in_scope)

        scoped = Device.objects.restrict(user, "view")
        assert scoped.filter(pk=in_scope.pk).exists() is True
        assert scoped.filter(pk=out_of_scope.pk).exists() is False
