"""Coverage tests for utils.py missing lines."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.django_db
class TestSerialScopeNormalization:
    """Juniper prefixes ENTITY-MIB serials with a literal "S/N ".

    The rewrite is a NormalizationRule rather than compiled-in, so an operator can see why a
    stored serial differs from the raw inventory and add the next vendor without a release.
    """

    def test_the_migration_seeds_the_juniper_rule(self):
        from netbox_librenms_plugin.models import NormalizationRule

        assert NormalizationRule.objects.filter(
            scope=NormalizationRule.SCOPE_SERIAL, match_pattern=r"^S/N\s+(.+)$"
        ).exists()

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("S/N BCFB9793", "BCFB9793"),
            ("  S/N BCFB9751  ", "BCFB9751"),
            ("BCFB9793", "BCFB9793"),
            ("SN12345", "SN12345"),
            ("S/NABC", "S/NABC"),
            (12345, "12345"),
            (0, "0"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_the_seeded_rule_strips_only_the_marker(self, raw, expected):
        from netbox_librenms_plugin.utils import normalize_inventory_serial

        assert normalize_inventory_serial(raw) == expected

    def test_normalize_serial_itself_no_longer_strips(self):
        """The transformation lives in the rule, so there is one place to look."""
        from netbox_librenms_plugin.utils import normalize_serial

        assert normalize_serial("S/N BCFB9793") == "S/N BCFB9793"

    def test_disabling_the_rule_stops_the_rewrite(self):
        """Proves the stored serial follows the rule rather than compiled-in behaviour."""
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.utils import normalize_inventory_serial

        NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()

        assert normalize_inventory_serial("S/N BCFB9793") == "S/N BCFB9793"


class TestConvertSpeedToKbps:
    """Boundary and type tests for convert_speed_to_kbps."""

    def test_none_returns_none(self):
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        assert convert_speed_to_kbps(None) is None

    def test_zero_returns_zero(self):
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        assert convert_speed_to_kbps(0) == 0

    def test_sub_kbps_rounds_down_to_zero(self):
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        assert convert_speed_to_kbps(1) == 0
        assert convert_speed_to_kbps(999) == 0

    def test_exact_kbps_boundary(self):
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        assert convert_speed_to_kbps(1000) == 1

    def test_1gbps(self):
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        assert convert_speed_to_kbps(1_000_000_000) == 1_000_000

    def test_string_input_raises_type_error(self):
        from netbox_librenms_plugin.utils import convert_speed_to_kbps

        with pytest.raises(TypeError):
            convert_speed_to_kbps("1000000")


class TestGetVirtualChassisMemberException:
    """Tests for get_virtual_chassis_member exception path (lines 76-77)."""

    def test_exception_returns_original_device(self):
        """When ObjectDoesNotExist raised, return original device."""
        from django.core.exceptions import ObjectDoesNotExist

        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        device = MagicMock()
        device.virtual_chassis = MagicMock()
        device.virtual_chassis.members.get.side_effect = ObjectDoesNotExist("not found")

        result = get_virtual_chassis_member(device, "Ethernet1")
        assert result is device

    def test_no_virtual_chassis_returns_device(self):
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        device = MagicMock()
        device.virtual_chassis = None
        result = get_virtual_chassis_member(device, "Ethernet1")
        assert result is device

    def test_port_name_no_digit_returns_device(self):
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        device = MagicMock()
        device.virtual_chassis = MagicMock()
        # Port name with no leading digit after alpha chars → no match
        result = get_virtual_chassis_member(device, "Management")
        assert result is device


@pytest.mark.django_db
class TestGetVirtualChassisMemberEmptyPrefetchMap:
    """An empty prefetched members_by_position map must not silently disable member resolution."""

    def test_empty_map_falls_back_to_the_db_lookup(self):
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        master = make_device("vc-empty-map-1")
        member2 = make_device("vc-empty-map-2")
        vc = VirtualChassis.objects.create(name="vc-empty-map", master=master)
        for dev, pos in ((master, 1), (member2, 2)):
            dev.virtual_chassis = vc
            dev.vc_position = pos
            dev.save()

        # A caller forwarding a raw-but-empty map must still resolve via the per-call query,
        # not short-circuit every row to the fallback device.
        assert get_virtual_chassis_member(master, "Ethernet2", members_by_position={}) == member2


class TestGetLibreNMSSyncDeviceServerKey:
    """Tests for get_librenms_sync_device with server_key (lines 113-125)."""

    def test_returns_member_with_dict_cf_for_server_key(self):
        """Priority 1: member with dict CF matching server_key."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc

        member1 = MagicMock()
        member1.cf = {"librenms_id": {"default": 42}}
        member2 = MagicMock()
        member2.cf = {"librenms_id": None}

        vc.members.all.return_value = [member1, member2]

        result = get_librenms_sync_device(device, server_key="default")
        assert result is member1

    def test_falls_back_to_get_librenms_device_id_when_no_dict(self):
        """Priority 2 legacy: falls back to get_librenms_device_id."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc

        member = MagicMock()
        member.cf = {"librenms_id": None}
        member.primary_ip = MagicMock()

        vc.members.all.return_value = [member]
        vc.master = None

        with patch("netbox_librenms_plugin.utils.get_librenms_device_id") as mock_get_id:
            mock_get_id.return_value = 99
            result = get_librenms_sync_device(device, server_key="default")
            assert result is member

    def test_server_key_none_matches_any_dict_member(self):
        """server_key=None: matches any member with any librenms_id in dict."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc

        member_with_id = MagicMock()
        member_with_id.cf = {"librenms_id": {"primary": 10}}
        member_without_id = MagicMock()
        member_without_id.cf = {"librenms_id": None}

        vc.members.all.return_value = [member_without_id, member_with_id]

        result = get_librenms_sync_device(device, server_key=None)
        assert result is member_with_id

    def test_float_id_is_rejected_not_int_truncated(self):
        """A float id (e.g. 1.0) is rejected as invalid, not truncated to an int."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc

        member_float = MagicMock()
        member_float.cf = {"librenms_id": {"default": 1.0}}  # invalid: float, not int/str
        member_valid = MagicMock()
        member_valid.cf = {"librenms_id": {"prod": 5}}

        vc.members.all.return_value = [member_float, member_valid]

        result = get_librenms_sync_device(device, server_key=None)
        assert result is member_valid

    def test_server_key_none_matches_legacy_cf(self):
        """server_key=None: matches member with legacy bare int librenms_id."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc

        member = MagicMock()
        member.cf = {"librenms_id": 42}  # legacy bare int

        vc.members.all.return_value = [member]

        result = get_librenms_sync_device(device, server_key=None)
        assert result is member


