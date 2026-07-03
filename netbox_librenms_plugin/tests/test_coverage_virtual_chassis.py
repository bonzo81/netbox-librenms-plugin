"""Coverage tests for virtual_chassis.py lines 431 and 435."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


def _make_master_device(serial="MASTER001"):
    """Build a mock master Device for VC creation tests."""
    master = MagicMock()
    master.name = "switch-master"
    master.serial = serial
    master.pk = 1
    master.rack = None
    master.location = None
    master.device_type = MagicMock()
    master.role = MagicMock()
    master.site = MagicMock()
    master.platform = MagicMock()
    return master


class TestCreateVirtualChassisWithMembersPositionConflict:
    """Tests specifically for lines 431 and 435 - position conflict resolution."""

    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.transaction")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device")
    def test_line_431_position_conflict_sets_discovered_pos_to_none(
        self, mock_Device, mock_VirtualChassis, mock_load_pattern, mock_transaction
    ):
        """
        Line 431: discovered_pos = None when position already in used_positions.

        Scenario: master is at position 1 (used_positions = {1}).
        First member takes position 2. Second member also claims position 2
        → discovered_pos set to None → falls back to sequential (position 3).
        """
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        # Make transaction.atomic() a no-op context manager
        @contextmanager
        def noop_atomic():
            yield

        mock_transaction.atomic = noop_atomic

        mock_load_pattern.return_value = "-M{position}"

        master = _make_master_device("MASTER001")
        vc_mock = MagicMock()
        vc_mock.members.count.return_value = 3
        mock_VirtualChassis.objects.create.return_value = vc_mock

        # Device.objects.filter(...).exists() → False (no conflicts)
        mock_filter = MagicMock()
        mock_filter.exists.return_value = False
        mock_filter.exclude.return_value = mock_filter
        mock_Device.objects.filter.return_value = mock_filter
        mock_Device.objects.create.return_value = MagicMock()

        # Members: first at position 2, second ALSO at position 2 (conflict)
        members_info = [
            {"serial": "SN002", "position": 2, "name": "Member2"},
            {"serial": "SN003", "position": 2, "name": "Member3-conflict"},  # triggers line 431
        ]
        libre_device = {"device_id": 99}

        create_virtual_chassis_with_members(master, members_info, libre_device)

        # VC should be created
        mock_VirtualChassis.objects.create.assert_called_once()

        # Two Device.objects.create calls for the two non-master members
        create_calls = mock_Device.objects.create.call_args_list
        assert len(create_calls) == 2
        # Map serial -> vc_position for precise identity assertions
        serial_to_pos = {c.kwargs.get("serial"): c.kwargs.get("vc_position") for c in create_calls}
        # First member (SN002) takes its explicit position 2
        assert serial_to_pos.get("SN002") == 2
        # Second member (SN003) conflicts at 2, falls back to 3
        assert serial_to_pos.get("SN003") == 3

    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.transaction")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device")
    def test_line_435_while_loop_skips_taken_slots(
        self, mock_Device, mock_VirtualChassis, mock_load_pattern, mock_transaction
    ):
        """Line 435: position += 1 in while loop when sequential slot is taken."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        @contextmanager
        def noop_atomic():
            yield

        mock_transaction.atomic = noop_atomic
        mock_load_pattern.return_value = "-M{position}"

        master = _make_master_device("MASTER001")
        vc_mock = MagicMock()
        vc_mock.members.count.return_value = 3
        mock_VirtualChassis.objects.create.return_value = vc_mock

        mock_filter = MagicMock()
        mock_filter.exists.return_value = False
        mock_filter.exclude.return_value = mock_filter
        mock_Device.objects.filter.return_value = mock_filter
        mock_Device.objects.create.return_value = MagicMock()

        # Member A explicitly at position 2
        # Member B has no position → sequential starts at 2 → taken → increments to 3 (line 435)
        members_info = [
            {"serial": "SN002", "position": 2, "name": "Member-explicit-2"},
            {"serial": "SN003", "position": None, "name": "Member-no-pos"},  # triggers line 435
        ]
        libre_device = {"device_id": 99}

        create_virtual_chassis_with_members(master, members_info, libre_device)
        mock_VirtualChassis.objects.create.assert_called_once()

        create_calls = mock_Device.objects.create.call_args_list
        positions_used = [c.kwargs.get("vc_position") for c in create_calls]
        # First member gets explicit position 2; second (no position) gets 3 after 2 is taken
        assert sorted(positions_used) == [2, 3]
        actual_entries = sorted([(c.kwargs.get("serial"), c.kwargs.get("vc_position")) for c in create_calls])
        assert actual_entries == [("SN002", 2), ("SN003", 3)]

    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.transaction")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device")
    def test_multiple_sequential_slots_taken_skips_all(
        self, mock_Device, mock_VirtualChassis, mock_load_pattern, mock_transaction
    ):
        """Multiple sequential increments: position = 2, 3 all taken → gets 4."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        @contextmanager
        def noop_atomic():
            yield

        mock_transaction.atomic = noop_atomic
        mock_load_pattern.return_value = "-M{position}"

        master = _make_master_device("MASTER001")
        vc_mock = MagicMock()
        vc_mock.members.count.return_value = 4
        mock_VirtualChassis.objects.create.return_value = vc_mock

        mock_filter = MagicMock()
        mock_filter.exists.return_value = False
        mock_filter.exclude.return_value = mock_filter
        mock_Device.objects.filter.return_value = mock_filter
        mock_Device.objects.create.return_value = MagicMock()

        # Members at positions 2 and 3; then one with no position → should get 4
        members_info = [
            {"serial": "SN002", "position": 2, "name": "M2"},
            {"serial": "SN003", "position": 3, "name": "M3"},
            {"serial": "SN004", "position": None, "name": "M-no-pos"},  # should get 4
        ]
        libre_device = {"device_id": 10}

        create_virtual_chassis_with_members(master, members_info, libre_device)

        create_calls = mock_Device.objects.create.call_args_list
        positions_used = [c.kwargs.get("vc_position") for c in create_calls]
        # Members at 2 and 3 are explicit; the member with no position gets 4
        assert sorted(positions_used) == [2, 3, 4]
        actual_entries = sorted([(c.kwargs.get("serial"), c.kwargs.get("vc_position")) for c in create_calls])
        assert actual_entries == [("SN002", 2), ("SN003", 3), ("SN004", 4)]

    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.transaction")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device")
    def test_member_with_same_serial_as_master_is_skipped(
        self, mock_Device, mock_VirtualChassis, mock_load_pattern, mock_transaction
    ):
        """Members with same serial as master device should be skipped."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        @contextmanager
        def noop_atomic():
            yield

        mock_transaction.atomic = noop_atomic
        mock_load_pattern.return_value = "-M{position}"
        master = _make_master_device("MASTER_SERIAL")
        vc_mock = MagicMock()
        vc_mock.members.count.return_value = 1
        mock_VirtualChassis.objects.create.return_value = vc_mock

        mock_filter = MagicMock()
        mock_filter.exists.return_value = False
        mock_filter.exclude.return_value = mock_filter
        mock_Device.objects.filter.return_value = mock_filter
        mock_Device.objects.create.return_value = MagicMock()

        members_info = [
            {"serial": "MASTER_SERIAL", "position": 2, "name": "Master-dup"},  # skipped
            {"serial": "SN999", "position": 3, "name": "Real member"},
        ]
        libre_device = {"device_id": 5}

        create_virtual_chassis_with_members(master, members_info, libre_device)

        # Only one Device.objects.create for the non-duplicate member
        create_calls = mock_Device.objects.create.call_args_list
        assert len(create_calls) == 1
        assert create_calls[0].kwargs.get("serial") == "SN999"


