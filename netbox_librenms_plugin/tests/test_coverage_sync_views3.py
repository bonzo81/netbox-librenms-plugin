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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_iv():
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    v = object.__new__(SyncInterfacesView)
    v._librenms_api = MagicMock()
    v._librenms_api.server_key = "default"
    v._post_server_key = "default"
    v.request = MagicMock()
    v.request.POST.get = lambda k, *a: None
    v.object = MagicMock()
    return v


def _make_dv():
    from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

    v = object.__new__(DeleteNetBoxInterfacesView)
    v._librenms_api = MagicMock()
    v.request = MagicMock()
    return v


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
        import pytest
        from django.http import Http404

        v = _make_iv()
        with pytest.raises(Http404):
            v.get_required_permissions_for_object_type("rack")


# ===========================================================================
# SyncInterfacesView.get_object
# ===========================================================================


class TestSyncInterfacesGetObject:
    def test_device_type(self):
        v = _make_iv()
        mock_obj = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_obj):
            assert v.get_object("device", 1) is mock_obj

    def test_vm_type(self):
        v = _make_iv()
        mock_obj = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_obj):
            assert v.get_object("virtualmachine", 2) is mock_obj

    def test_invalid_raises_http404(self):
        import pytest
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
    def _v(self):
        v = _make_iv()
        v.update_interface_attributes = MagicMock()
        v._sync_interface_vlans = MagicMock()
        v.get_netbox_interface_type = MagicMock(return_value="1000base-t")
        v._lookup_maps = {}
        return v

    def test_device_no_vc_uses_obj(self):
        from dcim.models import Device

        v = self._v()
        obj = MagicMock(spec=Device)
        obj.id = 1
        obj.virtual_chassis = None
        iface = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
            mc.objects.get_or_create.return_value = (iface, True)
            v.sync_interface(obj, {"ifName": "eth0"}, [], "ifName")
        mc.objects.get_or_create.assert_called_once_with(device=obj, name="eth0")
        v.update_interface_attributes.assert_called_once()

    def test_device_vc_target_in_valid_ids(self):
        from dcim.models import Device

        v = self._v()
        obj = MagicMock(spec=Device)
        obj.id = 1
        vc = MagicMock()
        vc.members.values_list.return_value = [1, 2, 3]
        obj.virtual_chassis = vc
        target = MagicMock()
        target.id = 2
        v.request.POST.get = lambda k, *a: "2" if k == "device_selection_eth0" else None
        iface = MagicMock()
        # Patch only Device.objects.get, not Device itself (isinstance must work)
        with patch("netbox_librenms_plugin.views.sync.interfaces.Device.objects.get", return_value=target):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.objects.get_or_create.return_value = (iface, True)
                v.sync_interface(obj, {"ifName": "eth0"}, [], "ifName")
        mc.objects.get_or_create.assert_called_once_with(device=target, name="eth0")

    def test_device_vc_target_not_in_valid_ids_falls_back(self):
        from dcim.models import Device

        v = self._v()
        obj = MagicMock(spec=Device)
        obj.id = 1
        vc = MagicMock()
        vc.members.values_list.return_value = [1, 2, 3]
        obj.virtual_chassis = vc
        target = MagicMock()
        target.id = 99
        v.request.POST.get = lambda k, *a: "99" if k == "device_selection_eth0" else None
        iface = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.Device.objects.get", return_value=target):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.objects.get_or_create.return_value = (iface, True)
                v.sync_interface(obj, {"ifName": "eth0"}, [], "ifName")
        mc.objects.get_or_create.assert_called_once_with(device=obj, name="eth0")

    def test_device_no_vc_wrong_selection_falls_back(self):
        from dcim.models import Device

        v = self._v()
        obj = MagicMock(spec=Device)
        obj.id = 1
        obj.virtual_chassis = None
        target = MagicMock()
        target.id = 99
        v.request.POST.get = lambda k, *a: "99" if k == "device_selection_eth0" else None
        iface = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.Device.objects.get", return_value=target):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.objects.get_or_create.return_value = (iface, True)
                v.sync_interface(obj, {"ifName": "eth0"}, [], "ifName")
        mc.objects.get_or_create.assert_called_once_with(device=obj, name="eth0")

    def test_device_selection_does_not_exist_falls_back(self):
        from dcim.models import Device

        v = self._v()
        obj = MagicMock(spec=Device)
        obj.id = 1
        obj.virtual_chassis = None
        v.request.POST.get = lambda k, *a: "999" if k == "device_selection_eth0" else None
        iface = MagicMock()
        with patch(
            "netbox_librenms_plugin.views.sync.interfaces.Device.objects.get",
            side_effect=Device.DoesNotExist,
        ):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.objects.get_or_create.return_value = (iface, True)
                v.sync_interface(obj, {"ifName": "eth0"}, [], "ifName")
        mc.objects.get_or_create.assert_called_once_with(device=obj, name="eth0")

    def test_vm_uses_vminterface(self):
        from virtualization.models import VirtualMachine

        v = self._v()
        obj = MagicMock(spec=VirtualMachine)
        iface = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface") as mc:
            mc.objects.get_or_create.return_value = (iface, True)
            v.sync_interface(obj, {"ifName": "eth0"}, [], "ifName")
        mc.objects.get_or_create.assert_called_once_with(virtual_machine=obj, name="eth0")
        v.update_interface_attributes.assert_called_once()

    def test_vlans_excluded_skips_sync(self):
        from dcim.models import Device

        v = self._v()
        obj = MagicMock(spec=Device)
        obj.id = 1
        obj.virtual_chassis = None
        iface = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
            mc.objects.get_or_create.return_value = (iface, True)
            v.sync_interface(obj, {"ifName": "eth0"}, ["vlans"], "ifName")
        v._sync_interface_vlans.assert_not_called()

    def test_vlans_not_excluded_calls_sync(self):
        from dcim.models import Device

        v = self._v()
        obj = MagicMock(spec=Device)
        obj.id = 1
        obj.virtual_chassis = None
        iface = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
            mc.objects.get_or_create.return_value = (iface, True)
            v.sync_interface(obj, {"ifName": "eth0"}, [], "ifName")
        v._sync_interface_vlans.assert_called_once()


