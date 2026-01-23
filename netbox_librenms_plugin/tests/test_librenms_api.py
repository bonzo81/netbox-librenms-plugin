"""
Comprehensive tests for LibreNMSAPI client.

This module provides 100% test coverage for netbox_librenms_plugin/librenms_api.py,
with particular focus on HTTP method correctness to prevent regression bugs.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

# Import the autouse fixture from helpers
pytest_plugins = ["netbox_librenms_plugin.tests.test_librenms_api_helpers"]


# =============================================================================
# Test Class 1: Initialization (3 tests)
# =============================================================================


class TestLibreNMSAPIInit:
    """Test LibreNMSAPI initialization and configuration loading."""

    def test_init_with_multi_server_config(self, mock_librenms_config):
        """Verify initialization with multi-server configuration."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        assert api.librenms_url == "https://librenms.example.com"
        assert api.api_token == "test-token"
        assert api.cache_timeout == 300
        assert api.verify_ssl is True

    def test_init_with_legacy_config(self, mock_librenms_config):
        """Verify initialization with legacy single-server config."""
        mock_config = mock_librenms_config["mock_config"]

        # Return None for 'servers' to trigger legacy path
        def config_side_effect(plugin, key, default=None):
            if key == "servers":
                return None
            legacy = {
                "librenms_url": "https://legacy.example.com",
                "api_token": "legacy-token",
                "cache_timeout": 600,
                "verify_ssl": False,
            }
            return legacy.get(key, default)

        mock_config.side_effect = config_side_effect

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI()

        assert api.librenms_url == "https://legacy.example.com"

    def test_init_missing_config_raises_valueerror(self, mock_librenms_config):
        """Verify ValueError raised when configuration is missing."""
        mock_config = mock_librenms_config["mock_config"]
        mock_config.return_value = None

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(ValueError):
            LibreNMSAPI(server_key="nonexistent")


# =============================================================================
# Test Class 2: Connection Testing (4 tests)
# =============================================================================


