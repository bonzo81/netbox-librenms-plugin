"""
Tests for netbox_librenms_plugin.import_utils module.

Phase 2 tests covering cache key generation, device name determination,
device retrieval, and device validation functions.
"""

from copy import deepcopy
import pytest

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


def _real_librenms_api(settings, server, *, server_key="test-server"):
    """Point the real LibreNMS client at the local HTTP test server."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        server_key: {
            "librenms_url": server.url,
            "api_token": "test-token",
            "verify_ssl": False,
        }
    }
    settings.PLUGINS_CONFIG = plugin_config
    return LibreNMSAPI(server_key=server_key)


@pytest.fixture
def librenms_server(monkeypatch):
    """Run a local HTTP server for real LibreNMS client requests."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        yield server


# =============================================================================
# TestCacheKeyGeneration - 4 tests
# =============================================================================


class TestCacheKeyGeneration:
    """Test cache key generation functions."""

    def test_get_cache_metadata_key_basic(self):
        """Generate cache metadata key with minimal filters."""
        from netbox_librenms_plugin.import_utils import get_cache_metadata_key

        key = get_cache_metadata_key(server_key="default", filters={}, vc_enabled=False)

        assert "default" in key
        assert "librenms_filter_cache_metadata" in key
        assert isinstance(key, str)

    def test_get_cache_metadata_key_all_params(self):
        """Generate cache metadata key with all filter parameters."""
        from netbox_librenms_plugin.import_utils import get_cache_metadata_key

        key = get_cache_metadata_key(
            server_key="production",
            filters={"location": "DC1", "type": "network", "hostname": "switch*"},
            vc_enabled=True,
        )

        assert "production" in key
        # Filter values are hashed, not embedded directly in the key
        assert "librenms_filter_cache_metadata" in key
        assert "True" in key or "true" in key.lower()

    def test_get_validated_device_cache_key(self):
        """Generate validated device cache key."""
        from netbox_librenms_plugin.import_utils import get_validated_device_cache_key

        key = get_validated_device_cache_key(
            server_key="default",
            filters={"location": "NYC"},
            device_id=123,
            vc_enabled=True,
        )

        assert "validated_device" in key
        assert "default" in key
        assert "123" in key
        assert "vc" in key

    def test_get_import_device_cache_key(self):
        """Generate raw device data cache key."""
        from netbox_librenms_plugin.import_utils import get_import_device_cache_key

        key = get_import_device_cache_key(device_id=456, server_key="secondary")

        assert "import_device_data" in key
        assert "secondary" in key
        assert "456" in key

    def test_validated_device_cache_key_unique_per_naming_mode(self):
        """Different naming preferences produce different cache keys."""
        from netbox_librenms_plugin.import_utils import get_validated_device_cache_key

        base_args = dict(server_key="default", filters={}, device_id=123, vc_enabled=False)
        key_default = get_validated_device_cache_key(**base_args)
        key_no_sysname = get_validated_device_cache_key(**base_args, use_sysname=False)
        key_strip = get_validated_device_cache_key(**base_args, strip_domain=True)

        assert key_default != key_no_sysname
        assert key_default != key_strip
        assert key_no_sysname != key_strip

    def test_cache_metadata_key_unique_per_naming_mode(self):
        """Different naming preferences produce different metadata cache keys."""
        from netbox_librenms_plugin.import_utils import get_cache_metadata_key

        base_args = dict(server_key="default", filters={}, vc_enabled=False)
        key_default = get_cache_metadata_key(**base_args)
        key_no_sysname = get_cache_metadata_key(**base_args, use_sysname=False)
        key_strip = get_cache_metadata_key(**base_args, strip_domain=True)

        assert key_default != key_no_sysname
        assert key_default != key_strip


# =============================================================================
# TestDeviceNameDetermination - 6 tests
# =============================================================================


class TestDeviceNameDetermination:
    """Test device name determination logic."""

    def test_determine_device_name_prefers_sysname(self):
        """sysName should be preferred over hostname when use_sysname=True."""
        from netbox_librenms_plugin.import_utils import _determine_device_name

        device_data = {"sysName": "switch-01", "hostname": "switch-01.example.com"}

        name = _determine_device_name(device_data, use_sysname=True)
        assert name == "switch-01"

    def test_determine_device_name_falls_back_to_hostname(self):
        """hostname used when sysName missing."""
        from netbox_librenms_plugin.import_utils import _determine_device_name

        device_data = {"hostname": "router-01.example.com"}

        name = _determine_device_name(device_data, use_sysname=True)
        assert name == "router-01.example.com"

    def test_determine_device_name_strips_domain(self):
        """FQDN domain suffix should be stripped when strip_domain=True."""
        from netbox_librenms_plugin.import_utils import _determine_device_name

        device_data = {
            "sysName": "router-core.datacenter.example.com",
            "hostname": "10.0.0.1",
        }

        name = _determine_device_name(device_data, use_sysname=True, strip_domain=True)
        assert name == "router-core"

    def test_determine_device_name_handles_empty_sysname(self):
        """Empty sysName should fall back to hostname."""
        from netbox_librenms_plugin.import_utils import _determine_device_name

        device_data = {"sysName": "", "hostname": "fallback-host"}

        name = _determine_device_name(device_data, use_sysname=True)
        assert name == "fallback-host"

    def test_determine_device_name_preserves_short_names(self):
        """Names without dots should remain unchanged."""
        from netbox_librenms_plugin.import_utils import _determine_device_name

        device_data = {"sysName": "shortname", "hostname": "192.168.1.1"}

        name = _determine_device_name(device_data, use_sysname=True, strip_domain=True)
        assert name == "shortname"

    def test_determine_device_name_handles_ip_address(self):
        """IP addresses should not be stripped even with strip_domain=True."""
        from netbox_librenms_plugin.import_utils import _determine_device_name

        device_data = {"sysName": "192.168.1.1", "hostname": "192.168.1.1"}

        name = _determine_device_name(device_data, use_sysname=True, strip_domain=True)
        # IP addresses should not have domain stripped
        assert name == "192.168.1.1"

    def test_determine_device_name_fallback_to_device_id(self):
        """Fallback to device_id when no name available."""
        from netbox_librenms_plugin.import_utils import _determine_device_name

        device_data = {}

        name = _determine_device_name(device_data, device_id=999)
        assert name == "device-999"

    @pytest.mark.parametrize("empty_first_label", [".example.com", ".", "..", ".corp.local"])
    def test_determine_device_name_falls_back_when_stripping_empties_the_name(self, empty_first_label):
        """A name whose first label is empty falls back to the device_id name instead of returning ''."""
        from netbox_librenms_plugin.import_utils import _determine_device_name

        device_data = {"sysName": empty_first_label, "hostname": ""}

        name = _determine_device_name(device_data, use_sysname=True, strip_domain=True, device_id=42)
        assert name == "device-42"

    @pytest.mark.parametrize("non_string", [7, 0, True, ["router01"], {"name": "router01"}, 3.5])
    @pytest.mark.parametrize("strip_domain", [True, False])
    def test_determine_device_name_ignores_a_non_string_name(self, non_string, strip_domain):
        """A non-string sysName is treated as absent, so the device_id fallback names the device."""
        from netbox_librenms_plugin.import_utils import _determine_device_name

        device_data = {"sysName": non_string, "hostname": None}

        name = _determine_device_name(device_data, use_sysname=True, strip_domain=strip_domain, device_id=42)
        assert name == "device-42"


# =============================================================================
# TestDeviceRetrieval - 10 tests
# =============================================================================


