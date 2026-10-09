"""
Virtual Chassis member positions are the member numbers the device reports, 0 included.

Junos numbers its members from 0 and names their ports ``ge-<member>/0/0``. NetBox's own
``{vc_position}`` placeholder makes the same assumption, so a member must sit at the number
that its interface names carry. A 1-based stack keeps its numbering unchanged.
"""

import pytest

from netbox_librenms_plugin.data_shapes.envelope import unwrap_response
from netbox_librenms_plugin.tests.conftest import make_superuser, make_virtual_chassis_members
from netbox_librenms_plugin.tests.recordings import load_recording
from netbox_librenms_plugin.tests.test_bulk_import_job_control import _libre_device, _prerequisites

pytestmark = pytest.mark.django_db


def _import_recording(live_librenms, recording, tag):
    """Import the recording's device through the real bulk import and return its Virtual Chassis."""
    from dcim.models import VirtualChassis

    from netbox_librenms_plugin.import_utils.bulk_import import bulk_import_devices_shared

    device_id = recording["device_id"]
    reported = unwrap_response(recording["responses"][f"GET /api/v0/devices/{device_id}"])[1]["devices"][0]
    prerequisites = _prerequisites(tag)
    row = _libre_device(
        device_id,
        f"{tag}-{device_id}",
        hardware=prerequisites["hardware"],
        serial=reported["serial"],
        location=prerequisites["location"],
    )
    live_librenms.server.load_recording(recording)
    live_librenms.server.register(f"/api/v0/devices/{device_id}", {"status": "ok", "devices": [row]}, method="GET")

    result = bulk_import_devices_shared(
        [device_id],
        server_key="default",
        manual_mappings_per_device={device_id: prerequisites},
        libre_devices_cache={device_id: row},
        user=make_superuser(f"{tag}-user"),
    )

    assert result["failed"] == []
    assert result["virtual_chassis_created"] == 1
    return VirtualChassis.objects.get(domain=f"librenms-default-{device_id}")


@pytest.mark.parametrize(
    "recording_name", ["juniper-ex4400-vc-2member", "juniper-vc-2member", "cisco-stackwise-3member"]
)
def test_an_imported_stack_puts_each_member_at_its_reported_number(live_librenms, recording_name):
    from netbox_librenms_plugin.utils import get_virtual_chassis_member

    recording = load_recording(recording_name)
    expected = recording["expected"]["virtual_chassis"]

    chassis = _import_recording(live_librenms, recording, recording_name)

    members = {member.vc_position: member for member in chassis.members.all()}
    assert sorted(members) == expected["member_positions"]
    assert [members[position].serial for position in expected["member_positions"]] == expected["member_serials"]
    assert chassis.master.vc_position == expected["master_position"]
    # The first number of a port name is the member that owns the port.
    for position, member in members.items():
        assert get_virtual_chassis_member(chassis.master, f"ge-{position}/0/0") == member


def test_a_missing_member_position_numbers_the_whole_stack_in_row_order(live_librenms):
    """FPC 0 reports no slot and FPC 1 reports slot 1: detection, import and module sync all use 1 and 2."""
    import copy

    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView, _inventory_item_key

    recording = copy.deepcopy(load_recording("juniper-ex4400-vc-2member"))
    for key, value in recording["responses"].items():
        if "/inventory/" in key:
            for row in unwrap_response(value)[1]["inventory"]:
                if row["entPhysicalIndex"] == 120:
                    row["entPhysicalParentRelPos"] = None
    inventory = unwrap_response(recording["responses"]["GET /api/v0/inventory/1002/all"])[1]["inventory"]
    fpc_serials = {row["entPhysicalIndex"]: row["entPhysicalSerialNum"] for row in inventory}

    chassis = _import_recording(live_librenms, recording, "missing-slot")
    detected = detect_virtual_chassis_from_inventory(live_librenms.api, recording["device_id"])

    assert [(m["index"], m["position"]) for m in detected["members"]] == [(120, 1), (121, 2)]
    members = {member.vc_position: member for member in chassis.members.all()}
    assert {position: member.serial for position, member in members.items()} == {
        1: fpc_serials[120],
        2: fpc_serials[121],
    }
    index_map = {row["entPhysicalIndex"]: row for row in inventory}
    _default, contexts = BaseModuleTableView._build_inventory_ignore_contexts(
        chassis.master, inventory, index_map, list(members.values()), lambda _manufacturer: []
    )
    psu = index_map[4]
    assert psu["entPhysicalDescr"].startswith("FPC 1 ")
    assert contexts[_inventory_item_key(psu)]["selected_device"] == members[2]


