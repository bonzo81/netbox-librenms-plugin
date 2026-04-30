"""Coverage tests for views/sync/device_fields.py (target >95%)."""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_view(ViewClass):
    """Create a view instance bypassing __init__, with a mock LibreNMS API."""
    view = object.__new__(ViewClass)
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    view.require_all_permissions = MagicMock(return_value=None)
    return view


def _make_request(post_data=None):
    req = MagicMock()
    req.POST = post_data or {}
    return req


# ---------------------------------------------------------------------------
# UpdateDeviceNameView
# ---------------------------------------------------------------------------


class TestUpdateDeviceNameView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView

        return _make_view(UpdateDeviceNameView)

    def test_permission_denied_returns_error(self):
        view = self._view()
        error_response = MagicMock()
        view.require_all_permissions = MagicMock(return_value=error_response)

        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404") as mock_get:
            result = view.post(_make_request(), pk=1)

        assert result is error_response
        mock_get.assert_not_called()

    def test_no_librenms_id_returns_error(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None

        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir,
        ):
            view.post(_make_request(), pk=1)

        mock_msg.error.assert_called_once()
        mock_redir.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (False, None)

        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)

        mock_msg.error.assert_called_once()

    def test_get_device_info_empty_dict(self):
        """An empty (falsy) device_info dict triggers the 'Failed to retrieve' error path."""
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {})

        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)

        # empty dict is falsy → triggers "Failed to retrieve device info" error
        mock_msg.error.assert_called_once()

    def test_no_sysname_returns_warning(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"sysName": None, "hostname": None})

        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.resolve_naming_preferences", return_value=(True, False)
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)

        mock_msg.warning.assert_called_once()

    def test_save_success(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"sysName": "router1"})

        mock_device = MagicMock()
        mock_device.name = "old-name"
        mock_device.virtual_chassis = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.resolve_naming_preferences",
                return_value=(True, False),
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields._determine_device_name",
                return_value="router1",
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir,
        ):
            view.post(_make_request(), pk=1)

        mock_device.full_clean.assert_called_once()
        mock_device.save.assert_called_once()
        assert mock_device.name == "router1"
        mock_msg.success.assert_called_once()
        mock_redir.assert_called_once()

    def test_save_validation_error_with_message_dict(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"sysName": "router1"})

        mock_device = MagicMock()
        mock_device.name = "old-name"
        mock_device.virtual_chassis = None
        exc = ValidationError({"name": ["duplicate"]})
        mock_device.full_clean.side_effect = exc

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.resolve_naming_preferences",
                return_value=(True, False),
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields._determine_device_name",
                return_value="router1",
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)

        mock_msg.error.assert_called_once()
        # Name should be restored
        assert mock_device.name == "old-name"

    def test_save_integrity_error_without_message_dict(self):
        from django.db import IntegrityError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_device_info.return_value = (True, {"sysName": "router1"})

        mock_device = MagicMock()
        mock_device.name = "old-name"
        mock_device.virtual_chassis = None
        mock_device.full_clean.side_effect = IntegrityError("duplicate key")

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.resolve_naming_preferences",
                return_value=(True, False),
            ),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields._determine_device_name",
                return_value="router1",
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)

        mock_msg.error.assert_called_once()


# ---------------------------------------------------------------------------
# UpdateDeviceSerialView
# ---------------------------------------------------------------------------


