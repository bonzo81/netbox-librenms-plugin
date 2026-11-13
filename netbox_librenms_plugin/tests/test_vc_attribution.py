"""
Module-sync member attribution through its module interface, ``attribute_inventory``.

Plain ENTITY-MIB rows and real NetBox member Devices; the modules-tab tests in
``test_modules_view.py`` cover the same rules end to end.
"""

import pytest

from netbox_librenms_plugin.import_utils.virtual_chassis import attribute_inventory
from netbox_librenms_plugin.serial_identity import SerialRules
from netbox_librenms_plugin.tests.conftest import junos_vc_inventory, make_junos_vc_members

pytestmark = pytest.mark.django_db


def _owners(page, members, inventory):
    """Return ``{entPhysicalIndex: owning Device}``; an unowned row stays on *page*, as the modules tab shows it."""
    owners = attribute_inventory(inventory, members, SerialRules.load().for_device(page), "reported")
    return {row["entPhysicalIndex"]: member or page for row, (member, _source) in zip(inventory, owners, strict=True)}


def _attribution(page, members, row):
    """Return ``(owning Device, source)`` for *row*, a member row of a stack whose other member rows have no serial."""
    taken = {row["entPhysicalParentRelPos"]}
    others = [
        _row(900 + member.vc_position + 1, "chassis", 900, position=member.vc_position)
        for member in members
        if member.vc_position not in taken
    ]
    inventory = [_row(900, "stack", 0), {**row, "entPhysicalClass": "chassis", "entPhysicalContainedIn": 900}, *others]
    member, source = attribute_inventory(inventory, members, SerialRules.load().for_device(page), "reported")[1]
    return member or page, source


def _row(index, entity_class, contained_in, position=None, **fields):
    row = {"entPhysicalIndex": index, "entPhysicalClass": entity_class, "entPhysicalContainedIn": contained_in}
    if position is not None:
        row["entPhysicalParentRelPos"] = position
    return {"entPhysicalName": f"{entity_class} {index}", "entPhysicalModelName": "", **row, **fields}


def _vc_members(serials):
    """Return real VC members at positions 1, 2, ... with *serials*."""
    from dcim.models import VirtualChassis

    from netbox_librenms_plugin.tests.conftest import make_device

    vc = VirtualChassis.objects.create(name=f"vc-infer-{'-'.join(serials)}")
    members = []
    for pos, serial in enumerate(serials, start=1):
        dev = make_device(f"vc-infer-member-{serial}", serial=serial)
        dev.virtual_chassis = vc
        dev.vc_position = pos
        dev.save()
        members.append(dev)
    vc.master = members[0]
    vc.save()
    return members


def _zero_based_members(tag):
    """Return two real VC members at positions 0 and 1 whose NetBox serials are blank."""
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

    _chassis, members = make_virtual_chassis_members(tag, count=2)
    for member in members:
        member.vc_position -= 1
        member.serial = ""
        member.save(update_fields=["vc_position", "serial"])
    return members


class TestSerialEvidence:
    """A member row's own serial decides first; serials can arrive as JSON numbers."""

    def test_numeric_item_serial_matches_the_member_stored_as_text(self):
        """An all-digit ENTITY serial arriving as an int must still resolve to its VC member instead of raising."""
        master, member2 = _vc_members(["100001", "100002"])
        item = {"entPhysicalIndex": 1, "entPhysicalSerialNum": 100002, "entPhysicalParentRelPos": 2}

        target, source = _attribution(master, [master, member2], item)

        assert target.pk == member2.pk
        assert source == "serial"

    def test_a_serial_without_three_letters_or_digits_is_no_serial_evidence(self):
        """A serial of JSON number 0 has no hardware identity, so it never attributes a row to a member."""
        master, member2 = _vc_members(["0", "100003"])
        item = {"entPhysicalIndex": 2, "entPhysicalSerialNum": 0, "entPhysicalParentRelPos": 1}

        target, source = _attribution(member2, [master, member2], item)

        assert (target, source) == (master, "position")

    def test_a_decorated_serial_is_serial_evidence(self):
        """Juniper reports "S/N 100007"; the serial rules strip the mark, so it matches the stored "100007"."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        first, second = _vc_members(["100006", "100007"])
        item = {"entPhysicalIndex": 6, "entPhysicalSerialNum": "S/N 100007", "entPhysicalParentRelPos": 2}

        target, source = _attribution(first, [first, second], item)

        assert target.pk == second.pk
        assert source == "serial"

    def test_a_default_serial_is_no_serial_evidence(self):
        """LibreNMS reports "default" for no serial, so a member stored with it claims nothing."""
        first, second = _vc_members(["100014", "default"])
        item = {"entPhysicalIndex": 7, "entPhysicalSerialNum": "default", "entPhysicalParentRelPos": 1}

        target, source = _attribution(second, [first, second], item)

        assert target.pk == first.pk
        assert source == "position"

    @pytest.mark.parametrize("stored", [("100015", "S/N 100015"), ("S/N 100015", "100015")])
    def test_two_members_with_one_serial_key_are_no_serial_evidence(self, stored):
        """Both members normalize to one key, so the serial names neither and the position decides."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        first, second = _vc_members(list(stored))
        item = {"entPhysicalIndex": 8, "entPhysicalSerialNum": "100015", "entPhysicalParentRelPos": 1}

        target, source = _attribution(second, [first, second], item)

        assert target.pk == first.pk
        assert source == "position"