class TestDeviceRetrieval:
    """Test device retrieval and filtering functions."""

    @pytest.fixture(autouse=True)
    def clear_import_cache(self):
        """Keep each test isolated while exercising Django's real cache backend."""
        from django.core.cache import cache

        cache.clear()
        yield
        cache.clear()

    def test_get_librenms_devices_for_import_success(self, settings, librenms_server):
        """Retrieve devices from LibreNMS API."""
        requests = []

        def devices_route(**request):
            requests.append(request)
            return 200, {
                "status": "ok",
                "devices": [
                    {"device_id": 1, "hostname": "switch-01"},
                    {"device_id": 2, "hostname": "switch-02"},
                ],
            }

        librenms_server.register("/api/v0/devices", devices_route, method="GET")
        api = _real_librenms_api(settings, librenms_server)

        from netbox_librenms_plugin.import_utils import get_librenms_devices_for_import

        devices = get_librenms_devices_for_import(api=api, filters={})

        assert len(devices) == 2
        assert devices[0]["hostname"] == "switch-01"
        assert len(requests) == 1
        assert requests[0]["query"] == {}

    def test_get_librenms_devices_for_import_uses_cache(self, settings, librenms_server):
        """Cached results returned on repeat call."""
        requests = []
        current_devices = [{"device_id": 1, "hostname": "cached-device"}]

        def devices_route(**request):
            requests.append(request)
            return 200, {"status": "ok", "devices": list(current_devices)}

        librenms_server.register("/api/v0/devices", devices_route, method="GET")
        api = _real_librenms_api(settings, librenms_server)

        from netbox_librenms_plugin.import_utils import get_librenms_devices_for_import

        first_devices = get_librenms_devices_for_import(api=api, filters={})
        current_devices[:] = [{"device_id": 2, "hostname": "uncached-device"}]
        cached_devices = get_librenms_devices_for_import(api=api, filters={})

        assert first_devices == [{"device_id": 1, "hostname": "cached-device"}]
        assert cached_devices == first_devices
        assert len(requests) == 1

    def test_get_librenms_devices_for_import_cache_miss(self, settings, librenms_server):
        """API called when cache empty."""
        requests = []

        def devices_route(**request):
            requests.append(request)
            return 200, {"status": "ok", "devices": [{"device_id": 3, "hostname": "fresh-device"}]}

        librenms_server.register("/api/v0/devices", devices_route, method="GET")
        api = _real_librenms_api(settings, librenms_server)

        from netbox_librenms_plugin.import_utils import get_librenms_devices_for_import

        devices = get_librenms_devices_for_import(api=api, filters={}, force_refresh=True)

        assert devices == [{"device_id": 3, "hostname": "fresh-device"}]
        assert len(requests) == 1

    def test_get_device_count_for_filters_success(self, settings, librenms_server):
        """Returns correct count from API."""
        requests = []

        def devices_route(**request):
            requests.append(request)
            return 200, {
                "status": "ok",
                "devices": [
                    {"device_id": 1, "hostname": "switch-01", "status": 1},
                    {"device_id": 2, "hostname": "switch-02", "status": 1},
                    {"device_id": 3, "hostname": "switch-03", "status": 0},
                ],
            }

        librenms_server.register("/api/v0/devices", devices_route, method="GET")
        api = _real_librenms_api(settings, librenms_server)

        from netbox_librenms_plugin.import_utils import get_device_count_for_filters

        count = get_device_count_for_filters(api=api, filters={})

        assert count == 3
        assert len(requests) == 1

    def test_get_device_count_excludes_disabled(self, settings, librenms_server):
        """Count respects show_disabled filter parameter: disabled==1 devices excluded."""
        requests = []

        def devices_route(**request):
            requests.append(request)
            return 200, {
                "status": "ok",
                "devices": [
                    {"device_id": 1, "hostname": "switch-01", "disabled": 0, "status": 1},
                    {"device_id": 2, "hostname": "switch-02", "disabled": 0, "status": 0},
                    {"device_id": 3, "hostname": "switch-03", "disabled": 1, "status": 1},
                ],
            }

        librenms_server.register("/api/v0/devices", devices_route, method="GET")
        api = _real_librenms_api(settings, librenms_server)

        from netbox_librenms_plugin.import_utils import get_device_count_for_filters

        count = get_device_count_for_filters(api=api, filters={}, show_disabled=False)

        assert count == 2
        assert len(requests) == 1

    def test_get_import_device_cache_key_default_server(self):
        """Generate cache key with explicit default server key."""
        from netbox_librenms_plugin.import_utils import get_import_device_cache_key

        key = get_import_device_cache_key(device_id=123, server_key="default")

        assert "default" in key
        assert "123" in key

    def test_get_validated_device_cache_key_no_vc(self):
        """Generate cache key without VC enabled."""
        from netbox_librenms_plugin.import_utils import get_validated_device_cache_key

        key = get_validated_device_cache_key(server_key="default", filters={}, device_id=100, vc_enabled=False)

        assert "novc" in key

    def test_get_validated_device_cache_key_with_vc(self):
        """Generate cache key with VC enabled."""
        from netbox_librenms_plugin.import_utils import get_validated_device_cache_key

        key_vc = get_validated_device_cache_key(server_key="default", filters={}, device_id=100, vc_enabled=True)
        key_novc = get_validated_device_cache_key(server_key="default", filters={}, device_id=100, vc_enabled=False)

        # Keys should be different based on VC setting
        assert key_vc != key_novc

    def test_empty_virtual_chassis_data(self):
        """Empty VC data helper returns correct structure."""
        from netbox_librenms_plugin.import_utils import empty_virtual_chassis_data

        data = empty_virtual_chassis_data()

        assert data["is_stack"] is False
        assert data["member_count"] == 0
        assert data["members"] == []
        assert data["detection_error"] is None

    def test_get_virtual_chassis_data_returns_empty_without_api(self):
        """Get VC data returns empty structure without API."""
        from netbox_librenms_plugin.import_utils import get_virtual_chassis_data

        result = get_virtual_chassis_data(api=None, device_id=123)

        assert result["is_stack"] is False
        assert result["member_count"] == 0


# =============================================================================
# TestDeviceValidation - 15 tests
# =============================================================================


@pytest.mark.django_db
class TestDeviceValidation:
    """Exercise validation against real NetBox models."""

    def test_matches_real_site_platform_and_device_type(self):
        from dcim.models import DeviceType, Manufacturer, Platform, Site

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        site = Site.objects.create(name="Validation Site", slug="validation-site")
        platform = Platform.objects.create(name="Validation OS", slug="validation-os")
        manufacturer = Manufacturer.objects.create(name="Validation Vendor", slug="validation-vendor")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="Validation Hardware",
            slug="validation-hardware",
        )

        result = validate_device_for_import(
            {
                "device_id": 1,
                "hostname": "validation-device",
                "location": site.name,
                "os": platform.name,
                "hardware": device_type.model,
            },
            include_vc_detection=False,
        )

        assert result["site"]["site"] == site
        assert result["platform"]["platform"] == platform
        assert result["device_type"]["device_type"] == device_type
        assert result["device_role"]["found"] is False
        assert any("role" in issue.lower() for issue in result["issues"])

    def test_validate_device_preselects_matched_parsed_rack(self):
        """Select the real rack resolved from the configured location pattern."""
        from dcim.models import Rack, Site

        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.models import LibreNMSSettings

        site = Site.objects.create(name="Rack Validation Site", slug="rack-validation-site")
        rack = Rack.objects.create(name="R1", site=site)
        LibreNMSSettings.objects.update_or_create(
            pk=1,
            defaults={"location_parse_pattern": "{site}, {rack}", "location_parse_is_regex": False},
        )

        result = validate_device_for_import(
            {
                "device_id": 1,
                "hostname": "rack-validation.example.test",
                "location": f"{site.name}, {rack.name}",
            },
            include_vc_detection=False,
        )

        assert result["site"]["site"] == site
        assert result["rack"]["rack"] == rack
        assert rack in result["rack"]["available_racks"]

    def test_validate_device_previews_location_hierarchy_and_tenant(self):
        """Resolve the real location ancestry and tenant for the validation preview."""
        from dcim.models import Location, Site
        from tenancy.models import Tenant

        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.models import LibreNMSSettings

        site = Site.objects.create(name="Hierarchy Validation Site", slug="hierarchy-validation-site")
        parent_location = Location.objects.create(name="Parent Hall", slug="parent-hall", site=site)
        child_location = Location.objects.create(name="Child Row", slug="child-row", site=site, parent=parent_location)
        tenant = Tenant.objects.create(name="Example Tenant", slug="example-tenant")
        LibreNMSSettings.objects.update_or_create(
            pk=1,
            defaults={"location_parse_pattern": "{site}, {location}, {tenant}", "location_parse_is_regex": False},
        )

        result = validate_device_for_import(
            {
                "device_id": 1,
                "hostname": "hierarchy-validation.example.test",
                "location": f"{site.name}, {child_location.name}, {tenant.name}",
            },
            include_vc_detection=False,
        )

        assert result["site"]["site"] == site
        assert result["region"] == {"found": False, "region": None}
        assert result["location"] == {
            "found": True,
            "location": child_location,
            "match_type": "exact",
            "token": child_location.name,
            "hierarchy": [parent_location, child_location],
        }
        assert result["tenant"] == {
            "found": True,
            "tenant": tenant,
            "match_type": "exact",
            "token": tenant.name,
        }

    @pytest.mark.parametrize(
        ("field", "result_key"),
        [
            ("location", "site"),
            ("os", "platform"),
            ("hardware", "device_type"),
        ],
    )
    def test_empty_match_inputs_are_handled(self, field, result_key):
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        result = validate_device_for_import(
            {"device_id": 2, "hostname": f"empty-{field}", field: ""},
            include_vc_detection=False,
        )

        assert result[result_key]["found"] is False

    def test_unknown_prerequisites_report_real_validation_results(self):
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        result = validate_device_for_import(
            {
                "device_id": 3,
                "hostname": "unknown-prerequisites",
                "location": "Missing Site",
                "os": "Missing Platform",
                "hardware": "Missing Hardware",
            },
            include_vc_detection=False,
        )

        assert result["site"]["found"] is False
        assert result["platform"]["found"] is False
        assert result["device_type"]["found"] is False
        assert any("site" in issue.lower() for issue in result["issues"])
        assert any("device type" in issue.lower() for issue in result["issues"])

    def test_existing_device_blocks_import(self):
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.tests.conftest import make_device

        existing_device = make_device("existing-device")

        result = validate_device_for_import(
            {"device_id": 4, "hostname": existing_device.name},
            include_vc_detection=False,
        )

        assert result["existing_device"] == existing_device
        assert result["can_import"] is False

    def test_vm_mode_uses_real_cluster(self):
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.tests.conftest import make_cluster

        cluster = make_cluster("Validation Cluster")

        result = validate_device_for_import(
            {"device_id": 5, "hostname": "new-vm"},
            import_as_vm=True,
            include_vc_detection=False,
        )

        assert result["import_as_vm"] is True
        assert cluster in result["cluster"]["available_clusters"]

    def test_existing_vm_blocks_import_and_populates_link(self):
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.tests.conftest import make_vm

        existing_vm = make_vm("existing-vm")
        existing_vm.custom_field_data["librenms_id"] = {"default": 42}
        existing_vm.save()

        result = validate_device_for_import(
            {"device_id": 42, "hostname": existing_vm.name},
            include_vc_detection=False,
        )

        assert result["existing_device"] == existing_vm
        assert result["existing_match_type"] == "librenms_id"
        assert result["can_import"] is False
        assert result["import_as_vm"] is True
        assert result["existing_librenms_link"] == {"host_id": 42, "oob_id": None, "oob_type": None}


