"""Tests for netbox_librenms_plugin.import_utils module.

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
        assert "DC1" in key or "location" in key
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
    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
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
        """Count respects show_disabled filter parameter."""
        mock_cache.get.return_value = [
            {"device_id": 1, "hostname": "switch-01", "status": 1},
            {"device_id": 2, "hostname": "switch-02", "status": 1},
            {"device_id": 3, "hostname": "switch-03", "status": 0},  # disabled
        ]
        mock_api = MagicMock()

        from netbox_librenms_plugin.import_utils import get_device_count_for_filters

        count = get_device_count_for_filters(api=mock_api, filters={}, show_disabled=False)

        assert count == 2

    def test_get_import_device_cache_key_default_server(self):
        """Generate cache key with default server."""
        from netbox_librenms_plugin.import_utils import get_import_device_cache_key

        key = get_import_device_cache_key(device_id=123)

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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
        mock_rack.objects.filter.return_value = []
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
    @patch("virtualization.models.Cluster")
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
        mock_cluster_module,
        mock_role,
        mock_match_type,
        mock_find_platform,
        mock_find_site,
        mock_device,
        mock_vm,
        mock_cluster_local,
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
        # Cluster is imported at module level in device_operations
        mock_cluster_module.objects.all.return_value = mock_clusters
        mock_cache.get.return_value = None  # Force cache miss to trigger Cluster.objects.all()
        mock_rack.objects.filter.return_value = []
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


class TestDeviceNamingPreferences:
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


class TestNameMatchesWithNamingPreferences:
    """Test that name_matches/name_sync_available respect naming preferences and VC patterns.

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
        """Configure mocks so that a device is found by librenms_id."""
        if as_vm:
            self.mock_vm.objects.filter.return_value.first.return_value = existing_device
            self.mock_device.objects.filter.return_value.first.return_value = None
        else:
            self.mock_vm.objects.filter.return_value.first.return_value = None

            def device_filter(**kwargs):
                result = MagicMock()
                if "custom_field_data__librenms_id" in kwargs:
                    result.first.return_value = existing_device
                else:
                    result.first.return_value = None
                return result

            self.mock_device.objects.filter.side_effect = device_filter

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

        def device_filter(**kwargs):
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

        def device_filter(**kwargs):
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

        def device_filter(**kwargs):
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
        assert "reinstalled" in result["warnings"][0]

    def test_hostname_match_diff_serial_offers_update(self):
        """Hostname matches but serial differs offers update_serial action."""
        existing = MagicMock()
        existing.name = "switch-01"
        existing.serial = "OLD_SERIAL"

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(**kwargs):
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
        self.mock_rack.objects.filter.return_value = []
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

        def device_filter(**kwargs):
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
        existing.virtual_chassis = None

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(**kwargs):
            result = MagicMock()
            if "custom_field_data__librenms_id" in kwargs:
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
        self.mock_rack.objects.filter.return_value = []
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

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(**kwargs):
            result = MagicMock()
            if "custom_field_data__librenms_id" in kwargs:
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
        self.mock_rack.objects.filter.return_value = []
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

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(**kwargs):
            result = MagicMock()
            if "custom_field_data__librenms_id" in kwargs:
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
        self.mock_rack.objects.filter.return_value = []
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
        mock_existing_role = MagicMock()
        mock_existing_role.name = "Access Switch"
        existing.role = mock_existing_role

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(**kwargs):
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
        self.mock_rack.objects.filter.return_value = []
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
        existing_device_type = MagicMock()
        existing_device_type.pk = 1
        existing_device_type.__str__ = lambda self: "Old Type"
        existing.device_type = existing_device_type
        existing.role = MagicMock()

        librenms_device_type = MagicMock()
        librenms_device_type.pk = 2
        librenms_device_type.__str__ = lambda self: "New Type"

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(**kwargs):
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
        self.mock_rack.objects.filter.return_value = []
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
        same_device_type = MagicMock()
        same_device_type.pk = 1
        existing.device_type = same_device_type
        existing.role = MagicMock()

        self.mock_vm.objects.filter.return_value.first.return_value = None

        def device_filter(**kwargs):
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
        self.mock_rack.objects.filter.return_value = []
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


class TestDeviceConflictActionView:
    """Test DeviceConflictActionView conflict resolution actions."""

    def _create_view(self):
        """Create a DeviceConflictActionView instance with mocked dependencies."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.request = MagicMock()
        view.request.user.has_perm.return_value = True
        return view

    def _create_request(self, action, existing_device_id, use_sysname=False, strip_domain=False):
        """Create a mock request with POST data."""
        request = MagicMock()
        post_data = {"action": action, "existing_device_id": str(existing_device_id)}
        if use_sysname:
            post_data["use-sysname-toggle"] = "on"
        if strip_domain:
            post_data["strip-domain-toggle"] = "on"
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
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == 10
        assert existing_device.name == "switch-01.example.com"
        existing_device.save.assert_called_once()

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
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == 10
        assert existing_device.serial == "NEW-SERIAL"
        assert existing_device.name == "new-name.example.com"
        existing_device.save.assert_called_once()

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
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == 10
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
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.post(request, device_id=10)

        # Serial should NOT be updated to '-'
        assert existing_device.serial == "EXISTING"

    def test_missing_action_returns_400(self):
        """Missing action or existing_device_id should return 400."""
        view = self._create_view()
        request = MagicMock()
        request.POST = {}

        response = view.post(request, device_id=10)
        assert response.status_code == 400

    def test_unknown_action_returns_400(self):
        """Unknown action should return 400."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = self._create_view()
        request = self._create_request("invalid_action", 42)

        existing_device = MagicMock()
        libre_device = {"device_id": 10, "hostname": "switch-01", "serial": "ABC"}

        with (
            patch.object(DeviceConflictActionView, "get_validated_device_with_selections") as mock_validate,
            patch("dcim.models.Device") as mock_device_cls,
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_validate.return_value = (libre_device, {}, {})

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
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.post(request, device_id=10)

        assert existing_device.name == "switch-01.example.com"
        existing_device.save.assert_called_once()

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
            mock_validate.return_value = (libre_device, validation, selections)

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
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.post(request, device_id=10)

        assert existing_device.custom_field_data["librenms_id"] == 10
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
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.post(request, device_id=10)

        assert existing_device.device_type == librenms_device_type
        assert existing_device.custom_field_data["librenms_id"] == 10
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
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

            view.post(request, device_id=10)

        assert existing_device.device_type == new_device_type
        existing_device.save.assert_called_once()

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
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

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
            patch("dcim.models.Platform") as mock_platform_cls,
        ):
            mock_device_cls.objects.get.return_value = existing_device
            mock_platform_cls.objects.get.return_value = mock_platform
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

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
            mock_hw_match.return_value = {"matched": True, "device_type": new_device_type}
            mock_validate.return_value = (libre_device, validation, selections)
            mock_render.return_value = MagicMock()

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
