"""Coverage tests for views/object_sync/devices.py."""

from unittest.mock import MagicMock, patch


def _make_device_view():
    """Create a DeviceLibreNMSSyncView instance bypassing __init__."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

    view = object.__new__(DeviceLibreNMSSyncView)
    view.request = MagicMock()
    view.request.path = "/dcim/devices/1/librenms-sync/"
    view.kwargs = {}
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    view._librenms_api.cache_timeout = 300
    return view


def _make_interface_view():
    """Create a DeviceInterfaceTableView instance bypassing __init__."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

    view = object.__new__(DeviceInterfaceTableView)
    view.request = MagicMock()
    view.request.path = "/dcim/devices/1/librenms-sync/"
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    return view


class TestDeviceLibreNMSSyncViewContextMethods:
    """Tests for DeviceLibreNMSSyncView context delegation (lines 47-73)."""

    def test_get_interface_context_delegates_to_interface_view(self):
        """get_interface_context() creates DeviceInterfaceTableView, copies request, and calls get_context_data."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"interfaces": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceInterfaceTableView.get_context_data",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_interface_name_field", return_value="ifName"
            ):
                result = view.get_interface_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        child_instance = mock_get_context.call_args[0][0]
        assert child_instance.request is request
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj

    def test_get_cable_context_delegates_to_cable_view(self):
        """get_cable_context() creates DeviceCableTableView, copies request, and calls get_context_data."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"cables": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceCableTableView.get_context_data",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            result = view.get_cable_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        child_instance = mock_get_context.call_args[0][0]
        assert child_instance.request is request
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj

    def test_get_ip_context_delegates_to_ip_view(self):
        """get_ip_context() creates DeviceIPAddressTableView and calls get_context_data with request and obj."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"ips": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceIPAddressTableView.get_context_data",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            result = view.get_ip_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj
        child_instance = mock_get_context.call_args[0][0]
        assert child_instance.request is request

    def test_get_vlan_context_delegates_to_vlan_view(self):
        """get_vlan_context() creates DeviceVLANTableView, copies request, and calls get_vlan_context."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"vlans": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceVLANTableView.get_vlan_context",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            result = view.get_vlan_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        child_instance = mock_get_context.call_args[0][0]
        assert child_instance.request is request
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj

    def test_get_module_context_delegates_to_module_view(self):
        """get_module_context() creates DeviceModuleTableView, copies request, and calls get_context_data."""

        view = _make_device_view()
        request = MagicMock()
        obj = MagicMock()

        mock_ctx = {"modules": []}
        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.DeviceModuleTableView.get_context_data",
            autospec=True,
            return_value=mock_ctx,
        ) as mock_get_context:
            result = view.get_module_context(request, obj)
        assert result == mock_ctx
        assert mock_get_context.called
        child_instance = mock_get_context.call_args[0][0]
        assert child_instance.request is request
        assert mock_get_context.call_args[0][1] is request
        assert mock_get_context.call_args[0][2] is obj