@pytest.mark.django_db
class TestSerialNumberMatchingRealDB:
    """Real-DB coverage for the serial-match path (issue #101): serial is not unique in NetBox, so the match must run against real rows."""

    @staticmethod
    def _make_device(name, serial):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        mfr, _ = Manufacturer.objects.get_or_create(name="ACME-101", slug="acme-101")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-101", slug="dt-101")
        role, _ = DeviceRole.objects.get_or_create(name="Role-101", slug="role-101")
        site, _ = Site.objects.get_or_create(name="Site-101", slug="site-101")
        return Device.objects.create(name=name, device_type=dt, role=role, site=site, status="active", serial=serial)

    def test_duplicate_serials_block_import(self):
        """Two real devices share the incoming serial → no arbitrary bind AND the row is blocked from import."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        self._make_device("dup-a-101", "DUP123")
        self._make_device("dup-b-101", "DUP123")

        result = validate_device_for_import(
            # device_id matches no NetBox librenms_id, so the flow reaches the serial block.
            {"device_id": 99999, "hostname": "new-host-101", "serial": "DUP123"},
            include_vc_detection=False,
        )

        assert result["existing_device"] is None
        assert result.get("existing_match_type") != "serial"
        # Ambiguity is a blocking issue, not a mere warning: importing anyway would mint a THIRD
        # device with the same serial.
        assert any("share serial" in i for i in result["issues"])
        assert result["serial_duplicate"] is True
        assert result["can_import"] is False

    @pytest.mark.parametrize("padding", ["  PAD123  ", "\tPAD123\r\n"], ids=["spaces", "tab_and_newline"])
    def test_a_padded_stored_serial_still_binds(self, padding):
        """Migration 0012 canonicalized existing rows, but another tool can write padding again."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        padded = self._make_device(f"padded-101-{len(padding)}", padding)
        padded.refresh_from_db()
        assert padded.serial == padding

        result = validate_device_for_import(
            {"device_id": 99997, "hostname": "new-host-101c", "serial": "PAD123"},
            include_vc_detection=False,
        )

        assert result["existing_device"] == padded
        assert result["existing_match_type"] == "serial"
        assert result["can_import"] is False

    def test_the_cached_row_refresh_binds_a_padded_stored_serial_too(self):
        """The refresh re-check keeps the breadth of validate_device_for_import, padding included.

        _refresh_existing_device lives in import_utils/bulk_import.py, whose primary home is
        test_coverage_bulk_import.py; it is exercised here so both serial lookups stay in step.
        """
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        padded = self._make_device("padded-refresh-101", "  PADR123  ")
        validation = {
            "device_id": 99996,
            "hostname": "new-host-101d",
            "import_as_vm": False,
            "existing_device": None,
            "existing_match_type": None,
            "existing_librenms_link": None,
            "is_ready": True,
            "can_import": True,
            "issues": [],
            "warnings": [],
            "device_role": {"found": True, "role": padded.role, "available_roles": []},
            "cluster": {"found": False, "cluster": None, "available_clusters": []},
            "site": {"found": True, "site": padded.site},
            "device_type": {"found": True, "device_type": padded.device_type},
        }

        _refresh_existing_device(
            validation,
            libre_device={"device_id": 99996, "hostname": "new-host-101d", "serial": "PADR123"},
            server_key="default",
        )

        assert validation["existing_device"] == padded
        assert validation["existing_match_type"] == "serial"
        assert validation["can_import"] is False

    def test_unique_serial_still_binds(self):
        """A single device with the serial still binds via the serial path (guards against the unique guard over-rejecting)."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        dev = self._make_device("solo-101", "SOLO123")

        result = validate_device_for_import(
            {"device_id": 99998, "hostname": "new-host-101b", "serial": "SOLO123"},
            include_vc_detection=False,
        )

        assert result["existing_device"] == dev
        assert result["existing_match_type"] == "serial"


@pytest.mark.django_db
class TestSerialNumberMatching:
    """Test serial number matching in device validation, against real Device/VM rows."""

    def test_serial_match_blocks_import(self):
        """Device with matching serial (but a different hostname) blocks import via the serial branch."""
        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("existing-device", serial="ABC123")

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "new-hostname", "serial": "ABC123"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["can_import"] is False
        assert result["existing_match_type"] == "serial"
        assert result["existing_device"] == existing

    def test_serial_match_same_hostname_offers_link(self):
        """Matching name and serial finds the device by hostname and offers its unlinked row for linking."""
        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("switch-01", serial="ABC123")

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "ABC123"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_device"] == existing
        assert result["existing_match_type"] == "hostname"
        assert "not linked to LibreNMS" in result["warnings"][0]

    # The serial-match-reinstall case (differing hostname, no OOB signal, no link → hostname_differs)
    # is covered end-to-end against real Device rows by
    # TestOOBDetection.test_serial_match_reinstall_no_oob_signal_yields_hostname_differs in
    # test_coverage_device_operations.py — the mock-ORM duplicate here was removed.

    def test_hostname_match_diff_serial_offers_update(self):
        """Hostname matches but serial differs offers update_serial action."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("switch-01", serial="OLD_SERIAL")

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "NEW_SERIAL"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["serial_action"] == "update_serial"
        assert result["existing_match_type"] == "hostname"
        assert "Hardware may have been replaced" in result["warnings"][0]

    def test_serial_dash_ignored(self):
        """Serial '-' is not treated as a match (empty DB → nothing matches)."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "-"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] is None
        assert result["serial_action"] is None

    def test_serial_empty_ignored(self):
        """Empty serial skips serial matching."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": ""}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] is None
        assert result["serial_action"] is None

    def test_serial_none_ignored(self):
        """None serial skips serial matching."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": None}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] is None
        assert result["serial_action"] is None

    def test_hostname_match_serial_conflict_warns(self):
        """Hostname matches, incoming serial already on another device warns about conflict."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("switch-01", serial="OLD_SERIAL")
        owner = make_device("other-device", serial="CONFLICTING_SERIAL")

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "CONFLICTING_SERIAL"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["serial_action"] == "conflict"
        assert result["existing_match_type"] == "hostname"
        # The owner's identity travels as structured data, never in a warning: only the display
        # gate may name it, and only to a viewer who may see it.
        assert result["serial_conflict"] == {"pk": owner.pk, "serial": "CONFLICTING_SERIAL", "phase": "importing"}
        assert not any("Serial conflict" in warning for warning in result["warnings"])

    def test_librenms_id_match_shows_serial_confirmed(self):
        """librenms_id match with matching serial shows confirmation."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("switch-01", serial="ABC123", librenms_cf={"default": {"id": 1}})

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "sysName": "switch-01", "serial": "ABC123"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] == "librenms_id"
        assert result["can_import"] is False
        assert result["serial_confirmed"] is True
        assert result["name_matches"] is True

    def test_librenms_id_match_detects_serial_drift(self):
        """librenms_id match with different serial warns about drift."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("switch-01", serial="OLD_SERIAL", librenms_cf={"default": {"id": 1}})

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "NEW_SERIAL"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] == "librenms_id"
        assert result["serial_action"] == "update_serial"
        assert any("Hardware may have been replaced" in w for w in result["warnings"])

    def test_librenms_id_match_still_validates_site(self):
        """librenms_id match continues to populate site/type validation."""
        from dcim.models import DeviceType, Manufacturer, Site

        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("switch-01", serial="", librenms_cf={"default": {"id": 1}})
        site, _ = Site.objects.get_or_create(name="DC1", slug="dc1")
        manufacturer, _ = Manufacturer.objects.get_or_create(name="Validation Mfr", slug="validation-mfr")
        device_type, _ = DeviceType.objects.get_or_create(
            manufacturer=manufacturer,
            model="WS-C4900M",
            slug="ws-c4900m",
        )

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "location": "DC1", "hardware": "WS-C4900M"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] == "librenms_id"
        assert result["can_import"] is False
        assert result["is_ready"] is False
        # Site and device_type should still be populated
        assert result["site"]["found"] is True
        assert result["site"]["site"] == site
        assert result["device_type"]["found"] is True
        assert result["device_type"]["device_type"] == device_type

    def test_existing_device_role_populated(self):
        """Existing device's role should be shown in validation details."""
        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("switch-01", serial="ABC123")

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "serial": "ABC123",
            "location": "",
            "hardware": "",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_device"] == existing
        assert result["device_role"]["found"] is True
        assert result["device_role"]["role"] == existing.role

    def test_device_type_mismatch_flagged(self):
        """Device type mismatch between existing device and LibreNMS should be flagged."""
        from dcim.models import DeviceType, Manufacturer

        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("switch-01", serial="ABC123")
        mfr, _ = Manufacturer.objects.get_or_create(name="MismatchMfr", slug="mismatch-mfr")
        librenms_device_type, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="New Type", slug="new-type")
        assert librenms_device_type.pk != existing.device_type.pk

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "serial": "ABC123",
            "location": "",
            "hardware": librenms_device_type.model,
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["device_type_mismatch"] is True
        assert any("Device type mismatch" in w for w in result["warnings"])

    def test_no_device_type_mismatch_when_types_match(self):
        """No mismatch flag when existing device type matches LibreNMS."""
        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("switch-01", serial="ABC123")

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "serial": "ABC123",
            "location": "",
            "hardware": existing.device_type.model,
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["device_type_mismatch"] is False