# ===========================================================================
# SyncInterfacesView.get_netbox_interface_type
# ===========================================================================


class TestGetNetboxInterfaceType:
    def test_speed_mapping_found(self):
        v = _make_iv()
        mm = MagicMock()
        mm.netbox_type = "1000base-t"
        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=1000000):
            with patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mc:
                qs = MagicMock()
                mc.objects.filter.return_value = qs
                qs.filter.return_value.order_by.return_value.first.return_value = mm
                result = v.get_netbox_interface_type({"ifType": "ethernetCsmacd", "ifSpeed": 1000000000})
        assert result == "1000base-t"

    def test_speed_not_found_falls_back_to_null(self):
        v = _make_iv()
        null_m = MagicMock()
        null_m.netbox_type = "null-type"
        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=1000000):
            with patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mc:
                qs = MagicMock()
                mc.objects.filter.return_value = qs
                qs.filter.return_value.order_by.return_value.first.return_value = None
                qs.filter.return_value.first.return_value = null_m
                result = v.get_netbox_interface_type({"ifType": "ethernetCsmacd", "ifSpeed": 1000000000})
        assert result == "null-type"

    def test_no_speed_uses_null_mapping(self):
        v = _make_iv()
        m = MagicMock()
        m.netbox_type = "virtual"
        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mc:
                qs = MagicMock()
                mc.objects.filter.return_value = qs
                qs.filter.return_value.first.return_value = m
                result = v.get_netbox_interface_type({"ifType": "eth", "ifSpeed": None})
        assert result == "virtual"

    def test_no_mapping_returns_other(self):
        v = _make_iv()
        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            with patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mc:
                qs = MagicMock()
                mc.objects.filter.return_value = qs
                qs.filter.return_value.first.return_value = None
                result = v.get_netbox_interface_type({"ifType": "unknown", "ifSpeed": None})
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
        import pytest
        from django.http import Http404

        v = _make_dv()
        with pytest.raises(Http404):
            v.get_required_permissions_for_object_type("invalid")


