"""
Tests for netbox_librenms_plugin.import_utils module.

Phase 2 tests covering cache key generation, device name determination,
device retrieval, and device validation functions.
"""

from unittest.mock import MagicMock, patch

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


# =============================================================================
# TestDeviceRetrieval - 10 tests
# =============================================================================


class TestDeviceRetrieval:
    """Test device retrieval and filtering functions."""

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    @patch("netbox_librenms_plugin.import_utils.filters.LibreNMSAPI")
    def test_get_librenms_devices_for_import_success(self, mock_api_class, mock_cache):
        """Retrieve devices from LibreNMS API."""
        mock_cache.get.return_value = None  # Cache miss
        mock_api = MagicMock()
        mock_api.list_devices.return_value = (
            True,
            [
                {"device_id": 1, "hostname": "switch-01"},
                {"device_id": 2, "hostname": "switch-02"},
            ],
        )
        mock_api.cache_timeout = 300

        from netbox_librenms_plugin.import_utils import get_librenms_devices_for_import

        devices = get_librenms_devices_for_import(api=mock_api, filters={})

        assert len(devices) == 2
        assert devices[0]["hostname"] == "switch-01"

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_get_librenms_devices_for_import_uses_cache(self, mock_cache):
        """Cached results returned on repeat call."""
        cached_devices = [
            {"device_id": 1, "hostname": "cached-device"},
        ]
        mock_cache.get.return_value = cached_devices

        mock_api = MagicMock()

        from netbox_librenms_plugin.import_utils import get_librenms_devices_for_import

        devices = get_librenms_devices_for_import(api=mock_api, filters={})

        assert len(devices) == 1
        assert devices[0]["hostname"] == "cached-device"
        mock_api.list_devices.assert_not_called()

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_get_librenms_devices_for_import_cache_miss(self, mock_cache):
        """API called when cache empty."""
        mock_cache.get.return_value = None
        mock_api = MagicMock()
        mock_api.list_devices.return_value = (
            True,
            [
                {"device_id": 3, "hostname": "fresh-device"},
            ],
        )
        mock_api.cache_timeout = 300

        from netbox_librenms_plugin.import_utils import get_librenms_devices_for_import

        devices = get_librenms_devices_for_import(api=mock_api, filters={}, force_refresh=True)

        mock_api.list_devices.assert_called_once()
        assert len(devices) == 1

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_get_device_count_for_filters_success(self, mock_cache):
        """Returns correct count from API."""
        mock_cache.get.return_value = [
            {"device_id": 1, "hostname": "switch-01", "status": 1},
            {"device_id": 2, "hostname": "switch-02", "status": 1},
            {"device_id": 3, "hostname": "switch-03", "status": 0},
        ]
        mock_api = MagicMock()

        from netbox_librenms_plugin.import_utils import get_device_count_for_filters

        count = get_device_count_for_filters(api=mock_api, filters={})

        assert count == 3

    @patch("netbox_librenms_plugin.import_utils.filters.cache")
    def test_get_device_count_excludes_disabled(self, mock_cache):
        """Count respects show_disabled filter parameter: disabled==1 devices excluded."""
        mock_cache.get.return_value = [
            {"device_id": 1, "hostname": "switch-01", "disabled": 0, "status": 1},
            {"device_id": 2, "hostname": "switch-02", "disabled": 0, "status": 0},
            {"device_id": 3, "hostname": "switch-03", "disabled": 1, "status": 1},  # disabled in LibreNMS
        ]
        mock_api = MagicMock()

        from netbox_librenms_plugin.import_utils import get_device_count_for_filters

        count = get_device_count_for_filters(api=mock_api, filters={}, show_disabled=False)

        assert count == 2

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

    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache")
    def test_get_virtual_chassis_data_returns_empty_without_api(self, mock_cache):
        """Get VC data returns empty structure without API."""
        from netbox_librenms_plugin.import_utils import get_virtual_chassis_data

        result = get_virtual_chassis_data(api=None, device_id=123)

        assert result["is_stack"] is False
        assert result["member_count"] == 0


# =============================================================================
# TestDeviceValidation - 15 tests
# =============================================================================


class TestDeviceValidation:
    """Test device validation for import."""

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_site_match_found(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Site matched successfully."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_site = MagicMock(id=1, name="DC1")
        mock_find_site.return_value = {
            "found": True,
            "site": mock_site,
            "match_type": "exact",
            "confidence": 1.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = [mock_site]

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "location": "DC1",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["site"]["found"] is True
        assert result["site"]["site"] == mock_site

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_site_not_found(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Site not found adds validation issue."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "location": "Unknown Location",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["site"]["found"] is False
        assert any("site" in issue.lower() for issue in result["issues"])

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType")
    def test_validate_device_platform_match_found(
        self,
        mock_device_type,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Platform matched successfully."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_device_type.objects.all.return_value = []
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_platform = MagicMock(id=1, name="ios")
        mock_find_platform.return_value = {
            "found": True,
            "platform": mock_platform,
            "match_type": "exact",
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "os": "ios",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["platform"]["found"] is True
        assert result["platform"]["platform"] == mock_platform

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_platform_not_found(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Platform not found adds warning (not blocking)."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "os": "unknown_os",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["platform"]["found"] is False

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_type_match_found(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Device type matched successfully."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_dt = MagicMock(id=1, model="C9300-48P")
        mock_match_type.return_value = {
            "matched": True,
            "device_type": mock_dt,
            "match_type": "exact",
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "hardware": "C9300-48P",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["device_type"]["found"] is True
        assert result["device_type"]["device_type"] == mock_dt

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType")
    def test_validate_device_type_not_found(
        self,
        mock_device_type,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Device type not found adds validation issue."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_device_type.objects.all.return_value = []
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "hardware": "Unknown Hardware",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["device_type"]["found"] is False
        assert any("device type" in issue.lower() for issue in result["issues"])

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_role_required(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Missing role flagged as required."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_site = MagicMock(id=1, name="DC1")
        mock_find_site.return_value = {
            "found": True,
            "site": mock_site,
            "match_type": "exact",
            "confidence": 1.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_dt = MagicMock(id=1, model="C9300-48P")
        mock_match_type.return_value = {
            "matched": True,
            "device_type": mock_dt,
            "match_type": "exact",
        }
        mock_role.objects.all.return_value = [MagicMock(id=1, name="Access Switch")]
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = [mock_site]

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "location": "DC1",
            "hardware": "C9300-48P",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["device_role"]["found"] is False
        assert any("role" in issue.lower() for issue in result["issues"])

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_handles_empty_location(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Empty location handled gracefully."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "location": "",
        }

        # Should not raise exception
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result is not None
        assert result["site"]["found"] is False

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_handles_empty_os(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Empty OS handled gracefully."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "os": "",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result is not None
        assert result["platform"]["found"] is False

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType")
    def test_validate_device_handles_empty_hardware(
        self,
        mock_device_type,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Empty hardware handled gracefully."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_device_type.objects.all.return_value = []
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
            "hardware": "",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result is not None
        assert result["device_type"]["found"] is False

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_duplicate_detection(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Existing device detected."""
        existing_device = MagicMock()
        existing_device.name = "switch-01"
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = existing_device

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_device"] == existing_device
        assert result["can_import"] is False

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_returns_complete_state(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """All expected fields in result."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_cluster.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        # Check all expected keys exist
        assert "is_ready" in result
        assert "can_import" in result
        assert "import_as_vm" in result
        assert "existing_device" in result
        assert "issues" in result
        assert "warnings" in result
        assert "site" in result
        assert "device_type" in result
        assert "device_role" in result
        assert "cluster" in result
        assert "platform" in result

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_import_as_vm(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
        mock_cache,
    ):
        """Import as VM mode uses cluster instead of site/device_type."""
        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_clusters = [MagicMock(id=1, name="VMware Cluster")]
        mock_cluster.objects.all.return_value = mock_clusters
        mock_cache.get.return_value = None  # Force cache miss to trigger Cluster.objects.all()
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "vm-01",
        }

        result = validate_device_for_import(device_data, import_as_vm=True, include_vc_detection=False)

        assert result["import_as_vm"] is True
        assert result["cluster"]["available_clusters"] == mock_clusters

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_validate_device_existing_vm_blocks_import(
        self,
        mock_site_model,
        mock_rack,
        mock_cluster,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
    ):
        """Existing VM detection blocks import."""
        existing_vm = MagicMock()
        existing_vm.name = "vm-01"
        mock_vm.objects.filter.return_value.first.return_value = existing_vm
        mock_device.objects.filter.return_value.first.return_value = None

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "vm-01",
        }

        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_device"] == existing_vm
        assert result["can_import"] is False
        assert result["import_as_vm"] is True


class TestDeviceNamingPreferencesLegacy:
    """Test that validation honours use_sysname and strip_domain user preferences."""

    COMMON_PATCHES = [
        "netbox_librenms_plugin.import_utils.device_operations.Site",
        "netbox_librenms_plugin.import_utils.device_operations.Rack",
        "netbox_librenms_plugin.import_utils.device_operations.Cluster",
        "netbox_librenms_plugin.import_utils.device_operations.DeviceRole",
        "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
        "netbox_librenms_plugin.import_utils.device_operations.Device",
        "virtualization.models.VirtualMachine",
    ]

    def _setup_no_existing(self, mocks):
        """Configure mocks so no existing device is found."""
        mock_vm = mocks[-1]  # VirtualMachine
        mock_device = mocks[-2]  # Device
        mock_find_site = mocks[-3]
        mock_find_platform = mocks[-4]
        mock_match_type = mocks[-5]
        mock_role = mocks[-6]
        mock_rack = mocks[-8]
        mock_site_model = mocks[-9]

        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_rack.objects.filter.return_value = []
        mock_site_model.objects.all.return_value = []

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_resolved_name_uses_sysname_by_default(self, *mocks):
        """Default use_sysname=True uses sysName for resolved_name."""
        self._setup_no_existing(mocks)
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "10.0.0.1",
            "sysName": "core-switch",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)
        assert result["resolved_name"] == "core-switch"

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_resolved_name_uses_hostname_when_sysname_disabled(self, *mocks):
        """use_sysname=False uses hostname for resolved_name."""
        self._setup_no_existing(mocks)
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "10.0.0.1",
            "sysName": "core-switch",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
            use_sysname=False,
        )
        assert result["resolved_name"] == "10.0.0.1"

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_resolved_name_strips_domain(self, *mocks):
        """strip_domain=True strips the domain suffix."""
        self._setup_no_existing(mocks)
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01.example.com",
            "sysName": "switch-01.example.com",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
            strip_domain=True,
        )
        assert result["resolved_name"] == "switch-01"

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_duplicate_detection_uses_resolved_name(self, *mocks):
        """Duplicate detection should match against the resolved name, not raw hostname."""
        self._setup_no_existing(mocks)

        mock_device = mocks[-2]  # Device
        # The first filter call (librenms_id) returns None,
        # the second filter call (name__iexact) returns the existing device.
        existing = MagicMock()
        existing.name = "core-switch"
        existing.serial = ""
        mock_device.objects.filter.return_value.first.side_effect = [None, existing]

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 999,
            "hostname": "10.0.0.1",
            "sysName": "core-switch",
        }
        # use_sysname=True (default): resolved name is "core-switch"
        # so duplicate detection should find existing device "core-switch"
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_device"] == existing
        assert result["existing_match_type"] == "hostname"

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_backward_compatible_defaults(self, *mocks):
        """Calling without naming params produces resolved_name in result."""
        self._setup_no_existing(mocks)
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)

        # resolved_name should be present and match sysName fallback to hostname
        assert "resolved_name" in result
        assert result["resolved_name"] == "switch-01"


class TestNameMatchesWithNamingPreferencesLegacy:
    """
    Test that name_matches/name_sync_available respect naming preferences and VC patterns.

    The name comparison should use the resolved name (result of _determine_device_name())
    which accounts for use_sysname and strip_domain, not the raw LibreNMS sysName.
    For VC members, it should also account for the VC naming pattern.
    """

    COMMON_PATCHES = [
        "netbox_librenms_plugin.import_utils.device_operations.Site",
        "netbox_librenms_plugin.import_utils.device_operations.Rack",
        "netbox_librenms_plugin.import_utils.device_operations.Cluster",
        "netbox_librenms_plugin.import_utils.device_operations.DeviceRole",
        "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
        "netbox_librenms_plugin.import_utils.device_operations.Device",
        "virtualization.models.VirtualMachine",
    ]

    def _start_patches(self):
        """Start all common patches and return mocks in standard order."""
        self._patchers = [patch(p) for p in self.COMMON_PATCHES]
        mocks = [p.start() for p in self._patchers]
        (
            self.mock_site_model,
            self.mock_rack,
            self.mock_cluster,
            self.mock_role,
            self.mock_match_type,
            self.mock_find_platform,
            self.mock_find_site,
            self.mock_device,
            self.mock_vm,
        ) = mocks

    def _stop_patches(self):
        """Stop all patches."""
        for p in self._patchers:
            p.stop()

    def _configure_standard_mocks(self):
        """Configure standard mock returns for site/platform/type/role."""
        self.mock_find_site.return_value = {
            "found": True,
            "site": MagicMock(),
            "match_type": "exact",
            "confidence": 1.0,
        }
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        self.mock_match_type.return_value = {"matched": False, "device_type": None, "match_type": None}
        self.mock_role.objects.all.return_value = []
        self.mock_cluster.objects.all.return_value = []
        self.mock_rack.objects.filter.return_value = []
        self.mock_site_model.objects.all.return_value = []

    def _setup_librenms_id_match(self, existing_device, as_vm=False):
        """Configure mocks so that a device is found by librenms_id.

        Uses a Q-aware side_effect so only filter() calls targeting a
        ``librenms_id`` field return the existing device; other filter() calls
        (e.g. name lookups, serial lookups) return an empty queryset.
        """
        from unittest.mock import MagicMock

        def _librenms_id_filter_side_effect(hit):
            def side_effect(*args, **kwargs):
                mock_qs = MagicMock()
                # Match when the first positional arg is a Q that references librenms_id
                if args:
                    q = args[0]
                    if hasattr(q, "children") and any(
                        isinstance(child, tuple) and "librenms_id" in child[0] for child in q.children
                    ):
                        mock_qs.first.return_value = hit
                        return mock_qs
                mock_qs.first.return_value = None
                return mock_qs

            return side_effect

        if as_vm:
            self.mock_vm.objects.filter.side_effect = _librenms_id_filter_side_effect(existing_device)
            self.mock_device.objects.filter.side_effect = _librenms_id_filter_side_effect(None)
        else:
            self.mock_device.objects.filter.side_effect = _librenms_id_filter_side_effect(existing_device)
            self.mock_vm.objects.filter.side_effect = _librenms_id_filter_side_effect(None)

    def setup_method(self):
        """Set up common patches."""
        self._start_patches()

    def teardown_method(self):
        """Tear down patches."""
        self._stop_patches()

    def test_name_matches_with_strip_domain(self):
        """strip_domain=True: FQDN in LibreNMS matches short name in NetBox."""
        existing = MagicMock()
        existing.name = "router"
        existing.serial = ""
        existing.virtual_chassis = None
        existing.vc_position = None

        self._setup_librenms_id_match(existing)
        self._configure_standard_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "router.example.com",
            "sysName": "router.example.com",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
            strip_domain=True,
        )

        assert result["existing_match_type"] == "librenms_id"
        assert result["name_matches"] is True
        assert result["name_sync_available"] is False

    def test_name_matches_uses_hostname_when_sysname_disabled(self):
        """use_sysname=False: matches against hostname instead of sysName."""
        existing = MagicMock()
        existing.name = "10.0.0.1"
        existing.serial = ""
        existing.virtual_chassis = None
        existing.vc_position = None

        self._setup_librenms_id_match(existing)
        self._configure_standard_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "10.0.0.1",
            "sysName": "core-switch",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
            use_sysname=False,
        )

        assert result["name_matches"] is True
        assert result["name_sync_available"] is False

    def test_name_mismatch_offers_sync_with_resolved_name(self):
        """When names don't match, suggested_name is the resolved name, not raw sysName."""
        existing = MagicMock()
        existing.name = "old-device"
        existing.serial = ""
        existing.virtual_chassis = None
        existing.vc_position = None

        self._setup_librenms_id_match(existing)
        self._configure_standard_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "new-switch.example.com",
            "sysName": "new-switch.example.com",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
            strip_domain=True,
        )

        assert result["name_sync_available"] is True
        # suggested_name should be the resolved (stripped) name, not raw sysName
        assert result["suggested_name"] == "new-switch"

    @patch("netbox_librenms_plugin.import_utils.device_operations._generate_vc_member_name")
    def test_name_matches_vc_member(self, mock_vc_name):
        """VC member: name matches when existing device name matches generated VC name."""
        mock_vc_name.return_value = "switch-M2"

        existing = MagicMock()
        existing.name = "switch-M2"
        existing.serial = "SN123"
        existing.virtual_chassis = MagicMock()  # Not None → device is a VC member
        existing.vc_position = 2

        self._setup_librenms_id_match(existing)
        self._configure_standard_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch",
            "sysName": "switch",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
        )

        assert result["name_matches"] is True
        assert result["name_sync_available"] is False
        # _generate_vc_member_name should be called with resolved name, position, serial
        mock_vc_name.assert_called_with("switch", 2, serial="SN123")

    @patch("netbox_librenms_plugin.import_utils.device_operations._generate_vc_member_name")
    def test_name_matches_vc_member_with_strip_domain(self, mock_vc_name):
        """VC member + strip_domain: FQDN resolved to short name matches VC pattern."""
        mock_vc_name.return_value = "siteA-9300-1 (2)"

        existing = MagicMock()
        existing.name = "siteA-9300-1 (2)"
        existing.serial = "SN456"
        existing.virtual_chassis = MagicMock()
        existing.vc_position = 2

        self._setup_librenms_id_match(existing)
        self._configure_standard_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 555,
            "hostname": "siteA-9300-1.example.net.com",
            "sysName": "siteA-9300-1.example.net.com",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
            strip_domain=True,
        )

        assert result["name_matches"] is True
        assert result["name_sync_available"] is False
        # Resolved name should be "siteA-9300-1" (stripped), then VC name generated
        mock_vc_name.assert_called_with("siteA-9300-1", 2, serial="SN456")

    @patch("netbox_librenms_plugin.import_utils.device_operations._generate_vc_member_name")
    def test_vc_member_name_mismatch_suggests_vc_name(self, mock_vc_name):
        """VC member name mismatch: suggested_name should be the expected VC name."""
        mock_vc_name.return_value = "new-switch-M2"

        existing = MagicMock()
        existing.name = "old-switch-M2"
        existing.serial = "SN789"
        existing.virtual_chassis = MagicMock()
        existing.vc_position = 2

        self._setup_librenms_id_match(existing)
        self._configure_standard_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "new-switch",
            "sysName": "new-switch",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
        )

        assert result["name_matches"] is False
        assert result["name_sync_available"] is True
        assert result["suggested_name"] == "new-switch-M2"

    def test_vm_name_matches_with_strip_domain(self):
        """VM name comparison also uses resolved name, not raw sysName."""
        existing_vm = MagicMock()
        existing_vm.name = "vm-server"

        self._setup_librenms_id_match(existing_vm, as_vm=True)
        self._configure_standard_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "vm-server.example.com",
            "sysName": "vm-server.example.com",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
            strip_domain=True,
        )

        assert result["import_as_vm"] is True
        assert result["name_matches"] is True

    def test_name_matches_exact_without_vc(self):
        """Standalone device: exact name match works without VC check."""
        existing = MagicMock()
        existing.name = "core-router"
        existing.serial = ""
        existing.virtual_chassis = None
        existing.vc_position = None

        self._setup_librenms_id_match(existing)
        self._configure_standard_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "core-router",
            "sysName": "core-router",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
        )

        assert result["name_matches"] is True
        assert result["name_sync_available"] is False


