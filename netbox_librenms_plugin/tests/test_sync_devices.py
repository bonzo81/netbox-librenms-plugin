"""Tests for device sync views: AddDeviceToLibreNMSView and field update views."""

from unittest.mock import MagicMock, patch


def _make_view(cls_name, module_path="netbox_librenms_plugin.views.sync.devices"):
    import importlib

    mod = importlib.import_module(module_path)
    cls = getattr(mod, cls_name)
    view = object.__new__(cls)
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = "default"
    view.request = MagicMock()
    return view


def _make_field_view(cls_name):
    return _make_view(cls_name, "netbox_librenms_plugin.views.sync.device_fields")


class TestAddDeviceToLibreNMSViewWiring:
    """AddDeviceToLibreNMSView must be correctly wired to LibreNMSAPIMixin."""

    def test_has_librenms_api_mixin(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        assert LibreNMSAPIMixin in AddDeviceToLibreNMSView.__mro__

    def test_has_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        assert LibreNMSPermissionMixin in AddDeviceToLibreNMSView.__mro__


class TestAddDeviceToLibreNMSViewFormValid:
    """form_valid() builds correct device_data payload and calls librenms_api.add_device."""

    def _make_view(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        view = object.__new__(AddDeviceToLibreNMSView)
        view._librenms_api = MagicMock()
        view.request = MagicMock()
        view.object = MagicMock()
        view.object.get_absolute_url.return_value = "/dcim/devices/1/"
        return view

    def _make_form(self, data):
        form = MagicMock()
        form.cleaned_data = data
        return form

    def test_v2c_form_includes_community(self):
        view = self._make_view()
        view._librenms_api.add_device.return_value = (True, "Device added")
        form = self._make_form(
            {
                "hostname": "switch1.example.com",
                "community": "public",
                "force_add": False,
            }
        )

        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages"):
                view.form_valid(form, snmp_version="v2c")

        call_args = view._librenms_api.add_device.call_args[0][0]
        assert call_args["snmp_version"] == "v2c"
        assert call_args["community"] == "public"
        assert call_args["hostname"] == "switch1.example.com"

    def test_v3_form_includes_auth_fields(self):
        view = self._make_view()
        view._librenms_api.add_device.return_value = (True, "Device added")
        form = self._make_form(
            {
                "hostname": "switch2.example.com",
                "authlevel": "authPriv",
                "authname": "admin",
                "authpass": "secret",
                "authalgo": "SHA",
                "cryptopass": "crypt",
                "cryptoalgo": "AES",
                "force_add": False,
            }
        )

        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages"):
                view.form_valid(form, snmp_version="v3")

        call_args = view._librenms_api.add_device.call_args[0][0]
        assert call_args["snmp_version"] == "v3"
        assert call_args["authlevel"] == "authPriv"
        assert "community" not in call_args

    def test_api_failure_adds_error_message(self):
        view = self._make_view()
        view._librenms_api.add_device.return_value = (False, "Connection refused")

        form = self._make_form(
            {
                "hostname": "fail.example.com",
                "community": "public",
                "force_add": False,
            }
        )

        with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
            with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                view.form_valid(form, snmp_version="v2c")

        mock_msg.error.assert_called_once()


class TestUpdateDeviceLocationView:
    """UpdateDeviceLocationView.post calls update_device_field with site name."""

    def test_calls_update_device_field_with_site(self):
        """post() resolves the NetBox site name and passes the exact API payload
        expected by LibreNMS's PATCH /api/v0/devices/{id}/field endpoint:
        ``{"field": ["location", "override_sysLocation"], "data": [name, "1"]}``.
        """
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        view = object.__new__(UpdateDeviceLocationView)
        view._librenms_api = MagicMock()
        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.update_device_field.return_value = (True, "ok")
        view.request = MagicMock()

        device = MagicMock()
        device.site = MagicMock()
        device.site.name = "London"
        device.get_absolute_url.return_value = "/dcim/devices/1/"

        with patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=device):
            with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                    view.post(view.request, pk=1)

        view._librenms_api.update_device_field.assert_called_once_with(
            42,
            {"field": ["location", "override_sysLocation"], "data": ["London", "1"]},
        )
        mock_msg.success.assert_called_once()

    def test_warning_when_no_site(self):
        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        view = object.__new__(UpdateDeviceLocationView)
        view._librenms_api = MagicMock()
        view._librenms_api.get_librenms_id.return_value = 42
        view.request = MagicMock()

        device = MagicMock()
        device.site = None
        device.pk = 1

        with patch("netbox_librenms_plugin.views.sync.devices.get_object_or_404", return_value=device):
            with patch("netbox_librenms_plugin.views.sync.devices.redirect"):
                with patch("netbox_librenms_plugin.views.sync.devices.messages") as mock_msg:
                    view.post(view.request, pk=1)

        view._librenms_api.update_device_field.assert_not_called()
        mock_msg.warning.assert_called_once()


class TestUpdateDeviceNameViewWiring:
    def test_has_all_required_mixins(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView
        from netbox_librenms_plugin.views.mixins import (
            LibreNMSAPIMixin,
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
        )

        mro = UpdateDeviceNameView.__mro__
        assert LibreNMSAPIMixin in mro
        assert LibreNMSPermissionMixin in mro
        assert NetBoxObjectPermissionMixin in mro

    def test_requires_change_device_permission(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceNameView
        from dcim.models import Device

        perms = UpdateDeviceNameView.required_object_permissions
        assert "POST" in perms
        assert any(action == "change" and model == Device for action, model in perms["POST"])


class TestUpdateDeviceSerialViewWiring:
    def test_has_all_required_mixins(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView
        from netbox_librenms_plugin.views.mixins import (
            LibreNMSAPIMixin,
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
        )

        assert LibreNMSAPIMixin in UpdateDeviceSerialView.__mro__
        assert LibreNMSPermissionMixin in UpdateDeviceSerialView.__mro__
        assert NetBoxObjectPermissionMixin in UpdateDeviceSerialView.__mro__

    def test_requires_change_device_permission(self):
        from netbox_librenms_plugin.views.sync.device_fields import UpdateDeviceSerialView
        from dcim.models import Device

        perms = UpdateDeviceSerialView.required_object_permissions
        assert "POST" in perms
        assert any(action == "change" and model == Device for action, model in perms["POST"])


class TestCreatePlatformFullClean:
    """CreateAndAssignPlatformView must call full_clean() so ValidationError is catchable."""

    def test_validation_error_caught_on_slug_collision(self):
        """When full_clean raises ValidationError, user sees error message instead of 500."""
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        view = object.__new__(CreateAndAssignPlatformView)

        request = MagicMock()
        request.method = "POST"
        request.POST = {"platform_name": "test-platform"}
        request.user.has_perm.return_value = True
        view.request = request

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"),
            patch("netbox_librenms_plugin.views.sync.device_fields.Manufacturer"),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform") as MockPlatform,
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            MockPlatform.objects.filter.return_value.exists.return_value = False
            platform_instance = MagicMock()
            platform_instance.full_clean.side_effect = ValidationError({"slug": ["Slug already exists"]})
            MockPlatform.return_value = platform_instance

            view.post(request, pk=1)

            platform_instance.full_clean.assert_called_once()
            platform_instance.save.assert_not_called()
            mock_messages.error.assert_called_once()
            error_msg = mock_messages.error.call_args[0][1]
            assert "could not be created" in error_msg
            assert "Slug already exists" in error_msg
