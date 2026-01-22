"""Shared pytest fixtures for NetBox LibreNMS Plugin tests."""

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Configuration Fixtures
# =============================================================================


@pytest.fixture
def mock_multi_server_config():
    """Multi-server configuration dict."""
    return {
        "default": {
            "librenms_url": "https://librenms-default.example.com",
            "api_token": "default-token-12345",
            "cache_timeout": 300,
            "verify_ssl": True,
        },
        "secondary": {
            "librenms_url": "https://librenms-secondary.example.com",
            "api_token": "secondary-token-67890",
            "cache_timeout": 600,
            "verify_ssl": False,
        },
    }


@pytest.fixture
def mock_legacy_config():
    """Legacy single-server configuration dict (flat structure)."""
    return {
        "librenms_url": "https://librenms.example.com",
        "api_token": "legacy-token-abcdef",
        "cache_timeout": 300,
        "verify_ssl": True,
    }


# =============================================================================
# API Instance Fixtures
# =============================================================================


@pytest.fixture
def mock_librenms_api(mock_multi_server_config):
    """Pre-configured LibreNMSAPI instance with mocked dependencies."""
    with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_config:
        mock_config.return_value = mock_multi_server_config
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSSettings"
        ) as mock_settings:
            mock_settings.objects.filter.return_value.first.return_value = None
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            api = LibreNMSAPI(server_key="default")
            yield api


# =============================================================================
# NetBox Object Mocks (Avoid Database)
# =============================================================================


@pytest.fixture
def mock_netbox_device():
    """Mock NetBox Device object without database."""
    device = MagicMock()
    device.name = "test-device"
    device.cf = {}  # Custom fields
    device.primary_ip4 = MagicMock()
    device.primary_ip4.address = MagicMock()
    device.primary_ip4.address.ip = "192.168.1.1"
    device.primary_ip4.__str__ = lambda self: "192.168.1.1/24"
    device.primary_ip6 = None
    device._meta.model_name = "device"
    return device


@pytest.fixture
def mock_netbox_vm():
    """Mock NetBox VirtualMachine object without database."""
    vm = MagicMock()
    vm.name = "test-vm"
    vm.cf = {}
    vm.primary_ip4 = MagicMock()
    vm.primary_ip4.address = MagicMock()
    vm.primary_ip4.address.ip = "10.0.0.1"
    vm.primary_ip6 = None
    vm._meta.model_name = "virtualmachine"
    return vm


# =============================================================================
# HTTP Response Fixtures
# =============================================================================


@pytest.fixture
def mock_response_factory():
    """Factory for creating mock HTTP responses."""

    def _create_response(status_code=200, json_data=None, raise_for_status=None):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = json_data or {}
        response.ok = 200 <= status_code < 300
        if raise_for_status:
            response.raise_for_status.side_effect = raise_for_status
        return response

    return _create_response


@pytest.fixture
def mock_success_response(mock_response_factory):
    """Standard successful API response."""
    return mock_response_factory(
        status_code=200, json_data={"status": "ok", "message": "Success"}
    )


@pytest.fixture
def mock_device_response(mock_response_factory):
    """Mock response for device info endpoint."""
    return mock_response_factory(
        status_code=200,
        json_data={
            "status": "ok",
            "devices": [
                {
                    "device_id": 42,
                    "hostname": "test-device.example.com",
                    "sysName": "test-device",
                    "ip": "192.168.1.1",
                    "status": 1,
                    "location": "Data Center 1",
                }
            ],
        },
    )


@pytest.fixture
def mock_error_response(mock_response_factory):
    """Standard error API response."""
    return mock_response_factory(
        status_code=500,
        json_data={"status": "error", "message": "Internal server error"},
    )


@pytest.fixture
def mock_auth_error_response(mock_response_factory):
    """Authentication error response (401)."""
    return mock_response_factory(
        status_code=401, json_data={"status": "error", "message": "Unauthorized"}
    )


# =============================================================================
# Phase 2: Import Utilities Fixtures
# =============================================================================


@pytest.fixture
def sample_librenms_device():
    """Sample LibreNMS device data for import tests."""
    return {
        "device_id": 1,
        "hostname": "switch-01.example.com",
        "sysName": "switch-01",
        "ip": "192.168.1.1",
        "location": "DC1",
        "os": "ios",
        "hardware": "C9300-48P",
        "version": "17.3.1",
        "status": 1,
    }


@pytest.fixture
def sample_librenms_device_minimal():
    """Minimal LibreNMS device data with missing fields."""
    return {
        "device_id": 2,
        "hostname": "10.0.0.1",
        "status": 1,
    }


@pytest.fixture
def sample_validation_state():
    """Sample validation state for testing updates."""
    return {
        "device_id": 1,
        "hostname": "switch-01",
        "is_ready": False,
        "can_import": False,
        "import_as_vm": False,
        "existing_device": None,
        "issues": ["Device role must be manually selected before import"],
        "warnings": [],
        "site": {
            "found": True,
            "site": MagicMock(id=1, name="DC1"),
            "match_type": "exact",
        },
        "device_type": {
            "found": True,
            "device_type": MagicMock(id=1, model="C9300-48P"),
            "match_type": "exact",
        },
        "device_role": {"found": False, "role": None, "available_roles": []},
        "cluster": {"found": False, "cluster": None, "available_clusters": []},
        "platform": {
            "found": True,
            "platform": MagicMock(id=1, name="ios"),
            "match_type": "exact",
        },
    }


@pytest.fixture
def sample_validation_state_vm():
    """Sample validation state for VM import testing."""
    return {
        "device_id": 1,
        "hostname": "vm-01",
        "is_ready": False,
        "can_import": False,
        "import_as_vm": True,
        "existing_device": None,
        "issues": ["Cluster must be manually selected before import"],
        "warnings": [],
        "cluster": {"found": False, "cluster": None, "available_clusters": []},
        "device_role": {"found": False, "role": None, "available_roles": []},
    }


@pytest.fixture
def mock_netbox_site():
    """Mock NetBox Site object."""
    site = MagicMock()
    site.id = 1
    site.name = "DC1"
    site.slug = "dc1"
    return site


@pytest.fixture
def mock_netbox_platform():
    """Mock NetBox Platform object."""
    platform = MagicMock()
    platform.id = 1
    platform.name = "Cisco IOS"
    platform.slug = "cisco_ios"
    return platform


@pytest.fixture
def mock_netbox_device_type():
    """Mock NetBox DeviceType object."""
    dt = MagicMock()
    dt.id = 1
    dt.model = "C9300-48P"
    dt.manufacturer = MagicMock(name="Cisco")
    return dt


@pytest.fixture
def mock_netbox_device_role():
    """Mock NetBox DeviceRole object."""
    role = MagicMock()
    role.id = 1
    role.name = "Access Switch"
    role.slug = "access-switch"
    return role


@pytest.fixture
def mock_netbox_cluster():
    """Mock NetBox Cluster object."""
    cluster = MagicMock()
    cluster.id = 1
    cluster.name = "VMware Cluster 1"
    return cluster


@pytest.fixture
def mock_netbox_rack():
    """Mock NetBox Rack object."""
    rack = MagicMock()
    rack.id = 1
    rack.name = "Rack A1"
    rack.site = MagicMock(id=1, name="DC1")
    return rack
