"""
Tests for netbox_librenms_plugin.utils module.

Phase 2 tests covering device type matching, site matching,
platform matching, and conversion helper functions.
"""

import json
from unittest.mock import MagicMock, patch

# =============================================================================
# TestDeviceTypeMatching - 5 tests
# =============================================================================


class TestDeviceTypeMatching:
    """Test device type matching logic."""

    @patch("netbox_librenms_plugin.models.DeviceTypeMapping", create=True)
    @patch("dcim.models.DeviceType")
    def test_match_device_type_exact_match_by_part_number(self, mock_device_type, mock_dtm):
        """Exact part_number string should match."""
        mock_dtm.DoesNotExist = Exception
        mock_dtm.objects.get.side_effect = Exception
        mock_dt = MagicMock(id=1, model="C9300-48P")
        mock_device_type.objects.get.return_value = mock_dt

        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("C9300-48P")

        assert result["matched"] is True
        assert result["device_type"] == mock_dt
        assert result["match_type"] == "exact"

    @patch("netbox_librenms_plugin.models.DeviceTypeMapping", create=True)
    @patch("dcim.models.DeviceType")
    def test_match_device_type_exact_match_by_model(self, mock_device_type, mock_dtm):
        """Exact model string should match when part_number fails."""
        mock_dtm.DoesNotExist = Exception
        mock_dtm.objects.get.side_effect = Exception
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

    @patch("netbox_librenms_plugin.models.DeviceTypeMapping", create=True)
    @patch("dcim.models.DeviceType")
    def test_match_device_type_not_found(self, mock_device_type, mock_dtm):
        """Returns not-found dict when no match found."""
        mock_dtm.DoesNotExist = Exception
        mock_dtm.objects.get.side_effect = Exception
        mock_device_type.DoesNotExist = Exception
        mock_device_type.objects.get.side_effect = mock_device_type.DoesNotExist

        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("NonexistentHardware")

        assert result["matched"] is False
        assert result["device_type"] is None
        assert result["match_type"] is None

    @patch("netbox_librenms_plugin.models.DeviceTypeMapping", create=True)
    def test_match_device_type_mapping_match(self, mock_dtm):
        """DeviceTypeMapping entry should be used before part_number/model fallback."""
        mock_dt = MagicMock(id=1, model="MX480")
        mock_mapping_obj = MagicMock(netbox_device_type=mock_dt)
        mock_dtm.DoesNotExist = Exception
        mock_dtm.objects.get.return_value = mock_mapping_obj

        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("Juniper MX480 Internet Backbone Router")

        assert result["matched"] is True
        assert result["device_type"] == mock_dt
        assert result["match_type"] == "mapping"

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

    @patch("netbox_librenms_plugin.models.DeviceTypeMapping", create=True)
    @patch("dcim.models.DeviceType")
    def test_match_device_type_ambiguous_part_number_returns_none(self, mock_device_type, mock_dtm):
        """MultipleObjectsReturned on part_number should return None, not pick .first()."""
        DoesNotExist = type("DoesNotExist", (Exception,), {})
        MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})
        mock_dtm.DoesNotExist = DoesNotExist
        mock_dtm.MultipleObjectsReturned = type("DTMMult", (Exception,), {})
        mock_dtm.objects.get.side_effect = DoesNotExist  # DTM not found
        mock_device_type.DoesNotExist = DoesNotExist
        mock_device_type.MultipleObjectsReturned = MultipleObjectsReturned
        mock_device_type.objects.get.side_effect = MultipleObjectsReturned

        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("DUPLICATE-PARTNUM")

        assert result is None

    @patch("netbox_librenms_plugin.models.DeviceTypeMapping", create=True)
    @patch("dcim.models.DeviceType")
    def test_match_device_type_ambiguous_model_returns_none(self, mock_device_type, mock_dtm):
        """MultipleObjectsReturned on model should return None, not pick .first()."""
        DoesNotExist = type("DoesNotExist", (Exception,), {})
        MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})
        mock_dtm.DoesNotExist = DoesNotExist
        mock_dtm.MultipleObjectsReturned = type("DTMMult", (Exception,), {})
        mock_dtm.objects.get.side_effect = DoesNotExist  # DTM not found
        mock_device_type.DoesNotExist = DoesNotExist
        mock_device_type.MultipleObjectsReturned = MultipleObjectsReturned
        # part_number raises DoesNotExist, model raises MultipleObjectsReturned
        mock_device_type.objects.get.side_effect = [
            DoesNotExist("not found"),
            MultipleObjectsReturned("ambiguous"),
        ]

        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("DUPLICATE-MODEL")

        assert result is None


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

    @patch("netbox_librenms_plugin.models.PlatformMapping")
    @patch("dcim.models.Platform")
    def test_find_platform_for_os_exact_match(self, mock_platform_model, mock_platform_mapping):
        """OS string matched to platform."""
        mock_platform = MagicMock(id=1, name="ios")
        mock_platform_model.objects.get.return_value = mock_platform
        mock_platform_mapping.DoesNotExist = Exception
        mock_platform_mapping.objects.get.side_effect = mock_platform_mapping.DoesNotExist

        from netbox_librenms_plugin.utils import find_matching_platform

        result = find_matching_platform("ios")

        assert result["found"] is True
        assert result["platform"] == mock_platform
        assert result["match_type"] == "exact"

    @patch("netbox_librenms_plugin.models.PlatformMapping")
    @patch("dcim.models.Platform")
    def test_find_platform_for_os_not_found(self, mock_platform_model, mock_platform_mapping):
        """Returns None when no match."""
        mock_platform_mapping.DoesNotExist = Exception
        mock_platform_mapping.objects.get.side_effect = mock_platform_mapping.DoesNotExist
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

    def test_get_librenms_sync_device_dict_preferred_over_legacy_bare_int(self):
        """
        In a partially migrated VC, a member with per-server dict format
        is preferred over a member with legacy bare-int format."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        # Member A: legacy bare-int librenms_id (not yet migrated)
        member_a = MagicMock()
        member_a.cf = {"librenms_id": 42}

        # Member B: migrated per-server dict format
        member_b = MagicMock()
        member_b.cf = {"librenms_id": {"default": 42}}

        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()
        # member_a listed first — the function should still prefer member_b
        mock_device.virtual_chassis.members.all.return_value = [member_a, member_b]

        result = get_librenms_sync_device(mock_device, server_key="default")

        assert result == member_b

    def test_get_librenms_sync_device_legacy_fallback_when_no_dict(self):
        """When no member has a per-server dict, fall back to legacy bare-int."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        member_a = MagicMock()
        member_a.cf = {"librenms_id": 42}
        member_b = MagicMock()
        member_b.cf = {}

        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.all.return_value = [member_b, member_a]

        result = get_librenms_sync_device(mock_device, server_key="default")

        assert result == member_a

    def test_get_librenms_sync_device_dict_for_different_server_falls_through(self):
        """Per-server dict with a different key does not match; legacy bare-int resolves instead."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        # Member A: legacy bare-int (universal fallback)
        member_a = MagicMock()
        member_a.cf = {"librenms_id": 42}

        # Member B: dict but only for "production", not "default"
        member_b = MagicMock()
        member_b.cf = {"librenms_id": {"production": 99}}

        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()
        mock_device.virtual_chassis.members.all.return_value = [member_a, member_b]

        result = get_librenms_sync_device(mock_device, server_key="default")

        assert result == member_a

    def test_get_librenms_sync_device_fallback_to_member_with_ip(self):
        """Priority 3: no dict member, master has no IP, another member has primary IP → that member."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        vc = MagicMock()
        master = MagicMock()
        master.vc_position = 1
        master.virtual_chassis = vc
        master.cf = {}
        master.primary_ip = None  # master has no IP

        member_with_ip = MagicMock()
        member_with_ip.vc_position = 2
        member_with_ip.cf = {}
        member_with_ip.primary_ip = MagicMock()  # this member has IP

        vc.master = None  # no designated master
        vc.members.all.return_value = [master, member_with_ip]

        result = get_librenms_sync_device(master, server_key="prod")

        assert result == member_with_ip

    def test_get_librenms_sync_device_fallback_lowest_vc_position(self):
        """Priority 4: no IPs anywhere → return member with lowest vc_position."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        vc = MagicMock()
        member_pos3 = MagicMock()
        member_pos3.vc_position = 3
        member_pos3.cf = {}
        member_pos3.primary_ip = None

        member_pos1 = MagicMock()
        member_pos1.vc_position = 1
        member_pos1.cf = {}
        member_pos1.primary_ip = None

        member_pos2 = MagicMock()
        member_pos2.vc_position = 2
        member_pos2.cf = {}
        member_pos2.primary_ip = None

        vc.master = None
        vc.members.all.return_value = [member_pos3, member_pos1, member_pos2]  # unordered

        member_pos2.virtual_chassis = vc

        result = get_librenms_sync_device(member_pos2, server_key="prod")

        assert result == member_pos1  # lowest vc_position wins

    def test_zero_id_is_not_a_valid_librenms_id(self):
        """LibreNMS uses MySQL auto-increment IDs starting at 1; device_id=0 cannot exist.
        A member whose resolved ID is 0 must be skipped so a real ID is preferred."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc
        vc.master = None

        member_zero = MagicMock()
        member_zero.primary_ip = None
        member_real = MagicMock()
        member_real.primary_ip = None

        def _id_side_effect(obj, server_key, **kwargs):
            if obj is member_zero:
                return 0
            if obj is member_real:
                return 5
            return None

        # member_zero comes first but has id=0; member_real has id=5 — real ID wins
        with patch("netbox_librenms_plugin.utils.get_librenms_device_id") as mock_get_id:
            mock_get_id.side_effect = _id_side_effect
            vc.members.all.return_value = [member_zero, member_real]
            result = get_librenms_sync_device(device, server_key="default")

        assert result is member_real