class TestDeviceInterfaceTableView:
    """Tests for DeviceInterfaceTableView (lines 76-109)."""

    def test_get_interfaces_returns_all_interfaces(self):
        """get_interfaces() returns obj.interfaces.all()."""
        view = _make_interface_view()
        obj = MagicMock()
        mock_qs = MagicMock()
        obj.interfaces.all.return_value = mock_qs

        result = view.get_interfaces(obj)
        assert result is mock_qs
        obj.interfaces.all.assert_called_once()

    def test_get_redirect_url_returns_device_url(self):
        """get_redirect_url() returns the device interface sync URL."""
        view = _make_interface_view()
        obj = MagicMock()
        obj.pk = 42

        with patch("netbox_librenms_plugin.views.object_sync.devices.reverse") as mock_reverse:
            mock_reverse.return_value = "/dcim/devices/42/interface-sync/"
            result = view.get_redirect_url(obj)
        mock_reverse.assert_called_once_with("plugins:netbox_librenms_plugin:device_interface_sync", kwargs={"pk": 42})
        assert result == "/dcim/devices/42/interface-sync/"

    def test_get_table_returns_vc_table_for_vc_device(self):
        """get_table() returns VCInterfaceTable when device has virtual_chassis."""

        view = _make_interface_view()
        obj = MagicMock()
        obj.virtual_chassis = MagicMock()  # Has VC
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        with patch("netbox_librenms_plugin.views.object_sync.devices.VCInterfaceTable") as mock_vc_table:
            mock_table = MagicMock()
            mock_vc_table.return_value = mock_table
            result = view.get_table([], obj, "ifName", vlan_groups=[])

        mock_vc_table.assert_called_once_with(
            [], device=obj, interface_name_field="ifName", vlan_groups=[], server_key="default"
        )
        assert result is mock_table

    def test_get_table_returns_librenms_table_for_non_vc_device(self):
        """get_table() returns LibreNMSInterfaceTable when no virtual_chassis."""
        view = _make_interface_view()
        obj = MagicMock()
        obj.virtual_chassis = None  # No VC

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSInterfaceTable") as mock_table_cls:
            mock_table = MagicMock()
            mock_table_cls.return_value = mock_table
            result = view.get_table([], obj, "ifName")

        mock_table_cls.assert_called_once_with(
            [], device=obj, interface_name_field="ifName", vlan_groups=None, server_key="default"
        )
        assert result is mock_table

    def test_get_table_sets_htmx_url(self):
        """get_table() sets htmx_url on the returned table."""
        view = _make_interface_view()
        view.request.path = "/dcim/devices/1/librenms-sync/"
        obj = MagicMock()
        obj.virtual_chassis = None

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSInterfaceTable") as mock_table_cls:
            mock_table = MagicMock()
            mock_table_cls.return_value = mock_table
            view.get_table([], obj, "ifName")

        assert mock_table.htmx_url == "/dcim/devices/1/librenms-sync/?tab=interfaces&server_key=default"


class TestSingleInterfaceVerifyView:
    """Tests for SingleInterfaceVerifyView (lines 112-152)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView
        from unittest.mock import MagicMock

        view = object.__new__(SingleInterfaceVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_returns_400_when_no_device_id(self):
        """Returns 400 JSON error when no device_id in request body."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"interface_name": "eth0"}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_returns_404_when_no_cached_data(self):
        """Returns 404 when cached ports data not found."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1, "interface_name": "eth0"}).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=mock_device):
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=mock_device
            ):
                with patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache:
                    mock_cache.get.return_value = None
                    with patch.object(view, "get_cache_key", return_value="test_key"):
                        response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 404

    def test_returns_404_when_interface_not_in_cache(self):
        """Returns 404 when interface not found in cached ports data."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1, "interface_name": "eth99"}).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None

        cached_data = {"ports": [{"ifName": "eth0", "speed": 1000}]}

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=mock_device):
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=mock_device
            ):
                with patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache:
                    mock_cache.get.return_value = cached_data
                    with patch.object(view, "get_cache_key", return_value="test_key"):
                        response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 404

    def test_returns_success_when_interface_found(self):
        """Returns success JSON with formatted_row when interface found in cache."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1, "interface_name": "eth0", "interface_name_field": "ifName"}).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None

        port_data = {"ifName": "eth0", "speed": 1000}
        cached_data = {"ports": [port_data]}

        mock_table = MagicMock()
        mock_table.format_interface_data.return_value = "<tr>row</tr>"

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=mock_device):
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=mock_device
            ):
                with patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache:
                    mock_cache.get.return_value = cached_data
                    with patch.object(view, "get_cache_key", return_value="test_key"):
                        with patch(
                            "netbox_librenms_plugin.views.object_sync.devices.LibreNMSInterfaceTable",
                            return_value=mock_table,
                        ):
                            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "success"
        assert "formatted_row" in data

    def test_non_vc_device_skips_sync_device_lookup(self):
        """For non-VC devices, get_librenms_sync_device is not called; selected_device used directly (line 123)."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1, "interface_name": "eth0", "interface_name_field": "ifName"}).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        port_data = {"ifName": "eth0", "speed": 1000}
        cached_data = {"ports": [port_data]}
        mock_table = MagicMock()
        mock_table.format_interface_data.return_value = "<tr>row</tr>"

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=mock_device):
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=None
            ) as mock_get_sync_device:
                with patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache:
                    mock_cache.get.return_value = cached_data
                    with patch.object(view, "get_cache_key", return_value="test_key") as get_cache_key_mock:
                        with patch(
                            "netbox_librenms_plugin.views.object_sync.devices.LibreNMSInterfaceTable",
                            return_value=mock_table,
                        ):
                            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        # Non-VC device: get_librenms_sync_device should NOT be called
        mock_get_sync_device.assert_not_called()
        # The cache key builder was called with mock_device directly
        assert get_cache_key_mock.call_args[0][0] is mock_device


