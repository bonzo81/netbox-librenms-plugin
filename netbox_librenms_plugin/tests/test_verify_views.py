"""Tests for SingleCableVerifyView and SingleInterfaceVerifyView VC resolution.

Verifies that both views delegate VC device resolution to
get_librenms_sync_device() and handle the None return gracefully
(e.g. empty VC members or vc_position type errors).
"""

import json
from unittest.mock import ANY, MagicMock, patch


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

        mock_sync.assert_called_once_with(device, server_key=ANY)
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


# ---------------------------------------------------------------------------
# SingleInterfaceVerifyView
# ---------------------------------------------------------------------------
class TestSingleInterfaceVerifyView:
    """SingleInterfaceVerifyView.post() VC resolution and None guard."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import SingleInterfaceVerifyView

        view = object.__new__(SingleInterfaceVerifyView)
        view._librenms_api = MagicMock()
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

        mock_sync.assert_called_once_with(device, server_key=ANY)
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