class TestUpdateDeviceSerialView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView

        return _make_view(UpdateDeviceSerialView)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)

        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404") as mock_get:
            result = view.post(_make_request(), pk=1)
        assert result is err
        mock_get.assert_not_called()

    def test_no_librenms_id(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (False, None)

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_serial_is_none(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": None})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_serial_is_dash(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "-"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_save_success_with_old_serial(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN001"})

        mock_device = MagicMock()
        mock_device.serial = "OLDSERIAL"
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.success.assert_called_once()
        assert "OLDSERIAL" in mock_msg.success.call_args[0][1]
        assert mock_device.serial == "SN001"
        mock_device.full_clean.assert_called_once()
        mock_device.save.assert_called_once()

    def test_save_success_no_old_serial(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN001"})

        mock_device = MagicMock()
        mock_device.serial = ""  # No old serial
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.success.assert_called_once()
        assert "set to" in mock_msg.success.call_args[0][1]
        assert mock_device.serial == "SN001"
        mock_device.full_clean.assert_called_once()
        mock_device.save.assert_called_once()

    def test_save_validation_error_with_message_dict(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN001"})

        mock_device = MagicMock()
        mock_device.serial = "OLD"
        mock_device.full_clean.side_effect = ValidationError({"serial": ["err"]})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()
        assert mock_device.serial == "OLD"

    def test_save_integrity_error(self):
        from django.db import IntegrityError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 5
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN001"})

        mock_device = MagicMock()
        mock_device.serial = "OLD"
        mock_device.full_clean.side_effect = IntegrityError("dup")
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()


# ---------------------------------------------------------------------------
# UpdateDeviceTypeView
# ---------------------------------------------------------------------------


class TestUpdateDeviceTypeView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceTypeView

        return _make_view(UpdateDeviceTypeView)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404") as mock_get:
            result = view.post(_make_request(), pk=1)
        assert result is err
        mock_get.assert_not_called()

    def test_no_librenms_id(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (False, None)
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_no_hardware(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": None})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_no_match_result(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": False},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_match_none_returns_ambiguous_error(self):
        """match_librenms_hardware_to_device_type returns None → ambiguous-match error path."""
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        mock_device = MagicMock()
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value=None,
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()
        assert "Ambiguous" in mock_msg.error.call_args[0][1]
        mock_device.full_clean.assert_not_called()
        mock_device.save.assert_not_called()

    def test_save_success(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        mock_dt = MagicMock()
        mock_device = MagicMock()
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": mock_dt},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_device.full_clean.assert_called_once()
        mock_device.save.assert_called_once()
        assert mock_device.device_type is mock_dt
        mock_msg.success.assert_called_once()

    def test_save_validation_error_with_message_dict(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        mock_dt = MagicMock()
        mock_device = MagicMock()
        mock_device.full_clean.side_effect = ValidationError({"device_type": ["err"]})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": mock_dt},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_save_integrity_error(self):
        from django.db import IntegrityError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 7
        view._librenms_api.get_device_info.return_value = (True, {"hardware": "Cisco 3750"})
        mock_dt = MagicMock()
        mock_device = MagicMock()
        mock_device.full_clean.side_effect = IntegrityError("dup")
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": mock_dt},
            ),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()


# ---------------------------------------------------------------------------
# UpdateDevicePlatformView
# ---------------------------------------------------------------------------


class TestUpdateDevicePlatformView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDevicePlatformView

        return _make_view(UpdateDevicePlatformView)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(_make_request(), pk=1)
        assert result is err

    def test_no_librenms_id(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (False, None)
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_no_os(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": None})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.warning.assert_called_once()

    def test_platform_does_not_exist(self):

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})

        mock_platform_cls = MagicMock()
        mock_platform_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_platform_cls.objects.get.side_effect = mock_platform_cls.DoesNotExist()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_save_success_with_old_platform(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})

        mock_platform = MagicMock()
        mock_platform_cls = MagicMock()
        mock_platform_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_platform_cls.objects.get.return_value = mock_platform

        mock_device = MagicMock()
        mock_device.platform = MagicMock()  # old platform exists

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.success.assert_called_once()
        assert "updated from" in mock_msg.success.call_args[0][1]
        assert mock_device.platform is mock_platform
        mock_device.full_clean.assert_called_once()
        mock_device.save.assert_called_once()

    def test_save_success_no_old_platform(self):
        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})

        mock_platform = MagicMock()
        mock_platform_cls = MagicMock()
        mock_platform_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_platform_cls.objects.get.return_value = mock_platform

        mock_device = MagicMock()
        mock_device.platform = None  # no old platform

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.success.assert_called_once()
        assert "set to" in mock_msg.success.call_args[0][1]
        assert mock_device.platform is mock_platform
        mock_device.full_clean.assert_called_once()
        mock_device.save.assert_called_once()

    def test_save_validation_error(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        view._librenms_api.get_librenms_id.return_value = 3
        view._librenms_api.get_device_info.return_value = (True, {"os": "ios"})

        mock_platform = MagicMock()
        mock_platform_cls = MagicMock()
        mock_platform_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_platform_cls.objects.get.return_value = mock_platform

        mock_device = MagicMock()
        mock_device.platform = None
        mock_device.full_clean.side_effect = ValidationError({"platform": ["err"]})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()


# ---------------------------------------------------------------------------
# CreateAndAssignPlatformView
# ---------------------------------------------------------------------------


class TestCreateAndAssignPlatformView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        return _make_view(CreateAndAssignPlatformView)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(_make_request(), pk=1)
        assert result is err

    def test_no_platform_name(self):
        view = self._view()
        req = _make_request({"platform_name": ""})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        assert "required" in mock_msg.error.call_args[0][1].lower()

    def test_platform_already_exists(self):
        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.exists.return_value = True

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.warning.assert_called_once()

    def test_manufacturer_not_found(self):
        """manufacturer_id provided but Manufacturer.DoesNotExist: manufacturer stays None."""
        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": "99"})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.exists.return_value = False
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance

        mock_manuf_cls = MagicMock()
        mock_manuf_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_manuf_cls.objects.get.side_effect = mock_manuf_cls.DoesNotExist()

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Manufacturer", mock_manuf_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        # Should succeed (manufacturer silently ignored)
        mock_msg.success.assert_called_once()
        assert mock_locked.platform == mock_platform_instance
        mock_locked.save.assert_called_once()

    def test_success_no_manufacturer(self):
        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.exists.return_value = False
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.success.assert_called_once()
        assert mock_locked.platform == mock_platform_instance
        mock_locked.save.assert_called_once()

    def test_platform_constructor_includes_slug(self):
        """Platform must be constructed with slug=slugify(name) — regression for #279."""
        from django.utils.text import slugify

        view = self._view()
        platform_name = "Cisco IOS-XE 17.x"
        req = _make_request({"platform_name": platform_name, "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.exists.return_value = False
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)

        mock_platform_cls.assert_called_once_with(
            name=platform_name,
            slug=slugify(platform_name),
            manufacturer=None,
        )

    def test_platform_validation_error(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.exists.return_value = False
        mock_platform_instance = MagicMock()
        mock_platform_instance.full_clean.side_effect = ValidationError({"name": ["err"]})
        mock_platform_cls.return_value = mock_platform_instance

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_device_does_not_exist_inside_transaction(self):
        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.exists.return_value = False
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.side_effect = DoesNotExist()

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_device_validation_error(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.exists.return_value = False
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.full_clean.side_effect = ValidationError({"platform": ["err"]})

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_integrity_error(self):
        from django.db import IntegrityError

        view = self._view()
        req = _make_request({"platform_name": "ios", "manufacturer": ""})

        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.exists.return_value = False
        mock_platform_instance = MagicMock()
        # Make save raise IntegrityError
        mock_platform_instance.save.side_effect = IntegrityError("duplicate")
        mock_platform_cls.return_value = mock_platform_instance

        # transaction.atomic().__exit__ must return False so IntegrityError propagates
        mock_atomic_cm = MagicMock()
        mock_atomic_cm.__enter__ = MagicMock(return_value=None)
        mock_atomic_cm.__exit__ = MagicMock(return_value=False)
        mock_txn = MagicMock()
        mock_txn.atomic.return_value = mock_atomic_cm
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def _success_patches(self, platform_name="ios", librenms_os="ios", create_mapping="1"):
        """Return (view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, mock_locked)."""
        view = self._view()
        req = _make_request(
            {
                "platform_name": platform_name,
                "manufacturer": "",
                "librenms_os": librenms_os,
                "create_mapping": create_mapping,
            }
        )
        mock_platform_cls = MagicMock()
        mock_platform_cls.objects.filter.return_value.exists.return_value = False
        mock_platform_instance = MagicMock()
        mock_platform_cls.return_value = mock_platform_instance
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked
        return view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, mock_locked

    def test_mapping_created_when_name_differs(self):
        """A PlatformMapping is created when name differs from librenms_os and checkbox is on."""
        view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, _ = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1"
        )
        mock_mapping_cls = MagicMock()
        mock_mapping_instance = MagicMock()
        mock_mapping_cls.return_value = mock_mapping_instance
        mock_mapping_cls.objects.filter.return_value.first.return_value = None

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", mock_mapping_cls),
        ):
            view.post(req, pk=1)

        mock_mapping_cls.assert_called_once_with(librenms_os="ios", netbox_platform=mock_platform_instance)
        mock_mapping_instance.full_clean.assert_called_once()
        mock_mapping_instance.save.assert_called_once()
        success_msg = mock_msg.success.call_args[0][1]
        assert "platform mapping" in success_msg

    def test_mapping_skipped_when_checkbox_off(self):
        """No PlatformMapping is created when checkbox is unchecked."""
        view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, _ = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping=""
        )
        mock_mapping_cls = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", mock_mapping_cls),
        ):
            view.post(req, pk=1)

        mock_mapping_cls.assert_not_called()

    def test_mapping_skipped_when_already_exists(self):
        """No duplicate PlatformMapping is created when one already exists for the OS."""
        view, req, mock_platform_cls, mock_platform_instance, mock_device_cls, _ = self._success_patches(
            platform_name="Cisco IOS", librenms_os="ios", create_mapping="1"
        )
        mock_mapping_cls = MagicMock()
        mock_mapping_cls.objects.filter.return_value.first.return_value = MagicMock()  # existing mapping

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform", mock_platform_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages"),
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("netbox_librenms_plugin.views.sync.device_fields.PlatformMapping", mock_mapping_cls),
        ):
            view.post(req, pk=1)

        mock_mapping_cls.assert_not_called()