class TestSingleVlanGroupVerifyView:
    """Tests for SingleVlanGroupVerifyView (lines 155-278)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleVlanGroupVerifyView

        view = object.__new__(SingleVlanGroupVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_returns_400_when_no_device_id(self):
        """Returns 400 when no device_id provided."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid": "10"}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_returns_400_when_no_vid(self):
        """Returns 400 when no vid provided."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_returns_400_when_invalid_vid(self):
        """Returns 400 when vid is not a valid integer."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1, "vid": "notanumber"}).encode()

        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=mock_device):
            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_returns_success_with_vlan_group(self):
        """Returns success with css_class when valid vlan_group_id provided."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps(
            {
                "device_id": 1,
                "vid": "10",
                "vlan_group_id": "5",
                "vlan_type": "U",
            }
        ).encode()

        mock_device = MagicMock()
        mock_netbox_iface = MagicMock()
        mock_netbox_iface.untagged_vlan = None
        mock_netbox_iface.tagged_vlans.all.return_value = []
        mock_device.interfaces.filter.return_value.first.return_value = mock_netbox_iface

        mock_vlan_group = MagicMock()
        mock_vlan_qs = MagicMock()
        mock_vlan_qs.values_list.return_value = [10]
        mock_global_qs = MagicMock()
        mock_global_qs.values_list.return_value = []

        mock_vlan_model = MagicMock()
        mock_vlan_model.objects.filter.return_value = mock_vlan_qs
        mock_vlan_group_model = MagicMock()
        mock_vlan_group_model.objects.filter.return_value = mock_global_qs

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404") as mock_get_obj:
            mock_get_obj.side_effect = [mock_device, mock_vlan_group]
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_untagged_vlan_css_class",
                return_value="text-success",
            ):
                with patch(
                    "netbox_librenms_plugin.views.object_sync.devices.get_tagged_vlan_css_class",
                    return_value="text-success",
                ):
                    with patch(
                        "netbox_librenms_plugin.views.object_sync.devices.get_missing_vlan_warning", return_value=""
                    ):
                        # Patch the VLAN/VLANGroup imports inside the method
                        mock_ipam = MagicMock()
                        mock_ipam.VLAN = mock_vlan_model
                        mock_ipam.VLANGroup = mock_vlan_group_model

                        with patch.dict("sys.modules", {"ipam.models": mock_ipam}):
                            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["css_class"] == "text-success"
        assert data["is_missing"] is False

    def test_returns_success_without_vlan_group(self):
        """Returns success with global VLANs when no vlan_group_id provided."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps(
            {
                "device_id": 1,
                "vid": "100",
                "vlan_type": "T",
            }
        ).encode()

        mock_device = MagicMock()
        mock_device.interfaces.filter.return_value.first.return_value = None

        mock_vlan_qs = MagicMock()
        mock_vlan_qs.values_list.return_value = []
        mock_vlan_model = MagicMock()
        mock_vlan_model.objects.filter.return_value = mock_vlan_qs

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=mock_device):
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_untagged_vlan_css_class",
                return_value="text-danger",
            ):
                with patch(
                    "netbox_librenms_plugin.views.object_sync.devices.get_tagged_vlan_css_class",
                    return_value="text-danger",
                ):
                    with patch(
                        "netbox_librenms_plugin.views.object_sync.devices.get_missing_vlan_warning", return_value=""
                    ):
                        mock_ipam = MagicMock()
                        mock_ipam.VLAN = mock_vlan_model
                        mock_ipam.VLANGroup = MagicMock()

                        with patch.dict("sys.modules", {"ipam.models": mock_ipam}):
                            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "success"
        assert data["css_class"] == "text-danger"
        assert data["is_missing"] is True  # vid=100 not in empty group VID list

    def test_existing_interface_with_untagged_and_tagged_vlans(self):
        """Covers NetBox VLAN extraction branches for untagged and tagged VLANs (lines 211-215)."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps(
            {
                "device_id": 1,
                "vid": "10",
                "vlan_group_id": "5",
                "vlan_type": "T",
                "interface_name": "eth0",
            }
        ).encode()

        untagged = MagicMock()
        untagged.vid = 1
        untagged.group_id = 9
        tagged = MagicMock()
        tagged.vid = 10
        tagged.group_id = 5

        mock_iface = MagicMock()
        mock_iface.untagged_vlan = untagged
        mock_iface.tagged_vlans.all.return_value = [tagged]
        mock_device = MagicMock()
        mock_device.interfaces.filter.return_value.first.return_value = mock_iface

        mock_vlan_group = MagicMock()
        mock_group_qs = MagicMock()
        mock_group_qs.values_list.return_value = [10]
        mock_global_qs = MagicMock()
        mock_global_qs.values_list.return_value = []
        mock_vlan_model = MagicMock()
        mock_vlan_model.objects.filter.side_effect = [mock_group_qs, mock_global_qs]

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404") as mock_get_obj:
            mock_get_obj.side_effect = [mock_device, mock_vlan_group]
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_tagged_vlan_css_class",
                return_value="text-success",
            ):
                with patch(
                    "netbox_librenms_plugin.views.object_sync.devices.get_missing_vlan_warning", return_value=""
                ):
                    mock_ipam = MagicMock()
                    mock_ipam.VLAN = mock_vlan_model
                    mock_ipam.VLANGroup = MagicMock()
                    with patch.dict("sys.modules", {"ipam.models": mock_ipam}):
                        response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200

    def test_render_vlans_cell_returns_dash_for_empty_values(self):
        """Empty VLAN inputs render em dash placeholder (line 276)."""
        view = self._make_view()
        assert view._render_vlans_cell(None, [], [], False, None, set()) == "—"


class TestVerifyVlanSyncGroupView:
    """Tests for VerifyVlanSyncGroupView (lines 281-326)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import VerifyVlanSyncGroupView

        view = object.__new__(VerifyVlanSyncGroupView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_returns_400_when_no_vid(self):
        """Returns 400 when no vid provided."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vlan_group_id": "1"}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_returns_400_when_invalid_vid(self):
        """Returns 400 when vid is not a valid integer."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid": "badvalue"}).encode()

        response = view.post(request)
        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_returns_success_with_vlan_group(self):
        """Returns success with exists_in_netbox and css_class for vlan_group_id path."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid": "10", "vlan_group_id": "3", "name": "vlan10"}).encode()

        mock_vlan = MagicMock()
        mock_vlan.name = "vlan10"
        mock_vlan_group = MagicMock()

        with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404") as mock_get_obj:
            mock_get_obj.return_value = mock_vlan_group
            with patch(
                "netbox_librenms_plugin.views.object_sync.devices.get_vlan_sync_css_class", return_value="text-success"
            ):
                mock_ipam = MagicMock()
                mock_vlan_model = MagicMock()
                mock_vlan_model.objects.filter.return_value.first.return_value = mock_vlan
                mock_ipam.VLAN = mock_vlan_model
                mock_ipam.VLANGroup = MagicMock()

                with patch.dict("sys.modules", {"ipam.models": mock_ipam}):
                    response = view.post(request)

        assert isinstance(response, JsonResponse)
        data = json.loads(response.content)
        assert response.status_code == 200
        assert data["status"] == "success"
        assert "exists_in_netbox" in data
        assert data["css_class"] == "text-success"

    def test_returns_success_without_vlan_group(self):
        """Returns success with global VLAN lookup when no vlan_group_id."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid": "20", "name": "vlan20"}).encode()

        with patch(
            "netbox_librenms_plugin.views.object_sync.devices.get_vlan_sync_css_class", return_value="text-danger"
        ):
            mock_ipam = MagicMock()
            mock_vlan_model = MagicMock()
            mock_vlan_model.objects.filter.return_value.first.return_value = None
            mock_ipam.VLAN = mock_vlan_model
            mock_ipam.VLANGroup = MagicMock()

            with patch.dict("sys.modules", {"ipam.models": mock_ipam}):
                response = view.post(request)

        assert isinstance(response, JsonResponse)
        data = json.loads(response.content)
        assert response.status_code == 200
        assert data["status"] == "success"
        assert data["exists_in_netbox"] is False
        assert data["css_class"] == "text-danger"


