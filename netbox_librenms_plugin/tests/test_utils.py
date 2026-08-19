"""
Tests for netbox_librenms_plugin.utils module.

Phase 2 tests covering device type matching, site matching,
platform matching, and conversion helper functions.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


def _two_member_vc(name, cf_first, cf_second):
    """Create a real VirtualChassis with two members (vc_position 1 then 2), seeding each member's ``librenms_id`` custom field with *cf_first* / *cf_second* (use ``_UNSET`` / skip by passing None to leave it empty)."""
    from dcim.models import VirtualChassis

    from netbox_librenms_plugin.tests.conftest import make_device

    vc = VirtualChassis.objects.create(name=name)
    first = make_device(f"{name}-m1")
    first.virtual_chassis = vc
    first.vc_position = 1
    if cf_first is not None:
        first.custom_field_data["librenms_id"] = cf_first
    first.save()
    second = make_device(f"{name}-m2")
    second.virtual_chassis = vc
    second.vc_position = 2
    if cf_second is not None:
        second.custom_field_data["librenms_id"] = cf_second
    second.save()
    return first, second


# =============================================================================
# TestDeviceTypeMatching - 5 tests
# =============================================================================


# match_librenms_hardware_to_device_type now runs the device_type NormalizationRule query
# (issue #90); enabling django_db lets that real (empty) query run while the lookups below
# stay mocked, so the matching assertions are unaffected.
@pytest.mark.django_db
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
        mock_dtm.objects.get.assert_called_once_with(librenms_hardware__iexact="Juniper MX480 Internet Backbone Router")

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
        # Exact name matched — PlatformMapping is never consulted
        mock_platform_mapping.objects.get.assert_not_called()
        mock_platform_model.objects.get.assert_called_once_with(name__iexact="ios")

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
        mock_platform_mapping.objects.get.assert_called_once_with(librenms_os__iexact="unknown_os")
        mock_platform_model.objects.get.assert_called_once_with(name__iexact="unknown_os")

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

    def test_normalize_librenms_port_id_accepts_positive_int_and_str(self):
        from netbox_librenms_plugin.utils import normalize_librenms_port_id

        assert normalize_librenms_port_id(42) == 42
        assert normalize_librenms_port_id("42") == 42

    def test_normalize_librenms_port_id_rejects_invalid_values(self):
        from netbox_librenms_plugin.utils import normalize_librenms_port_id

        assert normalize_librenms_port_id(None) is None
        assert normalize_librenms_port_id(True) is None
        assert normalize_librenms_port_id(False) is None
        assert normalize_librenms_port_id(0) is None
        assert normalize_librenms_port_id(-1) is None
        assert normalize_librenms_port_id("abc") is None
        assert normalize_librenms_port_id("2_0") is None
        assert normalize_librenms_port_id("２０") is None
        assert normalize_librenms_port_id("٢٠") is None
        assert normalize_librenms_port_id("1" * 5000) is None
        assert normalize_librenms_port_id(1.5) is None

    def test_oversized_digit_string_is_rejected_by_the_helper_not_the_interpreter(self):
        """The length cap must hold with CPython's int_max_str_digits limit disabled."""
        import sys

        from netbox_librenms_plugin.utils import normalize_librenms_port_id

        previous = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(0)
        try:
            assert normalize_librenms_port_id("1" * 5000) is None
            # 19 digits is the bigint width and stays acceptable.
            assert normalize_librenms_port_id("9" * 19) == int("9" * 19)
            assert normalize_librenms_port_id("9" * 20) is None
        finally:
            sys.set_int_max_str_digits(previous)

    def test_unambiguous_interface_name_index_rejects_duplicate_ids_and_names(self):
        from netbox_librenms_plugin.utils import get_interface_port_identity_sets

        ports = [
            {"port_id": 10, "ifDescr": "Ethernet"},
            {"port_id": 11, "ifDescr": "Ethernet"},
            {"port_id": 20, "ifDescr": "Parent"},
            {"port_id": 20, "ifDescr": "Duplicate ID"},
            {"port_id": 30, "ifDescr": "Unique"},
            {"port_id": 40, "ifDescr": "Unique", "_source": "oob"},
        ]

        assert get_interface_port_identity_sets(ports, "ifDescr") == ({10, 11, 30}, {30})

    def test_normalize_relationship_maps_normalizes_and_guards(self):
        from netbox_librenms_plugin.utils import normalize_relationship_maps

        # JSON-round-tripped string keys are normalized to int; both maps returned.
        lag, sub = normalize_relationship_maps({"lag_members": {"10": 100}, "sub_interfaces": {"5": 7}})
        assert lag == {10: 100}
        assert sub == {5: 7}

    def test_normalize_relationship_maps_drops_unresolvable_keys(self):
        from netbox_librenms_plugin.utils import normalize_relationship_maps

        relationships = {
            "lag_members": {"bad": 100, "": 101, None: 102, "10": 103},
            "sub_interfaces": {"0": 7, False: 8, "5": 9},
        }

        lag, sub = normalize_relationship_maps(relationships)

        assert lag == {10: 103}
        assert sub == {5: 9}

    def test_normalize_relationship_maps_normalizes_values_and_drops_invalid_edges(self):
        from netbox_librenms_plugin.utils import normalize_relationship_maps

        relationships = {
            "lag_members": {"10": "100", "11": [], "12": 0},
            "sub_interfaces": {"20": "21", "22": None, "23": False},
        }

        assert normalize_relationship_maps(relationships) == ({10: 100}, {20: 21})

    def test_normalize_relationship_maps_drops_conflicting_canonical_sources(self):
        from netbox_librenms_plugin.utils import normalize_relationship_maps

        first = {"lag_members": {"10": 20, "010": 30}, "sub_interfaces": {}}
        reversed_order = {"lag_members": {"010": 30, "10": 20}, "sub_interfaces": {}}

        assert normalize_relationship_maps(first) == ({}, {})
        assert normalize_relationship_maps(reversed_order) == ({}, {})

    def test_normalize_relationship_maps_coerces_corrupt_shapes_to_empty(self):
        from netbox_librenms_plugin.utils import normalize_relationship_maps

        # A non-dict relationships (corrupt / partial-write cache) must not raise.
        assert normalize_relationship_maps(["garbage"]) == ({}, {})
        assert normalize_relationship_maps(None) == ({}, {})
        # Present-but-non-dict nested maps collapse to {} instead of AttributeError on .items().
        assert normalize_relationship_maps({"lag_members": None, "sub_interfaces": [1, 2]}) == ({}, {})


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

    @pytest.mark.django_db
    def test_get_virtual_chassis_members_real_vc(self):
        """get_virtual_chassis_members returns every member Device for a VC device (from either member's perspective) and just [device] for a standalone one."""
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.utils import get_virtual_chassis_members

        solo = make_device("vcm-solo")
        assert get_virtual_chassis_members(solo) == [solo]

        vc = VirtualChassis.objects.create(name="vcm-vc")
        m1 = make_device("vcm-1")
        m1.virtual_chassis = vc
        m1.vc_position = 1
        m1.save()
        m2 = make_device("vcm-2")
        m2.virtual_chassis = vc
        m2.vc_position = 2
        m2.save()
        vc.master = m1
        vc.save()

        assert {d.pk for d in get_virtual_chassis_members(m1)} == {m1.pk, m2.pk}
        assert {d.pk for d in get_virtual_chassis_members(m2)} == {m1.pk, m2.pk}

    def test_get_virtual_chassis_members_returns_device_on_enumeration_error(self, caplog):
        """Non-enumerable / broken VC membership must fall back to [device] AND log a warning (not silently mask the error as 'device has no VC siblings', which would skip sibling-member matching)."""
        import logging
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.utils import get_virtual_chassis_members

        device = MagicMock()
        device.name = "sw1"
        device.virtual_chassis.members.all.side_effect = RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="netbox_librenms_plugin.utils"):
            assert get_virtual_chassis_members(device) == [device]
        assert any("virtual chassis members" in r.message for r in caplog.records)

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

    @pytest.mark.django_db
    def test_get_librenms_sync_device_with_librenms_id(self):
        """VC member with librenms_id is returned (real VC; the member without one iterated first)."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        without_id, with_id = _two_member_vc("sync-withid", None, {"default": 123})
        assert get_librenms_sync_device(without_id) == with_id

    @pytest.mark.django_db
    def test_get_librenms_sync_device_dict_preferred_over_legacy_bare_int(self):
        """In a partially migrated VC, a per-server dict member is preferred over a legacy bare-int."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        # member_a (legacy bare-int) iterated first — the function should still prefer member_b (dict).
        member_a, member_b = _two_member_vc("sync-dictpref", 42, {"default": 42})
        assert get_librenms_sync_device(member_a, server_key="default") == member_b

    @pytest.mark.django_db
    def test_get_librenms_sync_device_host_id_preferred_over_oob_only(self):
        """A member holding the real host id wins over an OOB-only member, even iterated first."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        member_a, member_b = _two_member_vc(
            "sync-hostpref",
            {"default": {"oob": {"id": 7, "type": "drac"}}},  # OOB-only, first
            {"default": {"id": 42}},  # real host id
        )
        assert get_librenms_sync_device(member_a, server_key="default") == member_b

    @pytest.mark.django_db
    def test_get_librenms_sync_device_oob_only_resolves_when_no_host_id(self):
        """When no member has a host id, an OOB-only mapping still resolves the sync device."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        member_a, member_b = _two_member_vc("sync-oobonly", None, {"default": {"oob": {"id": 7, "type": "drac"}}})
        assert get_librenms_sync_device(member_a, server_key="default") == member_b

    @pytest.mark.django_db
    def test_get_librenms_sync_device_legacy_fallback_when_no_dict(self):
        """When no member has a per-server dict, fall back to legacy bare-int."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        # member_b (no id) iterated first, member_a (legacy bare-int) second → member_a wins.
        member_b, member_a = _two_member_vc("sync-legacy", None, 42)
        assert get_librenms_sync_device(member_b, server_key="default") == member_a

    @pytest.mark.django_db
    def test_get_librenms_sync_device_dict_for_different_server_falls_through(self):
        """Per-server dict with a different key does not match; legacy bare-int resolves instead."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        # member_a: legacy bare-int (universal); member_b: dict only for "production".
        member_a, member_b = _two_member_vc("sync-diffserver", 42, {"production": 99})
        assert get_librenms_sync_device(member_a, server_key="default") == member_a

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

        vc.master = master  # designated master exists but has no primary_ip
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

    def test_get_module_template_interface_names_rewrites_for_vc_member(self):
        from netbox_librenms_plugin.utils import get_module_template_interface_names

        device = MagicMock()
        device.vc_position = 3
        device.virtual_chassis_id = 11
        device.virtual_chassis = MagicMock()
        device.virtual_chassis.members.values_list.return_value = [1, 2, 3]

        module = MagicMock()
        template = MagicMock()
        instantiated = MagicMock()
        instantiated.name = "TenGigabitEthernet1/1/1"
        template.instantiate.return_value = instantiated
        module.module_type.interfacetemplates.all.return_value = [template]

        result = get_module_template_interface_names(device, module)

        assert result == ["TenGigabitEthernet3/1/1"]

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

    @pytest.mark.parametrize("disabled_max", [0, None])
    @patch("netbox_librenms_plugin.utils.get_config")
    @patch("netbox_librenms_plugin.utils.netbox_get_paginate_count")
    def test_get_table_paginate_count_no_clamp_when_max_disabled(self, mock_netbox_paginate, mock_config, disabled_max):
        """MAX_PAGE_SIZE 0/None disables the NetBox ceiling; per_page must pass through unclamped."""
        # min(per_page, 0) would silently return 0 rows per page; min(per_page, None) TypeErrors.
        from netbox_librenms_plugin.utils import get_table_paginate_count

        mock_config.return_value.MAX_PAGE_SIZE = disabled_max
        mock_request = MagicMock()
        mock_request.GET = {"table1_per_page": "500"}

        assert get_table_paginate_count(mock_request, "table1_") == 500

    @patch("netbox_librenms_plugin.utils.get_config")
    @patch("netbox_librenms_plugin.utils.netbox_get_paginate_count")
    def test_get_table_paginate_count_default(self, mock_netbox_paginate, mock_config):
        """Default pagination used when no override."""
        from netbox_librenms_plugin.utils import get_table_paginate_count

        mock_netbox_paginate.return_value = 25
        mock_request = MagicMock()
        mock_request.GET = {}
        mock_request.POST = {}

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

        mock_plugin_config.return_value = "ifDescr"
        mock_request = MagicMock()
        mock_request.GET = {}
        mock_request.POST = {}
        mock_request.user.config.get.return_value = None

        result = get_interface_name_field(mock_request)

        assert result == "ifDescr"
        mock_plugin_config.assert_called_with("netbox_librenms_plugin", "interface_name_field")

    @patch("netbox_librenms_plugin.utils.get_plugin_config")
    def test_unsupported_config_value_falls_back_to_the_default(self, mock_plugin_config):
        """A configured field the readers reject must not reach a cached snapshot."""
        from netbox_librenms_plugin.constants import DEFAULT_INTERFACE_NAME_FIELD
        from netbox_librenms_plugin.utils import get_interface_name_field

        mock_plugin_config.return_value = "ifAlias"
        mock_request = MagicMock()
        mock_request.GET = {}
        mock_request.POST = {}
        mock_request.user.config.get.return_value = None

        result = get_interface_name_field(mock_request)

        assert result == DEFAULT_INTERFACE_NAME_FIELD

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
    def test_get_interface_name_field_does_not_persist_the_param(self, mock_plugin_config):
        """The read path honours the parameter without writing it.

        get_context_data() calls this on every GET render, so persisting here made a read mutate
        stored user state. The selector posts to the save_user_pref endpoint instead.
        """
        from netbox_librenms_plugin.utils import get_interface_name_field

        mock_request = MagicMock()
        mock_request.GET = {"interface_name_field": "ifDescr"}
        mock_request.POST = {}

        result = get_interface_name_field(mock_request)

        assert result == "ifDescr"
        mock_request.user.config.set.assert_not_called()

    @pytest.mark.django_db
    def test_persisting_the_preference_does_not_leak_to_other_users(self):
        """One user's choice must stay that user's.

        NetBox creates each UserConfig with the ``DEFAULT_USER_PREFERENCES`` dict itself (not a
        copy) and ``UserConfig.set()`` writes in place, so a naive write turns this choice into
        the process-wide default that every later-created user inherits.
        """
        from copy import deepcopy

        from django.contrib.auth import get_user_model
        from django.test import RequestFactory
        from netbox.config import get_config

        from netbox_librenms_plugin.utils import save_interface_name_preference

        user_model = get_user_model()
        pref_path = "plugins.netbox_librenms_plugin.interface_name_field"
        baseline = user_model.objects.create_user(username="pref-baseline")
        baseline_value = baseline.config.get(pref_path)
        chooser = user_model.objects.create_user(username="pref-chooser")
        defaults_before = deepcopy(get_config().DEFAULT_USER_PREFERENCES)
        # The writer, not the reader: get_interface_name_field no longer persists.
        request = RequestFactory().get("/", {"interface_name_field": "ifDescr"})
        request.user = chooser

        assert save_interface_name_preference(request, "ifDescr") is True

        stored = user_model.objects.get(pk=chooser.pk)
        assert stored.config.get(pref_path) == "ifDescr"
        assert get_config().DEFAULT_USER_PREFERENCES == defaults_before
        later = user_model.objects.create_user(username="pref-later")
        assert later.config.get(pref_path) == baseline_value


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