class TestCreateVirtualChassisServerKeyDomain:
    """Tests for server_key parameter in create_virtual_chassis_with_members domain."""

    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.transaction")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device")
    def test_server_key_included_in_domain(self, mock_Device, mock_VirtualChassis, mock_load_pattern, mock_transaction):
        """With server_key='production', domain should contain 'librenms-production-'."""
        from contextlib import contextmanager

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        @contextmanager
        def noop_atomic():
            yield

        mock_transaction.atomic = noop_atomic
        mock_load_pattern.return_value = "-M{position}"

        master = _make_master_device("SN001")
        vc_mock = MagicMock()
        vc_mock.members.count.return_value = 1
        mock_VirtualChassis.objects.create.return_value = vc_mock

        mock_filter = MagicMock()
        mock_filter.exists.return_value = False
        mock_filter.exclude.return_value = mock_filter
        mock_Device.objects.filter.return_value = mock_filter
        mock_Device.objects.create.return_value = MagicMock()

        libre_device = {"device_id": 42}

        create_virtual_chassis_with_members(master, [], libre_device, server_key="production")

        call_kwargs = mock_VirtualChassis.objects.create.call_args.kwargs
        assert "librenms-production-" in call_kwargs["domain"], f"domain was: {call_kwargs['domain']}"
        assert "42" in call_kwargs["domain"]

    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.transaction")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.VirtualChassis")
    @patch("netbox_librenms_plugin.import_utils.virtual_chassis.Device")
    def test_no_server_key_domain_prefix_is_librenms(
        self, mock_Device, mock_VirtualChassis, mock_load_pattern, mock_transaction
    ):
        """Without server_key, domain should start with 'librenms-' (no server suffix)."""
        from contextlib import contextmanager

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members

        @contextmanager
        def noop_atomic():
            yield

        mock_transaction.atomic = noop_atomic
        mock_load_pattern.return_value = "-M{position}"

        master = _make_master_device("SN002")
        vc_mock = MagicMock()
        vc_mock.members.count.return_value = 1
        mock_VirtualChassis.objects.create.return_value = vc_mock

        mock_filter = MagicMock()
        mock_filter.exists.return_value = False
        mock_filter.exclude.return_value = mock_filter
        mock_Device.objects.filter.return_value = mock_filter
        mock_Device.objects.create.return_value = MagicMock()

        libre_device = {"device_id": 99}

        create_virtual_chassis_with_members(master, [], libre_device, server_key=None)

        call_kwargs = mock_VirtualChassis.objects.create.call_args.kwargs
        domain = call_kwargs["domain"]
        assert domain.startswith("librenms-"), f"domain was: {domain}"
        # Should not have a second prefix like 'librenms-None-'
        assert "librenms-None" not in domain
        assert "99" in domain