class TestGetLibreNMSSyncDeviceLegacyInt:
    """Tests for get_librenms_sync_device legacy int CF (lines 132-133)."""

    def test_legacy_int_cf_with_server_key_uses_get_id(self):
        """server_key set, raw_cf is legacy int → doesn't match dict path, falls back."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc

        member = MagicMock()
        member.cf = {"librenms_id": 55}  # legacy int, not dict

        vc.members.all.return_value = [member]
        vc.master = None

        with patch("netbox_librenms_plugin.utils.get_librenms_device_id") as mock_get_id:
            mock_get_id.return_value = 55
            result = get_librenms_sync_device(device, server_key="default")
        assert result is member


class TestGetLibreNMSSyncDeviceFallbacks:
    """Tests for get_librenms_sync_device fallback paths (lines 138-150)."""

    def test_falls_back_to_master_with_primary_ip(self):
        """When no member has librenms_id, uses master with primary IP."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc

        member = MagicMock()
        member.cf = {"librenms_id": None}

        master = MagicMock()
        master.primary_ip = MagicMock()
        vc.master = master
        vc.members.all.return_value = [member]

        with patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=None):
            result = get_librenms_sync_device(device, server_key="default")
        assert result is master

    def test_falls_back_to_any_member_with_primary_ip(self):
        """When no master, falls back to any member with primary IP."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc

        member_no_ip = MagicMock()
        member_no_ip.cf = {"librenms_id": None}
        member_no_ip.primary_ip = None

        member_with_ip = MagicMock()
        member_with_ip.cf = {"librenms_id": None}
        member_with_ip.primary_ip = MagicMock()

        vc.master = None
        vc.members.all.return_value = [member_no_ip, member_with_ip]

        with patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=None):
            result = get_librenms_sync_device(device, server_key="default")
        assert result is member_with_ip

    def test_falls_back_to_lowest_vc_position(self):
        """Fallback to member with lowest vc_position when no IPs."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        device = MagicMock()
        vc = MagicMock()
        device.virtual_chassis = vc

        m1 = MagicMock()
        m1.cf = {"librenms_id": None}
        m1.primary_ip = None
        m1.vc_position = 3

        m2 = MagicMock()
        m2.cf = {"librenms_id": None}
        m2.primary_ip = None
        m2.vc_position = 1

        vc.master = None
        vc.members.all.return_value = [m1, m2]

        with patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=None):
            result = get_librenms_sync_device(device, server_key="default")
        assert result is m2


class TestGetTablePaginateCountValueError:
    """Tests for get_table_paginate_count fallback paths: a non-integer per_page hits the ValueError handler, and a zero/negative per_page hits the `< 1` guard — both fall back to the NetBox default rather than propagating to the paginator."""

    def test_invalid_per_page_falls_back_to_default(self):
        from netbox_librenms_plugin.utils import get_table_paginate_count

        request = MagicMock()
        request.GET = {"table_per_page": "not_a_number"}

        with patch("netbox_librenms_plugin.utils.get_config"):
            with patch("netbox_librenms_plugin.utils.netbox_get_paginate_count") as mock_paginate:
                mock_paginate.return_value = 50
                result = get_table_paginate_count(request, "table_")
        assert result == 50

    def test_non_positive_per_page_falls_back_to_default(self):
        """0 or negative input must not propagate to the paginator."""
        from netbox_librenms_plugin.utils import get_table_paginate_count

        for raw in ("0", "-5"):
            request = MagicMock()
            request.GET = {"table_per_page": raw}
            with patch("netbox_librenms_plugin.utils.get_config"):
                with patch("netbox_librenms_plugin.utils.netbox_get_paginate_count") as mock_paginate:
                    mock_paginate.return_value = 50
                    result = get_table_paginate_count(request, "table_")
            assert result == 50, f"per_page={raw!r} should fall back to default"


