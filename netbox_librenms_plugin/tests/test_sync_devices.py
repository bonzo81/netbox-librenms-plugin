"""Tests for device sync views: AddDeviceToLibreNMSView and field update views."""

from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device, make_vm, make_superuser
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms


@pytest.fixture
def librenms_server(monkeypatch):
    """Run the real HTTP boundary against a controlled loopback LibreNMS server."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        yield server


def _point_plugin_at(settings, url):
    """Configure the loopback server without changing unrelated plugin settings."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "default": {"librenms_url": url, "api_token": "test-token", "verify_ssl": False}
    }
    settings.PLUGINS_CONFIG = plugin_config
    return "default"


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
    """A location update crosses the real request, permission, ORM, and HTTP boundaries."""

    @pytest.mark.django_db
    def test_a_location_update_sends_the_netbox_site_to_the_active_server(
        self,
        client,
        librenms_server,
        settings,
    ):
        server_key = _point_plugin_at(settings, librenms_server.url)
        device_id = 42
        device = make_device("location-update-target", librenms_cf={server_key: device_id})
        received_requests = []

        def record_update(**request):
            received_requests.append(request)
            return 200, {"status": "ok"}

        librenms_server.register(f"/api/v0/devices/{device_id}", record_update, method="PATCH")
        client.force_login(make_superuser("location-update-writer"))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:update_device_location", args=[device.pk]),
            {"server_key": server_key},
        )

        assert response.status_code == 302
        assert response.url == (
            f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[device.pk])}"
            f"?server_key={server_key}"
        )
        assert [(request["method"], request["path"], request["body"]) for request in received_requests] == [
            (
                "PATCH",
                f"/api/v0/devices/{device_id}",
                {"field": ["location", "override_sysLocation"], "data": ["TestSite", "1"]},
            )
        ]
        assert "Device location updated in LibreNMS to TestSite" in [
            str(message) for message in get_messages(response.wsgi_request)
        ]


class TestAddDeviceObjectResolution:
    """Regression tests for AddDeviceToLibreNMSView.get_object()."""

    def test_get_object_resolves_a_virtualmachine_through_the_restricted_queryset(self):
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView
        from virtualization.models import VirtualMachine

        view = object.__new__(AddDeviceToLibreNMSView)
        vm_obj = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=vm_obj,
        ) as mock_get_obj:
            result = view.get_object(123, object_type="virtualmachine")

        assert result is vm_obj
        mock_get_obj.assert_called_once_with(VirtualMachine, "change", pk=123)


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


class TestRemoveServerMappingView:
    @pytest.mark.django_db
    def test_a_vm_writer_can_remove_an_orphaned_mapping_and_returns_to_the_vm_sync_page(self, client):
        from virtualization.models import VirtualMachine

        vm = make_vm("remove-orphaned-vm-mapping")
        vm.custom_field_data["librenms_id"] = {"orphaned-server": 42}
        vm.save(update_fields=["custom_field_data"])
        user = make_user_with_perms("remove-orphaned-vm-writer", [("change", VirtualMachine)])
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[vm.pk]),
            {"object_type": "virtualmachine", "server_key": "orphaned-server"},
        )

        assert response.status_code == 302
        assert response.url == reverse("plugins:netbox_librenms_plugin:vm_librenms_sync", args=[vm.pk])
        assert "Removed LibreNMS mapping for server 'orphaned-server'." in [
            str(message) for message in get_messages(response.wsgi_request)
        ]
        vm.refresh_from_db()
        assert vm.custom_field_data["librenms_id"] is None