@pytest.mark.django_db
class TestNameMatchesWithNamingPreferences:
    """Exercise naming preferences against real devices and virtual chassis."""

    @pytest.mark.parametrize(
        ("device_data", "options", "expected_name"),
        [
            (
                {"hostname": "switch.example.test", "sysName": "switch.example.test"},
                {"strip_domain": True},
                "switch",
            ),
            (
                {"hostname": "hostname-value", "sysName": "sysname-value"},
                {"use_sysname": False},
                "hostname-value",
            ),
        ],
    )
    def test_resolved_name_matches_real_device(self, device_data, options, expected_name):
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device(expected_name, serial="NAMING-SERIAL", librenms_cf={"default": {"id": 42}})
        device_data.update({"device_id": 42, "serial": existing.serial})

        result = validate_device_for_import(device_data, include_vc_detection=False, **options)

        assert result["existing_device"] == existing
        assert result["resolved_name"] == expected_name
        assert result["name_matches"] is True
        assert result["name_sync_available"] is False

    def test_name_mismatch_offers_real_device_rename(self):
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("old-name", serial="RENAME-SERIAL", librenms_cf={"default": {"id": 43}})

        result = validate_device_for_import(
            {
                "device_id": 43,
                "hostname": "new-name",
                "sysName": "new-name",
                "serial": existing.serial,
            },
            include_vc_detection=False,
        )

        assert result["existing_device"] == existing
        assert result["name_matches"] is False
        assert result["name_sync_available"] is True
        assert result["suggested_name"] == "new-name"

    @pytest.mark.parametrize(("existing_name_matches", "strip_domain"), [(True, False), (True, True), (False, False)])
    def test_virtual_chassis_member_name_uses_real_position(self, existing_name_matches, strip_domain):
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

        _virtual_chassis, members = make_virtual_chassis_members("naming", count=2)
        existing = members[1]
        master_name = "stack.example.test" if strip_domain else "stack"
        resolved_master_name = "stack"
        expected_name = _generate_vc_member_name(resolved_master_name, existing.vc_position, serial="VC-NAMING")
        existing.name = expected_name if existing_name_matches else "wrong-name"
        existing.serial = "VC-NAMING"
        existing.custom_field_data["librenms_id"] = {"default": {"id": 44}}
        existing.save()

        result = validate_device_for_import(
            {
                "device_id": 44,
                "hostname": master_name,
                "sysName": master_name,
                "serial": existing.serial,
            },
            include_vc_detection=False,
            strip_domain=strip_domain,
        )

        assert result["name_matches"] is existing_name_matches
        assert result["name_sync_available"] is (not existing_name_matches)
        if not existing_name_matches:
            assert result["suggested_name"] == expected_name

    @pytest.mark.parametrize(
        ("use_sysname", "hostname", "sysname", "expected_source"),
        [
            (True, "router-host", "router-system", "sysname"),
            (False, "router-host", "router-system", "hostname"),
            (False, "", "router-system", "sysname"),
            (False, "", "", "device-99"),
        ],
    )
    def test_naming_criteria_records_real_resolution(self, use_sysname, hostname, sysname, expected_source):
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        result = validate_device_for_import(
            {"device_id": 99, "hostname": hostname, "sysName": sysname},
            include_vc_detection=False,
            use_sysname=use_sysname,
        )

        assert result["naming_criteria"] == {
            "use_sysname": use_sysname,
            "strip_domain": False,
            "raw_sysname": sysname,
            "raw_hostname": hostname,
            "source": expected_source,
        }


@pytest.mark.django_db
class TestLegacyLibreNMSIdMigration:
    """Exercise legacy identity detection and migration with real devices."""

    @pytest.mark.parametrize(
        ("stored_value", "serial", "expected_migration", "expected_confirmed"),
        [
            (42, "LEGACY-SERIAL", True, True),
            (42, "", True, False),
            ({"default": {"id": 42}}, "LEGACY-SERIAL", False, True),
        ],
    )
    def test_validation_reports_identity_format(self, stored_value, serial, expected_migration, expected_confirmed):
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.tests.conftest import make_device

        existing = make_device("legacy-device", serial=serial, librenms_cf=stored_value)

        result = validate_device_for_import(
            {"device_id": 42, "hostname": existing.name, "serial": serial},
            include_vc_detection=False,
        )

        assert result["existing_device"] == existing
        assert result["existing_match_type"] == "librenms_id"
        assert result["librenms_id_needs_migration"] is expected_migration
        assert result["serial_confirmed"] is expected_confirmed

    @pytest.mark.parametrize(
        ("stored_value", "expected_changed", "expected_value"),
        [
            (42, True, {"primary": 42}),
            (" 42 ", True, {"primary": 42}),
            ({"primary": 42}, False, {"primary": 42}),
            (None, False, None),
            ("abc", False, "abc"),
        ],
    )
    def test_migration_helper_updates_real_shape(self, stored_value, expected_changed, expected_value):
        from types import SimpleNamespace

        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        custom_field_data = {} if stored_value is None else {"librenms_id": stored_value}
        obj = SimpleNamespace(custom_field_data=custom_field_data)

        assert migrate_legacy_librenms_id(obj, "primary") is expected_changed
        assert obj.custom_field_data.get("librenms_id") == expected_value

    def test_migration_gate_and_writer_agree(self):
        from types import SimpleNamespace

        from netbox_librenms_plugin.utils import is_legacy_librenms_id, migrate_legacy_librenms_id

        obj = SimpleNamespace(custom_field_data={"librenms_id": " 42 "})

        assert is_legacy_librenms_id(obj.custom_field_data["librenms_id"]) is True
        assert migrate_legacy_librenms_id(obj, "primary") is True
        assert obj.custom_field_data["librenms_id"] == {"primary": 42}