class TestGetUserPrefNoConfig:
    """Tests for get_user_pref when user has no config (line 179)."""

    def test_returns_default_when_no_config_attr(self):
        from netbox_librenms_plugin.utils import get_user_pref

        request = MagicMock(spec=["user"])
        request.user = MagicMock(spec=["has_perm"])  # No 'config' attr
        result = get_user_pref(request, "some.pref", default="fallback")
        assert result == "fallback"

    def test_returns_none_when_no_user(self):
        from netbox_librenms_plugin.utils import get_user_pref

        request = MagicMock(spec=[])  # No 'user' attr
        result = get_user_pref(request, "some.pref")
        assert result is None


class TestSaveUserPrefExceptions:
    """Tests for save_user_pref TypeError/ValueError exceptions (lines 187-188)."""

    def test_type_error_is_swallowed(self):
        from netbox_librenms_plugin.utils import save_user_pref

        request = MagicMock()
        request.user = MagicMock()
        request.user.config.set.side_effect = TypeError("bad type")

        # Should not raise
        save_user_pref(request, "some.pref", "value")

    def test_value_error_is_swallowed(self):
        from netbox_librenms_plugin.utils import save_user_pref

        request = MagicMock()
        request.user = MagicMock()
        request.user.config.set.side_effect = ValueError("bad value")

        save_user_pref(request, "some.pref", "value")


class TestMatchLibrenmsHardwareImportError:
    """Tests for DeviceTypeMapping ImportError guard (line 242)."""

    def test_no_hardware_returns_no_match(self):
        """Empty hardware string returns no match."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("")
        assert result["matched"] is False

    def test_dash_hardware_returns_no_match(self):
        """'-' hardware returns no match."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        result = match_librenms_hardware_to_device_type("-")
        assert result["matched"] is False


# match_librenms_hardware_to_device_type runs the device_type NormalizationRule query (issue
# #90); django_db lets that real (empty) query run while the lookups stay mocked below.
@pytest.mark.django_db
class TestMatchLibrenmsHardwareDeviceTypeMappingPaths:
    """Tests for DeviceTypeMapping paths (lines 251-261)."""

    def test_device_type_mapping_found(self):
        """DeviceTypeMapping.objects.get returns match → return mapping result."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        mock_device_type = MagicMock()
        mock_mapping = MagicMock()
        mock_mapping.netbox_device_type = mock_device_type

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})

        mock_dtm_class = MagicMock()
        mock_dtm_class.DoesNotExist = DoesNotExist
        mock_dtm_class.MultipleObjectsReturned = MultipleObjectsReturned
        mock_dtm_class.objects.get.return_value = mock_mapping

        with patch("netbox_librenms_plugin.models.DeviceTypeMapping", mock_dtm_class, create=True):
            result = match_librenms_hardware_to_device_type("C9300-48P")

        assert result["matched"] is True
        assert result["device_type"] is mock_device_type
        assert result["match_type"] == "mapping"

    def test_device_type_mapping_multiple_returns_logs_warning(self):
        """DeviceTypeMapping.MultipleObjectsReturned → logs warning and skips mapping."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})

        mock_dtm_class = MagicMock()
        mock_dtm_class.DoesNotExist = DoesNotExist
        mock_dtm_class.MultipleObjectsReturned = MultipleObjectsReturned
        mock_dtm_class.objects.get.side_effect = MultipleObjectsReturned("multiple")

        dt_DoesNotExist = type("DoesNotExist", (Exception,), {})
        dt_MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})

        with patch("netbox_librenms_plugin.models.DeviceTypeMapping", mock_dtm_class, create=True):
            with patch("dcim.models.DeviceType") as MockDT:
                MockDT.DoesNotExist = dt_DoesNotExist
                MockDT.MultipleObjectsReturned = dt_MultipleObjectsReturned
                MockDT.objects.get.side_effect = dt_DoesNotExist("no match")
                result = match_librenms_hardware_to_device_type("Ambiguous Hardware")

        assert result is None  # multiple DeviceTypeMapping matches returns None (ambiguous)