# ---------------------------------------------------------------------------
# AssignVCSerialView
# ---------------------------------------------------------------------------


class TestAssignVCSerialView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import AssignVCSerialView

        return _make_view(AssignVCSerialView)

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(_make_request(), pk=1)
        assert result is err

    def test_not_virtual_chassis(self):
        view = self._view()
        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request(), pk=1)
        mock_msg.error.assert_called_once()

    def test_no_serial_assignments_no_errors(self):
        """Loop doesn't execute — no serial_N keys in POST."""
        view = self._view()
        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({}), pk=1)
        mock_msg.info.assert_called_once()

    def test_member_id_missing(self):
        """member_id_{N} key is absent → counter incremented, no assignment."""
        view = self._view()
        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()
        # serial_1 exists but member_id_1 is empty
        req = _make_request({"serial_1": "SN100", "member_id_1": ""})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.info.assert_called_once()

    def test_member_not_found(self):
        view = self._view()
        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock(pk=10)

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.get.side_effect = DoesNotExist()

        req = _make_request({"serial_1": "SN100", "member_id_1": "99"})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        # Should call error for the missing device
        mock_msg.error.assert_called()

    def test_member_different_chassis(self):
        view = self._view()
        vc = MagicMock(pk=10)
        mock_device = MagicMock()
        mock_device.virtual_chassis = vc

        member = MagicMock()
        member.name = "sw-member"
        member.virtual_chassis = MagicMock(pk=99)  # different VC!

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.get.return_value = member

        req = _make_request({"serial_1": "SN100", "member_id_1": "5"})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called()

    def test_member_save_validation_error(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        vc = MagicMock(pk=10)
        mock_device = MagicMock()
        mock_device.virtual_chassis = vc

        member = MagicMock()
        member.name = "sw-member"
        member.virtual_chassis = vc  # same VC
        member.serial = "OLD"
        member.full_clean.side_effect = ValidationError({"serial": ["err"]})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.get.return_value = member

        req = _make_request({"serial_1": "SN100", "member_id_1": "5"})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called()

    def test_member_save_success(self):
        view = self._view()
        vc = MagicMock(pk=10)
        mock_device = MagicMock()
        mock_device.virtual_chassis = vc

        member = MagicMock()
        member.name = "sw-member"
        member.virtual_chassis = vc
        member.serial = "OLD"
        member.save = MagicMock()

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.get.return_value = member

        req = _make_request({"serial_1": "SN100", "member_id_1": "5"})
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.success.assert_called_once()
        assert member.serial == "SN100"
        member.save.assert_called_once()

    def test_assignments_and_errors_both_reported(self):
        """One success + one error → both messages emitted."""
        from django.core.exceptions import ValidationError

        view = self._view()
        vc = MagicMock(pk=10)
        mock_device = MagicMock()
        mock_device.virtual_chassis = vc

        good_member = MagicMock()
        good_member.name = "sw1"
        good_member.virtual_chassis = vc
        good_member.serial = ""

        bad_member = MagicMock()
        bad_member.name = "sw2"
        bad_member.virtual_chassis = vc
        bad_member.serial = ""
        bad_member.full_clean.side_effect = ValidationError({"serial": ["dup"]})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.get.side_effect = [good_member, bad_member]

        req = _make_request(
            {
                "serial_1": "SN001",
                "member_id_1": "1",
                "serial_2": "SN002",
                "member_id_2": "2",
            }
        )
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.success.assert_called()
        mock_msg.error.assert_called()
        assert good_member.serial == "SN001"
        good_member.save.assert_called_once()


# ---------------------------------------------------------------------------
# RemoveServerMappingView — helper methods
# ---------------------------------------------------------------------------


class TestRemoveServerMappingViewHelpers:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        view = object.__new__(RemoveServerMappingView)
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_get_object_device(self):
        view = self._view()
        mock_device = MagicMock()

        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device):
            obj, model = view._get_object("device", 1)
        assert obj is mock_device

    def test_get_object_vm(self):
        view = self._view()
        mock_vm = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_vm):
            obj, model = view._get_object("vm", 1)
        assert obj is mock_vm

    def test_sync_url_name_device(self):
        view = self._view()
        assert view._sync_url_name("device") == "plugins:netbox_librenms_plugin:device_librenms_sync"

    def test_sync_url_name_vm(self):
        view = self._view()
        assert view._sync_url_name("vm") == "plugins:netbox_librenms_plugin:vm_librenms_sync"

    def test_normalize_bool(self):
        view = self._view()
        assert view._normalize_librenms_mapping(True) == {}
        assert view._normalize_librenms_mapping(False) == {}

    def test_normalize_int(self):
        view = self._view()
        assert view._normalize_librenms_mapping(42) == {"default": 42}

    def test_normalize_string_digit(self):
        view = self._view()
        assert view._normalize_librenms_mapping("99") == {"default": 99}

    def test_normalize_dict(self):
        view = self._view()
        d = {"server1": 10}
        assert view._normalize_librenms_mapping(d) == d

    def test_normalize_non_digit_string_returns_empty(self):
        view = self._view()
        assert view._normalize_librenms_mapping("not-a-number") == {}

    def test_normalize_none_returns_empty(self):
        view = self._view()
        assert view._normalize_librenms_mapping(None) == {}


