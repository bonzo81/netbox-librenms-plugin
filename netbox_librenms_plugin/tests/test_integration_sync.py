"""
Integration tests using the mock LibreNMS HTTP server.

These tests verify that LibreNMSAPI correctly parses responses from a real
(but local, mocked) HTTP server, and that the full request/response cycle works.
No Django database access is used; NetBox model interactions are mocked.
"""

import json
import pytest

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


@pytest.fixture
def mock_server():
    with librenms_mock_server() as server:
        yield server


def _make_api(url, token="test-token"):
    """Create a LibreNMSAPI instance pointed at the mock server."""
    from unittest.mock import patch

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    servers_config = {
        "test": {
            "librenms_url": url,
            "api_token": token,
            "cache_timeout": 0,
            "verify_ssl": False,
        }
    }

    with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:
        mock_cfg.side_effect = lambda _plugin, key: servers_config if key == "servers" else None
        api = LibreNMSAPI(server_key="test")
    assert api.server_key == "test"
    return api


class TestMockServerSanity:
    """The mock server itself must start, serve, and stop cleanly."""

    def test_server_starts_and_responds(self, mock_server):
        import urllib.request

        mock_server.register("/api/v0/test", {"status": "ok"})
        with urllib.request.urlopen(f"{mock_server.url}/api/v0/test") as resp:
            data = json.loads(resp.read())
        assert data["status"] == "ok"

    def test_404_for_unregistered_path(self, mock_server):
        import urllib.request
        from urllib.error import HTTPError

        try:
            urllib.request.urlopen(f"{mock_server.url}/api/v0/nonexistent")
        except HTTPError as e:
            assert e.code == 404
        else:
            pytest.fail("Expected 404 HTTPError")


class TestLibreNMSAPIPortsFetch:
    """LibreNMSAPI.get_ports() correctly parses mock server responses."""

    def test_get_ports_returns_dict_with_ports_key(self, mock_server):
        """
        get_ports() returns a parsed dict and sends the required query parameters.

        A callable route is used so we can capture the outgoing query string and
        assert that both the ``columns`` field list and ``with=vlans`` are present —
        if either is ever dropped, the sync page will silently lose data.
        """
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

        mock_server.routes["/api/v0/devices/1/ports"] = _route
        api = _make_api(mock_server.url)

        success, data = api.get_ports(1)

        assert success is True
        assert isinstance(data, dict)
        assert "ports" in data
        assert data["ports"][0]["ifName"] == "GigabitEthernet0/1"
        assert "columns" in captured_query, "get_ports() must send a 'columns' query param"
        assert "vlans" in captured_query.get("with", []), "get_ports() must request 'with=vlans'"

    def test_get_ports_returns_false_on_auth_error(self, mock_server):
        mock_server.auth_error_response(path="/api/v0/devices/1/ports")
        api = _make_api(mock_server.url)

        success, _ = api.get_ports(1)

        assert success is False

    def test_get_ports_empty_list_when_no_ports(self, mock_server):
        mock_server.register("/api/v0/devices/99/ports", {"status": "ok", "ports": []})
        api = _make_api(mock_server.url)

        success, data = api.get_ports(99)

        assert success is True
        assert data["ports"] == []


class TestLibreNMSAPIDeviceInfo:
    """LibreNMSAPI.get_device_info() correctly parses device details."""

    def test_returns_device_info_dict(self, mock_server):
        mock_server.device_info_response(device_id=5, hostname="rtr01", hardware="ISR4351")
        api = _make_api(mock_server.url)

        success, info = api.get_device_info(5)

        assert success is True
        assert isinstance(info, dict)
        assert info["hostname"] == "rtr01"

    def test_returns_false_on_404(self, mock_server):
        # /api/v0/devices/999 not registered → 404
        api = _make_api(mock_server.url)

        success, info = api.get_device_info(999)

        assert success is False
        assert info is None