@pytest.mark.django_db
class TestMatchLibrenmsHardwareDeviceTypeNormalization:
    """Issue #90: the documented ``device_type`` NormalizationRule scope must clean the raw LibreNMS hardware string before the DeviceTypeMapping / part_number / model lookups (docs/usage_tips/mapping_rules.md)."""

    def _make_device_type(self):
        from dcim.models import DeviceType, Manufacturer

        mfr = Manufacturer.objects.create(name="Cisco-90", slug="cisco-90")
        return DeviceType.objects.create(manufacturer=mfr, model="C9300-48P", slug="c9300-48p-90")

    def test_device_type_scope_rule_normalizes_before_mapping_lookup(self):
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        dt = self._make_device_type()
        # Mapping is keyed on the normalized hardware string (model lowercases on save).
        DeviceTypeMapping.objects.create(librenms_hardware="C9300-48P", netbox_device_type=dt)
        # A device_type-scoped rule strips the "WS-" prefix the raw LibreNMS string carries.
        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(.+)$", replacement=r"\1", priority=10
        )

        result = match_librenms_hardware_to_device_type("WS-C9300-48P")

        assert result is not None
        assert result["matched"] is True
        assert result["device_type"] == dt
        assert result["match_type"] == "mapping"

    def test_wrong_scope_rule_does_not_affect_device_type_matching(self):
        """A module_type-scoped rule must NOT normalize device-type lookups: the raw string is used unchanged, so a WS-prefixed string does not match while the bare string does."""
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        dt = self._make_device_type()
        DeviceTypeMapping.objects.create(librenms_hardware="C9300-48P", netbox_device_type=dt)
        NormalizationRule.objects.create(
            scope="module_type", match_pattern=r"^WS-(.+)$", replacement=r"\1", priority=10
        )

        # Bare string matches the mapping directly; the wrong-scope WS- rule is not applied.
        assert match_librenms_hardware_to_device_type("C9300-48P")["matched"] is True
        assert match_librenms_hardware_to_device_type("WS-C9300-48P")["matched"] is False

    def test_whitespace_padded_hardware_matches_stripped_mapping(self):
        """Hardware with surrounding whitespace must still match the stripped-stored mapping (save() does .strip().lower(); iexact is not whitespace-insensitive, so the lookup must strip)."""
        from netbox_librenms_plugin.models import DeviceTypeMapping
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        dt = self._make_device_type()
        DeviceTypeMapping.objects.create(librenms_hardware="C9300-48P", netbox_device_type=dt)  # stored "c9300-48p"

        # Unfixed: with no rules, search_name is the raw " C9300-48P " passed unstripped to iexact
        # → never matches the stored "c9300-48p". Fixed: search_name is stripped before lookup.
        result = match_librenms_hardware_to_device_type(" C9300-48P ")
        assert result["matched"] is True
        assert result["device_type"] == dt
        assert result["match_type"] == "mapping"

    def test_whitespace_only_hardware_never_matches_blank_part_number(self):
        """A whitespace-only hardware string (truthy, so it passes the empty guard) must not match a DeviceType whose part_number is blank — part_number__iexact="" matches the field's default."""
        from dcim.models import DeviceType

        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        self._make_device_type()  # has no part number → a blank-part_number DeviceType exists
        assert DeviceType.objects.filter(part_number="").exists()

        result = match_librenms_hardware_to_device_type("   ")
        assert result is not None
        assert result["matched"] is False
        assert result["device_type"] is None

    def test_rule_emptied_hardware_does_not_match_blank_part_number(self):
        """A rule that reduces the whole hardware string to empty must not turn the exact lookups into part_number__iexact="" — the raw string is what the exact lookups get, and it matches nothing here."""
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        self._make_device_type()  # blank part_number exists
        # replacement may not be blank, so empty the string via an empty capture group.
        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(\s*)$", replacement=r"\1", priority=10
        )

        result = match_librenms_hardware_to_device_type("WS-")
        assert result is not None
        assert result["matched"] is False

    def test_crud_form_stored_mapping_still_matches_via_raw_fallback(self):
        """A mapping created through the standard CRUD/CSV write path (model clean: .strip().lower() only, NO rule normalization) must still be found: the lookup falls back to the raw hardware string when the normalized key misses."""
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        dt = self._make_device_type()
        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(.+)$", replacement=r"\1", priority=10
        )
        # The standard form/CSV path stores the entered string un-normalized → "ws-c9300-48p".
        DeviceTypeMapping.objects.create(librenms_hardware="WS-C9300-48P", netbox_device_type=dt)

        # Lookup normalizes to "C9300-48P" (miss) and must fall back to the raw key (hit).
        result = match_librenms_hardware_to_device_type("WS-C9300-48P")
        assert result is not None
        assert result["matched"] is True
        assert result["device_type"] == dt
        assert result["match_type"] == "mapping"

    def test_raw_exact_model_match_survives_rule_addition(self):
        """The part_number/model exact lookups use the RAW LibreNMS string (the documented rule scope is the DeviceTypeMapping lookup only) — adding a rule must not break a previously-working literal model match."""
        from dcim.models import DeviceType, Manufacturer

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        mfr = Manufacturer.objects.create(name="Cisco-raw-exact", slug="cisco-raw-exact")
        dt = DeviceType.objects.create(manufacturer=mfr, model="WS-C9300-48P", slug="ws-c9300-48p-raw")
        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(.+)$", replacement=r"\1", priority=10
        )

        # Raw string literally equals the DeviceType model; the rule must not hide it.
        result = match_librenms_hardware_to_device_type("WS-C9300-48P")
        assert result is not None
        assert result["matched"] is True
        assert result["device_type"] == dt
        assert result["match_type"] == "exact"