@pytest.mark.django_db
class TestBuildSyncInfo:
    """Build sync state from real NetBox objects and matchers."""

    @staticmethod
    def _device(*, serial="ABC123", platform=None):
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("sync-info-device", serial=serial)
        device.platform = platform
        device.save()
        return device

    def test_all_synced(self):
        from dcim.models import Platform

        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        platform = Platform.objects.create(name="Sync OS", slug="sync-os")
        existing = self._device(platform=platform)

        result = DeviceValidationDetailsView._build_sync_info(
            {"serial": existing.serial, "os": platform.name, "hardware": existing.device_type.model},
            existing,
        )

        assert result["all_synced"] is True
        assert result["serial_synced"] is True
        assert result["platform_synced"] is True
        assert result["device_type_synced"] is True

    def test_serial_out_of_sync(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        existing = self._device(serial="OLD123")

        result = DeviceValidationDetailsView._build_sync_info(
            {"serial": "NEW456", "os": "-", "hardware": "-"},
            existing,
        )

        assert result["serial_synced"] is False
        assert result["all_synced"] is False

    def test_platform_out_of_sync(self):
        from dcim.models import Platform

        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        old_platform = Platform.objects.create(name="Old OS", slug="old-os")
        new_platform = Platform.objects.create(name="New OS", slug="new-os")
        existing = self._device(platform=old_platform)

        result = DeviceValidationDetailsView._build_sync_info(
            {"serial": existing.serial, "os": new_platform.name, "hardware": "-"},
            existing,
        )

        assert result["platform_synced"] is False
        assert result["all_synced"] is False

    @pytest.mark.parametrize(("existing_platform", "os_name"), [(True, "Missing OS"), (False, "Present OS")])
    def test_platform_sync_state_is_boolean(self, existing_platform, os_name):
        from dcim.models import Platform

        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        matched_platform = Platform.objects.create(name="Present OS", slug="present-os")
        existing = self._device(platform=matched_platform if existing_platform else None)

        result = DeviceValidationDetailsView._build_sync_info(
            {"serial": existing.serial, "os": os_name, "hardware": "-"},
            existing,
        )

        assert result["platform_synced"] is False
        assert isinstance(result["platform_synced"], bool)

    def test_unmatched_hardware_is_out_of_sync(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        existing = self._device()

        result = DeviceValidationDetailsView._build_sync_info(
            {"serial": existing.serial, "os": "-", "hardware": "Unknown Hardware"},
            existing,
        )

        assert result["device_type_synced"] is False
        assert result["all_synced"] is False


@pytest.mark.django_db
class TestProcessDeviceFilters:
    """Exercise filtered imports through the real client, cache, and validator."""

    @pytest.fixture(autouse=True)
    def clear_import_cache(self):
        from django.core.cache import cache

        cache.clear()
        yield
        cache.clear()

    @pytest.mark.parametrize(
        ("show_disabled", "devices", "expected_names"),
        [
            (
                False,
                [
                    {"device_id": 1, "hostname": "enabled", "disabled": 0, "status": 1},
                    {"device_id": 2, "hostname": "disabled", "disabled": 1, "status": 1},
                ],
                ["enabled"],
            ),
            (
                False,
                [
                    {"device_id": 3, "hostname": "unreachable", "disabled": 0, "status": 0},
                    {"device_id": 4, "hostname": "disabled", "disabled": 1, "status": 0},
                ],
                ["unreachable"],
            ),
            (
                True,
                [
                    {"device_id": 5, "hostname": "enabled", "disabled": 0, "status": 1},
                    {"device_id": 6, "hostname": "disabled", "disabled": 1, "status": 0},
                ],
                ["enabled", "disabled"],
            ),
        ],
    )
    def test_disabled_filtering_uses_real_http_rows(
        self,
        settings,
        librenms_server,
        show_disabled,
        devices,
        expected_names,
    ):
        from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

        librenms_server.register(
            "/api/v0/devices",
            {"status": "ok", "devices": devices},
            method="GET",
        )
        api = _real_librenms_api(settings, librenms_server)

        result = process_device_filters(
            api,
            filters={},
            vc_detection_enabled=False,
            clear_cache=True,
            show_disabled=show_disabled,
        )

        assert [device["hostname"] for device in result] == expected_names
        assert all("_validation" in device for device in result)

    def test_empty_return_helper(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _empty_return

        assert _empty_return(True) == ([], False)
        assert _empty_return(False) == []


@pytest.mark.django_db
class TestVCPositionHandling:
    """Test VC position normalization and suggested name generation."""

    def test_clone_vc_data_position_fallback_is_one_based(self):
        """_clone_virtual_chassis_data fallback must be 1-based (idx+1, not idx)."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _clone_virtual_chassis_data

        data = {"is_stack": True, "member_count": 2, "members": [{"serial": "S1"}, {"serial": "S2"}]}
        result = _clone_virtual_chassis_data(data)
        positions = [m["position"] for m in result["members"]]
        # First member: idx=0 → position should be 1, not 0
        assert positions[0] == 1
        assert positions[1] == 2

    def test_clone_vc_data_preserves_explicit_positions(self):
        """_clone_virtual_chassis_data must preserve explicitly set positions."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _clone_virtual_chassis_data

        data = {
            "is_stack": True,
            "member_count": 2,
            "members": [{"serial": "S1", "position": 3}, {"serial": "S2", "position": 5}],
        }
        result = _clone_virtual_chassis_data(data)
        assert result["members"][0]["position"] == 3
        assert result["members"][1]["position"] == 5

    def test_clone_vc_data_bad_position_falls_back_to_one_based(self):
        """_clone_virtual_chassis_data falls back to idx+1 for non-int position."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _clone_virtual_chassis_data

        data = {
            "is_stack": True,
            "member_count": 2,
            "members": [{"serial": "S1", "position": "bad"}, {"serial": "S2", "position": None}],
        }
        result = _clone_virtual_chassis_data(data)
        # idx=0 → fallback 1, idx=1 → fallback 2
        assert result["members"][0]["position"] == 1
        assert result["members"][1]["position"] == 2

    def test_suggested_name_uses_position_directly(self):
        """Suggested virtual-chassis member names use the already one-based position without incrementing it."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        # position=1 should produce name with "1", not "2"
        name = _generate_vc_member_name("switch-1", 1, pattern="-M{position}")
        assert name == "switch-1-M1", f"Expected 'switch-1-M1', got '{name}'"

        # position=2 should produce "2", not "3"
        name = _generate_vc_member_name("switch-1", 2, pattern="-M{position}")
        assert name == "switch-1-M2", f"Expected 'switch-1-M2', got '{name}'"

    @pytest.mark.parametrize(
        ("master_name", "positions", "expected_names"),
        [
            ("switch-01", [1, 2], ["switch-01-M1", "switch-01-M2"]),
            ("router", [3], ["router-M3"]),
            ("sw", [0, -1], ["sw-M1", "sw-M2"]),
        ],
    )
    def test_update_vc_member_suggested_names_uses_real_settings(
        self,
        master_name,
        positions,
        expected_names,
    ):
        from netbox_librenms_plugin.import_utils.virtual_chassis import update_vc_member_suggested_names
        from netbox_librenms_plugin.models import LibreNMSSettings

        settings_row = LibreNMSSettings.objects.order_by("pk").first()
        if settings_row is None:
            settings_row = LibreNMSSettings.objects.create()
        settings_row.vc_member_name_pattern = "-M{position}"
        settings_row.save()
        vc_data = {
            "is_stack": True,
            "member_count": len(positions),
            "members": [
                {"serial": f"SERIAL-{index}", "position": position} for index, position in enumerate(positions, start=1)
            ],
        }

        result = update_vc_member_suggested_names(vc_data, master_name)

        assert [member["suggested_name"] for member in result["members"]] == expected_names
        assert [member["position"] for member in result["members"]] == [
            position if position > 0 else index for index, position in enumerate(positions, start=1)
        ]


# ---------------------------------------------------------------------------
# Additional virtual_chassis.py coverage
# ---------------------------------------------------------------------------


class TestEmptyVirtualChassisData:
    """Tests for empty_virtual_chassis_data helper."""

    def test_returns_expected_structure(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import empty_virtual_chassis_data

        result = empty_virtual_chassis_data()
        assert result["is_stack"] is False
        assert result["member_count"] == 0
        assert result["members"] == []
        assert result["detection_error"] is None

    def test_returns_new_dict_each_call(self):
        """Each call returns an independent dict (not a shared reference)."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import empty_virtual_chassis_data

        a = empty_virtual_chassis_data()
        b = empty_virtual_chassis_data()
        a["members"].append("x")
        assert b["members"] == []


class TestCloneVirtualChassisDataAdditional:
    """Additional _clone_virtual_chassis_data edge cases."""

    def test_none_input_returns_empty(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _clone_virtual_chassis_data

        result = _clone_virtual_chassis_data(None)
        assert result["is_stack"] is False
        assert result["members"] == []

    def test_empty_dict_returns_empty(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _clone_virtual_chassis_data

        result = _clone_virtual_chassis_data({})
        assert result["is_stack"] is False
        assert result["members"] == []

    def test_full_data_defensive_copy(self):
        """Members list is a new list; mutating it does not affect the source."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _clone_virtual_chassis_data

        data = {
            "is_stack": True,
            "member_count": 1,
            "members": [{"serial": "SN1", "position": 1}],
            "detection_error": None,
        }
        result = _clone_virtual_chassis_data(data)
        result["members"].append({"serial": "SN-NEW", "position": 2})
        assert len(data["members"]) == 1  # original untouched

    def test_detection_error_preserved(self):
        """detection_error field from source data is preserved."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _clone_virtual_chassis_data

        data = {
            "is_stack": True,
            "member_count": 1,
            "members": [],
            "detection_error": "Some error",
        }
        result = _clone_virtual_chassis_data(data)
        assert result["detection_error"] == "Some error"

    def test_member_with_zero_position_replaced_by_one_based(self):
        """A member with position=0 is replaced by idx+1 (1-based)."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _clone_virtual_chassis_data

        data = {
            "is_stack": True,
            "member_count": 2,
            "members": [{"serial": "S0", "position": 0}, {"serial": "S2", "position": 2}],
        }
        result = _clone_virtual_chassis_data(data)
        assert result["members"][0]["position"] == 1  # 0 → idx+1 = 1
        assert result["members"][1]["position"] == 2  # kept as-is

    def test_member_count_falls_back_to_len_when_zero(self):
        """member_count=0 in source is replaced by len(members)."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _clone_virtual_chassis_data

        data = {
            "is_stack": True,
            "member_count": 0,
            "members": [{"serial": "S1", "position": 1}, {"serial": "S2", "position": 2}],
        }
        result = _clone_virtual_chassis_data(data)
        assert result["member_count"] == 2


class TestVCCacheKey:
    """Build VC cache keys from real-shape values."""

    @pytest.mark.parametrize(
        ("server_key", "device_id", "expected_parts"),
        [
            ("default", 42, ["default", "42"]),
            ("server-a", 1, ["server-a", "1"]),
        ],
    )
    def test_cache_key_contains_scope(self, server_key, device_id, expected_parts):
        from types import SimpleNamespace

        from netbox_librenms_plugin.import_utils.virtual_chassis import _vc_cache_key

        key = _vc_cache_key(SimpleNamespace(server_key=server_key), device_id)

        assert "librenms_vc_detection" in key
        assert all(part in key for part in expected_parts)

    def test_cache_key_changes_with_server_and_device(self):
        from types import SimpleNamespace

        from netbox_librenms_plugin.import_utils.virtual_chassis import _vc_cache_key

        server_a = SimpleNamespace(server_key="server-a")
        server_b = SimpleNamespace(server_key="server-b")

        assert _vc_cache_key(server_a, 1) != _vc_cache_key(server_b, 1)
        assert _vc_cache_key(server_a, 1) != _vc_cache_key(server_a, 2)
        assert "default" in _vc_cache_key(object(), 1)


@pytest.mark.django_db
class TestVirtualChassisHTTPIntegration:
    """Exercise VC detection and caching through the real LibreNMS client."""

    @pytest.fixture(autouse=True)
    def clear_vc_cache(self):
        from django.core.cache import cache

        cache.clear()
        yield
        cache.clear()

    @staticmethod
    def _stack_root():
        return {"entPhysicalClass": "stack", "entPhysicalIndex": 100}

    @staticmethod
    def _members():
        return [
            {
                "entPhysicalClass": "chassis",
                "entPhysicalIndex": 201,
                "entPhysicalParentRelPos": 2,
                "entPhysicalSerialNum": "MEMBER-2",
                "entPhysicalModelName": "Model 2",
                "entPhysicalName": "Member 2",
                "entPhysicalDescr": "Second member",
            },
            {
                "entPhysicalClass": "chassis",
                "entPhysicalIndex": 200,
                "entPhysicalParentRelPos": 1,
                "entPhysicalSerialNum": "MEMBER-1",
                "entPhysicalModelName": "Model 1",
                "entPhysicalName": "Member 1",
                "entPhysicalDescr": "First member",
            },
        ]

    def test_missing_inputs_return_empty_state(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        assert get_virtual_chassis_data(None, 1)["is_stack"] is False
        assert get_virtual_chassis_data(object(), None)["is_stack"] is False

    def test_detects_stack_from_real_http_inventory(self, settings, librenms_server):
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        device_id = 42
        librenms_server.device_info_response(device_id=device_id, hostname="stack-master", serial="MEMBER-1")
        librenms_server.vc_inventory_callable(device_id, [self._stack_root()], {100: self._members()})
        api = _real_librenms_api(settings, librenms_server)

        result = get_virtual_chassis_data(api, device_id)

        assert result["is_stack"] is True
        assert result["member_count"] == 2
        assert [member["position"] for member in result["members"]] == [1, 2]
        assert result["members"][0]["is_master"] is True
        assert result["members"][0]["suggested_name"] == "stack-master-M1"

    def test_negative_result_is_cached_until_forced_refresh(self, settings, librenms_server):
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        device_id = 43
        librenms_server.device_info_response(device_id=device_id, hostname="cache-master")
        librenms_server.vc_inventory_callable(device_id, [], {})
        api = _real_librenms_api(settings, librenms_server)

        first = get_virtual_chassis_data(api, device_id)
        librenms_server.vc_inventory_callable(device_id, [self._stack_root()], {100: self._members()})
        cached = get_virtual_chassis_data(api, device_id)
        refreshed = get_virtual_chassis_data(api, device_id, force_refresh=True)

        assert first["is_stack"] is False
        assert cached["is_stack"] is False
        assert refreshed["is_stack"] is True

    def test_prefetch_populates_real_cache_for_each_device(self, settings, librenms_server):
        from netbox_librenms_plugin.import_utils.virtual_chassis import (
            get_virtual_chassis_data,
            prefetch_vc_data_for_devices,
        )

        device_ids = [44, 45]
        for device_id in device_ids:
            librenms_server.device_info_response(device_id=device_id, hostname=f"device-{device_id}")
            librenms_server.vc_inventory_callable(device_id, [], {})
        api = _real_librenms_api(settings, librenms_server)

        prefetch_vc_data_for_devices(api, device_ids)
        for device_id in device_ids:
            librenms_server.vc_inventory_callable(device_id, [self._stack_root()], {100: self._members()})

        assert [get_virtual_chassis_data(api, device_id)["is_stack"] for device_id in device_ids] == [False, False]


@pytest.mark.django_db
class TestLoadVCMemberNamePattern:
    """Load the naming pattern from the real settings table."""

    def test_returns_stored_pattern(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.all().delete()
        LibreNMSSettings.objects.create(vc_member_name_pattern="-SW{position}")

        assert _load_vc_member_name_pattern() == "-SW{position}"

    def test_no_settings_returns_default(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.all().delete()

        assert _load_vc_member_name_pattern() == "-M{position}"


@pytest.mark.django_db
class TestGenerateVCMemberNameAdditional:
    """Additional tests for _generate_vc_member_name."""

    def test_with_serial_in_pattern(self):
        """Pattern using {serial} placeholder substitutes the serial number."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        name = _generate_vc_member_name("switch-1", 2, serial="ABC123", pattern=" [{serial}]")
        assert name == "switch-1 [ABC123]"

    def test_empty_serial_produces_empty_brackets(self):
        """Empty serial with {serial} pattern results in empty brackets."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        name = _generate_vc_member_name("switch-1", 1, serial="", pattern=" [{serial}]")
        assert name == "switch-1 []"

    def test_invalid_placeholder_falls_back_to_default(self):
        """A KeyError from an unknown placeholder triggers the '-M{position}' fallback."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        name = _generate_vc_member_name("switch-1", 3, pattern="-{nonexistent_key}")
        assert name == "switch-1-M3"

    def test_none_pattern_loads_from_settings(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.all().delete()
        LibreNMSSettings.objects.create(vc_member_name_pattern="-node-{position}")

        assert _generate_vc_member_name("router", 5, pattern=None) == "router-node-5"

    def test_master_name_placeholder(self):
        """Pattern can also reference {master_name}."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        name = _generate_vc_member_name("sw", 2, pattern="-{master_name}-pos{position}")
        assert name == "sw-sw-pos2"


@pytest.mark.django_db
class TestUpdateVCMemberSuggestedNamesAdditional:
    """Additional tests for update_vc_member_suggested_names."""

    def test_not_stack_returns_vc_data_unchanged(self):
        """When is_stack=False, the function returns immediately without modifying members."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import update_vc_member_suggested_names

        vc_data = {
            "is_stack": False,
            "members": [{"serial": "S1", "position": 1, "suggested_name": "old-name"}],
        }
        result = update_vc_member_suggested_names(vc_data, "sw")
        # suggested_name must not be regenerated
        assert result["members"][0]["suggested_name"] == "old-name"

    def test_none_vc_data_returns_none(self):
        """None input is returned as-is (falsy guard)."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import update_vc_member_suggested_names

        result = update_vc_member_suggested_names(None, "sw")
        assert result is None

    def test_no_members_returns_empty_members(self):
        """is_stack=True with empty members list processes without error."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import update_vc_member_suggested_names

        vc_data = {"is_stack": True, "members": []}
        result = update_vc_member_suggested_names(vc_data, "sw")

        assert result["members"] == []


@pytest.mark.django_db
class TestCreateVirtualChassisWithMembers:
    """Create complete virtual chassis state in the real database."""

    def test_creates_chassis_and_assigns_master(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.tests.conftest import make_device

        master = make_device("vc-master", serial="MASTER-SERIAL")

        virtual_chassis = create_virtual_chassis_with_members(
            master,
            [],
            {"device_id": 101},
            server_key="test-server",
        )

        master.refresh_from_db()
        virtual_chassis.refresh_from_db()
        assert virtual_chassis.name == "vc-master"
        assert virtual_chassis.domain == "librenms-test-server-101"
        assert virtual_chassis.master == master
        assert master.virtual_chassis == virtual_chassis
        assert master.vc_position == 1
        assert master.name == "vc-master-M1"

    def test_creates_real_member_devices_at_discovered_positions(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.tests.conftest import make_device

        master = make_device("stack-master", serial="MASTER-SERIAL")
        members = [
            {"serial": "MASTER-SERIAL", "position": 1, "name": "Master", "is_master": True},
            {"serial": "MEMBER-SERIAL", "position": 2, "name": "Member"},
        ]

        virtual_chassis = create_virtual_chassis_with_members(
            master,
            members,
            {"device_id": 102},
            server_key="test-server",
        )

        created_members = list(virtual_chassis.members.order_by("vc_position"))
        assert [(device.name, device.serial, device.vc_position) for device in created_members] == [
            ("stack-master-M1", "MASTER-SERIAL", 1),
            ("stack-master-M2", "MEMBER-SERIAL", 2),
        ]
        assert virtual_chassis.master_id == master.pk


@pytest.mark.django_db
class TestSyncModuleBayCounter:
    """Synchronize the denormalized module-bay count with real rows."""

    @pytest.mark.parametrize("stored_count", [0, 2])
    def test_syncs_or_preserves_counter(self, stored_count):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.virtual_chassis import _sync_module_bay_counter
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

        device = make_device_with_module_bays(
            f"bay-counter-{stored_count}",
            ["Bay 1", "Bay 2"],
        )
        Device.objects.filter(pk=device.pk).update(module_bay_count=stored_count)
        device.refresh_from_db()

        _sync_module_bay_counter(device)

        device.refresh_from_db()
        assert device.module_bay_count == 2


class TestBuildIdServerInfo:
    """Test DeviceValidationDetailsView._build_id_server_info method."""

    def _make_device(self, librenms_id_value):
        from types import SimpleNamespace

        return SimpleNamespace(custom_field_data={"librenms_id": librenms_id_value})

    def test_returns_none_for_legacy_int(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = self._make_device(42)
        result = DeviceValidationDetailsView._build_id_server_info(device)
        assert result is None

    def test_returns_none_for_missing_cf(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = self._make_device(None)
        result = DeviceValidationDetailsView._build_id_server_info(device)
        assert result is None

    def test_single_server_resolves_display_name(self, settings):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = self._make_device({"production": 42})
        plugins_cfg = {
            "netbox_librenms_plugin": {
                "servers": {
                    "production": {"display_name": "Production LibreNMS", "librenms_url": "https://prod.example.com"},
                }
            }
        }
        settings.PLUGINS_CONFIG = plugins_cfg
        result = DeviceValidationDetailsView._build_id_server_info(device)

        assert result is not None
        assert len(result) == 1
        assert result[0]["server_key"] == "production"
        assert result[0]["display_name"] == "Production LibreNMS"
        assert result[0]["device_id"] == 42

    def test_unconfigured_server_uses_key_as_display_name(self, settings):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = self._make_device({"deleted-server": 77})
        plugins_cfg = {"netbox_librenms_plugin": {"servers": {}}}
        settings.PLUGINS_CONFIG = plugins_cfg
        result = DeviceValidationDetailsView._build_id_server_info(device)

        assert result is not None
        assert result[0]["display_name"] == "deleted-server"

    def test_empty_dict_returns_none(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = self._make_device({})
        result = DeviceValidationDetailsView._build_id_server_info(device)
        assert result is None


# ---------------------------------------------------------------------------
# Tests for _refresh_existing_device sys_name fallback fix
# ---------------------------------------------------------------------------


class TestRefreshExistingDeviceSysNameFallback:
    """Test that _refresh_existing_device tries sys_name even when hostname is empty."""

    @pytest.mark.django_db
    def test_sysname_used_when_hostname_empty(self):
        """When hostname is empty but sysName matches a REAL Device, the refresh binds to it."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("router-01")  # no librenms_id CF → find_by_librenms_id misses for real
        libre_device = {
            "device_id": 55,
            "hostname": "",  # empty hostname → falls through to sysName
            "sysName": "router-01",
            "serial": "SN-MATCH",
        }
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,
            "is_ready": False,
            "can_import": False,
            "issues": [],
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] == device

    @pytest.mark.django_db
    def test_hostname_lookup_succeeds_without_sysname(self):
        """When hostname matches a REAL Device, it binds before the (different) sysName is tried."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("sw-01")
        libre_device = {
            "device_id": 10,
            "hostname": "sw-01",
            "sysName": "sw-01-sysname",  # differs from hostname; hostname must win first
            "serial": "",
        }
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,
            "is_ready": False,
            "can_import": False,
            "issues": [],
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] == device


@pytest.mark.django_db
class TestRefreshExistingDeviceCrossModelIdWins:
    """An exact cross-model librenms_id owner must win over a same-named preferred-model object."""

    def test_cross_model_id_match_beats_same_name_device(self):
        """librenms_id belongs to a VM only (no Device owns it), but a same-named Device exists: the refresh must bind to the VM (the true id owner), not name-match the Device and silently re-home the row."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device, make_vm

        libre_id = 778899
        # The id's true owner is a VirtualMachine (the cross model for a device import).
        vm = make_vm("b3-shared-name")
        vm.custom_field_data["librenms_id"] = {"default": libre_id}
        vm.save()
        # A *different* Device shares the same name but holds no librenms_id link.
        device = make_device("b3-shared-name")

        libre_device = {"device_id": libre_id, "hostname": "b3-shared-name", "sysName": "b3-shared-name"}
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,  # Model=Device, CrossModel=VirtualMachine
            "is_ready": False,
            "can_import": False,
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # The exact id owner (the VM) must win over the same-named Device matched by hostname.
        # Compare objects, not .pk: VirtualMachine and Device IDs are table-local and can
        # legitimately coincide, so a pk equality check could pass/fail by accident.
        assert validation["existing_device"] == vm
        assert validation["existing_device"] != device
        # found_as_cross_model flips import_as_vm so future refreshes query the right model.
        assert validation["import_as_vm"] is True

    def test_cross_model_name_match_in_both_models_binds_neither(self):
        """When the name resolves to BOTH a Device and a VM (no id link, no serial/IP), the refresh binds neither and warns, mirroring the validator's cross-model hostname branch."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device, make_vm

        # Same name in both models, neither carrying a librenms_id link, no serial/IP identity.
        device = make_device("twin-name-host")
        make_vm("twin-name-host")
        libre_device = {"device_id": 4242, "hostname": "twin-name-host", "sysName": "twin-name-host"}
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,  # Model=Device, CrossModel=VirtualMachine
            "is_ready": False,
            "can_import": False,
            "issues": [],
            "warnings": [],
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # Pre-fix the preferred model (Device) was pinned by name; now neither is bound.
        assert validation["existing_device"] is None
        assert validation["existing_device"] != device
        assert any("Both a VM and Device exist with hostname" in w for w in validation.get("warnings", []))

    def test_cross_model_warning_not_duplicated_across_refreshes(self):
        """Repeated in-place refreshes keep only one cross-model ambiguity warning."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device, make_vm

        make_device("stack-warn-host")
        make_vm("stack-warn-host")
        libre_device = {"device_id": 4243, "hostname": "stack-warn-host", "sysName": "stack-warn-host"}
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,
            "is_ready": False,
            "can_import": False,
            "issues": [],
            "warnings": [],
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")
        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        cross_model = [w for w in validation["warnings"] if "Both a VM and Device exist with hostname" in w]
        assert len(cross_model) == 1

    def test_cross_model_warning_cleared_once_collision_resolved(self):
        """Resolving the VM/Device name collision must drop the cached warning on the next refresh."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device, make_vm

        device = make_device("resolved-warn-host")
        vm = make_vm("resolved-warn-host")
        libre_device = {"device_id": 4244, "hostname": "resolved-warn-host", "sysName": "resolved-warn-host"}
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,
            "is_ready": False,
            "can_import": False,
            "issues": [],
            "warnings": [],
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")
        assert any("Both a VM and Device exist with hostname" in w for w in validation["warnings"])

        vm.delete()
        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # The collision is gone: the stale warning must not survive the refresh, and the
        # remaining Device binds by name as usual.
        assert not any("Both a VM and Device exist with hostname" in w for w in validation["warnings"])
        assert validation["existing_device"] == device

    def test_vm_fresh_match_populates_cluster_display(self):
        """A newly matched VM exposes its actual cluster for the validation details template."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_cluster, make_vm

        cluster = make_cluster("fresh-vm-cluster")
        vm = make_vm("fresh-vm-clustered", cluster=cluster)
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": True,  # Model=VirtualMachine
            "is_ready": False,
            "can_import": False,
            "issues": [],
            "warnings": [],
            "cluster": {"found": False, "cluster": None, "available_clusters": []},
        }

        _refresh_existing_device(
            validation, libre_device={"device_id": 4245, "hostname": "fresh-vm-clustered"}, server_key="default"
        )

        assert validation["existing_device"] == vm
        assert validation["cluster"]["found"] is True
        assert validation["cluster"]["cluster"] == cluster
        # Existing-match gating stays force-blocked either way.
        assert validation["can_import"] is False
        assert validation["is_ready"] is False

    def test_vm_fresh_match_without_cluster_resets_stale_display(self):
        """A newly matched clusterless VM clears a stale cluster selection but keeps available clusters."""
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        vm = VirtualMachine.objects.create(name="fresh-vm-clusterless")
        stale = object()
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": True,
            "is_ready": False,
            "can_import": False,
            "issues": [],
            "warnings": [],
            "cluster": {"found": True, "cluster": stale, "available_clusters": ["keep-me"]},
        }

        _refresh_existing_device(
            validation, libre_device={"device_id": 4246, "hostname": "fresh-vm-clusterless"}, server_key="default"
        )

        assert validation["existing_device"] == vm
        assert validation["cluster"]["found"] is False
        assert validation["cluster"]["cluster"] is None
        assert validation["cluster"]["available_clusters"] == ["keep-me"]

    def test_serial_fallback_ambiguity_fails_closed(self):
        """When the serial fallback resolves more than one NetBox device, the refresh re-check must fail closed (ambiguous match + can_import False), not bind to an arbitrary duplicate."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("dup-serial-a", serial="DUPSERIAL1")
        make_device("dup-serial-b", serial="DUPSERIAL1")  # same serial, different device
        libre_device = {
            "device_id": 555,
            "hostname": "no-name-match",  # matches no device name → falls to serial fallback
            "sysName": "no-name-match",
            "serial": "DUPSERIAL1",
        }
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,
            "is_ready": True,
            "can_import": True,
            "issues": [],
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["can_import"] is False
        assert validation["existing_match_type"] == "ambiguous_hostname_or_serial"
        assert any("Multiple NetBox devices match" in i for i in validation.get("issues", []))

    def test_stale_serial_ip_ambiguity_blocker_cleared_on_refresh(self):
        """A cached serial/IP ambiguity blocker must be cleared on refresh re-check once the duplicate is resolved (now a single match), so the row isn't stuck blocked on the stale issue until cache expiry."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("now-unique-host", serial="UNIQSERIAL9")  # only ONE device has this serial now
        libre_device = {
            "device_id": 321,
            "hostname": "no-name-match",
            "sysName": "no-name-match",
            "serial": "UNIQSERIAL9",
        }
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,
            "is_ready": False,
            "can_import": False,
            # Stale ambiguity state cached from a prior refresh when the serial was duplicated.
            "existing_match_type": "ambiguous_hostname_or_serial",
            "issues": [
                "Multiple NetBox devices match this device's serial or management IP; resolve the duplicate before importing."
            ],
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # The stale ambiguity blocker is purged (the duplicate is gone — now a clean single match).
        assert not any("serial or management IP" in i for i in validation.get("issues", []))
        assert validation["existing_match_type"] != "ambiguous_hostname_or_serial"

    def test_stale_hostname_serial_ambiguity_blocker_cleared_on_refresh(self):
        """A cached 'hostname/serial' ambiguity blocker must also be cleared on refresh once resolved."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("now-unique-host2", serial="UNIQSERIALX")  # only ONE device has this serial now
        libre_device = {
            "device_id": 654,
            "hostname": "no-name-match2",
            "sysName": "no-name-match2",
            "serial": "UNIQSERIALX",
        }
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,
            "is_ready": False,
            "can_import": False,
            # Stale ambiguity state cached when the name/serial was duplicated — this wording is
            # emitted by validate_device_for_import's duplicate-name/serial guard, NOT the refresh
            # serial/IP fallback, so it does not contain the "serial or management IP" substring.
            "existing_match_type": "ambiguous_hostname_or_serial",
            "issues": [
                "Multiple NetBox devices share this device's hostname/serial; resolve the duplicate before importing or linking."
            ],
        }

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # The broadened marker set must strip the hostname/serial blocker too (the duplicate is gone).
        assert not any("hostname/serial" in i for i in validation.get("issues", []))
        assert validation["existing_match_type"] != "ambiguous_hostname_or_serial"


# ---------------------------------------------------------------------------
# Tests for _get_hostname_for_action helper
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGetHostnameForAction:
    """Resolve action names from cached state or a real request."""

    def test_returns_resolved_name_when_set(self):
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.imports.actions import _get_hostname_for_action

        request = RequestFactory().post("/device-import/")
        result = _get_hostname_for_action(
            request,
            {"resolved_name": "cached-name"},
            {"hostname": "raw-hostname", "sysName": "raw-sysname"},
        )

        assert result == "cached-name"

    def test_falls_back_to_request_naming_preferences(self):
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.imports.actions import _get_hostname_for_action

        request = RequestFactory().post(
            "/device-import/",
            {"use-sysname-toggle": "off", "strip-domain-toggle": "off"},
        )
        request.user = AnonymousUser()

        result = _get_hostname_for_action(
            request,
            {},
            {"hostname": "host.example.test", "sysName": "host"},
        )

        assert result == "host.example.test"


@pytest.mark.django_db
class TestResolveNamingPreferencesKeys:
    """Resolve naming toggles through real Django requests and preferences."""

    @pytest.mark.parametrize(
        ("method", "values", "expected"),
        [
            ("post", {"use-sysname-toggle": "on", "strip-domain-toggle": "off"}, (True, False)),
            ("post", {"use_sysname-toggle": "on", "strip_domain-toggle": "on"}, (True, True)),
            ("get", {"use-sysname-toggle": "off", "strip-domain-toggle": "on"}, (False, True)),
            ("post", {"use-sysname-toggle": "true", "strip-domain-toggle": "1"}, (True, True)),
            ("post", {"use-sysname-toggle": "no", "strip-domain-toggle": "false"}, (False, False)),
        ],
    )
    def test_request_values_take_precedence(self, method, values, expected):
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from netbox_librenms_plugin.utils import resolve_naming_preferences

        factory = RequestFactory()
        request = getattr(factory, method)("/device-import/", values)
        request.user = AnonymousUser()

        assert resolve_naming_preferences(request) == expected

    def test_real_user_preferences_are_used_without_toggles(self, django_user_model):
        from django.test import RequestFactory

        from netbox_librenms_plugin.utils import resolve_naming_preferences, save_user_pref

        user = django_user_model.objects.create_user(username="naming-preferences")
        request = RequestFactory().get("/device-import/")
        request.user = user
        save_user_pref(request, "plugins.netbox_librenms_plugin.use_sysname", False)
        save_user_pref(request, "plugins.netbox_librenms_plugin.strip_domain", True)

        assert resolve_naming_preferences(request) == (False, True)


class TestVCDomainStackDedup:
    """Test that bulk_import_devices_shared deduplicates VC creation by member serials."""

    def test_vc_domain_uses_member_serials(self):
        """vc_domain for two stack members with the same serials should be identical."""
        # The logic lives inline; test the produced key directly from vc_data
        members = [
            {"serial": "SN100", "position": 1},
            {"serial": "SN200", "position": 2},
        ]
        member_serials = sorted(m.get("serial") for m in members if m.get("serial"))
        vc_domain = f"librenms-stack-{','.join(member_serials)}"

        # Same members from a different device's perspective should produce the same key
        assert vc_domain == "librenms-stack-SN100,SN200"

    def test_vc_domain_fallback_to_device_id_when_no_serials(self):
        """When no member serials are available, device_id is used as fallback."""
        members = [
            {"position": 1},
            {"position": 2},
        ]
        member_serials = sorted(m.get("serial") for m in members if m.get("serial"))
        device_id = 42
        vc_domain = f"librenms-stack-{','.join(member_serials)}" if member_serials else f"librenms-{device_id}"
        assert vc_domain == "librenms-42"

    def test_different_stacks_produce_different_keys(self):
        """Two stacks with different serials produce distinct dedup keys."""
        members_a = [{"serial": "SN-A1"}, {"serial": "SN-A2"}]
        members_b = [{"serial": "SN-B1"}, {"serial": "SN-B2"}]
        key_a = f"librenms-stack-{','.join(sorted(m['serial'] for m in members_a))}"
        key_b = f"librenms-stack-{','.join(sorted(m['serial'] for m in members_b))}"
        assert key_a != key_b


@pytest.mark.django_db
class TestResolveSetPrimaryIp:
    """Resolve the primary-IP option from real requests and user preferences."""

    @pytest.mark.parametrize(
        ("method", "values", "expected"),
        [
            ("get", {}, False),
            ("post", {"set-primary-ip-toggle": "on"}, True),
            ("post", {"set-primary-ip-toggle": "off"}, False),
            ("post", {"set_primary_ip": "1"}, True),
            ("get", {"set-primary-ip-toggle": "true"}, True),
        ],
    )
    def test_request_cascade(self, method, values, expected):
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from netbox_librenms_plugin.utils import resolve_set_primary_ip

        request = getattr(RequestFactory(), method)("/sync/", values)
        request.user = AnonymousUser()

        assert resolve_set_primary_ip(request) is expected

    def test_real_user_preference_is_used_without_toggle(self, django_user_model):
        from django.test import RequestFactory

        from netbox_librenms_plugin.utils import resolve_set_primary_ip, save_user_pref

        user = django_user_model.objects.create_user(username="primary-ip-preference")
        request = RequestFactory().get("/sync/")
        request.user = user
        save_user_pref(request, "plugins.netbox_librenms_plugin.set_primary_ip", True)

        assert resolve_set_primary_ip(request) is True


class TestDeviceValidationDetailsTemplate:
    """The 'Full Sync Page' link in the import-validation panel must carry the active server_key so it opens on the same LibreNMS instance the user is validating against, not the session/default server."""

    def _source(self):
        from django.template.loader import get_template

        return get_template("netbox_librenms_plugin/htmx/device_validation_details.html").template.source

    def test_full_sync_link_includes_active_server_key(self):
        import re

        src = self._source()
        # Bind the conditional, url-encoded server_key to the SAME <a> that carries the "Full
        # Sync Page" text. Asserting the token appears *somewhere* in the source (the old check)
        # passes even when server_key is wired into a different href entirely. re.S so the match
        # spans the href attributes and the icon markup between the tag and the link text.
        # The tempered "(?:(?!</a>).)*?" forbids crossing a closing </a>, so server_key and the
        # link text must sit in ONE anchor — a plain ".*?" with re.S would happily bridge a
        # server_key in an earlier href to a "Full Sync Page" text in a later, unrelated <a>.
        assert re.search(
            r'<a\b[^>]*\bhref="[^"]*\?server_key=\{\{\s*server_key\s*\|\s*urlencode\s*\}\}[^"]*"[^>]*>'
            r"(?:(?!</a>).)*?Full Sync Page(?:(?!</a>).)*?</a>",
            src,
            re.S,
        )


@pytest.mark.django_db(transaction=True)
def test_device_and_vm_imports_serialize_one_librenms_id_claim(settings):
    """Concurrent Device and VM imports must leave one owner for a server-scoped ID."""
    from dcim.models import Device
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.import_utils.device_operations import import_single_device
    from netbox_librenms_plugin.import_utils.vm_operations import create_vm_from_librenms
    from netbox_librenms_plugin.tests.claim_race_helpers import run_librenms_id_claim_race
    from netbox_librenms_plugin.tests.conftest import _shared_infra, make_cluster
    from netbox_librenms_plugin.tests.import_server_helpers import configure_servers

    configure_servers(settings)
    site, _manufacturer, device_type, role = _shared_infra()
    cluster = make_cluster("claim-race-cluster")
    librenms_id = 61002

    def import_device():
        validation = {
            "existing_device": None,
            "resolved_name": "claim-race-device",
            "site": {"found": True, "site": site},
            "device_type": {"matched": True, "device_type": device_type},
            "device_role": {"found": True, "role": role},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }
        libre_device = {
            "device_id": librenms_id,
            "hostname": "claim-race-device",
            "sysName": "claim-race-device",
            "hardware": "TestDT",
            "serial": "-",
            "os": "-",
            "status": 1,
            "location": "",
        }
        result = import_single_device(
            librenms_id,
            server_key="primary",
            validation=validation,
            libre_device=libre_device,
            sync_options={"sync_interfaces": False, "sync_cables": False},
        )
        return result["success"]

    def import_vm():
        validation = {
            "can_import": True,
            "cluster": {"cluster": cluster},
            "platform": {"platform": None},
        }
        try:
            create_vm_from_librenms(
                {
                    "device_id": librenms_id,
                    "hostname": "claim-race-vm",
                    "_computed_name": "claim-race-vm",
                },
                validation,
                server_key="primary",
            )
        except ValueError as exc:
            assert "already assigned" in str(exc)
            return False
        return True

    outcomes, claim_keys = run_librenms_id_claim_race(import_device, import_vm)

    owners = list(Device.objects.filter(name="claim-race-device")) + list(
        VirtualMachine.objects.filter(name="claim-race-vm")
    )
    assert len(claim_keys) == 2
    assert len(set(claim_keys)) == 1
    assert sorted(outcomes) == [False, True]
    assert len(owners) == 1
    assert owners[0].custom_field_data["librenms_id"]["primary"] == librenms_id
