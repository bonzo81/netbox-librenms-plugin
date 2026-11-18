"""Real-model coverage for virtual chassis creation and naming helpers."""

import pytest


@pytest.mark.django_db
class TestCreateVirtualChassisWithMembers:
    @staticmethod
    def _create(tag, members, *, server_key="default", master_serial="MASTER"):
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.tests.conftest import make_device

        master = make_device(f"{tag}-master", serial=master_serial)
        master_row = {"serial": master_serial, "position": 1, "name": "Master"}
        virtual_chassis = create_virtual_chassis_with_members(
            master,
            [master_row, *members],
            {"device_id": master.pk},
            server_key=server_key,
            master_member=master_row,
        )
        return master, virtual_chassis

    def test_a_shared_position_numbers_the_stack_in_row_order(self):
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

    def test_a_missing_position_numbers_the_stack_in_row_order(self):
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
        from netbox_librenms_plugin.import_utils.virtual_chassis import create_virtual_chassis_with_members
        from netbox_librenms_plugin.tests.conftest import make_device

        master = make_device("serial-boundary-master", serial="MASTER")
        members = [
            {"serial": "MASTER", "position": 1, "name": "Master"},
            {"serial": value, "position": 2, "name": "Member 2"},
        ]
        virtual_chassis = create_virtual_chassis_with_members(
            master, members, {"device_id": master.pk}, server_key="default", master_member=members[0]
        )

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
            "vc_data": {"is_stack": True, "master_identified": False, "member_count": 2, "members": members},
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