# =============================================================================
# TestSafeDisabled - tests for _safe_disabled in bulk_import.py and filters.py
# =============================================================================


class TestSafeDisabledBulkImport:
    """Tests for _safe_disabled in import_utils/bulk_import.py."""

    def _call(self, val):
        from netbox_librenms_plugin.import_utils.bulk_import import _safe_disabled

        return _safe_disabled({"disabled": val})

    def test_bool_true(self):
        assert self._call(True) == 1

    def test_bool_false(self):
        assert self._call(False) == 0

    def test_string_true_lowercase(self):
        assert self._call("true") == 1

    def test_string_yes(self):
        assert self._call("yes") == 1

    def test_string_on(self):
        assert self._call("on") == 1

    def test_string_false_lowercase(self):
        assert self._call("false") == 0

    def test_string_no(self):
        assert self._call("no") == 0

    def test_string_off(self):
        assert self._call("off") == 0

    def test_numeric_one(self):
        assert self._call(1) == 1

    def test_numeric_zero(self):
        assert self._call(0) == 0

    def test_none_defaults_to_zero(self):
        assert self._call(None) == 0

    def test_missing_key_defaults_to_zero(self):
        from netbox_librenms_plugin.import_utils.bulk_import import _safe_disabled

        assert _safe_disabled({}) == 0

    def test_string_true_uppercase(self):
        assert self._call("TRUE") == 1

    def test_non_zero_int_is_disabled(self):
        assert self._call(2) == 1

    def test_negative_int_is_disabled(self):
        assert self._call(-1) == 1


