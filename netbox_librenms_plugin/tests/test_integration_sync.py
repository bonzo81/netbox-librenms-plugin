"""
Integration tests using the mock LibreNMS HTTP server.

These tests verify that LibreNMSAPI correctly parses responses from a real
(but local) HTTP server, and that the full request/response cycle works.
"""

import json
from copy import deepcopy

import pytest
from django.conf import settings
from django.test import override_settings


def _make_api(url, token="test-token"):
    """Create a LibreNMSAPI instance pointed at the mock server."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    servers_config = {
        "test": {
            "librenms_url": url,
            "api_token": token,
            "cache_timeout": 1,
            "verify_ssl": False,
        }
    }
    plugin_config["netbox_librenms_plugin"]["servers"] = servers_config
    with override_settings(PLUGINS_CONFIG=plugin_config):
        api = LibreNMSAPI(server_key="test")
    assert api.server_key == "test"
    return api


def test_integration_api_uses_a_short_nonzero_cache_timeout(librenms_server):
    """Integration requests must exercise cache reads without retaining state between tests."""
    assert _make_api(librenms_server.url).cache_timeout == 1


class TestMockServerSanity:
    """The mock server itself must start, serve, and stop cleanly."""

    def test_server_starts_and_responds(self, librenms_server):
        import urllib.request

        librenms_server.register("/api/v0/test", {"status": "ok"})
        with urllib.request.urlopen(f"{librenms_server.url}/api/v0/test") as resp:
            data = json.loads(resp.read())
        assert data["status"] == "ok"

    def test_404_for_unregistered_path(self, librenms_server):
        import urllib.request
        from urllib.error import HTTPError

        try:
            urllib.request.urlopen(f"{librenms_server.url}/api/v0/nonexistent")
        except HTTPError as e:
            assert e.code == 404
        else:
            pytest.fail("Expected 404 HTTPError")


class TestLibreNMSAPIPortsFetch:
    """LibreNMSAPI.get_ports() correctly parses mock server responses."""

    def test_get_ports_returns_dict_with_ports_key(self, librenms_server):
        """Verify that get_ports returns parsed ports and sends both columns and with=vlans query parameters."""
        captured_query: dict = {}
        ports_body = {
            "status": "ok",
            "ports": [
                {
                    "port_id": 101,
                    "ifName": "GigabitEthernet0/1",
                    "ifDescr": "GigabitEthernet0/1",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifAdminStatus": "up",
                    "ifAlias": "uplink",
                    "ifPhysAddress": "aa:bb:cc:dd:ee:01",
                    "ifMtu": 1500,
                    "ifVlan": 1,
                    "ifTrunk": 0,
                }
            ],
        }

        def _route(method, path, query, headers, body):
            # query is already parsed by the mock server (dict of lists)
            captured_query.update(query)
            return 200, ports_body

        librenms_server.routes["/api/v0/devices/1/ports"] = _route
        api = _make_api(librenms_server.url)

        success, data = api.get_ports(1)

        assert success is True
        assert isinstance(data, dict)
        assert "ports" in data
        assert data["ports"][0]["ifName"] == "GigabitEthernet0/1"
        assert "columns" in captured_query, "get_ports() must send a 'columns' query param"
        assert "vlans" in captured_query.get("with", []), "get_ports() must request 'with=vlans'"

    def test_get_ports_returns_false_on_auth_error(self, librenms_server):
        librenms_server.auth_error_response(path="/api/v0/devices/1/ports")
        api = _make_api(librenms_server.url)

        success, _ = api.get_ports(1)

        assert success is False

    def test_get_ports_empty_list_when_no_ports(self, librenms_server):
        librenms_server.register("/api/v0/devices/99/ports", {"status": "ok", "ports": []})
        api = _make_api(librenms_server.url)

        success, data = api.get_ports(99)

        assert success is True
        assert data["ports"] == []


class TestLibreNMSAPIPortStack:
    """LibreNMS port-stack responses resolve through the HTTP client."""

    def test_documented_port_stack_shape_resolves_lag_membership(self, librenms_server):
        librenms_server.register(
            "/api/v0/devices/42/ports",
            {
                "status": "ok",
                "ports": [
                    {"port_id": "101", "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
                    {"port_id": "102", "ifName": "Port-Channel1", "ifType": "ieee8023adLag"},
                ],
            },
        )
        librenms_server.register(
            "/api/v0/devices/42/port_stack",
            {
                "status": "ok",
                "message": "",
                "count": 1,
                "mappings": [
                    {
                        "device_id": "42",
                        "high_port_id": "101",
                        "low_port_id": "102",
                        "ifStackStatus": "active",
                    }
                ],
            },
        )
        api = _make_api(librenms_server.url)

        ports_ok, ports_data = api.get_ports(42)
        stack_ok, port_stack = api.get_port_stack(42)
        # Assert the call outcomes before using the payloads: a failed call would otherwise raise
        # an opaque TypeError/KeyError below instead of naming the API call that failed.
        assert ports_ok is True
        assert stack_ok is True

        relationships = api.resolve_port_relationships(ports_data["ports"], port_stack, lag_patterns={})

        assert relationships["lag_members"] == {101: 102}


class TestLibreNMSAPIDeviceInfo:
    """LibreNMSAPI.get_device_info() correctly parses device details."""

    def test_returns_device_info_dict(self, librenms_server):
        librenms_server.device_info_response(device_id=5, hostname="rtr01", hardware="ISR4351")
        api = _make_api(librenms_server.url)

        success, info = api.get_device_info(5)

        assert success is True
        assert isinstance(info, dict)
        assert info["hostname"] == "rtr01"

    def test_returns_false_on_404(self, librenms_server):
        # /api/v0/devices/999 not registered → 404
        api = _make_api(librenms_server.url)

        success, info = api.get_device_info(999)

        assert success is False
        assert info is None


class TestLibreNMSAPIAddDevice:
    """LibreNMSAPI.add_device() posts correctly and interprets the response."""

    def test_add_device_success(self, librenms_server):
        librenms_server.add_device_response(device_id=10)
        api = _make_api(librenms_server.url)

        success, message = api.add_device(
            {
                "hostname": "switch1.example.com",
                "snmp_version": "v2c",
                "community": "public",
                "force_add": False,
            }
        )

        assert success is True

    def test_add_device_failure_on_server_error(self, librenms_server):
        librenms_server.register("/api/v0/devices", {"status": "error", "message": "duplicate"}, status=500)
        api = _make_api(librenms_server.url)

        success, message = api.add_device(
            {
                "hostname": "dup.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        assert success is False


class TestLibreNMSAPIInventory:
    """LibreNMSAPI.get_device_inventory() correctly parses mock server responses."""

    def test_returns_inventory_list(self, librenms_server):
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalDescr": "Chassis",
                "entPhysicalClass": "chassis",
                "entPhysicalSerialNum": "SN-CHASSIS-001",
                "entPhysicalModelName": "WS-C4900M",
                "entPhysicalName": "Chassis 1",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalDescr": "Linecard",
                "entPhysicalClass": "module",
                "entPhysicalSerialNum": "SN-CARD-002",
                "entPhysicalModelName": "WS-X4748-RJ45V+E",
                "entPhysicalName": "Slot 1",
                "entPhysicalContainedIn": 1,
            },
        ]
        librenms_server.register("/api/v0/inventory/7/all", {"status": "ok", "inventory": inventory})
        api = _make_api(librenms_server.url)

        success, data = api.get_device_inventory(7)

        assert success is True
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["entPhysicalClass"] == "chassis"
        assert data[1]["entPhysicalModelName"] == "WS-X4748-RJ45V+E"

    def test_returns_empty_list_when_no_inventory(self, librenms_server):
        librenms_server.register("/api/v0/inventory/99/all", {"status": "ok", "inventory": []})
        api = _make_api(librenms_server.url)

        success, data = api.get_device_inventory(99)

        assert success is True
        assert data == []

    def test_returns_false_on_network_error(self, librenms_server):
        # Unregistered path → 404 → raise_for_status → RequestException
        api = _make_api(librenms_server.url)

        success, _ = api.get_device_inventory(404)

        assert success is False

    def test_inventory_items_preserve_all_fields(self, librenms_server):
        inventory = [
            {
                "entPhysicalIndex": 5,
                "entPhysicalDescr": "10 Gigabit Ethernet Module",
                "entPhysicalClass": "module",
                "entPhysicalSerialNum": "JAE123XYZ",
                "entPhysicalModelName": "X2-10GB-LR",
                "entPhysicalName": "TenGigabitEthernet1/1",
                "entPhysicalContainedIn": 1,
                "entPhysicalParentRelPos": 1,
            }
        ]
        librenms_server.register("/api/v0/inventory/3/all", {"status": "ok", "inventory": inventory})
        api = _make_api(librenms_server.url)

        success, data = api.get_device_inventory(3)

        assert success is True
        item = data[0]
        assert item["entPhysicalParentRelPos"] == 1
        assert item["entPhysicalSerialNum"] == "JAE123XYZ"


class TestLibreNMSAPIDiscovery:
    """Verify device discovery by IP, DNS name, and hostname fallback."""

    _DEVICE_RESPONSE = {
        "status": "ok",
        "devices": [{"device_id": 42, "hostname": "sw01.example.com"}],
    }

    def test_get_device_id_by_ip_returns_id(self, librenms_server):
        librenms_server.register("/api/v0/devices/10.0.0.1", self._DEVICE_RESPONSE)
        api = _make_api(librenms_server.url)

        device_id = api.get_device_id_by_ip("10.0.0.1")

        assert device_id == 42

    def test_get_device_id_by_ip_returns_none_on_404(self, librenms_server):
        # no route registered → 404
        api = _make_api(librenms_server.url)

        device_id = api.get_device_id_by_ip("10.0.0.2")

        assert device_id is None

    def test_get_device_id_by_hostname_returns_id(self, librenms_server):
        librenms_server.register("/api/v0/devices/sw01.example.com", self._DEVICE_RESPONSE)
        api = _make_api(librenms_server.url)

        device_id = api.get_device_id_by_hostname("sw01.example.com")

        assert device_id == 42

    def test_get_device_id_by_hostname_returns_none_on_404(self, librenms_server):
        api = _make_api(librenms_server.url)

        device_id = api.get_device_id_by_hostname("unknown.example.com")

        assert device_id is None

    @pytest.mark.django_db
    def test_get_librenms_id_resolves_by_ip(self, librenms_server):
        """get_librenms_id() resolves via IP when the device has a primary_ip."""
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device

        librenms_server.register("/api/v0/devices/198.18.0.10", self._DEVICE_RESPONSE)
        api = _make_api(librenms_server.url)
        device = make_device("integration-discovery-ip")
        device.primary_ip4 = ip_on(device, "198.18.0.10/24", "Ethernet1")
        device.save(update_fields=["primary_ip4"])

        result = api.get_librenms_id(device)

        assert result == 42

    @pytest.mark.django_db
    def test_get_librenms_id_falls_back_to_hostname_when_ip_fails(self, librenms_server):
        """get_librenms_id() falls back to hostname when IP lookup returns no result."""
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device

        # IP path returns 404 (unregistered) → fallback to hostname
        librenms_server.register("/api/v0/devices/sw01.example.test", self._DEVICE_RESPONSE)
        api = _make_api(librenms_server.url)
        device = make_device("sw01.example.test")
        device.primary_ip4 = ip_on(device, "198.18.0.11/24", "Ethernet1")
        device.save(update_fields=["primary_ip4"])

        result = api.get_librenms_id(device)

        assert result == 42


class TestLibreNMSAPIErrorResponses:
    """Integration tests: API client handles unusual server responses gracefully."""

    def test_401_returns_false(self, librenms_server):
        """HTTP 401 Unauthorized must return (False, ...) not raise."""
        api = _make_api(librenms_server.url)
        librenms_server.register("/api/v0/devices", {"message": "Unauthorized"}, status=401)
        success, data = api.list_devices()
        assert success is False

    def test_500_returns_false(self, librenms_server):
        """HTTP 500 must return (False, ...) not raise."""
        api = _make_api(librenms_server.url)
        librenms_server.register("/api/v0/devices", {"message": "Internal error"}, status=500)
        success, data = api.list_devices()
        assert success is False

    def test_null_inventory_returns_false(self, librenms_server):
        """{"status":"ok","inventory":null} must not raise, must return (False, error_string)."""
        api = _make_api(librenms_server.url)
        librenms_server.register("/api/v0/inventory/1/all", {"status": "ok", "inventory": None})
        success, data = api.get_device_inventory(1)
        assert success is False
        assert isinstance(data, str) and data, "expected a non-empty error string describing the malformed inventory"

    def test_null_devices_field_get_device_info(self, librenms_server):
        """{"devices": null} in get_device_info must return (False, None) not crash."""
        api = _make_api(librenms_server.url)
        librenms_server.register("/api/v0/devices/42", {"devices": None})
        success, data = api.get_device_info(42)
        assert success is False
        assert data is None

    def test_empty_devices_list_get_device_info(self, librenms_server):
        """{"devices": []} in get_device_info must return (False, None)."""
        api = _make_api(librenms_server.url)
        librenms_server.register("/api/v0/devices/42", {"devices": []})
        success, data = api.get_device_info(42)
        assert success is False
        assert data is None

    def test_404_get_device_info(self, librenms_server):
        """404 on device endpoint must return (False, None)."""
        api = _make_api(librenms_server.url)
        librenms_server.register("/api/v0/devices/99", {"message": "Device not found"}, status=404)
        success, data = api.get_device_info(99)
        assert success is False
        assert data is None