class TestDetectVCNormalizationNoop:
    """detect_vc_normalization_noop flags only the no-vendor-support case."""

    def _make_device(self, vc_position=3, vc_id=11, member_positions=(1, 2, 3, 4)):
        device = MagicMock()
        device.vc_position = vc_position
        device.virtual_chassis_id = vc_id
        if vc_id is None:
            device.virtual_chassis = None
        else:
            device.virtual_chassis = MagicMock()
            device.virtual_chassis.members.values_list.return_value = list(member_positions)
        return device

    def _make_module(self, instantiated_names, raw_names=None):
        module = MagicMock()
        templates = []
        for idx, name in enumerate(instantiated_names):
            tmpl = MagicMock()
            raw = (raw_names or [None] * len(instantiated_names))[idx]
            tmpl.name = raw or name
            instance = MagicMock()
            instance.name = name
            tmpl.instantiate.return_value = instance
            templates.append(tmpl)
        module.module_type.interfacetemplates.all.return_value = templates
        module.module_type.manufacturer.slug = "vendor"
        module.module_type.model = "MODEL-X"
        module.module_bay.name = "Bay 0"
        return module

    def test_returns_none_when_device_not_in_vc(self):
        from netbox_librenms_plugin.utils import detect_vc_normalization_noop

        device = self._make_device(vc_position=None, vc_id=None)
        module = self._make_module(["2/x1/1/c9"])
        assert detect_vc_normalization_noop(device, module) is None

    def test_returns_none_when_a_name_matches_regex(self):
        """Cisco-style name matches the rewrite regex → not a vendor-support issue."""
        from netbox_librenms_plugin.utils import detect_vc_normalization_noop

        device = self._make_device()
        device.device_type.model = "C9300-48T"
        module = self._make_module(["TenGigabitEthernet1/1/1"])
        assert detect_vc_normalization_noop(device, module) is None

    def test_returns_diagnostic_when_no_names_match_regex(self):
        """Nokia-style name doesn't match the rewrite regex → flag for reporting."""
        from netbox_librenms_plugin.utils import detect_vc_normalization_noop

        device = self._make_device(vc_position=3, member_positions=(1, 2, 3, 4))
        device.device_type.model = "7250-IXR"
        module = self._make_module(["2/x1/1/c9"], raw_names=["{module}"])

        diag = detect_vc_normalization_noop(device, module)
        assert diag is not None
        assert diag["vc_position"] == 3
        assert diag["vc_member_positions"] == [1, 2, 3, 4]
        assert diag["template_pairs"] == [("{module}", "2/x1/1/c9")]
        assert diag["device_type_model"] == "7250-IXR"
        assert diag["module_type_model"] == "MODEL-X"
        assert diag["manufacturer_slug"] == "vendor"
        assert diag["module_bay_name"] == "Bay 0"
        assert "regex" in diag

    def test_returns_none_when_no_templates(self):
        from netbox_librenms_plugin.utils import detect_vc_normalization_noop

        device = self._make_device()
        module = self._make_module([])
        assert detect_vc_normalization_noop(device, module) is None

    def test_template_instantiate_exception_is_skipped(self):
        """Templates that raise on instantiate are skipped; no false positive if all skip."""
        from netbox_librenms_plugin.utils import detect_vc_normalization_noop

        device = self._make_device()
        module = MagicMock()
        bad_template = MagicMock()
        bad_template.instantiate.side_effect = ValueError("boom")
        module.module_type.interfacetemplates.all.return_value = [bad_template]
        assert detect_vc_normalization_noop(device, module) is None


