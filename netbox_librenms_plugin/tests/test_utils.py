"""Tests for netbox_librenms_plugin.utils module.

Phase 2 tests covering device type matching, site matching,
platform matching, and conversion helper functions.
"""

from unittest.mock import MagicMock, patch

# =============================================================================
# TestDeviceTypeMatching - 5 tests
# =============================================================================


class TestDeviceTypeMatching:
    """Test device type matching logic."""

    @patch("dcim.models.DeviceType")
    def test_match_device_type_exact_match_by_part_number(self, mock_device_type):
        """Exact part_number string should match."""
        mock_dt = MagicMock(id=1, model="C9300-48P")
        mock_device_type.objects.get.return_value = mock_dt

        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("C9300-48P")

        assert result["matched"] is True
        assert result["device_type"] == mock_dt
        assert result["match_type"] == "exact"

    @patch("dcim.models.DeviceType")
    def test_match_device_type_exact_match_by_model(self, mock_device_type):
        """Exact model string should match when part_number fails."""
        mock_dt = MagicMock(id=1, model="WS-C3750X-48P")
        # Part number lookup fails, model lookup succeeds
        mock_device_type.DoesNotExist = Exception
        mock_device_type.objects.get.side_effect = [
            mock_device_type.DoesNotExist,  # part_number lookup fails
            mock_dt,  # model lookup succeeds
        ]

        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("WS-C3750X-48P")

        assert result["matched"] is True
        assert result["device_type"] == mock_dt
        assert result["match_type"] == "exact"

    @patch("dcim.models.DeviceType")
    def test_match_device_type_not_found(self, mock_device_type):
        """Returns None when no match found."""
        mock_device_type.DoesNotExist = Exception
        mock_device_type.objects.get.side_effect = mock_device_type.DoesNotExist

        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("NonexistentHardware")

        assert result["matched"] is False
        assert result["device_type"] is None
        assert result["match_type"] is None

    def test_match_device_type_empty_hardware(self):
        """Empty string returns None."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("")

        assert result["matched"] is False
        assert result["device_type"] is None

    def test_match_device_type_dash_hardware(self):
        """Dash placeholder returns None."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("-")

        assert result["matched"] is False
        assert result["device_type"] is None


# =============================================================================
# TestSiteMatching - 4 tests
# =============================================================================


class TestSiteMatching:
    """Test site matching logic."""

    @patch("dcim.models.Site")
    def test_find_site_for_location_exact_match(self, mock_site_model):
        """Location name matched to site."""
        mock_site = MagicMock(id=1, name="DC1")
        mock_site_model.objects.get.return_value = mock_site

        from netbox_librenms_plugin.utils import find_matching_site

        result = find_matching_site("DC1")

        assert result["found"] is True
        assert result["site"] == mock_site
        assert result["match_type"] == "exact"
        assert result["confidence"] == 1.0

    @patch("dcim.models.Site")
    def test_find_site_for_location_not_found(self, mock_site_model):
        """Returns None when no match."""
        mock_site_model.DoesNotExist = Exception
        mock_site_model.objects.get.side_effect = mock_site_model.DoesNotExist

        from netbox_librenms_plugin.utils import find_matching_site

        result = find_matching_site("Unknown Location")

        assert result["found"] is False
        assert result["site"] is None
        assert result["confidence"] == 0.0

    def test_find_site_for_location_empty(self):
        """Empty location returns None."""
        from netbox_librenms_plugin.utils import find_matching_site

        result = find_matching_site("")

        assert result["found"] is False
        assert result["site"] is None

    def test_find_site_for_location_dash(self):
        """Dash placeholder returns None."""
        from netbox_librenms_plugin.utils import find_matching_site

        result = find_matching_site("-")

        assert result["found"] is False
        assert result["site"] is None


# =============================================================================
# TestPlatformMatching - 4 tests
# =============================================================================


