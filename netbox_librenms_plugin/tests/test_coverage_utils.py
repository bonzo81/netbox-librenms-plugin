"""Coverage tests for utils.py missing lines."""

from unittest.mock import MagicMock, patch


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
        import pytest
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
    """Tests for get_table_paginate_count ValueError path (lines 169-170)."""

    def test_invalid_per_page_falls_back_to_default(self):
        from netbox_librenms_plugin.utils import get_table_paginate_count

        request = MagicMock()
        request.GET = {"table_per_page": "not_a_number"}

        with patch("netbox_librenms_plugin.utils.get_config"):
            with patch("netbox_librenms_plugin.utils.netbox_get_paginate_count") as mock_paginate:
                mock_paginate.return_value = 50
                result = get_table_paginate_count(request, "table_")
        assert result == 50


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


class TestMatchLibrenmsHardwareDeviceTypeMultipleReturned:
    """Tests for DeviceType MultipleObjectsReturned (lines 277-279, 291-293)."""

    def test_part_number_multiple_returns_uses_first(self):
        """DeviceType.MultipleObjectsReturned for part_number → use first()."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        mock_dt = MagicMock()

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
                MockDT.objects.filter.return_value.first.return_value = mock_dt

                result = match_librenms_hardware_to_device_type("C9300")

        assert result["matched"] is True
        assert result["device_type"] is mock_dt

    def test_model_multiple_returns_uses_first(self):
        """DeviceType.MultipleObjectsReturned for model → use first()."""
        from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

        mock_dt = MagicMock()

        DoesNotExist = type("DoesNotExist", (Exception,), {})
        MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})
        call_count = [0]

        def get_side_effect(**kwargs):
            call_count[0] += 1
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
                MockDT.objects.filter.return_value.first.return_value = mock_dt

                result = match_librenms_hardware_to_device_type("SomeModel")

        assert result["matched"] is True
        assert result["device_type"] is mock_dt


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


class TestFindMatchingPlatformMultipleReturned:
    """Tests for find_matching_platform MultipleObjectsReturned (lines 358-360)."""

    def test_multiple_objects_returned_uses_first(self):
        from netbox_librenms_plugin.utils import find_matching_platform

        mock_platform = MagicMock()
        Platform_DoesNotExist = type("DoesNotExist", (Exception,), {})
        Platform_MultipleObjectsReturned = type("MultipleObjectsReturned", (Exception,), {})

        with patch("dcim.models.Platform") as MockPlatform:
            MockPlatform.DoesNotExist = Platform_DoesNotExist
            MockPlatform.MultipleObjectsReturned = Platform_MultipleObjectsReturned
            MockPlatform.objects.get.side_effect = Platform_MultipleObjectsReturned("multiple")
            MockPlatform.objects.filter.return_value.first.return_value = mock_platform

            result = find_matching_platform("ios")
            assert result["found"] is True
            assert result["platform"] is mock_platform


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