class TestBuildVCNormalizationReport:
    """Markdown formatter produces a stable, copyable block with optional-strip suffixes."""

    def test_contains_all_diagnostic_fields(self):
        from netbox_librenms_plugin.utils import build_vc_normalization_report

        diagnostic = {
            "manufacturer_slug": "nokia",
            "device_type_model": "7250-IXR",
            "module_type_model": "QSFP-DD",
            "module_bay_name": "Bay c9",
            "vc_position": 3,
            "vc_member_positions": [1, 2, 3, 4],
            "template_pairs": [("{module}", "2/x1/1/c9")],
            "regex": "^[A-Za-z][A-Za-z0-9]*\\d+[/:].+$",
        }
        out = build_vc_normalization_report(diagnostic)
        assert "**VC interface normalization — no match**" in out
        assert "`nokia`" in out
        assert "`7250-IXR`" in out
        assert "`QSFP-DD`" in out
        assert "`Bay c9`" in out
        assert "VC position (target): 3" in out
        assert "[1, 2, 3, 4]" in out
        assert "`{module}` → `2/x1/1/c9`" in out
        assert "Plugin:" in out

    def test_catalog_lines_get_optional_strip_suffix(self):
        from netbox_librenms_plugin.utils import build_vc_normalization_report

        out = build_vc_normalization_report(
            {
                "manufacturer_slug": "v",
                "device_type_model": "d",
                "module_type_model": "m",
                "module_bay_name": "b",
                "vc_position": 1,
                "vc_member_positions": [1],
                "template_pairs": [("a", "b")],
                "regex": "x",
            }
        )
        catalog_lines = [
            line
            for line in out.splitlines()
            if line.startswith("- Manufacturer:")
            or line.startswith("- Device type:")
            or line.startswith("- Module type:")
            or line.startswith("- Module bay:")
        ]
        assert len(catalog_lines) == 4
        assert all("(optional, you can remove this line)" in line for line in catalog_lines)
        # VC + template lines must NOT carry the suffix.
        for line in out.splitlines():
            if line.startswith("- VC ") or line.startswith("  - `"):
                assert "(optional" not in line

    def test_missing_catalog_value_renders_unknown(self):
        from netbox_librenms_plugin.utils import build_vc_normalization_report

        out = build_vc_normalization_report(
            {
                "manufacturer_slug": None,
                "device_type_model": "",
                "module_type_model": "m",
                "module_bay_name": "b",
                "vc_position": 2,
                "vc_member_positions": [1, 2],
                "template_pairs": [],
                "regex": "x",
            }
        )
        assert "Manufacturer: _(unknown)_" in out
        assert "Device type: _(unknown)_" in out
        # Template pairs section falls back to a no-templates note.
        assert "_(no templates)_" in out


