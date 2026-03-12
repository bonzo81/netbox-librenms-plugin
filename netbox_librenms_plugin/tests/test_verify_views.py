"""Tests for SingleCableVerifyView and SingleInterfaceVerifyView VC resolution.

Covers the fix for the NoneType crash when a VC device has no primary_ip
on any member — both views must use get_librenms_sync_device() and guard
against None.
"""

import json
from unittest.mock import MagicMock, patch


def _make_request(body: dict) -> MagicMock:
    """Create a mock POST request with JSON body."""
    request = MagicMock()
    request.body = json.dumps(body).encode()
    request.user.has_perm.return_value = True
    return request


def _make_device(pk=1, has_vc=False, name="test-device"):
    """Create a mock Device with optional virtual_chassis."""
    device = MagicMock()
    device.pk = pk
    device.id = pk
    device.name = name
    device._meta.model_name = "device"
    device.virtual_chassis = MagicMock() if has_vc else None
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
    def test_vc_device_no_primary_ip_returns_empty_row(self, mock_cache, mock_sync, mock_get_obj):
        """VC with no primary_ip: get_librenms_sync_device returns None → empty row, no crash."""
        device = _make_device(pk=1, has_vc=True)
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
    def test_vc_device_with_sync_device_uses_cache(self, mock_cache, mock_sync, mock_get_obj):
        """VC with valid sync device: cache is queried with the sync device's key."""
        device = _make_device(pk=1, has_vc=True)
        sync_device = _make_device(pk=2, name="sync-device")
        mock_get_obj.return_value = device
        mock_sync.return_value = sync_device
        mock_cache.get.return_value = None  # No cached data

        view = self._make_view()
        request = _make_request({"device_id": 1, "local_port_id": "42"})
        view.post(request)

        mock_sync.assert_called_once_with(device)
        mock_cache.get.assert_called_once()
        cache_key = mock_cache.get.call_args[0][0]
        assert "device" in cache_key
        assert "2" in cache_key

    @patch("netbox_librenms_plugin.views.base.cables_view.get_object_or_404")
    @patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.base.cables_view.cache")
    def test_non_vc_device_skips_sync_device_lookup(self, mock_cache, mock_sync, mock_get_obj):
        """Non-VC device: get_librenms_sync_device is NOT called."""
        device = _make_device(pk=5, has_vc=False)
        mock_get_obj.return_value = device
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
        return view

    @patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404")
    @patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.object_sync.devices.cache")
    def test_vc_device_no_sync_device_returns_404(self, mock_cache, mock_sync, mock_get_obj):
        """VC with no sync device: returns 404 JSON error, no crash."""
        device = _make_device(pk=1, has_vc=True)
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
    def test_vc_device_with_sync_device_uses_cache(self, mock_cache, mock_sync, mock_get_obj):
        """VC with valid sync device: cache is queried with the sync device's key."""
        device = _make_device(pk=1, has_vc=True)
        sync_device = _make_device(pk=3, name="sync-member")
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

        mock_sync.assert_called_once_with(device)
        mock_cache.get.assert_called_once()
        cache_key = mock_cache.get.call_args[0][0]
        assert "3" in cache_key

    @patch("netbox_librenms_plugin.views.object_sync.devices.get_object_or_404")
    @patch("netbox_librenms_plugin.views.object_sync.devices.get_librenms_sync_device")
    @patch("netbox_librenms_plugin.views.object_sync.devices.cache")
    def test_non_vc_device_skips_sync_device_lookup(self, mock_cache, mock_sync, mock_get_obj):
        """Non-VC device: get_librenms_sync_device is NOT called."""
        device = _make_device(pk=5, has_vc=False)
        mock_get_obj.return_value = device
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