@pytest.mark.django_db
class TestChassisFallbackHonorsPreloadedRules:
    """Issue #90 / N+1: the chassis inventory fallback (_try_chassis_device_type_match) must thread the preloaded device_type rules into its own match call."""

    def test_chassis_fallback_uses_preloaded_rules_no_per_call_query(self):
        from unittest.mock import MagicMock

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from dcim.models import DeviceType, Manufacturer
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule
        from netbox_librenms_plugin.utils import preload_normalization_rules

        mfr = Manufacturer.objects.create(name="Cisco-chassis", slug="cisco-chassis")
        dt = DeviceType.objects.create(manufacturer=mfr, model="C9300-48P", slug="c9300-48p-chassis")
        DeviceTypeMapping.objects.create(librenms_hardware="C9300-48P", netbox_device_type=dt)
        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(.+)$", replacement=r"\1", priority=10
        )
        preloaded = preload_normalization_rules("device_type")

        # api is a true external boundary (LibreNMS HTTP): stub only the inventory call.
        api = MagicMock()
        api.get_inventory_filtered.return_value = (True, [{"entPhysicalName": "WS-C9300-48P"}])

        with CaptureQueriesContext(connection) as ctx:
            result = _try_chassis_device_type_match(api, 123, preloaded_device_type_rules=preloaded)

        assert result is not None
        assert result["matched"] is True
        assert result["device_type"] == dt
        assert result["match_type"] == "chassis"
        # The preloaded rules must reach the inner matcher, so no NormalizationRule query fires here.
        assert not any("normalizationrule" in q["sql"].lower() for q in ctx.captured_queries)

    def test_preloaded_rules_skip_the_per_call_normalization_query(self):
        """Perf (issue #90 / N+1): passing preloaded_rules must avoid the per-call device_type NormalizationRule query — the bulk-import loop preloads it once instead of per device."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.utils import (
            match_librenms_hardware_to_device_type,
            preload_normalization_rules,
        )

        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(.+)$", replacement=r"\1", priority=10
        )
        preloaded = preload_normalization_rules("device_type")

        with CaptureQueriesContext(connection) as preloaded_ctx:
            match_librenms_hardware_to_device_type("WS-C9300-48P", preloaded_rules=preloaded)
        assert not any("normalizationrule" in q["sql"].lower() for q in preloaded_ctx.captured_queries)

        # Without preloading, the match does hit NormalizationRule (the per-device cost).
        with CaptureQueriesContext(connection) as unpreloaded_ctx:
            match_librenms_hardware_to_device_type("WS-C9300-48P")
        assert any("normalizationrule" in q["sql"].lower() for q in unpreloaded_ctx.captured_queries)


@pytest.mark.django_db
class TestMatchLibrenmsHardwareDeviceTypeMultipleReturned:
    """Tests for DeviceType MultipleObjectsReturned — ambiguity surfaces as None."""

    def test_part_number_multiple_returns_none(self):
        """DeviceType.MultipleObjectsReturned for part_number → return None (not silently pick first)."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})

        dtm_DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_dtm = MagicMock()
        mock_dtm.DoesNotExist = dtm_DoesNotExist
        mock_dtm.MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})
        mock_dtm.objects.get.side_effect = dtm_DoesNotExist()

        with patch("netbox_librenms_plugin.models.DeviceTypeMapping", mock_dtm, create=True):
            with patch("dcim.models.DeviceType") as MockDT:
                MockDT.DoesNotExist = DoesNotExist
                MockDT.MultipleObjectsReturned = MultipleObjectsReturned
                MockDT.objects.get.side_effect = MultipleObjectsReturned("multiple")

                result = match_librenms_hardware_to_device_type("C9300")

        assert result is None

    def test_model_multiple_returns_none(self):
        """DeviceType.MultipleObjectsReturned for model → return None (not silently pick first)."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})

        def get_side_effect(**kwargs):
            if "part_number__iexact" in kwargs:
                raise DoesNotExist("no part number")
            raise MultipleObjectsReturned("multiple models")

        dtm_DoesNotExist = type("DoesNotExist", (Exception,), {})
        mock_dtm = MagicMock()
        mock_dtm.DoesNotExist = dtm_DoesNotExist
        mock_dtm.MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})
        mock_dtm.objects.get.side_effect = dtm_DoesNotExist()

        with patch("netbox_librenms_plugin.models.DeviceTypeMapping", mock_dtm, create=True):
            with patch("dcim.models.DeviceType") as MockDT:
                MockDT.DoesNotExist = DoesNotExist
                MockDT.MultipleObjectsReturned = MultipleObjectsReturned
                MockDT.objects.get.side_effect = get_side_effect

                result = match_librenms_hardware_to_device_type("SomeModel")

        assert result is None


class TestFindMatchingSiteMultipleReturned:
    """Tests for find_matching_site MultipleObjectsReturned (lines 325-327)."""

    def test_multiple_objects_returned_uses_first(self):
        from netbox_librenms_plugin.utils import find_matching_site

        mock_site = MagicMock()
        Site_DoesNotExist = type("DoesNotExist", (Exception,), {})
        Site_MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})

        with patch("dcim.models.Site") as MockSite:
            MockSite.DoesNotExist = Site_DoesNotExist
            MockSite.MultipleObjectsReturned = Site_MultipleObjectsReturned
            MockSite.objects.get.side_effect = Site_MultipleObjectsReturned("multiple")
            MockSite.objects.filter.return_value.first.return_value = mock_site

            result = find_matching_site("NYC")
            assert result["found"] is True
            assert result["site"] is mock_site


@pytest.mark.django_db
class TestFindMatchingPlatformMultipleReturned:
    """find_matching_platform must fail closed when the Platform name is ambiguous."""

    def test_multiple_objects_returned_returns_ambiguous(self):
        from dcim.models import Platform

        from netbox_librenms_plugin.utils import find_matching_platform

        # Platform.name is unique case-SENSITIVELY while the lookup is case-INsensitive, so
        # these two rows coexist and name__iexact matches both. No PlatformMapping exists to
        # break the tie, so the ambiguity is surfaced rather than resolved arbitrarily.
        Platform.objects.create(name="ios", slug="ios")
        Platform.objects.create(name="IOS", slug="ios-upper")

        result = find_matching_platform("ios")

        assert result == {"found": False, "platform": None, "match_type": "ambiguous", "ambiguity_source": "platform"}


class TestGetMissingVlanWarning:
    """Tests for get_missing_vlan_warning when vid in missing_vlans (lines 462-467)."""

    def test_vid_in_missing_vlans_returns_warning_html(self):
        from netbox_librenms_plugin.utils import get_missing_vlan_warning

        result = get_missing_vlan_warning(100, [100, 200])
        assert "mdi-alert" in result
        assert "text-danger" in result

    def test_vid_not_in_missing_vlans_returns_empty_string(self):
        from netbox_librenms_plugin.utils import get_missing_vlan_warning

        result = get_missing_vlan_warning(999, [100, 200])
        assert result == ""


class TestGetLibreNMSDeviceIdStringNormalization:
    """Tests for get_librenms_device_id string normalization (lines 557-558)."""

    def test_string_id_normalized_to_int_and_saved(self):
        """String stored as librenms_id is normalized to int and saved."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": "42"}
        obj.custom_field_data = {"librenms_id": "42"}

        result = get_librenms_device_id(obj, "default", auto_save=True)
        assert result == 42
        # Should save to normalize
        obj.save.assert_called_once()

    def test_string_id_returned_without_save_when_auto_save_false(self):
        """String normalized but not saved when auto_save=False."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": "99"}
        obj.custom_field_data = {"librenms_id": "99"}

        result = get_librenms_device_id(obj, "default", auto_save=False)
        assert result == 99
        obj.save.assert_not_called()

    def test_dict_with_string_value_normalized(self):
        """Dict entry with string value is normalized to int."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": {"default": "77"}}
        obj.custom_field_data = {"librenms_id": {"default": "77"}}

        result = get_librenms_device_id(obj, "default", auto_save=True)
        assert result == 77
        obj.save.assert_called_once()

    def test_invalid_string_returns_none(self):
        """Non-digit string in librenms_id returns None."""
        from netbox_librenms_plugin.utils import get_librenms_device_id

        obj = MagicMock()
        obj.cf = {"librenms_id": "not-a-number"}
        obj.custom_field_data = {"librenms_id": "not-a-number"}

        result = get_librenms_device_id(obj, "default")
        assert result is None