class TestOneBasedStackKeepsPortZeroLocal:
    """On a stack numbered 1, 2, 3 a port named ``...0/0`` is the viewed member's own port."""

    def test_member_resolution_keeps_port_zero_on_the_viewed_device(self):
        from netbox_librenms_plugin.utils import get_virtual_chassis_member

        _chassis, members = make_virtual_chassis_members("cat9300-mgmt", count=3)
        by_position = {member.vc_position: member for member in members}

        for viewed in members:
            assert get_virtual_chassis_member(viewed, "GigabitEthernet0/0") == viewed
            assert get_virtual_chassis_member(viewed, "GigabitEthernet0/0", by_position) == viewed
            assert get_virtual_chassis_member(viewed, "GigabitEthernet2/0/1", by_position) == by_position[2]

    def test_the_rename_helpers_leave_port_zero_unchanged(self):
        from netbox_librenms_plugin.utils import get_vc_member_positions, rewrite_interface_name_for_vc_member

        _chassis, members = make_virtual_chassis_members("cat9300-rename", count=3)

        positions = get_vc_member_positions(members[1])

        assert positions == {1, 2, 3}
        assert rewrite_interface_name_for_vc_member("GigabitEthernet0/0", 2, positions) is None
        assert rewrite_interface_name_for_vc_member("GigabitEthernet1/0/1", 2, positions) == "GigabitEthernet2/0/1"

    def test_interface_sync_rows_keep_port_zero_on_the_viewed_device(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        _chassis, members = make_virtual_chassis_members("cat9300-rows", count=3)
        viewed = members[0]
        snapshot = {
            "ports": [
                {"port_id": 9601, "ifName": "GigabitEthernet0/0", "ifType": "ethernetCsmacd"},
                {"port_id": 9602, "ifName": "GigabitEthernet3/0/1", "ifType": "ethernetCsmacd"},
            ]
        }
        view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        view.request = make_request("get", user=make_superuser("cat9300-rows-user"))

        view.get_context_data(view.request, viewed, "ifName", "default", fresh_data=snapshot, sync_device=viewed)

        assert snapshot["ports"][0]["selected_object_id"] == viewed.pk
        assert snapshot["ports"][1]["selected_object_id"] == members[2].pk


class TestZeroBasedMemberRename:
    """The VC-aware rename moves a name to member 0 like to any other member."""

    def _zero_based_chassis(self, tag):
        _chassis, members = make_virtual_chassis_members(tag, count=2)
        for member in members:
            member.vc_position -= 1
            member.save(update_fields=["vc_position"])
        return members

    def test_member_zero_is_a_known_position(self):
        from netbox_librenms_plugin.utils import get_vc_member_positions

        members = self._zero_based_chassis("junos-positions")

        assert get_vc_member_positions(members[0]) == {0, 1}

    @pytest.mark.parametrize(
        ("name", "position", "expected"),
        [
            ("Te1/1/1", 0, "Te0/1/1"),
            ("Te0/1/1", 1, "Te1/1/1"),
            ("xe-1/2/0", 0, "xe-0/2/0"),
            ("xe-0/2/0", 1, "xe-1/2/0"),
        ],
    )
    def test_a_name_moves_to_the_selected_member(self, name, position, expected):
        from netbox_librenms_plugin.utils import rewrite_interface_name_for_vc_member

        assert rewrite_interface_name_for_vc_member(name, position, {0, 1}) == expected