# =============================================================================
# DeviceTypeMapping re-normalization data migration (0011)
# =============================================================================
@pytest.mark.django_db
class TestRenormalizeDeviceTypeMappingsMigration:
    """The 0011 data migration re-keys pre-existing DeviceTypeMapping rows through the
    device_type NormalizationRule scope so they keep matching the normalized lookup.

    Exercises the REAL objects end-to-end: a real NormalizationRule, a real
    DeviceTypeMapping stored under its un-normalized key, the real
    match_librenms_hardware_to_device_type lookup, and the real migration function.
    """

    @staticmethod
    def _device_type(model="Renorm-DT"):
        from dcim.models import DeviceType, Manufacturer

        mfr, _ = Manufacturer.objects.get_or_create(name="Renorm-Mfr", slug="renorm-mfr")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=model, slug="renorm-dt")
        return dt

    @staticmethod
    def _run_migration():
        import importlib

        from django.apps import apps

        module = importlib.import_module("netbox_librenms_plugin.migrations.0011_renormalize_device_type_mappings")
        module.renormalize_device_type_mappings(apps, None)

    def test_existing_mapping_is_rekeyed_so_lookup_matches(self):
        """A pre-normalization mapping only matches the normalized lookup after the migration re-keys it."""
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        dt = self._device_type()
        # device_type rule strips the 'acme-' vendor prefix (capture-group replacement, since
        # the replacement field cannot be blank).
        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_DEVICE_TYPE,
            match_pattern=r"^acme-(.+)$",
            replacement=r"\1",
            priority=10,
        )
        # Pre-normalization row: stored under the raw (only lowercased) hardware key.
        DeviceTypeMapping.objects.create(librenms_hardware="acme-router-x", netbox_device_type=dt)

        # BEFORE the migration the normalized lookup ('router-x') misses the raw row, but the
        # lookup's raw-key fallback still finds it — the migration's job is canonicalization,
        # not rescue (a lookup for a DIFFERENT raw spelling, e.g. 'ACME-Router-X ', normalizes
        # to 'router-x' and only matches once the row is re-keyed).
        before = match_librenms_hardware_to_device_type("acme-router-x")
        assert before["matched"] is True
        assert not DeviceTypeMapping.objects.filter(librenms_hardware="router-x").exists()

        self._run_migration()

        # The row is re-keyed to the normalized value...
        assert DeviceTypeMapping.objects.filter(librenms_hardware="router-x").exists()
        # ...so the same lookup now resolves to the mapped device type.
        after = match_librenms_hardware_to_device_type("acme-router-x")
        assert after["matched"] is True
        assert after["match_type"] == "mapping"
        assert after["device_type"] == dt

    def test_noop_when_no_device_type_rules(self):
        """With no device_type rules the migration leaves the stored key untouched."""
        from netbox_librenms_plugin.models import DeviceTypeMapping

        dt = self._device_type(model="Renorm-DT2")
        DeviceTypeMapping.objects.create(librenms_hardware="plain-hw-9000", netbox_device_type=dt)

        self._run_migration()

        assert DeviceTypeMapping.objects.filter(librenms_hardware="plain-hw-9000").exists()

    def test_collision_is_skipped_not_crashed(self):
        """Two raw mappings normalizing to the same key are skipped, not crashed on the unique constraint."""
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        dt = self._device_type(model="Renorm-DT3")
        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_DEVICE_TYPE,
            match_pattern=r"^v\d+-(.+)$",
            replacement=r"\1",
            priority=10,
        )
        # Both collapse to 'switch-1' after stripping the version prefix; plus an already-clean row.
        DeviceTypeMapping.objects.create(librenms_hardware="switch-1", netbox_device_type=dt)
        DeviceTypeMapping.objects.create(librenms_hardware="v2-switch-1", netbox_device_type=dt)

        # Must not raise (IntegrityError) on the unique librenms_hardware constraint.
        self._run_migration()

        # The clean row survives; the colliding raw row is left as-is (not silently dropped).
        assert DeviceTypeMapping.objects.filter(librenms_hardware="switch-1").exists()
        assert DeviceTypeMapping.objects.filter(librenms_hardware="v2-switch-1").exists()

    def test_row_save_failure_is_skipped_not_aborted(self, monkeypatch):
        """An unexpected DB error on one row's save must skip that row and continue, not abort the upgrade.

        The pre-check only guards the known uniqueness clash; a different write failure (e.g. an
        over-length value) would otherwise propagate out of the migration and block the whole
        upgrade. The bad row is left at its original key and the rest are still re-keyed.
        """
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        dt = self._device_type(model="Renorm-DT5")
        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_DEVICE_TYPE,
            match_pattern=r"^x-(.+)$",
            replacement=r"\1",
            priority=10,
        )
        DeviceTypeMapping.objects.create(librenms_hardware="x-boom", netbox_device_type=dt)
        DeviceTypeMapping.objects.create(librenms_hardware="x-good", netbox_device_type=dt)

        real_save = DeviceTypeMapping.save

        def flaky_save(inst, *args, **kwargs):
            # The migration mutates librenms_hardware to the normalized value before save, so the
            # boom row reaches save() keyed "boom". Fail only that write; let every other save run.
            if inst.librenms_hardware == "boom":
                raise RuntimeError("simulated write failure (e.g. value too long)")
            return real_save(inst, *args, **kwargs)

        monkeypatch.setattr(DeviceTypeMapping, "save", flaky_save)

        # Must NOT propagate the boom row's save error out of the migration.
        self._run_migration()

        # The failed row is left at its original key (skipped); the good row is still re-keyed.
        assert DeviceTypeMapping.objects.filter(librenms_hardware="x-boom").exists()
        assert DeviceTypeMapping.objects.filter(librenms_hardware="good").exists()

    def test_rule_queries_are_constant_regardless_of_row_count(self):
        """The migration preloads the device_type rule chain once, so it issues a constant number of NormalizationRule queries instead of one per mapping row (N+1)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        dt = self._device_type(model="Renorm-DT-Perf")
        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_DEVICE_TYPE,
            match_pattern=r"^raw-(.+)$",
            replacement=r"\1",
            priority=10,
        )
        # Several rows that all get re-keyed → each would trigger its own rule fetch without the
        # single preload. Distinct normalized targets so none collide on the unique constraint.
        for suffix in ("a", "b", "c", "d"):
            DeviceTypeMapping.objects.create(librenms_hardware=f"raw-{suffix}", netbox_device_type=dt)

        with CaptureQueriesContext(connection) as ctx:
            self._run_migration()

        rule_queries = [q for q in ctx.captured_queries if "normalizationrule" in q["sql"].lower()]
        # preload_normalization_rules(scope="device_type") issues exactly one NormalizationRule
        # SELECT (unscoped, manufacturer__isnull=True); the per-row calls then read the preloaded
        # dict with zero further queries. Before the fix this was one query PER ROW (== 4 here),
        # scaling with install size. Assert it does not scale with the 4 rows.
        assert len(rule_queries) <= 2, (
            f"expected a constant number of NormalizationRule queries from the single preload, got "
            f"{len(rule_queries)} for 4 mapping rows — the migration is re-querying rules per row (N+1)"
        )
        # Sanity: behaviour is unchanged — every row was still re-keyed.
        for suffix in ("a", "b", "c", "d"):
            assert DeviceTypeMapping.objects.filter(librenms_hardware=suffix).exists()

    def test_db_error_on_one_row_does_not_poison_the_rest(self, monkeypatch):
        """A real DB error on one row's save must be confined to that row's savepoint so the rest still migrate.

        A plain try/except cannot recover from a database error: on PostgreSQL it aborts the whole
        transaction, so the NEXT row's query raises "current transaction is aborted". Only a per-row
        transaction.atomic() savepoint lets the migration skip the bad row and continue. This drives
        a genuine DB error (SELECT 1/0), not a Python raise, so it actually exercises the savepoint.
        """
        from django.db import connection

        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        dt = self._device_type(model="Renorm-DT6")
        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_DEVICE_TYPE,
            match_pattern=r"^y-(.+)$",
            replacement=r"\1",
            priority=10,
        )
        # Iterated in librenms_hardware order, so 'y-boom' (→ 'boom') is processed before 'y-good'.
        DeviceTypeMapping.objects.create(librenms_hardware="y-boom", netbox_device_type=dt)
        DeviceTypeMapping.objects.create(librenms_hardware="y-good", netbox_device_type=dt)

        real_save = DeviceTypeMapping.save

        def poisoning_save(inst, *args, **kwargs):
            if inst.librenms_hardware == "boom":
                # A genuine DB error poisons the transaction the way a real IntegrityError/DataError
                # mid-migration would — recoverable only by rolling back this row's savepoint.
                with connection.cursor() as cur:
                    cur.execute("SELECT 1 / 0")
            return real_save(inst, *args, **kwargs)

        monkeypatch.setattr(DeviceTypeMapping, "save", poisoning_save)

        # Without the savepoint, the good row's query would raise "current transaction is aborted".
        self._run_migration()

        # The poisoned row is left at its original key (savepoint rolled back); the good row re-keyed.
        assert DeviceTypeMapping.objects.filter(librenms_hardware="y-boom").exists()
        assert DeviceTypeMapping.objects.filter(librenms_hardware="good").exists()

    def test_db_error_in_normalization_does_not_poison_the_rest(self, monkeypatch):
        """A DB error inside apply_normalization_rules must be confined to that row's savepoint too.

        The rule queries (and the clash .exists()) run inside the migration's outer atomic
        transaction just like the save: an unguarded DB failure there aborts the transaction on
        PostgreSQL, so the NEXT row's query raises "current transaction is aborted" and the
        upgrade crashes partway — the exact failure the per-save savepoint was added to prevent.
        This drives a genuine DB error (SELECT 1/0) through the normalization call.
        """
        from django.db import connection

        from netbox_librenms_plugin import utils as plugin_utils
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        dt = self._device_type(model="Renorm-DT7")
        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_DEVICE_TYPE,
            match_pattern=r"^z-(.+)$",
            replacement=r"\1",
            priority=10,
        )
        # Iterated in librenms_hardware order, so 'z-boom' is processed before 'z-good'.
        DeviceTypeMapping.objects.create(librenms_hardware="z-boom", netbox_device_type=dt)
        DeviceTypeMapping.objects.create(librenms_hardware="z-good", netbox_device_type=dt)

        real_apply = plugin_utils.apply_normalization_rules

        def poisoned_apply(value, scope, *args, **kwargs):
            if value == "z-boom":
                with connection.cursor() as cur:
                    cur.execute("SELECT 1 / 0")
            return real_apply(value, scope, *args, **kwargs)

        # The migration imports the symbol inside the function body, so patching the module
        # attribute is exactly what its runtime import resolves.
        monkeypatch.setattr(plugin_utils, "apply_normalization_rules", poisoned_apply)

        # Without a per-row savepoint around the normalization, the good row's clash check
        # would raise "current transaction is aborted".
        self._run_migration()

        # The poisoned row is left untouched; the good row is still re-keyed.
        assert DeviceTypeMapping.objects.filter(librenms_hardware="z-boom").exists()
        assert DeviceTypeMapping.objects.filter(librenms_hardware="good").exists()

    def test_preload_failure_leaves_all_rows_untouched(self, monkeypatch):
        """A DB error while preloading the rule chain must leave EVERY row untouched, not abort the upgrade.

        The single preload (the N+1 fix) runs once before the per-row loop and issues its own DB
        query. A failure there (connection/query error mid-upgrade) would otherwise propagate before
        any per-row savepoint runs — and, being a DB error, would poison the migration's outer atomic
        transaction, so even catching the Python exception could not leave a clean state. Its own
        savepoint must contain the failure and the migration must bail out leaving all rows at their
        original keys. This drives a genuine DB error (SELECT 1/0) through the preload, not a Python
        raise, so it actually exercises the savepoint.
        """
        from django.db import connection

        from netbox_librenms_plugin import utils as plugin_utils
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        dt = self._device_type(model="Renorm-DT8")
        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_DEVICE_TYPE,
            match_pattern=r"^w-(.+)$",
            replacement=r"\1",
            priority=10,
        )
        DeviceTypeMapping.objects.create(librenms_hardware="w-alpha", netbox_device_type=dt)
        DeviceTypeMapping.objects.create(librenms_hardware="w-beta", netbox_device_type=dt)

        def poisoned_preload(*args, **kwargs):
            # A genuine DB failure in the preload query, the way a real connection/query error
            # during upgrade would behave — recoverable only by rolling back its savepoint.
            with connection.cursor() as cur:
                cur.execute("SELECT 1 / 0")

        # The migration imports the symbol inside the function body, so patching the module
        # attribute is exactly what its runtime import resolves.
        monkeypatch.setattr(plugin_utils, "preload_normalization_rules", poisoned_preload)

        # Must NOT propagate the preload error out of the migration...
        self._run_migration()

        # ...and every row must be left at its original (un-normalized) key — nothing re-keyed.
        assert DeviceTypeMapping.objects.filter(librenms_hardware="w-alpha").exists()
        assert DeviceTypeMapping.objects.filter(librenms_hardware="w-beta").exists()
        assert not DeviceTypeMapping.objects.filter(librenms_hardware="alpha").exists()
        assert not DeviceTypeMapping.objects.filter(librenms_hardware="beta").exists()


# =============================================================================
# Device serial canonicalization data migration (0012)
# =============================================================================
@pytest.mark.django_db
class TestNormalizeDeviceSerialsMigration:
    """The 0012 migration canonicalizes legacy Device serials and indexes exact lookups."""

    @staticmethod
    def _run_migration():
        import importlib
        from types import SimpleNamespace

        from django.apps import apps
        from django.db import connection

        module = importlib.import_module("netbox_librenms_plugin.migrations.0012_normalize_device_serials")
        module.normalize_device_serials(apps, SimpleNamespace(connection=connection))

    def test_all_surrounding_whitespace_is_trimmed_and_blank_serials_are_canonicalized(self):
        """Python strip semantics remove tabs/newlines/Unicode whitespace, including whitespace-only values."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device

        padded = make_device("serial-migration-padded", serial="\t SN-MIG-1 \n")
        blank = make_device("serial-migration-blank", serial="\u2003\t\n")
        unchanged = make_device("serial-migration-clean", serial="SN-MIG-2")

        self._run_migration()

        assert Device.objects.get(pk=padded.pk).serial == "SN-MIG-1"
        assert Device.objects.get(pk=blank.pk).serial == ""
        assert Device.objects.get(pk=unchanged.pk).serial == "SN-MIG-2"

    def test_serial_rewrite_is_not_wrapped_in_one_migration_transaction(self):
        """Each idempotent batch commits independently so a large Device table is not locked for the full rewrite."""
        import importlib

        module = importlib.import_module("netbox_librenms_plugin.migrations.0012_normalize_device_serials")

        assert module.Migration.atomic is False
        assert module.Migration.operations[0].atomic is False
        assert module.Migration.operations[1].atomic is False

    def test_plain_serial_index_exists(self):
        """The migration adds the ordinary B-tree index used by exact serial equality lookups."""
        from django.db import connection

        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, "dcim_device")

        index = constraints["nblp_dcim_device_serial_idx"]
        assert index["index"] is True
        assert index["columns"] == ["serial"]

    def test_preexisting_valid_serial_index_is_reused(self):
        """Retrying 0012 accepts an already-valid index without rebuilding it."""
        import importlib

        from django.apps import apps
        from django.db import connection

        module = importlib.import_module("netbox_librenms_plugin.migrations.0012_normalize_device_serials")

        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)::oid", ["nblp_dcim_device_serial_idx"])
            original_oid = cursor.fetchone()[0]
        assert original_oid is not None, "0012 must have created nblp_dcim_device_serial_idx"

        with connection.schema_editor(atomic=False) as schema_editor:
            module.ensure_device_serial_index(apps, schema_editor)

        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)::oid", ["nblp_dcim_device_serial_idx"])
            assert cursor.fetchone()[0] == original_oid