# ===========================================================================
# DeleteNetBoxInterfacesView.post
# ===========================================================================


class TestDeleteNetBoxInterfacesPost:
    def test_permission_denied(self):
        v = _make_dv()
        err = MagicMock()
        v.require_all_permissions_json = MagicMock(return_value=err)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        req = MagicMock()
        req.POST.getlist.return_value = ["1"]
        assert v.post(req, "device", 1) is err

    def test_invalid_object_type_400(self):
        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        req = MagicMock()
        req.POST.getlist.return_value = ["1"]
        resp = v.post(req, "rack", 1)
        assert resp.status_code == 400

    def test_no_ids_400(self):
        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        req = MagicMock()
        req.POST.getlist.return_value = []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404"):
            resp = v.post(req, "device", 1)
        assert resp.status_code == 400

    def test_device_successful_delete(self):
        import json

        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        obj = MagicMock()
        obj.id = 1
        obj.virtual_chassis = None
        iface = MagicMock()
        iface.name = "eth0"
        iface.device_id = 1
        req = MagicMock()
        req.POST.getlist.side_effect = lambda k: ["10"] if k == "interface_ids" else []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=obj):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.objects.get.return_value = iface
                with patch("netbox_librenms_plugin.views.sync.interfaces.transaction") as mt:
                    mt.atomic = _pa
                    resp = v.post(req, "device", 1)
        data = json.loads(resp.content)
        assert data["deleted_count"] == 1
        iface.delete.assert_called_once()

    def test_device_wrong_device_id_error(self):
        import json

        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        obj = MagicMock()
        obj.id = 1
        obj.virtual_chassis = None
        iface = MagicMock()
        iface.name = "eth0"
        iface.device_id = 99
        req = MagicMock()
        req.POST.getlist.side_effect = lambda k: ["10"] if k == "interface_ids" else []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=obj):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.objects.get.return_value = iface
                with patch("netbox_librenms_plugin.views.sync.interfaces.transaction") as mt:
                    mt.atomic = _pa
                    resp = v.post(req, "device", 1)
        data = json.loads(resp.content)
        assert data["deleted_count"] == 0
        assert len(data["errors"]) > 0

    def test_device_vc_interface_not_in_members(self):
        import json

        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        obj = MagicMock()
        obj.id = 1
        vc = MagicMock()
        m1 = MagicMock()
        m1.id = 1
        m2 = MagicMock()
        m2.id = 2
        vc.members.all.return_value = [m1, m2]
        obj.virtual_chassis = vc
        iface = MagicMock()
        iface.name = "eth0"
        iface.device_id = 99
        req = MagicMock()
        req.POST.getlist.side_effect = lambda k: ["10"] if k == "interface_ids" else []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=obj):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.objects.get.return_value = iface
                with patch("netbox_librenms_plugin.views.sync.interfaces.transaction") as mt:
                    mt.atomic = _pa
                    resp = v.post(req, "device", 1)
        data = json.loads(resp.content)
        assert data["deleted_count"] == 0
        assert len(data["errors"]) > 0

    def test_device_vc_interface_in_members_deleted(self):
        import json

        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        obj = MagicMock()
        obj.id = 1
        vc = MagicMock()
        m1 = MagicMock()
        m1.id = 1
        m2 = MagicMock()
        m2.id = 2
        vc.members.all.return_value = [m1, m2]
        obj.virtual_chassis = vc
        iface = MagicMock()
        iface.name = "eth0"
        iface.device_id = 2
        req = MagicMock()
        req.POST.getlist.side_effect = lambda k: ["10"] if k == "interface_ids" else []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=obj):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.objects.get.return_value = iface
                with patch("netbox_librenms_plugin.views.sync.interfaces.transaction") as mt:
                    mt.atomic = _pa
                    resp = v.post(req, "device", 1)
        data = json.loads(resp.content)
        assert data["deleted_count"] == 1

    def test_vm_successful_delete(self):
        import json

        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        obj = MagicMock()
        obj.id = 5
        iface = MagicMock()
        iface.name = "eth0"
        iface.virtual_machine_id = 5
        req = MagicMock()
        req.POST.getlist.side_effect = lambda k: ["20"] if k == "interface_ids" else []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=obj):
            with patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface") as mc:
                mc.objects.get.return_value = iface
                with patch("netbox_librenms_plugin.views.sync.interfaces.transaction") as mt:
                    mt.atomic = _pa
                    resp = v.post(req, "virtualmachine", 5)
        data = json.loads(resp.content)
        assert data["deleted_count"] == 1
        iface.delete.assert_called_once()

    def test_vm_wrong_vm_error(self):
        import json

        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        obj = MagicMock()
        obj.id = 5
        iface = MagicMock()
        iface.name = "eth0"
        iface.virtual_machine_id = 99
        req = MagicMock()
        req.POST.getlist.side_effect = lambda k: ["20"] if k == "interface_ids" else []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=obj):
            with patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface") as mc:
                mc.objects.get.return_value = iface
                with patch("netbox_librenms_plugin.views.sync.interfaces.transaction") as mt:
                    mt.atomic = _pa
                    resp = v.post(req, "virtualmachine", 5)
        data = json.loads(resp.content)
        assert data["deleted_count"] == 0
        assert len(data["errors"]) > 0

    def test_interface_not_found_adds_error(self):
        import json
        from dcim.models import Interface
        from virtualization.models import VMInterface as VMI

        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        obj = MagicMock()
        obj.id = 1
        obj.virtual_chassis = None
        req = MagicMock()
        req.POST.getlist.side_effect = lambda k: ["999"] if k == "interface_ids" else []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=obj):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.DoesNotExist = Interface.DoesNotExist
                mc.objects.get.side_effect = Interface.DoesNotExist
                with patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface") as mvc:
                    mvc.DoesNotExist = VMI.DoesNotExist
                    with patch("netbox_librenms_plugin.views.sync.interfaces.transaction") as mt:
                        mt.atomic = _pa
                        resp = v.post(req, "device", 1)
        data = json.loads(resp.content)
        assert any("999" in e for e in data.get("errors", []))

    def test_response_with_errors_includes_error_message(self):
        import json

        v = _make_dv()
        v.require_all_permissions_json = MagicMock(return_value=None)
        v.get_required_permissions_for_object_type = MagicMock(return_value=[])
        obj = MagicMock()
        obj.id = 1
        obj.virtual_chassis = None
        iface_ok = MagicMock()
        iface_ok.name = "eth0"
        iface_ok.device_id = 1
        iface_bad = MagicMock()
        iface_bad.name = "eth1"
        iface_bad.device_id = 99
        n = [0]

        def get_se(**kw):
            n[0] += 1
            return iface_ok if n[0] == 1 else iface_bad

        req = MagicMock()
        req.POST.getlist.side_effect = lambda k: ["10", "20"] if k == "interface_ids" else []
        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=obj):
            with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mc:
                mc.objects.get.side_effect = get_se
                with patch("netbox_librenms_plugin.views.sync.interfaces.transaction") as mt:
                    mt.atomic = _pa
                    resp = v.post(req, "device", 1)
        data = json.loads(resp.content)
        assert data["deleted_count"] == 1
        assert "error(s)" in data["message"]


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
    def test_returns_sync_data(self):
        """Lines 44, 49: build sync_data list and return it."""
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        v = object.__new__(SyncSiteLocationView)
        v.request = MagicMock()
        v.request.GET = {}
        v.filterset = None
        sd = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.locations.Site") as ms:
            ms.objects.all.return_value = [MagicMock()]
            with patch.object(v, "get_librenms_locations", return_value=(True, [{"location": "T"}])):
                with patch.object(v, "create_sync_data", return_value=sd):
                    result = v.get_queryset()
        assert result == [sd]

    def test_filterset_branch(self):
        """Lines 46-47: filterset branch when request.GET is truthy."""
        from netbox_librenms_plugin.views.sync.locations import SyncSiteLocationView

        v = object.__new__(SyncSiteLocationView)
        v.request = MagicMock()
        v.request.GET = {"name": "x"}
        mf = MagicMock()
        filtered = [MagicMock()]
        mf.return_value.qs = filtered
        v.filterset = mf
        with patch("netbox_librenms_plugin.views.sync.locations.Site") as ms:
            ms.objects.all.return_value = [MagicMock()]
            with patch.object(v, "get_librenms_locations", return_value=(True, [{"location": "T"}])):
                with patch.object(v, "create_sync_data", return_value=MagicMock()):
                    result = v.get_queryset()
        assert result is filtered


