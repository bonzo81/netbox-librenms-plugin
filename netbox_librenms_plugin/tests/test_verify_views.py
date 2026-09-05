"""Tests for SingleCableVerifyView and SingleInterfaceVerifyView VC resolution.

Verifies that both views delegate VC device resolution to
get_librenms_sync_device() and handle the None return gracefully
(e.g. empty VC members or vc_position type errors).
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory

from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView
from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView


def _make_request(body: dict) -> MagicMock:
    """Create a mock POST request with JSON body."""
    request = MagicMock()
    request.body = json.dumps(body).encode()
    request.user.has_perm.return_value = True
    return request


def _verify_superuser(tag):
    """A real superuser so the object-perm gate passes and restrict() resolves the real device."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_superuser(username=f"vv-{tag}", email="", password="x")


def _real_vc_device(tag, name=None):
    """A real Device that belongs to a VirtualChassis (so post() takes the VC sync-resolution branch)."""
    from dcim.models import VirtualChassis

    device = _make_gate_device(name=name or f"vc-{tag}")
    vc = VirtualChassis.objects.create(name=f"VV-VC-{tag}")
    device.virtual_chassis = vc
    device.vc_position = 1
    device.save()
    return device


def _real_verify_view(view_cls, body, user, *, server_key="default"):
    """Build a real verify view + real request; _librenms_api is stubbed only to supply the active-server key."""
    from django.test import RequestFactory

    view = view_cls()
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = server_key
    request = RequestFactory().post("/verify/", data=json.dumps(body), content_type="application/json")
    request.user = user
    view.request = request
    view.kwargs = {}
    view.args = ()
    return view, request