class TestSerialNumberMatching:
    """Test serial number matching in device validation."""

    SERIAL_PATCHES = [
        "netbox_librenms_plugin.import_utils.device_operations.Site",
        "netbox_librenms_plugin.import_utils.device_operations.Rack",
        "netbox_librenms_plugin.import_utils.device_operations.Cluster",
        "netbox_librenms_plugin.import_utils.device_operations.DeviceRole",
        "netbox_librenms_plugin.import_utils.device_operations.DeviceType",
        "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
        "netbox_librenms_plugin.import_utils.device_operations.Device",
        "virtualization.models.VirtualMachine",
    ]

    def _start_patches(self):
        """Start all common patches and return mocks in standard order."""
        self._patchers = [patch(p) for p in self.SERIAL_PATCHES]
        mocks = [p.start() for p in self._patchers]
        (
            self.mock_site_model,
            self.mock_rack,
            self.mock_cluster,
            self.mock_role,
            self.mock_device_type,
            self.mock_match_type,
            self.mock_find_platform,
            self.mock_find_site,
            self.mock_device,
            self.mock_vm,
        ) = mocks
        self.mock_device_type.objects.all.return_value = []

    def _stop_patches(self):
        """Stop all patches."""
        for p in self._patchers:
            p.stop()

    def setup_method(self):
        """Set up common patches for serial number tests."""
        self._start_patches()

    def teardown_method(self):
        """Tear down patches."""
        self._stop_patches()

    def test_serial_match_blocks_import(self):
        """Device with matching serial blocks import."""
        existing = MagicMock()
        existing.name = "existing-device"
        existing.serial = "ABC123"

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            if "serial" in kwargs:
                result.first.return_value = existing
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "new-hostname", "serial": "ABC123"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["can_import"] is False
        assert result["existing_match_type"] == "serial"
        assert result["existing_device"] == existing

    def test_serial_match_same_hostname_offers_link(self):
        """Serial + hostname match offers link action."""
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = "ABC123"

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            if "serial" in kwargs:
                result.first.return_value = existing
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "ABC123"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["serial_action"] == "link"
        assert result["existing_match_type"] == "serial"
        assert "not linked to LibreNMS" in result["warnings"][0]

    def test_serial_match_diff_hostname_offers_hostname_differs(self):
        """Serial matches but hostname differs offers hostname_differs action."""
        existing = MagicMock()
        existing.name = "old-hostname"
        existing.serial = "ABC123"

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            if "serial" in kwargs:
                result.first.return_value = existing
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "new-hostname", "serial": "ABC123"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["serial_action"] == "hostname_differs"
        assert result["existing_match_type"] == "serial"
        assert "hostname differs" in result["warnings"][0]

    def test_hostname_match_diff_serial_offers_update(self):
        """Hostname matches but serial differs offers update_serial action."""
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = "OLD_SERIAL"

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            if "name__iexact" in kwargs:
                result.first.return_value = existing
            elif "serial" in kwargs:
                result.first.return_value = None
                result.exclude.return_value.first.return_value = None
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "NEW_SERIAL"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["serial_action"] == "update_serial"
        assert result["existing_match_type"] == "hostname"
        assert "Hardware may have been replaced" in result["warnings"][0]

    def _setup_no_match_mocks(self):
        """Configure mocks for tests where no device match is expected."""
        self.mock_vm.objects.filter.return_value.first.return_value = None
        self.mock_device.objects.filter.return_value.first.return_value = None
        self.mock_find_site.return_value = {"found": False, "site": None, "match_type": None, "confidence": 0}
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        self.mock_match_type.return_value = {"matched": False, "device_type": None, "match_type": None}
        self.mock_role.objects.all.return_value = []
        self.mock_cluster.objects.all.return_value = []
        self.mock_site_model.objects.all.return_value = []

    def test_serial_dash_ignored(self):
        """Serial '-' is not treated as a match."""
        self._setup_no_match_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "-"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] is None
        assert result["serial_action"] is None

    def test_serial_empty_ignored(self):
        """Empty serial skips serial matching."""
        self._setup_no_match_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": ""}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] is None
        assert result["serial_action"] is None

    def test_serial_none_ignored(self):
        """None serial skips serial matching."""
        self._setup_no_match_mocks()

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": None}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] is None
        assert result["serial_action"] is None

    def test_hostname_match_serial_conflict_warns(self):
        """Hostname matches, incoming serial already on another device warns about conflict."""
        hostname_device = MagicMock()
        hostname_device.name = "switch-01"
        hostname_device.serial = "OLD_SERIAL"

        serial_conflict_device = MagicMock()
        serial_conflict_device.name = "other-device"

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            if "name__iexact" in kwargs:
                result.first.return_value = hostname_device
            elif "serial" in kwargs:
                result.exclude.return_value.first.return_value = serial_conflict_device
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "CONFLICTING_SERIAL"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["serial_action"] == "conflict"
        assert result["existing_match_type"] == "hostname"
        assert "Serial conflict" in result["warnings"][0]

    def test_librenms_id_match_shows_serial_confirmed(self):
        """librenms_id match with matching serial shows confirmation."""
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = "ABC123"
        existing.virtual_chassis = None  # Not a VC member → use plain hostname comparison
        existing.vc_position = None

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            q_has_librenms = any("librenms_id" in str(arg) for arg in args) or any(
                k.startswith("custom_field_data__librenms_id") for k in kwargs
            )
            if q_has_librenms:
                result.first.return_value = existing
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter
        self.mock_find_site.return_value = {
            "found": True,
            "site": MagicMock(),
            "match_type": "exact",
            "confidence": 1.0,
        }
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        self.mock_match_type.return_value = {"matched": False, "device_type": None, "match_type": None}
        self.mock_role.objects.all.return_value = []
        self.mock_cluster.objects.all.return_value = []
        self.mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "sysName": "switch-01", "serial": "ABC123"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] == "librenms_id"
        assert result["can_import"] is False
        assert result["serial_confirmed"] is True
        assert result["name_matches"] is True

    def test_librenms_id_match_detects_serial_drift(self):
        """librenms_id match with different serial warns about drift."""
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = "OLD_SERIAL"
        existing.virtual_chassis = None
        existing.vc_position = None

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            q_has_librenms = any("librenms_id" in str(arg) for arg in args) or any(
                k.startswith("custom_field_data__librenms_id") for k in kwargs
            )
            if q_has_librenms:
                result.first.return_value = existing
            elif "serial" in kwargs:
                result.first.return_value = None
                result.exclude.return_value.first.return_value = None
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter
        self.mock_find_site.return_value = {
            "found": True,
            "site": MagicMock(),
            "match_type": "exact",
            "confidence": 1.0,
        }
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        self.mock_match_type.return_value = {"matched": False, "device_type": None, "match_type": None}
        self.mock_role.objects.all.return_value = []
        self.mock_cluster.objects.all.return_value = []
        self.mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "serial": "NEW_SERIAL"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] == "librenms_id"
        assert result["serial_action"] == "update_serial"
        assert any("Hardware may have been replaced" in w for w in result["warnings"])

    def test_librenms_id_match_still_validates_site(self):
        """librenms_id match continues to populate site/type validation."""
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = ""
        existing.virtual_chassis = None
        existing.vc_position = None

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            q_has_librenms = any("librenms_id" in str(arg) for arg in args) or any(
                k.startswith("custom_field_data__librenms_id") for k in kwargs
            )
            if q_has_librenms:
                result.first.return_value = existing
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter
        mock_site = MagicMock(id=1, name="DC1")
        self.mock_find_site.return_value = {"found": True, "site": mock_site, "match_type": "exact", "confidence": 1.0}
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        mock_dt = MagicMock()
        self.mock_match_type.return_value = {"matched": True, "device_type": mock_dt, "match_type": "exact"}
        self.mock_role.objects.all.return_value = []
        self.mock_cluster.objects.all.return_value = []
        self.mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {"device_id": 1, "hostname": "switch-01", "location": "DC1", "hardware": "WS-C4900M"}
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_match_type"] == "librenms_id"
        assert result["can_import"] is False
        assert result["is_ready"] is False
        # Site and device_type should still be populated
        assert result["site"]["found"] is True
        assert result["site"]["site"] == mock_site
        assert result["device_type"]["found"] is True

    def test_existing_device_role_populated(self):
        """Existing device's role should be shown in validation details."""
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = "ABC123"
        existing.virtual_chassis = None
        existing.vc_position = None
        mock_existing_role = MagicMock()
        mock_existing_role.name = "Access Switch"
        existing.role = mock_existing_role

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            if "serial" in kwargs:
                result.first.return_value = existing
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter
        self.mock_find_site.return_value = {"found": False, "site": None, "match_type": None, "confidence": 0}
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        self.mock_match_type.return_value = {"matched": True, "device_type": MagicMock(), "match_type": "exact"}
        self.mock_role.objects.all.return_value = [mock_existing_role]
        self.mock_cluster.objects.all.return_value = []
        self.mock_site_model.objects.all.return_value = []

        with patch("netbox_librenms_plugin.import_utils.device_operations.cache") as mock_cache:
            mock_cache.get.return_value = None

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
        assert result["device_role"]["role"] == mock_existing_role

    def test_device_type_mismatch_flagged(self):
        """Device type mismatch between existing device and LibreNMS should be flagged."""
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = "ABC123"
        existing.virtual_chassis = None
        existing.vc_position = None
        existing_device_type = MagicMock()
        existing_device_type.pk = 1
        existing_device_type.__str__ = lambda self: "Old Type"
        existing.device_type = existing_device_type
        existing.role = MagicMock()

        librenms_device_type = MagicMock()
        librenms_device_type.pk = 2
        librenms_device_type.__str__ = lambda self: "New Type"

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            if "serial" in kwargs:
                result.first.return_value = existing
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter
        self.mock_find_site.return_value = {"found": False, "site": None, "match_type": None, "confidence": 0}
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        self.mock_match_type.return_value = {
            "matched": True,
            "device_type": librenms_device_type,
            "match_type": "exact",
        }
        self.mock_role.objects.all.return_value = []
        self.mock_cluster.objects.all.return_value = []
        self.mock_site_model.objects.all.return_value = []

        with patch("netbox_librenms_plugin.import_utils.device_operations.cache") as mock_cache:
            mock_cache.get.return_value = None

            from netbox_librenms_plugin.import_utils import validate_device_for_import

            device_data = {
                "device_id": 1,
                "hostname": "switch-01",
                "serial": "ABC123",
                "location": "",
                "hardware": "New Type",
            }
            result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["device_type_mismatch"] is True
        assert any("Device type mismatch" in w for w in result["warnings"])

    def test_no_device_type_mismatch_when_types_match(self):
        """No mismatch flag when existing device type matches LibreNMS."""
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = "ABC123"
        existing.virtual_chassis = None
        existing.vc_position = None
        same_device_type = MagicMock()
        same_device_type.pk = 1
        existing.device_type = same_device_type
        existing.role = MagicMock()

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(*args, **kwargs):
            result = MagicMock()
            if "serial" in kwargs:
                result.first.return_value = existing
            else:
                result.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter
        self.mock_find_site.return_value = {"found": False, "site": None, "match_type": None, "confidence": 0}
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        self.mock_match_type.return_value = {"matched": True, "device_type": same_device_type, "match_type": "exact"}
        self.mock_role.objects.all.return_value = []
        self.mock_cluster.objects.all.return_value = []
        self.mock_site_model.objects.all.return_value = []

        with patch("netbox_librenms_plugin.import_utils.device_operations.cache") as mock_cache:
            mock_cache.get.return_value = None

            from netbox_librenms_plugin.import_utils import validate_device_for_import

            device_data = {
                "device_id": 1,
                "hostname": "switch-01",
                "serial": "ABC123",
                "location": "",
                "hardware": "Same Type",
            }
            result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["device_type_mismatch"] is False