class TestFindByLibreNMSIdNoneGuard:
    """Verify find_by_librenms_id returns None for None input without querying the DB."""

    def test_none_id_returns_none_without_query(self):
        """find_by_librenms_id(None, ...) must return None without hitting the DB."""
        from netbox_librenms_plugin.utils import find_by_librenms_id

        model = MagicMock()
        result = find_by_librenms_id(model, None, server_key="default")
        assert result is None
        model.objects.filter.assert_not_called()

    @pytest.mark.django_db(transaction=False)
    def test_select_for_update_locks_and_returns_owner(self):
        """find_by_librenms_id(..., select_for_update=True) locks + returns the owning row inside a txn (serializes a concurrent conflict check against an existing owner)."""
        from django.db import transaction

        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.utils import find_by_librenms_id

        device = make_device("locked-owner", librenms_cf={"default": 4242})
        # select_for_update requires an open transaction; the locked read must still resolve
        # the owner (best-effort serialization is additive, not a behaviour change).
        with transaction.atomic():
            found = find_by_librenms_id(Device, 4242, server_key="default", select_for_update=True)
        assert found is not None and found.pk == device.pk


class TestNetboxResolvesModuleTokenPerLeaf:
    """Version-gating helper for {module} token resolution behaviour (NetBox #20467)."""

    def test_returns_true_for_4_5_6(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 5, 6)):
            assert utils.netbox_resolves_module_token_per_leaf() is True

    def test_returns_true_for_4_6_0(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 6, 0)):
            assert utils.netbox_resolves_module_token_per_leaf() is True

    def test_returns_false_for_4_5_5(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 5, 5)):
            assert utils.netbox_resolves_module_token_per_leaf() is False

    def test_returns_false_for_4_4_10(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 4, 10)):
            assert utils.netbox_resolves_module_token_per_leaf() is False

    def test_returns_true_when_version_undetectable(self):
        """Permissive default: avoid false positives on unknown versions."""
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=None):
            assert utils.netbox_resolves_module_token_per_leaf() is True