class TestPlatformMatching:
    """Test platform matching logic."""

    @patch("dcim.models.Platform")
    def test_find_platform_for_os_exact_match(self, mock_platform_model):
        """OS string matched to platform."""
        mock_platform = MagicMock(id=1, name="ios")
        mock_platform_model.objects.get.return_value = mock_platform

        from netbox_librenms_plugin.utils import find_matching_platform

        result = find_matching_platform("ios")

        assert result["found"] is True
        assert result["platform"] == mock_platform
        assert result["match_type"] == "exact"

    @patch("dcim.models.Platform")
    def test_find_platform_for_os_not_found(self, mock_platform_model):
        """Returns None when no match."""
        mock_platform_model.DoesNotExist = Exception
        mock_platform_model.objects.get.side_effect = mock_platform_model.DoesNotExist

        from netbox_librenms_plugin.utils import find_matching_platform

        result = find_matching_platform("unknown_os")

        assert result["found"] is False
        assert result["platform"] is None

    def test_find_platform_for_os_empty(self):
        """Empty OS returns None."""
        from netbox_librenms_plugin.utils import find_matching_platform

        result = find_matching_platform("")

        assert result["found"] is False
        assert result["platform"] is None

    def test_find_platform_for_os_dash(self):
        """Dash placeholder returns None."""
        from netbox_librenms_plugin.utils import find_matching_platform

        result = find_matching_platform("-")

        assert result["found"] is False
        assert result["platform"] is None


# =============================================================================
# TestConversionHelpers - 4 tests
# =============================================================================


class TestConversionHelpers:
    """Test data conversion helper functions."""

    def test_convert_speed_to_kbps_basic(self):
        """Convert bps to kbps."""
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        # 1 Gbps = 1,000,000,000 bps = 1,000,000 kbps
        result = convert_speed_to_kbps(1000000000)
        assert result == 1000000

    def test_convert_speed_to_kbps_megabit(self):
        """Convert megabit speed to kbps."""
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        # 100 Mbps = 100,000,000 bps = 100,000 kbps
        result = convert_speed_to_kbps(100000000)
        assert result == 100000

    def test_convert_speed_to_kbps_zero(self):
        """Zero handled correctly."""
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        result = convert_speed_to_kbps(0)
        assert result == 0

    def test_convert_speed_to_kbps_none(self):
        """None returns None."""
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        result = convert_speed_to_kbps(None)
        assert result is None

    def test_format_mac_address_valid(self):
        """Format valid MAC address."""
        from netbox_librenms_plugin.utils import format_mac_address

        result = format_mac_address("aabbccddeeff")
        assert result == "AA:BB:CC:DD:EE:FF"

    def test_format_mac_address_with_colons(self):
        """Format MAC address that already has colons."""
        from netbox_librenms_plugin.utils import format_mac_address

        result = format_mac_address("aa:bb:cc:dd:ee:ff")
        assert result == "AA:BB:CC:DD:EE:FF"

    def test_format_mac_address_with_dashes(self):
        """Format MAC address with dashes."""
        from netbox_librenms_plugin.utils import format_mac_address

        result = format_mac_address("aa-bb-cc-dd-ee-ff")
        assert result == "AA:BB:CC:DD:EE:FF"

    def test_format_mac_address_invalid(self):
        """Returns error message for invalid MAC."""
        from netbox_librenms_plugin.utils import format_mac_address

        result = format_mac_address("invalid")
        assert result == "Invalid MAC Address"

    def test_format_mac_address_empty(self):
        """Empty string returns empty string."""
        from netbox_librenms_plugin.utils import format_mac_address

        result = format_mac_address("")
        assert result == ""

    def test_format_mac_address_none(self):
        """None returns empty string."""
        from netbox_librenms_plugin.utils import format_mac_address

        result = format_mac_address(None)
        assert result == ""


# =============================================================================
# TestVirtualChassisHelpers - 4 tests
# =============================================================================