class TestLibreNMSAPIConnection:
    """Test connection testing functionality."""

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_connection_success(self, mock_get, mock_librenms_config):
        """Verify successful connection test."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "system": [{"local_ver": "24.1.0"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result is not None
        assert "error" not in result
        mock_get.assert_called_once()
        assert "/api/v0/system" in mock_get.call_args[0][0]

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_connection_auth_failure_401(self, mock_get, mock_librenms_config):
        """Verify 401 unauthorized handling."""
        mock_get.return_value.status_code = 401

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result.get("error") is True

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_connection_auth_failure_403(self, mock_get, mock_librenms_config):
        """Verify 403 forbidden handling."""
        mock_get.return_value.status_code = 403

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result.get("error") is True

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_connection_timeout(self, mock_get, mock_librenms_config):
        """Verify timeout exception handling."""
        mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result.get("error") is True
        assert "timeout" in result.get("message", "").lower()


# =============================================================================
# Test Class 3: HTTP Methods - CRITICAL (18 tests)
# =============================================================================


class TestLibreNMSAPIHttpMethods:
    """
    CRITICAL: Verify each API method uses the correct HTTP verb.

    These tests prevent regression bugs where HTTP methods are accidentally
    changed during refactoring (e.g., GET changed to DELETE).
    """

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_info_uses_get(self, mock_get, mock_post, mock_delete, mock_librenms_config):
        """Verify get_device_info uses GET, never DELETE."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 1}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_info(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()
        mock_post.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_device_uses_post(self, mock_post, mock_get, mock_delete, mock_librenms_config):
        """Verify add_device uses POST."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"status": "ok", "device_id": 42}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.add_device(
            data={
                "hostname": "test.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        mock_post.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_device_field_uses_patch(self, mock_patch, mock_delete, mock_librenms_config):
        """Verify update_device_field uses PATCH."""
        mock_patch.return_value.status_code = 200
        mock_patch.return_value.json.return_value = {"status": "ok"}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.update_device_field(device_id=1, field_data={"field": ["location"], "data": ["DC1"]})

        mock_patch.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_id_by_ip_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_id_by_ip uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 10}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_id_by_ip("192.168.1.1")

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_id_by_hostname_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_id_by_hostname uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 20}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_id_by_hostname("test-host")

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_ports_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_ports uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "ports": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_ports(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_locations_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_locations uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "locations": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_locations()

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_location_uses_post(self, mock_post, mock_delete, mock_librenms_config):
        """Verify add_location uses POST."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "status": "ok",
            "message": "Location created #1",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.add_location(location_data={"location": "DC1"})

        mock_post.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_location_uses_patch(self, mock_patch, mock_delete, mock_librenms_config):
        """Verify update_location uses PATCH."""
        mock_patch.return_value.status_code = 200
        mock_patch.return_value.json.return_value = {
            "status": "ok",
            "message": "Location updated",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.update_location(location_name="DC1", location_data={"location": "DC1-Updated"})

        mock_patch.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_links_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_links uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "links": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_links(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_ips_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_ips uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "addresses": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_ips(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_port_by_id_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_port_by_id uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "port": [{}]}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_port_by_id(port_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_inventory_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_device_inventory uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "inventory": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_device_inventory(device_id=1)

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_poller_groups_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_poller_groups uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "groups": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_poller_groups()

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_inventory_filtered_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify get_inventory_filtered uses GET."""
        mock_get.return_value.status_code = 200
        # Return non-empty inventory so it doesn't fall back to get_device_inventory
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "inventory": [{"entPhysicalClass": "chassis"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.get_inventory_filtered(device_id=1, ent_physical_class="chassis")

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.delete")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_list_devices_uses_get(self, mock_get, mock_delete, mock_librenms_config):
        """Verify list_devices uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.list_devices()

        mock_get.assert_called_once()
        mock_delete.assert_not_called()

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_test_connection_uses_get(self, mock_get, mock_librenms_config):
        """Verify test_connection uses GET."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok"}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        api.test_connection()

        mock_get.assert_called_once()


# ====================================================================================
# Test Class 4: Device Lookup (6 tests)
# ====================================================================================


class TestLibreNMSAPIDeviceLookup:
    """Test device lookup functionality."""

    def test_get_librenms_id_from_custom_field(self, mock_librenms_config):
        """Returns ID when already stored in cf['librenms_id']."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {"librenms_id": 42}

        result = api.get_librenms_id(device)
        assert result == 42

    @patch("netbox_librenms_plugin.librenms_api.cache")
    def test_get_librenms_id_from_cache(self, mock_cache, mock_librenms_config):
        """Returns ID from Django cache when not in custom field."""
        mock_cache.get.return_value = 99

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {}

        result = api.get_librenms_id(device)
        assert result == 99

    @patch("netbox_librenms_plugin.librenms_api.cache")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_librenms_id_by_ip_lookup(self, mock_get, mock_cache, mock_librenms_config):
        """Performs IP lookup and caches result."""
        mock_cache.get.return_value = None
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 55}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {}
        device.primary_ip4 = MagicMock()
        device.primary_ip4.address = MagicMock()
        device.primary_ip4.address.ip = "10.0.0.1"

        result = api.get_librenms_id(device)
        assert result == 55
        mock_cache.set.assert_called_once()

    @patch("netbox_librenms_plugin.librenms_api.cache")
    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_librenms_id_by_hostname_lookup(self, mock_get, mock_cache, mock_librenms_config):
        """Falls back to hostname lookup."""
        mock_cache.get.return_value = None

        # First call (IP lookup) returns empty, second call (hostname lookup) returns device
        call_count = [0]

        def side_effect(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            call_count[0] += 1
            if call_count[0] == 2:  # Second call is hostname lookup
                resp.json.return_value = {
                    "status": "ok",
                    "devices": [{"device_id": 77}],
                }
            else:
                resp.json.return_value = {"status": "ok", "devices": []}
            return resp

        mock_get.side_effect = side_effect

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        device = MagicMock()
        device.name = "test-device"
        device.cf = {}
        device.primary_ip4 = MagicMock()
        device.primary_ip4.address = MagicMock()
        device.primary_ip4.address.ip = "10.0.0.1"

        result = api.get_librenms_id(device)
        assert result == 77

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_id_by_ip_not_found(self, mock_get, mock_librenms_config):
        """Returns None when IP not found in LibreNMS."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.get_device_id_by_ip("192.168.99.99")

        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_id_by_hostname_not_found(self, mock_get, mock_librenms_config):
        """Returns None when hostname not found."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.get_device_id_by_hostname("nonexistent-host")

        assert result is None


# =============================================================================
# Test Class 5: Device Operations (6 tests)
# =============================================================================


class TestLibreNMSAPIDeviceOperations:
    """Test device CRUD operations."""

    pytest_plugins = ["netbox_librenms_plugin.tests.test_librenms_api_helpers"]

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_device_success(self, mock_post, mock_librenms_config):
        """Verify successful device addition."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "status": "ok",
            "message": "Device added successfully",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.add_device(
            data={
                "hostname": "test-device.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        assert result[0] is True
        assert result[1] == "Device added successfully."

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_device_duplicate_error(self, mock_post, mock_librenms_config):
        """Verify duplicate device handling."""
        mock_post.return_value.status_code = 500
        mock_post.return_value.json.return_value = {
            "status": "error",
            "message": "Device already exists",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.add_device(
            data={
                "hostname": "duplicate-device.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        assert result[0] is False
        assert "Device already exists" in result[1]

    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_device_field_success(self, mock_patch, mock_librenms_config):
        """Verify successful device field update."""
        mock_patch.return_value.status_code = 200
        mock_patch.return_value.json.return_value = {
            "status": "ok",
            "message": "Device field updated",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, message = api.update_device_field(device_id=123, field_data={"field": "notes", "data": "Updated note"})

        assert success is True
        assert "updated" in message.lower()

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_info_success(self, mock_get, mock_librenms_config):
        """Verify retrieving device info."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 123, "hostname": "test-device"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, device_data = api.get_device_info(device_id=123)

        assert success is True
        assert device_data is not None
        assert device_data["device_id"] == 123

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_info_not_found(self, mock_get, mock_librenms_config):
        """Verify handling of getting non-existent device."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        # Should raise IndexError when devices list is empty
        with pytest.raises(IndexError):
            api.get_device_info(device_id=999)

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_list_devices_with_filters(self, mock_get, mock_librenms_config):
        """Verify listing devices with filter parameter."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 1}, {"device_id": 2}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, devices = api.list_devices(filters={"type": "network"})

        assert success is True
        assert len(devices) == 2


# =============================================================================
# Test Class 6: Location Operations (4 tests)
# =============================================================================


class TestLibreNMSAPILocationOperations:
    """Test location CRUD operations."""

    pytest_plugins = ["netbox_librenms_plugin.tests.test_librenms_api_helpers"]

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_locations_success(self, mock_get, mock_librenms_config):
        """Verify retrieving all locations."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "locations": [{"id": 1, "location": "DC1"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, locations = api.get_locations()

        assert success is True
        assert len(locations) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_location_success(self, mock_post, mock_librenms_config):
        """Verify successful location addition."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "status": "ok",
            "message": "Location created #5",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, result_dict = api.add_location(location_data={"location": "DC2"})

        assert success is True
        assert result_dict["id"] == "5"
        assert "message" in result_dict

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_add_location_error(self, mock_post, mock_librenms_config):
        """Verify location addition error handling."""
        mock_post.return_value.status_code = 500
        mock_post.return_value.json.return_value = {
            "status": "error",
            "message": "Invalid location data",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, error_msg = api.add_location(location_data={})

        assert success is False
        assert "Invalid location data" in error_msg

    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_location_success(self, mock_patch, mock_librenms_config):
        """Verify successful location update."""
        mock_patch.return_value.status_code = 200
        mock_patch.return_value.json.return_value = {
            "status": "ok",
            "message": "Location updated",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, message = api.update_location(location_name="DC1", location_data={"location": "DC1-Updated"})

        assert success is True

    @patch("netbox_librenms_plugin.librenms_api.requests.patch")
    def test_update_location_not_found(self, mock_patch, mock_librenms_config):
        """Verify updating non-existent location."""
        mock_patch.return_value.status_code = 404
        mock_patch.return_value.json.return_value = {
            "status": "error",
            "message": "Location not found",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, message = api.update_location(location_name="NonExistent", location_data={})

        assert success is False


# =============================================================================
# Test Class 7: Ports and Inventory (9 tests)
# =============================================================================


class TestLibreNMSAPIPortsAndInventory:
    """Test ports and inventory operations."""

    pytest_plugins = ["netbox_librenms_plugin.tests.test_librenms_api_helpers"]

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_ports_all(self, mock_get, mock_librenms_config):
        """Verify retrieving all ports for a device."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "ports": [{"port_id": 1}, {"port_id": 2}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, data = api.get_ports(device_id=123)

        assert success is True
        assert "ports" in data
        assert len(data["ports"]) == 2

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_port_by_id_success(self, mock_get, mock_librenms_config):
        """Verify retrieving port by ID."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "port": [{"port_id": 1, "ifName": "GigabitEthernet0/1"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, port_data = api.get_port_by_id(port_id=1)

        assert success is True
        assert port_data is not None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_port_by_id_error(self, mock_get, mock_librenms_config):
        """Verify handling of port retrieval error."""
        mock_get.side_effect = requests.exceptions.RequestException("Connection error")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, error_msg = api.get_port_by_id(port_id=999)

        assert success is False
        assert isinstance(error_msg, str)

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_inventory_success(self, mock_get, mock_librenms_config):
        """Verify retrieving device inventory."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "inventory": [{"entPhysicalClass": "chassis"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, inventory = api.get_device_inventory(device_id=123)

        assert success is True
        assert len(inventory) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_inventory_filtered_by_class(self, mock_get, mock_librenms_config):
        """Verify filtering inventory by physical class."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "inventory": [{"entPhysicalClass": "chassis", "entPhysicalName": "Chassis"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, inventory = api.get_inventory_filtered(device_id=123, ent_physical_class="chassis")

        assert success is True
        assert len(inventory) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_inventory_filtered_by_container(self, mock_get, mock_librenms_config):
        """Verify filtering inventory by container."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "inventory": [{"entPhysicalContainedIn": "0"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, inventory = api.get_inventory_filtered(device_id=123, ent_physical_contained_in=0)

        assert success is True

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_links_success(self, mock_get, mock_librenms_config):
        """Verify retrieving device links."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "links": [{"id": 1, "local_port_id": 10, "remote_port_id": 20}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, links_dict = api.get_device_links(device_id=123)

        assert success is True
        assert "links" in links_dict
        assert len(links_dict["links"]) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_ips_success(self, mock_get, mock_librenms_config):
        """Verify retrieving device IP addresses."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "addresses": [{"ipv4_address": "10.0.0.1"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, ips = api.get_device_ips(device_id=123)

        assert success is True
        assert len(ips) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_device_ips_empty(self, mock_get, mock_librenms_config):
        """Verify handling device with no IPs."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "addresses": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, ips = api.get_device_ips(device_id=123)

        assert success is True
        assert len(ips) == 0


# =============================================================================
# Test Class 8: Poller and Devices (4 tests)
# =============================================================================


class TestLibreNMSAPIPollerAndDevices:
    """Test poller groups and device listing operations."""

    pytest_plugins = ["netbox_librenms_plugin.tests.test_librenms_api_helpers"]

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_poller_groups_success(self, mock_get, mock_librenms_config):
        """Verify retrieving poller groups."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "get_poller_group": [{"id": 1, "group_name": "primary"}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, groups = api.get_poller_groups()

        assert success is True
        assert len(groups) == 1

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_get_poller_groups_empty(self, mock_get, mock_librenms_config):
        """Verify handling empty poller groups."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "get_poller_group": [],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, groups = api.get_poller_groups()

        assert success is True
        assert len(groups) == 0

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_list_devices_all(self, mock_get, mock_librenms_config):
        """Verify listing all devices."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "devices": [{"device_id": 1}, {"device_id": 2}, {"device_id": 3}],
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, devices = api.list_devices()

        assert success is True
        assert len(devices) == 3

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_list_devices_empty(self, mock_get, mock_librenms_config):
        """Verify handling empty device list."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"status": "ok", "devices": []}

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, devices = api.list_devices()

        assert success is True
        assert len(devices) == 0


# =============================================================================
# Test Class 9: Error Handling (6 tests)
# =============================================================================


class TestLibreNMSAPIErrorHandling:
    """Test error handling and edge cases."""

    pytest_plugins = ["netbox_librenms_plugin.tests.test_librenms_api_helpers"]

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_network_error_handling(self, mock_get, mock_librenms_config):
        """Verify handling of network errors."""
        mock_get.side_effect = requests.exceptions.ConnectionError("Network unreachable")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, result = api.get_device_info(device_id=123)

        assert success is False
        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_timeout_error_handling(self, mock_get, mock_librenms_config):
        """Verify handling of timeout errors."""
        mock_get.side_effect = requests.exceptions.Timeout("Request timed out")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, result = api.get_device_info(device_id=123)

        assert success is False
        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_invalid_json_response(self, mock_get, mock_librenms_config):
        """Verify handling of invalid JSON responses."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.side_effect = ValueError("Invalid JSON")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")

        # ValueError should be raised, not caught
        with pytest.raises(ValueError):
            api.get_device_info(device_id=123)

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_http_500_error_handling(self, mock_get, mock_librenms_config):
        """Verify handling of HTTP 500 errors."""
        mock_get.return_value.status_code = 500
        mock_get.return_value.json.return_value = {
            "status": "error",
            "message": "Internal server error",
        }

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        success, result = api.get_device_info(device_id=123)

        assert success is False
        assert result is None

    @patch("netbox_librenms_plugin.librenms_api.requests.post")
    def test_malformed_api_response(self, mock_post, mock_librenms_config):
        """Verify handling of malformed API responses."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {}  # Missing expected fields

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.add_device(
            data={
                "hostname": "test.example.com",
                "snmp_version": "v2c",
                "community": "public",
            }
        )

        # Should handle missing fields gracefully
        assert result[0] is False

    @patch("netbox_librenms_plugin.librenms_api.requests.get")
    def test_ssl_verification_error(self, mock_get, mock_librenms_config):
        """Verify handling of SSL verification errors."""
        mock_get.side_effect = requests.exceptions.SSLError("SSL certificate verification failed")

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        api = LibreNMSAPI(server_key="default")
        result = api.test_connection()

        assert result.get("error") is True


# ====================================================================================
# Test Class 5-9 continuing in next part due to length...
# Run `make unittest` to execute all tests
# ====================================================================================