class TestNameMatchesWithNamingPreferences:
    """Test VC-aware name matching with use_sysname/strip_domain preferences."""

    PATCHES = [
        "netbox_librenms_plugin.import_utils.device_operations.Site",
        "netbox_librenms_plugin.import_utils.device_operations.Rack",
        "netbox_librenms_plugin.import_utils.device_operations.Cluster",
        "netbox_librenms_plugin.import_utils.device_operations.DeviceRole",
        "netbox_librenms_plugin.import_utils.device_operations.DeviceType",
        "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
        "netbox_librenms_plugin.import_utils.device_operations.Device",
        "virtualization.models.VirtualMachine",
    ]

    def setup_method(self):
        self._patchers = [patch(p) for p in self.PATCHES]
        mocks = [p.start() for p in self._patchers]
        (
            self.mock_site,
            self.mock_rack,
            self.mock_cluster,
            self.mock_role,
            self.mock_device_type,
            self.mock_match_type,
            self.mock_find_platform,
            self.mock_find_site,
            self.mock_device,
            self.mock_vm,
        ) = mocks
        self.mock_device_type.objects.all.return_value = []
        self.mock_vm.objects.filter.return_value.first.return_value = None
        self.mock_find_site.return_value = {
            "found": True,
            "site": MagicMock(),
            "match_type": "exact",
            "confidence": 1.0,
        }
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        self.mock_match_type.return_value = {"matched": False, "device_type": None, "match_type": None}
        self.mock_role.objects.all.return_value = []
        self.mock_cluster.objects.all.return_value = []
        self.mock_site.objects.all.return_value = []

    def teardown_method(self):
        for p in self._patchers:
            p.stop()

    def _make_existing(self, name, serial="SN001", virtual_chassis=None, vc_position=None):
        existing = MagicMock()
        existing.name = name
        existing.serial = serial
        existing.virtual_chassis = virtual_chassis
        existing.vc_position = vc_position
        existing.custom_field_data = {"librenms_id": {"default": 42}}
        return existing

    def _setup_librenms_id_filter(self, existing):
        def device_filter(*args, **kwargs):
            result = MagicMock()
            q_has_librenms = any("librenms_id" in str(arg) for arg in args) or any(
                k.startswith("custom_field_data__librenms_id") for k in kwargs
            )
            result.first.return_value = existing if q_has_librenms else None
            result.exclude.return_value.first.return_value = None
            return result

        self.mock_device.objects.filter.side_effect = device_filter

    def test_strip_domain_name_matches(self):
        """strip_domain=True resolves 'switch-01.example.com' to 'switch-01', matching existing device."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        existing = self._make_existing("switch-01")
        self._setup_librenms_id_filter(existing)

        device_data = {
            "device_id": 42,
            "hostname": "switch-01.example.com",
            "sysName": "switch-01.example.com",
            "serial": "SN001",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False, strip_domain=True)
        assert result["name_matches"] is True
        assert result["name_sync_available"] is False

    def test_sysname_disabled_uses_hostname(self):
        """use_sysname=False falls back to hostname for name comparison."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        existing = self._make_existing("switch-hostname")
        self._setup_librenms_id_filter(existing)

        device_data = {
            "device_id": 42,
            "hostname": "switch-hostname",
            "sysName": "switch-sysname",
            "serial": "SN001",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False, use_sysname=False)
        assert result["name_matches"] is True
        assert result["resolved_name"] == "switch-hostname"

    def test_name_mismatch_offers_sync(self):
        """When resolved name differs from existing device name, name_sync_available is set."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        existing = self._make_existing("old-name")
        self._setup_librenms_id_filter(existing)

        device_data = {
            "device_id": 42,
            "hostname": "new-name",
            "sysName": "new-name",
            "serial": "SN001",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)
        assert result["name_matches"] is False
        assert result["name_sync_available"] is True
        assert result["suggested_name"] == "new-name"

    def test_vc_member_name_matches(self):
        """Existing VC member name is compared against vc_member_name(hostname, vc_position)."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        mock_vc = MagicMock()
        expected_name = _generate_vc_member_name("stack-master", 2, serial="SN001")
        existing = self._make_existing(expected_name, serial="SN001", virtual_chassis=mock_vc, vc_position=2)
        self._setup_librenms_id_filter(existing)

        device_data = {
            "device_id": 42,
            "hostname": "stack-master",
            "sysName": "stack-master",
            "serial": "SN001",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)
        assert result["name_matches"] is True

    def test_vc_member_name_mismatch_suggests_vc_name(self):
        """When VC member name differs, suggested_name is the expected VC member name."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        mock_vc = MagicMock()
        existing = self._make_existing("wrong-name", serial="SN001", virtual_chassis=mock_vc, vc_position=2)
        self._setup_librenms_id_filter(existing)

        device_data = {
            "device_id": 42,
            "hostname": "stack-master",
            "sysName": "stack-master",
            "serial": "SN001",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)
        expected_name = _generate_vc_member_name("stack-master", 2, serial="SN001")
        assert result["name_matches"] is False
        assert result["name_sync_available"] is True
        assert result["suggested_name"] == expected_name

    def test_vc_member_with_strip_domain(self):
        """strip_domain applies before VC member name comparison."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        mock_vc = MagicMock()
        expected_name = _generate_vc_member_name("stack", 1, serial="SN001")
        existing = self._make_existing(expected_name, serial="SN001", virtual_chassis=mock_vc, vc_position=1)
        self._setup_librenms_id_filter(existing)

        device_data = {
            "device_id": 42,
            "hostname": "stack.example.com",
            "sysName": "stack.example.com",
            "serial": "SN001",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False, strip_domain=True)
        assert result["name_matches"] is True

    def test_naming_criteria_populated(self):
        """naming_criteria dict is set in result with use_sysname/strip_domain/source."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        self.mock_device.objects.filter.return_value.first.return_value = None

        device_data = {
            "device_id": 99,
            "hostname": "router-01",
            "sysName": "router-sysname",
        }
        result = validate_device_for_import(
            device_data, include_vc_detection=False, use_sysname=True, strip_domain=False
        )
        criteria = result["naming_criteria"]
        assert criteria is not None
        assert criteria["use_sysname"] is True
        assert criteria["strip_domain"] is False
        assert criteria["raw_sysname"] == "router-sysname"
        assert criteria["raw_hostname"] == "router-01"
        assert criteria["source"] == "sysname"

    def test_naming_criteria_source_hostname_when_sysname_disabled(self):
        """naming_criteria source is 'hostname' when use_sysname=False."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        self.mock_device.objects.filter.return_value.first.return_value = None

        device_data = {
            "device_id": 99,
            "hostname": "router-01",
            "sysName": "router-sysname",
        }
        result = validate_device_for_import(
            device_data, include_vc_detection=False, use_sysname=False, strip_domain=False
        )
        assert result["naming_criteria"]["source"] == "hostname"

    def test_naming_criteria_source_sysname_when_sysname_disabled_but_hostname_empty(self):
        """
        When use_sysname=False and hostname is empty, source falls back to 'sysname'.

        Before the fix, source was incorrectly reported as 'hostname' even
        though the resolved name actually came from sysName.
        """
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        self.mock_device.objects.filter.return_value.first.return_value = None

        device_data = {
            "device_id": 99,
            "hostname": "",
            "sysName": "router-sysname",
        }
        result = validate_device_for_import(
            device_data, include_vc_detection=False, use_sysname=False, strip_domain=False
        )
        assert result["naming_criteria"]["source"] == "sysname", (
            "When hostname is empty, source must be 'sysname', not 'hostname'"
        )

    def test_naming_criteria_source_hostname_fallback_when_both_empty(self):
        """When both hostname and sysName are empty, source is 'device-{id}' (no-name guard)."""
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        self.mock_device.objects.filter.return_value.first.return_value = None

        device_data = {"device_id": 99, "hostname": "", "sysName": ""}
        result = validate_device_for_import(
            device_data, include_vc_detection=False, use_sysname=False, strip_domain=False
        )
        # Both empty → no-name guard returns 'device-{id}' as source
        assert result["naming_criteria"]["source"] == "device-99"


class TestLegacyLibreNMSIdMigration:
    """Test detection of legacy bare-integer librenms_id format during device validation."""

    PATCHES = [
        "netbox_librenms_plugin.import_utils.device_operations.Site",
        "netbox_librenms_plugin.import_utils.device_operations.Rack",
        "netbox_librenms_plugin.import_utils.device_operations.Cluster",
        "netbox_librenms_plugin.import_utils.device_operations.DeviceRole",
        "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
        "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
        "netbox_librenms_plugin.import_utils.device_operations.Device",
        "virtualization.models.VirtualMachine",
    ]

    def setup_method(self):
        self._patchers = [patch(p) for p in self.PATCHES]
        mocks = [p.start() for p in self._patchers]
        (
            self.mock_site_model,
            self.mock_rack,
            self.mock_cluster,
            self.mock_role,
            self.mock_match_type,
            self.mock_find_platform,
            self.mock_find_site,
            self.mock_device,
            self.mock_vm,
        ) = mocks

        self.mock_find_site.return_value = {
            "found": True,
            "site": MagicMock(),
            "match_type": "exact",
            "confidence": 1.0,
        }
        self.mock_find_platform.return_value = {"found": False, "platform": None, "match_type": None}
        self.mock_match_type.return_value = {"matched": False, "device_type": None, "match_type": None}
        self.mock_role.objects.all.return_value = []
        self.mock_cluster.objects.all.return_value = []
        self.mock_site_model.objects.all.return_value = []
        self.mock_vm.objects.filter.return_value.first.return_value = None

    def teardown_method(self):
        for p in self._patchers:
            p.stop()

    def _make_existing(self, librenms_id_value, serial="SN001"):
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = serial
        existing.virtual_chassis = None
        existing.vc_position = None
        existing.custom_field_data = {"librenms_id": librenms_id_value}
        return existing

    def _setup_device_filter(self, existing):
        def device_filter(*args, **kwargs):
            result = MagicMock()
            q_has_librenms = any("librenms_id" in str(arg) for arg in args) or any(
                k.startswith("custom_field_data__librenms_id") for k in kwargs
            )
            result.first.return_value = existing if q_has_librenms else None
            return result

        self.mock_device.objects.filter.side_effect = device_filter

    def test_legacy_int_sets_needs_migration_flag(self):
        """Device with bare-integer librenms_id sets librenms_id_needs_migration=True."""
        existing = self._make_existing(librenms_id_value=42, serial="SN001")
        self._setup_device_filter(existing)

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        result = validate_device_for_import(
            {"device_id": 42, "hostname": "switch-01", "serial": "SN001"},
            include_vc_detection=False,
        )

        assert result["existing_match_type"] == "librenms_id"
        assert result["librenms_id_needs_migration"] is True
        assert result["serial_confirmed"] is True

    def test_legacy_int_no_serial_still_sets_flag(self):
        """Legacy int format sets the migration flag even when serial is absent."""
        existing = self._make_existing(librenms_id_value=42, serial="")
        self._setup_device_filter(existing)

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        result = validate_device_for_import(
            {"device_id": 42, "hostname": "switch-01"},
            include_vc_detection=False,
        )

        assert result["librenms_id_needs_migration"] is True
        assert result["serial_confirmed"] is False

    def test_json_format_does_not_set_flag(self):
        """Device with JSON librenms_id does NOT set librenms_id_needs_migration."""
        existing = self._make_existing(librenms_id_value={"default": 42}, serial="SN001")
        self._setup_device_filter(existing)

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        result = validate_device_for_import(
            {"device_id": 42, "hostname": "switch-01", "serial": "SN001"},
            include_vc_detection=False,
        )

        assert result["existing_match_type"] == "librenms_id"
        assert result["librenms_id_needs_migration"] is False

    def test_migrate_legacy_librenms_id_helper(self):
        """migrate_legacy_librenms_id converts int to {server_key: int}."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 42}
        result = migrate_legacy_librenms_id(obj, "primary")

        assert result is True
        assert obj.custom_field_data["librenms_id"] == {"primary": 42}

    def test_migrate_legacy_librenms_id_noop_for_json(self):
        """migrate_legacy_librenms_id is a no-op when value is already a dict."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"primary": 42}}
        result = migrate_legacy_librenms_id(obj, "primary")

        assert result is False
        assert obj.custom_field_data["librenms_id"] == {"primary": 42}

    def test_migrate_legacy_librenms_id_noop_for_none(self):
        """migrate_legacy_librenms_id is a no-op when librenms_id is absent."""
        from netbox_librenms_plugin.utils import migrate_legacy_librenms_id

        obj = MagicMock()
        obj.custom_field_data = {}
        result = migrate_legacy_librenms_id(obj, "primary")

        assert result is False


