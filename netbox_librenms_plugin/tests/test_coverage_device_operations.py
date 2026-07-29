"""Coverage tests for import_utils/device_operations.py."""

from unittest.mock import MagicMock, patch

import pytest


class TestTryChassisDeviceTypeMatch:
    """Tests for _try_chassis_device_type_match (lines 45-65)."""

    def test_api_failure_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        api.get_inventory_filtered.return_value = (False, [])
        result = _try_chassis_device_type_match(api, 1)
        assert result is None

    def test_empty_inventory_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        api.get_inventory_filtered.return_value = (True, [])
        result = _try_chassis_device_type_match(api, 1)
        assert result is None

    def test_matched_physical_name_returns_match(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        mock_dt = MagicMock()
        api = MagicMock()
        api.get_inventory_filtered.return_value = (
            True,
            [{"entPhysicalName": "CHAS-BP-MX480-S", "entPhysicalModelName": "model1"}],
        )

        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type"
        ) as mock_match:
            mock_match.return_value = {"matched": True, "device_type": mock_dt, "match_type": "exact"}
            result = _try_chassis_device_type_match(api, 1)

        assert result is not None
        assert result["matched"] is True
        assert result["match_type"] == "chassis"
        assert result["chassis_model"] == "CHAS-BP-MX480-S"

    def test_skips_empty_values(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        api.get_inventory_filtered.return_value = (True, [{"entPhysicalName": "", "entPhysicalModelName": "-"}])

        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type"
        ) as mock_match:
            mock_match.return_value = {"matched": False}
            result = _try_chassis_device_type_match(api, 1)

        mock_match.assert_not_called()
        assert result is None

    def test_exception_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        api.get_inventory_filtered.side_effect = RuntimeError("API Error")
        result = _try_chassis_device_type_match(api, 1)
        assert result is None

    def test_fallback_to_model_name_when_name_not_matched(self):
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        mock_dt = MagicMock()
        api = MagicMock()
        api.get_inventory_filtered.return_value = (
            True,
            [{"entPhysicalName": "Unrecognized", "entPhysicalModelName": "710-017414"}],
        )

        call_count = [0]

        def match_side_effect(value, **kwargs):
            # **kwargs accepts the preloaded_rules the chassis fallback now threads through (#90 N+1).
            call_count[0] += 1
            if value == "Unrecognized":
                return {"matched": False}
            return {"matched": True, "device_type": mock_dt, "match_type": "exact"}

        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
            side_effect=match_side_effect,
        ):
            result = _try_chassis_device_type_match(api, 1)

        assert result is not None
        assert result["matched"] is True
        assert result["chassis_model"] == "710-017414"

    def test_match_returning_none_is_skipped(self):
        """match_librenms_hardware_to_device_type returning None does not raise; continues."""
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        api.get_inventory_filtered.return_value = (
            True,
            [{"entPhysicalName": "SomeChassis", "entPhysicalModelName": ""}],
        )

        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
            return_value=None,
        ):
            result = _try_chassis_device_type_match(api, 1)

        # None return from matcher is safely skipped; function returns None overall
        assert result is None


class TestDetermineDeviceName:
    """Tests for _determine_device_name (lines 68-122)."""

    def test_use_sysname_true_prefers_sysname(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "router01", "hostname": "router01.example.com"}, use_sysname=True)
        assert result == "router01"

    def test_use_sysname_false_prefers_hostname(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "router01", "hostname": "router01.example.com"}, use_sysname=False)
        assert result == "router01.example.com"

    def test_fallback_to_device_id_when_no_name(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({}, device_id=42)
        assert result == "device-42"

    def test_fallback_to_device_id_field_when_no_name_no_id(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"device_id": 99})
        assert result == "device-99"

    def test_strip_domain_true_strips_suffix(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "router01.example.com"}, strip_domain=True)
        assert result == "router01"

    def test_strip_domain_does_not_strip_ip(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "192.168.1.1"}, strip_domain=True)
        assert result == "192.168.1.1"

    def test_hostname_fallback_when_sysname_empty(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "", "hostname": "sw01.example.com"}, use_sysname=True)
        assert result == "sw01.example.com"

    def test_none_sysname_falls_back_to_hostname(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": None, "hostname": "sw02"})
        assert result == "sw02"

    def test_none_hostname_falls_back_to_device_id(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": None, "hostname": None, "device_id": 7})
        assert result == "device-7"

    def test_result_is_never_empty_string(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({})
        assert isinstance(result, str)
        assert result != ""

    def test_fqdn_multiple_dots_strips_to_first_label(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "a.b.c.d.example.com"}, strip_domain=True)
        assert result == "a"

    def test_strip_domain_false_keeps_fqdn(self):
        from netbox_librenms_plugin.import_utils.device_operations import _determine_device_name

        result = _determine_device_name({"sysName": "router.example.com"}, strip_domain=False)
        assert result == "router.example.com"


class TestValidateDeviceStateMachine:
    """Tests for validate_device_for_import is_ready / can_import state transitions."""

    def _run_validate(self, libre_device, patches_overrides=None, **kwargs):
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300

        mock_device = MagicMock()
        mock_device.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.exclude.return_value.first.return_value = None

        mock_vm = MagicMock()
        mock_vm.objects.filter.return_value.first.return_value = None

        mock_cluster = MagicMock()
        mock_cluster.objects.all.return_value = []

        mock_role = MagicMock()
        mock_role.objects.all.return_value = []

        mock_ip = MagicMock()
        mock_ip.objects.filter.return_value.first.return_value = None

        base_patches = [
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
                return_value={"found": False, "site": None, "match_type": None, "suggestions": []},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                return_value={"matched": False, "device_type": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": [], "detection_error": None},
            ),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole", mock_role),
            patch("netbox_librenms_plugin.import_utils.device_operations.Cluster", mock_cluster),
            patch("netbox_librenms_plugin.import_utils.device_operations.Device", mock_device),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType", MagicMock()),
            patch("netbox_librenms_plugin.import_utils.device_operations.Site", MagicMock()),
            patch("netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.import_utils.device_operations.cache"),
            patch("virtualization.models.VirtualMachine", mock_vm),
        ]
        if patches_overrides:
            base_patches.extend(patches_overrides)

        for p in base_patches:
            p.start()
        try:
            result = validate_device_for_import(libre_device, api=api, **kwargs)
        finally:
            for p in reversed(base_patches):
                p.stop()
        return result

    def _base_device(self, device_id=1, hostname="router01"):
        return {
            "device_id": device_id,
            "hostname": hostname,
            "sysName": hostname,
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }

    def test_new_device_without_any_matches_is_not_ready(self):
        """New device with no site/type/role match must not be ready."""
        result = self._run_validate(self._base_device())
        assert result["existing_device"] is None
        assert result["is_ready"] is False
        assert result["can_import"] is False

    def test_ambiguous_librenms_id_blocks_import(self):
        """An ambiguous librenms_id (find_by raises) must block import, not fall through to the not-found path and import as new."""
        from unittest.mock import patch

        from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError

        result = self._run_validate(
            self._base_device(),
            patches_overrides=[
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id",
                    side_effect=AmbiguousLibreNMSIdError("dup host pk=1, pk=2"),
                ),
            ],
        )
        assert result["can_import"] is False
        assert result["existing_match_type"] == "ambiguous_librenms_id"
        assert any("matches more than one" in w for w in result["warnings"])

    def test_ambiguous_librenms_id_is_terminal_no_new_import_blockers(self):
        """An ambiguous librenms_id is the terminal blocker — validation must NOT fall through into the new-import site/device_type/role/cluster checks and pile unrelated 'must select ...' issues onto the row (mirrors bulk_import's terminal ambiguity handling)."""
        from unittest.mock import patch

        from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError

        # find_matching_site returns found=False in the harness; pre-fix the new-import block ran
        # and appended "No matching site found ..." — this asserts that no longer happens.
        result = self._run_validate(
            self._base_device(),
            patches_overrides=[
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id",
                    side_effect=AmbiguousLibreNMSIdError("dup host pk=1, pk=2"),
                ),
            ],
        )
        assert result["ambiguous_librenms_id"] is True
        assert result["can_import"] is False
        assert result["is_ready"] is False
        # None of the new-import blockers may be present — the duplicate id is the only blocker.
        joined = " ".join(result["issues"]).lower()
        assert "site" not in joined
        assert "role" not in joined
        assert "cluster" not in joined

    def test_flag_ambiguous_is_a_durable_blocker(self):
        """The ambiguity must land in issues (not only warnings) so the readiness step's `can_import = len(issues) == 0` recompute cannot silently re-enable the import."""
        from netbox_librenms_plugin.import_utils.device_operations import _flag_ambiguous_librenms_id

        result = {
            "can_import": True,
            "existing_match_type": None,
            "ambiguous_librenms_id": False,
            "warnings": [],
            "issues": [],
        }
        _flag_ambiguous_librenms_id(result, 42, Exception("dup host pk=1, pk=2"))

        assert result["ambiguous_librenms_id"] is True
        assert result["can_import"] is False
        assert any("matches more than one" in i for i in result["issues"])
        # Simulate the later readiness recompute with no other issues — must stay blocked.
        result["can_import"] = len(result["issues"]) == 0
        assert result["can_import"] is False

    def test_vm_match_with_ambiguous_device_lookup_drops_vm_binding(self):
        """VM matches by librenms_id but the cross-model Device collision check is itself ambiguous (raises): the VM binding must be dropped and the import fail closed, never rebound as a definitive 'librenms_id' match."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError

        matched_vm = MagicMock()
        matched_vm.name = "vm01"

        result = self._run_validate(
            self._base_device(),
            patches_overrides=[
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id",
                    # 1st call (VM) → a match; 2nd call (cross-model Device check) → ambiguous.
                    side_effect=[matched_vm, AmbiguousLibreNMSIdError("dup device pk=3, pk=4")],
                ),
            ],
        )
        assert result["can_import"] is False
        assert result["ambiguous_librenms_id"] is True
        # VM binding must be dropped — not surfaced as the existing object/match.
        assert result["existing_device"] is None
        assert result["existing_match_type"] != "librenms_id"

    def test_ambiguous_librenms_id_blocks_hostname_rebind(self):
        """When the librenms_id is ambiguous, the hostname/serial/IP fallback must NOT run and rebind existing_device — even when a NetBox device shares the hostname, the import has to stay fail-closed on the ambiguity rather than silently adopt a match."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError

        hostname_match = MagicMock()
        hostname_match.name = "router01"
        # A NetBox Device DOES exist with this hostname; the guard must ignore it.
        mock_device = MagicMock()
        mock_device.objects.filter.return_value.first.return_value = hostname_match
        mock_device.objects.filter.return_value.exclude.return_value.first.return_value = None

        result = self._run_validate(
            self._base_device(),
            patches_overrides=[
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id",
                    side_effect=AmbiguousLibreNMSIdError("dup host pk=1, pk=2"),
                ),
                patch("netbox_librenms_plugin.import_utils.device_operations.Device", mock_device),
            ],
        )
        assert result["ambiguous_librenms_id"] is True
        # Fail-closed: the hostname match must NOT be adopted as the existing device.
        assert result["existing_device"] is None
        assert result["existing_match_type"] == "ambiguous_librenms_id"

    def test_new_vm_without_cluster_is_not_ready(self):
        """New VM import with no cluster available must not be ready."""
        result = self._run_validate(self._base_device(hostname="vm01"), import_as_vm=True)
        assert result["is_ready"] is False
        assert result.get("import_as_vm") is True
        assert result["cluster"]["found"] is False

    def test_new_device_site_and_type_found_but_role_manual(self):
        """
        New device with site+type matched still requires manual role selection.

        validate_device_for_import always sets device_role["found"]=False for new
        devices and adds an issue. is_ready becomes True only AFTER the user selects
        a role via apply_role_to_validation + recalculate_validation_status.
        """
        from unittest.mock import MagicMock, patch

        site_mock = MagicMock()
        dt_mock = MagicMock()
        role_mock = MagicMock()
        role_mock.pk = 1

        result = self._run_validate(
            self._base_device(),
            patches_overrides=[
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
                    return_value={"found": True, "site": site_mock, "match_type": "exact", "suggestions": []},
                ),
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                    return_value={"matched": True, "device_type": dt_mock, "match_type": "exact"},
                ),
            ],
        )
        # Site and type found, but role is always manual for new devices
        assert result["site"]["found"] is True
        assert result["device_type"]["found"] is True
        assert result["device_role"]["found"] is False
        assert result["can_import"] is False  # blocked by role issue

        # Simulate user selecting a role via apply_role_to_validation
        from netbox_librenms_plugin.import_validation_helpers import (
            apply_role_to_validation,
            recalculate_validation_status,
        )

        apply_role_to_validation(result, role=role_mock, is_vm=False)
        recalculate_validation_status(result, is_vm=False)

        # After role selection, device should be ready
        assert result["device_role"]["found"] is True
        assert result["can_import"] is True
        assert result["is_ready"] is True

    def test_is_ready_false_when_site_missing_even_with_type_and_role(self):
        """is_ready requires ALL of site+type+role; missing site -> False."""
        from unittest.mock import MagicMock, patch

        dt_mock = MagicMock()
        role_mock = MagicMock()

        result = self._run_validate(
            self._base_device(),
            patches_overrides=[
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                    return_value={"matched": True, "device_type": dt_mock, "match_type": "exact"},
                ),
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.DeviceRole",
                    MagicMock(objects=MagicMock(all=MagicMock(return_value=[role_mock]))),
                ),
            ],
        )
        assert result["is_ready"] is False
        assert result["site"]["found"] is False

    def test_import_as_vm_skips_device_only_fields(self):
        """VM import path must not fail on device-only type fields."""
        from unittest.mock import MagicMock, patch

        dt_mock = MagicMock()
        result = self._run_validate(
            self._base_device(hostname="vm01"),
            import_as_vm=True,
            patches_overrides=[
                patch(
                    "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                    return_value={"matched": True, "device_type": dt_mock, "match_type": "exact"},
                ),
            ],
        )
        assert result.get("import_as_vm") is True
        assert result["is_ready"] is False  # no cluster found


