"""Real-model coverage for virtual chassis creation and naming helpers."""

import pytest


@pytest.mark.django_db
class TestCreateVirtualChassisWithMembers:
    @staticmethod
    def _create(tag, members, *, server_key="default", master_serial="MASTER"):
        from netbox_librenms_plugin.tests.conftest import create_prepared_stack, make_device

        master = make_device(f"{tag}-master", serial=master_serial)
        master_row = {"serial": master_serial, "position": 1, "name": "Master"}
        virtual_chassis = create_prepared_stack(
            master, [master_row, *members], {"device_id": master.pk}, server_key=server_key
        )
        return master, virtual_chassis

    @pytest.mark.parametrize("second", [1, None, "2"], ids=["shared", "missing", "text"])
    def test_a_stack_whose_positions_are_not_numbered_for_import_is_refused_before_any_write(self, second):
        """Creation writes the prepared numbers; it never numbers a stack itself."""
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.tests.conftest import make_device

        master = make_device("unnumbered-stack-master", serial="MASTER")
        members = [
            {"serial": "MASTER", "position": 1, "name": "Master", "is_master": True},
            {"serial": "MEMBER-2", "position": second, "name": "Member 2", "is_master": False},
        ]
        prepared = {"members": members, "device_type_id": master.device_type_id}

        with pytest.raises(ValueError, match="not numbered for import"):
            create_virtual_chassis_with_members(master, prepared, {"device_id": master.pk}, master_member=members[0])

        assert not VirtualChassis.objects.exists()

    def test_a_stack_prepared_for_another_device_type_is_refused_before_any_write(self):
        from dcim.models import DeviceType, VirtualChassis

        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.tests.conftest import make_device, prepared_stack

        master = make_device("other-type-stack-master", serial="MASTER")
        prepared, master_member = prepared_stack(
            master, [{"serial": "MASTER", "position": 1}, {"serial": "MEMBER-2", "position": 2}]
        )
        other = DeviceType.objects.create(manufacturer=master.device_type.manufacturer, model="Other", slug="other")

        with pytest.raises(ValueError, match="another device type"):
            create_virtual_chassis_with_members(
                master, {**prepared, "device_type_id": other.pk}, {"device_id": master.pk}, master_member=master_member
            )

        assert not VirtualChassis.objects.exists()

    def test_two_rows_with_the_master_serial_name_no_master(self):
        """No single member is the master, so the import creates no chassis and no duplicate device."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import identify_vc_master
        from netbox_librenms_plugin.serial_identity import SerialRules

        members = [
            {"serial": "MASTER", "position": 1, "name": "Master"},
            {"serial": "MASTER", "position": 2, "name": "Duplicate master"},
            {"serial": "MEMBER", "position": 3, "name": "Member"},
        ]

        assert identify_vc_master(members, "MASTER", SerialRules.load().context(None)) is None

    def test_server_key_is_part_of_domain(self):
        master, virtual_chassis = self._create("server-domain", [], server_key="production")

        assert virtual_chassis.domain == f"librenms-production-{master.pk}"

    def test_omitted_server_key_uses_plain_domain_prefix(self):
        master, virtual_chassis = self._create("default-domain", [], server_key=None)

        assert virtual_chassis.domain == f"librenms-{master.pk}"


@pytest.mark.django_db
class TestStackMemberSerials:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, "0"), (123456, "123456"), (None, ""), ("-", ""), ("  SN-1  ", "SN-1")],
    )
    def test_member_creation_normalizes_serials(self, value, expected):
        from netbox_librenms_plugin.tests.conftest import create_prepared_stack, make_device

        master = make_device("serial-boundary-master", serial="MASTER")
        members = [
            {"serial": "MASTER", "position": 1, "name": "Master"},
            {"serial": value, "position": 2, "name": "Member 2"},
        ]
        virtual_chassis = create_prepared_stack(master, members, {"device_id": master.pk}, server_key="default")

        assert virtual_chassis.members.count() == 2
        assert virtual_chassis.members.get(vc_position=2).serial == expected


@pytest.mark.parametrize("serials", [["OTHER", "MEMBER"], ["MASTER", "MASTER"]])
def test_the_vc_modal_explains_that_no_unique_member_matches(serials):
    from django.template.loader import render_to_string

    members = [{"name": f"Member {index}", "serial": serial} for index, serial in enumerate(serials)]
    content = render_to_string(
        "netbox_librenms_plugin/htmx/device_vc_details.html",
        {
            "libre_device": {"hostname": "test-device"},
            "vc_data": {
                "is_stack": True,
                "numbering_available": True,
                "master_identified": False,
                "member_count": 2,
                "members": members,
            },
        },
    )

    assert "No unique member matches the device serial" in content