class TestCacheRemainingTtl:
    """cache_remaining_ttl reads .ttl on django-redis and degrades to None on backends without it."""

    def test_returns_none_when_backend_has_no_ttl(self):
        """A backend without .ttl (e.g. LocMemCache) yields None instead of AttributeError."""
        from django.core.cache.backends.locmem import LocMemCache

        from netbox_librenms_plugin.utils import cache_remaining_ttl

        backend = LocMemCache("cr116-ttl", {})
        backend.set("k", "v")
        # Precondition: this backend lacks the django-redis .ttl the bare call sites assumed.
        assert not hasattr(backend, "ttl")
        assert cache_remaining_ttl(backend, "k") is None

    def test_delegates_to_ttl_when_present(self):
        """When the backend exposes .ttl (django-redis), the helper returns its value for the key."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.utils import cache_remaining_ttl

        seen = {}

        def _ttl(key):
            seen["key"] = key
            return 123

        backend = SimpleNamespace(ttl=_ttl)
        assert cache_remaining_ttl(backend, "librenms_ports_device_1_default") == 123
        assert seen["key"] == "librenms_ports_device_1_default"

    def test_returns_none_when_ttl_call_raises(self):
        """A present-but-raising .ttl (transient Redis error) degrades to None, not a 500 up the render."""
        from types import SimpleNamespace

        from netbox_librenms_plugin.utils import cache_remaining_ttl

        def _boom(key):
            raise ConnectionError("redis unreachable")

        backend = SimpleNamespace(ttl=_boom)
        # Precondition: the backend DOES expose .ttl, so the absence-guard doesn't cover this.
        assert hasattr(backend, "ttl")
        assert cache_remaining_ttl(backend, "librenms_ports_device_1_default") is None


class TestPredictModuleInterfaceRenameSignalGuard:
    """predict_module_interface_rename must not 500 on a receiver that returns a non-list."""

    @staticmethod
    def _call_with_receiver(return_value):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import predict_module_interface_rename

        class _Dev:
            pass

        class _Mod:
            pass

        @receiver(predict_module_interface_names)
        def _bad(sender, device, module, names, **kwargs):  # noqa: ARG001
            return return_value

        try:
            return predict_module_interface_rename(_Dev(), _Mod(), ["Gi0/0", "Gi0/1"])
        finally:
            predict_module_interface_names.disconnect(_bad)

    def test_non_iterable_return_is_ignored(self):
        """A receiver returning a scalar int is dropped, keeping the caller's names (was a TypeError-500)."""
        assert self._call_with_receiver(1) == ["Gi0/0", "Gi0/1"]

    def test_scalar_string_return_is_ignored(self):
        """A bare string is iterable but must not be exploded into characters and mispaired."""
        assert self._call_with_receiver("Gi0/0") == ["Gi0/0", "Gi0/1"]