# ---------------------------------------------------------------------------
# _load_vc_member_name_pattern — reads the configured pattern (real settings)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestLoadVcMemberNamePattern:
    """_load_vc_member_name_pattern returns the configured pattern from LibreNMSSettings, else the default."""

    DEFAULT = "-M{position}"

    def _call(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern

        return _load_vc_member_name_pattern()

    def _set_pattern(self, value):
        from netbox_librenms_plugin.models import LibreNMSSettings

        # LibreNMSSettings.save() pins pk=1 (singleton); reuse that single row.
        obj, _ = LibreNMSSettings.objects.get_or_create(pk=1)
        obj.vc_member_name_pattern = value
        obj.save()

    def test_returns_configured_pattern(self):
        self._set_pattern("-SW{position}")
        assert self._call() == "-SW{position}"

    def test_returns_default_for_empty_string(self):
        self._set_pattern("")
        assert self._call() == self.DEFAULT

    def test_returns_default_for_whitespace_only(self):
        self._set_pattern("   ")
        assert self._call() == self.DEFAULT

    def test_returns_default_when_no_settings(self):
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.all().delete()
        assert self._call() == self.DEFAULT

    def test_returns_default_on_db_error(self):
        """The load path swallows an infrastructure failure and falls back to the default."""
        # Injecting a DB error at the query boundary is the one non-real dependency worth faking.
        with patch("netbox_librenms_plugin.models.LibreNMSSettings.objects") as mock_objs:
            mock_objs.order_by.side_effect = RuntimeError("db error")
            assert self._call() == self.DEFAULT


# ---------------------------------------------------------------------------
# _generate_vc_member_name — pattern handling (pure)
# ---------------------------------------------------------------------------
class TestGenerateVcMemberName:
    """_generate_vc_member_name must respect a caller-supplied pattern and catch format errors."""

    def _call(self, master_name, position, serial=None, pattern=None):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        return _generate_vc_member_name(master_name, position, serial=serial, pattern=pattern)

    def test_explicit_pattern_used(self):
        """When a pattern is passed it is used directly (no DB query)."""
        assert self._call("switch01", 2, pattern="-SW{position}") == "switch01-SW2"

    def test_serial_in_pattern(self):
        assert self._call("switch01", 2, serial="ABC123", pattern=" [{serial}]") == "switch01 [ABC123]"

    def test_none_pattern_loads_from_settings(self):
        """When pattern is None, _load_vc_member_name_pattern is consulted."""
        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-STACK{position}",
        ):
            assert self._call("core01", 3, pattern=None) == "core01-STACK3"

    def test_malformed_pattern_falls_back_to_default(self):
        """An invalid format spec falls back to -M{position}."""
        assert self._call("switch01", 2, pattern="{position!z}") == "switch01-M2"

    def test_missing_key_falls_back_to_default(self):
        """An unknown placeholder falls back to -M{position}."""
        assert self._call("switch01", 2, pattern="-{unknown_key}") == "switch01-M2"

    def test_default_pattern(self):
        assert self._call("switch01", 2, pattern="-M{position}") == "switch01-M2"