class TestNetboxCleanReadsParentVirtualChassis:
    """Version gate for the NetBox 4.4.0 Interface.clean() parent dereference (issue #20197)."""

    def test_true_for_the_affected_release(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 4, 0)):
            assert utils.netbox_clean_reads_parent_virtual_chassis() is True

    def test_false_once_the_fix_shipped(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 4, 1)):
            assert utils.netbox_clean_reads_parent_virtual_chassis() is False

    def test_false_for_a_later_release(self):
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 6, 5)):
            assert utils.netbox_clean_reads_parent_virtual_chassis() is False

    def test_false_for_a_release_older_than_the_defect(self):
        """Pins the equality: widening the gate to <= would silently pass every other case."""
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 3, 9)):
            assert utils.netbox_clean_reads_parent_virtual_chassis() is False

    def test_true_when_version_undetectable(self):
        """Keep the tolerance rather than turn a known core defect into a 500."""
        from netbox_librenms_plugin import utils

        with patch.object(utils, "_get_netbox_version_tuple", return_value=None):
            assert utils.netbox_clean_reads_parent_virtual_chassis() is True


class TestHasNestedNameConflictVersionGating:
    """has_nested_name_conflict() must short-circuit on NetBox >= 4.5.6 (issue #20467)."""

    def _build_args(self, with_module_token=True):
        """Build (module_type, module_bay, sibling_counts) that would trigger
        the legacy conflict (nested bay, sibling exists, {module} in template)."""
        template = MagicMock()
        template.name = "{module}" if with_module_token else "Gi0/1"
        module_type = MagicMock()
        module_type.model = "X2-10GB-LR"
        module_type.interfacetemplates.all.return_value = [template]

        module_bay = MagicMock()
        module_bay.module_id = 820
        module_bay.device = MagicMock()

        sibling_counts = {820: 8}
        return module_type, module_bay, sibling_counts

    def test_skipped_on_supported_netbox(self):
        from netbox_librenms_plugin import utils

        module_type, module_bay, sibling_counts = self._build_args()
        with patch.object(utils, "netbox_resolves_module_token_per_leaf", return_value=True):
            result = utils.has_nested_name_conflict(module_type, module_bay, sibling_counts)
        assert result == ""

    def test_warns_on_old_netbox(self):
        from netbox_librenms_plugin import utils

        module_type, module_bay, sibling_counts = self._build_args()
        with patch.object(utils, "netbox_resolves_module_token_per_leaf", return_value=False):
            result = utils.has_nested_name_conflict(module_type, module_bay, sibling_counts)
        assert result != ""
        assert "X2-10GB-LR" in result
        assert "4.5.6" in result
        assert "20467" in result

    def test_old_netbox_no_module_token_no_conflict(self):
        from netbox_librenms_plugin import utils

        module_type, module_bay, sibling_counts = self._build_args(with_module_token=False)
        with patch.object(utils, "netbox_resolves_module_token_per_leaf", return_value=False):
            result = utils.has_nested_name_conflict(module_type, module_bay, sibling_counts)
        assert result == ""

    def test_old_netbox_top_level_bay_no_conflict(self):
        from netbox_librenms_plugin import utils

        module_type, module_bay, sibling_counts = self._build_args()
        module_bay.module_id = None
        with patch.object(utils, "netbox_resolves_module_token_per_leaf", return_value=False):
            result = utils.has_nested_name_conflict(module_type, module_bay, sibling_counts)
        assert result == ""

    def test_old_netbox_single_sibling_no_conflict(self):
        from netbox_librenms_plugin import utils

        module_type, module_bay, sibling_counts = self._build_args()
        sibling_counts = {820: 1}
        with patch.object(utils, "netbox_resolves_module_token_per_leaf", return_value=False):
            result = utils.has_nested_name_conflict(module_type, module_bay, sibling_counts)
        assert result == ""


class TestGetNetboxVersionTuple:
    """Parse netbox.settings.RELEASE.version into a comparable tuple."""

    def test_parses_standard_version(self):
        from netbox_librenms_plugin import utils

        fake_release = MagicMock(version="4.5.8")
        with patch("netbox.settings.RELEASE", fake_release):
            assert utils._get_netbox_version_tuple() == (4, 5, 8)

    def test_strips_build_suffix(self):
        from netbox_librenms_plugin import utils

        fake_release = MagicMock(version="4.5.8-Docker-4.0.2")
        with patch("netbox.settings.RELEASE", fake_release):
            assert utils._get_netbox_version_tuple() == (4, 5, 8)

    def test_returns_none_on_unparseable(self):
        from netbox_librenms_plugin import utils

        fake_release = MagicMock(version="not-a-version")
        with patch("netbox.settings.RELEASE", fake_release):
            assert utils._get_netbox_version_tuple() is None


class TestIsLegacyLibrenmsId:
    """is_legacy_librenms_id: one place defining what counts as the legacy bare-integer form."""

    def test_bare_int_is_legacy(self):
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        assert is_legacy_librenms_id(42) is True

    def test_int_parseable_string_is_legacy(self):
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        # int()-based, unlike str.isdigit(): a whitespace-padded digit string is still legacy.
        assert is_legacy_librenms_id("42") is True
        assert is_legacy_librenms_id(" 42 ") is True

    def test_bool_is_not_legacy(self):
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        # bool is an int subclass; True/False must not be treated as a legacy id.
        assert is_legacy_librenms_id(True) is False
        assert is_legacy_librenms_id(False) is False

    def test_dict_form_and_none_are_not_legacy(self):
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        assert is_legacy_librenms_id({"default": {"id": 42}}) is False
        assert is_legacy_librenms_id(None) is False

    def test_non_numeric_string_is_not_legacy(self):
        from netbox_librenms_plugin.utils import is_legacy_librenms_id

        assert is_legacy_librenms_id("abc") is False
        assert is_legacy_librenms_id("") is False


