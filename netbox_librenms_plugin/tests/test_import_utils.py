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

    @patch("netbox_librenms_plugin.import_utils.cache")
    @patch("netbox_librenms_plugin.import_utils.LibreNMSAPI")
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

    @patch("netbox_librenms_plugin.import_utils.cache")
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

    @patch("netbox_librenms_plugin.import_utils.cache")
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

        devices = get_librenms_devices_for_import(
            api=mock_api, filters={}, force_refresh=True
        )

        mock_api.list_devices.assert_called_once()
        assert len(devices) == 1

    @patch("netbox_librenms_plugin.import_utils.cache")
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

    @patch("netbox_librenms_plugin.import_utils.cache")
    def test_get_device_count_excludes_disabled(self, mock_cache):
        """Count respects show_disabled filter parameter."""
        mock_cache.get.return_value = [
            {"device_id": 1, "hostname": "switch-01", "status": 1},
            {"device_id": 2, "hostname": "switch-02", "status": 1},
            {"device_id": 3, "hostname": "switch-03", "status": 0},  # disabled
        ]
        mock_api = MagicMock()

        from netbox_librenms_plugin.import_utils import get_device_count_for_filters

        count = get_device_count_for_filters(
            api=mock_api, filters={}, show_disabled=False
        )

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

        key = get_validated_device_cache_key(
            server_key="default", filters={}, device_id=100, vc_enabled=False
        )

        assert "novc" in key

    def test_get_validated_device_cache_key_with_vc(self):
        """Generate cache key with VC enabled."""
        from netbox_librenms_plugin.import_utils import get_validated_device_cache_key

        key_vc = get_validated_device_cache_key(
            server_key="default", filters={}, device_id=100, vc_enabled=True
        )
        key_novc = get_validated_device_cache_key(
            server_key="default", filters={}, device_id=100, vc_enabled=False
        )

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

    @patch("netbox_librenms_plugin.import_utils.cache")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
    @patch("netbox_librenms_plugin.import_utils.DeviceType")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
    @patch("netbox_librenms_plugin.import_utils.DeviceType")
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

        assert result["device_type"]["matched"] is False
        assert any("device type" in issue.lower() for issue in result["issues"])

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
    @patch("netbox_librenms_plugin.import_utils.DeviceType")
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
        assert result["device_type"]["matched"] is False

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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

    @patch("netbox_librenms_plugin.import_utils.cache")
    @patch("virtualization.models.Cluster")
    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
        # Cluster is imported locally in the VM path, so we need to mock it there
        mock_cluster_local.objects.all.return_value = mock_clusters
        mock_cache.get.return_value = (
            None  # Force cache miss to trigger Cluster.objects.all()
        )
        mock_rack.objects.filter.return_value = []
        mock_site_model.objects.all.return_value = []

        from netbox_librenms_plugin.import_utils import validate_device_for_import

        device_data = {
            "device_id": 1,
            "hostname": "vm-01",
        }

        result = validate_device_for_import(
            device_data, import_as_vm=True, include_vc_detection=False
        )

        assert result["import_as_vm"] is True
        assert result["cluster"]["available_clusters"] == mock_clusters

    @patch("virtualization.models.VirtualMachine")
    @patch("netbox_librenms_plugin.import_utils.Device")
    @patch("netbox_librenms_plugin.import_utils.find_matching_site")
    @patch("netbox_librenms_plugin.import_utils.find_matching_platform")
    @patch("netbox_librenms_plugin.import_utils.match_librenms_hardware_to_device_type")
    @patch("netbox_librenms_plugin.import_utils.DeviceRole")
    @patch("netbox_librenms_plugin.import_utils.Cluster")
    @patch("netbox_librenms_plugin.import_utils.Rack")
    @patch("netbox_librenms_plugin.import_utils.Site")
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