# ---------------------------------------------------------------------------
# RemoveServerMappingView — post()
# ---------------------------------------------------------------------------


class TestRemoveServerMappingViewPost:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        view = object.__new__(RemoveServerMappingView)
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_invalid_object_type_returns_400(self):
        view = self._view()
        req = _make_request({"object_type": "badtype"})
        result = view.post(req, pk=1)
        assert result.status_code == 400

    def test_virtualmachine_object_type_normalized_to_vm(self):
        """object_type='virtualmachine' is normalised to 'vm'."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "virtualmachine", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_vm_cls = MagicMock()
        mock_vm_cls.DoesNotExist = DoesNotExist
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5}}
        mock_vm_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.VirtualMachine", mock_vm_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.success.assert_called_once()

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        req = _make_request({"object_type": "device", "server_key": "x"})
        result = view.post(req, pk=1)
        assert result is err

    def test_no_server_key(self):
        view = self._view()
        req = _make_request({"object_type": "device", "server_key": ""})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()

    def test_mapping_not_found_wrong_type(self):
        """cf_value is not a dict → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": None}

        req = _make_request({"object_type": "device", "server_key": "default"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.warning.assert_called_once()

    def test_mapping_not_found_missing_key(self):
        """server_key not in cf_value dict → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"other": 5}}

        req = _make_request({"object_type": "device", "server_key": "default"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(req, pk=1)
        mock_msg.warning.assert_called_once()

    def test_configured_servers_non_dict_treated_as_empty(self):
        """servers config is a list (non-dict) → treated as empty dict, orphan key can be removed."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5}}

        mock_device_cls = MagicMock()
        mock_device_cls.__name__ = "Device"
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        # servers is a list (non-dict) → line 496 normalises it to {}
        mock_cfg = {"netbox_librenms_plugin": {"servers": ["not", "a", "dict"], "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.success.assert_called_once()

    def test_configured_server_key_in_servers_dict(self):
        """server_key is in configured servers → error."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"production": 10}}

        req = _make_request({"object_type": "device", "server_key": "production"})

        mock_cfg = {"netbox_librenms_plugin": {"servers": {"production": {}}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        assert "Cannot remove" in mock_msg.error.call_args[0][1]

    def test_legacy_default_server_protected(self):
        """Legacy mode with librenms_url set and server_key='default' → error."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"default": 7}}

        req = _make_request({"object_type": "device", "server_key": "default"})

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": "https://librenms.example.com"}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()

    def test_object_no_longer_exists_inside_transaction(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.__name__ = "Device"
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.side_effect = DoesNotExist()

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()

    def test_mapping_already_removed_in_lock(self):
        """server_key is gone from the locked object's cf → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        # Key was removed between the first read and the lock
        mock_locked.custom_field_data = {"librenms_id": {}}

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.warning.assert_called_once()

    def test_validation_error_on_save(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5}}
        mock_locked.full_clean.side_effect = ValidationError({"librenms_id": ["err"]})

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_unexpected_error_on_save(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5}}
        mock_locked.full_clean.side_effect = RuntimeError("disk full")

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_success_removes_mapping(self):
        """Happy path: mapping removed, last entry → cf set to None."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5}}

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.success.assert_called_once()
        mock_locked.full_clean.assert_called_once()
        mock_locked.save.assert_called_once()
        # After deleting the last key, cf should be set to None
        assert mock_locked.custom_field_data["librenms_id"] is None

    def test_success_keeps_remaining_mappings(self):
        """Happy path: mapping removed, other entries remain → cf retains them."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"orphan": 5, "other": 6}}

        req = _make_request({"object_type": "device", "server_key": "orphan"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"orphan": 5, "other": 6}}

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_cfg = {"netbox_librenms_plugin": {"servers": {}, "librenms_url": ""}}
        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.PLUGINS_CONFIG = mock_cfg
            view.post(req, pk=1)
        mock_msg.success.assert_called_once()
        mock_locked.full_clean.assert_called_once()
        mock_locked.save.assert_called_once()
        assert mock_locked.custom_field_data["librenms_id"] == {"other": 6}


# ---------------------------------------------------------------------------
# ConvertLegacyLibreNMSIdView — helper methods
# ---------------------------------------------------------------------------


class TestConvertLegacyLibreNMSIdViewHelpers:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        view = object.__new__(ConvertLegacyLibreNMSIdView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_get_model_and_object_device(self):
        view = self._view()
        mock_device = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_device):
            model, obj = view._get_model_and_object("device", 1)
        assert obj is mock_device

    def test_get_model_and_object_vm(self):
        view = self._view()
        mock_vm = MagicMock()
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_vm):
            model, obj = view._get_model_and_object("vm", 1)
        assert obj is mock_vm

    def test_sync_url_device(self):
        view = self._view()
        with patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir:
            view._sync_url("device", 1)
        mock_redir.assert_called_once_with("plugins:netbox_librenms_plugin:device_librenms_sync", pk=1)

    def test_sync_url_vm(self):
        view = self._view()
        with patch("netbox_librenms_plugin.views.sync.device_fields.redirect") as mock_redir:
            view._sync_url("vm", 1)
        mock_redir.assert_called_once_with("plugins:netbox_librenms_plugin:vm_librenms_sync", pk=1)


# ---------------------------------------------------------------------------
# ConvertLegacyLibreNMSIdView — post()
# ---------------------------------------------------------------------------


class TestConvertLegacyLibreNMSIdViewPost:
    def _view(self):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        view = object.__new__(ConvertLegacyLibreNMSIdView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_invalid_object_type_returns_400(self):
        view = self._view()
        req = _make_request({"object_type": "badtype"})
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(req, pk=1)
        assert result.status_code == 400

    def test_virtualmachine_object_type_normalised(self):
        """object_type='virtualmachine' is accepted as 'vm'."""
        view = self._view()
        # Provide a legacy string int as cf_value
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": "42"}
        mock_obj.serial = "SN-MATCH"

        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": "42"}
        mock_locked.serial = "SN-MATCH"

        mock_vm_cls = MagicMock()
        mock_vm_cls.DoesNotExist = DoesNotExist
        mock_vm_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.VirtualMachine", mock_vm_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True
            ) as mock_migrate,
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "virtualmachine"}), pk=1)
        mock_msg.success.assert_called_once()
        mock_migrate.assert_called_once()
        mock_locked.full_clean.assert_called_once()
        mock_locked.save.assert_called_once()

    def test_permission_denied(self):
        view = self._view()
        err = MagicMock()
        view.require_all_permissions = MagicMock(return_value=err)
        req = _make_request({"object_type": "device"})
        with patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"):
            result = view.post(req, pk=1)
        assert result is err

    def test_already_json_format_dict(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": {"default": 5}}

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.warning.assert_called_once()
        assert "already" in mock_msg.warning.call_args[0][1].lower()

    def test_already_json_format_bool(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": True}

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_non_digit_string_cf_value(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": "not-a-number"}

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_get_device_info_failure(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        view._librenms_api.get_device_info.return_value = (False, None)

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_serial_mismatch_empty_netbox_serial(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = ""
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-ABC"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()
        assert "Serial" in mock_msg.error.call_args[0][1]

    def test_serial_mismatch_different(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-XYZ"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-ABC"})

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_object_no_longer_exists_in_lock(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_device_cls = MagicMock()
        mock_device_cls.__name__ = "Device"
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.side_effect = DoesNotExist()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_cf_value_changed_to_json_after_lock(self):
        """Locked row shows cf_value already as dict → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": {"default": 42}}  # already dict

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.warning.assert_called_once()

    def test_cf_value_not_int_after_lock(self):
        """Locked row shows non-digit string → error: cannot convert."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": "not-a-digit"}

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_data_changed_before_lock(self):
        """locked_id or locked_serial differs → error: aborting."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.custom_field_data = {"librenms_id": 99}  # different id
        mock_locked.serial = "SN-MATCH"

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()

    def test_conflict_with_another_object(self):
        """Another object already has the same librenms_id for this server."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"

        other_obj = MagicMock()
        other_obj.pk = 99  # different pk → conflict

        mock_device_cls = MagicMock()
        mock_device_cls.__name__ = "Device"
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=other_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_migrate_returns_false(self):
        """migrate_legacy_librenms_id returns False → warning."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=False),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.warning.assert_called_once()

    def test_validation_error_on_save(self):
        from django.core.exceptions import ValidationError

        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"
        mock_locked.full_clean.side_effect = ValidationError({"librenms_id": ["err"]})

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_unexpected_error_on_save(self):
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"
        mock_locked.full_clean.side_effect = RuntimeError("disk full")

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        mock_txn = MagicMock()
        mock_txn.set_rollback = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction", mock_txn),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.error.assert_called_once()
        mock_txn.set_rollback.assert_called_once_with(True)

    def test_success_integer_cf_value(self):
        """Happy path with integer cf_value → success message."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.success.assert_called_once()
        mock_locked.full_clean.assert_called_once()
        mock_locked.save.assert_called_once()
        assert "42" in mock_msg.success.call_args[0][1]

    def test_success_string_cf_value(self):
        """Happy path with string digit cf_value → success message."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": "42"}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": "42"}
        mock_locked.serial = "SN-MATCH"

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.success.assert_called_once()
        mock_locked.full_clean.assert_called_once()
        mock_locked.save.assert_called_once()

    def test_conflict_same_object_is_not_conflict(self):
        """find_by_librenms_id returns the same object → no conflict, proceeds."""
        view = self._view()
        mock_obj = MagicMock()
        mock_obj.custom_field_data = {"librenms_id": 42}
        mock_obj.serial = "SN-MATCH"
        view._librenms_api.get_device_info.return_value = (True, {"serial": "SN-MATCH"})
        view._librenms_api.server_key = "default"

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_locked = MagicMock()
        mock_locked.pk = 1
        mock_locked.custom_field_data = {"librenms_id": 42}
        mock_locked.serial = "SN-MATCH"

        # find_by_librenms_id returns the SAME object → match.pk == locked.pk → no conflict
        same_obj = MagicMock()
        same_obj.pk = 1

        mock_device_cls = MagicMock()
        mock_device_cls.DoesNotExist = DoesNotExist
        mock_device_cls.objects.select_for_update.return_value.get.return_value = mock_locked

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404", return_value=mock_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.Device", mock_device_cls),
            patch("netbox_librenms_plugin.views.sync.device_fields.find_by_librenms_id", return_value=same_obj),
            patch("netbox_librenms_plugin.views.sync.device_fields.migrate_legacy_librenms_id", return_value=True),
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            view.post(_make_request({"object_type": "device"}), pk=1)
        mock_msg.success.assert_called_once()
        mock_locked.full_clean.assert_called_once()
        mock_locked.save.assert_called_once()


# ---------------------------------------------------------------------------
# Wiring assertions — ensure views keep required mixins and permissions
# ---------------------------------------------------------------------------


class TestDeviceFieldsViewWiring:
    """Structural checks: views must retain required mixins and permissions."""

    def test_convert_legacy_id_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        assert issubclass(ConvertLegacyLibreNMSIdView, LibreNMSAPIMixin)

    def test_convert_legacy_id_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import ConvertLegacyLibreNMSIdView

        assert "POST" in ConvertLegacyLibreNMSIdView.required_object_permissions

    def test_remove_server_mapping_has_required_object_permissions(self):
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        assert "POST" in RemoveServerMappingView.required_object_permissions