class TestSafeDisabledFilters:
    """Tests for _safe_disabled in import_utils/filters.py (same contract)."""

    def _call(self, val):
        from netbox_librenms_plugin.import_utils.filters import _safe_disabled

        return _safe_disabled({"disabled": val})

    def test_bool_true(self):
        assert self._call(True) == 1

    def test_bool_false(self):
        assert self._call(False) == 0

    def test_string_true(self):
        assert self._call("true") == 1

    def test_string_yes(self):
        assert self._call("yes") == 1

    def test_string_on(self):
        assert self._call("on") == 1

    def test_string_false(self):
        assert self._call("false") == 0

    def test_string_off(self):
        assert self._call("off") == 0

    def test_string_uppercase_true(self):
        assert self._call("TRUE") == 1

    def test_string_no(self):
        assert self._call("no") == 0

    def test_numeric_one(self):
        assert self._call(1) == 1

    def test_none_defaults_to_zero(self):
        assert self._call(None) == 0

    def test_non_zero_int_is_disabled(self):
        assert self._call(2) == 1

    def test_negative_int_is_disabled(self):
        assert self._call(-1) == 1

    def test_missing_key_defaults_to_zero(self):
        from netbox_librenms_plugin.import_utils.filters import _safe_disabled

        assert _safe_disabled({}) == 0


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
        mock_request.user.config.get.return_value = None

        result = get_interface_name_field(mock_request)

        assert result == "ifAlias"
        mock_plugin_config.assert_called_with("netbox_librenms_plugin", "interface_name_field")

    @patch("netbox_librenms_plugin.utils.get_plugin_config")
    def test_get_interface_name_field_from_user_pref(self, mock_plugin_config):
        """Falls back to user preference before plugin config."""
        from netbox_librenms_plugin.utils import get_interface_name_field

        mock_request = MagicMock()
        mock_request.GET = {}
        mock_request.POST = {}
        mock_request.user.config.get.return_value = "ifName"

        result = get_interface_name_field(mock_request)

        assert result == "ifName"
        mock_plugin_config.assert_not_called()

    @patch("netbox_librenms_plugin.utils.get_plugin_config")
    def test_get_interface_name_field_persists_to_user_pref(self, mock_plugin_config):
        """Explicit GET param should be persisted to user preferences."""
        from netbox_librenms_plugin.utils import get_interface_name_field

        mock_request = MagicMock()
        mock_request.GET = {"interface_name_field": "ifDescr"}
        mock_request.POST = {}

        result = get_interface_name_field(mock_request)

        assert result == "ifDescr"
        mock_request.user.config.set.assert_called_once_with(
            "plugins.netbox_librenms_plugin.interface_name_field", "ifDescr", commit=True
        )


