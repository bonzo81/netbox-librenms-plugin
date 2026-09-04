"""Integration tests for device and VM synchronization actions."""

from copy import deepcopy

import pytest
from django.contrib import messages
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser, make_vm
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms


SERVER_KEY = "default"


def _point_plugin_at(settings, url):
    """Configure the loopback server without changing unrelated plugin settings."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        SERVER_KEY: {
            "display_name": "Synchronization test server",
            "librenms_url": url,
            "api_token": "sync-test-token",
            "cache_timeout": 300,
            "verify_ssl": False,
        }
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def librenms_server(settings, monkeypatch):
    """Run the real HTTP boundary against a controlled loopback server."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    cache.delete("librenms_poller_group_choices_default")
    with librenms_mock_server() as server:
        _point_plugin_at(settings, server.url)
        server.register(
            "/api/v0/poller_group",
            {"status": "ok", "get_poller_group": []},
        )
        yield server
    cache.delete("librenms_poller_group_choices_default")


def _messages(response, level=None):
    wanted = None if level is None else getattr(messages, level.upper())
    return [
        str(message) for message in get_messages(response.wsgi_request) if wanted is None or message.level == wanted
    ]


def _add_url(obj):
    return reverse("plugins:netbox_librenms_plugin:add_device_to_librenms", args=[obj.pk])


def _v2_payload(obj, **overrides):
    data = {
        "object_type": obj._meta.model_name,
        "v1v2-snmp_version": "v2c",
        "v1v2-hostname": "router.example.test",
        "v1v2-community": "test-community",
    }
    data.update(overrides)
    return data


