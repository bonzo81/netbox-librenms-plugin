"""Shared pytest fixtures for NetBox LibreNMS Plugin tests."""

import os
from copy import deepcopy
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from netbox_librenms_plugin.tests.parallel import isolated_test_database_name


_TEST_DATABASE_BASE_NAME = os.environ["TEST_DB_NAME"]


def clear_test_cache(cache_backend):
    """Clear only the active test namespace when the backend supports patterns."""
    try:
        cache_backend.delete_pattern("*")
    except (AttributeError, NotImplementedError):
        cache_backend.clear()


def _isolated_cache_config(caches_config):
    """Return the worker cache config with a unique per-test namespace."""
    isolated = deepcopy(caches_config)
    default = isolated.setdefault("default", {})
    unique_prefix = f"nblp-test-{uuid4().hex}"
    configured_prefix = default.get("KEY_PREFIX")
    default["KEY_PREFIX"] = f"{configured_prefix}:{unique_prefix}" if configured_prefix else unique_prefix
    return isolated


@pytest.fixture(autouse=True)
def _isolate_test_cache(settings):
    """Run every test in a unique namespace inside its worker cache database.

    The isolated settings assign each pytest worker its own Redis database. A unique key
    prefix then isolates individual tests without changing that worker assignment or clearing
    another test process's cache.
    """
    settings.CACHES = _isolated_cache_config(settings.CACHES)
    yield


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings):
    """Give each pytest worker a private PostgreSQL database."""
    from django.conf import settings

    test_config = dict(settings.DATABASES["default"].get("TEST") or {})
    test_config["NAME"] = isolated_test_database_name(
        _TEST_DATABASE_BASE_NAME,
        os.environ.get("PYTEST_XDIST_WORKER"),
    )
    settings.DATABASES["default"]["TEST"] = test_config


@pytest.fixture(autouse=True)
def _clear_device_info_cache(_isolate_test_cache):
    """Flush the plugin's caches between tests.

    NetBox uses Redis, which (unlike the test DB) is NOT rolled back between tests, while
    primary keys ARE reused after each rollback. Every plugin cache key is built from a model
    name and a pk (``CacheMixin.get_cache_key`` → ``librenms_links_device_7_default``), so a
    value cached by one test is read by the next test that draws the same pk, with entirely
    different data behind it.

    ``_isolate_test_cache`` already gives this test its own key prefix, so clearing the whole
    namespace is exact: it cannot reach another test or the dev server. That covers the
    ``librenms_*`` render caches, ``get_device_info()``'s short-lived lookup cache and the
    ``import_device_data_*`` keys the bulk-import collision gate reads through
    ``cache.get_many`` before it falls back to the stubbed ``get_device_info``.
    """
    from django.core.cache import cache

    clear_test_cache(cache)
    yield
    # Each test gets a fresh KEY_PREFIX, so without this the namespace it filled stays in Redis
    # until its TTL expires and a long parallel run accumulates dead keys in every worker.
    clear_test_cache(cache)


def _seeded_model_rows():
    """Yield ``(model, lookup_field, value_field, rows)`` for every data-migration seed."""
    import importlib

    from netbox_librenms_plugin.models import PortStackLagPattern

    # The migration module name starts with a digit, so import syntax cannot reach it.
    lag = importlib.import_module("netbox_librenms_plugin.migrations.0013_portstacklagpattern")
    yield PortStackLagPattern, "librenms_os", "lag_name_pattern", lag.INITIAL_LAG_PATTERNS


def _seeded_sap_rows():
    """Yield ``(model, lookup_field, value_field, rows)`` for the seed that UPDATES existing rows.

    Kept apart from :func:`_seeded_model_rows` because migration 0016 sets a second field on rows
    0013 already created, so these rows are applied with ``update()`` rather than
    ``get_or_create()``. Both the restore and its intactness check read this one definition.
    """
    import importlib

    from netbox_librenms_plugin.models import PortStackLagPattern

    sap = importlib.import_module("netbox_librenms_plugin.migrations.0016_portstacklagpattern_sap_name_pattern")
    yield PortStackLagPattern, "librenms_os", "sap_name_pattern", sap.INITIAL_SAP_PATTERNS


def seed_migration_rows():
    """Recreate every row the plugin's data migrations seed."""
    for model, lookup_field, value_field, rows in _seeded_model_rows():
        for lookup, value in rows:
            model.objects.get_or_create(**{lookup_field: lookup}, defaults={value_field: value})

    # get_or_create above matches the 0013 row and leaves the 0016 field at the model's blank
    # default, so the Nokia SAP rule silently disappears for every test after the first
    # transactional one. Re-apply it from the migration's own seed data.
    for model, lookup_field, value_field, rows in _seeded_sap_rows():
        for lookup, value in rows:
            model.objects.filter(**{lookup_field: lookup}).update(**{value_field: value})