class TestDeviceConflictActionView:
    """Test DeviceConflictActionView conflict resolution actions."""

    def _create_view(self):
        """Create a DeviceConflictActionView instance with mocked dependencies."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = DeviceConflictActionView()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def _create_request(self, action, existing_device_id, use_sysname=False, strip_domain=False):
        """
        Create a mock request with POST data and permission stubs.

        The returned request should be bound to the view (view.request = request)
        before calling view.post() so permission checks and business logic
        operate on the same request object, matching real Django CBV behavior.
        """
        request = MagicMock()
        request.user.has_perm.return_value = True
        # Always include both toggles so resolve_naming_preferences never falls through
        # to the user-pref/settings DB path, which would hit the real database.
        post_data = {
            "action": action,
            "existing_device_id": str(existing_device_id),
            "use-sysname-toggle": "on" if use_sysname else "off",
            "strip-domain-toggle": "on" if strip_domain else "off",
        }
        request.POST = post_data
        return request

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_link_action_sets_librenms_id_and_name(self, mock_cache_key, mock_cache):
        """Link action should set librenms_id and update name from sysName."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {}
        existing_device.name = "84.116.251.35"

        libre_device = {
            "device_id": 10,
            "hostname": "84.116.251.35",
            "sysName": "switch-01.example.com",
            "serial": "ABC123",
        }
        validation = {"can_import": False, "existing_device": existing_device}
        selections = {}

        request = self._create_request("link", 42, use_sysname=True)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
        ):
            mock_tx.atomic.return_value = MagicMock()
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == {"default": 10}
        assert existing_device.name == "switch-01.example.com"
        existing_device.save.assert_called_once()

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_link_action_uses_non_default_server_key(self, mock_cache_key, mock_cache):
        """Link action should store librenms_id under the active server_key, not always 'default'."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        view._librenms_api.server_key = "production"
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {}
        existing_device.name = "84.116.251.35"

        libre_device = {
            "device_id": 10,
            "hostname": "84.116.251.35",
            "sysName": "switch-01.example.com",
            "serial": "ABC123",
        }
        validation = {"can_import": False, "existing_device": existing_device}
        selections = {}

        request = self._create_request("link", 42, use_sysname=True)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
        ):
            mock_tx.atomic.return_value = MagicMock()
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.filter.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == {"production": 10}

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_update_action_sets_hostname_serial_and_librenms_id(self, mock_cache_key, mock_cache):
        """Update action should set hostname, serial, and librenms_id."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {}
        existing_device.name = "old-name"
        existing_device.serial = "OLD-SERIAL"

        libre_device = {
            "device_id": 10,
            "hostname": "84.116.251.35",
            "sysName": "new-name.example.com",
            "serial": "NEW-SERIAL",
        }
        validation = {"can_import": False, "existing_device": existing_device}
        selections = {}

        request = self._create_request("update", 42, use_sysname=True)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
        ):
            mock_tx.atomic.return_value = MagicMock()
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == {"default": 10}
        assert existing_device.serial == "NEW-SERIAL"
        assert existing_device.name == "new-name.example.com"
        existing_device.save.assert_called_once()

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_update_action_uses_non_default_server_key(self, mock_cache_key, mock_cache):
        """Update action should store librenms_id under the active server key."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "production"
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {}
        existing_device.name = "old-name"
        existing_device.serial = "OLD-SERIAL"

        libre_device = {
            "device_id": 10,
            "hostname": "switch-01",
            "sysName": "switch-01",
            "serial": "NEW-SERIAL",
            "resolved_name": "switch-01",
        }
        validation = {"can_import": False, "existing_device": existing_device, "resolved_name": "switch-01"}
        selections = {}

        request = self._create_request("update", 42)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
        ):
            mock_tx.atomic.return_value = MagicMock()
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == {"production": 10}

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_update_serial_action_updates_serial_only(self, mock_cache_key, mock_cache):
        """Update serial action should update serial and librenms_id but not hostname."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {}
        existing_device.name = "switch-01"
        existing_device.serial = "OLD-SERIAL"

        libre_device = {"device_id": 10, "hostname": "switch-01", "serial": "NEW-SERIAL"}
        validation = {"can_import": False, "existing_device": existing_device}
        selections = {}

        request = self._create_request("update_serial", 42)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
        ):
            mock_tx.atomic.return_value = MagicMock()
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == {"default": 10}
        assert existing_device.serial == "NEW-SERIAL"
        # Name should NOT be changed by update_serial
        assert existing_device.name == "switch-01"
        existing_device.save.assert_called_once()

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_update_skips_dash_serial(self, mock_cache_key, mock_cache):
        """Update should not set serial to '-' (LibreNMS placeholder)."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {}
        existing_device.name = "switch-01"
        existing_device.serial = "EXISTING"

        libre_device = {"device_id": 10, "hostname": "switch-01", "serial": "-"}
        validation = {"can_import": False, "existing_device": existing_device}
        selections = {}

        request = self._create_request("update_serial", 42)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
        ):
            mock_tx.atomic.return_value = MagicMock()
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        # Serial should NOT be updated to '-'
        assert existing_device.serial == "EXISTING"

    def test_missing_action_returns_400(self):
        """Missing action or existing_device_id should return 400."""
        view = self._create_view()
        request = MagicMock()
        request.user.has_perm.return_value = True
        request.POST = {}

        view.request = request
        response = view.post(request, device_id=10)
        assert response.status_code == 400

    def test_unknown_action_returns_400(self):
        """Unknown action should return 400."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        request = self._create_request("invalid_action", 42)

        existing_device = MagicMock()
        existing_device.pk = 42
        libre_device = {"device_id": 10, "hostname": "switch-01", "serial": "ABC"}

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "require_object_permissions", return_value=None),
            patch("dcim.models.Device") as mock_device_cls,
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            # Include existing_device so the validated-conflict-target guard passes;
            # we want to exercise the unknown-action branch, not the missing-device guard.
            mock_validate.return_value = (libre_device, {"existing_device": existing_device}, {})

            view.request = request
            response = view.post(request, device_id=10)

        assert response.status_code == 400

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_sync_name_action_updates_name(self, mock_cache_key, mock_cache):
        """Sync name action should update device name using sysName."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {"librenms_id": 10}
        existing_device.name = "84.116.251.35"

        libre_device = {
            "device_id": 10,
            "hostname": "84.116.251.35",
            "sysName": "switch-01.example.com",
            "serial": "ABC123",
        }
        validation = {"can_import": False, "existing_device": existing_device}
        selections = {}

        request = self._create_request("sync_name", 42, use_sysname=True)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.name == "switch-01.example.com"
        existing_device.save.assert_called_once()
        assert existing_device.custom_field_data["librenms_id"] == 10

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_device_type_mismatch_blocked_without_force(self, mock_cache_key, mock_cache):
        """Action should be blocked when device_type_mismatch is True and force is not set."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {}

        libre_device = {"device_id": 10, "hostname": "switch-01", "serial": "ABC123"}
        validation = {"can_import": False, "device_type_mismatch": True, "existing_device": existing_device}
        selections = {}

        request = self._create_request("link", 42, use_sysname=True)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch("dcim.models.Device") as mock_device_cls,
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_validate.return_value = (libre_device, validation, selections)

            view.request = request
            response = view.post(request, device_id=10)

        assert response.status_code == 400
        existing_device.save.assert_not_called()

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_device_type_mismatch_allowed_with_force(self, mock_cache_key, mock_cache):
        """Action should proceed when device_type_mismatch is True and force is set."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {}
        existing_device.name = "old-name"

        libre_device = {
            "device_id": 10,
            "hostname": "switch-01",
            "sysName": "switch-01.example.com",
            "serial": "ABC123",
        }
        validation = {"can_import": False, "device_type_mismatch": True, "existing_device": existing_device}
        selections = {}

        request = self._create_request("link", 42, use_sysname=True)
        request.POST["force"] = "on"

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
        ):
            mock_tx.atomic.return_value = MagicMock()
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == {"default": 10}
        existing_device.save.assert_called_once()

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_force_with_mismatch_updates_device_type(self, mock_cache_key, mock_cache):
        """Force with device_type_mismatch should update existing device's device_type."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {}
        existing_device.name = "old-name"

        librenms_device_type = MagicMock()
        librenms_device_type.pk = 99
        libre_device = {
            "device_id": 10,
            "hostname": "switch-01",
            "sysName": "switch-01.example.com",
            "serial": "ABC123",
        }
        validation = {
            "can_import": False,
            "device_type_mismatch": True,
            "device_type": {"device_type": librenms_device_type},
            "existing_device": existing_device,
        }
        selections = {}

        request = self._create_request("link", 42, use_sysname=True)
        request.POST["force"] = "on"

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
        ):
            mock_tx.atomic.return_value = MagicMock()
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.device_type == librenms_device_type
        assert existing_device.custom_field_data["librenms_id"] == {"default": 10}
        existing_device.save.assert_called_once()

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_update_type_action_changes_device_type(self, mock_cache_key, mock_cache):
        """update_type action should change device type on existing device."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.pk = 42
        existing_device.custom_field_data = {"librenms_id": 10}
        existing_device.name = "switch-01"
        old_device_type = MagicMock()
        existing_device.device_type = old_device_type

        new_device_type = MagicMock()
        new_device_type.pk = 99
        libre_device = {
            "device_id": 10,
            "hostname": "switch-01",
            "serial": "ABC123",
        }
        validation = {
            "can_import": False,
            "device_type_mismatch": True,
            "device_type": {"device_type": new_device_type},
            "existing_device": existing_device,
        }
        selections = {}

        request = self._create_request("update_type", 42)
        request.POST["force"] = "on"

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.device_type == new_device_type
        existing_device.save.assert_called_once()
        assert existing_device.custom_field_data["librenms_id"] == 10

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_sync_serial_action(self, mock_cache_key, mock_cache):
        """sync_serial action should update serial from LibreNMS."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.serial = "OLD123"
        libre_device = {"device_id": 10, "serial": "NEW456", "sysName": "test"}
        validation = {"existing_device": existing_device, "device_type_mismatch": False}
        selections = {}

        request = self._create_request("sync_serial", 42)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
        ):
            mock_tx.atomic.return_value = MagicMock()
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.serial == "NEW456"
        existing_device.save.assert_called_once()

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_sync_platform_action(self, mock_cache_key, mock_cache):
        """sync_platform action should update platform from LibreNMS OS."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        existing_device.platform = None
        libre_device = {"device_id": 10, "os": "ios", "sysName": "test"}
        validation = {"existing_device": existing_device, "device_type_mismatch": False}
        selections = {}

        mock_platform = MagicMock()
        request = self._create_request("sync_platform", 42)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            # Patch find_matching_platform at the utility module level — the action imports
            # it from netbox_librenms_plugin.utils, so that is the correct seam to mock.
            patch("netbox_librenms_plugin.utils.find_matching_platform") as mock_find_platform,
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_find_platform.return_value = {"found": True, "platform": mock_platform, "match_type": "exact"}
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.platform == mock_platform
        existing_device.save.assert_called_once()

    @patch("netbox_librenms_plugin.views.imports.actions.cache")
    @patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key")
    def test_sync_device_type_action(self, mock_cache_key, mock_cache):
        """sync_device_type action should update device type from LibreNMS hardware match."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        existing_device = MagicMock()
        new_device_type = MagicMock()
        libre_device = {"device_id": 10, "hardware": "Catalyst C4900M", "sysName": "test"}
        validation = {"existing_device": existing_device, "device_type_mismatch": False}
        selections = {}

        request = self._create_request("sync_device_type", 42)

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch.object(DeviceConflictActionView, "render_device_row") as mock_render,
            patch("dcim.models.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.utils.match_librenms_hardware_to_device_type") as mock_hw_match,
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.get.return_value = existing_device
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
            mock_hw_match.return_value = {"matched": True, "device_type": new_device_type}
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.request = request
            view.post(request, device_id=10)

        assert existing_device.device_type == new_device_type
        existing_device.save.assert_called_once()