# =============================================================================
# TestSaveUserPrefView - 6 tests
# =============================================================================


class TestSaveUserPrefView:
    """Test SaveUserPrefView endpoint for JS-driven preference persistence."""

    def _make_request(self, body, has_perm=True):
        """Create a mock POST request with JSON body."""
        request = MagicMock()
        request.body = json.dumps(body).encode()
        request.user.has_perm.return_value = has_perm
        request.user.config = MagicMock()
        request.method = "POST"
        return request

    def test_save_valid_boolean_pref(self):
        """Saving a valid boolean preference returns ok."""
        from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView

        view = SaveUserPrefView()
        request = self._make_request({"key": "use_sysname", "value": True})
        view.request = request

        response = view.post(request)

        assert response.status_code == 200
        data = json.loads(response.content)
        assert data["status"] == "ok"
        request.user.config.set.assert_called_once_with("plugins.netbox_librenms_plugin.use_sysname", True, commit=True)

    def test_save_string_pref(self):
        """Saving interface_name_field string value works."""
        from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView

        view = SaveUserPrefView()
        request = self._make_request({"key": "interface_name_field", "value": "ifDescr"})
        view.request = request

        response = view.post(request)

        assert response.status_code == 200
        request.user.config.set.assert_called_once_with(
            "plugins.netbox_librenms_plugin.interface_name_field", "ifDescr", commit=True
        )

    def test_reject_invalid_key(self):
        """Invalid preference key returns 400."""
        from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView

        view = SaveUserPrefView()
        request = self._make_request({"key": "malicious_key", "value": True})
        view.request = request

        response = view.post(request)

        assert response.status_code == 400
        data = json.loads(response.content)
        assert "Invalid preference key" in data["error"]
        request.user.config.set.assert_not_called()

    def test_reject_invalid_json(self):
        """Invalid JSON body returns 400."""
        from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView

        view = SaveUserPrefView()
        request = MagicMock()
        request.body = b"not valid json"
        view.request = request

        response = view.post(request)

        assert response.status_code == 400
        data = json.loads(response.content)
        assert "Invalid JSON" in data["error"]

    def test_save_false_value(self):
        """Saving False for a toggle works correctly."""
        from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView

        view = SaveUserPrefView()
        request = self._make_request({"key": "strip_domain", "value": False})
        view.request = request

        response = view.post(request)

        assert response.status_code == 200
        request.user.config.set.assert_called_once_with(
            "plugins.netbox_librenms_plugin.strip_domain", False, commit=True
        )

    def test_uses_permission_mixin(self):
        """SaveUserPrefView inherits from LibreNMSPermissionMixin."""
        from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        assert issubclass(SaveUserPrefView, LibreNMSPermissionMixin)