class TestSaveVlanGroupOverridesView:
    """Tests for SaveVlanGroupOverridesView (lines 329-374)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import SaveVlanGroupOverridesView

        view = object.__new__(SaveVlanGroupOverridesView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_returns_error_when_no_device_id(self):
        """Returns 400 error when no device_id in request."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"vid_group_map": {}}).encode()

        with patch.object(view, "require_write_permission_json", return_value=None):
            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_requires_write_permission(self):
        """Returns error response when user lacks write permission."""
        import json

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1}).encode()

        error_response = MagicMock()
        with patch.object(view, "require_write_permission_json", return_value=error_response):
            result = view.post(request)

        assert result is error_response

    def test_returns_error_when_no_cached_ports(self):
        """Returns 400 when ports cache TTL is zero (no cached data)."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1, "vid_group_map": {"10": "5"}}).encode()

        mock_device = MagicMock()

        with patch.object(view, "require_write_permission_json", return_value=None):
            with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=mock_device):
                with patch(
                    "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device",
                    return_value=mock_device,
                ):
                    with patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache:
                        mock_cache.ttl.return_value = 0
                        with patch.object(view, "get_cache_key", return_value="ports_key"):
                            response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 400

    def test_saves_overrides_to_cache(self):
        """Successfully saves VLAN group overrides to cache."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps(
            {
                "device_id": 1,
                "vid_group_map": {"10": "5", "20": "5"},
                "server_key": "default",
            }
        ).encode()

        mock_device = MagicMock()

        with patch.object(view, "require_write_permission_json", return_value=None):
            with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=mock_device):
                with patch(
                    "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device",
                    return_value=mock_device,
                ):
                    with patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache:
                        mock_cache.ttl.return_value = 300
                        mock_cache.get.return_value = {}
                        with patch.object(view, "get_cache_key", return_value="ports_key"):
                            with patch.object(view, "get_vlan_overrides_key", return_value="vlan_overrides_key"):
                                response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "success"
        mock_cache.set.assert_called_once()

    def test_save_overrides_uses_device_when_sync_device_none(self):
        """If VC sync-device resolution fails, fallback uses original device (line 358)."""
        import json

        from django.http import JsonResponse

        view = self._make_view()
        request = MagicMock()
        request.body = json.dumps({"device_id": 1, "vid_group_map": {"10": "5"}, "server_key": "default"}).encode()
        mock_device = MagicMock()

        with patch.object(view, "require_write_permission_json", return_value=None):
            with patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404", return_value=mock_device):
                with patch(
                    "netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device", return_value=None
                ) as mock_get_sync_device:
                    with patch("netbox_librenms_plugin.views.object_sync.devices.cache") as mock_cache:
                        mock_cache.ttl.return_value = 300
                        mock_cache.get.return_value = {}
                        with patch.object(view, "get_cache_key", return_value="ports_key"):
                            with patch.object(view, "get_vlan_overrides_key", return_value="vlan_overrides_key"):
                                response = view.post(request)

        assert isinstance(response, JsonResponse)
        assert response.status_code == 200
        # Verify fallback: get_librenms_sync_device was called with the selected device
        mock_get_sync_device.assert_called_once()
        assert mock_get_sync_device.call_args[0][0] is mock_device