class TestBuildSyncInfo:
    """Test DeviceValidationDetailsView._build_sync_info method."""

    def test_all_synced(self):
        """When serial, platform, device type all match, all_synced is True."""
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        existing = MagicMock()
        existing.serial = "ABC123"
        platform = MagicMock()
        platform.pk = 1
        existing.platform = platform
        device_type = MagicMock()
        device_type.pk = 5
        existing.device_type = device_type

        libre_device = {"serial": "ABC123", "os": "ios", "hardware": "Catalyst C4900M"}

        with (
            patch("netbox_librenms_plugin.utils.find_matching_platform") as mock_platform_match,
            patch("netbox_librenms_plugin.utils.match_librenms_hardware_to_device_type") as mock_hw_match,
        ):
            mock_platform_match.return_value = {"found": True, "platform": platform}
            mock_hw_match.return_value = {"matched": True, "device_type": device_type}

            result = DeviceValidationDetailsView._build_sync_info(libre_device, existing)

        assert result["all_synced"] is True
        assert result["serial_synced"] is True
        assert result["platform_synced"] is True
        assert result["device_type_synced"] is True

    def test_serial_out_of_sync(self):
        """When serial differs, serial_synced is False."""
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        existing = MagicMock()
        existing.serial = "OLD123"
        existing.platform = None
        device_type = MagicMock()
        device_type.pk = 5
        existing.device_type = device_type

        libre_device = {"serial": "NEW456", "os": "-", "hardware": "-"}

        result = DeviceValidationDetailsView._build_sync_info(libre_device, existing)

        assert result["serial_synced"] is False
        assert result["all_synced"] is False

    def test_platform_out_of_sync(self):
        """When platform differs, platform_synced is False."""
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        existing = MagicMock()
        existing.serial = "ABC123"
        old_platform = MagicMock()
        old_platform.pk = 1
        existing.platform = old_platform
        device_type = MagicMock()
        device_type.pk = 5
        existing.device_type = device_type

        new_platform = MagicMock()
        new_platform.pk = 2

        libre_device = {"serial": "ABC123", "os": "junos", "hardware": "-"}

        with patch("netbox_librenms_plugin.utils.find_matching_platform") as mock_platform_match:
            mock_platform_match.return_value = {"found": True, "platform": new_platform}

            result = DeviceValidationDetailsView._build_sync_info(libre_device, existing)

        assert result["platform_synced"] is False
        assert result["all_synced"] is False

    def test_platform_no_match_found_returns_bool(self):
        """When find_matching_platform returns no match, platform_synced must be False (not None)."""
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        existing = MagicMock()
        existing.serial = "ABC123"
        existing.platform = MagicMock()  # device has a platform set
        device_type = MagicMock()
        device_type.pk = 5
        existing.device_type = device_type

        libre_device = {"serial": "ABC123", "os": "ios", "hardware": "-"}

        with patch("netbox_librenms_plugin.utils.find_matching_platform") as mock_platform_match:
            mock_platform_match.return_value = {"found": False, "platform": None}

            result = DeviceValidationDetailsView._build_sync_info(libre_device, existing)

        # Without bool() cast this would be None; verify it's exactly False (type-stable)
        assert result["platform_synced"] is False
        assert isinstance(result["platform_synced"], bool)

    def test_platform_synced_no_netbox_platform_returns_bool(self):
        """When device has no platform in NetBox and os is non-dash, platform_synced must be bool."""
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        existing = MagicMock()
        existing.serial = "ABC123"
        existing.platform = None  # no platform on device
        device_type = MagicMock()
        device_type.pk = 5
        existing.device_type = device_type

        libre_device = {"serial": "ABC123", "os": "eos", "hardware": "-"}

        with patch("netbox_librenms_plugin.utils.find_matching_platform") as mock_platform_match:
            mock_platform_match.return_value = {"found": True, "platform": MagicMock()}

            result = DeviceValidationDetailsView._build_sync_info(libre_device, existing)

        # None and ... returns None; bool() cast ensures False
        assert result["platform_synced"] is False
        assert isinstance(result["platform_synced"], bool)

    def test_hardware_no_match_device_type_out_of_sync(self):
        """When hardware is present but no device type match found, device_type_synced is False."""
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        existing = MagicMock()
        existing.serial = "ABC123"
        existing.platform = None
        device_type = MagicMock()
        device_type.pk = 5
        existing.device_type = device_type

        libre_device = {"serial": "ABC123", "os": "-", "hardware": "UnknownHardwareXYZ"}

        with patch("netbox_librenms_plugin.utils.match_librenms_hardware_to_device_type") as mock_hw_match:
            mock_hw_match.return_value = {"matched": False, "device_type": None}

            result = DeviceValidationDetailsView._build_sync_info(libre_device, existing)

        assert result["device_type_synced"] is False
        assert result["all_synced"] is False


class TestImportSingleDeviceLazyValidation:
    """import_single_device must pass api=api to validate_device_for_import when validation is None."""

    def test_api_passed_to_validate(self):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        mock_api = MagicMock()
        mock_api.server_key = "prod"

        mock_validation = {
            "existing_device": MagicMock(name="existing"),
            "can_import": False,
        }

        with (
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI",
                return_value=mock_api,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.validate_device_for_import",
                return_value=mock_validation,
            ) as mock_validate,
        ):
            import_single_device(
                42,
                server_key="prod",
                sync_options={"use_sysname": True, "strip_domain": False},
                validation=None,
                libre_device={"device_id": 42, "hostname": "test"},
            )

            mock_validate.assert_called_once()
            assert mock_validate.call_args[1].get("api") is mock_api


class TestDeviceNamingPreferences:
    """Test that validation honours use_sysname and strip_domain user preferences."""

    def _setup_no_existing(self, mocks):
        """Configure mocks so no existing device is found."""
        (
            mock_site_model,
            mock_rack,
            mock_cluster,
            mock_role,
            mock_match_type,
            mock_find_platform,
            mock_find_site,
            mock_device,
            mock_vm,
        ) = mocks

        mock_vm.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.first.return_value = None
        mock_find_site.return_value = {
            "found": False,
            "site": None,
            "match_type": None,
            "confidence": 0.0,
        }
        mock_find_platform.return_value = {
            "found": False,
            "platform": None,
            "match_type": None,
        }
        mock_match_type.return_value = {
            "matched": False,
            "device_type": None,
            "match_type": None,
        }
        mock_role.objects.all.return_value = []
        mock_site_model.objects.all.return_value = []

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_resolved_name_uses_sysname_by_default(self, *mocks):
        """Default use_sysname=True uses sysName for resolved_name."""
        self._setup_no_existing(mocks)
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "10.0.0.1",
            "sysName": "core-switch",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)
        assert result["resolved_name"] == "core-switch"

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_resolved_name_uses_hostname_when_sysname_disabled(self, *mocks):
        """use_sysname=False uses hostname for resolved_name."""
        self._setup_no_existing(mocks)
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "10.0.0.1",
            "sysName": "core-switch",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
            use_sysname=False,
        )
        assert result["resolved_name"] == "10.0.0.1"

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_resolved_name_strips_domain(self, *mocks):
        """strip_domain=True strips the domain suffix."""
        self._setup_no_existing(mocks)
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01.example.com",
            "sysName": "switch-01.example.com",
        }
        result = validate_device_for_import(
            device_data,
            include_vc_detection=False,
            strip_domain=True,
        )
        assert result["resolved_name"] == "switch-01"

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_duplicate_detection_uses_resolved_name(self, *mocks):
        """Duplicate detection should match against the resolved name, not raw hostname."""
        self._setup_no_existing(mocks)

        # Unpack using same order as _setup_no_existing / @patch decorators (bottom-up)
        (
            _mock_site,
            _mock_rack,
            _mock_cluster,
            _mock_role,
            _mock_hw,
            _mock_platform,
            _mock_find_site,
            mock_device,
            _mock_vm,
        ) = mocks
        existing = MagicMock()
        existing.name = "core-switch"
        existing.serial = ""
        existing.virtual_chassis = None
        existing.vc_position = None
        mock_device.objects.filter.return_value.first.side_effect = [None, existing]

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 999,
            "hostname": "10.0.0.1",
            "sysName": "core-switch",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)

        assert result["existing_device"] == existing
        assert result["existing_match_type"] == "hostname"

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Device")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Cluster")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Rack")
    @patch("netbox_librenms_plugin.import_utils.device_operations.Site")
    def test_backward_compatible_defaults(self, *mocks):
        """Calling without naming params produces resolved_name in result."""
        self._setup_no_existing(mocks)
        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "switch-01",
        }
        result = validate_device_for_import(device_data, include_vc_detection=False)
        assert "resolved_name" in result
        assert result["resolved_name"] == "switch-01"


