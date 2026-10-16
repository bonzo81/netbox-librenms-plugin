"""
Coverage tests for remaining gaps in views/sync/.
Targets:
- interfaces.py (SyncInterfacesView + DeleteNetBoxInterfacesView) - was 34%
- cables.py lines 147-149 (exception path in process_interface_sync)
- devices.py lines 77, 81-82 (port_association_mode, invalid poller_group)
- locations.py lines 26-28, 32-35, 44-49 (get_table, get_context_data, get_queryset)
- vlans.py lines 134-139 (grouped VLAN update/skip paths).
"""

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_virtual_chassis_members, make_vm
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_user_with_perms,
    make_view,
    message_texts,
    missing_pk,
)
from netbox_librenms_plugin.tests.view_test_helpers import post as _post

# Every view here is now built with a real request and a real user, so all of it needs the DB.
pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_iv(request=None):
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    v = make_view(SyncInterfacesView, request)
    v._post_server_key = "default"
    v.object = None
    return v


def _make_dv(request=None):
    from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

    return make_view(DeleteNetBoxInterfacesView, request)


# ===========================================================================
# SyncInterfacesView.get_required_permissions_for_object_type
# ===========================================================================


class TestGetRequiredPermissionsForObjectType:
    def test_device_returns_interface_perms(self):
        from dcim.models import Interface

        v = _make_iv()
        perms = v.get_required_permissions_for_object_type("device")
        assert any(a == "add" and m is Interface for a, m in perms)
        assert any(a == "change" and m is Interface for a, m in perms)

    def test_vm_returns_vminterface_perms(self):
        from virtualization.models import VMInterface

        v = _make_iv()
        perms = v.get_required_permissions_for_object_type("virtualmachine")
        assert any(a == "add" and m is VMInterface for a, m in perms)

    def test_invalid_raises_http404(self):
        from django.http import Http404

        v = _make_iv()
        with pytest.raises(Http404):
            v.get_required_permissions_for_object_type("rack")


# ===========================================================================
# SyncInterfacesView.get_object
# ===========================================================================


class TestSyncInterfacesGetObject:
    def test_device_type(self):
        dev = make_device("getobj-device")

        assert _make_iv().get_object("device", dev.pk) == dev

    def test_vm_type(self):
        vm = make_vm("getobj-vm")

        assert _make_iv().get_object("virtualmachine", vm.pk) == vm

    def test_a_device_outside_the_grant_404s(self):
        """The pk comes from the URL, so an out-of-scope id must 404 like a missing one."""
        from dcim.models import Device
        from django.http import Http404

        make_device("getobj-mine")
        theirs = make_device("getobj-theirs")
        user = make_user_with_perms("getobj-scoped", [("view", Device)], constraints={"name": "getobj-mine"})
        v = _make_iv(make_request(user=user))

        with pytest.raises(Http404):
            v.get_object("device", theirs.pk)

    def test_invalid_raises_http404(self):
        from django.http import Http404

        v = _make_iv()
        with pytest.raises(Http404):
            v.get_object("rack", 1)


# ===========================================================================
# SyncInterfacesView.post
# ===========================================================================


# ===========================================================================
# SyncInterfacesView.sync_interface
# ===========================================================================