# ---------------------------------------------------------------------------
# SingleCableVerifyView
# ---------------------------------------------------------------------------
class TestSingleCableVerifyView:
    """SingleCableVerifyView.post() VC resolution and None guard (real device, real gate/restrict)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        view.require_object_permissions_json = MagicMock(return_value=None)
        return view

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.base.cables_view.cache")
    def test_vc_no_resolvable_sync_device_returns_empty_row(self, mock_cache, mock_sync):
        """VC where get_librenms_sync_device returns None -> empty row, no crash."""
        device = _real_vc_device("cbl-nosync")
        mock_sync.return_value = None
        view, request = _real_verify_view(
            SingleCableVerifyView, {"device_id": device.pk, "local_port_id": "42"}, _verify_superuser("cbl-nosync")
        )
        response = view.post(request)

        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["formatted_row"]["cable_status"] == "Missing Ports"
        mock_cache.get.assert_not_called()

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.base.cables_view.cache")
    def test_vc_resolved_sync_device_uses_cache(self, mock_cache, mock_sync):
        """VC with a resolved sync device: cache is queried with that device's key."""
        device = _real_vc_device("cbl-sync")
        sync_device = _real_vc_device("cbl-syncmember", name="cbl-sync-member")
        mock_sync.return_value = sync_device
        mock_cache.get.return_value = None
        view, request = _real_verify_view(
            SingleCableVerifyView, {"device_id": device.pk, "local_port_id": "42"}, _verify_superuser("cbl-sync")
        )
        view.post(request)

        mock_sync.assert_called_once_with(device, server_key=view._librenms_api.server_key)
        mock_cache.get.assert_called_once()
        cache_key = mock_cache.get.call_args[0][0]
        assert "device" in cache_key
        assert str(sync_device.pk) in cache_key

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.base.cables_view.cache")
    def test_non_vc_device_skips_sync_device_lookup(self, mock_cache, mock_sync):
        """Non-VC device: get_librenms_sync_device is NOT called."""
        device = _make_gate_device(name="cbl-nonvc")
        mock_cache.get.return_value = None
        view, request = _real_verify_view(
            SingleCableVerifyView, {"device_id": device.pk, "local_port_id": "10"}, _verify_superuser("cbl-nonvc")
        )
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

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.base.cables_view.cache")
    def test_malformed_cached_links_fail_closed(self, mock_cache, mock_sync):
        """A corrupt cached links entry must fail closed (empty row + purge), not 500 the verify path."""
        device = _make_gate_device(name="cbl-malformed")
        # A non-dict link row: the old code iterates it and calls str.get(...) -> AttributeError.
        mock_cache.get.return_value = {"links": ["not-a-dict"]}
        view, request = _real_verify_view(
            SingleCableVerifyView, {"device_id": device.pk, "local_port_id": "10"}, _verify_superuser("cbl-malformed")
        )
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
    """SingleInterfaceVerifyView.post() VC resolution and None guard (real device, real gate/restrict)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        view = object.__new__(SingleInterfaceVerifyView)
        view._librenms_api = MagicMock()
        view.require_object_permissions_json = MagicMock(return_value=None)
        return view

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.object_sync.devices.cache")
    def test_vc_no_resolvable_sync_device_returns_404(self, mock_cache, mock_sync):
        """VC where get_librenms_sync_device returns None -> 404 JSON error, no crash."""
        device = _real_vc_device("if-nosync")
        mock_sync.return_value = None
        view, request = _real_verify_view(
            SingleInterfaceVerifyView,
            {"device_id": device.pk, "interface_name": "eth0", "interface_name_field": "ifName"},
            _verify_superuser("if-nosync"),
        )
        response = view.post(request)

        assert response.status_code == 404
        data = json.loads(response.content)
        assert data["status"] == "error"
        assert "sync device" in data["message"].lower()
        mock_cache.get.assert_not_called()

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.object_sync.devices.cache")
    def test_vc_resolved_sync_device_uses_cache(self, mock_cache, mock_sync):
        """VC with a resolved sync device: cache is queried with that device's key."""
        device = _real_vc_device("if-sync")
        sync_device = _real_vc_device("if-syncmember", name="if-sync-member")
        mock_sync.return_value = sync_device
        mock_cache.get.return_value = None
        view, request = _real_verify_view(
            SingleInterfaceVerifyView,
            {"device_id": device.pk, "interface_name": "eth0", "interface_name_field": "ifName"},
            _verify_superuser("if-sync"),
        )
        view.post(request)

        mock_sync.assert_called_once_with(device, server_key=view._librenms_api.server_key)
        mock_cache.get.assert_called_once()
        cache_key = mock_cache.get.call_args[0][0]
        assert str(sync_device.pk) in cache_key

    @pytest.mark.django_db
    @patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.object_sync.devices.cache")
    def test_non_vc_device_skips_sync_device_lookup(self, mock_cache, mock_sync):
        """Non-VC device: get_librenms_sync_device is NOT called."""
        device = _make_gate_device(name="if-nonvc")
        mock_cache.get.return_value = None
        view, request = _real_verify_view(
            SingleInterfaceVerifyView,
            {"device_id": device.pk, "interface_name": "eth0", "interface_name_field": "ifName"},
            _verify_superuser("if-nonvc"),
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

    @pytest.mark.django_db
    def test_verify_does_not_name_match_interface_bound_to_another_port(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("verify-conflicting-port-id")
        wrong_interface = make_interface(device, "Ethernet1")
        set_librenms_device_id(wrong_interface, 30, "default")
        wrong_interface.save()
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 20,
                        "ifName": "Ethernet1",
                        "ifDescr": "Ethernet1",
                        "ifAlias": "",
                        "ifType": "ethernetCsmacd",
                        "ifSpeed": 1_000_000_000,
                        "ifPhysAddress": "",
                        "ifMtu": 1500,
                        "ifAdminStatus": "up",
                        "_source": "host",
                    }
                ],
                "port_stack_relationships": {},
            },
        )
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": device.pk,
                    "interface_name": "Ethernet1",
                    "interface_name_field": "ifName",
                    "port_id": 20,
                }
            ),
            user=_verify_superuser("conflicting-port-id"),
            path="/verify/",
            content_type="application/json",
        )

        try:
            response = view.post(request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200
        row = json.loads(response.content)["formatted_row"]
        assert "text-danger" in row["name"]

    @pytest.mark.django_db
    def test_verify_member_switch_returns_the_selected_members_vlan_group(self):
        from django.contrib.contenttypes.models import ContentType
        from django.core.cache import cache
        from dcim.models import Rack
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        _virtual_chassis, (member1, member2) = make_virtual_chassis_members("verify-vlan-owner")
        rack1 = Rack.objects.create(name="Verify VLAN Rack 1", site=member1.site, status="active")
        rack2 = Rack.objects.create(name="Verify VLAN Rack 2", site=member1.site, status="active")
        member1.rack = rack1
        member2.rack = rack2
        member1.save()
        member2.save()
        rack_type = ContentType.objects.get_for_model(Rack)
        group1 = VLANGroup.objects.create(
            name="Verify VLAN Group 1",
            slug="verify-vlan-group-1",
            scope_type=rack_type,
            scope_id=rack1.pk,
        )
        group2 = VLANGroup.objects.create(
            name="Verify VLAN Group 2",
            slug="verify-vlan-group-2",
            scope_type=rack_type,
            scope_id=rack2.pk,
        )
        VLAN.objects.create(vid=100, name="Verify Rack 1 VLAN", group=group1, status="active")
        VLAN.objects.create(vid=100, name="Verify Rack 2 VLAN", group=group2, status="active")
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(member1, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet2",
                        "ifDescr": "Ethernet2",
                        "ifAlias": "",
                        "ifType": "ethernetCsmacd",
                        "ifSpeed": 1_000_000_000,
                        "ifPhysAddress": "",
                        "ifMtu": 1500,
                        "ifAdminStatus": "up",
                        "untagged_vlan": 100,
                        "tagged_vlans": [],
                    }
                ],
                "port_stack_relationships": {},
            },
        )
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": member2.pk,
                    "interface_name": "Ethernet2",
                    "interface_name_field": "ifName",
                    "port_id": 10,
                }
            ),
            user=_verify_superuser("vlan-owner"),
            path="/verify/",
            content_type="application/json",
        )

        try:
            response = view.post(request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200
        vlan_cell = json.loads(response.content)["formatted_row"]["vlans"]
        assert f'name="vlan_group_10_100" value="{group2.pk}"' in vlan_cell
        assert f'name="vlan_group_10_100" value="{group1.pk}"' not in vlan_cell

    @pytest.mark.django_db
    def test_verify_normalizes_relationship_map_keys(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("verify-string-relationship-key")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        child.save()
        parent.save()
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": child.name,
                        "ifDescr": child.name,
                        "ifAlias": "",
                        "ifType": "l2vlan",
                        "ifSpeed": 1_000_000_000,
                        "ifPhysAddress": "",
                        "ifMtu": 1500,
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 20,
                        "ifName": parent.name,
                        "ifDescr": parent.name,
                        "ifAlias": "",
                        "ifType": "ethernetCsmacd",
                        "ifSpeed": 1_000_000_000,
                        "ifPhysAddress": "",
                        "ifMtu": 1500,
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {"10": 20}},
            },
        )
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": device.pk,
                    "interface_name_field": "ifName",
                    "port_id": 10,
                }
            ),
            user=_verify_superuser("string-relationship-key"),
            path="/verify/",
            content_type="application/json",
        )

        try:
            response = view.post(request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200
        assert parent.name in json.loads(response.content)["formatted_row"]["parent"]

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "bad_relationships",
        [
            None,
            ["not", "a", "dict"],
            "garbage",
            {"lag_members": None, "sub_interfaces": None},
            {"lag_members": "nope", "sub_interfaces": 42},
        ],
    )
    def test_verify_handles_malformed_relationship_cache(self, bad_relationships):
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        device = make_device("verify-malformed-relationships")
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Et1",
                        "ifDescr": "Et1",
                        "ifAlias": "",
                        "ifType": "ethernetCsmacd",
                        "ifSpeed": 1_000_000_000,
                        "ifPhysAddress": "",
                        "ifMtu": 1500,
                        "ifAdminStatus": "up",
                    }
                ],
                "port_stack_relationships": bad_relationships,
            },
        )
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": device.pk,
                    "interface_name_field": "ifName",
                    "port_id": 10,
                }
            ),
            user=_verify_superuser(f"malformed-{type(bad_relationships).__name__}"),
            path="/verify/",
            content_type="application/json",
        )

        try:
            response = view.post(request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200
        row = json.loads(response.content)["formatted_row"]
        assert "Parent" not in row["parent"]

    @pytest.mark.django_db
    def test_verify_missed_port_id_does_not_repaint_same_named_row(self):
        """A supplied port_id is authoritative: if it misses, do NOT fall back to a same-named row."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        device = make_device("verify-authoritative-pid")
        user = _verify_superuser("authoritative-pid")
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 99,
                        "ifName": "Et1",
                        "ifDescr": "Et1",
                        "ifAlias": "",
                        "ifType": "ethernetCsmacd",
                        "ifSpeed": 1_000_000_000,
                        "ifPhysAddress": "",
                        "ifMtu": 1500,
                        "ifAdminStatus": "up",
                        "_source": "host",
                    }
                ]
            },
        )
        matching_request = make_request(
            "post",
            json.dumps(
                {"device_id": device.pk, "interface_name": "Et1", "interface_name_field": "ifName", "port_id": 99}
            ),
            user=user,
            path="/verify/",
            content_type="application/json",
        )
        missed_request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": device.pk,
                    "interface_name": "Et1",
                    "interface_name_field": "ifName",
                    "port_id": 777,
                }
            ),
            user=user,
            path="/verify/",
            content_type="application/json",
        )

        try:
            assert view.post(matching_request).status_code == 200
            response = view.post(missed_request)
        finally:
            cache.delete(cache_key)

        # The authoritative-but-missed id yields "not found", never a wrong-row repaint.
        assert response.status_code == 404
        data = json.loads(response.content)
        assert data["status"] == "error"

    @pytest.mark.django_db
    def test_verify_name_hint_derived_from_matched_row(self):
        """The interface name fallback is taken from the port_id-matched cached row, not the posted display name."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        device = make_device("verify-namehint")
        make_interface(device, "Et1")
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Et1",
                        "ifDescr": "Et1",
                        "ifAlias": "",
                        "ifType": "ethernetCsmacd",
                        "ifSpeed": 1_000_000_000,
                        "ifPhysAddress": "",
                        "ifMtu": 1500,
                        "ifAdminStatus": "up",
                    }
                ]
            },
        )
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": device.pk,
                    "interface_name": "stale-display-name",
                    "interface_name_field": "ifName",
                    "port_id": 10,
                }
            ),
            user=_verify_superuser("namehint"),
            path="/verify/",
            content_type="application/json",
        )

        try:
            response = view.post(request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200
        row = json.loads(response.content)["formatted_row"]
        assert "text-success" in row["name"]
        assert "stale-display-name" not in row["name"]

    @pytest.mark.django_db
    def test_table_and_verify_resolve_the_same_stable_id_row(self, client):
        """The table and verify endpoint must prefer the same stable-ID interface."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        device = make_device("verify-table-resolution")
        stable_match = make_interface(device, "NetBoxStable")
        name_candidate = make_interface(device, "Ethernet1")
        set_librenms_device_id(stable_match, 10, server_key)
        stable_match.mtu = 9000
        stable_match.save()
        name_candidate.mtu = 1500
        name_candidate.save()
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": "Ethernet1",
                    "ifDescr": "Ethernet1",
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 9000,
                    "ifAdminStatus": "up",
                }
            ],
            "port_stack_relationships": {},
        }
        user = _verify_superuser("table-resolution")
        table_request = make_request("get", user=user)
        table_view = DeviceInterfaceTableView()
        table_view._librenms_api = LibreNMSAPI(server_key)
        table_view.request = table_request
        cache_key = table_view.get_cache_key(device, "ports", server_key)
        cache.set(cache_key, snapshot)

        try:
            context = table_view.get_context_data(
                table_request,
                device,
                "ifName",
                server_key,
                fresh_data=snapshot,
                sync_device=device,
            )
            table_row = snapshot["ports"][0]
            table_name = str(context["table"].render_name(table_row["ifName"], table_row))
            table_mtu = str(context["table"].render_mtu(table_row["ifMtu"], table_row))

            client.force_login(user)
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:verify_interface"),
                data=json.dumps(
                    {
                        "device_id": device.pk,
                        "server_key": server_key,
                        "interface_name_field": "ifName",
                        "port_id": 10,
                    }
                ),
                content_type="application/json",
            )
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200, response.content
        formatted_row = json.loads(response.content)["formatted_row"]
        verify_name = formatted_row["name"]
        assert verify_name == table_name
        assert formatted_row["mtu"] == table_mtu
        assert "text-success" in formatted_row["mtu"]

    @pytest.mark.django_db
    def test_verify_keeps_view_only_interface_match_without_offering_relationship_write(self):
        from django.core.cache import cache
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("verify-view-only-match")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        child.save()
        parent.save()
        user = make_user_with_perms(
            "verify-view-only-match",
            [("view", Device), ("view", Interface)],
        )
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": child.name,
                    "ifDescr": child.name,
                    "ifAlias": "",
                    "ifType": "l2vlan",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
                {
                    "port_id": 20,
                    "ifName": parent.name,
                    "ifDescr": parent.name,
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": device.pk,
                    "interface_name_field": "ifName",
                    "port_id": 10,
                }
            ),
            user=user,
            path="/verify/",
            content_type="application/json",
        )

        try:
            response = view.post(request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200
        row = json.loads(response.content)["formatted_row"]
        assert "text-success" in row["name"]
        assert "parent-sync-btn" not in row["parent"]

    @pytest.mark.django_db
    def test_plugin_read_only_user_never_receives_relationship_write_button(self):
        from django.apps import apps
        from django.core.cache import cache
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import (
            grant,
            make_request,
            make_user_with_perms,
        )
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("verify-plugin-read-only")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        child.save()
        parent.save()
        user = make_user_with_perms(
            "verify-plugin-read-only",
            [("view", Device), ("view", Interface), ("change", Interface)],
            plugin_write=False,
        )
        settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        user = grant(user, "view", settings_model)
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": child.name,
                    "ifDescr": child.name,
                    "ifAlias": "",
                    "ifType": "l2vlan",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
                {
                    "port_id": 20,
                    "ifName": parent.name,
                    "ifDescr": parent.name,
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_request = make_request("get", user=user)
        table_view = DeviceInterfaceTableView()
        table_view._librenms_api = api
        table_view.request = table_request
        cache_key = table_view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)

        try:
            context = table_view.get_context_data(
                table_request,
                device,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=device,
            )
            table_html = str(context["table"].render_parent(None, snapshot["ports"][0]))

            verify_view = SingleInterfaceVerifyView()
            verify_view._librenms_api = api
            verify_request = make_request(
                "post",
                json.dumps({"device_id": device.pk, "interface_name_field": "ifName", "port_id": 10}),
                user=user,
                path="/verify/",
                content_type="application/json",
            )
            verify_response = verify_view.post(verify_request)
        finally:
            cache.delete(cache_key)

        assert verify_response.status_code == 200, verify_response.content
        assert "parent-sync-btn" not in table_html
        assert "parent-sync-btn" not in json.loads(verify_response.content)["formatted_row"]["parent"]

    @pytest.mark.django_db
    @pytest.mark.parametrize("migrated", [False, True], ids=["active-page", "migrated-page"])
    def test_virtual_chassis_member_verify_uses_the_origin_page_mode(self, migrated):
        """Member verification must keep the page mode and target the selected member."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import (
            make_device,
            make_interface,
            make_virtual_chassis_members,
        )
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser
        from netbox_librenms_plugin.utils import mark_librenms_migrated, set_librenms_device_id

        _virtual_chassis, (page_device, selected_device) = make_virtual_chassis_members("verify-migrated-page")
        winner = make_device("verify-migrated-winner")
        child = make_interface(selected_device, "Ethernet2.100", iface_type="virtual")
        parent = make_interface(selected_device, "Ethernet2")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        child.save()
        parent.save()
        if migrated:
            mark_librenms_migrated(page_device, winner.pk, "default")
            page_device.save()
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": child.name,
                    "ifDescr": child.name,
                    "ifAlias": "",
                    "ifType": "l2vlan",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
                {
                    "port_id": 20,
                    "ifName": parent.name,
                    "ifDescr": parent.name,
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(cache_key, snapshot)
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": selected_device.pk,
                    "origin_device_id": page_device.pk,
                    "interface_name_field": "ifName",
                    "port_id": 10,
                }
            ),
            user=make_superuser("verify-migrated-page"),
            path="/verify/",
            content_type="application/json",
        )

        try:
            response = view.post(request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200, response.content
        parent_html = json.loads(response.content)["formatted_row"]["parent"]
        if migrated:
            assert "parent-sync-btn" not in parent_html
        else:
            assert "parent-sync-btn" in parent_html
            assert f"/device/{selected_device.pk}/sync-interface-parent/" in parent_html

    @pytest.mark.django_db
    def test_verify_response_does_not_expose_inaccessible_vc_members(self):
        from django.core.cache import cache
        from dcim.models import Device

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms

        _virtual_chassis, (page_device, selected_device, hidden_device) = make_virtual_chassis_members(
            "verify-member-scope",
            count=3,
        )
        user = make_user_with_perms("verify-member-scope", [])
        user = grant(user, "view", Device, constraints={"pk": page_device.pk})
        user = grant(user, "view", Device, constraints={"pk": selected_device.pk})
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": "Ethernet2",
                    "ifDescr": "Ethernet2",
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                }
            ],
            "port_stack_relationships": {},
        }
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(cache_key, snapshot)
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": selected_device.pk,
                    "origin_device_id": page_device.pk,
                    "interface_name_field": "ifName",
                    "port_id": 10,
                }
            ),
            user=user,
            path="/verify/",
            content_type="application/json",
        )

        try:
            response = view.post(request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200, response.content
        formatted_row = json.loads(response.content)["formatted_row"]
        body = response.content.decode()
        assert "device_selection" not in formatted_row
        assert hidden_device.name not in body
        # Match the pk as a device REFERENCE, not as a bare substring: `str(pk) in body`
        # also matches any rendered number (ifMtu 1500 collided with pk 1500), so the old
        # assertion failed on pk allocation rather than on a member actually leaking.
        assert f"/dcim/devices/{hidden_device.pk}/" not in body

    @pytest.mark.django_db
    def test_hidden_related_owner_stays_unavailable_through_verify_and_inline_post(self):
        from types import SimpleNamespace

        from django.core.cache import cache
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_interface, make_virtual_chassis_members
        from netbox_librenms_plugin.tests.view_test_helpers import (
            grant,
            make_request,
            make_user_with_perms,
            post,
        )
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        _virtual_chassis, (page_device, hidden_parent_device) = make_virtual_chassis_members(
            "verify-hidden-parent-owner"
        )
        child = make_interface(page_device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(hidden_parent_device, "Ethernet2")
        # Use the real configured key. The devcontainer names it ``stub`` while CI names it
        # ``default``; a hardcoded key makes the request fail before the permission behavior.
        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        set_librenms_device_id(child, 10, server_key)
        set_librenms_device_id(parent, 20, server_key)
        child.save()
        parent.save()
        user = make_user_with_perms("verify-hidden-parent-owner", [])
        user = grant(user, "view", Device, constraints={"pk": page_device.pk})
        user = grant(user, "change", Interface, constraints={"pk": child.pk})
        user = grant(user, "view", Interface, constraints={"pk": parent.pk})
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": child.name,
                    "ifDescr": child.name,
                    "ifAlias": "",
                    "ifType": "l2vlan",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
                {
                    "port_id": 20,
                    "ifName": parent.name,
                    "ifDescr": parent.name,
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        api = object.__new__(LibreNMSAPI)
        api.server_key = server_key
        table_request = make_request("get", user=user)
        table_view = DeviceInterfaceTableView()
        table_view._librenms_api = api
        table_view.request = table_request
        cache_key = table_view.get_cache_key(page_device, "ports", server_key)
        cache.set(cache_key, snapshot)

        try:
            context = table_view.get_context_data(
                table_request,
                page_device,
                "ifName",
                server_key,
                fresh_data=snapshot,
                sync_device=page_device,
            )
            assert "parent-sync-btn" not in str(context["table"].render_parent(None, snapshot["ports"][0]))

            verify_view = SingleInterfaceVerifyView()
            verify_view._librenms_api = api
            verify_request = make_request(
                "post",
                json.dumps(
                    {
                        "device_id": page_device.pk,
                        "server_key": server_key,
                        "interface_name_field": "ifName",
                        "port_id": 10,
                    }
                ),
                user=user,
                path="/verify/",
                content_type="application/json",
            )
            verify_response = verify_view.post(verify_request)
            assert verify_response.status_code == 200, verify_response.content
            assert "parent-sync-btn" not in json.loads(verify_response.content)["formatted_row"]["parent"]

            relationship_view = SyncInterfaceParentView()
            relationship_view._librenms_api = SimpleNamespace(server_key=server_key)
            relationship_request = make_request(
                "post",
                {
                    "port_id": "10",
                    "parent_port_id": "20",
                    "server_key": server_key,
                },
                user=user,
            )
            relationship_response = post(
                relationship_view,
                relationship_request,
                object_type="device",
                object_id=page_device.pk,
            )
        finally:
            cache.delete(cache_key)

        assert relationship_response.status_code == 404, relationship_response.content
        child.refresh_from_db()
        assert child.parent_id is None

    @pytest.mark.django_db
    @pytest.mark.parametrize("invalid_origin_id", [[], {}])
    def test_verify_rejects_malformed_origin_device_id(self, invalid_origin_id):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        device = make_device("verify-malformed-origin")
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": device.pk,
                    "origin_device_id": invalid_origin_id,
                    "interface_name_field": "ifName",
                    "port_id": 10,
                }
            ),
            user=_verify_superuser(f"malformed-origin-{type(invalid_origin_id).__name__}"),
            path="/verify/",
            content_type="application/json",
        )

        response = view.post(request)

        assert response.status_code == 400
        assert json.loads(response.content)["status"] == "error"

    @pytest.mark.django_db
    def test_view_only_non_lag_target_never_receives_lag_promotion_button(self):
        from types import SimpleNamespace

        from django.core.cache import cache
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import (
            grant,
            make_request,
            make_user_with_perms,
            post,
        )
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        server_key = next(iter(LibreNMSAPI.get_available_servers()))
        device = make_device("verify-view-only-lag-target")
        member = make_interface(device, "Ethernet1")
        aggregate = make_interface(device, "Port-Channel1", iface_type="other")
        set_librenms_device_id(member, 10, server_key)
        set_librenms_device_id(aggregate, 20, server_key)
        member.save()
        aggregate.save()
        user = make_user_with_perms("verify-view-only-lag-target", [("view", Device)])
        user = grant(user, "change", Interface, constraints={"pk": member.pk})
        user = grant(user, "view", Interface, constraints={"pk": aggregate.pk})
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": member.name,
                    "ifDescr": member.name,
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
                {
                    "port_id": 20,
                    "ifName": aggregate.name,
                    "ifDescr": aggregate.name,
                    "ifAlias": "",
                    "ifType": "ieee8023adLag",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
            ],
            "port_stack_relationships": {"lag_members": {10: 20}, "sub_interfaces": {}},
        }
        api = object.__new__(LibreNMSAPI)
        api.server_key = server_key
        table_request = make_request("get", user=user)
        table_view = DeviceInterfaceTableView()
        table_view._librenms_api = api
        table_view.request = table_request
        cache_key = table_view.get_cache_key(device, "ports", server_key)
        cache.set(cache_key, snapshot)

        try:
            context = table_view.get_context_data(
                table_request,
                device,
                "ifName",
                server_key,
                fresh_data=snapshot,
                sync_device=device,
            )
            table_html = str(context["table"].render_parent(None, snapshot["ports"][0]))

            verify_view = SingleInterfaceVerifyView()
            verify_view._librenms_api = api
            verify_request = make_request(
                "post",
                json.dumps({"device_id": device.pk, "interface_name_field": "ifName", "port_id": 10}),
                user=user,
                path="/verify/",
                content_type="application/json",
            )
            verify_response = verify_view.post(verify_request)

            inline_view = SyncInterfaceLagView()
            inline_view._librenms_api = SimpleNamespace(server_key=server_key)
            inline_request = make_request(
                "post",
                {"port_id": "10", "lag_port_id": "20", "server_key": server_key},
                user=user,
            )
            inline_response = post(
                inline_view,
                inline_request,
                object_type="device",
                object_id=device.pk,
            )
        finally:
            cache.delete(cache_key)

        assert verify_response.status_code == 200, verify_response.content
        # Pin the refusal status: "!= 200" also passes for a 500 from an unrelated crash, and the
        # state assertions below hold after any failed request because nothing was written.
        assert inline_response.status_code == 403, inline_response.content
        assert "lag-sync-btn" not in table_html
        assert "lag-sync-btn" not in json.loads(verify_response.content)["formatted_row"]["parent"]
        member.refresh_from_db()
        aggregate.refresh_from_db()
        assert member.lag_id is None
        assert aggregate.type != "lag"

    @pytest.mark.django_db
    def test_verify_falls_back_when_interface_name_field_is_not_a_string(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        device = make_device("verify-malformed-name-field")
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": "Ethernet1",
                    "ifDescr": "Ethernet1",
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                }
            ],
            "port_stack_relationships": {},
        }
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        request = make_request(
            "post",
            json.dumps(
                {
                    "device_id": device.pk,
                    "interface_name_field": ["ifName"],
                    "port_id": 10,
                }
            ),
            user=_verify_superuser("malformed-name-field"),
            path="/verify/",
            content_type="application/json",
        )

        try:
            response = view.post(request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200
        # 200 alone does not say how the list was handled: a regression that coerced it into a
        # column name would also return 200. Pin the fallback to the default name column.
        assert "Ethernet1" in json.loads(response.content)["formatted_row"]["name"]

    @pytest.mark.django_db
    def test_verify_materializes_only_relationship_candidates(self):
        from django.core.cache import cache
        from django.db.models.signals import post_init
        from dcim.models import Interface

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("verify-candidate-scope")
        source = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(source, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        source.save()
        parent.save()
        for index in range(40):
            make_interface(device, f"unrelated-{index}")
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": source.name,
                    "ifDescr": source.name,
                    "ifAlias": "",
                    "ifType": "l2vlan",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
                {
                    "port_id": 20,
                    "ifName": parent.name,
                    "ifDescr": parent.name,
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                },
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        view = SingleInterfaceVerifyView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        request = make_request(
            "post",
            json.dumps({"device_id": device.pk, "interface_name_field": "ifName", "port_id": 10}),
            user=_verify_superuser("candidate-scope"),
            path="/verify/",
            content_type="application/json",
        )
        materialized_interface_ids = []

        def capture_interface(instance, **_kwargs):
            materialized_interface_ids.append(instance.pk)

        post_init.connect(capture_interface, sender=Interface, weak=False)
        try:
            response = view.post(request)
        finally:
            post_init.disconnect(capture_interface, sender=Interface)
            cache.delete(cache_key)

        assert response.status_code == 200
        assert len(materialized_interface_ids) <= 10


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


@pytest.mark.django_db
class TestSaveVlanGroupOverridesObjectScope:
    """SaveVlanGroupOverridesView WRITES overrides, so it must object-scope the device too.

    require_write_permission_json only checks plugin-wide write access, so a plugin-writer with a
    *constrained* view_device grant could otherwise persist VLAN overrides for a device they can't
    see. The lookup must go through restrict() so an out-of-scope pk 404s.
    """

    SERVER_KEY = "default"

    @staticmethod
    def _plugin_writer_scoped_to(in_scope_device):
        """A real user with plugin write access AND a pk-constrained view_device grant (no superuser)."""
        from core.models import ObjectType
        from dcim.models import Device
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        LibreNMSSettings = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")

        user = get_user_model().objects.create_user(username="scoped-writer", password="x")

        write = ObjectPermission.objects.create(name="plugin-write", actions=["change"])
        write.object_types.set([ObjectType.objects.get_for_model(LibreNMSSettings)])
        write.users.set([user])

        view_dev = ObjectPermission.objects.create(
            name="scoped-view-dev", actions=["view"], constraints={"pk": in_scope_device.pk}
        )
        view_dev.object_types.set([ObjectType.objects.get_for_model(Device)])
        view_dev.users.set([user])

        return get_user_model().objects.get(pk=user.pk)  # clear the per-request perm cache

    def _view_and_request(self, user, device_pk):
        from netbox_librenms_plugin.views.object_sync.devices import SaveVlanGroupOverridesView

        view = SaveVlanGroupOverridesView()
        request = RequestFactory().post(
            "/plugins/librenms_plugin/save-vlan-overrides/",
            data=json.dumps({"device_id": device_pk, "vid_group_map": {"10": 3}, "server_key": self.SERVER_KEY}),
            content_type="application/json",
        )
        request.user = user
        view.request = request
        view.kwargs = {}
        view.args = ()
        return view, request

    def test_out_of_scope_device_is_blocked(self):
        """A plugin-writer scoped to another device cannot persist overrides for an out-of-scope device."""
        from django.core.cache import cache as real_cache
        from django.http import Http404

        in_scope = _make_gate_device(name="ovr-in")
        out_of_scope = _make_gate_device(name="ovr-out")
        user = self._plugin_writer_scoped_to(in_scope)
        view, request = self._view_and_request(user, out_of_scope.pk)

        overrides_key = view.get_vlan_overrides_key(out_of_scope, self.SERVER_KEY)
        real_cache.delete(overrides_key)
        # Even with the ports cache warm (so the write path would otherwise proceed), the device
        # must 404 at the restricted lookup before anything is persisted.
        real_cache.set(view.get_cache_key(out_of_scope, "ports", self.SERVER_KEY), {"ports": []}, timeout=300)
        try:
            with pytest.raises(Http404):
                view.post(request)
            assert real_cache.get(overrides_key) is None  # nothing written for the out-of-scope device
        finally:
            real_cache.delete(overrides_key)
            real_cache.delete(view.get_cache_key(out_of_scope, "ports", self.SERVER_KEY))

    def test_in_scope_device_resolves_past_the_gate(self):
        """The device the grant DOES cover resolves through restrict() (no over-block — reaches the write path)."""
        from django.core.cache import cache as real_cache
        from django.http import Http404, JsonResponse

        in_scope = _make_gate_device(name="ovr-in-2")
        user = self._plugin_writer_scoped_to(in_scope)
        view, request = self._view_and_request(user, in_scope.pk)
        try:
            # Must NOT 404: an in-scope device is resolvable. (It then hits the ports-cache/TTL check,
            # returning a JsonResponse either way — the point is the restricted lookup didn't block it.)
            try:
                response = view.post(request)
            except Http404:
                pytest.fail("in-scope device was wrongly blocked by restrict()")
            assert isinstance(response, JsonResponse)
        finally:
            real_cache.delete(view.get_vlan_overrides_key(in_scope, self.SERVER_KEY))


# ---------------------------------------------------------------------------
# SingleModuleVerifyView — object-permission ordering (device enumeration guard)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSingleModuleVerifyPermissionOrder:
    """SingleModuleVerifyView.post() must reject a caller lacking dcim.view_device before resolving the device (no 404-vs-403 enumeration)."""

    def _view_for(self, user):
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import SingleModuleVerifyView

        request = RequestFactory().post(
            "/verify-module/",
            data=json.dumps({"device_id": 999_999_999, "ent_physical_index": 1, "server_key": "default"}),
            content_type="application/json",
        )
        request.user = user
        view = SingleModuleVerifyView()
        view.request = request
        return view, request

    def test_missing_view_device_perm_returns_403_not_404_for_absent_device(self):
        """A real user without dcim.view_device POSTing a non-existent device_id gets 403, not 404 (real ObjectPermissionBackend decides has_perm)."""
        from django.contrib.auth import get_user_model
        from django.http import Http404

        user = get_user_model().objects.create_user("no-view-device")  # non-superuser, no object perms
        view, request = self._view_for(user)

        try:
            response = view.post(request)
        except Http404:
            pytest.fail(
                "get_object_or_404(Device) ran before the permission check: a caller without "
                "dcim.view_device can enumerate devices by observing 404-vs-403."
            )
        assert response.status_code == 403
        assert "view_device" in json.loads(response.content)["error"]


@pytest.mark.django_db
def test_verify_rejects_a_device_id_beyond_the_bigint_range():
    """An oversized primary key must fail validation here, not in the database driver."""
    import json as json_module

    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.utils import _POSTGRES_BIGINT_MAX
    from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

    view = SingleInterfaceVerifyView()
    request = make_request(
        "post",
        json_module.dumps({"device_id": _POSTGRES_BIGINT_MAX + 1, "port_id": 10}),
        user=_verify_superuser("bigint-device-id"),
        path="/verify/",
        content_type="application/json",
    )

    response = view.post(request)

    assert response.status_code == 400
    assert json_module.loads(response.content)["message"] == "No device ID provided"