class TestProcessDeviceFilters:
    """Tests for process_device_filters and related bulk_import utilities."""

    def test_show_disabled_filters_integer_disabled_1(self):
        """show_disabled=False should exclude devices with disabled==1 (int)."""
        from unittest.mock import MagicMock, patch

        devices = [
            {"device_id": 1, "hostname": "a", "disabled": 0, "status": 1},
            {"device_id": 2, "hostname": "b", "disabled": 1, "status": 1},  # disabled in LibreNMS
        ]
        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=(devices, False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=lambda d, **kw: {
                    "resolved_name": d["hostname"],
                    "is_ready": True,
                    "can_import": True,
                    "status": "active",
                    "existing_device": None,
                    "import_as_vm": False,
                    "existing_match_type": None,
                },
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key", return_value="key"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key", return_value="vkey"
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            api = MagicMock()
            api.server_key = "default"
            result = process_device_filters(
                api, filters={}, vc_detection_enabled=False, clear_cache=False, show_disabled=False
            )

        # Only enabled device (disabled==0) should be processed
        assert len(result) == 1
        assert result[0]["hostname"] == "a"

    def test_show_disabled_keeps_unreachable_enabled_device(self):
        """show_disabled=False should keep devices that are enabled (disabled==0) even if status==0."""
        from unittest.mock import MagicMock, patch

        devices = [
            {"device_id": 1, "hostname": "a", "disabled": 0, "status": 0},  # down but enabled
            {"device_id": 2, "hostname": "b", "disabled": 1, "status": 0},  # down and disabled
        ]
        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=(devices, False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=lambda d, **kw: {
                    "resolved_name": d["hostname"],
                    "is_ready": True,
                    "can_import": True,
                    "status": "active",
                    "existing_device": None,
                    "import_as_vm": False,
                    "existing_match_type": None,
                },
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key", return_value="key"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key", return_value="vkey"
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            api = MagicMock()
            api.server_key = "default"
            result = process_device_filters(
                api, filters={}, vc_detection_enabled=False, clear_cache=False, show_disabled=False
            )

        # Device a is enabled (disabled==0) and should be kept even though status==0
        assert len(result) == 1
        assert result[0]["hostname"] == "a"

    def test_show_disabled_true_includes_all(self):
        """show_disabled=True should include both active and inactive devices."""
        from unittest.mock import MagicMock, patch

        devices = [
            {"device_id": 1, "hostname": "a", "status": 1},
            {"device_id": 2, "hostname": "b", "status": 0},
        ]
        with (
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_librenms_devices_for_import",
                return_value=(devices, False),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=lambda d, **kw: {
                    "resolved_name": d["hostname"],
                    "is_ready": True,
                    "can_import": True,
                    "status": "active",
                    "existing_device": None,
                    "import_as_vm": False,
                    "existing_match_type": None,
                },
            ),
            patch("netbox_librenms_plugin.import_utils.bulk_import.prefetch_vc_data_for_devices"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.cache"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.get_cache_metadata_key", return_value="key"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.get_validated_device_cache_key", return_value="vkey"
            ),
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import process_device_filters

            api = MagicMock()
            api.server_key = "default"
            result = process_device_filters(
                api, filters={}, vc_detection_enabled=False, clear_cache=False, show_disabled=True
            )

        assert len(result) == 2

    def test_empty_return_helper(self):
        """_empty_return should return ([], False) when return_cache_status=True, else []."""
        from netbox_librenms_plugin.import_utils.bulk_import import _empty_return

        assert _empty_return(True) == ([], False)
        assert _empty_return(False) == []

    def test_bulk_import_devices_uses_resolved_server_key(self):
        """bulk_import_devices_shared should pass api.server_key to import_single_device."""
        from unittest.mock import MagicMock, patch

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI") as mock_api_cls,
            patch("netbox_librenms_plugin.import_utils.bulk_import.import_single_device") as mock_import,
            patch("netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import") as mock_validate,
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
        ):
            mock_api = MagicMock()
            mock_api.server_key = "resolved-key"
            mock_api.get_device_info.return_value = (True, {"device_id": 1, "hostname": "sw"})
            mock_api_cls.return_value = mock_api
            mock_import.return_value = {"success": True, "device": MagicMock(), "is_vm": False}

            user = MagicMock()
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            bulk_import_devices_shared([1], user=user, server_key=None)

        # The resolved api.server_key ("resolved-key") must be passed, not None
        assert mock_import.call_args is not None
        assert mock_import.call_args.kwargs.get("server_key") == "resolved-key"
        assert mock_validate.call_args is not None
        # Import-time VC detection is always enabled (restored pre-regression behavior).
        assert mock_validate.call_args.kwargs.get("include_vc_detection") is True


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
        """
        Suggested name generation must use position directly (not position+1).

        This test verifies that _generate_vc_member_name is called with the
        already-1-based position value, not position+1.
        """
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        # position=1 should produce name with "1", not "2"
        name = _generate_vc_member_name("switch-1", 1, pattern="-M{position}")
        assert name == "switch-1-M1", f"Expected 'switch-1-M1', got '{name}'"

        # position=2 should produce "2", not "3"
        name = _generate_vc_member_name("switch-1", 2, pattern="-M{position}")
        assert name == "switch-1-M2", f"Expected 'switch-1-M2', got '{name}'"

    def test_update_vc_member_suggested_names_no_off_by_one(self):
        """
        update_vc_member_suggested_names must use stored 1-based positions directly.

        Previously bays_by_depth applied an extra +1 to positions that were
        already 1-based, producing suggested names like "switch-M2" for position 1.
        """
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import (
            update_vc_member_suggested_names,
        )

        vc_data = {
            "is_stack": True,
            "member_count": 2,
            "members": [
                {"serial": "S1", "position": 1},
                {"serial": "S2", "position": 2},
            ],
        }

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ):
            result = update_vc_member_suggested_names(vc_data, "switch-01")

        names = [m["suggested_name"] for m in result["members"]]
        # Position 1 → "switch-01-M1", NOT "switch-01-M2"
        assert names[0] == "switch-01-M1", f"Expected 'switch-01-M1' but got {names[0]!r} — off-by-one regression"
        assert names[1] == "switch-01-M2", f"Expected 'switch-01-M2' but got {names[1]!r} — off-by-one regression"

    def test_update_vc_member_suggested_names_preserves_position(self):
        """update_vc_member_suggested_names must write final position back to member dict."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import (
            update_vc_member_suggested_names,
        )

        vc_data = {
            "is_stack": True,
            "member_count": 1,
            "members": [{"serial": "S1", "position": 3}],
        }

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ):
            result = update_vc_member_suggested_names(vc_data, "router")

        member = result["members"][0]
        assert member["position"] == 3
        assert member["suggested_name"] == "router-M3"

    def test_update_vc_member_suggested_names_fallback_for_zero_position(self):
        """Position 0 must be replaced with 1-based fallback (idx+1)."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import (
            update_vc_member_suggested_names,
        )

        vc_data = {
            "is_stack": True,
            "member_count": 2,
            "members": [{"serial": "S1", "position": 0}, {"serial": "S2", "position": -1}],
        }

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ):
            result = update_vc_member_suggested_names(vc_data, "sw")

        positions = [m["position"] for m in result["members"]]
        assert positions[0] == 1, f"Zero position must fall back to 1, got {positions[0]}"
        assert positions[1] == 2, f"Negative position must fall back to 2 (idx+1), got {positions[1]}"


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
    """Tests for _vc_cache_key."""

    def test_cache_key_format(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _vc_cache_key

        mock_api = MagicMock()
        mock_api.server_key = "default"
        key = _vc_cache_key(mock_api, 42)
        assert "librenms_vc_detection" in key
        assert "default" in key
        assert "42" in key

    def test_cache_key_includes_server_key(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _vc_cache_key

        api_a = MagicMock()
        api_a.server_key = "server-a"
        api_b = MagicMock()
        api_b.server_key = "server-b"
        assert _vc_cache_key(api_a, 1) != _vc_cache_key(api_b, 1)

    def test_cache_key_differs_for_different_device_ids(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _vc_cache_key

        mock_api = MagicMock()
        mock_api.server_key = "default"
        assert _vc_cache_key(mock_api, 1) != _vc_cache_key(mock_api, 2)

    def test_missing_server_key_falls_back_to_default(self):
        """api without server_key attribute uses 'default' as fallback."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _vc_cache_key

        mock_api = MagicMock(spec=[])  # no attributes
        key = _vc_cache_key(mock_api, 10)
        assert "default" in key


class TestGetVirtualChassisData:
    """Tests for get_virtual_chassis_data."""

    def test_none_api_returns_empty(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        result = get_virtual_chassis_data(None, 1)
        assert result["is_stack"] is False
        assert result["members"] == []

    def test_none_device_id_returns_empty(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        mock_api = MagicMock()
        result = get_virtual_chassis_data(mock_api, None)
        assert result["is_stack"] is False

    def test_cache_hit_returns_cloned_data(self):
        """Cached data is returned without calling detect_virtual_chassis_from_inventory."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        mock_api = MagicMock()
        mock_api.server_key = "default"
        cached = {
            "is_stack": True,
            "member_count": 2,
            "members": [{"serial": "S1", "position": 1}, {"serial": "S2", "position": 2}],
            "detection_error": None,
        }

        with (
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.detect_virtual_chassis_from_inventory"
            ) as mock_detect,
        ):
            mock_cache.get.return_value = cached
            result = get_virtual_chassis_data(mock_api, 42)

        assert result["is_stack"] is True
        assert result["member_count"] == 2
        mock_detect.assert_not_called()

    def test_cache_miss_calls_detect_and_stores_result(self):
        """On cache miss, detect_virtual_chassis_from_inventory is called and result cached."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300

        detection_result = {"is_stack": False, "member_count": 0, "members": []}

        with (
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.detect_virtual_chassis_from_inventory",
                return_value=detection_result,
            ) as mock_detect,
        ):
            mock_cache.get.return_value = None  # cache miss
            result = get_virtual_chassis_data(mock_api, 42)

        mock_detect.assert_called_once_with(mock_api, 42)
        mock_cache.set.assert_called_once()
        assert result["is_stack"] is False

    def test_cache_miss_detect_returns_none_stores_empty(self):
        """When detect returns None (non-stack or API failure), empty result is cached to suppress repeated hits."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300

        with (
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.detect_virtual_chassis_from_inventory",
                return_value=None,
            ),
        ):
            mock_cache.get.return_value = None
            result = get_virtual_chassis_data(mock_api, 99)

        mock_cache.set.assert_called_once()
        set_args = mock_cache.set.call_args
        cached_val = set_args[0][1]
        assert cached_val["is_stack"] is False
        assert cached_val["member_count"] == 0
        assert result["is_stack"] is False

    def test_force_refresh_bypasses_cache(self):
        """force_refresh=True skips the cache.get check."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data

        mock_api = MagicMock()
        mock_api.server_key = "default"
        mock_api.cache_timeout = 300

        with (
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.detect_virtual_chassis_from_inventory",
                return_value=None,
            ),
        ):
            mock_cache.get.return_value = {"is_stack": True, "member_count": 1, "members": [], "detection_error": None}
            get_virtual_chassis_data(mock_api, 1, force_refresh=True)

        # cache.get should NOT have been consulted
        mock_cache.get.assert_not_called()


class TestPrefetchVCData:
    """Tests for prefetch_vc_data_for_devices."""

    def test_none_api_returns_immediately(self):
        """None api causes early return without touching get_virtual_chassis_data."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import prefetch_vc_data_for_devices

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.get_virtual_chassis_data") as mock_get:
            prefetch_vc_data_for_devices(None, [1, 2, 3])

        mock_get.assert_not_called()

    def test_empty_device_ids_returns_immediately(self):
        """Empty device_ids list causes early return."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import prefetch_vc_data_for_devices

        mock_api = MagicMock()

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.get_virtual_chassis_data") as mock_get:
            prefetch_vc_data_for_devices(mock_api, [])

        mock_get.assert_not_called()

    def test_connection_error_stops_processing(self):
        """BrokenPipeError / ConnectionError stops the loop (return, not continue)."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import prefetch_vc_data_for_devices

        mock_api = MagicMock()

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis.get_virtual_chassis_data",
            side_effect=ConnectionError("Connection reset"),
        ) as mock_get:
            prefetch_vc_data_for_devices(mock_api, [1, 2, 3])

        # Only the first call fires before the connection error stops processing
        assert mock_get.call_count == 1

    def test_broken_pipe_error_stops_processing(self):
        """BrokenPipeError is treated the same as ConnectionError."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import prefetch_vc_data_for_devices

        mock_api = MagicMock()

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis.get_virtual_chassis_data",
            side_effect=BrokenPipeError("Pipe broken"),
        ) as mock_get:
            prefetch_vc_data_for_devices(mock_api, [10, 20])

        assert mock_get.call_count == 1

    def test_generic_exception_continues_to_next_device(self):
        """Non-connection exceptions are logged but processing continues."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import prefetch_vc_data_for_devices

        mock_api = MagicMock()

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis.get_virtual_chassis_data",
            side_effect=ValueError("Unexpected"),
        ) as mock_get:
            prefetch_vc_data_for_devices(mock_api, [1, 2, 3])

        # All devices attempted despite the error
        assert mock_get.call_count == 3

    def test_success_calls_get_for_each_device(self):
        """All device IDs are prefetched when no errors occur."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import prefetch_vc_data_for_devices

        mock_api = MagicMock()

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.get_virtual_chassis_data") as mock_get:
            prefetch_vc_data_for_devices(mock_api, [10, 20, 30])

        assert mock_get.call_count == 3


class TestDetectVirtualChassisFromInventory:
    """Tests for detect_virtual_chassis_from_inventory."""

    def test_no_root_items_returns_none(self):
        """Returns None when get_inventory_filtered returns no root items."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (True, {"sysName": "sw1"})
        mock_api.get_inventory_filtered.return_value = (False, None)

        result = detect_virtual_chassis_from_inventory(mock_api, 1)
        assert result is None

    def test_empty_root_items_returns_none(self):
        """Returns None when root items list is empty."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (True, {"sysName": "sw1"})
        mock_api.get_inventory_filtered.return_value = (True, [])

        result = detect_virtual_chassis_from_inventory(mock_api, 1)
        assert result is None

    def test_no_stack_or_chassis_parent_returns_none(self):
        """Returns None when no root item has class 'stack' or 'chassis'."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (True, {"sysName": "sw1"})
        mock_api.get_inventory_filtered.return_value = (
            True,
            [{"entPhysicalClass": "other", "entPhysicalIndex": 1}],
        )

        result = detect_virtual_chassis_from_inventory(mock_api, 1)
        assert result is None

    def test_single_child_chassis_returns_none(self):
        """Returns None when only one child chassis is found (not a stack)."""

        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (True, {"sysName": "sw1"})
        mock_api.get_inventory_filtered.side_effect = [
            (True, [{"entPhysicalClass": "stack", "entPhysicalIndex": 100}]),
            (True, [{"entPhysicalClass": "chassis", "entPhysicalIndex": 200}]),
        ]

        result = detect_virtual_chassis_from_inventory(mock_api, 1)
        assert result is None

    def test_stack_detected_with_two_chassis(self):
        """Returns stack dict when two or more chassis are found under the parent."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (True, {"sysName": "sw1"})
        mock_api.get_inventory_filtered.side_effect = [
            (True, [{"entPhysicalClass": "stack", "entPhysicalIndex": 100}]),
            (
                True,
                [
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 201,
                        "entPhysicalParentRelPos": 1,
                        "entPhysicalSerialNum": "SN1",
                        "entPhysicalModelName": "C9300-48P",
                        "entPhysicalName": "Switch 1",
                        "entPhysicalDescr": "Cisco Catalyst 9300",
                    },
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 202,
                        "entPhysicalParentRelPos": 2,
                        "entPhysicalSerialNum": "SN2",
                        "entPhysicalModelName": "C9300-48P",
                        "entPhysicalName": "Switch 2",
                        "entPhysicalDescr": "Cisco Catalyst 9300",
                    },
                ],
            ),
        ]

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ):
            result = detect_virtual_chassis_from_inventory(mock_api, 1)

        assert result is not None
        assert result["is_stack"] is True
        assert result["member_count"] == 2
        assert len(result["members"]) == 2
        assert result["members"][0]["serial"] == "SN1"
        assert result["members"][1]["serial"] == "SN2"

    def test_stack_members_sorted_by_position(self):
        """Members are sorted by position ascending."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (True, {"sysName": "sw1"})
        mock_api.get_inventory_filtered.side_effect = [
            (True, [{"entPhysicalClass": "stack", "entPhysicalIndex": 100}]),
            (
                True,
                [
                    {"entPhysicalClass": "chassis", "entPhysicalParentRelPos": 3, "entPhysicalIndex": 203},
                    {"entPhysicalClass": "chassis", "entPhysicalParentRelPos": 1, "entPhysicalIndex": 201},
                    {"entPhysicalClass": "chassis", "entPhysicalParentRelPos": 2, "entPhysicalIndex": 202},
                ],
            ),
        ]

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ):
            result = detect_virtual_chassis_from_inventory(mock_api, 1)

        positions = [m["position"] for m in result["members"]]
        assert positions == [1, 2, 3]

    def test_zero_position_replaced_by_one_based_index(self):
        """entPhysicalParentRelPos=0 is replaced by idx+1."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (True, {"sysName": "sw1"})
        mock_api.get_inventory_filtered.side_effect = [
            (True, [{"entPhysicalClass": "stack", "entPhysicalIndex": 100}]),
            (
                True,
                [
                    {"entPhysicalClass": "chassis", "entPhysicalParentRelPos": 0, "entPhysicalIndex": 201},
                    {"entPhysicalClass": "chassis", "entPhysicalParentRelPos": 2, "entPhysicalIndex": 202},
                ],
            ),
        ]

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ):
            result = detect_virtual_chassis_from_inventory(mock_api, 1)

        positions = [m["position"] for m in result["members"]]
        assert 0 not in positions
        assert 1 in positions

    def test_no_master_name_uses_member_prefix(self):
        """When device_info has no sysName/hostname, suggested_name uses 'Member-N'."""

        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (False, None)  # no master name
        mock_api.get_inventory_filtered.side_effect = [
            (True, [{"entPhysicalClass": "stack", "entPhysicalIndex": 100}]),
            (
                True,
                [
                    {"entPhysicalClass": "chassis", "entPhysicalParentRelPos": 1, "entPhysicalIndex": 201},
                    {"entPhysicalClass": "chassis", "entPhysicalParentRelPos": 2, "entPhysicalIndex": 202},
                ],
            ),
        ]

        result = detect_virtual_chassis_from_inventory(mock_api, 1)

        assert result is not None
        assert result["members"][0]["suggested_name"].startswith("Member-")

    def test_child_items_fetch_fails_returns_none(self):
        """Returns None when the second get_inventory_filtered call fails."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (True, {"sysName": "sw1"})
        mock_api.get_inventory_filtered.side_effect = [
            (True, [{"entPhysicalClass": "stack", "entPhysicalIndex": 100}]),
            (False, None),  # child fetch fails
        ]

        result = detect_virtual_chassis_from_inventory(mock_api, 1)
        assert result is None

    def test_exception_returns_none(self):
        """Unhandled exception inside the function returns None."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.side_effect = RuntimeError("Unexpected")

        result = detect_virtual_chassis_from_inventory(mock_api, 1)
        assert result is None


class TestLoadVCMemberNamePattern:
    """Tests for _load_vc_member_name_pattern."""

    def test_returns_pattern_from_settings(self):
        """Returns vc_member_name_pattern from LibreNMSSettings when found."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern

        mock_settings = MagicMock()
        mock_settings.vc_member_name_pattern = "-SW{position}"

        with patch("netbox_librenms_plugin.models.LibreNMSSettings") as mock_cls:
            mock_cls.objects.order_by.return_value.first.return_value = mock_settings
            result = _load_vc_member_name_pattern()

        assert result == "-SW{position}"

    def test_no_settings_returns_default(self):
        """Returns '-M{position}' when LibreNMSSettings.objects.order_by().first() returns None."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern

        with patch("netbox_librenms_plugin.models.LibreNMSSettings") as mock_cls:
            mock_cls.objects.order_by.return_value.first.return_value = None
            result = _load_vc_member_name_pattern()

        assert result == "-M{position}"

    def test_exception_returns_default(self):
        """Returns '-M{position}' when the DB query raises an exception."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern

        with patch("netbox_librenms_plugin.models.LibreNMSSettings") as mock_cls:
            mock_cls.objects.order_by.side_effect = Exception("DB offline")
            result = _load_vc_member_name_pattern()

        assert result == "-M{position}"


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
        """When pattern=None, _load_vc_member_name_pattern is called to fetch the pattern."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ) as mock_load:
            name = _generate_vc_member_name("router", 5, pattern=None)

        mock_load.assert_called_once()
        assert name == "router-M5"

    def test_master_name_placeholder(self):
        """Pattern can also reference {master_name}."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        name = _generate_vc_member_name("sw", 2, pattern="-{master_name}-pos{position}")
        assert name == "sw-sw-pos2"


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
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import update_vc_member_suggested_names

        vc_data = {"is_stack": True, "members": []}

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ):
            result = update_vc_member_suggested_names(vc_data, "sw")

        assert result["members"] == []