class TestNumberingRefusals:
    """Rows of a virtual chassis get a member only from a stack whose member rows agree with the stored members."""

    @pytest.mark.parametrize(
        "inventory",
        [
            [_row(3, "module", 0, entPhysicalName="2/1/1")],
            [_row(130, "stack", 0), _row(131, "chassis", 130, position=2), _row(132, "module", 131)],
            [_row(170, "container", 0, position=0), _row(171, "chassis", 170, position=1), _row(172, "fan", 171)],
            [_row(190, "chassis", 0, position=1), _row(191, "powerSupply", 190)],
        ],
        ids=["lone-row", "one-chassis-under-a-stack", "one-chassis-under-a-container", "lone-chassis"],
    )
    def test_an_inventory_without_a_recognized_stack_is_refused(self, inventory):
        from netbox_librenms_plugin.import_utils.virtual_chassis import VCNumberingError

        page, member = _vc_members(["100010", "100011"])

        with pytest.raises(VCNumberingError):
            attribute_inventory(inventory, [page, member], SerialRules.load().for_device(page), "reported")

    def test_a_member_row_position_that_no_member_holds_is_refused(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import VCNumberingError

        page, member = _vc_members(["100016", "100017"])
        inventory = [
            _row(140, "stack", 0),
            _row(141, "chassis", 140, position=0),
            _row(142, "chassis", 140, position=1),
        ]

        with pytest.raises(VCNumberingError):
            attribute_inventory(inventory, [page, member], SerialRules.load().for_device(page), "reported")

    def test_the_numbering_mode_projects_the_member_rows(self):
        """Under "number from 1" the rows reported at 0 and 1 belong to the members at 1 and 2."""
        page, member = _vc_members(["100018", "100019"])
        inventory = [
            _row(150, "stack", 0),
            _row(151, "chassis", 150, position=0),
            _row(152, "chassis", 150, position=1),
            _row(153, "module", 151),
        ]

        owners = attribute_inventory(inventory, [page, member], SerialRules.load().for_device(page), "one_based")

        assert [owner for owner, _source in owners] == [None, page, member, page]

    def test_a_device_outside_a_virtual_chassis_keeps_every_row_on_its_page(self):
        owners = attribute_inventory([_row(3, "module", 0, entPhysicalName="2/1/1")], [], None, None)

        assert owners == [(None, "default")]


class TestRecognizedStack:
    """Only a member row gives a member by position; its subtree inherits it."""

    @pytest.mark.parametrize("page_position", [0, 1])
    def test_chassis_stack_members_own_their_subtrees_from_any_page(self, page_position):
        """Each chassis member row owns its subtree by its position, whichever member's page is open."""
        first, second = _zero_based_members(f"chassis-stack-{page_position}")
        page = (first, second)[page_position]
        inventory = [
            _row(180, "chassis", 0),
            _row(181, "chassis", 180, position=0),
            _row(182, "chassis", 180, position=1),
            _row(183, "powerSupply", 181, entPhysicalModelName="PSU-0"),
            _row(184, "powerSupply", 182, entPhysicalModelName="PSU-1"),
            # A row directly under the stack root is no member row, so its slot number is no member id.
            _row(185, "fan", 180, position=1, entPhysicalModelName="FAN"),
        ]

        owner = _owners(page, [first, second], inventory)

        assert (owner[181], owner[183]) == (first, first)
        assert (owner[182], owner[184]) == (second, second)
        assert owner[180] == page
        assert owner[185] == page

    def test_junos_member_attribution_skips_routing_engine_numbers(self):
        """A fan names its member in the description; a Routing Engine number is not a member id."""
        master, member = make_junos_vc_members("junos-attribution")

        owner = _owners(master, [master, member], junos_vc_inventory())

        # Fan tray parent positions are 10 and 11 for member 1, so only the description names it.
        assert owner[30] == member
        assert owner[121] == member
        assert owner[271] == master

    def test_a_junos_row_inherits_the_nearer_serial_over_its_fpc(self):
        """A module below FPC 0 that carries member 1's serial takes its serial-less child to member 1."""
        master, member = make_junos_vc_members("junos-nearer-serial")
        inventory = [
            *junos_vc_inventory(),
            _row(1201, "module", 120, position=0, entPhysicalSerialNum="12350", entPhysicalModelName="MOVED"),
            _row(1202, "powerSupply", 1201, entPhysicalModelName="PSU"),
        ]

        owner = _owners(master, [master, member], inventory)

        assert owner[1201] == member
        assert owner[1202] == member

    def test_an_fpc_whose_serial_names_the_member_at_another_position_is_refused(self):
        """NetBox numbers the members the other way round, so no row of the inventory gets a member."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import VCNumberingError

        master, member = make_junos_vc_members("junos-swapped")
        # FPC 1 (slot 1, position 1) carries the serial of the NetBox device at position 0, and the reverse.
        master.serial, member.serial = "12350", "12345"
        for device in (master, member):
            device.save(update_fields=["serial"])

        with pytest.raises(VCNumberingError):
            _owners(member, [master, member], junos_vc_inventory())

    def test_junos_fpcs_sharing_a_slot_follow_the_detected_positions(self):
        """Two FPCs that both report slot 1 get positions 1 and 2 from detection; module sync must agree."""
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

        _chassis, (first, second) = make_virtual_chassis_members("junos-shared-slot", count=2)
        for device in (first, second):
            device.serial = ""
            device.save(update_fields=["serial"])
        fpc = {"entPhysicalDescr": "FPC: EX4400-24X @ 1/*/*", "entPhysicalName": "EX4400-24X-S"}
        inventory = [
            _row(1, "chassis", 0, entPhysicalDescr="Juniper Virtual Chassis Switch", entPhysicalSerialNum="SN-AAA"),
            _row(120, "container", 1, position=1, entPhysicalSerialNum="SN-AAA", **fpc),
            _row(121, "container", 1, position=1, entPhysicalSerialNum="SN-BBB", **fpc),
            _row(1210, "module", 121, position=2, entPhysicalModelName="PIC"),
            # "FPC 1" names a slot that two FPCs share, so it names no member.
            _row(4, "powerSupply", 1, position=2, entPhysicalDescr="FPC 1 Power Supply 0", entPhysicalModelName="PSU"),
        ]

        owner = _owners(second, [first, second], inventory)

        assert owner[120] == first
        assert (owner[121], owner[1210]) == (second, second)
        assert owner[4] == second


@pytest.mark.parametrize("stored", ["ABC-SMD", "S/N ABC-SMD"])
def test_serial_matches_device_keys_the_serial_of_the_attributed_member(stored):
    """A chassis-level "S/N ABC" row matches a member stored as "ABC" or as "S/N ABC" under the seeded rule."""
    from netbox_librenms_plugin.inventory_policy import load_inventory_policy
    from netbox_librenms_plugin.models import InventoryIgnoreRule, NormalizationRule
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView, _check_ignore_rules

    NormalizationRule.objects.get_or_create(
        scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
    )
    InventoryIgnoreRule.objects.all().delete()
    InventoryIgnoreRule.objects.create(
        name="Hide the member board", match_type="serial_matches_device", pattern="", action="skip"
    )
    page, member = _vc_members(["PAGE-SMD", stored])
    row = _row(5, "module", 900, entPhysicalSerialNum="S/N ABC-SMD")
    stack = [_row(900, "chassis", 0), _row(901, "chassis", 900, position=1), _row(902, "chassis", 900, position=2)]
    policy = load_inventory_policy(page.pk)

    _default, contexts = BaseModuleTableView._build_inventory_ignore_contexts(
        policy.page_device, [*stack, row], policy.vc_members, policy
    )

    context = contexts[("index", 5)]
    assert context["selected_device"].pk == member.pk
    index_map = {item["entPhysicalIndex"]: item for item in [*stack, row]}
    assert _check_ignore_rules(row, stack[0], context["ignore_rules"], index_map, context["device_identity"]) == "skip"
