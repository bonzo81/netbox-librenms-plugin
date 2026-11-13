"""Real-model coverage for virtual chassis creation and naming helpers."""

import pytest

from netbox_librenms_plugin.tests.conftest import transactional_db_with_all_apps


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

    def test_numeric_member_serials_that_compare_equal_keep_their_own_text(self):
        """The JSON numbers 123 and 123.0 are one Python dict key, but "123.0" is a serial another device holds."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import create_prepared_stack, make_device

        make_device("numeric-serial-holder", serial="123.0")
        master = make_device("numeric-serial-master", serial="MASTER")
        members = [
            {"serial": "MASTER", "position": 1, "name": "Master"},
            {"serial": 123, "position": 2, "name": "Member 2"},
            {"serial": 123.0, "position": 3, "name": "Member 3"},
        ]

        virtual_chassis = create_prepared_stack(master, members, {"device_id": master.pk}, server_key="default")

        assert sorted(virtual_chassis.members.values_list("serial", flat=True)) == ["123", "MASTER"]
        assert Device.objects.filter(serial="123.0").count() == 1


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


def _register_inventory(live_librenms, device_id, root, children):
    live_librenms.server.register(
        f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [{"device_id": device_id, "hostname": "vc-rules"}]}
    )
    live_librenms.server.vc_inventory_callable(device_id, [root], {root["entPhysicalIndex"]: children})


def _row(index, row_class, descr, serial=""):
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": row_class,
        "entPhysicalDescr": descr,
        "entPhysicalName": descr,
        "entPhysicalSerialNum": serial,
        "entPhysicalParentRelPos": index - 1,
    }


@pytest.mark.django_db
@pytest.mark.parametrize("root_descr", ["Chassis", "Juniper EX4400 Virtual Chassis"], ids=["chassis", "junos-root"])
def test_a_root_with_one_psu_is_no_stack_beside_a_broken_serial_rule(live_librenms, root_descr):
    """The shape is no stack before any serial is compared, so a broken rule of another manufacturer is not read."""
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory
    from netbox_librenms_plugin.tests.test_coverage_device_operations import _broken_serial_rule

    _broken_serial_rule("vc-psu")
    _register_inventory(
        live_librenms, 7101, _row(1, "chassis", root_descr, "ROOT-7101"), [_row(2, "powerSupply", "PSU 0")]
    )

    assert detect_virtual_chassis_from_inventory(live_librenms.api, 7101) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("root_serial", "is_stack", "failed"),
    [("FPC-A-7102", True, False), ("ZZ FPC-A-7102", False, True)],
    ids=["global-rules-decide", "broken-rule-decides"],
)
def test_a_junos_fpc_decision_reads_a_broken_rule_only_when_it_needs_it(live_librenms, root_serial, is_stack, failed):
    """The global rules explain the first stack; only the broken chain could explain the second, so it fails closed."""
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory
    from netbox_librenms_plugin.tests.test_coverage_device_operations import _broken_serial_rule

    _broken_serial_rule("vc-fpc")
    fpcs = [
        _row(2, "container", "FPC: EX4400 @ 0/*/*", "FPC-A-7102"),
        _row(3, "container", "FPC: EX4400 @ 1/*/*", "FPC-B-7102"),
    ]
    _register_inventory(live_librenms, 7102, _row(1, "chassis", "Juniper EX4400 Virtual Chassis", root_serial), fpcs)

    result = detect_virtual_chassis_from_inventory(live_librenms.api, 7102)

    assert (bool(result and result["is_stack"]), bool(result and result["detection_failed"])) == (is_stack, failed)


@transactional_db_with_all_apps()
def test_member_creation_reads_the_master_maker_under_the_type_row_lock():
    """A maker change of the master type after the context read must wait, or "XMEMBER" would duplicate "MEMBER"."""
    from dcim.models import Device, DeviceType, Manufacturer
    from django.db import OperationalError, connection

    from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_device, prepared_stack
    from netbox_librenms_plugin.tests.lock_conflict_helpers import second_connection

    make_device("vc-type-race-holder", serial="MEMBER-VC-RACE")
    master = make_device("vc-type-race-master", serial="MASTER-VC-RACE")
    plain_maker = Manufacturer.objects.create(name="VC race plain maker", slug="vc-race-plain-maker")
    rule_maker = Manufacturer.objects.create(name="VC race rule maker", slug="vc-race-rule-maker")
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=rule_maker
    )
    master.device_type = DeviceType.objects.create(
        manufacturer=plain_maker, model="VC race model", slug="vc-race-model"
    )
    master.save()
    members = [{"serial": "MASTER-VC-RACE", "position": 1}, {"serial": "XMEMBER-VC-RACE", "position": 2}]
    prepared, master_member = prepared_stack(master, members)
    attempts = []

    def change_after_the_type_read(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if not attempts and 'FROM "dcim_devicetype"' in sql and connection.in_atomic_block:
            with second_connection() as other, other.cursor() as cursor:
                cursor.execute("SET lock_timeout = '200ms'")
                try:
                    cursor.execute(
                        'UPDATE "dcim_devicetype" SET "manufacturer_id" = %s WHERE id = %s',
                        [rule_maker.pk, master.device_type_id],
                    )
                except OperationalError as exc:
                    # Only a lock timeout proves the wait; any other error fails the test.
                    if getattr(exc.__cause__, "sqlstate", None) != "55P03":
                        raise
                    attempts.append("waited")
                else:
                    other.commit()
                    attempts.append("committed")
        return result

    with connection.execute_wrapper(change_after_the_type_read):
        chassis = create_virtual_chassis_with_members(
            master, prepared, {"device_id": 7103}, server_key="default", master_member=master_member
        )

    # The type row lock holds the maker that decided the member context until the members exist.
    assert attempts == ["waited"]
    assert DeviceType.objects.get(pk=master.device_type_id).manufacturer_id == plain_maker.pk
    assert sorted(chassis.members.values_list("serial", flat=True)) == ["MASTER-VC-RACE", "XMEMBER-VC-RACE"]
    assert Device.objects.filter(serial="MEMBER-VC-RACE").count() == 1