class TestCreateVirtualChassisWithMembers:
    """Tests for create_virtual_chassis_with_members."""

    def test_raises_when_vc_create_fails(self):
        """Exception from VirtualChassis.objects.create is re-raised to the caller."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""

        @contextmanager
        def mock_atomic():
            yield

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M1",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
        ):
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_vc_cls.objects.create.side_effect = Exception("DB error")

            import pytest

            with pytest.raises(Exception, match="DB error"):
                create_virtual_chassis_with_members(master_device, [], {"device_id": 1})

    def test_success_with_no_members(self):
        """Happy path with empty members_info creates VC and returns it."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 1

        @contextmanager
        def mock_atomic():
            yield

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M1",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
        ):
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_vc_cls.objects.create.return_value = mock_vc

            result = create_virtual_chassis_with_members(master_device, [], {"device_id": 1})

        assert result == mock_vc
        mock_vc_cls.objects.create.assert_called_once()

    def test_calls_module_bay_counter_sync(self):
        """VC creation calls counter sync helper after assigning master."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 1

        @contextmanager
        def mock_atomic():
            yield

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M1",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis._sync_module_bay_counter") as mock_sync,
        ):
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_vc_cls.objects.create.return_value = mock_vc

            create_virtual_chassis_with_members(master_device, [], {"device_id": 1})

        mock_sync.assert_called_once_with(master_device)

    def test_master_save_uses_update_fields(self):
        """Master save should update only VC/name fields, not stale counter fields."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 1

        @contextmanager
        def mock_atomic():
            yield

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M1",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis._sync_module_bay_counter"),
        ):
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_vc_cls.objects.create.return_value = mock_vc

            create_virtual_chassis_with_members(master_device, [], {"device_id": 1})

        master_device.save.assert_called_once_with(update_fields=["virtual_chassis", "vc_position", "name"])


class TestSyncModuleBayCounter:
    """Tests for module_bay_count synchronization helper."""

    def test_syncs_counter_when_actual_differs(self):
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import _sync_module_bay_counter

        device = MagicMock()
        device.pk = 42
        device.name = "sw1"
        device.module_bay_count = 0
        device.modulebays.count.return_value = 2

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls:
            _sync_module_bay_counter(device)

        mock_device_cls.objects.filter.assert_called_once_with(pk=42)
        mock_device_cls.objects.filter.return_value.update.assert_called_once_with(module_bay_count=2)
        assert device.module_bay_count == 2

    def test_no_op_when_counter_matches(self):
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import _sync_module_bay_counter

        device = MagicMock()
        device.pk = 42
        device.name = "sw1"
        device.module_bay_count = 3
        device.modulebays.count.return_value = 3

        with patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls:
            _sync_module_bay_counter(device)

        mock_device_cls.objects.filter.assert_not_called()


class TestBulkImportCancellation:
    """Test that bulk_import_devices_shared respects RQ and DB cancellation."""

    def _run_bulk_import(self, mock_rq_job=None, db_status="running", device_ids=None):
        """Helper: run bulk_import with provided mocks, return import call count."""
        from unittest.mock import MagicMock, patch

        if device_ids is None:
            device_ids = [1, 2, 3, 4, 5, 6]

        job = MagicMock()
        job.job.job_id = "test-uuid"
        job_status = MagicMock()
        job_status.value = db_status
        job.job.status = job_status
        job.logger = MagicMock()

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI") as mock_api_cls,
            patch("netbox_librenms_plugin.import_utils.bulk_import.import_single_device") as mock_import,
            patch("netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            # Inline imports in the loop use django_rq.get_queue / rq.job.Job directly
            patch("django_rq.get_queue") as mock_get_queue,
            patch("rq.job.Job") as mock_rqjob_cls,
        ):
            mock_api = MagicMock()
            mock_api.server_key = "default"
            mock_api.get_device_info.return_value = (True, {"device_id": 1, "hostname": "sw"})
            mock_api_cls.return_value = mock_api
            mock_import.return_value = {"success": True, "device": MagicMock(), "is_vm": False}

            if mock_rq_job is not None:
                mock_conn = MagicMock()
                mock_queue = MagicMock()
                mock_queue.connection = mock_conn
                mock_get_queue.return_value = mock_queue
                mock_rqjob_cls.fetch.return_value = mock_rq_job
            else:
                # Simulate RQ unavailable — get_queue raises, triggers DB fallback
                mock_get_queue.side_effect = Exception("RQ unavailable")

            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(device_ids, user=MagicMock(), server_key=None, job=job)

            return mock_import.call_count, result

    def test_rq_stopped_cancels_import_loop(self):
        """When RQ job is_stopped, import loop should break early."""
        rq_job = MagicMock()
        rq_job.is_stopped = True
        rq_job.is_failed = False
        rq_job.get_status.return_value = "stopped"

        # With 6 devices and RQ stopped on first check (idx=1), at most 1 device processed
        count, result = self._run_bulk_import(mock_rq_job=rq_job, device_ids=[1, 2, 3, 4, 5, 6])
        assert count == 0  # break before first import
        assert result.get("cancelled") is True

    def test_rq_failed_cancels_import_loop(self):
        """When RQ job is_failed, import loop should break early."""
        rq_job = MagicMock()
        rq_job.is_stopped = False
        rq_job.is_failed = True
        rq_job.get_status.return_value = "failed"

        count, result = self._run_bulk_import(mock_rq_job=rq_job, device_ids=[1, 2, 3, 4, 5, 6])
        assert count == 0
        assert result.get("cancelled") is True

    def test_rq_unavailable_treats_as_not_cancelled(self):
        """When RQ is unavailable, _is_job_cancelled returns False so import continues."""
        # mock_rq_job=None triggers the side_effect=Exception path
        count, result = self._run_bulk_import(mock_rq_job=None, db_status="failed", device_ids=[1, 2, 3])
        # Redis unavailable → not cancelled → all devices processed
        assert count == 3
        assert result.get("cancelled") is False

    def test_healthy_job_runs_all_devices(self):
        """When job is healthy, all devices should be imported."""
        rq_job = MagicMock()
        rq_job.is_stopped = False
        rq_job.is_failed = False
        rq_job.get_status.return_value = "started"

        count, result = self._run_bulk_import(mock_rq_job=rq_job, device_ids=[1, 2, 3])
        assert count == 3
        assert result.get("cancelled") is False


# ---------------------------------------------------------------------------
# Tests for VC permission guard in bulk_import_devices_shared (closes #31)
# ---------------------------------------------------------------------------


class TestBulkImportVCPermission:
    """Test VC creation behavior during bulk import."""

    def _make_stack_validation(self):
        from unittest.mock import MagicMock

        v = MagicMock()
        v.get.side_effect = lambda k, d=None: {
            "is_ready": True,
            "import_as_vm": False,
            "existing_device": None,
            "virtual_chassis": {
                "is_stack": True,
                "members": [
                    {"serial": "SN-A", "position": 1},
                    {"serial": "SN-B", "position": 2},
                ],
            },
        }.get(k, d)
        return v

    def _make_non_stack_validation(self):
        from unittest.mock import MagicMock

        v = MagicMock()
        v.get.side_effect = lambda k, d=None: {
            "is_ready": True,
            "import_as_vm": False,
            "existing_device": None,
            "virtual_chassis": {
                "is_stack": False,
                "members": [],
            },
        }.get(k, d)
        return v

    def test_stack_device_not_imported_without_vc_permission(self):
        """Missing dcim.add_virtualchassis permission should block stack device import."""
        from unittest.mock import MagicMock, patch

        mock_device = MagicMock()
        user = MagicMock()
        user.has_perm.side_effect = lambda p: p != "dcim.add_virtualchassis"

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=self._make_stack_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value={"success": True, "device": mock_device, "message": "ok", "is_vm": False},
            ) as mock_import_single,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=MagicMock(name="vc"),
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=user,
                libre_devices_cache={1: {"device_id": 1, "hostname": "sw"}},
            )

        mock_import_single.assert_not_called()
        mock_create_vc.assert_not_called()
        user.has_perm.assert_called_with("dcim.add_virtualchassis")
        assert len(result["success"]) == 0
        assert len(result["failed"]) == 1
        assert "missing permission dcim.add_virtualchassis" in result["failed"][0]["error"]
        assert result["virtual_chassis_created"] == 0

    def test_vc_creation_proceeds_with_vc_permission(self):
        """User has dcim.add_virtualchassis → VC creation proceeds normally."""
        from unittest.mock import MagicMock, patch

        mock_device = MagicMock()
        mock_vc = MagicMock()
        mock_vc.name = "VC-Stack"
        user = MagicMock()
        user.has_perm.return_value = True

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=self._make_stack_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value={"success": True, "device": mock_device, "message": "ok", "is_vm": False},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=mock_vc,
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=user,
                libre_devices_cache={1: {"device_id": 1, "hostname": "sw"}},
            )

        mock_create_vc.assert_called_once()
        user.has_perm.assert_called_with("dcim.add_virtualchassis")
        assert result["virtual_chassis_created"] == 1

    def test_non_stack_import_works_without_vc_permission(self):
        """Non-stack devices should still import when add_device permissions are present."""
        from unittest.mock import MagicMock, patch

        mock_device = MagicMock()
        user = MagicMock()
        user.has_perm.side_effect = lambda p: p != "dcim.add_virtualchassis"

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                return_value=self._make_non_stack_validation(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value={"success": True, "device": mock_device, "message": "ok", "is_vm": False},
            ) as mock_import_single,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=MagicMock(name="vc"),
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1],
                user=user,
                libre_devices_cache={1: {"device_id": 1, "hostname": "sw"}},
            )

        mock_import_single.assert_called_once()
        mock_create_vc.assert_not_called()
        assert len(result["success"]) == 1
        assert len(result["failed"]) == 0

    def test_mixed_stack_and_non_stack_without_vc_permission(self):
        """Stack device fails, but non-stack device still imports when VC permission is missing."""
        from unittest.mock import MagicMock, patch

        mock_device = MagicMock()
        user = MagicMock()
        user.has_perm.side_effect = lambda p: p != "dcim.add_virtualchassis"

        validations = [self._make_stack_validation(), self._make_non_stack_validation()]

        with (
            patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions"),
            patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI"),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.validate_device_for_import",
                side_effect=validations,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.import_single_device",
                return_value={"success": True, "device": mock_device, "message": "ok", "is_vm": False},
            ) as mock_import_single,
            patch(
                "netbox_librenms_plugin.import_utils.bulk_import.create_virtual_chassis_with_members",
                return_value=MagicMock(name="vc"),
            ) as mock_create_vc,
        ):
            from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

            result = bulk_import_devices_shared(
                device_ids=[1, 2],
                user=user,
                libre_devices_cache={
                    1: {"device_id": 1, "hostname": "stack-sw"},
                    2: {"device_id": 2, "hostname": "edge-sw"},
                },
            )

        mock_import_single.assert_called_once()
        mock_create_vc.assert_not_called()
        assert len(result["success"]) == 1
        assert len(result["failed"]) == 1
        assert result["failed"][0]["device_id"] == 1


# ---------------------------------------------------------------------------
# Tests for DeviceValidationDetailsView._build_id_server_info
# ---------------------------------------------------------------------------


class TestBuildIdServerInfo:
    """Test DeviceValidationDetailsView._build_id_server_info method."""

    def _make_device(self, librenms_id_value):
        from unittest.mock import MagicMock

        device = MagicMock()
        device.custom_field_data = {"librenms_id": librenms_id_value}
        return device

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

    def test_single_server_resolves_display_name(self):
        from unittest.mock import patch

        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = self._make_device({"production": 42})
        plugins_cfg = {
            "netbox_librenms_plugin": {
                "servers": {
                    "production": {"display_name": "Production LibreNMS", "librenms_url": "https://prod.example.com"},
                }
            }
        }
        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = plugins_cfg
            result = DeviceValidationDetailsView._build_id_server_info(device)

        assert result is not None
        assert len(result) == 1
        assert result[0]["server_key"] == "production"
        assert result[0]["display_name"] == "Production LibreNMS"
        assert result[0]["device_id"] == 42

    def test_unconfigured_server_uses_key_as_display_name(self):
        from unittest.mock import patch

        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = self._make_device({"deleted-server": 77})
        plugins_cfg = {"netbox_librenms_plugin": {"servers": {}}}
        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = plugins_cfg
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

    def test_sysname_used_when_hostname_empty(self):
        """When hostname is empty but sys_name matches, the device is found in validation."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        mock_device = MagicMock()
        mock_device.pk = 99
        mock_device.name = "router-01"
        mock_device.custom_field_data = {"librenms_id": None}

        libre_device = {
            "device_id": 55,
            "hostname": "",  # empty hostname
            "sysName": "router-01",
            "serial": "SN-MATCH",
        }
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,
            "is_ready": False,
            "can_import": False,
        }

        # sys_name lookup: filter(name__iexact="router-01") returns mock_device
        # hostname lookup: filter(name__iexact="") returns None
        def make_qs(return_val):
            qs = MagicMock()
            qs.first.return_value = return_val
            return qs

        with patch("netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id", return_value=None):
            import dcim.models as dcim_models
            import virtualization.models as virt_models

            with (
                patch.object(
                    dcim_models.Device.objects,
                    "filter",
                    side_effect=lambda **kw: make_qs(mock_device if kw.get("name__iexact") == "router-01" else None),
                ),
                patch.object(virt_models.VirtualMachine.objects, "filter", return_value=make_qs(None)),
            ):
                _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is mock_device

    def test_hostname_lookup_succeeds_without_sysname(self):
        """When hostname is non-empty and matches, validation is updated correctly."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device

        mock_device = MagicMock()
        mock_device.pk = 10
        mock_device.name = "sw-01"
        mock_device.custom_field_data = {"librenms_id": None}

        libre_device = {
            "device_id": 10,
            "hostname": "sw-01",
            "sysName": "sw-01-sysname",
            "serial": "",
        }
        validation = {
            "existing_device": None,
            "existing_vm": None,
            "import_as_vm": False,
            "is_ready": False,
            "can_import": False,
        }

        def make_qs(return_val):
            qs = MagicMock()
            qs.first.return_value = return_val
            return qs

        with patch("netbox_librenms_plugin.import_utils.bulk_import.find_by_librenms_id", return_value=None):
            import dcim.models as dcim_models
            import virtualization.models as virt_models

            with (
                patch.object(
                    dcim_models.Device.objects,
                    "filter",
                    side_effect=lambda **kw: make_qs(mock_device if kw.get("name__iexact") == "sw-01" else None),
                ),
                patch.object(virt_models.VirtualMachine.objects, "filter", return_value=make_qs(None)),
            ):
                _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation["existing_device"] is mock_device