@pytest.mark.django_db
class TestSetDeviceIpFkFamily:
    """set_device_ip_fk() must enforce the IP family that NetBox's Device.clean() requires for primary_ip4/primary_ip6, since the helper persists via save(update_fields=...) which bypasses full_clean()."""

    def test_primary_ip4_rejects_ipv6_address(self):
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("fam-v4dev")
        v6 = ip_on(device, "2001:db8::1/64", "eth0")
        with pytest.raises(ValueError, match="non-IPv4"):
            set_device_ip_fk(device, "primary_ip4", v6)
        device.refresh_from_db()
        assert device.primary_ip4_id is None  # nothing persisted

    def test_primary_ip6_rejects_ipv4_address(self):
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("fam-v6dev")
        v4 = ip_on(device, "10.0.0.1/24", "eth0")
        with pytest.raises(ValueError, match="non-IPv6"):
            set_device_ip_fk(device, "primary_ip6", v4)
        device.refresh_from_db()
        assert device.primary_ip6_id is None

    def test_matching_families_are_accepted(self):
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("fam-okdev")
        v4 = ip_on(device, "10.0.0.2/24", "eth0")
        v6 = ip_on(device, "2001:db8::2/64", "eth1")
        set_device_ip_fk(device, "primary_ip4", v4)
        set_device_ip_fk(device, "primary_ip6", v6)
        device.refresh_from_db()
        assert device.primary_ip4_id == v4.pk
        assert device.primary_ip6_id == v6.pk

    def test_oob_ip_is_family_agnostic(self):
        # oob_ip has no family restriction in NetBox; an IPv6 oob_ip must be accepted.
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("fam-oobdev")
        v6 = ip_on(device, "2001:db8::3/64", "eth0")
        set_device_ip_fk(device, "oob_ip", v6)
        device.refresh_from_db()
        assert device.oob_ip_id == v6.pk

    def test_families_accepted_with_netbox44_family_property(self):
        """The guard must not read IPAddress.family: on NetBox 4.4 the property raises on an in-memory str address and getattr's default turns that into a bogus refusal (forced)."""
        from unittest.mock import patch

        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.conftest import ip_on, make_device
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("fam-nb44dev")
        v4 = ip_on(device, "10.0.0.4/24", "eth0")
        v4.address = "10.0.0.4/24"  # in-memory str, as after IPAddress.objects.create()
        # NetBox 4.4's family property verbatim — 4.5+ added a str-tolerant branch.
        netbox44_family = property(lambda self: self.address.version if self.address else None)
        with patch.object(IPAddress, "family", netbox44_family):
            set_device_ip_fk(device, "primary_ip4", v4)
        device.refresh_from_db()
        assert device.primary_ip4_id == v4.pk