class TestGetLibreNMSDeviceById:
    """Tests for get_librenms_device_by_id (lines 912-933)."""

    def test_success_returns_device(self):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api = MagicMock()
        device = {"device_id": 42, "hostname": "router01"}
        api.get_device_info.return_value = (True, device)

        result = get_librenms_device_by_id(api, 42)
        assert result is device

    def test_api_failure_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api = MagicMock()
        api.get_device_info.return_value = (False, None)

        result = get_librenms_device_by_id(api, 42)
        assert result is None

    def test_device_not_found_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api = MagicMock()
        api.get_device_info.return_value = (True, None)

        result = get_librenms_device_by_id(api, 42)
        assert result is None

    def test_exception_returns_none(self):
        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id

        api = MagicMock()
        api.get_device_info.side_effect = RuntimeError("Network error")

        result = get_librenms_device_by_id(api, 42)
        assert result is None


class TestFetchDeviceWithCache:
    """Tests for fetch_device_with_cache (lines 936-987)."""

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_from_pre_fetched_cache_dict(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        device = {"device_id": 1}
        cache_dict = {1: device}

        result = fetch_device_with_cache(1, api, libre_devices_cache=cache_dict)
        assert result is device
        mock_cache.get.assert_not_called()

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_from_django_cache(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        device = {"device_id": 1}
        mock_cache.get.return_value = device

        result = fetch_device_with_cache(1, api)
        assert result is device
        api.get_device_info.assert_not_called()

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_cache_miss_falls_back_to_api(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        device = {"device_id": 1}
        mock_cache.get.return_value = None
        api.get_device_info.return_value = (True, device)

        result = fetch_device_with_cache(1, api)
        assert result is device
        mock_cache.set.assert_called_once()

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_api_returns_none_returns_none(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        mock_cache.get.return_value = None
        api.get_device_info.return_value = (False, None)

        result = fetch_device_with_cache(1, api)
        assert result is None

    @patch("netbox_librenms_plugin.import_utils.device_operations.cache")
    def test_uses_provided_server_key(self, mock_cache):
        from netbox_librenms_plugin.import_utils.device_operations import fetch_device_with_cache

        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        mock_cache.get.return_value = None
        api.get_device_info.return_value = (True, {"device_id": 1})

        fetch_device_with_cache(1, api, server_key="secondary")
        # The cache key should use "secondary"
        cache_key = mock_cache.get.call_args[0][0]
        assert "secondary" in cache_key


@pytest.mark.django_db
class TestValidateDeviceForImport:
    """Main validation logic of validate_device_for_import, against real Device/VM rows."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.get_device_info.return_value = (True, {"device_id": 1})
        return api

    def _validate(self, libre_device, **kwargs):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        kwargs.setdefault("include_vc_detection", False)
        return validate_device_for_import(libre_device, api=self._make_api(), **kwargs)

    def test_minimal_device_validation(self):
        # A brand-new device (nothing in NetBox matches) still returns a well-formed result.
        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
            "type": "network",
        }
        result = self._validate(libre_device)

        assert result is not None
        assert "is_ready" in result
        assert result["existing_device"] is None

    def test_vm_import_uses_correct_model(self):
        """import_as_vm=True matches a VirtualMachine by hostname (uses the VM model, not Device)."""
        from netbox_librenms_plugin.tests.conftest import make_vm

        vm = make_vm("vm01")
        libre_device = {
            "device_id": 1,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
            "type": "network",
        }
        result = self._validate(libre_device, import_as_vm=True)

        assert result.get("import_as_vm") is True
        assert result["existing_device"].pk == vm.pk

    def test_existing_device_detected(self):
        """A device carrying the incoming librenms_id is found via find_by_librenms_id."""
        from netbox_librenms_plugin.tests.conftest import make_device

        dev = make_device("router01", librenms_cf={"default": 1})
        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        result = self._validate(libre_device)

        assert result["existing_device"].pk == dev.pk
        assert result["existing_match_type"] == "librenms_id"


@pytest.mark.django_db
class TestImportSingleDevice:
    """import_single_device against real NetBox rows."""

    def _make_libre_device(self):
        return {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "Cisco",
            "serial": "SN001",
            "os": "ios",
            "status": 1,
            "location": "-",
        }

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_missing_site_returns_error(self, MockAPI):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import _shared_infra

        MockAPI.return_value.server_key = "default"
        _, _, dtype, role = _shared_infra()
        validation = {
            "existing_device": None,
            "site": {"found": False, "site": None},
            "device_type": {"matched": True, "device_type": dtype},
            "device_role": {"found": True, "role": role},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }
        result = import_single_device(
            1, server_key="default", validation=validation, libre_device=self._make_libre_device()
        )
        assert result["success"] is False
        assert "Site" in result["error"]

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_missing_device_type_returns_error(self, MockAPI):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import _shared_infra

        MockAPI.return_value.server_key = "default"
        site, _, _, role = _shared_infra()
        validation = {
            "existing_device": None,
            "site": {"found": True, "site": site},
            "device_type": {"matched": False, "device_type": None},
            "device_role": {"found": True, "role": role},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }
        result = import_single_device(
            1, server_key="default", validation=validation, libre_device=self._make_libre_device()
        )
        assert result["success"] is False
        assert "device type" in result["error"].lower()

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_missing_device_role_returns_error(self, MockAPI):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import _shared_infra

        MockAPI.return_value.server_key = "default"
        site, _, dtype, _ = _shared_infra()
        validation = {
            "existing_device": None,
            "site": {"found": True, "site": site},
            "device_type": {"matched": True, "device_type": dtype},
            "device_role": {"found": False, "role": None},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }
        result = import_single_device(
            1, server_key="default", validation=validation, libre_device=self._make_libre_device()
        )
        assert result["success"] is False
        assert "role" in result["error"].lower()

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_creates_real_device_and_persists_link(self, MockAPI):
        """The success path creates a real Device (full_clean + save) with the resolved name, the matched FKs, the LibreNMS serial/status, and the librenms_id custom field."""
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import _shared_infra

        MockAPI.return_value.server_key = "default"
        site, _, dtype, role = _shared_infra()
        validation = {
            "existing_device": None,
            "resolved_name": "router01-created",
            "site": {"found": True, "site": site},
            "device_type": {"matched": True, "device_type": dtype},
            "device_role": {"found": True, "role": role},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }
        result = import_single_device(
            1, server_key="default", validation=validation, libre_device=self._make_libre_device()
        )

        assert result["success"] is True
        assert result["error"] is None
        dev = result["device"]
        # Reload from the DB to prove it really committed through full_clean + save.
        reloaded = Device.objects.get(pk=dev.pk)
        assert reloaded.name == "router01-created"
        assert reloaded.site_id == site.pk
        assert reloaded.device_type_id == dtype.pk
        assert reloaded.role_id == role.pk
        assert reloaded.serial == "SN001"
        assert reloaded.status == "active"  # libre status == 1
        assert reloaded.custom_field_data["librenms_id"]["default"] == 1

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_ambiguous_librenms_id_blocks_create_even_with_manual_mappings(self, MockAPI):
        """An ambiguous librenms_id (validate sets existing_device=None + ambiguous_librenms_id=True) must NOT create a device, even when manual_mappings supply site/type/role — the create path had only an existing_device guard, so a manual import could bypass the fail-closed state."""
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        MockAPI.return_value.server_key = "default"
        before = Device.objects.count()
        result = import_single_device(
            1,
            server_key="default",
            validation={"ambiguous_librenms_id": True, "existing_device": None},
            manual_mappings={"site_id": 1, "device_type_id": 1, "device_role_id": 1},
            libre_device=self._make_libre_device(),
        )
        assert result["success"] is False
        assert result["device"] is None
        assert "ambiguous" in result["error"].lower()
        assert Device.objects.count() == before

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_ambiguous_hostname_or_serial_blocks_create_even_with_manual_mappings(self, MockAPI):
        """Terminal hostname/serial ambiguity must block the create even when manual_mappings supply site/type/role."""
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import _shared_infra

        MockAPI.return_value.server_key = "default"
        site, _, dtype, role = _shared_infra()
        before = Device.objects.count()
        validation = {
            "existing_device": None,
            "existing_match_type": "ambiguous_hostname_or_serial",
            "can_import": False,
            "resolved_name": "dup-ambiguous-host",
            "site": {"found": True, "site": site},
            "device_type": {"matched": True, "device_type": dtype},
            "device_role": {"found": True, "role": role},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }
        result = import_single_device(
            1,
            server_key="default",
            validation=validation,
            manual_mappings={"site_id": site.pk, "device_type_id": dtype.pk, "device_role_id": role.pk},
            libre_device=self._make_libre_device(),
        )
        assert result["success"] is False
        assert result["device"] is None
        assert any(t in result["error"].lower() for t in ("hostname", "serial", "duplicate", "ambiguous"))
        # validate_device_for_import reuses this match type for a duplicate *management-IP*
        # collision too (device_operations.py ~1042), so the hard-block error must name the IP
        # path — otherwise it sends operators chasing a hostname/serial duplicate that isn't there.
        assert "management ip" in result["error"].lower()
        assert Device.objects.count() == before


@pytest.mark.django_db
class TestValidateDeviceForImportEdgeCases:
    """Edge cases for validate_device_for_import, against real Device/VM/IP rows."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.get_device_info.return_value = (True, {"device_id": 1})
        return api

    def _validate(self, libre_device, *, include_vc_detection=False, **kwargs):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        return validate_device_for_import(
            libre_device, api=self._make_api(), include_vc_detection=include_vc_detection, **kwargs
        )

    def _start_patches(self, extra_patches=None):
        mock_device = MagicMock()
        mock_device.objects.filter.return_value.first.return_value = None
        mock_device.objects.filter.return_value.exclude.return_value.first.return_value = None
        mock_device.objects.all.return_value = []

        mock_vm = MagicMock()
        mock_vm.objects.filter.return_value.first.return_value = None

        mock_cluster = MagicMock()
        mock_cluster.objects.all.return_value = []

        mock_role = MagicMock()
        mock_role.objects.all.return_value = []

        mock_ip = MagicMock()
        mock_ip.objects.filter.return_value.first.return_value = None

        mock_site = MagicMock()
        mock_site.objects.all.return_value = []

        patches = [
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
                return_value={"found": False, "site": None, "match_type": None, "suggestions": []},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                return_value={"matched": False, "device_type": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": [], "detection_error": None},
            ),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole", mock_role),
            patch("netbox_librenms_plugin.import_utils.device_operations.Cluster", mock_cluster),
            patch("netbox_librenms_plugin.import_utils.device_operations.Device", mock_device),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType", MagicMock()),
            patch("netbox_librenms_plugin.import_utils.device_operations.Site", mock_site),
            patch("netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.import_utils.device_operations.cache"),
            patch("virtualization.models.VirtualMachine", mock_vm),
            patch("ipam.models.IPAddress", mock_ip),
        ]
        if extra_patches:
            patches.extend(extra_patches)
        started = []
        for p in patches:
            started.append(p.start())
        return patches, started

    def _stop_patches(self, patches):
        for p in reversed(patches):
            p.stop()

    def test_duplicate_hostname_match_fails_closed(self):
        """When the hostname match is non-unique (duplicate device names), the earlier .first() existing_device is an arbitrary row."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        mfr, _ = Manufacturer.objects.get_or_create(name="ACME-853", slug="acme-853")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-853", slug="dt-853")
        role, _ = DeviceRole.objects.get_or_create(name="Role-853", slug="role-853")
        site_a, _ = Site.objects.get_or_create(name="Site-853a", slug="site-853a")
        site_b, _ = Site.objects.get_or_create(name="Site-853b", slug="site-853b")

        # Two devices share the hostname "dup-host" (different sites). One is LibreNMS-linked.
        Device.objects.create(
            name="dup-host",
            device_type=dt,
            role=role,
            site=site_a,
            status="active",
            serial="SER853",
            custom_field_data={"librenms_id": {"default": 7}},
        )
        Device.objects.create(
            name="dup-host",
            device_type=dt,
            role=role,
            site=site_b,
            status="active",
            serial="OTHER",
        )

        libre_device = {
            "device_id": 999,
            "hostname": "dup-host",
            "sysName": "dup-host",
            "serial": "SER853",
            "hardware": "Model-X",
            "os": "ios",
        }
        result = self._validate(libre_device)

        # Fail closed: no actionable serial/OOB state, not importable, blocking issue present.
        assert result["serial_action"] is None
        assert result.get("oob_candidate") is None
        assert result["can_import"] is False
        assert any("resolve the duplicate" in i for i in result["issues"])
        # The arbitrary .first() match must NOT keep a hostname/serial match_type: both the
        # device_status table (has_actions) and device_validation_details.html branch on it to
        # render a "Link to LibreNMS" action, which would link the wrong NetBox device.
        assert result["existing_match_type"] == "ambiguous_hostname_or_serial"
        # ...and the arbitrary .first() device + its match-derived linkage/name state must be
        # CLEARED, not just demoted. bulk_import's _refresh_existing_device short-circuits on a
        # set existing_device (skipping the ambiguity re-check) and exclude_existing / collision
        # handling keys off it, so a retained row pins the import to the wrong device.
        assert result["existing_device"] is None
        assert result["existing_librenms_link"] is None
        assert result["name_matches"] is False
        assert result["name_sync_available"] is False
        assert result["suggested_name"] is None
        assert result["serial_confirmed"] is False
        assert result["serial_duplicate"] is False

    def test_oob_ip_match_without_os_token_still_oob_candidate(self):
        """An incoming IP equal to device.oob_ip is still an OOB candidate when no os token classifies a type."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_ip

        dev = make_device("host-with-oob-ctrl")
        # Decoy row sharing the host address, created FIRST so .first() returns it — it is not
        # anyone's oob_ip, so the match must come from the full matching-IP set, not .first().
        make_ip("10.10.10.9/32")
        oob_ip = make_ip("10.10.10.9/32")  # bare OOB address, assigned to no interface
        dev.oob_ip = oob_ip
        dev.save()

        libre_device = {
            "device_id": 555,
            "hostname": "mgmt-controller-z",  # does NOT match dev.name → falls to the IP branch
            "sysName": "mgmt-controller-z",
            "serial": "-",
            "hardware": "-",
            "os": "-",  # no OOB-classifying token → normalize_oob_type() == ""
            "ip": "10.10.10.9",
        }
        result = self._validate(libre_device)

        assert result.get("existing_device").pk == dev.pk
        assert result["existing_match_type"] == "primary_ip"
        assert result["serial_action"] == "oob_candidate"
        assert result["oob_candidate"] is not None
        assert result["oob_candidate"]["type"] == "oob"  # generic fallback when no token

    def test_duplicate_hostname_without_serial_fails_closed(self):
        """A duplicate-hostname match with no usable serial must still fail closed (the check ran only inside the serial-gated merge block)."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        mfr, _ = Manufacturer.objects.get_or_create(name="ACME-113c", slug="acme-113c")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-113c", slug="dt-113c")
        role, _ = DeviceRole.objects.get_or_create(name="Role-113c", slug="role-113c")
        site_a, _ = Site.objects.get_or_create(name="Site-113ca", slug="site-113ca")
        site_b, _ = Site.objects.get_or_create(name="Site-113cb", slug="site-113cb")

        # Two devices share hostname "dup-noserial"; the incoming LibreNMS row carries no serial.
        Device.objects.create(
            name="dup-noserial",
            device_type=dt,
            role=role,
            site=site_a,
            status="active",
            custom_field_data={"librenms_id": {"default": 7}},
        )
        Device.objects.create(name="dup-noserial", device_type=dt, role=role, site=site_b, status="active")

        libre_device = {
            "device_id": 999,
            "hostname": "dup-noserial",
            "sysName": "dup-noserial",
            "serial": "-",  # no usable serial → merge block is skipped; check must still run
            "hardware": "Model-X",
            "os": "ios",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] is None
        assert result.get("oob_candidate") is None
        assert result["can_import"] is False
        assert any("resolve the duplicate" in i for i in result["issues"])
        assert result["existing_match_type"] == "ambiguous_hostname_or_serial"

    def test_duplicate_hostname_with_primary_ip_match_stays_terminal(self):
        """A duplicate-hostname row whose management IP resolves to a SINGLE device must stay
        terminal.

        The hostname/serial ambiguity block clears existing_device and demotes match_type to
        ``ambiguous_hostname_or_serial`` but (before the fix) did not return, so the later
        primary-IP fallback pass re-bound existing_device and demoted match_type to
        ``primary_ip`` — silently re-homing a duplicate-hostname row onto an arbitrary
        IP-matched device and dropping the terminal blocker.
        """
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        from netbox_librenms_plugin.tests.conftest import ip_on

        mfr, _ = Manufacturer.objects.get_or_create(name="ACME-clob", slug="acme-clob")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-clob", slug="dt-clob")
        role, _ = DeviceRole.objects.get_or_create(name="Role-clob", slug="role-clob")
        site_a, _ = Site.objects.get_or_create(name="Site-cloba", slug="site-cloba")
        site_b, _ = Site.objects.get_or_create(name="Site-clobb", slug="site-clobb")

        # Two devices share hostname "dup-clob" (terminal ambiguity). The FIRST also owns the
        # incoming management IP on an interface, so the primary-IP fallback pass resolves to a
        # single device — the state that previously clobbered the terminal ambiguity.
        dev1 = Device.objects.create(
            name="dup-clob",
            device_type=dt,
            role=role,
            site=site_a,
            status="active",
            custom_field_data={"librenms_id": {"default": 7}},
        )
        Device.objects.create(name="dup-clob", device_type=dt, role=role, site=site_b, status="active")
        ip_on(dev1, "192.168.77.1/24", "eth0")

        libre_device = {
            "device_id": 999,
            "hostname": "dup-clob",
            "sysName": "dup-clob",
            "serial": "-",  # hostname-only match → _match_type 'hostname', 2 peers → terminal
            "hardware": "Model-X",
            "os": "ios",
            "ip": "192.168.77.1",  # owned by dev1's interface → single primary-IP match
        }
        result = self._validate(libre_device)

        # The terminal hostname/serial ambiguity must survive the primary-IP fallback pass:
        # neither the match_type nor the cleared existing_device may be overwritten.
        assert result["existing_match_type"] == "ambiguous_hostname_or_serial"
        assert result["existing_device"] is None
        assert result["can_import"] is False
        assert any("resolve the duplicate" in i for i in result["issues"])

    def test_vm_librenms_id_not_int_falls_back(self):
        """device_id None → no librenms_id lookup; validation still returns cleanly."""
        libre_device = {
            "device_id": None,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        result = self._validate(libre_device, import_as_vm=True)

        assert result is not None
        assert result["existing_device"] is None

    def test_vm_with_legacy_librenms_id_flags_migration(self):
        """An existing VM with a legacy bare-int librenms_id is found and flagged for migration."""
        from netbox_librenms_plugin.tests.conftest import make_vm

        vm = make_vm("vm01")
        vm.custom_field_data["librenms_id"] = 42  # legacy bare int (pre multi-server)
        vm.save()
        libre_device = {
            "device_id": 42,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        result = self._validate(libre_device, import_as_vm=True)

        assert result.get("existing_device").pk == vm.pk
        assert result.get("librenms_id_needs_migration") is True

    def test_vm_whitespace_padded_legacy_id_flags_migration(self):
        """A whitespace-padded legacy id (' 42 ') is detected via the shared int-coercion helper, not the stricter isdigit()."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 42,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        api = self._make_api()

        existing_vm = MagicMock()
        existing_vm.name = "vm01"
        existing_vm.serial = ""
        existing_vm.custom_field_data = {"librenms_id": " 42 "}  # legacy, whitespace-padded → isdigit() is False

        patches, _ = self._start_patches()
        try:
            with patch("netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id") as mock_find:
                mock_find.side_effect = [existing_vm, None]  # VM found, then no Device
                result = validate_device_for_import(libre_device, import_as_vm=True, api=api)
        finally:
            self._stop_patches(patches)

        assert result.get("librenms_id_needs_migration") is True

    def test_device_whitespace_padded_legacy_id_flags_migration(self):
        """The Device branch also uses the shared helper, so a padded legacy id flags migration consistently with the VM branch."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 42,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        api = self._make_api()

        existing_device = MagicMock()
        existing_device.name = "sw01"
        existing_device.serial = ""
        existing_device.virtual_chassis = None
        existing_device.custom_field_data = {"librenms_id": " 42 "}  # legacy, whitespace-padded

        patches, _ = self._start_patches()
        try:
            with patch("netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id") as mock_find:
                mock_find.side_effect = [None, existing_device]  # no VM, then Device found
                result = validate_device_for_import(libre_device, api=api)
        finally:
            self._stop_patches(patches)

        assert result.get("librenms_id_needs_migration") is True

    def test_vc_detection_called_for_device_with_api(self):
        """VC detection runs when include_vc_detection=True and an API is supplied."""
        from unittest.mock import patch

        libre_device = {
            "device_id": 1,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "Cisco",
            "serial": "SN001",
            "os": "ios",
            "location": "-",
        }
        vc_data = {"is_stack": True, "member_count": 2, "members": [{"serial": "SN001"}, {"serial": "SN002"}]}
        # get_virtual_chassis_data / update_vc_member_suggested_names call the LibreNMS API
        # (external boundary) — mock just those, run the rest against the real (empty) DB.
        with (
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data", return_value=vc_data
            ) as mock_get_vc,
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.update_vc_member_suggested_names",
                return_value=vc_data,
            ) as mock_update_vc,
        ):
            result = self._validate(libre_device, include_vc_detection=True)

        assert result["virtual_chassis"] is not None
        assert result["virtual_chassis"]["is_stack"] is True
        assert result["virtual_chassis"]["member_count"] == 2
        mock_get_vc.assert_called_once()
        mock_update_vc.assert_called_once()

    def test_no_vc_detection_when_disabled(self):
        """VC detection is skipped when include_vc_detection=False."""
        from unittest.mock import patch

        libre_device = {
            "device_id": 1,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        with patch("netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data") as mock_vc:
            self._validate(libre_device, include_vc_detection=False)
            mock_vc.assert_not_called()

    def test_chassis_inventory_fallback_used(self):
        """_try_chassis_device_type_match falls back to the model-name field on a miss."""
        from netbox_librenms_plugin.import_utils.device_operations import _try_chassis_device_type_match

        api = MagicMock()
        mock_dt = MagicMock()
        # api.get_inventory_filtered is the LibreNMS API boundary; match_librenms_hardware is the
        # control-flow seam this unit test pins (miss on name, hit on model name).
        api.get_inventory_filtered.return_value = (
            True,
            [{"entPhysicalName": "MX480", "entPhysicalModelName": "Juniper MX480"}],
        )
        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type"
        ) as mock_match:
            mock_match.side_effect = [
                {"matched": False},
                {"matched": True, "device_type": mock_dt, "match_type": "exact"},
            ]
            result = _try_chassis_device_type_match(api, 1)

        assert result is not None
        assert mock_match.call_count == 2
        assert result["matched"] is True
        assert result.get("device_type") is mock_dt

    def test_primary_ip_match_check(self):
        """A device whose interface owns the incoming IP is matched as existing (primary_ip)."""
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device

        dev = make_device("existing_router")
        ip_on(dev, "192.168.1.1/24", "eth0")
        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
            "ip": "192.168.1.1",
        }
        result = self._validate(libre_device)

        assert result.get("existing_device").pk == dev.pk
        assert result.get("existing_match_type") == "primary_ip"

    def test_primary_ip_match_with_decoy_duplicate_net_host_row(self):
        """The interface-assigned device must be found by scanning EVERY duplicate net_host row, not just .first(): a decoy unassigned row created first must not hide the real assigned device (mirrors the bulk_import.py fix)."""
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device, make_ip

        dev = make_device("decoy-host-router")
        # Decoy row sharing the host address, created FIRST and assigned to nothing — so .first()
        # returns it and the match must come from scanning the whole matching-IP set.
        make_ip("192.168.5.1/32")
        ip_on(dev, "192.168.5.1/24", "eth0")  # the REAL management IP, on an interface

        libre_device = {
            "device_id": 77,
            "hostname": "router-decoy",  # does not match dev.name → falls to the IP branch
            "sysName": "router-decoy",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
            "ip": "192.168.5.1",
        }
        result = self._validate(libre_device)

        assert result.get("existing_device") is not None
        assert result["existing_device"].pk == dev.pk
        assert result.get("existing_match_type") == "primary_ip"

    def test_primary_ip_ambiguity_across_devices_fails_closed(self):
        """When duplicate net_host rows resolve to MORE THAN ONE distinct device, validation must fail closed (blocking issue + can_import False) rather than bind to an arbitrary one."""
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device

        dev_a = make_device("amb-host-a")
        dev_b = make_device("amb-host-b")
        ip_on(dev_a, "192.168.9.1/24", "eth0")
        ip_on(dev_b, "192.168.9.1/24", "eth0")  # same host address on a DIFFERENT device

        libre_device = {
            "device_id": 88,
            "hostname": "amb-router",  # matches neither device name → IP branch
            "sysName": "amb-router",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
            "ip": "192.168.9.1",
        }
        result = self._validate(libre_device)

        # The collision message carries the shared "serial or management IP" marker (so the
        # bulk-import refresh cleanup can strip it once resolved) while still naming the IP, and
        # the row enters the terminal ambiguity match_type the cleanup keys on.
        assert any("serial or management IP" in i and "192.168.9.1" in i for i in result.get("issues", []))
        assert result.get("existing_match_type") == "ambiguous_hostname_or_serial"
        assert result.get("can_import") is False
        # Must NOT have arbitrarily bound to either device.
        assert result.get("existing_device") is None

    def test_cached_primary_ip_collision_cleared_on_refresh_after_resolution(self):
        """Refresh clears a cached primary-IP collision blocker once the duplicate IP is resolved."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device

        dev_a = make_device("clr-host-a")
        dev_b = make_device("clr-host-b")
        ip_on(dev_a, "192.168.9.7/24", "eth0")
        ip_b = ip_on(dev_b, "192.168.9.7/24", "eth0")  # duplicate host address on a 2nd device

        libre_device = {
            "device_id": 91,
            "hostname": "clr-router",  # matches neither device name → IP branch
            "sysName": "clr-router",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
            "ip": "192.168.9.7",
        }
        validation = self._validate(libre_device)
        # The collision blocks the row with exactly the state the refresh cleanup keys on.
        assert validation.get("existing_match_type") == "ambiguous_hostname_or_serial"
        assert any("serial or management IP" in i for i in validation.get("issues", []))

        # Resolve the duplicate: dev_b no longer carries the shared host address.
        ip_b.delete()

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        # The stale collision blocker is purged (not left to block the row until cache expiry), and
        # the row rebinds to the single remaining device.
        assert not any("Multiple NetBox devices" in i and "IP address" in i for i in validation.get("issues", []))
        assert not any("serial or management IP" in i for i in validation.get("issues", []))
        assert validation.get("existing_match_type") == "primary_ip"
        assert validation.get("existing_device") is not None and validation["existing_device"].pk == dev_a.pk

    def test_refresh_serial_fallback_accepts_a_numeric_serial(self):
        """The refresh serial fallback must coerce an int serial before stripping it, or the whole refresh raises AttributeError instead of rebinding the row."""
        from netbox_librenms_plugin.import_utils.bulk_import import _refresh_existing_device
        from netbox_librenms_plugin.tests.conftest import make_device

        dev = make_device("refresh-numeric-serial", serial="555777")
        libre_device = {
            "device_id": 92,
            "hostname": "refresh-numeric-row",  # matches no device name → falls to the serial fallback
            "sysName": "refresh-numeric-row",
            "hardware": "-",
            "serial": 555777,  # int, not str
            "os": "-",
            "location": "-",
        }
        validation = {"existing_device": None, "issues": [], "warnings": []}

        _refresh_existing_device(validation, libre_device=libre_device, server_key="default")

        assert validation.get("existing_match_type") == "serial"
        assert validation.get("existing_device") is not None and validation["existing_device"].pk == dev.pk

    def test_no_hostname_adds_issue(self):
        """Empty hostname/sysName → _determine_device_name falls back to 'device-{id}'."""
        libre_device = {
            "device_id": 1,
            "hostname": "",
            "sysName": "",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "-",
        }
        result = self._validate(libre_device)

        assert isinstance(result, dict)
        assert "Device has no hostname" not in result.get("issues", [])
        assert result.get("resolved_name", "").startswith("device-")


@pytest.mark.django_db
class TestValidateDeviceMoreEdgeCases:
    """More edge cases for validate_device_for_import, against real rows."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.get_device_info.return_value = (True, {"device_id": 1})
        return api

    def _validate(self, libre_device, *, include_vc_detection=False, **kwargs):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        return validate_device_for_import(
            libre_device, api=self._make_api(), include_vc_detection=include_vc_detection, **kwargs
        )

    def test_serial_dash_normalized(self):
        """serial '-' is treated as empty — no serial mismatch flagged on a librenms_id match."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("router01", serial="SN001", librenms_cf={"default": 1})
        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "-",  # dash → normalized to empty
            "os": "-",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result is not None
        assert result.get("serial_action") is None

    def test_serial_conflict_with_existing_device(self):
        """Incoming serial differs from the linked device AND is already owned by another → conflict."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("router01", serial="OLD_SN", librenms_cf={"default": 1})
        make_device("router02", serial="NEW_SN")  # already owns the incoming serial
        libre_device = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "hardware": "-",
            "serial": "NEW_SN",
            "os": "-",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result.get("serial_action") == "conflict"

    def test_both_vm_and_device_with_same_hostname(self):
        """A VM and a Device share the hostname → ambiguous, warned and not bound."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_vm

        make_vm("server01")
        make_device("server01")
        libre_device = {
            "device_id": 1,
            "hostname": "server01",
            "sysName": "server01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result is not None
        assert any("VM" in w and "Device" in w for w in result.get("warnings", []))

    def test_existing_vm_by_hostname(self):
        """A VM matched by hostname (no Device match) sets existing_device + hostname match type."""
        from netbox_librenms_plugin.tests.conftest import make_vm

        vm = make_vm("vm01")
        libre_device = {
            "device_id": 1,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result.get("existing_device").pk == vm.pk
        assert result.get("existing_match_type") == "hostname"

    def test_vc_detection_exception_handled(self):
        """A VC-detection exception is caught and surfaced as virtual_chassis.detection_error."""
        from unittest.mock import patch

        libre_device = {
            "device_id": 1,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "",
        }
        with patch(
            "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
            side_effect=Exception("VC error"),
        ):
            result = self._validate(libre_device, include_vc_detection=True)

        assert result is not None
        assert "detection_error" in result.get("virtual_chassis", {})


@pytest.mark.django_db
class TestImportSingleDeviceEdgeCases:
    """import_single_device edge cases against real rows."""

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_no_libre_device_api_failure(self, MockAPI):
        """libre_device=None and the API reports failure → error dict (no device created)."""
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        MockAPI.return_value.server_key = "default"
        MockAPI.return_value.get_device_info.return_value = (False, None)  # the HTTP boundary

        result = import_single_device(device_id=1, libre_device=None, server_key="default")
        assert result["success"] is False
        assert "Failed to retrieve device" in result.get("error", "")

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_manual_mappings_are_applied(self, MockAPI):
        """manual_mappings resolve real Site/DeviceType/DeviceRole rows and the created device is persisted with those FKs (validation supplies none, so the manual ids must win)."""
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import _shared_infra

        MockAPI.return_value.server_key = "default"
        site, _, dtype, role = _shared_infra()
        libre_device = {
            "device_id": 1,
            "hostname": "router01-mm",
            "sysName": "router01-mm",
            "hardware": "Cisco",
            "serial": "SN001",
            "os": "ios",
            "status": 1,
            "location": "",
        }
        validation = {
            "existing_device": None,
            "resolved_name": "router01-mm",
            "site": {"found": True, "site": None},
            "device_type": {"found": True, "device_type": None},
            "device_role": {"found": False, "role": None},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }
        manual_mappings = {"site_id": site.pk, "device_type_id": dtype.pk, "device_role_id": role.pk}

        result = import_single_device(
            device_id=1,
            libre_device=libre_device,
            validation=validation,
            manual_mappings=manual_mappings,
            server_key="default",
        )

        assert result["success"] is True
        reloaded = Device.objects.get(pk=result["device"].pk)
        assert reloaded.site_id == site.pk
        assert reloaded.device_type_id == dtype.pk
        assert reloaded.role_id == role.pk


@pytest.mark.django_db
class TestImportSingleDeviceMoreEdgeCases:
    """import_single_device manual platform/rack mappings against real rows."""

    def _libre(self, name):
        return {
            "device_id": 1,
            "hostname": name,
            "sysName": name,
            "serial": "-",
            "hardware": "-",
            "os": "-",
            "location": "",
            "status": 1,
        }

    def _validation(self, name, site, dtype, role):
        return {
            "existing_device": None,
            "resolved_name": name,
            "site": {"found": True, "site": site},
            "device_type": {"found": True, "device_type": dtype},
            "device_role": {"found": True, "role": role},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_platform_manual_mapping(self, MockAPI):
        """manual_mappings platform_id resolves a real Platform and is persisted on the device."""
        from dcim.models import Device, Platform

        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import _shared_infra

        MockAPI.return_value.server_key = "default"
        site, _, dtype, role = _shared_infra()
        platform = Platform.objects.create(name="TestPlat", slug="test-plat")

        result = import_single_device(
            device_id=1,
            libre_device=self._libre("r01-plat"),
            validation=self._validation("r01-plat", site, dtype, role),
            manual_mappings={"platform_id": platform.pk},
            server_key="default",
        )

        assert result["success"] is True
        assert Device.objects.get(pk=result["device"].pk).platform_id == platform.pk

    @patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI")
    def test_rack_manual_mapping(self, MockAPI):
        """manual_mappings rack_id resolves a real Rack (in the device's site) and is persisted."""
        from dcim.models import Device, Rack

        from netbox_librenms_plugin.import_utils.device_operations import import_single_device
        from netbox_librenms_plugin.tests.conftest import _shared_infra

        MockAPI.return_value.server_key = "default"
        site, _, dtype, role = _shared_infra()
        rack = Rack.objects.create(name="R1-mm", site=site)

        result = import_single_device(
            device_id=1,
            libre_device=self._libre("r01-rack"),
            validation=self._validation("r01-rack", site, dtype, role),
            manual_mappings={"rack_id": rack.pk},
            server_key="default",
        )

        assert result["success"] is True
        assert Device.objects.get(pk=result["device"].pk).rack_id == rack.pk


class TestValidateDeviceExistingVMGuard:
    """Test that existing VMs skip device-specific validations (g06 fix)."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        return api

    def test_existing_vm_skips_device_validations(self):
        """
        When import_as_vm=True and existing_device is set, site/device_type/device_role
        are marked found=True without running device-specific validation logic."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        existing_vm = MagicMock()
        existing_vm.name = "vm01"
        existing_vm.custom_field_data = {}

        libre_device = {
            "device_id": 1,
            "hostname": "vm01",
            "sysName": "vm01",
            "hardware": "-",
            "serial": "-",
            "os": "-",
            "location": "unknown-location",
        }
        api = self._make_api()

        mock_vm_model = MagicMock()
        mock_vm_model.objects.filter.return_value.first.return_value = existing_vm

        mock_device_model = MagicMock()
        mock_device_model.objects.filter.return_value.first.return_value = None
        mock_device_model.objects.filter.return_value.exclude.return_value.first.return_value = None
        mock_device_model.objects.all.return_value = []

        with (
            patch("netbox_librenms_plugin.import_utils.device_operations.Site"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole"),
            patch("netbox_librenms_plugin.import_utils.device_operations.Device", new=mock_device_model),
            patch("netbox_librenms_plugin.import_utils.device_operations.Cluster"),
            patch("netbox_librenms_plugin.import_utils.device_operations.cache"),
            patch("virtualization.models.VirtualMachine", new=mock_vm_model),
            patch("ipam.models.IPAddress"),
            patch("netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id", return_value=None),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type"
            ) as mock_match,
            patch("netbox_librenms_plugin.import_utils.device_operations.find_matching_site") as mock_site,
        ):
            result = validate_device_for_import(libre_device, import_as_vm=True, api=api)

        # find_matching_site and match_librenms_hardware_to_device_type should NOT be called for VMs
        mock_site.assert_not_called()
        mock_match.assert_not_called()
        # Device-specific fields are marked found=True for all VMs
        assert result["site"]["found"] is True
        assert result["device_type"]["found"] is True
        assert result["device_role"]["found"] is True
        # No cluster-required error for existing VMs
        assert not any("Cluster must be" in i for i in result.get("issues", []))


class TestValidateDeviceChassisMatch:
    """Test chassis match path (line 539) in validate_device_for_import."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        return api

    def test_chassis_match_overrides_hardware_match(self):
        """Line 539: chassis_match succeeds → dt_match = chassis_match."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        libre_device = {
            "device_id": 1,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "Cisco Catalyst 9300",
            "serial": "SN001",
            "os": "ios",
            "location": "",
        }
        api = self._make_api()

        chassis_dt = MagicMock()
        chassis_dt.model = "Catalyst 9300"
        chassis_match = {"matched": True, "device_type": chassis_dt, "match_type": "chassis_inventory"}

        vm_no_match = MagicMock()
        vm_no_match.objects.filter.return_value.first.return_value = None  # no hostname collision

        device_patch = patch("netbox_librenms_plugin.import_utils.device_operations.Device")
        mock_device_cls = device_patch.start()
        mock_device_cls.objects.filter.return_value.first.return_value = None
        mock_device_cls.objects.filter.return_value.exclude.return_value.first.return_value = None

        patches = [
            patch("netbox_librenms_plugin.import_utils.device_operations.Site"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceType"),
            patch("netbox_librenms_plugin.import_utils.device_operations.DeviceRole"),
            patch("netbox_librenms_plugin.import_utils.device_operations.cache"),
            patch("virtualization.models.VirtualMachine", new=vm_no_match),
            patch("ipam.models.IPAddress"),
            patch("netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id", return_value=None),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                return_value={"matched": False},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations._try_chassis_device_type_match",
                return_value=chassis_match,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": None},
            ),
        ]

        for p in patches:
            p.start()

        try:
            result = validate_device_for_import(libre_device, api=api)
        finally:
            for p in patches:
                p.stop()
            device_patch.stop()

        assert result["device_type"].get("device_type") is chassis_dt


@pytest.mark.django_db
class TestValidateForcesDeviceModeRealDB:
    """A Device match must force import_as_vm=False even when VM mode was selected (real DB)."""

    def _api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.get_device_info.return_value = (True, {"device_id": 50})
        return api

    def _make_device(self, name, librenms_cf=None):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        mfr, _ = Manufacturer.objects.get_or_create(name="ACME-114d", slug="acme-114d")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-114d", slug="dt-114d")
        role, _ = DeviceRole.objects.get_or_create(name="Role-114d", slug="role-114d")
        site, _ = Site.objects.get_or_create(name="Site-114d", slug="site-114d")
        cf = {"librenms_id": librenms_cf} if librenms_cf else {}
        return Device.objects.create(
            name=name, device_type=dt, role=role, site=site, status="active", custom_field_data=cf
        )

    def test_librenms_id_device_match_forces_device_mode(self):
        """A librenms_id Device match flips a user-selected VM mode back to Device mode."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        device = self._make_device("force-dev-mode", librenms_cf={"default": 50})
        libre_device = {
            "device_id": 50,
            "hostname": "force-dev-mode",
            "sysName": "force-dev-mode",
            "serial": "-",
            "hardware": "-",
            "os": "-",
            "location": "-",
        }

        result = validate_device_for_import(
            libre_device, import_as_vm=True, api=self._api(), include_vc_detection=False
        )

        assert result["existing_match_type"] == "librenms_id"
        assert result["existing_device"].pk == device.pk
        assert result["import_as_vm"] is False

    def test_hostname_device_match_forces_device_mode(self):
        """A hostname Device match (no librenms_id) also flips selected VM mode back to Device mode."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        device = self._make_device("force-dev-host")
        libre_device = {
            "device_id": 777,
            "hostname": "force-dev-host",
            "sysName": "force-dev-host",
            "serial": "-",
            "hardware": "-",
            "os": "-",
            "location": "-",
        }

        result = validate_device_for_import(
            libre_device, import_as_vm=True, api=self._api(), include_vc_detection=False
        )

        assert result["existing_match_type"] == "hostname"
        assert result["existing_device"].pk == device.pk
        assert result["import_as_vm"] is False


@pytest.mark.django_db
class TestValidateSerialMatchStripsWhitespace:
    """A whitespace-padded incoming serial (common from SNMP) must still match an existing device by serial, so import doesn't mint a duplicate (real DB)."""

    def _make_device(self, name, serial):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        mfr, _ = Manufacturer.objects.get_or_create(name="ACME-serialws", slug="acme-serialws")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-serialws", slug="dt-serialws")
        role, _ = DeviceRole.objects.get_or_create(name="Role-serialws", slug="role-serialws")
        site, _ = Site.objects.get_or_create(name="Site-serialws", slug="site-serialws")
        return Device.objects.create(name=name, device_type=dt, role=role, site=site, status="active", serial=serial)

    def test_whitespace_padded_incoming_serial_matches_existing(self):
        """An incoming serial with surrounding whitespace resolves to the existing device, not a new duplicate."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        device = self._make_device("serial-ws-existing", serial="SN-WS-4242")
        libre_device = {
            "device_id": 7777,
            # Hostname matches no device, so the identity search falls through to the serial match.
            "hostname": "serial-ws-importrow",
            "sysName": "serial-ws-importrow",
            "serial": " SN-WS-4242 ",  # SNMP-style whitespace padding around the real serial
            "hardware": "-",
            "os": "-",
            "location": "-",
        }

        result = validate_device_for_import(libre_device, api=None, server_key="default", include_vc_detection=False)

        assert result["existing_device"] is not None, "whitespace-padded serial was not matched (duplicate risk)"
        assert result["existing_device"].pk == device.pk

    def test_whitespace_only_serial_difference_is_not_a_drift_conflict(self):
        """A hostname-matched device whose stored serial equals the incoming serial modulo whitespace must not be reported as a serial difference/conflict (the drift check compares the trimmed value)."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        device = self._make_device("serial-drift-host", serial="SN-DRIFT-7")
        libre_device = {
            "device_id": 8812,
            "hostname": "serial-drift-host",  # hostname-matches the existing device
            "sysName": "serial-drift-host",
            "serial": " SN-DRIFT-7 ",  # same serial, only SNMP whitespace differs
            "hardware": "-",
            "os": "-",
            "location": "-",
        }

        result = validate_device_for_import(libre_device, api=None, server_key="default", include_vc_detection=False)

        assert result["existing_device"].pk == device.pk
        assert result.get("serial_action") is None, "whitespace-only serial diff wrongly flagged as drift"
        assert not any("differs" in w for w in result.get("warnings", [])), result.get("warnings")

    def test_numeric_serial_is_coerced_not_crashed(self):
        """A numeric serial (an int, e.g. an all-digit serial parsed from JSON) must be coerced to a string before .strip(), not raise AttributeError, and still match an existing device stored with the string form."""
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        device = self._make_device("numeric-serial-host", serial="123456")
        libre_device = {
            "device_id": 8814,
            "hostname": "numeric-import-row",  # matches no device → falls to the serial-identity match
            "sysName": "numeric-import-row",
            "serial": 123456,  # int, not str — .strip() would raise AttributeError without a str() cast
            "hardware": "-",
            "os": "-",
            "location": "-",
        }

        result = validate_device_for_import(libre_device, api=None, server_key="default", include_vc_detection=False)

        assert result["existing_device"] is not None, "numeric serial was not matched (crash swallowed by validator)"
        assert result["existing_device"].pk == device.pk
        # The later duplicate/merge stages read the serial again; an uncast read raises AttributeError
        # there and validate_device_for_import swallows it into a generic "Validation error" issue.
        assert not any("Validation error" in i for i in result.get("issues", [])), result.get("issues")


@pytest.mark.django_db
class TestImportPersistsTrimmedSerial:
    """import_single_device must persist a whitespace-trimmed serial, so the next import's trimmed filter(serial=...) matches it instead of minting a duplicate (real DB, real Device.save())."""

    def test_padded_incoming_serial_is_stored_trimmed(self):
        from unittest.mock import patch

        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        mfr, _ = Manufacturer.objects.get_or_create(name="ACME-trimp", slug="acme-trimp")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-trimp", slug="dt-trimp")
        role, _ = DeviceRole.objects.get_or_create(name="Role-trimp", slug="role-trimp")
        site, _ = Site.objects.get_or_create(name="Site-trimp", slug="site-trimp")

        libre_device = {
            "device_id": 8813,
            "hostname": "trim-persist-host",
            "sysName": "trim-persist-host",
            "hardware": "-",
            "serial": "  SN-PERSIST-9  ",  # SNMP whitespace padding around the real serial
            "os": "-",
            "status": 1,
            "location": "-",
        }
        validation = {
            "existing_device": None,
            "resolved_name": "trim-persist-host",
            "site": {"found": True, "site": site},
            "device_type": {"matched": True, "device_type": dt},
            "device_role": {"found": True, "role": role},
            "platform": {"found": False, "platform": None},
            "rack": {"rack": None},
        }

        # Patch only set_librenms_device_id: it writes the librenms_id custom field, which isn't
        # registered in the isolated test DB and would fail Device.full_clean() — orthogonal to the
        # serial persistence under test. The real Device is created, full_clean'd, and saved.
        with (
            patch("netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI"),
            patch("netbox_librenms_plugin.import_utils.device_operations.set_librenms_device_id"),
        ):
            result = import_single_device(
                8813,
                server_key="default",
                validation=validation,
                libre_device=libre_device,
                sync_options={"sync_interfaces": False},
            )

        assert result["success"] is True, result.get("error")
        device = Device.objects.get(name="trim-persist-host")
        # Stored TRIMMED → a later filter(serial="SN-PERSIST-9") finds it, so no duplicate is minted.
        assert device.serial == "SN-PERSIST-9"


@pytest.mark.django_db
class TestImportFallbackReadsLive:
    """Import fallbacks read LibreNMS live (use_cache=False) rather than the 60s get_device_info snapshot."""

    def _seed_stale(self, server_key="default", device_id=4242):
        from django.core.cache import cache

        cache.set(
            f"librenms_device_info_{server_key}_{device_id}",
            (True, {"hostname": "STALE-HOST", "device_id": device_id}),
            60,
        )

    def test_get_librenms_device_by_id_bypasses_stale_cache(self):
        """use_cache=False skips the seeded snapshot and does a (here-failing) live fetch."""
        from django.test import override_settings
        from requests.exceptions import ConnectionError as RequestsConnectionError

        from netbox_librenms_plugin.import_utils.device_operations import get_librenms_device_by_id
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        cfg = {"netbox_librenms_plugin": {"servers": {"default": {"librenms_url": "http://d", "api_token": "t"}}}}
        # Mock the live HTTP boundary instead of relying on a real request to "http://d" failing:
        # a real call is non-deterministic and slow (DNS/timeout, and behind a proxy it can 503 or
        # hang rather than refuse). RequestsConnectionError subclasses requests.RequestException, so
        # get_device_info's `except requests.exceptions.RequestException` catches it → (False, ...).
        with (
            override_settings(PLUGINS_CONFIG=cfg),
            patch(
                "netbox_librenms_plugin.librenms_api.requests.get",
                side_effect=RequestsConnectionError("offline"),
            ),
        ):
            self._seed_stale()
            api = LibreNMSAPI(server_key="default")
            # Positive control: the cache IS populated and use_cache=True returns the stale snapshot.
            assert get_librenms_device_by_id(api, 4242, use_cache=True)["hostname"] == "STALE-HOST"
            # The fix: the import fallback reads live, so it does NOT serve the stale snapshot; the
            # live HTTP call fails in-test (mocked offline) -> None, proving the cache was bypassed.
            assert get_librenms_device_by_id(api, 4242, use_cache=False) is None

    def test_import_single_device_does_not_build_from_stale_snapshot(self):
        """import_single_device's None-branch fallback reads live, so a stale snapshot can't seed a device."""
        from django.test import override_settings
        from requests.exceptions import ConnectionError as RequestsConnectionError

        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        cfg = {"netbox_librenms_plugin": {"servers": {"default": {"librenms_url": "http://d", "api_token": "t"}}}}
        # Mock the live HTTP boundary (see the sibling test): deterministic offline failure instead of
        # depending on a real request to "http://d" refusing.
        with (
            override_settings(PLUGINS_CONFIG=cfg),
            patch(
                "netbox_librenms_plugin.librenms_api.requests.get",
                side_effect=RequestsConnectionError("offline"),
            ),
        ):
            self._seed_stale()
            result = import_single_device(4242, server_key="default", libre_device=None)

        assert result["success"] is False
        assert "retrieve" in (result.get("error") or "").lower()


class TestDetectOOBTypeFromName:
    """_detect_oob_type_from_name must use the same normalization as normalize_oob_type so a vendor-specific token wins over the generic "oob", even when "oob" appears earlier in the name."""

    def _detect(self, name):
        from netbox_librenms_plugin.import_utils.device_operations import _detect_oob_type_from_name

        return _detect_oob_type_from_name(name)

    def test_vendor_token_after_generic_oob_is_preserved(self):
        # "oob" appears before "idrac9"; the vendor-specific token must still win.
        assert self._detect("leaf01-oob-idrac9") == "idrac"

    def test_generic_oob_only_returns_oob(self):
        assert self._detect("switch-oob") == "oob"

    def test_vendor_token_alone(self):
        assert self._detect("ilo-mgmt-01") == "ilo"

    def test_no_oob_token_returns_none(self):
        assert self._detect("core-switch-01") is None

    def test_empty_name_returns_none(self):
        assert self._detect("") is None
        assert self._detect(None) is None


@pytest.mark.django_db
class TestOOBDetection:
    """OOB-candidate / promote-to-host / merge detection in validate_device_for_import."""

    def _make_api(self, server_key="default"):
        api = MagicMock()
        api.server_key = server_key
        api.cache_timeout = 300
        return api

    def _validate(self, libre_device, *, server_key="default"):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        # include_vc_detection=False keeps this focused on the OOB/serial role logic without
        # needing to mock the VC API surface (the role decision is independent of VC data).
        return validate_device_for_import(
            libre_device, api=self._make_api(server_key), server_key=server_key, include_vc_detection=False
        )

    # ------------------------------------------------------------------
    # Case 1: Serial match + OOB regex → oob_candidate
    # ------------------------------------------------------------------
    def test_serial_match_oob_type_sets_oob_candidate(self):
        """Serial matches, incoming os=idrac, name differs → serial_action='oob_candidate'."""
        from netbox_librenms_plugin.tests.conftest import make_device

        # Existing host linked to libre #42; incoming device #17 is a separate iDRAC sharing the
        # chassis serial, with a different hostname → the OOB side of the same physical box.
        make_device("server01", serial="ABC123", librenms_cf={"default": 42})
        libre_device = {
            "device_id": 17,
            "hostname": "idrac-server01",
            "sysName": "idrac-server01",
            "hardware": "iDRAC9",
            "serial": "ABC123",
            "os": "idrac",
            "ip": "10.0.0.5",
            "version": "5.10.50",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "oob_candidate"
        assert result["oob_candidate"] is not None
        assert result["oob_candidate"]["type"] == "idrac"
        assert result["oob_candidate"]["ip"] == "10.0.0.5"
        assert result["oob_candidate"]["version"] == "5.10.50"
        assert result["can_import"] is False

    # ------------------------------------------------------------------
    # Case 2: Serial match + OOB regex + a DIFFERENT OOB already set → serial_action="link"
    # ------------------------------------------------------------------
    def test_serial_match_oob_already_linked_is_informational(self):
        """Serial matches, incoming is OOB-typed, but the device already has a (different) OOB linked → informational 'oob_already_linked' (re-import updates the existing OOB entry)."""
        from netbox_librenms_plugin.tests.conftest import make_device

        # Stored oob id 888 ≠ incoming 17, so find_by_librenms_id() does NOT match (host 42, oob
        # 888); the device is reached via the serial branch, where existing_oob is already set.
        make_device(
            "server01",
            serial="ABC123",
            librenms_cf={"default": {"id": 42, "oob": {"id": 888, "type": "idrac"}}},
        )
        libre_device = {
            "device_id": 17,
            "hostname": "idrac-server01",
            "sysName": "idrac-server01",
            "hardware": "iDRAC9",
            "serial": "ABC123",
            "os": "idrac",
            "ip": "10.0.0.5",
            "version": "5.10.50",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "oob_already_linked"
        assert result["oob_candidate"] is None
        assert any("already has an OOB controller linked" in w for w in result["warnings"])

    # ------------------------------------------------------------------
    # Case 1b: string device_id is coerced — still oob_candidate
    # ------------------------------------------------------------------
    def test_serial_match_oob_type_string_device_id(self):
        """device_id as string '17' (and a string-stored host id) coerces → oob_candidate."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("server01", serial="ABC123", librenms_cf={"default": "42"})
        libre_device = {
            "device_id": "17",  # string, not int
            "hostname": "idrac-server01",
            "sysName": "idrac-server01",
            "hardware": "iDRAC9",
            "serial": "ABC123",
            "os": "idrac",
            "ip": "10.0.0.5",
            "version": "5.10.50",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "oob_candidate"
        assert result["oob_candidate"] is not None
        assert result["oob_candidate"]["type"] == "idrac"

    # ------------------------------------------------------------------
    # Case 2b: string device_id '17' matches a stored OOB id 17 → re-import path
    # ------------------------------------------------------------------
    def test_serial_match_oob_already_linked_string_device_id(self):
        """String device_id '17' coerces to match the stored int OOB id 17 → the device is found via find_by_librenms_id's oob predicate (existing_match_type='librenms_oob'), the real re-import path — proving "17"==17 coercion in the OOB lookup."""
        from netbox_librenms_plugin.tests.conftest import make_device

        dev = make_device(
            "server01",
            serial="ABC123",
            librenms_cf={"default": {"id": 42, "oob": {"id": 17, "type": "idrac"}}},
        )
        libre_device = {
            "device_id": "17",  # string, not int
            "hostname": "idrac-server01",
            "sysName": "idrac-server01",
            "hardware": "iDRAC9",
            "serial": "ABC123",
            "os": "idrac",
            "ip": "10.0.0.5",
            "version": "5.10.50",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["existing_match_type"] == "librenms_oob"
        assert result["existing_device"].pk == dev.pk
        # Matched as the OOB controller, not the serial branch → no oob_candidate offered.
        assert result["oob_candidate"] is None

    def test_serial_match_non_oob_type_uses_standard_logic(self):
        """Non-OOB incoming whose hostname matches a device that ALSO shares the serial with a peer device (VM+Device name collision forces the serial branch): names match → plain 'link', no OOB role offered."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_vm

        # A VM and a Device share the hostname → the hostname branch hits its "both exist /
        # ambiguous" path and leaves existing_device unset, so the serial branch runs with a
        # device whose name == hostname (names_match=True) — the only way that path is reachable.
        make_vm("server01")
        make_device("server01", serial="ABC123")
        libre_device = {
            "device_id": 42,
            "hostname": "server01",
            "sysName": "server01",
            "hardware": "PowerEdge R640",
            "serial": "ABC123",
            "os": "linux",
            "ip": "192.168.1.1",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "link"
        assert result["oob_candidate"] is None

    # ------------------------------------------------------------------
    # Case 4: result dict always has oob_candidate key (even on a clean new import)
    # ------------------------------------------------------------------
    def test_result_always_contains_oob_candidate_key(self):
        """A brand-new import (no matching device) still includes oob_candidate=None."""
        libre_device = {
            "device_id": 1,
            "hostname": "sw01",
            "sysName": "sw01",
            "hardware": "SomeSwitch",
            "serial": "",
            "os": "ios",
            "ip": "",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert "oob_candidate" in result
        assert result["oob_candidate"] is None

    # ------------------------------------------------------------------
    # Case 4b: Inverse-OOB — existing device named like an OOB, linked elsewhere → promote_to_host
    # ------------------------------------------------------------------
    def test_serial_match_inverse_oob_sets_promote_to_host(self):
        """Existing device named 'idrac-*' linked to libre #99; incoming host (os=linux) shares the serial → promote_to_host."""
        from netbox_librenms_plugin.tests.conftest import make_device

        dev = make_device("idrac-jhw6nc4", serial="ABC123", librenms_cf={"default": {"id": 99}})
        libre_device = {
            "device_id": 42,
            "hostname": "eve-ng-02",
            "sysName": "eve-ng-02",
            "hardware": "Dell PowerEdge R770",
            "serial": "ABC123",
            "os": "linux",
            "ip": "10.0.0.10",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "promote_to_host"
        pt = result["promote_to_host"]
        assert pt["existing_libre_id"] == 99
        assert pt["existing_oob_type"] == "idrac"
        assert pt["existing_device"].pk == dev.pk
        assert result["existing_librenms_link"] == {"host_id": 99, "oob_id": None, "oob_type": None}
        assert result["can_import"] is False

    def test_serial_match_inverse_oob_skipped_when_existing_already_has_oob(self):
        """If the existing device already has an OOB linked, do NOT offer promote_to_host."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device(
            "idrac-jhw6nc4",
            serial="ABC123",
            librenms_cf={"default": {"id": 100, "oob": {"id": 99, "type": "idrac"}}},
        )
        libre_device = {
            "device_id": 42,
            "hostname": "eve-ng-02",
            "sysName": "eve-ng-02",
            "hardware": "Dell PowerEdge R770",
            "serial": "ABC123",
            "os": "linux",
            "ip": "10.0.0.10",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] != "promote_to_host"
        assert result.get("promote_to_host") is None
        assert result["existing_librenms_link"]["oob_id"] == 99

    def test_serial_role_choice_available_offers_both_options_when_feasible(self):
        """Existing device with a different host link, no OOB, OOB-style name → both roles feasible: promote_to_host default + serial_role_choice_available toggle."""
        from netbox_librenms_plugin.tests.conftest import make_device

        dev = make_device("idrac-jhw6nc4", serial="ABC123", librenms_cf={"default": {"id": 25}})
        libre_device = {
            "device_id": 42,
            "hostname": "eve-ng-02",
            "sysName": "eve-ng-02",
            "hardware": "Dell PowerEdge R770",
            "serial": "ABC123",
            "os": "linux",
            "ip": "10.0.0.10",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "promote_to_host"
        assert result["serial_role_choice_available"] is True
        assert result["oob_candidate"] is not None
        assert result["oob_candidate"]["device"].pk == dev.pk
        assert result["promote_to_host"] is not None
        assert result["promote_to_host"]["existing_libre_id"] == 25

    def test_serial_role_choice_not_available_when_names_match_and_no_link(self):
        """Exact name match + no existing LibreNMS link → simple link case, no role toggle."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_vm

        # VM+Device name collision forces the serial branch (see non_oob test); the device has no
        # link and its name matches the hostname → not a chassis pair, so no role choice.
        make_vm("server01")
        make_device("server01", serial="XYZ789")
        libre_device = {
            "device_id": 42,
            "hostname": "server01",
            "sysName": "server01",
            "hardware": "PowerEdge R640",
            "serial": "XYZ789",
            "os": "linux",
            "ip": "192.168.1.1",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "link"
        assert result.get("serial_role_choice_available") is False
        assert result["oob_candidate"] is None
        assert result.get("promote_to_host") is None

    def test_serial_match_inverse_oob_requires_oob_pattern_in_name(self):
        """Existing device WITHOUT an OOB-style name but linked to a different LibreNMS id is an ambiguous chassis pair: both roles populated, toggle offered, default oob_candidate."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("old-server-name", serial="ABC123", librenms_cf={"default": {"id": 99}})
        libre_device = {
            "device_id": 42,
            "hostname": "eve-ng-02",
            "sysName": "eve-ng-02",
            "hardware": "Dell PowerEdge R770",
            "serial": "ABC123",
            "os": "linux",
            "ip": "10.0.0.10",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "oob_candidate"
        assert result["serial_role_choice_available"] is True
        assert result["oob_candidate"] is not None
        assert result["promote_to_host"] is not None
        assert result["promote_to_host"]["existing_libre_id"] == 99
        assert result["existing_librenms_link"]["host_id"] == 99

    def test_serial_match_reinstall_no_oob_signal_yields_hostname_differs(self):
        """Serial match + differing hostname with no OOB signal and no existing link is a reinstall (hostname_differs), not an OOB candidate."""
        from netbox_librenms_plugin.tests.conftest import make_device

        # Same chassis serial, a NEW hostname, no LibreNMS link, and neither side OOB-flavoured:
        # this is a device reinstall, not a host/OOB chassis pair. Offering "Add as OOB controller"
        # here would steer the user to mis-pair a reinstalled host with its own stale record.
        make_device("old-server-name", serial="ABC123")  # no librenms_cf → not linked
        libre_device = {
            "device_id": 42,
            "hostname": "eve-ng-02",  # differs from the existing device name
            "sysName": "eve-ng-02",
            "hardware": "Dell PowerEdge R770",
            "serial": "ABC123",
            "os": "linux",  # not OOB-typed
            "ip": "10.0.0.10",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "hostname_differs"
        assert result["existing_match_type"] == "serial"
        assert result["serial_role_choice_available"] is False
        assert result["oob_candidate"] is None
        assert result.get("promote_to_host") is None

    # ------------------------------------------------------------------
    # Stage 2: two-NetBox-device merge detection
    # ------------------------------------------------------------------
    def test_merge_candidates_detected_when_hostname_and_serial_match_different_devices(self):
        """Hostname matches device A, serial matches a different device B (both linked) → merge_netbox_devices with both candidates surfaced."""
        from netbox_librenms_plugin.tests.conftest import make_device

        # Incoming #7 matches neither stored id, so it isn't bound by find_by_librenms_id; the
        # hostname matches host_named and the serial matches a different oob_named device.
        host = make_device("eve-ng-02", serial="ABC123", librenms_cf={"default": {"id": 42}})
        oob = make_device("idrac-jhw6nc4", serial="ABC123", librenms_cf={"default": {"id": 99}})
        libre_device = {
            "device_id": 7,
            "hostname": "eve-ng-02",
            "sysName": "eve-ng-02",
            "hardware": "Dell PowerEdge R770",
            "serial": "ABC123",
            "os": "linux",
            "ip": "10.0.0.10",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] == "merge_netbox_devices"
        assert result["merge_candidates"] is not None
        assert result["merge_candidates"]["host_named"]["pk"] == host.pk
        assert result["merge_candidates"]["host_named"]["name"] == "eve-ng-02"
        assert result["merge_candidates"]["oob_named"]["pk"] == oob.pk
        assert result["merge_candidates"]["oob_named"]["name"] == "idrac-jhw6nc4"
        assert result["merge_candidates"]["host_named"]["librenms_link"]["host_id"] == 42
        assert result["merge_candidates"]["oob_named"]["librenms_link"]["host_id"] == 99
        assert result["can_import"] is False

    def test_merge_candidates_detected_for_a_numeric_serial(self):
        """An all-digit serial arriving as an int must still pair the hostname- and serial-matched devices; the pairing stage reads the serial again and would otherwise raise into the silent merge-detection catch."""
        from netbox_librenms_plugin.tests.conftest import make_device

        host = make_device("eve-ng-num", serial="123456", librenms_cf={"default": {"id": 42}})
        oob = make_device("idrac-num", serial="123456", librenms_cf={"default": {"id": 99}})
        libre_device = {
            "device_id": 7,
            "hostname": "eve-ng-num",
            "sysName": "eve-ng-num",
            "hardware": "Dell PowerEdge R770",
            "serial": 123456,  # int, not str
            "os": "linux",
            "ip": "10.0.0.11",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["merge_candidates"] is not None
        assert result["merge_candidates"]["host_named"]["pk"] == host.pk
        assert result["merge_candidates"]["oob_named"]["pk"] == oob.pk

    def test_merge_candidates_skipped_when_neither_device_has_librenms_link(self):
        """Two devices share serial but neither has a LibreNMS link → conservative skip."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("eve-ng-02", serial="ABC123")
        make_device("idrac-jhw6nc4", serial="ABC123")
        libre_device = {
            "device_id": 7,
            "hostname": "eve-ng-02",
            "sysName": "eve-ng-02",
            "hardware": "Dell PowerEdge R770",
            "serial": "ABC123",
            "os": "linux",
            "ip": "10.0.0.10",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] != "merge_netbox_devices"
        assert result["merge_candidates"] is None

    def test_merge_candidates_skipped_when_only_one_device(self):
        """Hostname matches, no other device shares the serial → no merge candidates."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("eve-ng-02", serial="ABC123", librenms_cf={"default": {"id": 42}})
        libre_device = {
            "device_id": 7,
            "hostname": "eve-ng-02",
            "sysName": "eve-ng-02",
            "hardware": "Dell PowerEdge R770",
            "serial": "ABC123",
            "os": "linux",
            "ip": "10.0.0.10",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["serial_action"] != "merge_netbox_devices"
        assert result["merge_candidates"] is None

    # ------------------------------------------------------------------
    # Case 5: Re-import via OOB id → existing_match_type = "librenms_oob"
    # ------------------------------------------------------------------
    def test_reimport_via_oob_id_sets_match_type_librenms_oob(self):
        """Re-importing the OOB controller (device_id == stored oob.id) is matched via the oob predicate → existing_match_type='librenms_oob'."""
        from netbox_librenms_plugin.tests.conftest import make_device

        dev = make_device(
            "server01",
            serial="ABC123",
            librenms_cf={"default": {"id": 42, "oob": {"id": 17, "type": "idrac"}}},
        )
        libre_device = {
            "device_id": 17,
            "hostname": "idrac-server01",
            "sysName": "idrac-server01",
            "hardware": "iDRAC9",
            "serial": "ABC123",
            "os": "idrac",
            "ip": "10.0.0.5",
            "version": "5.10.50",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["existing_match_type"] == "librenms_oob"
        assert result["existing_device"].pk == dev.pk

    def test_librenms_oob_match_skips_host_serial_drift(self):
        """An OOB-id match (existing_match_type='librenms_oob') must skip the host serial-drift comparison: the incoming payload is the OOB controller's, so comparing it against the host record's serial would surface a bogus replacement warning."""
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device(
            "server01",
            serial="HOST-SERIAL",
            librenms_cf={"default": {"id": 42, "oob": {"id": 17, "type": "idrac"}}},
        )
        libre_device = {
            "device_id": 17,
            "hostname": "idrac-server01",
            "sysName": "idrac-server01",
            "hardware": "iDRAC9",
            "serial": "INCOMING-SERIAL",  # differs from the host serial above
            "os": "idrac",
            "ip": "10.0.0.5",
            "version": "",
            "location": "",
        }
        result = self._validate(libre_device)

        assert result["existing_match_type"] == "librenms_oob"
        assert result.get("serial_action") is None
        assert not any("Serial number differs" in w for w in result["warnings"])


@pytest.mark.django_db
class TestMergeCandidateNonUniqueSerialPeer:
    """The merge-candidate serial-peer lookup must require a UNIQUE peer."""

    def _validate(self, libre_device):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import

        api = MagicMock(server_key="default", cache_timeout=300)
        # Patch only the external/heavy boundaries; Device + VirtualMachine stay real so the
        # serial-peer query runs against the DB rows created below.
        patches = [
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_site",
                return_value={"found": False, "site": None, "match_type": None, "suggestions": []},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.match_librenms_hardware_to_device_type",
                return_value={"matched": False, "device_type": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.find_matching_platform",
                return_value={"found": False, "platform": None, "match_type": None},
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": [], "detection_error": None},
            ),
            patch("netbox_librenms_plugin.import_utils.device_operations.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.import_utils.device_operations.cache"),
        ]
        for p in patches:
            p.start()
        try:
            return validate_device_for_import(libre_device, api=api, include_vc_detection=False)
        finally:
            for p in reversed(patches):
                p.stop()

    def _libre(self, serial):
        return {
            "device_id": 1,
            "hostname": "host1",
            "sysName": "host1",
            "hardware": "-",
            "serial": serial,
            "os": "-",
            "location": "-",
        }

    def test_multiple_serial_peers_skip_merge_suggestion(self):
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("host1")  # hostname match (no serial)
        make_device("dup-b", serial="SHARED")  # two NetBox devices share this serial
        make_device("dup-c", serial="SHARED")

        result = self._validate(self._libre("SHARED"))

        # The guard warns and skips the suggestion instead of pairing an arbitrary peer.
        assert any("Multiple NetBox devices share serial 'SHARED'" in w for w in result["warnings"])
        assert not result.get("merge_candidates")

    def test_single_serial_peer_still_considered(self):
        from netbox_librenms_plugin.tests.conftest import make_device

        make_device("host1")  # hostname match (no LibreNMS link)
        # Exactly one same-serial peer, and it already has a LibreNMS link so the conservative
        # "at least one side linked" guard passes and the merge suggestion actually fires.
        make_device("dup-b", serial="SHARED", librenms_cf={"default": {"id": 99}})

        result = self._validate(self._libre("SHARED"))

        # A unique peer must NOT trip the multi-peer guard...
        assert not any("Multiple NetBox devices share serial" in w for w in result["warnings"])
        # ...and the positive outcome must actually be produced: the unique peer is paired and
        # surfaced as a merge suggestion. (Asserting only the absence of the warning would still
        # pass with merge detection fully disabled — this pins the real behavior.)
        assert result["serial_action"] == "merge_netbox_devices"
        assert result.get("merge_candidates")

    def test_current_hostname_side_nonunique_skips_merge(self):
        """The CURRENT merge side (existing_match_type='hostname') is itself taken from a .first() match, so duplicate device names make it an arbitrary row that must fail closed instead of offering a merge."""
        from dcim.models import Device, Site

        from netbox_librenms_plugin.tests.conftest import make_device

        d1 = make_device("dup-host")  # site A
        site_b = Site.objects.create(name="merge-site-b", slug="merge-site-b")
        # Second device sharing the name in a different site (names are unique only per-site),
        # so Device.objects.filter(name__iexact="dup-host").first() is arbitrary.
        Device.objects.create(name="dup-host", device_type=d1.device_type, role=d1.role, site=site_b, status="active")
        # Unique serial peer carrying a LibreNMS link so the "at least one side linked" guard
        # would otherwise let the merge fire.
        make_device("serial-peer", serial="SER1", librenms_cf={"default": {"id": 99}})

        libre = {
            "device_id": 1,
            "hostname": "dup-host",
            "sysName": "dup-host",
            "hardware": "-",
            "serial": "SER1",
            "os": "-",
            "location": "-",
        }
        result = self._validate(libre)

        # The arbitrary non-unique current side is a terminal blocking state: match_type is
        # demoted to "ambiguous_hostname_or_serial" (which also suppresses the merge-candidate
        # pairing) and a blocking issue is surfaced — strictly safer than offering a merge whose
        # current side is an arbitrary .first() row.
        assert result["existing_match_type"] == "ambiguous_hostname_or_serial"
        assert not result.get("merge_candidates")
        assert result["can_import"] is False
        assert any("resolve the duplicate" in i for i in result["issues"])


@pytest.mark.django_db
class TestValidateDeviceForImportOOBIPFallback:
    """validate_device_for_import must find a device that references the LibreNMS IP via its Device.oob_ip FK even when that IP is assigned to no interface (assigned_object is None) — e.g."""

    def _make_api(self):
        api = MagicMock()
        api.server_key = "default"
        api.cache_timeout = 300
        api.get_device_info.return_value = (True, {"device_id": 7})
        return api

    def test_unassigned_oob_ip_device_is_detected(self):
        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import
        from netbox_librenms_plugin.tests.conftest import make_device, make_ip

        device = make_device("b2-oob-host")
        # An OOB IP assigned to no interface; set it on the device via update_fields so NetBox's
        # full_clean() (which would require an interface assignment) is bypassed — the exact state
        # the import's assigned_object-gated lookup used to miss.
        oob_ip = make_ip("192.0.2.50/32")
        assert oob_ip.assigned_object is None
        device.oob_ip = oob_ip
        device.save(update_fields=["oob_ip"])

        # A LibreNMS row that self-identifies as an OOB controller (iDRAC) whose IP is the
        # device's oob_ip; hostname/serial deliberately don't match so only the oob_ip FK links.
        libre_device = {
            "device_id": 7,
            "hostname": "idrac-probe-xyz",
            "sysName": "idrac-probe-xyz",
            "hardware": "iDRAC9",
            "serial": "-",
            "os": "-",
            "location": "-",
            "type": "network",
            "ip": "192.0.2.50",
        }

        result = validate_device_for_import(libre_device, api=self._make_api())

        # Found via the oob_ip fallback → OOB candidate (pre-fix: device None → block skipped).
        assert result["existing_device"] == device
        assert result["serial_action"] == "oob_candidate"
        assert result["oob_candidate"]["device"] == device


class TestDescribeLinkNote:
    """_describe_link_note: single source of truth for the host/OOB/unlinked phrasing."""

    def _note(self, link):
        from netbox_librenms_plugin.import_utils.device_operations import _describe_link_note

        return _describe_link_note(link)

    def test_host_id_phrasing(self):
        assert self._note({"host_id": 42, "oob_id": None}) == "currently linked to LibreNMS device #42"

    def test_oob_only_phrasing(self):
        # host_id absent but oob_id present → reported as OOB, not "not linked" (the old serial-match drift).
        assert self._note({"host_id": None, "oob_id": 7}) == "currently linked to LibreNMS as an OOB controller"

    def test_host_id_wins_over_oob(self):
        assert self._note({"host_id": 42, "oob_id": 7}) == "currently linked to LibreNMS device #42"

    def test_unlinked_phrasing(self):
        assert self._note({"host_id": None, "oob_id": None}) == "not linked to LibreNMS"

    def test_none_input_is_unlinked(self):
        assert self._note(None) == "not linked to LibreNMS"


@pytest.mark.django_db
class TestDetectSerialMatchRole:
    """_detect_serial_match_role is the pure role-decision step extracted from validate_device_for_import's serial-match branch."""

    def _role(self, existing_device, hostname, libre_device, *, server_key="default"):
        from netbox_librenms_plugin.import_utils.device_operations import (
            _describe_existing_librenms_link,
            _detect_serial_match_role,
        )

        existing_link = _describe_existing_librenms_link(existing_device, server_key)
        serial = str(libre_device.get("serial") or "").strip()
        return _detect_serial_match_role(existing_device, existing_link, hostname, serial, libre_device, server_key)

    def test_oob_candidate_default_when_incoming_is_oob_and_name_differs(self):
        # Existing unlinked host; incoming LibreNMS row is clearly an iDRAC whose hostname
        # differs (the OOB side of the same chassis) → default to oob_candidate.
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("host-server-1")
        libre_device = {
            "device_id": 7,
            "os": "",
            "hardware": "iDRAC9",
            "hostname": "host-server-1-idrac",
            "serial": "ABC123",
        }
        out = self._role(device, "host-server-1-idrac", libre_device)

        assert out["serial_action"] == "oob_candidate"
        assert out["oob_candidate"]["device"] == device
        assert out["oob_candidate"]["type"] == "idrac"
        assert out["promote_to_host"] is None
        # Host promotion isn't feasible (existing has no host link), so no manual toggle.
        assert out["serial_role_choice_available"] is False
        assert out["warnings"] == []

    def test_promote_to_host_when_existing_named_oob_and_linked_elsewhere(self):
        # Existing device is NAME-tagged as the OOB ("...-idrac") and already host-linked to a
        # DIFFERENT LibreNMS id; incoming row is a plain host → default to promote_to_host, with
        # both roles feasible so the UI can offer the toggle.
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("leaf01-idrac", librenms_cf={"default": 99})
        libre_device = {
            "device_id": 7,
            "os": "linux",
            "hardware": "PowerEdge R740",
            "hostname": "leaf01",
            "serial": "ABC123",
        }
        out = self._role(device, "leaf01", libre_device)

        assert out["serial_action"] == "promote_to_host"
        assert out["promote_to_host"]["existing_libre_id"] == 99
        assert out["promote_to_host"]["existing_oob_type"] == "idrac"
        assert out["promote_to_host"]["existing_device"] == device
        # Both oob_candidate and promote_to_host are feasible → user may flip the default.
        assert out["serial_role_choice_available"] is True

    def test_oob_candidate_type_falls_back_to_generic_sentinel(self):
        # Chassis pair signalled by the EXISTING name ("-bmc"), but the INCOMING row has no OS/
        # hardware/name OOB token, so the oob_candidate type takes the real `... or "oob"` fallback
        # (the production sentinel the AddAsOOBView tests must not reimplement inline).
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("rack1-bmc")  # existing name carries the OOB signal
        libre_device = {
            "device_id": 7,
            "os": "ubuntu",  # normalize_oob_type -> None
            "hardware": "",
            "hostname": "rack1",  # differs from existing, no OOB token
            "serial": "ABC123",
        }
        out = self._role(device, "rack1", libre_device)

        assert out["serial_action"] == "oob_candidate"
        assert out["oob_candidate"]["type"] == "oob"  # the real generic fallback, not a test copy

    def test_plain_link_when_names_match_and_unlinked(self):
        # Names match and the existing device has no LibreNMS link → not a chassis pair; fall
        # back to a plain "link" with the unlinked warning, no role choice.
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("switch-7")
        libre_device = {
            "device_id": 7,
            "os": "linux",
            "hardware": "Catalyst",
            "hostname": "switch-7",
            "serial": "ABC123",
        }
        out = self._role(device, "switch-7", libre_device)

        assert out["serial_action"] == "link"
        assert out["oob_candidate"] is None
        assert out["promote_to_host"] is None
        assert out["serial_role_choice_available"] is False
        assert any("not linked to LibreNMS" in w for w in out["warnings"])

    def test_oob_already_linked_is_informational_not_generic_link(self):
        # OOB-typed incoming row, but the existing serial-matched device ALREADY has an OOB
        # controller linked at this server key. This must be informational only — NOT the
        # generic "link" action, which renders an actionable host-link form ("Link to LibreNMS")
        # that would post an indistinguishable host-link request.
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device(
            "host-1",
            librenms_cf={"default": {"oob": {"id": 99, "type": "idrac"}}},
        )
        libre_device = {
            "device_id": 7,
            "os": "",
            "hardware": "iDRAC9",
            "hostname": "host-1-idrac",
            "serial": "ABC123",
        }
        out = self._role(device, "host-1-idrac", libre_device)

        assert out["serial_action"] == "oob_already_linked"
        assert out["serial_action"] != "link"
        assert any("already has an OOB controller linked" in w for w in out["warnings"])


@pytest.mark.django_db
class TestResolveDeviceByHostIP:
    """resolve_device_by_host_ip: the shared host-IP resolver must fail closed when a management IP maps to more than one distinct device (interface assignment + oob_ip FK), so neither import path binds to an arbitrary one."""

    def test_fails_closed_when_two_devices_share_the_host_ip(self):
        from netbox_librenms_plugin.import_utils.device_operations import resolve_device_by_host_ip
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device, make_ip

        dev_a = make_device("host-ip-a")
        ip_on(dev_a, "198.51.100.5/24", "eth0")  # assigned to dev_a's interface
        dev_b = make_device("host-ip-b")
        # A second IP row with the SAME host address, set as dev_b's oob_ip (FK, not assigned).
        dev_b.oob_ip = make_ip("198.51.100.5/32")
        dev_b.save()

        device, ambiguous, _matching = resolve_device_by_host_ip("198.51.100.5")

        assert ambiguous is True
        assert device is None

    def test_resolves_the_single_owning_device(self):
        from netbox_librenms_plugin.import_utils.device_operations import resolve_device_by_host_ip
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device

        dev = make_device("host-ip-single")
        ip_on(dev, "198.51.100.9/24", "eth0")

        device, ambiguous, _matching = resolve_device_by_host_ip("198.51.100.9")

        assert ambiguous is False
        assert device.pk == dev.pk


# ---------------------------------------------------------------------------
# normalize_oob_type (constants) — OOB controller family detection
# ---------------------------------------------------------------------------
class TestNormalizeOOBTypeCimc:
    """The docs advertise CIMC as a supported OOB controller family, so normalize_oob_type() must recognise it (and it must be in OOB_TYPES)."""

    def test_cimc_in_canonical_types(self):
        from netbox_librenms_plugin.constants import OOB_TYPES

        assert "cimc" in OOB_TYPES

    def test_cimc_detected_from_os_and_hardware(self):
        from netbox_librenms_plugin.constants import normalize_oob_type

        assert normalize_oob_type("cimc", "") == "cimc"
        assert normalize_oob_type("", "Cisco CIMC") == "cimc"

    def test_non_oob_still_none(self):
        from netbox_librenms_plugin.constants import normalize_oob_type

        assert normalize_oob_type("ubuntu", "") is None

    def test_prefix_inside_unrelated_word_does_not_match(self):
        """Whole-token matching: 'drac' inside 'dracut' (and 'ipmi' inside 'ipmitool') must NOT classify a normal device as an OOB controller."""
        from netbox_librenms_plugin.constants import normalize_oob_type

        assert normalize_oob_type("dracut", "") is None
        assert normalize_oob_type("ipmitool", "") is None
        assert normalize_oob_type("iDRAC9", "") == "idrac"
        assert normalize_oob_type("drac9", "") == "drac"


class TestNormalizeOOBTypePrefersVendorSpecific:
    """A vendor-specific match must win over the generic 'oob' token, even when the generic token appears earlier."""

    def test_generic_oob_in_os_does_not_mask_specific_hardware(self):
        from netbox_librenms_plugin.constants import normalize_oob_type

        assert normalize_oob_type("oob", "iDRAC9") == "idrac"

    def test_os_specific_still_wins_over_generic_hardware(self):
        from netbox_librenms_plugin.constants import normalize_oob_type

        # os-first ordering is preserved for the specific docstring example.
        assert normalize_oob_type("drac9", "iDRAC9") == "drac"

    def test_only_generic_present_returns_oob(self):
        from netbox_librenms_plugin.constants import normalize_oob_type

        assert normalize_oob_type("oob", "") == "oob"
        assert normalize_oob_type("", "generic oob device") == "oob"


# ---------------------------------------------------------------------------
# import_single_device — lazy validation passes api through
# ---------------------------------------------------------------------------
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
            # Call with validation=None so lazy path triggers.
            import_single_device(
                42,
                server_key="prod",
                sync_options={"use_sysname": True, "strip_domain": False},
                validation=None,
                libre_device={"device_id": 42, "hostname": "test"},
            )

            mock_validate.assert_called_once()
            # api must be passed as keyword arg.
            assert mock_validate.call_args[1].get("api") is mock_api


# ---------------------------------------------------------------------------
# _detect_serial_match_role — serial-match role classification (real DB)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSerialMatchRoleIgnoresMissingDeviceId:
    """A missing/zero incoming device_id is unknown, not a 'linked elsewhere' mismatch."""

    def test_missing_device_id_does_not_offer_chassis_pair_toggle(self):
        from netbox_librenms_plugin.import_utils.device_operations import _detect_serial_match_role
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("host1", serial="SN1")
        existing_link = {"host_id": 42, "oob_id": None}
        result = _detect_serial_match_role(
            existing_by_serial=device,
            existing_link=existing_link,
            hostname="host1",  # matches device.name
            serial="SN1",
            libre_device={"os": "ios", "hardware": "C9300"},  # no device_id key → normalizes to None
            server_key="default",
        )

        # Names match and there's no real incoming id to mismatch against, so this is a plain link
        # — NOT a host/OOB chassis-pair situation. The role-choice toggle must not be offered.
        assert result["serial_role_choice_available"] is False
        assert result["serial_action"] == "link"


@pytest.mark.django_db
class TestSerialMatchRoleSameNameOobController:
    """An incoming OOB-typed device stages as oob_candidate even when its hostname matches the host."""

    def test_same_name_oob_typed_device_stages_oob_candidate_not_link(self):
        from netbox_librenms_plugin.import_utils.device_operations import _detect_serial_match_role
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("host1", serial="SN1")  # real host, no OOB linked → existing_oob is None
        result = _detect_serial_match_role(
            existing_by_serial=device,
            existing_link=None,  # not yet linked to any LibreNMS id
            hostname="host1",  # iDRAC mirrors the host's hostname → names_match is True
            serial="SN1",
            libre_device={"device_id": 77, "os": "idrac", "hardware": "iDRAC9"},
            server_key="default",
        )

        # LibreNMS reports the incoming device as an OOB controller, so a same-name match must NOT
        # collapse to the legacy host link (which would attach the iDRAC's id #77 as the HOST id).
        # It has to surface as an OOB candidate instead.
        assert result["serial_action"] == "oob_candidate"
        assert result["oob_candidate"] is not None
        assert result["oob_candidate"]["device"] == device
        assert result["oob_candidate"]["type"] == "idrac"


@pytest.mark.django_db
class TestValidateDedupsSerialDuplicateQuery:
    """The Stage-1 duplicate guard and Stage-2 merge detection share one serial[:2] lookup."""

    def test_serial_match_runs_serial_dup_query_once(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.import_utils.device_operations import validate_device_for_import
        from netbox_librenms_plugin.tests.conftest import make_device

        # One NetBox device matched by serial; its name differs from the LibreNMS hostname so
        # validation takes the serial-match path (which runs both dup-detection stages).
        make_device("nb-name", serial="UNIQSER1")
        api = MagicMock(server_key="default", cache_timeout=300)
        api.get_device_info.return_value = (True, {"device_id": 1})
        libre_device = {
            "device_id": 5,
            "hostname": "libre-name",
            "sysName": "libre-name",
            "serial": "UNIQSER1",
            "hardware": "Model-X",
            "os": "ios",
        }

        with CaptureQueriesContext(connection) as ctx:
            validate_device_for_import(libre_device, api=api, include_vc_detection=False)

        # The duplicate-detection serial lookup (serial[:2], no .exclude) must run exactly once,
        # not once per stage. The .first() match query is LIMIT 1; the cross-side query has NOT.
        serial_dup_queries = [
            q["sql"]
            for q in ctx.captured_queries
            if 'serial" =' in q["sql"].lower() and "limit 2" in q["sql"].lower() and "not" not in q["sql"].lower()
        ]
        assert len(serial_dup_queries) == 1