# ===========================================================================
# vlans.py lines 134-139: grouped VLAN update/skip within if row_vlan_group: block
# ===========================================================================


class TestVlansGroupedUpdateAndSkip:
    def _v(self):
        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        v = object.__new__(SyncVLANsView)
        v._librenms_api = MagicMock()
        v._librenms_api.server_key = "default"
        v._post_server_key = "default"
        v.get_cache_key = MagicMock(return_value="k")
        v._redirect = MagicMock(return_value=MagicMock())
        req = MagicMock()
        req.POST.getlist = lambda k: ["100"] if k == "select" else []
        req.POST.get = lambda key, default="": "3" if key == "vlan_group_100" else default
        v.request = req
        return v

    def test_grouped_update_path_lines_134_to_137(self):
        """elif vlan.name != librenms_name: update triggered."""
        from ipam.models import VLANGroup

        v = self._v()
        mg = MagicMock()
        mv = MagicMock()
        mv.name = "OldName"
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mc:
            mc.get.return_value = [{"vlan_vlan": 100, "vlan_name": "NewName"}]
            with patch("netbox_librenms_plugin.views.sync.vlans.VLANGroup") as mvg:
                mvg.DoesNotExist = VLANGroup.DoesNotExist
                mvg.objects.get.return_value = mg
                with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mvl:
                    mvl.objects.get_or_create.return_value = (mv, False)
                    with patch("netbox_librenms_plugin.views.sync.vlans.transaction"):
                        with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mm:
                            v._handle_create_vlans(v.request, MagicMock(), "device", 1)
        mv.save.assert_called_once()
        assert mv.name == "NewName"
        assert "updated" in str(mm.success.call_args)

    def test_grouped_skip_path_lines_138_to_139(self):
        """else: skipped_count when name unchanged."""
        from ipam.models import VLANGroup

        v = self._v()
        mg = MagicMock()
        mv = MagicMock()
        mv.name = "Same"
        with patch("netbox_librenms_plugin.views.sync.vlans.cache") as mc:
            mc.get.return_value = [{"vlan_vlan": 100, "vlan_name": "Same"}]
            with patch("netbox_librenms_plugin.views.sync.vlans.VLANGroup") as mvg:
                mvg.DoesNotExist = VLANGroup.DoesNotExist
                mvg.objects.get.return_value = mg
                with patch("netbox_librenms_plugin.views.sync.vlans.VLAN") as mvl:
                    mvl.objects.get_or_create.return_value = (mv, False)
                    with patch("netbox_librenms_plugin.views.sync.vlans.transaction"):
                        with patch("netbox_librenms_plugin.views.sync.vlans.messages") as mm:
                            v._handle_create_vlans(v.request, MagicMock(), "device", 1)
        mv.save.assert_not_called()
        assert "unchanged" in str(mm.success.call_args)