class TestDeviceCableTableView:
    """Tests for DeviceCableTableView (lines 377-386)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        view = object.__new__(DeviceCableTableView)
        view._librenms_api = MagicMock()
        return view

    def test_get_table_returns_vc_cable_table_for_vc_device(self):
        """get_table() returns VCCableTable when device has virtual_chassis."""
        view = self._make_view()
        obj = MagicMock()
        obj.virtual_chassis = MagicMock()

        with patch("netbox_librenms_plugin.views.object_sync.devices.VCCableTable") as mock_vc_table:
            mock_table = MagicMock()
            mock_vc_table.return_value = mock_table
            result = view.get_table([], obj)

        assert result is mock_table
        mock_vc_table.assert_called_once_with([], device=obj)

    def test_get_table_returns_librenms_cable_table_for_non_vc_device(self):
        """get_table() returns LibreNMSCableTable when no virtual_chassis."""
        view = self._make_view()
        obj = MagicMock()
        obj.virtual_chassis = None

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSCableTable") as mock_cable_table:
            mock_table = MagicMock()
            mock_cable_table.return_value = mock_table
            result = view.get_table([], obj)

        assert result is mock_table
        mock_cable_table.assert_called_once_with([], device=obj)


class TestDeviceModuleTableView:
    """Tests for DeviceModuleTableView (lines 401-411)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        view = object.__new__(DeviceModuleTableView)
        view.request = MagicMock()
        view.request.path = "/dcim/devices/1/librenms-sync/"
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "prod-server"
        view.has_write_permission = MagicMock(return_value=True)
        view.request.user.has_perm = MagicMock(
            side_effect=lambda p: p in {"dcim.add_module", "dcim.change_module", "dcim.delete_module"}
        )
        return view

    def test_get_table_returns_librenms_module_table(self):
        """get_table() returns LibreNMSModuleTable with device and server_key."""
        view = self._make_view()
        obj = MagicMock()

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSModuleTable") as mock_table_cls:
            mock_table = MagicMock()
            mock_table_cls.return_value = mock_table
            result = view.get_table([], obj)

        mock_table_cls.assert_called_once_with(
            [],
            device=obj,
            server_key="prod-server",
            has_write_permission=True,
            can_add_module=True,
            can_change_module=True,
            can_delete_module=True,
        )
        assert result is mock_table

    def test_get_table_sets_htmx_url(self):
        """get_table() sets htmx_url with modules tab."""
        view = self._make_view()
        obj = MagicMock()

        with patch("netbox_librenms_plugin.views.object_sync.devices.LibreNMSModuleTable") as mock_table_cls:
            mock_table = MagicMock()
            mock_table_cls.return_value = mock_table
            view.get_table([], obj)

        assert mock_table.htmx_url == "/dcim/devices/1/librenms-sync/?tab=modules&server_key=prod-server"
