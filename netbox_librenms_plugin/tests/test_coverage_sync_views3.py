"""
Coverage tests for remaining gaps in views/sync/.
Targets:
- interfaces.py (SyncInterfacesView + DeleteNetBoxInterfacesView) - was 34%
- cables.py lines 147-149 (exception path in process_interface_sync)
- devices.py lines 77, 81-82 (port_association_mode, invalid poller_group)
- locations.py lines 26-28, 32-35, 44-49 (get_table, get_context_data, get_queryset)
- vlans.py lines 134-139 (grouped VLAN update/skip paths)
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_vm
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
    v.object = MagicMock()
    return v


def _make_dv(request=None):
    from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

    return make_view(DeleteNetBoxInterfacesView, request)


@contextmanager
def _pa():
    """Passthrough atomic: real context manager that does not suppress exceptions."""
    yield


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
# SyncInterfacesView.get_selected_interfaces
# ===========================================================================


class TestSyncGetSelectedInterfaces:
    def test_empty_returns_none_and_error(self):
        v = _make_iv()
        req = MagicMock()
        req.POST.getlist.return_value = []
        with patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mm:
            result = v.get_selected_interfaces(req, "ifName")
        assert result is None
        mm.error.assert_called_once()

    def test_with_values_returns_list(self):
        v = _make_iv()
        req = MagicMock()
        req.POST.getlist.return_value = ["eth0", "eth1"]
        assert v.get_selected_interfaces(req, "ifName") == ["eth0", "eth1"]


# ===========================================================================
# SyncInterfacesView.get_cached_ports_data
# ===========================================================================


class TestGetCachedPortsData:
    def test_cache_miss_warns_and_returns_none(self):
        v = _make_iv()
        v.get_cache_key = MagicMock(return_value="k")
        with patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mc:
            mc.get.return_value = None
            with patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mm:
                result = v.get_cached_ports_data(MagicMock(), MagicMock())
        assert result is None
        mm.warning.assert_called_once()

    def test_cache_hit_returns_ports(self):
        v = _make_iv()
        v.get_cache_key = MagicMock(return_value="k")
        ports = [{"ifName": "eth0"}]
        with patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mc:
            mc.get.return_value = {"ports": ports}
            assert v.get_cached_ports_data(MagicMock(), MagicMock()) == ports


# ===========================================================================
# SyncInterfacesView.post
# ===========================================================================


class TestSyncInterfacesPost:
    def _s(self):
        v = _make_iv()
        v.require_all_permissions = MagicMock(return_value=None)
        v.get_vlan_groups_for_device = MagicMock(return_value=[])
        v._build_vlan_lookup_maps = MagicMock(return_value={})
        return v

    def test_permission_denied(self):
        v = self._s()
        err = MagicMock()
        v.require_all_permissions = MagicMock(return_value=err)
        assert v.post(MagicMock(), "device", 1) is err

    def test_no_selected_redirects(self):
        from dcim.models import Device

        v = self._s()
        obj = MagicMock(spec=Device)
        obj.pk = 1
        v.get_object = MagicMock(return_value=obj)
        v.get_selected_interfaces = MagicMock(return_value=None)
        req = MagicMock()
        req.POST.get = lambda k, *a: None
        req.POST.getlist = lambda k: []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"):
            with patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/s/"):
                with patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mr:
                    v.request = req
                    v.post(req, "device", 1)
        mr.assert_called_once()

    def test_no_ports_data_redirects(self):
        from dcim.models import Device

        v = self._s()
        obj = MagicMock(spec=Device)
        obj.pk = 1
        v.get_object = MagicMock(return_value=obj)
        v.get_selected_interfaces = MagicMock(return_value=["eth0"])
        v.get_cached_ports_data = MagicMock(return_value=None)
        req = MagicMock()
        req.POST.get = lambda k, *a: None
        req.POST.getlist = lambda k: []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"):
            with patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/s/"):
                with patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mr:
                    v.request = req
                    v.post(req, "device", 1)
        mr.assert_called_once()

    def test_full_success_device(self):
        from dcim.models import Device

        v = self._s()
        obj = MagicMock(spec=Device)
        obj.pk = 1
        v.get_object = MagicMock(return_value=obj)
        v.get_selected_interfaces = MagicMock(return_value=["eth0"])
        v.get_cached_ports_data = MagicMock(return_value=[{"ifName": "eth0"}])
        v.sync_selected_interfaces = MagicMock()
        req = MagicMock()
        req.POST.get = lambda k, *a: "default" if k == "server_key" else None
        req.POST.getlist = lambda k: []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"):
            with patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/s/"):
                with patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mr:
                    with patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mm:
                        v.request = req
                        v.post(req, "device", 1)
        v.sync_selected_interfaces.assert_called_once()
        mm.success.assert_called_once()
        mr.assert_called_once()

    def test_full_success_vm(self):
        from virtualization.models import VirtualMachine

        v = self._s()
        obj = MagicMock(spec=VirtualMachine)
        obj.pk = 2
        v.get_object = MagicMock(return_value=obj)
        v.get_selected_interfaces = MagicMock(return_value=["eth0"])
        v.get_cached_ports_data = MagicMock(return_value=[{"ifName": "eth0"}])
        v.sync_selected_interfaces = MagicMock()
        req = MagicMock()
        req.POST.get = lambda k, *a: None
        req.POST.getlist = lambda k: []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"):
            with patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/s/"):
                with patch("netbox_librenms_plugin.views.sync.interfaces.redirect"):
                    with patch("netbox_librenms_plugin.views.sync.interfaces.messages"):
                        v.request = req
                        v.post(req, "virtualmachine", 2)
        v.sync_selected_interfaces.assert_called_once()


# ===========================================================================
# SyncInterfacesView.sync_selected_interfaces
# ===========================================================================


class TestSyncSelectedInterfaces:
    def test_only_selected_processed(self):
        from dcim.models import Device

        v = _make_iv()
        v.sync_interface = MagicMock()
        obj = MagicMock(spec=Device)
        ports = [{"ifName": "eth0"}, {"ifName": "eth1"}]
        with patch("netbox_librenms_plugin.views.sync.interfaces.transaction"):
            v.sync_selected_interfaces(obj, ["eth0"], ports, [], "ifName")
        assert v.sync_interface.call_count == 1
        assert v.sync_interface.call_args[0][1]["ifName"] == "eth0"


# ===========================================================================
# SyncInterfacesView.sync_interface
# ===========================================================================


class TestSyncInterface:
    """Which device the LibreNMS row is written to, resolved against real rows.

    ``update_interface_attributes`` and ``_sync_interface_vlans`` stay stubbed: they are the
    view's own next steps, and these tests are about target selection, not field copying.
    """

    def _v(self, request=None):
        v = _make_iv(request)
        v.update_interface_attributes = MagicMock()
        v._sync_interface_vlans = MagicMock()
        v._lookup_maps = {}
        v._skipped_conflicts = []
        return v

    @staticmethod
    def _vc(tag, count=2):
        """A real VirtualChassis with *count* members at consecutive positions."""
        from dcim.models import VirtualChassis

        vc = VirtualChassis.objects.create(name=f"vc-{tag}")
        members = []
        for position in range(1, count + 1):
            member = make_device(f"{tag}-m{position}")
            member.virtual_chassis = vc
            member.vc_position = position
            member.save()
            members.append(member)
        return vc, members

    def test_device_no_vc_uses_obj(self):
        from dcim.models import Interface

        dev = make_device("sync-novc")
        v = self._v()

        v.sync_interface(dev, {"ifName": "eth0"}, [], "ifName")

        assert Interface.objects.filter(device=dev, name="eth0").exists()
        v.update_interface_attributes.assert_called_once()

    def test_device_vc_target_in_valid_ids(self):
        """A posted sibling of the same chassis is honoured: the interface lands on the sibling."""
        from dcim.models import Interface

        _vc, (host, sibling) = self._vc("sync-vc-ok")
        req = make_request("post", {"device_selection_eth0": str(sibling.pk)})
        v = self._v(req)

        v.sync_interface(host, {"ifName": "eth0"}, [], "ifName")

        assert Interface.objects.filter(device=sibling, name="eth0").exists()
        assert not Interface.objects.filter(device=host, name="eth0").exists()

    def test_device_vc_target_not_in_valid_ids_is_skipped(self):
        """A device outside the chassis is refused without writing to the page device."""
        from dcim.models import Interface

        _vc, (host, _sibling) = self._vc("sync-vc-outsider")
        outsider = make_device("sync-vc-outsider-x")
        req = make_request("post", {"device_selection_eth0": str(outsider.pk)})
        v = self._v(req)

        v.sync_interface(host, {"ifName": "eth0"}, [], "ifName")

        assert not Interface.objects.filter(device=host, name="eth0").exists()
        assert not Interface.objects.filter(device=outsider, name="eth0").exists()
        assert v._skipped_conflicts == ["eth0"]

    def test_device_no_vc_wrong_selection_is_skipped(self):
        from dcim.models import Interface

        dev = make_device("sync-novc-self")
        other = make_device("sync-novc-other")
        req = make_request("post", {"device_selection_eth0": str(other.pk)})
        v = self._v(req)

        v.sync_interface(dev, {"ifName": "eth0"}, [], "ifName")

        assert not Interface.objects.filter(device=dev, name="eth0").exists()
        assert not Interface.objects.filter(device=other, name="eth0").exists()
        assert v._skipped_conflicts == ["eth0"]

    def test_device_selection_does_not_exist_is_skipped(self):
        from dcim.models import Device, Interface

        dev = make_device("sync-gone")
        absent_pk = missing_pk(Device)
        req = make_request("post", {"device_selection_eth0": str(absent_pk)})
        v = self._v(req)

        v.sync_interface(dev, {"ifName": "eth0"}, [], "ifName")

        assert not Interface.objects.filter(device=dev, name="eth0").exists()
        assert v._skipped_conflicts == ["eth0"]

    def test_device_selection_outside_the_grant_is_skipped(self):
        """The posted id is client-supplied, so a constrained grant must not reach the sibling."""
        from dcim.models import Device, Interface

        _vc, (host, sibling) = self._vc("sync-vc-scoped")
        user = make_user_with_perms("sync-scoped", [("view", Device)], constraints={"name": "sync-vc-scoped-m1"})
        req = make_request("post", {"device_selection_eth0": str(sibling.pk)}, user=user)
        v = self._v(req)

        v.sync_interface(host, {"ifName": "eth0"}, [], "ifName")

        assert not Interface.objects.filter(device=host, name="eth0").exists()
        assert not Interface.objects.filter(device=sibling, name="eth0").exists()
        assert v._skipped_conflicts == ["eth0"]

    def test_vm_uses_vminterface(self):
        from virtualization.models import VMInterface

        vm = make_vm("sync-vm")
        v = self._v()

        v.sync_interface(vm, {"ifName": "eth0"}, [], "ifName")

        assert VMInterface.objects.filter(virtual_machine=vm, name="eth0").exists()
        v.update_interface_attributes.assert_called_once()

    def test_vlans_excluded_skips_sync(self):
        dev = make_device("sync-novlan")
        v = self._v()

        v.sync_interface(dev, {"ifName": "eth0"}, ["vlans"], "ifName")

        v._sync_interface_vlans.assert_not_called()

    def test_vlans_not_excluded_calls_sync(self):
        dev = make_device("sync-vlan")
        v = self._v()

        v.sync_interface(dev, {"ifName": "eth0"}, [], "ifName")

        v._sync_interface_vlans.assert_called_once()


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

    def test_no_mapping_returns_other(self):
        self._mapping("ethernetCsmacd", "virtual")  # a mapping exists, but not for this type

        result = _make_iv().get_netbox_interface_type({"ifType": "unknown", "ifSpeed": None})

        assert result == "other"


# ===========================================================================
# SyncInterfacesView._sync_interface_vlans
# ===========================================================================


class TestSyncInterfaceVlans:
    def test_builds_vlan_group_map_for_untagged_and_tagged(self):
        v = _make_iv()
        v._lookup_maps = {}
        v._update_interface_vlan_assignment = MagicMock()
        iface = MagicMock()
        port = {"untagged_vlan": 100, "tagged_vlans": [200]}

        def pg(key, default=""):
            return {"vlan_group_eth0_100": "5", "vlan_group_eth0_200": "5"}.get(key, default)

        v.request.POST.get = pg
        v._sync_interface_vlans(iface, port, "eth0")
        args = v._update_interface_vlan_assignment.call_args[0]
        assert args[2].get("100") == "5"
        assert args[2].get("200") == "5"

    def test_no_vlans_empty_map(self):
        v = _make_iv()
        v._lookup_maps = {}
        v._update_interface_vlan_assignment = MagicMock()
        v.request.POST.get = lambda k, *a: ""
        v._sync_interface_vlans(MagicMock(), {"untagged_vlan": None, "tagged_vlans": []}, "eth0")
        assert v._update_interface_vlan_assignment.call_args[0][2] == {}

    def test_special_chars_in_name(self):
        v = _make_iv()
        v._lookup_maps = {}
        v._update_interface_vlan_assignment = MagicMock()
        v.request.POST.get = lambda k, *a: ""
        v._sync_interface_vlans(MagicMock(), {"untagged_vlan": None, "tagged_vlans": []}, "eth0/1:2")
        v._update_interface_vlan_assignment.assert_called_once()


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
# cables.py lines 147-149: exception path in process_interface_sync
# ===========================================================================


class TestCablesExceptionPath:
    def test_exception_hits_147_to_149(self):
        """Lines 147-149: logger.exception + invalid.append when _passthrough_atomic used."""
        from netbox_librenms_plugin.views.sync.cables import SyncCablesView

        v = object.__new__(SyncCablesView)
        v._librenms_api = MagicMock()
        v.request = MagicMock()

        def raise_err(iface, links):
            raise RuntimeError("deliberate for coverage")

        v.process_single_interface = raise_err

        with patch("netbox_librenms_plugin.views.sync.cables.transaction") as mt:
            mt.atomic = _pa
            results = v.process_interface_sync([{"local_port_id": "eth_x"}], [])

        assert "eth_x" in results["invalid"]


# ===========================================================================
# devices.py lines 77, 81-82: port_association_mode + invalid poller_group
# ===========================================================================


class TestDevicesFormValidEdgeCases:
    def _v(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        v = object.__new__(AddDeviceToLibreNMSView)
        v._librenms_api = MagicMock()
        v._librenms_api.add_device.return_value = (True, "Added")
        v._librenms_api.server_key = "default"
        v.request = MagicMock()
        v.object = MagicMock()
        v.object.get_absolute_url.return_value = "/d/"
        return v

    def test_port_association_mode_line_77(self):
        """Line 77: device_data[port_association_mode] set when truthy."""
        v = self._v()
        f = MagicMock()
        f.cleaned_data = {"hostname": "h", "force_add": False, "port_association_mode": 2, "community": "pub"}
        with patch("netbox_librenms_plugin.views.sync.devices.messages"):
            with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                v.form_valid(f, snmp_version="v2c")
        dd = v._librenms_api.add_device.call_args[0][0]
        assert dd["port_association_mode"] == 2

    def test_invalid_poller_group_lines_81_82(self):
        """Lines 81-82: except (ValueError, TypeError) silently catches invalid int."""
        v = self._v()
        f = MagicMock()
        f.cleaned_data = {"hostname": "h", "force_add": False, "poller_group": "bad-int", "community": "pub"}
        with patch("netbox_librenms_plugin.views.sync.devices.messages"):
            with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                v.form_valid(f, snmp_version="v2c")
        dd = v._librenms_api.add_device.call_args[0][0]
        assert "poller_group" not in dd


# ===========================================================================
# locations.py lines 26-28, 32-35, 44-49
# ===========================================================================


class TestSyncSiteLocationViewGetTable:
    def test_get_table_configures_table(self):
        """Lines 26-28: get_table calls super().get_table then table.configure(request)."""
        import django_tables2
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        v = object.__new__(SyncSiteLocationView)
        v.request = MagicMock()
        mt = MagicMock()
        with patch.object(django_tables2.SingleTableView, "get_table", return_value=mt):
            result = v.get_table()
        mt.configure.assert_called_once_with(v.request)
        assert result is mt


class TestSyncSiteLocationViewGetContextData:
    def test_adds_filter_form(self):
        """Lines 32-35: adds filter_form to context."""
        import django_tables2
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        v = object.__new__(SyncSiteLocationView)
        v.request = MagicMock()
        v.request.GET = {}
        mf = MagicMock()
        mf.return_value.form = MagicMock()
        v.filterset = mf
        with patch.object(django_tables2.SingleTableView, "get_context_data", return_value={}):
            with patch.object(type(v), "get_queryset", return_value=[]):
                ctx = v.get_context_data()
        assert "filter_form" in ctx


class TestSyncSiteLocationViewGetQuerysetSuccess:
    def _view(self, get_params, locations):
        """The real view over the real Site table, with only the LibreNMS locations call stubbed."""
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        v = make_view(SyncSiteLocationView, make_request("get", get_params))
        v._librenms_api.get_locations.return_value = (True, locations)
        return v

    def test_returns_sync_data(self):
        """One SyncData row per real Site, paired with its LibreNMS location."""
        from dcim.models import Site

        Site.objects.create(name="Loc Site A", slug="loc-site-a")
        Site.objects.create(name="Loc Site B", slug="loc-site-b")
        v = self._view({}, [{"location": "Loc Site A"}])

        result = v.get_queryset()

        assert {row.netbox_site.name for row in result} == set(Site.objects.values_list("name", flat=True))
        matched = next(row for row in result if row.netbox_site.name == "Loc Site A")
        assert matched.librenms_location == {"location": "Loc Site A"}

    def test_filterset_branch(self):
        """A GET query narrows the rows through the real SiteLocationFilterSet."""
        from dcim.models import Site

        Site.objects.create(name="Filter Hit", slug="filter-hit")
        Site.objects.create(name="Filter Miss", slug="filter-miss")
        v = self._view({"q": "Hit"}, [{"location": "Filter Hit"}])

        result = v.get_queryset()

        assert [row.netbox_site.name for row in result] == ["Filter Hit"]


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
        cache.set(
            view.get_cache_key(dev, "vlans", "default"),
            [{"vlan_vlan": 100, "vlan_name": cached_name}],
        )
        return view, req, dev, vlan

    def test_grouped_update_path_lines_134_to_137(self):
        """A grouped VLAN whose LibreNMS name differs is renamed and persisted."""
        from ipam.models import VLAN

        view, req, dev, vlan = self._setup("update", cached_name="NewName", existing_name="OldName")

        view._handle_create_vlans(req, dev, "device", dev.pk)

        assert VLAN.objects.get(pk=vlan.pk).name == "NewName"
        assert any("updated" in t for t in message_texts(req, "success"))

    def test_grouped_skip_path_lines_138_to_139(self):
        """A grouped VLAN already carrying the LibreNMS name is left untouched."""
        from ipam.models import VLAN

        view, req, dev, vlan = self._setup("skip", cached_name="Same", existing_name="Same")
        last_updated = VLAN.objects.get(pk=vlan.pk).last_updated

        view._handle_create_vlans(req, dev, "device", dev.pk)

        assert VLAN.objects.get(pk=vlan.pk).last_updated == last_updated
        assert any("unchanged" in t for t in message_texts(req, "success"))
