"""
Coverage tests for views/sync/interfaces.py

SyncInterfacesView + DeleteNetBoxInterfacesView
Target: 95%+ coverage
"""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(post_data=None, get_data=None):
    """Build a mock request with proper POST / GET dicts."""
    req = MagicMock()
    _post = post_data or {}
    post_mock = MagicMock()
    post_mock.get = lambda k, d=None: _post.get(k, d)
    post_mock.getlist = lambda k: _post[k] if isinstance(_post.get(k), list) else ([] if k not in _post else [_post[k]])
    req.POST = post_mock
    req.GET = get_data or {}
    return req


def _denied_response():
    resp = MagicMock()
    resp.status_code = 403
    return resp


# ===========================================================================
# SyncInterfacesView.get_required_permissions_for_object_type
# ===========================================================================


class TestSyncInterfacesViewPermissions:
    def test_device_type_returns_interface_perms(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        from dcim.models import Interface

        view = object.__new__(SyncInterfacesView)
        perms = view.get_required_permissions_for_object_type("device")
        assert ("change", Interface) in perms

    def test_vm_type_returns_vminterface_perms(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        from virtualization.models import VMInterface

        view = object.__new__(SyncInterfacesView)
        perms = view.get_required_permissions_for_object_type("virtualmachine")
        assert ("change", VMInterface) in perms

    def test_invalid_type_raises_http404(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        from django.http import Http404
        import pytest

        view = object.__new__(SyncInterfacesView)
        with pytest.raises(Http404):
            view.get_required_permissions_for_object_type("invalid")


# ===========================================================================
# SyncInterfacesView.get_object
# ===========================================================================


class TestSyncInterfacesViewGetObject:
    def test_get_device(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        mock_device = MagicMock()

        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device):
            result = view.get_object("device", 1)
        assert result is mock_device

    def test_get_vm(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        mock_vm = MagicMock()

        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_vm):
            result = view.get_object("virtualmachine", 2)
        assert result is mock_vm

    def test_invalid_type_raises_http404(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        from django.http import Http404
        import pytest

        view = object.__new__(SyncInterfacesView)
        with pytest.raises(Http404):
            view.get_object("invalid", 1)


# ===========================================================================
# SyncInterfacesView.get_selected_interfaces
# ===========================================================================


class TestSyncInterfacesViewGetSelectedInterfaces:
    def test_empty_selection_returns_none(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        req = _make_request(post_data={})
        with patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs:
            result = view.get_selected_interfaces(req, "ifName")
        assert result is None
        mock_msgs.error.assert_called_once()

    def test_with_selection_returns_list(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        req = _make_request(post_data={"select": ["Gi0/1", "Gi0/2"]})
        result = view.get_selected_interfaces(req, "ifName")
        assert result == ["Gi0/1", "Gi0/2"]


# ===========================================================================
# SyncInterfacesView.get_cached_ports_data
# ===========================================================================


class TestSyncInterfacesViewGetCachedPortsData:
    def test_cache_miss_warns_and_returns_none(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.get_cache_key = MagicMock(return_value="k")
        req = _make_request()
        mock_obj = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
        ):
            mock_cache.get.return_value = None
            result = view.get_cached_ports_data(req, mock_obj, "default")

        assert result is None
        mock_msgs.warning.assert_called_once()

    def test_cache_hit_returns_ports(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.get_cache_key = MagicMock(return_value="k")
        req = _make_request()
        mock_obj = MagicMock(pk=1)
        ports = [{"ifName": "Gi0/1"}]

        with patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache:
            mock_cache.get.return_value = {"ports": ports}
            result = view.get_cached_ports_data(req, mock_obj, "default")

        assert result == ports

    def test_no_server_key_uses_librenms_api(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="mykey")
        req = _make_request()
        mock_obj = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = None
            view.get_cached_ports_data(req, mock_obj, None)

        # get_cache_key should have been called with (obj, "ports", resolved_server_key)
        view.get_cache_key.assert_called_once_with(mock_obj, "ports", "mykey")


# ===========================================================================
# SyncInterfacesView.post — full flows
# ===========================================================================


class TestSyncInterfacesViewPost:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=_denied_response())
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        req = _make_request(post_data={"select": ["Gi0/1"]})
        result = view.post(req, "device", 1)
        assert result.status_code == 403

    def test_no_selection_redirects(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        req = _make_request(post_data={})  # No selection

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.post(req, "device", 1)

        mock_msgs.error.assert_called_once()
        mock_redirect.assert_called_once()

    def test_cache_miss_redirects(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        req = _make_request(post_data={"select": ["Gi0/1"]})

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages"),
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch.object(type(view), "get_vlan_groups_for_device", return_value=[]),
            patch.object(view.__class__, "get_cache_key", return_value="k"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = None
            view.post(req, "device", 1)

        mock_redirect.assert_called()

    def test_device_post_success(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        ports = [{"ifName": "Gi0/1", "port_id": 10}]
        req = _make_request(post_data={"select": ["Gi0/1"], "server_key": "default"})

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
            patch.object(view, "sync_interface"),
            patch.object(type(view), "get_vlan_groups_for_device", return_value=[]),
            patch.object(view.__class__, "get_cache_key", return_value="k"),
            patch.object(view.__class__, "_build_vlan_lookup_maps", return_value={}),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ports": ports}
            view.post(req, "device", 1)

        mock_msgs.success.assert_called_once()
        mock_redirect.assert_called_once()

    def test_vm_post_success(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")

        mock_vm = MagicMock(pk=5)
        ports = [{"ifName": "eth0", "port_id": 20}]
        req = _make_request(post_data={"select": ["eth0"], "server_key": "default"})

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_vm),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
            patch.object(view, "sync_interface"),
            patch.object(type(view), "get_vlan_groups_for_device", return_value=[]),
            patch.object(view.__class__, "get_cache_key", return_value="k"),
            patch.object(view.__class__, "_build_vlan_lookup_maps", return_value={}),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ports": ports}
            view.post(req, "virtualmachine", 5)

        mock_msgs.success.assert_called_once()
        mock_redirect.assert_called_once()


# ===========================================================================
# SyncInterfacesView.sync_interface — Device paths
# ===========================================================================


class TestSyncInterfacesViewSyncInterfaceDevice:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request()
        view._post_server_key = "default"
        view._lookup_maps = {}
        view.interface_name_field = "ifName"
        view.update_interface_attributes = MagicMock()
        view._sync_interface_vlans = MagicMock()
        return view

    def test_device_interface_created(self):
        from dcim.models import Device

        view = self._make_view()
        # __class__ = Device makes isinstance(mock_device, Device) → True
        mock_device = MagicMock()
        mock_device.__class__ = Device
        mock_device.virtual_chassis = None

        mock_interface = MagicMock()
        librenms_port = {"ifName": "Gi0/1", "port_id": None}

        with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls:
            mock_intf_cls.objects.get_or_create.return_value = (mock_interface, True)
            view.get_netbox_interface_type = MagicMock(return_value="1000base-t")
            view.sync_interface(mock_device, librenms_port, [], "ifName")

        mock_intf_cls.objects.get_or_create.assert_called_once()
        view.update_interface_attributes.assert_called_once()

    def test_device_selection_with_vc_valid(self):
        from dcim.models import Device

        view = self._make_view()
        view.request = _make_request(post_data={"device_selection_Gi0/1": "2"})

        mock_device = MagicMock()
        mock_device.__class__ = Device
        mock_device.id = 1
        mock_vc = MagicMock()
        mock_vc.members.values_list.return_value = [1, 2]
        mock_device.virtual_chassis = mock_vc

        mock_target_device = MagicMock()
        mock_target_device.__class__ = Device
        mock_target_device.id = 2

        mock_interface = MagicMock()
        librenms_port = {"ifName": "Gi0/1", "port_id": None}

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch.object(Device, "objects") as mock_device_objects,
        ):
            mock_device_objects.get.return_value = mock_target_device
            mock_intf_cls.objects.get_or_create.return_value = (mock_interface, True)
            view.get_netbox_interface_type = MagicMock(return_value="other")
            view.sync_interface(mock_device, librenms_port, [], "ifName")

        mock_intf_cls.objects.get_or_create.assert_called_once_with(device=mock_target_device, name="Gi0/1")

    def test_device_selection_invalid_defaults_to_obj(self):
        from dcim.models import Device

        view = self._make_view()
        view.request = _make_request(post_data={"device_selection_Gi0/1": "99"})

        mock_device = MagicMock()
        mock_device.__class__ = Device
        mock_device.id = 1
        mock_device.virtual_chassis = None

        mock_other_device = MagicMock()
        mock_other_device.__class__ = Device
        mock_other_device.id = 99  # Different id, no VC → falls back to obj

        mock_interface = MagicMock()
        librenms_port = {"ifName": "Gi0/1", "port_id": None}

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch.object(Device, "objects") as mock_device_objects,
        ):
            mock_device_objects.get.return_value = mock_other_device
            mock_intf_cls.objects.get_or_create.return_value = (mock_interface, True)
            view.get_netbox_interface_type = MagicMock(return_value="other")
            view.sync_interface(mock_device, librenms_port, [], "ifName")

        # Should use mock_device (obj), not mock_other_device
        call_kwargs = mock_intf_cls.objects.get_or_create.call_args[1]
        assert call_kwargs["device"] is mock_device

    def test_device_selection_does_not_exist_defaults_to_obj(self):
        from dcim.models import Device

        view = self._make_view()
        view.request = _make_request(post_data={"device_selection_Gi0/1": "999"})

        mock_device = MagicMock()
        mock_device.__class__ = Device
        mock_device.id = 1
        mock_device.virtual_chassis = None

        mock_interface = MagicMock()
        librenms_port = {"ifName": "Gi0/1", "port_id": None}

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch.object(Device, "objects") as mock_device_objects,
        ):
            mock_device_objects.get.side_effect = Device.DoesNotExist()
            mock_intf_cls.objects.get_or_create.return_value = (mock_interface, True)
            view.get_netbox_interface_type = MagicMock(return_value="other")
            view.sync_interface(mock_device, librenms_port, [], "ifName")

        call_kwargs = mock_intf_cls.objects.get_or_create.call_args[1]
        assert call_kwargs["device"] is mock_device


class TestSyncInterfacesViewSyncInterfaceVM:
    def test_vm_interface_created(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        from virtualization.models import VirtualMachine

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request()
        view._post_server_key = "default"
        view._lookup_maps = {}
        view.interface_name_field = "ifName"
        view.update_interface_attributes = MagicMock()
        view._sync_interface_vlans = MagicMock()

        mock_vm = MagicMock()
        mock_vm.__class__ = VirtualMachine
        mock_vm_interface = MagicMock()
        librenms_port = {"ifName": "eth0", "port_id": None}

        with patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface") as mock_vmintf_cls:
            mock_vmintf_cls.objects.get_or_create.return_value = (mock_vm_interface, True)
            view.sync_interface(mock_vm, librenms_port, [], "ifName")

        mock_vmintf_cls.objects.get_or_create.assert_called_once()
        view.update_interface_attributes.assert_called_once()

    def test_invalid_obj_raises_value_error(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        import pytest

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request()
        librenms_port = {"ifName": "eth0"}

        with pytest.raises(ValueError):
            view.sync_interface(MagicMock(), librenms_port, [], "ifName")


# ===========================================================================
# SyncInterfacesView.get_netbox_interface_type
# ===========================================================================


class TestSyncInterfacesViewGetNetboxInterfaceType:
    def test_with_speed_uses_speed_mapping(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        mock_mapping = MagicMock()
        mock_mapping.netbox_type = "1000base-t"

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=1000),
            patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mock_cls,
        ):
            # mappings = objects.filter(...); speed_mapping = mappings.filter(...).order_by(...).first()
            mock_cls.objects.filter.return_value.filter.return_value.order_by.return_value.first.return_value = (
                mock_mapping
            )
            result = view.get_netbox_interface_type({"ifType": "ethernetCsmacd", "ifSpeed": 1000000000})

        assert result == "1000base-t"

    def test_no_speed_uses_null_mapping(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        mock_mapping = MagicMock()
        mock_mapping.netbox_type = "virtual"

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None),
            patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mock_cls,
        ):
            mock_qs = MagicMock()
            mock_qs.filter.return_value.first.return_value = mock_mapping
            mock_cls.objects.filter.return_value = mock_qs
            result = view.get_netbox_interface_type({"ifType": "softwareLoopback", "ifSpeed": None})

        assert result == "virtual"

    def test_no_mapping_returns_other(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None),
            patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mock_cls,
        ):
            mock_qs = MagicMock()
            mock_qs.filter.return_value.first.return_value = None
            mock_cls.objects.filter.return_value = mock_qs
            result = view.get_netbox_interface_type({"ifType": "unknown", "ifSpeed": None})

        assert result == "other"

    def test_speed_mapping_falls_back_to_null(self):
        """When speed mapping returns None, falls back to null-speed mapping."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        mock_null_mapping = MagicMock()
        mock_null_mapping.netbox_type = "other"

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=1000),
            patch("netbox_librenms_plugin.views.sync.interfaces.InterfaceTypeMapping") as mock_cls,
        ):
            mock_speed_qs = MagicMock()
            mock_speed_qs.order_by.return_value.first.return_value = None  # No speed match
            mock_null_qs = MagicMock()
            mock_null_qs.first.return_value = mock_null_mapping
            mock_qs = MagicMock()
            mock_qs.filter.side_effect = [mock_speed_qs, mock_null_qs]
            mock_cls.objects.filter.return_value = mock_qs
            result = view.get_netbox_interface_type({"ifType": "ethernetCsmacd", "ifSpeed": 1000})

        assert result == "other"


# ===========================================================================
# SyncInterfacesView.handle_mac_address
# ===========================================================================


class TestSyncInterfacesViewHandleMacAddress:
    def test_no_mac_address_does_nothing(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        interface = MagicMock()
        view.handle_mac_address(interface, None)
        interface.mac_addresses.add.assert_not_called()

    def test_new_mac_created_and_added(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        interface = MagicMock()
        interface.mac_addresses.filter.return_value.first.return_value = None
        mock_mac = MagicMock()

        with patch("netbox_librenms_plugin.views.sync.interfaces.MACAddress") as mock_mac_cls:
            mock_mac_cls.objects.create.return_value = mock_mac
            view.handle_mac_address(interface, "aa:bb:cc:dd:ee:ff")

        interface.mac_addresses.add.assert_called_once_with(mock_mac)

    def test_existing_mac_added_without_create(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        existing_mac = MagicMock()
        interface = MagicMock()
        interface.mac_addresses.filter.return_value.first.return_value = existing_mac

        with patch("netbox_librenms_plugin.views.sync.interfaces.MACAddress") as mock_mac_cls:
            view.handle_mac_address(interface, "aa:bb:cc:dd:ee:ff")

        mock_mac_cls.objects.create.assert_not_called()
        interface.mac_addresses.add.assert_called_once_with(existing_mac)

    def test_primary_mac_assigned_if_attribute_exists(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        interface = MagicMock()
        interface.mac_addresses.filter.return_value.first.return_value = None
        mock_mac = MagicMock()

        with patch("netbox_librenms_plugin.views.sync.interfaces.MACAddress") as mock_mac_cls:
            mock_mac_cls.objects.create.return_value = mock_mac
            # Ensure interface has primary_mac_address attribute
            interface.primary_mac_address = None
            view.handle_mac_address(interface, "aa:bb:cc:dd:ee:ff")

        assert interface.primary_mac_address == mock_mac


# ===========================================================================
# SyncInterfacesView.update_interface_attributes
# ===========================================================================


class TestSyncInterfacesViewUpdateInterfaceAttributes:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request()
        view._post_server_key = "default"
        view.handle_mac_address = MagicMock()
        return view

    def test_basic_attributes_set(self):
        from dcim.models import Interface

        view = self._make_view()
        interface = MagicMock()
        interface.__class__ = Interface
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1000000000,
            "ifAlias": "uplink",
            "ifMtu": 1500,
            "port_id": None,
            "ifAdminStatus": "up",
        }

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=1000000):
            view.update_interface_attributes(interface, librenms_port, "1000base-t", [], "ifName")

        interface.save.assert_called_once()
        assert interface.name == "Gi0/1"
        assert interface.type == "1000base-t"
        assert interface.speed == 1000000
        assert interface.description == "uplink"
        assert interface.mtu == 1500
        assert interface.enabled is True

    def test_excluded_columns_skipped(self):
        from dcim.models import Interface

        view = self._make_view()
        # MagicMock(spec=Interface) passes isinstance check; explicit attr init makes changes detectable
        interface = MagicMock(spec=Interface)
        interface.name = None
        interface.type = None
        interface.speed = None
        interface.description = None
        interface.mtu = None
        interface.enabled = None
        interface.mac_address = None
        interface.save = MagicMock()
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 0,
            "ifAlias": None,
            "ifMtu": None,
            "port_id": None,
            "ifAdminStatus": None,
        }

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=0):
            view.handle_mac_address = MagicMock()
            view.update_interface_attributes(
                interface,
                librenms_port,
                "other",
                ["name", "type", "speed", "description", "mtu", "enabled", "mac_address"],
                "ifName",
            )

        # save still called
        interface.save.assert_called_once()
        # MAC handler must not be invoked when "mac_address" is in excluded_columns
        view.handle_mac_address.assert_not_called()
        # All other excluded attributes remain at their initial None
        assert interface.name is None
        assert interface.type is None
        assert interface.speed is None
        assert interface.description is None
        assert interface.mtu is None
        assert interface.enabled is None

    def test_admin_status_down_sets_disabled(self):
        from dcim.models import Interface

        view = self._make_view()
        interface = MagicMock()
        interface.__class__ = Interface
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": None,
            "ifSpeed": None,
            "ifAlias": None,
            "ifMtu": None,
            "port_id": None,
            "ifAdminStatus": "down",
        }

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(interface, librenms_port, None, [], "ifName")

        assert interface.enabled is False

    def test_port_id_calls_set_librenms_device_id(self):
        from dcim.models import Interface

        view = self._make_view()
        interface = MagicMock()
        interface.__class__ = Interface
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": None,
            "ifSpeed": None,
            "ifAlias": None,
            "ifMtu": None,
            "port_id": 42,
            "ifAdminStatus": "up",
        }

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None),
            patch("netbox_librenms_plugin.views.sync.interfaces.set_librenms_device_id") as mock_set,
        ):
            view.update_interface_attributes(interface, librenms_port, None, [], "ifName")

        mock_set.assert_called_once_with(interface, 42, "default")

    def test_ifalias_not_set_when_same_as_name(self):
        """ifAlias should not overwrite when equal to interface name."""
        from dcim.models import Interface

        view = self._make_view()
        # MagicMock(spec=Interface) with explicit init so plain assignments are detectable
        interface = MagicMock(spec=Interface)
        interface.description = None
        interface.save = MagicMock()
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": None,
            "ifSpeed": None,
            "ifAlias": "Gi0/1",  # Same as interface name → should not set description
            "ifMtu": None,
            "port_id": None,
            "ifAdminStatus": "up",
        }

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(interface, librenms_port, None, [], "ifName")

        # description should remain None since ifAlias == interface_name
        assert interface.description is None


# ===========================================================================
# SyncInterfacesView._sync_interface_vlans
# ===========================================================================


class TestSyncInterfacesViewSyncInterfaceVlans:
    def test_no_vlans_calls_update_with_empty(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request()
        view._lookup_maps = {}
        view._update_interface_vlan_assignment = MagicMock()

        interface = MagicMock()
        librenms_port = {}

        view._sync_interface_vlans(interface, librenms_port, "Gi0/1")

        view._update_interface_vlan_assignment.assert_called_once()

    def test_with_vlans_builds_group_map(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request(post_data={"vlan_group_Gi0_1_100": "5"})
        view._lookup_maps = {}
        view._update_interface_vlan_assignment = MagicMock()

        interface = MagicMock()
        librenms_port = {"untagged_vlan": 100, "tagged_vlans": [200]}

        view._sync_interface_vlans(interface, librenms_port, "Gi0/1")

        call_args = view._update_interface_vlan_assignment.call_args
        vlan_group_map = call_args[0][2]
        assert vlan_group_map.get("100") == "5"


# ===========================================================================
# SyncInterfacesView.sync_selected_interfaces
# ===========================================================================


class TestSyncInterfacesViewSyncSelected:
    def test_syncs_matching_ports(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.interface_name_field = "ifName"
        view.sync_interface = MagicMock()

        ports_data = [{"ifName": "Gi0/1"}, {"ifName": "Gi0/2"}]
        selected = ["Gi0/1"]

        with patch("netbox_librenms_plugin.views.sync.interfaces.transaction"):
            view.sync_selected_interfaces(MagicMock(), selected, ports_data, [], "ifName")

        assert view.sync_interface.call_count == 1
        call_args = view.sync_interface.call_args
        assert call_args[0][1]["ifName"] == "Gi0/1"


# ===========================================================================
# DeleteNetBoxInterfacesView
# ===========================================================================


class TestDeleteNetBoxInterfacesViewPermissions:
    def test_device_returns_interface_delete_perm(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        from dcim.models import Interface

        view = object.__new__(DeleteNetBoxInterfacesView)
        perms = view.get_required_permissions_for_object_type("device")
        assert ("delete", Interface) in perms

    def test_vm_returns_vminterface_delete_perm(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        from virtualization.models import VMInterface

        view = object.__new__(DeleteNetBoxInterfacesView)
        perms = view.get_required_permissions_for_object_type("virtualmachine")
        assert ("delete", VMInterface) in perms

    def test_invalid_type_raises_http404(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        from django.http import Http404
        import pytest

        view = object.__new__(DeleteNetBoxInterfacesView)
        with pytest.raises(Http404):
            view.get_required_permissions_for_object_type("invalid")


class TestDeleteNetBoxInterfacesViewPost:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=_denied_response())

        req = _make_request(post_data={"interface_ids": ["1"]})
        result = view.post(req, "device", 1)
        assert result.status_code == 403

    def test_empty_interface_ids_returns_400(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        from django.http import JsonResponse

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=None)

        mock_device = MagicMock(pk=1)
        req = _make_request(post_data={})  # No interface_ids

        with patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device):
            result = view.post(req, "device", 1)

        assert isinstance(result, JsonResponse)
        assert result.status_code == 400

    def test_invalid_object_type_returns_400(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        from django.http import JsonResponse

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=None)

        req = _make_request(post_data={"interface_ids": ["1"]})
        result = view.post(req, "badtype", 1)

        assert isinstance(result, JsonResponse)
        assert result.status_code == 400

    def test_device_interface_deleted_successfully(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        from django.http import JsonResponse
        import json

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=None)

        mock_device = MagicMock(pk=1)
        mock_device.id = 1
        mock_device.virtual_chassis = None

        mock_interface = MagicMock()
        mock_interface.name = "Gi0/1"
        mock_interface.device_id = 1

        req = _make_request(post_data={"interface_ids": ["5"]})

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
        ):
            mock_intf_cls.objects.get.return_value = mock_interface

            class _DNE(Exception):
                pass

            mock_intf_cls.DoesNotExist = _DNE

            result = view.post(req, "device", 1)

        assert isinstance(result, JsonResponse)
        data = json.loads(result.content)
        assert data["deleted_count"] == 1
        mock_interface.delete.assert_called_once()

    def test_vm_interface_deleted_successfully(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        from virtualization.models import VMInterface
        from django.http import JsonResponse
        import json

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=None)

        mock_vm = MagicMock(pk=2)
        mock_vm.id = 2

        mock_interface = MagicMock(spec=VMInterface)
        mock_interface.name = "eth0"
        mock_interface.virtual_machine_id = 2

        req = _make_request(post_data={"interface_ids": ["7"]})

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_vm),
            patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface") as mock_vmintf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
        ):
            mock_vmintf_cls.objects.get.return_value = mock_interface

            class _DNE(Exception):
                pass

            mock_vmintf_cls.DoesNotExist = _DNE

            result = view.post(req, "virtualmachine", 2)

        assert isinstance(result, JsonResponse)
        data = json.loads(result.content)
        assert data["deleted_count"] == 1
        mock_interface.delete.assert_called_once()

    def test_device_interface_wrong_device_adds_error(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        import json

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=None)

        mock_device = MagicMock(pk=1)
        mock_device.id = 1
        mock_device.virtual_chassis = None

        mock_interface = MagicMock()
        mock_interface.name = "Gi0/1"
        mock_interface.device_id = 99  # Wrong device

        req = _make_request(post_data={"interface_ids": ["5"]})

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
        ):
            mock_intf_cls.objects.get.return_value = mock_interface

            class _DNE(Exception):
                pass

            mock_intf_cls.DoesNotExist = _DNE

            result = view.post(req, "device", 1)

        data = json.loads(result.content)
        assert data["deleted_count"] == 0
        assert len(data["errors"]) == 1

    def test_device_interface_with_vc_wrong_member_adds_error(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        import json

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=None)

        mock_device = MagicMock(pk=1)
        mock_device.id = 1
        mock_vc = MagicMock()
        mock_member = MagicMock()
        mock_member.id = 1
        mock_vc.members.all.return_value = [mock_member]
        mock_device.virtual_chassis = mock_vc

        mock_interface = MagicMock()
        mock_interface.name = "Gi0/1"
        mock_interface.device_id = 999  # Not in VC

        req = _make_request(post_data={"interface_ids": ["5"]})

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
        ):
            mock_intf_cls.objects.get.return_value = mock_interface

            class _DNE(Exception):
                pass

            mock_intf_cls.DoesNotExist = _DNE

            result = view.post(req, "device", 1)

        data = json.loads(result.content)
        assert data["deleted_count"] == 0
        assert len(data["errors"]) == 1

    def test_interface_not_found_adds_error(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        import json

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=None)

        mock_device = MagicMock(pk=1)
        mock_device.id = 1
        mock_device.virtual_chassis = None

        req = _make_request(post_data={"interface_ids": ["999"]})

        class _DNE(Exception):
            pass

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
        ):
            mock_intf_cls.DoesNotExist = _DNE
            mock_intf_cls.objects.get.side_effect = _DNE()

            result = view.post(req, "device", 1)

        data = json.loads(result.content)
        assert data["deleted_count"] == 0
        assert len(data["errors"]) == 1

    def test_vm_interface_wrong_vm_adds_error(self):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        from virtualization.models import VMInterface
        import json

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=None)

        mock_vm = MagicMock(pk=2)
        mock_vm.id = 2

        mock_interface = MagicMock(spec=VMInterface)
        mock_interface.name = "eth0"
        mock_interface.virtual_machine_id = 99  # Wrong VM

        req = _make_request(post_data={"interface_ids": ["7"]})

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_vm),
            patch("netbox_librenms_plugin.views.sync.interfaces.VMInterface") as mock_vmintf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
        ):
            mock_vmintf_cls.objects.get.return_value = mock_interface

            class _DNE(Exception):
                pass

            mock_vmintf_cls.DoesNotExist = _DNE

            result = view.post(req, "virtualmachine", 2)

        data = json.loads(result.content)
        assert data["deleted_count"] == 0
        assert len(data["errors"]) == 1

    def test_response_includes_errors_in_message(self):
        """When errors exist, message mentions error count."""
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView
        from dcim.models import Interface
        import json

        view = object.__new__(DeleteNetBoxInterfacesView)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        view.require_all_permissions_json = MagicMock(return_value=None)

        mock_device = MagicMock(pk=1)
        mock_device.id = 1
        mock_device.virtual_chassis = None

        # Two interfaces: one that belongs, one that doesn't
        mock_interface_ok = MagicMock(spec=Interface)
        mock_interface_ok.name = "Gi0/1"
        mock_interface_ok.device_id = 1

        mock_interface_bad = MagicMock(spec=Interface)
        mock_interface_bad.name = "Gi0/2"
        mock_interface_bad.device_id = 99  # Wrong device

        req = _make_request(post_data={"interface_ids": ["5", "6"]})

        call_count = [0]

        def get_side_effect(id):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_interface_ok
            return mock_interface_bad

        class _DNE(Exception):
            pass

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
        ):
            mock_intf_cls.DoesNotExist = _DNE
            mock_intf_cls.objects.get.side_effect = get_side_effect
            result = view.post(req, "device", 1)

        data = json.loads(result.content)
        assert data["deleted_count"] == 1
        assert "error" in data["message"]
        mock_interface_ok.delete.assert_called_once()