def _v3_payload(obj, **overrides):
    data = {
        "object_type": obj._meta.model_name,
        "v3-snmp_version": "v3",
        "v3-hostname": "router-v3.example.test",
        "v3-authlevel": "authPriv",
        "v3-authname": "snmp-user",
        "v3-authpass": "test-auth-password",
        "v3-authalgo": "SHA",
        "v3-cryptopass": "test-crypto-password",
        "v3-cryptoalgo": "AES",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestAddDeviceToLibreNMSView:
    def test_v2c_device_submission_sends_the_validated_payload(self, client, librenms_server):
        from dcim.models import Device

        device = make_device("add-v2c-device")
        user = make_user_with_perms("add-v2c-writer", [("change", Device)])
        received = []

        def add_device(**request):
            received.append(request)
            return 200, {"status": "ok"}

        librenms_server.register("/api/v0/devices", add_device, method="POST")
        client.force_login(user)

        response = client.post(
            _add_url(device),
            _v2_payload(
                device,
                **{
                    "v1v2-port": "1161",
                    "v1v2-transport": "udp6",
                    "v1v2-port_association_mode": "ifName",
                    "v1v2-force_add": "on",
                },
            ),
        )

        assert response.status_code == 302
        assert response.url == device.get_absolute_url()
        assert [request["body"] for request in received] == [
            {
                "hostname": "router.example.test",
                "snmpver": "v2c",
                "force_add": True,
                "port": 1161,
                "transport": "udp6",
                "port_association_mode": "ifName",
                "community": "test-community",
            }
        ]
        assert _messages(response, "success") == ["Device added successfully."]

    def test_v3_vm_submission_resolves_the_vm_and_sends_auth_fields(self, client, librenms_server):
        from virtualization.models import VirtualMachine

        vm = make_vm("add-v3-vm")
        user = make_user_with_perms("add-v3-vm-writer", [("change", VirtualMachine)])
        received = []

        def add_device(**request):
            received.append(request)
            return 200, {"status": "ok"}

        librenms_server.register("/api/v0/devices", add_device, method="POST")
        client.force_login(user)

        response = client.post(_add_url(vm), _v3_payload(vm))

        assert response.status_code == 302
        assert response.url == vm.get_absolute_url()
        assert [request["body"] for request in received] == [
            {
                "hostname": "router-v3.example.test",
                "snmpver": "v3",
                "force_add": False,
                "authlevel": "authPriv",
                "authname": "snmp-user",
                "authpass": "test-auth-password",
                "authalgo": "SHA",
                "cryptopass": "test-crypto-password",
                "cryptoalgo": "AES",
            }
        ]

    def test_librenms_failure_is_reported_to_the_user(self, client, librenms_server):
        from dcim.models import Device

        device = make_device("add-device-api-failure")
        user = make_user_with_perms("add-device-api-failure-writer", [("change", Device)])
        librenms_server.register(
            "/api/v0/devices",
            {"status": "error", "message": "SNMP discovery failed"},
            method="POST",
        )
        client.force_login(user)

        response = client.post(_add_url(device), _v2_payload(device))

        assert response.status_code == 302
        assert _messages(response, "error") == ["SNMP discovery failed"]

    def test_invalid_form_reports_real_field_errors_without_an_add_request(self, client, librenms_server):
        from dcim.models import Device

        device = make_device("add-device-invalid-form")
        user = make_user_with_perms("add-device-invalid-form-writer", [("change", Device)])
        received = []

        def add_device(**request):
            received.append(request)
            return 200, {"status": "ok"}

        librenms_server.register("/api/v0/devices", add_device, method="POST")
        client.force_login(user)

        response = client.post(
            _add_url(device),
            {"object_type": "device", "v1v2-snmp_version": "v2c"},
        )

        assert response.status_code == 302
        assert received == []
        errors = _messages(response, "error")
        assert any(message.startswith("hostname:") for message in errors)
        assert any(message.startswith("community:") for message in errors)

    def test_invalid_object_type_is_rejected_before_object_or_api_access(self, client, librenms_server):
        device = make_device("add-device-invalid-type")
        client.force_login(make_superuser("add-device-invalid-type-user"))

        response = client.post(
            _add_url(device),
            {"object_type": "rack<script>", "v1v2-snmp_version": "v2c"},
        )

        assert response.status_code == 400
        assert b"rack&lt;script&gt;" in response.content

    def test_user_without_device_change_permission_cannot_submit(self, client, librenms_server):
        device = make_device("add-device-permission-denied")
        user = make_user_with_perms("add-device-permission-denied-user", [])
        received = []

        def add_device(**request):
            received.append(request)
            return 200, {"status": "ok"}

        librenms_server.register("/api/v0/devices", add_device, method="POST")
        client.force_login(user)

        response = client.post(
            _add_url(device),
            _v2_payload(device),
            HTTP_REFERER=device.get_absolute_url(),
        )

        assert response.status_code == 302
        assert response.url == device.get_absolute_url()
        assert received == []
        assert _messages(response, "error") == ["Missing permissions: dcim.change_device"]


@pytest.mark.django_db
class TestUpdateDeviceLocationView:
    def test_location_update_sends_the_netbox_site_to_the_active_server(self, client, librenms_server):
        device_id = 42
        device = make_device("location-update-target", librenms_cf={SERVER_KEY: device_id})
        received = []

        def record_update(**request):
            received.append(request)
            return 200, {"status": "ok"}

        librenms_server.register(f"/api/v0/devices/{device_id}", record_update, method="PATCH")
        client.force_login(make_superuser("location-update-writer"))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:update_device_location", args=[device.pk]),
            {"server_key": SERVER_KEY},
        )

        assert response.status_code == 302
        assert response.url == (
            f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[device.pk])}"
            f"?server_key={SERVER_KEY}"
        )
        assert [(request["method"], request["path"], request["body"]) for request in received] == [
            (
                "PATCH",
                f"/api/v0/devices/{device_id}",
                {"field": ["location", "override_sysLocation"], "data": ["TestSite", "1"]},
            )
        ]
        assert _messages(response, "success") == ["Device location updated in LibreNMS to TestSite"]

    def test_unlinked_device_does_not_patch_a_missing_id(self, client, librenms_server):
        """An unlinked device must stop before the LibreNMS update request."""
        device = make_device("location-update-unlinked", librenms_cf={SERVER_KEY: None})
        client.force_login(make_superuser("location-update-unlinked-writer"))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:update_device_location", args=[device.pk]),
            {"server_key": SERVER_KEY},
        )

        assert response.status_code == 302
        assert response.url == (
            f"{reverse('plugins:netbox_librenms_plugin:device_librenms_sync', args=[device.pk])}"
            f"?server_key={SERVER_KEY}"
        )
        assert _messages(response, "error") == ["Device not found in LibreNMS"]
        assert all("/devices/None" not in request["path"] for request in librenms_server.requests)
        assert all(request["method"] != "PATCH" for request in librenms_server.requests)


@pytest.mark.django_db
class TestRemoveServerMappingView:
    def test_vm_writer_removes_an_orphaned_mapping_and_returns_to_vm_sync(self, client):
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
        assert _messages(response, "success") == ["Removed LibreNMS mapping for server 'orphaned-server'."]
        vm.refresh_from_db()
        assert vm.custom_field_data["librenms_id"] is None

    def test_mapping_removal_preserves_unrelated_legacy_device_state(self, client):
        from dcim.models import Device

        device = make_device("remove-orphaned-device-mapping", librenms_cf={"orphaned-server": 42})
        Device.objects.filter(pk=device.pk).update(face="front")
        user = make_user_with_perms("remove-orphaned-device-writer", [("change", Device)])
        client.force_login(user)

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:remove_server_mapping", args=[device.pk]),
            {"object_type": "device", "server_key": "orphaned-server"},
        )

        assert response.status_code == 302
        assert _messages(response, "success") == ["Removed LibreNMS mapping for server 'orphaned-server'."]
        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"] is None
        assert device.face == "front"
