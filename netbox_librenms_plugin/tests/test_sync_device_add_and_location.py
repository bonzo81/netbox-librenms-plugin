"""
Branch coverage for ``views/sync/devices.py``: object type, poller group, SNMP version, location.

The primary home for these two views is ``test_sync_devices.py``. These cases live in their own
file because higher branches of the PR stack grow that file's tail, so appending there conflicts
on every restack.
"""

import pytest
from django.contrib import messages
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, make_device, make_superuser
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms


SERVER_KEY = "default"


@pytest.fixture
def librenms_server(settings, monkeypatch):
    """Point the plugin at a loopback LibreNMS that answers the poller-group lookup."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        configure_librenms_servers(
            settings,
            {
                SERVER_KEY: {
                    "librenms_url": server.url,
                    "api_token": "add-device-token",
                    "cache_timeout": 300,
                    "verify_ssl": False,
                }
            },
        )
        server.register("/api/v0/poller_group", {"status": "ok", "get_poller_group": []})
        yield server


def _messages(response, level=None):
    """Read the flash messages the real view queued on the request."""
    wanted = None if level is None else getattr(messages, level.upper())
    return [
        str(message) for message in get_messages(response.wsgi_request) if wanted is None or message.level == wanted
    ]


def _record_route(server, path, method):
    """Register a recording route and return the list it appends every request to."""
    received = []

    def handler(**request):
        received.append(request)
        return 200, {"status": "ok"}

    server.register(path, handler, method=method)
    return received


def _add_url(obj):
    return reverse("plugins:netbox_librenms_plugin:add_device_to_librenms", args=[obj.pk])


def _location_url(device):
    return reverse("plugins:netbox_librenms_plugin:update_device_location", args=[device.pk])


def _v2_payload(**overrides):
    data = {
        "object_type": "device",
        "v1v2-snmp_version": "v2c",
        "v1v2-hostname": "router.example.test",
        "v1v2-community": "test-community",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestAddDeviceObjectResolution:
    """AddDeviceToLibreNMSView.get_object() only serves the two object types the URL accepts."""

    def test_unknown_object_type_resolves_to_no_object(self, librenms_server):
        """An object_type the view does not serve resolves to None instead of a Device."""
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.sync.devices import AddDeviceToLibreNMSView

        device = make_device("add-device-unknown-type")
        view = AddDeviceToLibreNMSView()
        view.setup(make_request("post", {"object_type": "rack"}, user=make_superuser("add-device-unknown-type-user")))

        assert view.get_object(device.pk, object_type="rack") is None
        assert view.get_object(device.pk, object_type=None) is None
        assert view.get_object(device.pk, object_type="device") == device


@pytest.mark.django_db
class TestAddDevicePollerGroup:
    """form_valid() forwards a numeric poller group and drops one LibreNMS reports as non-numeric."""

    def test_numeric_poller_group_is_sent_as_an_integer(self, client, librenms_server):
        """A selected poller group reaches LibreNMS as an int, not the posted string."""
        from dcim.models import Device

        device = make_device("add-device-poller-int")
        librenms_server.register(
            "/api/v0/poller_group",
            {"status": "ok", "get_poller_group": [{"id": 3, "group_name": "edge", "descr": "edge pollers"}]},
        )
        received = _record_route(librenms_server, "/api/v0/devices", "POST")
        client.force_login(make_user_with_perms("add-device-poller-int-writer", [("change", Device)]))

        response = client.post(_add_url(device), _v2_payload(**{"v1v2-poller_group": "3"}))

        assert response.status_code == 302
        assert [request["body"].get("poller_group") for request in received] == [3]
        assert _messages(response, "success") == ["Device added successfully."]

    def test_non_numeric_poller_group_is_dropped_instead_of_failing_the_add(self, client, librenms_server):
        """A poller group id LibreNMS reports as non-numeric is omitted, and the add still runs."""
        from dcim.models import Device

        device = make_device("add-device-poller-text")
        librenms_server.register(
            "/api/v0/poller_group",
            {"status": "ok", "get_poller_group": [{"id": "edge-a", "group_name": "edge"}]},
        )
        received = _record_route(librenms_server, "/api/v0/devices", "POST")
        client.force_login(make_user_with_perms("add-device-poller-text-writer", [("change", Device)]))

        response = client.post(_add_url(device), _v2_payload(**{"v1v2-poller_group": "edge-a"}))

        assert response.status_code == 302
        assert [sorted(request["body"]) for request in received] == [["community", "force_add", "hostname", "snmpver"]]
        assert _messages(response, "success") == ["Device added successfully."]


@pytest.mark.django_db
class TestAddDeviceUnknownSNMPVersion:
    """form_valid() refuses a version it cannot build credentials for."""

    def test_unknown_snmp_version_is_reported_without_calling_librenms(self, client, librenms_server):
        """A version that is neither v1/v2c nor v3 stops before the add request."""
        from dcim.models import Device

        device = make_device("add-device-unknown-snmp")
        received = _record_route(librenms_server, "/api/v0/devices", "POST")
        client.force_login(make_user_with_perms("add-device-unknown-snmp-writer", [("change", Device)]))

        response = client.post(
            _add_url(device),
            {
                "object_type": "device",
                "v3-snmp_version": "v2",
                "v3-hostname": "router-bad.example.test",
                "v3-authlevel": "noAuthNoPriv",
                "v3-authname": "snmp-user",
            },
        )

        assert response.status_code == 302
        assert response.url == device.get_absolute_url()
        assert _messages(response, "error") == ["Unknown SNMP version."]
        assert received == []


@pytest.mark.django_db
class TestUpdateDeviceLocationBranches:
    """UpdateDeviceLocationView gates on real permissions and reports a LibreNMS refusal."""

    def test_missing_device_view_permission_blocks_the_location_write(self, client, librenms_server):
        """A plugin writer without dcim.view_device never reaches the LibreNMS PATCH."""
        device = make_device("location-update-denied", librenms_cf={SERVER_KEY: 77})
        received = _record_route(librenms_server, "/api/v0/devices/77", "PATCH")
        client.force_login(make_user_with_perms("location-update-denied-user", []))

        response = client.post(
            _location_url(device),
            {"server_key": SERVER_KEY},
            HTTP_REFERER=device.get_absolute_url(),
        )

        assert response.status_code == 302
        assert received == []
        assert _messages(response, "error") == ["Missing permissions: dcim.view_device"]

    def test_librenms_refusal_is_reported_as_an_error(self, client, librenms_server):
        """A LibreNMS error body becomes the failure message, not a success toast."""
        device = make_device("location-update-failure", librenms_cf={SERVER_KEY: 88})
        librenms_server.register(
            "/api/v0/devices/88",
            {"status": "error", "message": "Location is read-only"},
            method="PATCH",
        )
        client.force_login(make_superuser("location-update-failure-user"))

        response = client.post(_location_url(device), {"server_key": SERVER_KEY})

        assert response.status_code == 302
        assert _messages(response, "error") == ["Failed to update device location in LibreNMS: Location is read-only"]
        assert _messages(response, "success") == []