class TestSyncInterface:
    """Verify which real device or virtual chassis member receives each LibreNMS interface row."""

    def _v(self, request=None):
        v = _make_iv(request)
        v._lookup_maps = {}
        v._skipped_conflicts = []
        return v

    def test_device_no_vc_uses_obj(self):
        from dcim.models import Interface

        dev = make_device("sync-novc")
        v = self._v()

        v.sync_interface(dev, {"ifName": "eth0"}, [], "ifName")

        assert Interface.objects.filter(device=dev, name="eth0").exists()

    def test_device_vc_target_in_valid_ids(self):
        """A posted sibling of the same chassis is honoured: the interface lands on the sibling."""
        from dcim.models import Interface

        _vc, (host, sibling) = make_virtual_chassis_members("sync-vc-ok")
        req = make_request("post", {"device_selection_10": str(sibling.pk)})
        v = self._v(req)

        v.sync_interface(host, {"ifName": "eth0", "port_id": 10}, [], "ifName")

        assert Interface.objects.filter(device=sibling, name="eth0").exists()
        assert not Interface.objects.filter(device=host, name="eth0").exists()

    def test_device_vc_target_not_in_valid_ids_is_skipped(self):
        """An explicit device outside the chassis is refused without a fallback write."""
        from dcim.models import Interface

        _vc, (host, _sibling) = make_virtual_chassis_members("sync-vc-outsider")
        outsider = make_device("sync-vc-outsider-x")
        req = make_request("post", {"device_selection_10": str(outsider.pk)})
        v = self._v(req)

        v.sync_interface(host, {"ifName": "eth0", "port_id": 10}, [], "ifName")

        assert not Interface.objects.filter(device=host, name="eth0").exists()
        assert not Interface.objects.filter(device=outsider, name="eth0").exists()
        assert v._skipped_conflicts == ["eth0 (selected target unavailable)"]

    def test_device_no_vc_wrong_selection_is_skipped(self):
        from dcim.models import Interface

        dev = make_device("sync-novc-self")
        other = make_device("sync-novc-other")
        req = make_request("post", {"device_selection_10": str(other.pk)})
        v = self._v(req)

        v.sync_interface(dev, {"ifName": "eth0", "port_id": 10}, [], "ifName")

        assert not Interface.objects.filter(device=dev, name="eth0").exists()
        assert not Interface.objects.filter(device=other, name="eth0").exists()
        assert v._skipped_conflicts == ["eth0 (selected target unavailable)"]

    def test_device_selection_does_not_exist_is_skipped(self):
        from dcim.models import Device, Interface

        dev = make_device("sync-gone")
        absent_pk = missing_pk(Device)
        req = make_request("post", {"device_selection_10": str(absent_pk)})
        v = self._v(req)

        v.sync_interface(dev, {"ifName": "eth0", "port_id": 10}, [], "ifName")

        assert not Interface.objects.filter(device=dev, name="eth0").exists()
        assert v._skipped_conflicts == ["eth0 (selected target unavailable)"]

    def test_device_selection_outside_the_grant_is_skipped(self):
        """The posted id is client-supplied, so a constrained grant must not reach the sibling."""
        from dcim.models import Device, Interface

        _vc, (host, sibling) = make_virtual_chassis_members("sync-vc-scoped")
        user = make_user_with_perms("sync-scoped", [("view", Device)], constraints={"name": "sync-vc-scoped-m1"})
        req = make_request("post", {"device_selection_10": str(sibling.pk)}, user=user)
        v = self._v(req)

        v.sync_interface(host, {"ifName": "eth0", "port_id": 10}, [], "ifName")

        assert not Interface.objects.filter(device=host, name="eth0").exists()
        assert not Interface.objects.filter(device=sibling, name="eth0").exists()
        assert v._skipped_conflicts == ["eth0 (selected target unavailable)"]

    def test_existing_interface_outside_the_change_grant_is_skipped(self):
        """A natural-key match must not bypass the caller's constrained change grant."""
        from dcim.models import Device, Interface

        device = make_device("sync-interface-change-scope")
        hidden = make_interface(device, "eth0")
        allowed = make_interface(device, "eth1")
        user = make_user_with_perms(
            "sync-interface-change-scope",
            [("view", Device), ("add", Interface)],
        )
        user = grant(user, "change", Interface, constraints={"pk": allowed.pk})
        request = make_request("post", user=user)
        view = self._v(request)

        view.sync_interface(device, {"ifName": hidden.name}, [], "ifName")

        assert view._skipped_conflicts == ["eth0 (port already mapped elsewhere or ambiguous)"]

    def test_existing_interface_with_an_unconstrained_change_grant_is_synced(self):
        """The permission-scoped skip must disappear when the existing interface is changeable."""
        from dcim.models import Device, Interface

        device = make_device("sync-interface-change-control")
        existing = make_interface(device, "eth0")
        make_interface(device, "eth1")
        user = make_user_with_perms(
            "sync-interface-change-control",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = make_request("post", user=user)
        view = self._v(request)

        view.sync_interface(device, {"ifName": existing.name}, [], "ifName")

        assert view._skipped_conflicts == []

    def test_vm_uses_vminterface(self):
        from virtualization.models import VMInterface

        vm = make_vm("sync-vm")
        v = self._v()

        v.sync_interface(vm, {"ifName": "eth0"}, [], "ifName")

        assert VMInterface.objects.filter(virtual_machine=vm, name="eth0").exists()


# ===========================================================================
# SyncInterfacesView.get_netbox_interface_type
# ===========================================================================


class TestGetNetboxInterfaceType:
    """Type selection driven by real InterfaceTypeMapping rows and the real speed filters."""

    @staticmethod
    def _mapping(librenms_type, netbox_type, speed=None):
        from netbox_librenms_plugin.models import InterfaceTypeMapping

        return InterfaceTypeMapping.objects.create(
            librenms_type=librenms_type, netbox_type=netbox_type, librenms_speed=speed
        )

    def test_speed_mapping_found(self):
        """The highest speed row at or below the port's speed wins over the catch-all."""
        self._mapping("ethernetCsmacd", "virtual")  # NULL-speed catch-all
        self._mapping("ethernetCsmacd", "100base-tx", speed=100000)
        self._mapping("ethernetCsmacd", "1000base-t", speed=1000000)
        self._mapping("ethernetCsmacd", "10gbase-x-sfpp", speed=10000000)  # above the port speed

        result = _make_iv().get_netbox_interface_type({"ifType": "ethernetCsmacd", "ifSpeed": 1000000000})

        assert result == "1000base-t"

    def test_speed_not_found_falls_back_to_null(self):
        """No speed row at or below the port's speed → the NULL-speed row for that type."""
        self._mapping("ethernetCsmacd", "virtual")
        self._mapping("ethernetCsmacd", "10gbase-x-sfpp", speed=10000000)

        result = _make_iv().get_netbox_interface_type({"ifType": "ethernetCsmacd", "ifSpeed": 1000000})

        assert result == "virtual"

    def test_no_speed_uses_null_mapping(self):
        self._mapping("softwareLoopback", "virtual")

        result = _make_iv().get_netbox_interface_type({"ifType": "softwareLoopback", "ifSpeed": None})

        assert result == "virtual"

    def test_no_mapping_returns_no_opinion(self):
        """None, not "other": the writer must not flatten a correct type it cannot map."""
        self._mapping("ethernetCsmacd", "virtual")  # a mapping exists, but not for this type

        result = _make_iv().get_netbox_interface_type({"ifType": "unknown", "ifSpeed": None})

        assert result is None


# ===========================================================================
# DeleteNetBoxInterfacesView.get_required_permissions_for_object_type
# ===========================================================================


class TestDeleteGetRequiredPermissions:
    def test_device_delete_interface(self):
        from dcim.models import Interface

        v = _make_dv()
        perms = v.get_required_permissions_for_object_type("device")
        assert any(a == "delete" and m is Interface for a, m in perms)

    def test_vm_delete_vminterface(self):
        from virtualization.models import VMInterface

        v = _make_dv()
        perms = v.get_required_permissions_for_object_type("virtualmachine")
        assert any(a == "delete" and m is VMInterface for a, m in perms)

    def test_invalid_raises_http404(self):
        from django.http import Http404

        v = _make_dv()
        with pytest.raises(Http404):
            v.get_required_permissions_for_object_type("invalid")


# ===========================================================================
# DeleteNetBoxInterfacesView.post
# ===========================================================================


class TestDeleteNetBoxInterfacesPost:
    """The delete endpoint against real interfaces: every count is a real row disappearing."""

    @staticmethod
    def _payload(response):
        import json

        return json.loads(response.content)

    def test_permission_denied(self):
        """Without delete_interface nothing is removed and the JSON gate refuses."""
        from dcim.models import Device, Interface

        dev = make_device("del-denied")
        iface = make_interface(dev, "eth0")
        user = make_user_with_perms("del-viewer", [("view", Device), ("view", Interface)])
        req = make_request("post", {"interface_ids": [str(iface.pk)]}, user=user)

        response = _post(_make_dv(req), req, object_type="device", object_id=dev.pk)

        assert response.status_code == 403
        assert self._payload(response)["error"] == "Missing permissions: dcim.delete_interface"
        assert Interface.objects.filter(pk=iface.pk).exists()

    def test_invalid_object_type_raises_http404(self):
        """get_required_permissions_for_object_type rejects the type before any lookup."""
        from django.http import Http404

        req = make_request("post", {"interface_ids": ["1"]})

        with pytest.raises(Http404):
            _post(_make_dv(req), req, object_type="rack", object_id=1)

    def test_no_ids_400(self):
        dev = make_device("del-noids")
        req = make_request("post", {})

        response = _post(_make_dv(req), req, object_type="device", object_id=dev.pk)

        assert response.status_code == 400

    def test_device_successful_delete(self):
        from dcim.models import Interface

        dev = make_device("del-ok")
        iface = make_interface(dev, "eth0")
        req = make_request("post", {"interface_ids": [str(iface.pk)]})

        response = _post(_make_dv(req), req, object_type="device", object_id=dev.pk)

        assert self._payload(response)["deleted_count"] == 1
        assert not Interface.objects.filter(pk=iface.pk).exists()

    def test_device_wrong_device_id_error(self):
        from dcim.models import Interface

        dev = make_device("del-owner")
        other = make_device("del-other")
        stranger = make_interface(other, "eth0")
        req = make_request("post", {"interface_ids": [str(stranger.pk)]})

        response = _post(_make_dv(req), req, object_type="device", object_id=dev.pk)

        data = self._payload(response)
        assert data["deleted_count"] == 0
        assert any("does not belong to this device" in e for e in data["errors"])
        assert Interface.objects.filter(pk=stranger.pk).exists()

    def test_interface_outside_the_grant_is_reported_not_deleted(self):
        """The interface id is client-supplied, so a constrained delete grant must fail closed."""
        from dcim.models import Device, Interface

        dev = make_device("del-scoped")
        keep = make_interface(dev, "eth0")
        make_interface(dev, "eth1")
        user = make_user_with_perms("del-scoped-user", [("view", Device)])
        user = grant(user, "delete", Interface, constraints={"name": "eth1"})
        req = make_request("post", {"interface_ids": [str(keep.pk)]}, user=user)

        response = _post(_make_dv(req), req, object_type="device", object_id=dev.pk)

        data = self._payload(response)
        assert data["deleted_count"] == 0
        assert any(f"Interface with ID {keep.pk} not found" in e for e in data["errors"])
        assert Interface.objects.filter(pk=keep.pk).exists()

    def test_device_vc_interface_not_in_members(self):
        from dcim.models import Interface, VirtualChassis

        vc = VirtualChassis.objects.create(name="vc-del")
        host = make_device("del-vc-host")
        host.virtual_chassis = vc
        host.vc_position = 1
        host.save()
        outsider = make_device("del-vc-outsider")
        stranger = make_interface(outsider, "eth0")
        req = make_request("post", {"interface_ids": [str(stranger.pk)]})

        response = _post(_make_dv(req), req, object_type="device", object_id=host.pk)

        data = self._payload(response)
        assert data["deleted_count"] == 0
        assert any("virtual chassis" in e for e in data["errors"])
        assert Interface.objects.filter(pk=stranger.pk).exists()

    def test_device_vc_interface_in_members_deleted(self):
        """An interface on a sibling of the same chassis is in scope and is removed."""
        from dcim.models import Interface, VirtualChassis

        vc = VirtualChassis.objects.create(name="vc-del-ok")
        host = make_device("del-vcok-host")
        host.virtual_chassis = vc
        host.vc_position = 1
        host.save()
        sibling = make_device("del-vcok-member")
        sibling.virtual_chassis = vc
        sibling.vc_position = 2
        sibling.save()
        iface = make_interface(sibling, "eth0")
        req = make_request("post", {"interface_ids": [str(iface.pk)]})

        response = _post(_make_dv(req), req, object_type="device", object_id=host.pk)

        assert self._payload(response)["deleted_count"] == 1
        assert not Interface.objects.filter(pk=iface.pk).exists()

    def test_vm_successful_delete(self):
        from virtualization.models import VMInterface

        vm = make_vm("del-vm")
        iface = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        req = make_request("post", {"interface_ids": [str(iface.pk)]})

        response = _post(_make_dv(req), req, object_type="virtualmachine", object_id=vm.pk)

        assert self._payload(response)["deleted_count"] == 1
        assert not VMInterface.objects.filter(pk=iface.pk).exists()

    def test_vm_wrong_vm_error(self):
        from virtualization.models import VMInterface

        vm = make_vm("del-vm-owner")
        other = make_vm("del-vm-other")
        stranger = VMInterface.objects.create(virtual_machine=other, name="eth0")
        req = make_request("post", {"interface_ids": [str(stranger.pk)]})

        response = _post(_make_dv(req), req, object_type="virtualmachine", object_id=vm.pk)

        data = self._payload(response)
        assert data["deleted_count"] == 0
        assert any("does not belong to this virtual machine" in e for e in data["errors"])
        assert VMInterface.objects.filter(pk=stranger.pk).exists()

    def test_interface_not_found_adds_error(self):
        from dcim.models import Interface

        dev = make_device("del-missing")
        gone_pk = missing_pk(Interface)
        req = make_request("post", {"interface_ids": [str(gone_pk)]})

        response = _post(_make_dv(req), req, object_type="device", object_id=dev.pk)

        assert any(str(gone_pk) in e for e in self._payload(response)["errors"])

    def test_response_with_errors_includes_error_message(self):
        """A mixed batch deletes what it may and reports the rest — in one transaction."""
        from dcim.models import Interface

        dev = make_device("del-mixed")
        other = make_device("del-mixed-other")
        mine = make_interface(dev, "eth0")
        stranger = make_interface(other, "eth1")
        req = make_request("post", {"interface_ids": [str(mine.pk), str(stranger.pk)]})

        response = _post(_make_dv(req), req, object_type="device", object_id=dev.pk)

        data = self._payload(response)
        assert data["deleted_count"] == 1
        assert "error(s)" in data["message"]
        assert not Interface.objects.filter(pk=mine.pk).exists()
        assert Interface.objects.filter(pk=stranger.pk).exists()


# ===========================================================================
# vlans.py lines 134-139: grouped VLAN update/skip within if row_vlan_group: block
# ===========================================================================


class TestVlansGroupedUpdateAndSkip:
    def _setup(self, tag, cached_name, existing_name):
        """A real grouped VLAN plus a request selecting it; returns (view, request, device, vlan)."""
        from django.core.cache import cache
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        group = VLANGroup.objects.create(name=f"grp-{tag}", slug=f"grp-{tag}")
        vlan = VLAN.objects.create(vid=100, group=group, name=existing_name, status="active")
        dev = make_device(f"vlan-{tag}")
        req = make_request("post", {"select": ["100"], "vlan_group_100": str(group.pk)})
        view = make_view(SyncVLANsView, req)
        view._post_server_key = "default"
        cache_key = view.get_cache_key(dev, "vlans", "default")
        cache.set(
            cache_key,
            [{"vlan_vlan": 100, "vlan_name": cached_name}],
        )
        return view, req, dev, vlan, cache_key

    def test_grouped_vlan_with_different_name_requires_confirmation(self):
        """A grouped VLAN whose LibreNMS name differs is disclosed without a write."""
        from django.core.cache import cache
        from ipam.models import VLAN

        view, req, dev, vlan, cache_key = self._setup("update", cached_name="NewName", existing_name="OldName")

        try:
            response = view._handle_create_vlans(req, dev, "device", dev.pk)

            assert response.status_code == 200
            assert b"Confirm VLAN changes" in response.content
            assert VLAN.objects.get(pk=vlan.pk).name == "OldName"
        finally:
            cache.delete(cache_key)

    def test_grouped_vlan_with_matching_name_is_unchanged(self):
        """A grouped VLAN already carrying the LibreNMS name is left untouched."""
        from django.core.cache import cache
        from ipam.models import VLAN

        view, req, dev, vlan, cache_key = self._setup("skip", cached_name="Same", existing_name="Same")
        last_updated = VLAN.objects.get(pk=vlan.pk).last_updated

        try:
            view._handle_create_vlans(req, dev, "device", dev.pk)

            assert VLAN.objects.get(pk=vlan.pk).last_updated == last_updated
            assert any("unchanged" in t for t in message_texts(req, "success"))
        finally:
            cache.delete(cache_key)