class TestLibreNMSAPIAddDevice:
    """LibreNMSAPI.add_device() posts correctly and interprets the response."""

    def test_add_device_success(self, mock_server):
        mock_server.add_device_response(device_id=10)
        api = _make_api(mock_server.url)

        success, message = api.add_device(
            {
                "hostname": "switch1.example.com",
                "snmp_version": "v2c",
                "community": "public",
                "force_add": False,
            }
        )

        assert success is True

    def test_add_device_failure_on_server_error(self, mock_server):
        mock_server.register("/api/v0/devices", {"status": "error", "message": "duplicate"}, status=500)
        api = _make_api(mock_server.url)

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

    def test_returns_inventory_list(self, mock_server):
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
        mock_server.register("/api/v0/inventory/7/all", {"status": "ok", "inventory": inventory})
        api = _make_api(mock_server.url)

        success, data = api.get_device_inventory(7)

        assert success is True
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["entPhysicalClass"] == "chassis"
        assert data[1]["entPhysicalModelName"] == "WS-X4748-RJ45V+E"

    def test_returns_empty_list_when_no_inventory(self, mock_server):
        mock_server.register("/api/v0/inventory/99/all", {"status": "ok", "inventory": []})
        api = _make_api(mock_server.url)

        success, data = api.get_device_inventory(99)

        assert success is True
        assert data == []

    def test_returns_false_on_network_error(self, mock_server):
        # Unregistered path → 404 → raise_for_status → RequestException
        api = _make_api(mock_server.url)

        success, _ = api.get_device_inventory(404)

        assert success is False

    def test_inventory_items_preserve_all_fields(self, mock_server):
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
        mock_server.register("/api/v0/inventory/3/all", {"status": "ok", "inventory": inventory})
        api = _make_api(mock_server.url)

        success, data = api.get_device_inventory(3)

        assert success is True
        item = data[0]
        assert item["entPhysicalParentRelPos"] == 1
        assert item["entPhysicalSerialNum"] == "JAE123XYZ"


class TestLibreNMSAPIDiscovery:
    """
    LibreNMSAPI device-ID discovery: lookup by IP and hostname fallback.

    Covers get_device_id_by_ip(), get_device_id_by_hostname(), and the
    get_librenms_id() fallback chain (IP → DNS name → hostname).
    """

    _DEVICE_RESPONSE = {
        "status": "ok",
        "devices": [{"device_id": 42, "hostname": "sw01.example.com"}],
    }

    def test_get_device_id_by_ip_returns_id(self, mock_server):
        mock_server.register("/api/v0/devices/10.0.0.1", self._DEVICE_RESPONSE)
        api = _make_api(mock_server.url)

        device_id = api.get_device_id_by_ip("10.0.0.1")

        assert device_id == 42

    def test_get_device_id_by_ip_returns_none_on_404(self, mock_server):
        # no route registered → 404
        api = _make_api(mock_server.url)

        device_id = api.get_device_id_by_ip("10.0.0.2")

        assert device_id is None

    def test_get_device_id_by_hostname_returns_id(self, mock_server):
        mock_server.register("/api/v0/devices/sw01.example.com", self._DEVICE_RESPONSE)
        api = _make_api(mock_server.url)

        device_id = api.get_device_id_by_hostname("sw01.example.com")

        assert device_id == 42

    def test_get_device_id_by_hostname_returns_none_on_404(self, mock_server):
        api = _make_api(mock_server.url)

        device_id = api.get_device_id_by_hostname("unknown.example.com")

        assert device_id is None

    def test_get_librenms_id_resolves_by_ip(self, mock_server):
        """get_librenms_id() resolves via IP when the device has a primary_ip."""
        from unittest.mock import MagicMock, patch

        mock_server.register("/api/v0/devices/10.0.0.10", self._DEVICE_RESPONSE)
        api = _make_api(mock_server.url)

        obj = MagicMock()
        obj.cf = {}  # no stored ID
        obj.primary_ip.address.ip = "10.0.0.10"
        obj.primary_ip.dns_name = None
        obj.name = "sw01"

        with patch.object(api, "_get_cache_key", return_value="test-key"):
            with patch("netbox_librenms_plugin.librenms_api.cache") as mock_cache:
                mock_cache.get.return_value = None
                with patch.object(api, "_store_librenms_id"):
                    result = api.get_librenms_id(obj)

        assert result == 42

    def test_get_librenms_id_falls_back_to_hostname_when_ip_fails(self, mock_server):
        """get_librenms_id() falls back to hostname when IP lookup returns no result."""
        from unittest.mock import MagicMock, patch

        # IP path returns 404 (unregistered) → fallback to hostname
        mock_server.register("/api/v0/devices/sw01.example.com", self._DEVICE_RESPONSE)
        api = _make_api(mock_server.url)

        obj = MagicMock()
        obj.cf = {}  # no stored ID
        obj.primary_ip.address.ip = "192.0.2.1"  # unregistered → 404 → None
        obj.primary_ip.dns_name = None
        obj.name = "sw01.example.com"

        with patch.object(api, "_get_cache_key", return_value="test-key"):
            with patch("netbox_librenms_plugin.librenms_api.cache") as mock_cache:
                mock_cache.get.return_value = None
                with patch.object(api, "_store_librenms_id"):
                    result = api.get_librenms_id(obj)

        assert result == 42