@pytest.fixture(autouse=True)
def _restore_migration_seeded_rows(request):
    """Re-seed the rows a transactional test's flush removes.

    ``django_db(transaction=True)`` truncates every table, including rows a data migration
    created, so LAG detection loses its per-OS name patterns and every later test in the run
    sees an empty recognition table.

    The flush runs in the ``transactional_db`` teardown, which finalizes AFTER this fixture, so
    the restore has to happen on the way in. A non-transactional test runs inside a transaction
    that is rolled back, so seeding here is visible to it and leaves nothing behind.
    """
    # Gate on the marker, not on request.fixturenames: pytest-django pulls "db" in dynamically
    # from its own autouse fixture, so it is still absent from the closure at this point.
    marker = request.node.get_closest_marker("django_db")
    requested = {"db", "transactional_db"} & set(request.fixturenames)
    if marker is None and not requested:
        yield
        return

    db_fixture = "transactional_db" if (marker and marker.kwargs.get("transaction")) else "db"
    # An autouse fixture is set up BEFORE the fixtures it does not request, so ask for the
    # database one here: querying without it raises "Database access not allowed".
    request.getfixturevalue(db_fixture)
    for model, _lookup_field, _value_field, _rows in _seeded_model_rows():
        if not model.objects.exists():
            seed_migration_rows()
            break
    _restore_librenms_id_custom_field()
    yield


def _restore_librenms_id_custom_field():
    """Recreate the librenms_id custom field a transactional flush removed.

    The handler short-circuits on its own ``_executed_aliases`` guard, so after a flush it will
    not recreate the field by itself. Only act when the field is actually gone, which keeps this
    to one cheap existence check for the non-transactional majority.
    """
    from extras.models import CustomField

    from netbox_librenms_plugin import _ensure_librenms_id_custom_field

    if CustomField.objects.filter(name="librenms_id").exists():
        return
    _ensure_librenms_id_custom_field._executed_aliases.discard("default")
    _ensure_librenms_id_custom_field(sender=None, using="default")


@pytest.fixture(scope="session", autouse=True)
def _reseed_after_transactional_flush(django_db_setup, django_db_blocker):
    """Leave the reused database seeded for the next run.

    The last transactional test flushes on its way out, after every function-scoped fixture has
    finalized. Without this the seeded rows stay missing in the reused database and the next run
    starts with LAG pattern detection silently disabled.
    """
    yield

    with django_db_blocker.unblock():
        seed_migration_rows()


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
    return site, mfr, dtype, role


def make_device(name, *, serial="", librenms_cf=None):
    """Create a real Device on the shared infra, optionally seeding its librenms_id CF."""
    from dcim.models import Device

    site, _mfr, dtype, role = _shared_infra()
    dev = Device.objects.create(name=name, device_type=dtype, role=role, site=site, status="active", serial=serial)
    if librenms_cf is not None:
        dev.custom_field_data["librenms_id"] = librenms_cf
        dev.save()
    return dev


def make_virtual_chassis_members(tag, count=2):
    """Create a VirtualChassis with members at consecutive positions."""
    from dcim.models import VirtualChassis

    virtual_chassis = VirtualChassis.objects.create(name=f"vc-{tag}")
    members = []
    for position in range(1, count + 1):
        member = make_device(f"{tag}-m{position}")
        member.virtual_chassis = virtual_chassis
        member.vc_position = position
        member.save()
        members.append(member)
    return virtual_chassis, members


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
    """Create a real Device with optional ConsoleServerPorts / ConsolePorts."""
    from dcim.models import ConsolePort, ConsoleServerPort

    dev = make_device(name)
    csps = [ConsoleServerPort.objects.create(device=dev, name=n) for n in csp_names]
    cps = [ConsolePort.objects.create(device=dev, name=n) for n in cp_names]
    return dev, csps, cps


def make_virtual_chassis(name, *devices):
    """Create a VirtualChassis and enroll *devices* as members (vc_position by order)."""
    from dcim.models import VirtualChassis

    vc = VirtualChassis.objects.create(name=name)
    for position, dev in enumerate(devices, start=1):
        dev.virtual_chassis = vc
        dev.vc_position = position
        dev.save()
    return vc


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