class TestIpFamily:
    """ip_family(): family read that works on NetBox 4.4, whose IPAddress.family lacks the 4.5+ str-address branch."""

    def test_str_address(self):
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import ip_family

        assert ip_family(IPAddress(address="10.0.0.1/24")) == 4
        assert ip_family(IPAddress(address="2001:db8::1/64")) == 6

    def test_ipnetwork_address(self):
        import netaddr
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import ip_family

        assert ip_family(IPAddress(address=netaddr.IPNetwork("10.0.0.1/24"))) == 4

    def test_empty_address_is_none(self):
        from ipam.models import IPAddress

        from netbox_librenms_plugin.utils import ip_family

        assert ip_family(IPAddress()) is None


class TestCoercePositiveInt:
    """coerce_positive_int accepts only positive int / int-string and rejects non-integer types (no float truncation)."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (5, 5),
            ("7", 7),
            (1, 1),
            (0, None),
            (-3, None),
            ("0", None),
            ("-3", None),
            (None, None),
            (True, None),  # bool is an int subclass — must not become 1
            (False, None),
            (1.9, None),  # float must NOT int()-truncate to 1
            (1.0, None),  # even a whole float is rejected (non-integer type)
            ("1.9", None),  # non-integer string
            ("abc", None),
            ([], None),
            ({}, None),
        ],
    )
    def test_coercion(self, value, expected):
        from netbox_librenms_plugin.utils import coerce_positive_int

        assert coerce_positive_int(value) == expected


@pytest.mark.django_db
class TestGetVirtualChassisMemberNoneName:
    """A LibreNMS port row can lack the selected name field entirely (port.get(...) -> None)."""

    def test_none_port_name_returns_device_instead_of_typeerror(self):
        """A None port name falls back to the viewed device instead of raising TypeError (was a 500)."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        dev = make_device("vc-none-name-dev")
        member = make_device("vc-none-name-m2")
        make_virtual_chassis("vc-none-name", dev, member)

        assert get_virtual_chassis_member(dev, None) is dev
        # The prefetched-map variant must survive the same input.
        assert get_virtual_chassis_member(dev, None, members_by_position={2: member}) is dev


def test_whitespace_only_names_do_not_count_as_unambiguous():
    """A blank-after-strip name is not a name, the same rule the sync path applies.

    ``get_interface_port_identity_sets`` used a bare truthiness test while every other site
    stripped first, so "   " counted as a distinct interface name here and as unsyncable
    there. Two readers of one rule cannot disagree.
    """
    from netbox_librenms_plugin.utils import get_interface_port_identity_sets

    ports = [
        {"port_id": 10, "ifDescr": "   "},
        {"port_id": 20, "ifDescr": "Unique"},
    ]

    unique_port_ids, unambiguous_name_port_ids = get_interface_port_identity_sets(ports, "ifDescr")

    assert unique_port_ids == {10, 20}
    assert unambiguous_name_port_ids == {20}