class TestValidateRegexField:
    """validate_regex_field centralizes the re.compile -> ValidationError validator the mapping models share (NormalizationRule / CarrierAutoInstallRule / InventoryIgnoreRule / ModuleBayMapping / PortStackLagPattern)."""

    def test_valid_pattern_returns_compiled(self):
        import re

        from netbox_librenms_plugin.utils import validate_regex_field

        compiled = validate_regex_field(r"^Po\d+$", "lag_name_pattern")
        assert isinstance(compiled, re.Pattern)
        assert compiled.match("Po10")

    def test_invalid_pattern_raises_field_scoped_validation_error(self):
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.utils import validate_regex_field

        with pytest.raises(ValidationError) as exc_info:
            validate_regex_field("[unbalanced(", "device_type_pattern")
        # Error attached to the named field with the shared "Invalid regex" wording.
        assert "device_type_pattern" in exc_info.value.message_dict
        assert "Invalid regex" in exc_info.value.message_dict["device_type_pattern"][0]

    def test_model_clean_routes_bad_regex_through_the_shared_validator(self):
        """A mapping model's clean() must reject an invalid regex via the shared helper — the bad-regex path through these models was previously untested."""
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.models import ModuleBayMapping

        mapping = ModuleBayMapping.__new__(ModuleBayMapping)
        mapping.librenms_name = "[unbalanced("
        mapping.librenms_class = ""
        mapping.netbox_bay_name = "Slot 1"
        mapping.is_regex = True
        mapping.description = ""
        with patch("netbox.models.NetBoxModel.clean"):
            with pytest.raises(ValidationError) as exc_info:
                mapping.clean()
        assert "librenms_name" in exc_info.value.message_dict
        assert "Invalid regex" in exc_info.value.message_dict["librenms_name"][0]


@pytest.mark.django_db
class TestInterfaceNameFallbackMatchesPort:
    """The fallback reader must agree with get_librenms_device_id on every stored shape.

    The two used to walk custom_field_data separately, so they could drift on which shapes
    resolve. Only the "no binding recorded" rules stay local to the fallback.
    """

    def _interface(self, stored):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("iface-fallback-shapes")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        if stored is not None:
            interface.custom_field_data["librenms_id"] = stored
            interface.save(update_fields=["custom_field_data"])
        return interface

    @pytest.mark.parametrize(
        "stored",
        [
            {"default": 42},
            {"default": "42"},
            {"default": {"id": 42}},
            {"default": {"id": "42"}},
            42,
            "42",
        ],
    )
    def test_agrees_with_the_shared_reader(self, stored):
        from netbox_librenms_plugin.utils import get_librenms_device_id, interface_name_fallback_matches_port

        interface = self._interface(stored)
        assert get_librenms_device_id(interface, "default", auto_save=False) == 42
        assert interface_name_fallback_matches_port(interface, 42, "default") is True
        assert interface_name_fallback_matches_port(interface, 43, "default") is False

    def test_absent_field_counts_as_unbound(self):
        from netbox_librenms_plugin.utils import interface_name_fallback_matches_port

        interface = self._interface(None)
        assert interface_name_fallback_matches_port(interface, 42, "default") is True

    def test_other_server_key_counts_as_unbound(self):
        from netbox_librenms_plugin.utils import interface_name_fallback_matches_port

        interface = self._interface({"other": 42})
        assert interface_name_fallback_matches_port(interface, 42, "default") is True

    def test_unusable_entry_for_this_server_is_not_a_match(self):
        from netbox_librenms_plugin.utils import interface_name_fallback_matches_port

        interface = self._interface({"default": {"no_id": 42}})
        assert interface_name_fallback_matches_port(interface, 42, "default") is False


@pytest.mark.django_db(transaction=True)
class TestAcquireAdvisoryTransactionLock:
    """The lock must bind to the connection that owns the caller's transaction."""

    def test_requires_an_open_transaction(self):
        from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock

        with pytest.raises(RuntimeError, match="requires an open transaction"):
            acquire_advisory_transaction_lock("nblp-test:no-transaction")

    def test_takes_the_lock_on_the_named_alias(self):
        from django.db import transaction

        from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock

        with transaction.atomic(using="default"):
            acquire_advisory_transaction_lock("nblp-test:aliased", using="default")

    def test_the_named_alias_selects_the_connection(self):
        """The named alias must select its connection."""
        from django.db import transaction
        from django.utils.connection import ConnectionDoesNotExist

        from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock

        with transaction.atomic(using="default"):
            with pytest.raises(ConnectionDoesNotExist):
                acquire_advisory_transaction_lock("nblp-test:unknown-alias", using="nblp-no-such-alias")

    def test_named_alias_outside_a_transaction_still_refuses(self):
        from netbox_librenms_plugin.utils import acquire_advisory_transaction_lock

        with pytest.raises(RuntimeError, match="requires an open transaction"):
            acquire_advisory_transaction_lock("nblp-test:aliased-no-transaction", using="default")