def configure_librenms_servers(settings, servers):
    """Replace the plugin's configured LibreNMS servers with *servers*."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = deepcopy(servers)
    settings.PLUGINS_CONFIG = plugin_config


def configure_default_librenms_server(settings):
    """Configure one LibreNMS server under the key ``default`` and return that key."""
    configure_librenms_servers(
        settings, {"default": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"}}
    )
    return "default"


def configure_no_librenms_servers(settings):
    """Leave the plugin without any server ``LibreNMSAPI()`` can bind."""
    from copy import deepcopy

    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_settings = plugin_config["netbox_librenms_plugin"]
    plugin_settings["servers"] = {}
    plugin_settings.pop("librenms_url", None)
    plugin_settings.pop("api_token", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def librenms_server(monkeypatch):
    """A real loopback HTTP LibreNMS whose responses the test registers."""
    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        yield server


def make_module_type(model, *, manufacturer=None):
    """Create a real ModuleType (on the shared TestMfr unless one is supplied)."""
    from dcim.models import ModuleType

    if manufacturer is None:
        _, manufacturer, _, _ = _shared_infra()
    return ModuleType.objects.create(manufacturer=manufacturer, model=model)


def make_module_bay(device, name):
    """Create a real ModuleBay on *device*."""
    from dcim.models import ModuleBay

    return ModuleBay.objects.create(device=device, name=name)


def make_module_type_with_bays(model, *, manufacturer=None, bay_names=()):
    """get_or_create a real ModuleType and (when first created) attach ModuleBayTemplates."""
    from dcim.models import ModuleBayTemplate, ModuleType

    if manufacturer is None:
        _, manufacturer, _, _ = _shared_infra()
    # Additive: reusing the same model with new bay_names must add the missing templates rather
    # than silently skip them (the create-only guard made tests order-dependent).
    mt, _ = ModuleType.objects.get_or_create(manufacturer=manufacturer, model=model)
    for bn in bay_names:
        ModuleBayTemplate.objects.get_or_create(module_type=mt, name=bn)
    return mt


def make_device_with_module_bays(name, bay_names, *, manufacturer=None, serial=""):
    """Create a real Device on a dedicated DeviceType carrying device-level ModuleBayTemplates."""
    from dcim.models import Device, DeviceType, ModuleBayTemplate

    site, mfr, _, role = _shared_infra()
    if manufacturer is None:
        manufacturer = mfr
    slug = f"mbt-dt-{name}".lower().replace(" ", "-")
    dtype = DeviceType.objects.create(manufacturer=manufacturer, model=f"DT-{name}", slug=slug)
    for bn in bay_names:
        ModuleBayTemplate.objects.create(device_type=dtype, name=bn)
    return Device.objects.create(name=name, device_type=dtype, role=role, site=site, status="active", serial=serial)


def load_contrib_bay_mappings():
    """Create real ModuleBayMapping rows from contrib/module_bay_mappings.yaml."""
    from pathlib import Path

    import yaml

    from netbox_librenms_plugin.models import ModuleBayMapping

    contrib = Path(__file__).resolve().parents[2] / "contrib" / "module_bay_mappings.yaml"
    with open(contrib) as f:
        data = yaml.safe_load(f)
    return [
        ModuleBayMapping.objects.create(
            librenms_name=m["librenms_name"],
            librenms_class=m.get("librenms_class") or "",
            netbox_bay_name=m["netbox_bay_name"],
            is_regex=m.get("is_regex", False),
        )
        for m in data
    ]


def install_module(device, bay_name, model, *, serial="", child_bays=(), manufacturer=None, parent_module=None):
    """Install a real Module of type *model* into *device*'s ModuleBay named *bay_name*."""
    from dcim.models import Module, ModuleBay

    mt = make_module_type_with_bays(model, manufacturer=manufacturer, bay_names=child_bays)
    qs = ModuleBay.objects.filter(device=device, name=bay_name)
    if parent_module is not None:
        qs = qs.filter(module=parent_module)
    bay = qs.get()
    return Module.objects.create(device=device, module_bay=bay, module_type=mt, serial=serial, status="active")


def ip_on(device, address, ifname, *, iface_type="1000base-t"):
    """Create an Interface on *device* and assign a real IPAddress to it."""
    iface = make_interface(device, ifname, iface_type=iface_type)
    ip = make_ip(address, assigned_object=iface)
    # NetBox 4.4's IP pre-delete receiver reads address.version. Coerce the string passed to
    # objects.create() through the model field before returning so every caller can delete safely.
    ip.refresh_from_db()
    return ip


def delete_keeping_pk(obj):
    """Delete the row via the queryset so the in-memory instance keeps its pk."""
    type(obj).objects.filter(pk=obj.pk).delete()


def make_superuser(username="review-su"):
    """Return an ACTUAL active superuser for permission-sensitive view tests.

    ``User.objects.first()`` can hand back a pre-seeded non-superuser (DB-ordering dependent),
    which would run the test under the wrong principal and cover the wrong branch. This filters
    explicitly for an active superuser, and otherwise get_or_creates one — reusing and correcting
    a pre-existing inactive/non-superuser row of the same username rather than tripping the
    unique-username constraint a bare create() would hit. NetBox's User model has no is_staff
    field; only is_superuser/is_active gate access here.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(is_superuser=True, is_active=True).first()
    if user:
        return user
    user, _ = User.objects.get_or_create(
        username=username,
        defaults={"is_superuser": True, "is_active": True},
    )
    if not user.is_superuser or not user.is_active:
        user.is_superuser = True
        user.is_active = True
        user.save(update_fields=["is_superuser", "is_active"])
    return user


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