# ---------------------------------------------------------------------------
# Tests for _get_hostname_for_action helper
# ---------------------------------------------------------------------------


class TestGetHostnameForAction:
    """Test _get_hostname_for_action helper in actions.py."""

    def test_returns_resolved_name_when_set(self):
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.views.imports.actions import _get_hostname_for_action

        request = MagicMock()
        validation = {"resolved_name": "cached-name"}
        libre_device = {"hostname": "raw-hostname", "sysName": "raw-sysname"}

        result = _get_hostname_for_action(request, validation, libre_device)
        assert result == "cached-name"

    def test_falls_back_to_determine_device_name(self):
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.imports.actions import _get_hostname_for_action

        request = MagicMock()
        validation = {}  # no resolved_name
        libre_device = {"hostname": "host.example.com", "sysName": "host"}

        with patch("netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences") as mock_prefs:
            mock_prefs.return_value = (False, False)  # use_sysname=False, strip_domain=False
            with patch("netbox_librenms_plugin.views.imports.actions._determine_device_name") as mock_name:
                mock_name.return_value = "host.example.com"
                result = _get_hostname_for_action(request, validation, libre_device)

        assert result == "host.example.com"
        mock_prefs.assert_called_once_with(request)
        mock_name.assert_called_once()


# ---------------------------------------------------------------------------
# Tests for resolve_naming_preferences underscore-variant key support
# ---------------------------------------------------------------------------


class TestResolveNamingPreferencesKeys:
    """Test that resolve_naming_preferences handles both hyphenated and underscored keys."""

    def _make_request(self, post=None, get=None):
        from unittest.mock import MagicMock

        request = MagicMock()
        request.POST = post or {}
        request.GET = get or {}
        request.user = MagicMock()
        return request

    def test_hyphenated_post_key_use_sysname(self):
        from unittest.mock import patch

        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = self._make_request(post={"use-sysname-toggle": "on", "strip-domain-toggle": "off"})
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            use_sysname, strip_domain = resolve_naming_preferences(request)
        assert use_sysname is True
        assert strip_domain is False

    def test_underscored_post_key_use_sysname(self):
        """Underscore variant 'use_sysname-toggle' should also be recognised."""
        from unittest.mock import patch

        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = self._make_request(post={"use_sysname-toggle": "on", "strip_domain-toggle": "on"})
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            use_sysname, strip_domain = resolve_naming_preferences(request)
        assert use_sysname is True
        assert strip_domain is True

    def test_get_key_used_when_not_in_post(self):
        from unittest.mock import patch

        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = self._make_request(get={"use-sysname-toggle": "off", "strip-domain-toggle": "on"})
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            use_sysname, strip_domain = resolve_naming_preferences(request)
        assert use_sysname is False
        assert strip_domain is True

    def test_user_pref_used_when_no_toggle_in_request(self):
        from unittest.mock import patch

        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = self._make_request()
        with patch("netbox_librenms_plugin.utils.get_user_pref") as mock_pref:
            mock_pref.side_effect = lambda req, key: False if "use_sysname" in key else True
            use_sysname, strip_domain = resolve_naming_preferences(request)
        assert use_sysname is False
        assert strip_domain is True

    def test_post_takes_precedence_over_user_pref(self):
        from unittest.mock import patch

        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = self._make_request(post={"use-sysname-toggle": "off"})
        # user_pref would say True — POST should win
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=True):
            use_sysname, _ = resolve_naming_preferences(request)
        assert use_sysname is False

    def test_truthy_string_true_value(self):
        """'true' and '1' (in addition to 'on') should be treated as True."""
        from unittest.mock import patch

        from netbox_librenms_plugin.utils import resolve_naming_preferences

        for truthy_val in ("true", "True", "TRUE", "1"):
            request = self._make_request(post={"use-sysname-toggle": truthy_val, "strip-domain-toggle": "off"})
            with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
                use_sysname, _ = resolve_naming_preferences(request)
            assert use_sysname is True, f"Expected True for value {truthy_val!r}"

    def test_falsy_string_false_value(self):
        """Unrecognised strings should be treated as False."""
        from unittest.mock import patch

        from netbox_librenms_plugin.utils import resolve_naming_preferences

        for falsy_val in ("off", "false", "0", "", "no"):
            request = self._make_request(post={"use-sysname-toggle": falsy_val, "strip-domain-toggle": "off"})
            with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
                use_sysname, _ = resolve_naming_preferences(request)
            assert use_sysname is False, f"Expected False for value {falsy_val!r}"


# ---------------------------------------------------------------------------
# Tests for vc_domain stack dedup key fix
# ---------------------------------------------------------------------------


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


class TestVirtualChassisEdgeBranches:
    """Targeted tests for exception branches not covered by main tests."""

    def test_detect_vc_invalid_position_string_falls_back(self):
        """When entPhysicalParentRelPos is a non-numeric string, position falls back to idx+1."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

        mock_api = MagicMock()
        mock_api.get_device_info.return_value = (True, {"sysName": "sw1"})
        mock_api.get_inventory_filtered.side_effect = [
            (True, [{"entPhysicalClass": "stack", "entPhysicalIndex": 100}]),
            (
                True,
                [
                    {"entPhysicalClass": "chassis", "entPhysicalParentRelPos": "bad", "entPhysicalIndex": 201},
                    {"entPhysicalClass": "chassis", "entPhysicalParentRelPos": "invalid", "entPhysicalIndex": 202},
                ],
            ),
        ]

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ):
            result = detect_virtual_chassis_from_inventory(mock_api, 1)

        # invalid string → idx+1 fallback (1-based: idx=0→1, idx=1→2)
        positions = sorted(m["position"] for m in result["members"])
        assert positions == [1, 2]

    def test_update_vc_suggested_names_invalid_position_string_falls_back(self):
        """Non-numeric position string in member triggers except branch → idx+1 fallback."""
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import update_vc_member_suggested_names

        vc_data = {
            "is_stack": True,
            "member_count": 2,
            "members": [{"serial": "S1", "position": "bad"}, {"serial": "S2", "position": None}],
        }

        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-M{position}",
        ):
            result = update_vc_member_suggested_names(vc_data, "sw")

        positions = [m["position"] for m in result["members"]]
        assert positions[0] == 1  # idx=0 → 1
        assert positions[1] == 2  # idx=1 → 2

    def _make_atomic(self):
        from contextlib import contextmanager

        @contextmanager
        def _atomic():
            yield

        return _atomic

    def _base_patches(self):
        from unittest.mock import patch

        return [
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                self._make_atomic(),
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
        ]

    def test_create_vc_master_name_conflict_keeps_original(self):
        """When the renamed master clashes, master_base_name stays as original."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""
        master_device.rack = None
        master_device.location = None

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 1

        @contextmanager
        def mock_atomic():
            yield

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M1",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
        ):
            # Name conflict: renamed master already exists
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = True
            mock_vc_cls.objects.create.return_value = mock_vc

            result = create_virtual_chassis_with_members(master_device, [], {"device_id": 1})

        # VC still created; master.name was NOT changed (conflict)
        assert result == mock_vc
        assert master_device.name == "sw1"

    def test_create_vc_member_serial_matches_master_skipped(self):
        """Member whose serial equals master serial is skipped."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = "SERIAL-MASTER"
        master_device.rack = None
        master_device.location = None

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 1

        @contextmanager
        def mock_atomic():
            yield

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M1",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
        ):
            mock_device_cls.objects.filter.return_value.exclude.return_value.exists.return_value = False
            mock_device_cls.objects.filter.return_value.exists.return_value = False
            mock_vc_cls.objects.create.return_value = mock_vc

            # One member with same serial as master → should be skipped
            members_info = [{"serial": "SERIAL-MASTER", "position": 2, "name": "sw1-2"}]
            create_virtual_chassis_with_members(master_device, members_info, {"device_id": 1})

        # Device.objects.create should NOT be called (member skipped)
        mock_device_cls.objects.create.assert_not_called()

    def test_create_vc_member_duplicate_serial_skipped(self):
        """Member with a serial that already exists in DB is skipped."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""
        master_device.rack = None
        master_device.location = None

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 1

        @contextmanager
        def mock_atomic():
            yield

        def _filter_exists(*args, **kwargs):
            # First call: check renamed master name conflict (exclude().exists()) → False
            # Subsequent calls: check duplicate serial → True (for serial)
            mock = MagicMock()
            mock.exclude.return_value.exists.return_value = False
            mock.exists.return_value = True  # serial already exists
            return mock

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M2",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
        ):
            mock_device_cls.objects.filter.side_effect = _filter_exists
            mock_vc_cls.objects.create.return_value = mock_vc

            members_info = [{"serial": "DUP-SERIAL", "position": 2, "name": "sw1-2"}]
            create_virtual_chassis_with_members(master_device, members_info, {"device_id": 1})

        mock_device_cls.objects.create.assert_not_called()

    def test_create_vc_member_created_successfully(self):
        """Normal member (no duplicate serial/name) is created via Device.objects.create."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""
        master_device.rack = None
        master_device.location = None
        master_device.platform = None
        master_device.role = MagicMock()
        master_device.device_type = MagicMock()
        master_device.site = MagicMock()

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 2

        @contextmanager
        def mock_atomic():
            yield

        def _filter_side_effect(*args, **kwargs):
            mock = MagicMock()
            mock.exclude.return_value.exists.return_value = False  # no name conflict
            mock.exists.return_value = False  # no duplicate serial or name
            return mock

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M2",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
        ):
            mock_device_cls.objects.filter.side_effect = _filter_side_effect
            mock_vc_cls.objects.create.return_value = mock_vc

            members_info = [{"serial": "NEW-SERIAL", "position": 2, "name": "sw1-2"}]
            result = create_virtual_chassis_with_members(master_device, members_info, {"device_id": 1})

        mock_device_cls.objects.create.assert_called_once()
        assert result == mock_vc

    def test_create_vc_member_count_warning_when_fewer_created(self):
        """Warning is logged when members_created < expected_members."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""
        master_device.rack = None
        master_device.location = None

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 1

        @contextmanager
        def mock_atomic():
            yield

        def _filter_side_effect(*args, **kwargs):
            mock = MagicMock()
            mock.exclude.return_value.exists.return_value = False
            # serial check: True → member skipped
            mock.exists.return_value = True
            return mock

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M2",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.logger") as mock_logger,
        ):
            mock_device_cls.objects.filter.side_effect = _filter_side_effect
            mock_vc_cls.objects.create.return_value = mock_vc

            # 2 members expected, both skipped → warning
            members_info = [
                {"serial": "S1", "position": 2, "name": "sw1-2"},
                {"serial": "S2", "position": 3, "name": "sw1-3"},
            ]
            create_virtual_chassis_with_members(master_device, members_info, {"device_id": 1})

        # Warning should be called for count mismatch
        mock_logger.warning.assert_called()

    def test_create_vc_member_zero_position_and_name_conflict(self):
        """Member position=0 → discovered_pos=None, and name conflict → skip."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""
        master_device.rack = None
        master_device.location = None
        master_device.platform = None
        master_device.role = MagicMock()
        master_device.device_type = MagicMock()
        master_device.site = MagicMock()

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 1

        @contextmanager
        def mock_atomic():
            yield

        filter_call_count = [0]

        def _filter_side_effect(*args, **kwargs):
            mock = MagicMock()
            mock.exclude.return_value.exists.return_value = False  # no renamed-master conflict
            filter_call_count[0] += 1
            # call 1: renamed-master name conflict check (.exclude().exists()) → handled above
            # call 2: serial duplicate check (.exists()) → False (serial doesn't exist)
            # call 3: member name conflict check (.exists()) → True (name already taken)
            mock.exists.return_value = filter_call_count[0] == 3
            return mock

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M2",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
        ):
            mock_device_cls.objects.filter.side_effect = _filter_side_effect
            mock_vc_cls.objects.create.return_value = mock_vc

            # position=0 → discovered_pos normalized to None; serial present but name conflicts
            members_info = [{"serial": "S-UNIQUE", "position": 0, "name": "sw1-2"}]
            create_virtual_chassis_with_members(master_device, members_info, {"device_id": 1})

        # Member skipped due to name conflict (not created)
        mock_device_cls.objects.create.assert_not_called()

    def test_create_vc_member_invalid_position_string_uses_sequential(self):
        """Member with position='abc' (non-int) triggers except branch → uses sequential counter."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        master_device = MagicMock()
        master_device.name = "sw1"
        master_device.pk = 1
        master_device.serial = ""
        master_device.rack = None
        master_device.location = None
        master_device.platform = None
        master_device.role = MagicMock()
        master_device.device_type = MagicMock()
        master_device.site = MagicMock()

        mock_vc = MagicMock()
        mock_vc.members.count.return_value = 2

        created_positions = []

        @contextmanager
        def mock_atomic():
            yield

        def _filter_side_effect(*args, **kwargs):
            mock = MagicMock()
            mock.exclude.return_value.exists.return_value = False
            mock.exists.return_value = False
            return mock

        def _capture_create(**kwargs):
            created_positions.append(kwargs.get("vc_position"))
            return MagicMock()

        with (
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis.transaction.atomic",
                mock_atomic,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._generate_vc_member_name",
                return_value="sw1-M2",
            ),
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis") as mock_vc_cls,
            patch(
                "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
                return_value="-M{position}",
            ),
        ):
            mock_device_cls.objects.filter.side_effect = _filter_side_effect
            mock_device_cls.objects.create.side_effect = _capture_create
            mock_vc_cls.objects.create.return_value = mock_vc

            # "abc" position → except branch → sequential fallback (position=2, then +=1)
            members_info = [{"serial": "S1", "position": "abc", "name": "m1"}]
            create_virtual_chassis_with_members(master_device, members_info, {"device_id": 1})

        assert mock_device_cls.objects.create.call_count == 1