class TestVirtualChassisHelpers:
    """Test virtual chassis helper functions."""

    def test_get_virtual_chassis_member_no_vc(self, mock_netbox_device):
        """Device without VC returns original device."""
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        mock_netbox_device.virtual_chassis = None

        result = get_virtual_chassis_member(mock_netbox_device, "Ethernet1")

        assert result == mock_netbox_device

    def test_get_virtual_chassis_member_with_vc(self):
        """Device with VC returns correct member."""
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        mock_device = MagicMock()
        mock_member = MagicMock(name="member-1")
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.get.return_value = mock_member

        result = get_virtual_chassis_member(mock_device, "Ethernet1")

        # Should try to get VC member with position 1
        mock_device.virtual_chassis.members.get.assert_called_once_with(vc_position=1)
        assert result == mock_member

    def test_get_virtual_chassis_member_invalid_port(self):
        """Invalid port name returns original device."""
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()

        result = get_virtual_chassis_member(mock_device, "InvalidPort")

        assert result == mock_device

    def test_get_librenms_sync_device_no_vc(self, mock_netbox_device):
        """Device without VC returns itself."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        mock_netbox_device.virtual_chassis = None

        result = get_librenms_sync_device(mock_netbox_device)

        assert result == mock_netbox_device

    def test_get_librenms_sync_device_with_librenms_id(self):
        """VC member with librenms_id is returned."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        mock_device = MagicMock()
        mock_member_with_id = MagicMock()
        mock_member_with_id.cf = {"librenms_id": 123}
        mock_member_without_id = MagicMock()
        mock_member_without_id.cf = {}

        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.all.return_value = [
            mock_member_without_id,
            mock_member_with_id,
        ]

        result = get_librenms_sync_device(mock_device)

        assert result == mock_member_with_id


# =============================================================================
# TestPaginationHelpers - 2 tests
# =============================================================================


class TestPaginationHelpers:
    """Test pagination helper functions."""

    @patch("netbox_librenms_plugin.utils.get_config")
    @patch("netbox_librenms_plugin.utils.netbox_get_paginate_count")
    def test_get_table_paginate_count_from_request(self, mock_netbox_paginate, mock_config):
        """Custom per_page from request is used."""
        from netbox_librenms_plugin.utils import get_table_paginate_count

        mock_config.return_value.MAX_PAGE_SIZE = 1000
        mock_request = MagicMock()
        mock_request.GET = {"table1_per_page": "50"}

        result = get_table_paginate_count(mock_request, "table1_")

        assert result == 50

    @patch("netbox_librenms_plugin.utils.get_config")
    @patch("netbox_librenms_plugin.utils.netbox_get_paginate_count")
    def test_get_table_paginate_count_default(self, mock_netbox_paginate, mock_config):
        """Default pagination used when no override."""
        from netbox_librenms_plugin.utils import get_table_paginate_count

        mock_netbox_paginate.return_value = 25
        mock_request = MagicMock()
        mock_request.GET = {}

        result = get_table_paginate_count(mock_request, "table1_")

        assert result == 25
        mock_netbox_paginate.assert_called_once()


# =============================================================================
# TestInterfaceNameField - 3 tests
# =============================================================================


class TestInterfaceNameField:
    """Test interface name field retrieval."""

    @patch("netbox_librenms_plugin.utils.get_plugin_config")
    def test_get_interface_name_field_from_get(self, mock_plugin_config):
        """Override from GET request parameter."""
        from netbox_librenms_plugin.utils import get_interface_name_field

        mock_request = MagicMock()
        mock_request.GET = {"interface_name_field": "ifDescr"}
        mock_request.POST = {}

        result = get_interface_name_field(mock_request)

        assert result == "ifDescr"

    @patch("netbox_librenms_plugin.utils.get_plugin_config")
    def test_get_interface_name_field_from_post(self, mock_plugin_config):
        """Override from POST request parameter."""
        from netbox_librenms_plugin.utils import get_interface_name_field

        mock_request = MagicMock()
        mock_request.GET = {}
        mock_request.POST = {"interface_name_field": "ifName"}

        result = get_interface_name_field(mock_request)

        assert result == "ifName"

    @patch("netbox_librenms_plugin.utils.get_plugin_config")
    def test_get_interface_name_field_from_config(self, mock_plugin_config):
        """Falls back to plugin config."""
        from netbox_librenms_plugin.utils import get_interface_name_field

        mock_plugin_config.return_value = "ifAlias"
        mock_request = MagicMock()
        mock_request.GET = {}
        mock_request.POST = {}

        result = get_interface_name_field(mock_request)

        assert result == "ifAlias"
        mock_plugin_config.assert_called_with("netbox_librenms_plugin", "interface_name_field")
