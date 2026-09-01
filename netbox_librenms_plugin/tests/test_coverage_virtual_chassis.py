"""Real-model coverage for virtual chassis creation and naming helpers."""

import pytest


@pytest.mark.django_db
class TestCreateVirtualChassisWithMembers:
    @staticmethod
    def _create(tag, members, *, server_key="default", master_serial="MASTER"):
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.tests.conftest import make_device

        master = make_device(f"{tag}-master", serial=master_serial)
        virtual_chassis = create_virtual_chassis_with_members(
            master,
            members,
            {"device_id": master.pk},
            server_key=server_key,
        )
        return master, virtual_chassis

    def test_duplicate_discovered_position_uses_next_free_slot(self):
        _master, virtual_chassis = self._create(
            "duplicate-position",
            [
                {"serial": "MEMBER-2", "position": 2, "name": "Member 2"},
                {"serial": "MEMBER-3", "position": 2, "name": "Conflicting member"},
            ],
        )

        assert list(virtual_chassis.members.order_by("vc_position").values_list("serial", "vc_position")) == [
            ("MASTER", 1),
            ("MEMBER-2", 2),
            ("MEMBER-3", 3),
        ]

    def test_missing_positions_skip_all_taken_slots(self):
        _master, virtual_chassis = self._create(
            "sequential-position",
            [
                {"serial": "MEMBER-2", "position": 2, "name": "Member 2"},
                {"serial": "MEMBER-3", "position": 3, "name": "Member 3"},
                {"serial": "MEMBER-4", "position": None, "name": "Sequential member"},
            ],
        )

        assert list(virtual_chassis.members.order_by("vc_position").values_list("serial", "vc_position")) == [
            ("MASTER", 1),
            ("MEMBER-2", 2),
            ("MEMBER-3", 3),
            ("MEMBER-4", 4),
        ]

    def test_master_serial_entry_is_not_duplicated(self):
        _master, virtual_chassis = self._create(
            "master-serial",
            [
                {"serial": "MASTER", "position": 2, "name": "Duplicate master"},
                {"serial": "MEMBER", "position": 3, "name": "Member"},
            ],
        )

        assert list(virtual_chassis.members.values_list("serial", flat=True)).count("MASTER") == 1
        assert virtual_chassis.members.filter(serial="MEMBER", vc_position=3).exists()

    def test_server_key_is_part_of_domain(self):
        master, virtual_chassis = self._create("server-domain", [], server_key="production")

        assert virtual_chassis.domain == f"librenms-production-{master.pk}"

    def test_omitted_server_key_uses_plain_domain_prefix(self):
        master, virtual_chassis = self._create("default-domain", [], server_key=None)

        assert virtual_chassis.domain == f"librenms-{master.pk}"


@pytest.mark.django_db
class TestLoadVcMemberNamePattern:
    DEFAULT = "-M{position}"

    @staticmethod
    def _call():
        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern

        return _load_vc_member_name_pattern()

    @staticmethod
    def _set_pattern(value):
        from netbox_librenms_plugin.models import LibreNMSSettings

        settings, _ = LibreNMSSettings.objects.get_or_create(pk=1)
        settings.vc_member_name_pattern = value
        settings.save()

    def test_returns_configured_pattern(self):
        self._set_pattern("-SW{position}")
        assert self._call() == "-SW{position}"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_pattern_uses_default(self, value):
        self._set_pattern(value)
        assert self._call() == self.DEFAULT

    def test_missing_settings_uses_default(self):
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.all().delete()
        assert self._call() == self.DEFAULT


@pytest.mark.django_db
class TestGenerateVcMemberName:
    @staticmethod
    def _call(master_name, position, serial=None, pattern=None):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        return _generate_vc_member_name(master_name, position, serial=serial, pattern=pattern)

    def test_explicit_pattern_and_serial_are_formatted(self):
        assert self._call("switch01", 2, serial="ABC123", pattern="-SW{position}-{serial}") == "switch01-SW2-ABC123"

    def test_none_pattern_reads_real_settings(self):
        from netbox_librenms_plugin.models import LibreNMSSettings

        settings, _ = LibreNMSSettings.objects.get_or_create(pk=1)
        settings.vc_member_name_pattern = "-STACK{position}"
        settings.save()

        assert self._call("core01", 3) == "core01-STACK3"

    @pytest.mark.parametrize("pattern", ["{position!z}", "-{unknown_key}"])
    def test_malformed_pattern_uses_default(self, pattern):
        assert self._call("switch01", 2, pattern=pattern) == "switch01-M2"


@pytest.mark.django_db
class TestStackMemberSerials:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, "0"), (123456, "123456"), (None, ""), ("-", ""), ("  SN-1  ", "SN-1")],
    )
    def test_member_creation_normalizes_serials(self, value, expected):
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.tests.conftest import make_device

        master = make_device("serial-boundary-master", serial="MASTER")
        virtual_chassis = create_virtual_chassis_with_members(
            master,
            [{"serial": value, "position": 2, "name": "Member 2"}],
            {"device_id": master.pk},
            server_key="default",
        )

        assert virtual_chassis.members.count() == 2
        assert virtual_chassis.members.get(vc_position=2).serial == expected
