"""Shared pytest fixtures for NetBox LibreNMS Plugin tests."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_device_info_cache():
    """Clear get_device_info()'s short-lived cache between tests.

    get_device_info() caches successful lookups in the shared cache. Without this,
    a cached success from one test leaks into another that reuses the same
    (server_key, device_id) but mocks a different response — e.g. the failure-path
    tests keyed on device_id=123 that run after test_get_device_info_success.
    """
    from django.core.cache import cache

    try:
        cache.delete_pattern("librenms_device_info_*")
    except (AttributeError, NotImplementedError):
        cache.clear()
    yield


# =============================================================================
# Real-DB builders (shared by the DB-backed conversions)
# =============================================================================
#
# These plain helpers create real NetBox objects for tests marked
# ``@pytest.mark.django_db`` (they must be called from within a DB-enabled test).
# Centralised here so the DB-backed tests stop hand-rolling a private
# Site/Manufacturer/DeviceType/DeviceRole quartet in every file. No new dependency
# (e.g. factory_boy) is introduced — get_or_create keeps the shared infra to a single
# row set per test transaction, and everything is rolled back between tests.


def _shared_infra():
    """get_or_create the shared Site / Manufacturer / DeviceType / DeviceRole."""
    from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

    site, _ = Site.objects.get_or_create(name="TestSite", slug="test-site")
    mfr, _ = Manufacturer.objects.get_or_create(name="TestMfr", slug="test-mfr")
    dtype, _ = DeviceType.objects.get_or_create(model="TestDT", slug="test-dt", defaults={"manufacturer": mfr})
    role, _ = DeviceRole.objects.get_or_create(name="TestRole", slug="test-role", defaults={"color": "00ff00"})
    return site, dtype, role


def make_device(name, *, serial="", librenms_cf=None):
    """Create a real Device on the shared infra, optionally seeding its librenms_id CF."""
    from dcim.models import Device

    site, dtype, role = _shared_infra()
    dev = Device.objects.create(name=name, device_type=dtype, role=role, site=site, status="active", serial=serial)
    if librenms_cf is not None:
        dev.custom_field_data["librenms_id"] = librenms_cf
        dev.save()
    return dev


def make_cluster(name):
    """Create a real Cluster on a shared ClusterType."""
    from virtualization.models import Cluster, ClusterType

    ctype, _ = ClusterType.objects.get_or_create(name="TestCType", slug="test-ctype")
    return Cluster.objects.create(name=name, type=ctype)


def make_vm(name, cluster=None):
    """Create a real VirtualMachine (on a shared cluster unless one is supplied)."""
    from virtualization.models import Cluster, ClusterType, VirtualMachine

    if cluster is None:
        ctype, _ = ClusterType.objects.get_or_create(name="TestCType", slug="test-ctype")
        cluster, _ = Cluster.objects.get_or_create(name="TestCluster", defaults={"type": ctype})
    return VirtualMachine.objects.create(name=name, cluster=cluster, status="active")


def make_serial_device(name, *, csp_names=(), cp_names=()):
    """Create a real Device with optional ConsoleServerPorts / ConsolePorts.

    Returns ``(device, console_server_ports, console_ports)``.
    """
    from dcim.models import ConsolePort, ConsoleServerPort

    dev = make_device(name)
    csps = [ConsoleServerPort.objects.create(device=dev, name=n) for n in csp_names]
    cps = [ConsolePort.objects.create(device=dev, name=n) for n in cp_names]
    return dev, csps, cps


def cable_together(term_a, term_b):
    """Create a real Cable between two terminations (NetBox 4.x multi-termination API)."""
    from dcim.models import Cable

    cable = Cable(a_terminations=[term_a], b_terminations=[term_b])
    cable.save()
    return cable


def make_interface(device, name, *, iface_type="other"):
    """Create a real Interface on *device*."""
    from dcim.models import Interface

    return Interface.objects.create(device=device, name=name, type=iface_type)


def make_ip(address, *, assigned_object=None, status="active"):
    """Create a real IPAddress, optionally assigned to an interface/object."""
    from ipam.models import IPAddress

    return IPAddress.objects.create(address=address, assigned_object=assigned_object, status=status)


def make_module_type(model, *, manufacturer=None):
    """Create a real ModuleType (on the shared TestMfr unless one is supplied)."""
    from dcim.models import ModuleType

    if manufacturer is None:
        _, _, _ = _shared_infra()  # ensure shared infra exists
        from dcim.models import Manufacturer

        manufacturer = Manufacturer.objects.get(slug="test-mfr")
    return ModuleType.objects.create(manufacturer=manufacturer, model=model)


def make_module_bay(device, name):
    """Create a real ModuleBay on *device*."""
    from dcim.models import ModuleBay

    return ModuleBay.objects.create(device=device, name=name)


def ip_on(device, address, ifname, *, iface_type="1000base-t"):
    """Create an Interface on *device* and assign a real IPAddress to it."""
    iface = make_interface(device, ifname, iface_type=iface_type)
    return make_ip(address, assigned_object=iface)


def delete_keeping_pk(obj):
    """Delete the row via the queryset so the in-memory instance keeps its pk.

    ``Model.delete()`` nulls ``instance.pk``; tests that simulate "object vanished since
    caching" need the cached instance to retain its original pk, so the delete goes through
    the manager rather than the instance.
    """
    type(obj).objects.filter(pk=obj.pk).delete()


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
    return mock_response_factory(status_code=200, json_data={"status": "ok", "message": "Success"})


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
    return mock_response_factory(status_code=401, json_data={"status": "error", "message": "Unauthorized"})


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


# =============================================================================
# Server Mapping Fixtures (used by test_sync_view_mismatch.py)
# =============================================================================


@pytest.fixture
def mock_plugins_config_single_server():
    """PLUGINS_CONFIG with a single 'production' server (for _build_all_server_mappings tests)."""
    return {
        "netbox_librenms_plugin": {
            "servers": {
                "production": {
                    "display_name": "Production LibreNMS",
                    "librenms_url": "https://librenms.example.com",
                },
            }
        }
    }


@pytest.fixture
def mock_plugins_config_empty_servers():
    """PLUGINS_CONFIG with no configured servers (simulates all orphaned)."""
    return {"netbox_librenms_plugin": {"servers": {}}}


@pytest.fixture
def mock_plugins_config_multi_server_mapping():
    """PLUGINS_CONFIG with 'production' and 'mock-dev' servers (for multi-server mapping tests)."""
    return {
        "netbox_librenms_plugin": {
            "servers": {
                "production": {
                    "display_name": "Production LibreNMS",
                    "librenms_url": "https://librenms.example.com",
                },
                "mock-dev": {
                    "display_name": "Mock",
                    "librenms_url": "http://mock.example.com",
                },
            }
        }
    }
