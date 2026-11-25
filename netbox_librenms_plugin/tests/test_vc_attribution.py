"""
Module-sync member attribution through its module interface, ``attribute_inventory``.

Plain ENTITY-MIB rows and real NetBox member Devices; the modules-tab tests in
``test_modules_view.py`` cover the same rules end to end.
"""

import pytest

from netbox_librenms_plugin.import_utils.virtual_chassis import attribute_inventory
from netbox_librenms_plugin.tests.conftest import junos_vc_inventory, make_junos_vc_members

pytestmark = pytest.mark.django_db


def _owners(page, members, inventory):
    """Return ``{entPhysicalIndex: owning Device}``; an unowned row stays on *page*, as the modules tab shows it."""
    from netbox_librenms_plugin.serial_identity import SerialRules

    owners = attribute_inventory(inventory, members, SerialRules.load().for_device(page))
    return {row["entPhysicalIndex"]: member or page for row, (member, _source) in zip(inventory, owners, strict=True)}


def _attribution(page, members, row):
    """Return ``(owning Device, source)`` for one row on its own."""
    from netbox_librenms_plugin.serial_identity import SerialRules

    member, source = attribute_inventory([row], members, SerialRules.load().for_device(page))[0]
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
    """A row's own serial decides first; serials can arrive as JSON numbers."""

    def test_numeric_item_serial_matches_the_member_stored_as_text(self):
        """An all-digit ENTITY serial arriving as an int must still resolve to its VC member instead of raising."""
        master, member2 = _vc_members(["100001", "100002"])
        item = {"entPhysicalIndex": 1, "entPhysicalSerialNum": 100002, "entPhysicalContainedIn": 0}

        target, source = _attribution(master, [master, member2], item)

        assert target.pk == member2.pk
        assert source == "serial"

    def test_a_serial_without_three_letters_or_digits_is_no_serial_evidence(self):
        """A serial of JSON number 0 has no hardware identity, so it never attributes a row to a member."""
        master, member2 = _vc_members(["0", "100003"])
        item = {"entPhysicalIndex": 2, "entPhysicalSerialNum": 0, "entPhysicalContainedIn": 0}

        _target, source = _attribution(member2, [master, member2], item)

        assert source != "serial"

    def test_a_decorated_serial_is_serial_evidence(self):
        """Juniper reports "S/N 100007"; the serial rules strip the mark, so it matches the stored "100007"."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        first, second = _vc_members(["100006", "100007"])
        item = {"entPhysicalIndex": 6, "entPhysicalSerialNum": "S/N 100007", "entPhysicalParentRelPos": 1}

        target, source = _attribution(first, [first, second], {**item, "entPhysicalContainedIn": 0})

        assert target.pk == second.pk
        assert source == "serial"

    def test_a_default_serial_is_no_serial_evidence(self):
        """LibreNMS reports "default" for no serial, so a member stored with it claims nothing."""
        first, second = _vc_members(["100014", "default"])
        item = {"entPhysicalIndex": 7, "entPhysicalSerialNum": "default", "entPhysicalParentRelPos": 1}

        target, source = _attribution(second, [first, second], {**item, "entPhysicalContainedIn": 0})

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

        target, source = _attribution(second, [first, second], {**item, "entPhysicalContainedIn": 0})

        assert target.pk == first.pk
        assert source == "position"


class TestUnrecognizedShape:
    """An inventory that is not a recognized stack keeps the position and name heuristic from 1."""

    @pytest.mark.parametrize("field", ["entPhysicalName", "entPhysicalDescr"])
    def test_numeric_name_hints_do_not_crash(self, field):
        """Numeric ENTITY hint fields are normalized before prefix matching."""
        master, member2 = _vc_members(["100004", "100005"])
        item = {"entPhysicalIndex": 3, field: 2, "entPhysicalContainedIn": 0}

        target, source = _attribution(master, [master, member2], item)

        assert target.pk == master.pk
        assert source == "default"

    def test_a_name_hint_for_a_position_no_member_holds_resolves_nothing(self):
        """On a stack numbered from 1, a "0/0" name is a local port, not member 0."""
        first, second = _vc_members(["100008", "100009"])
        item = {"entPhysicalIndex": 5, "entPhysicalName": "0/0", "entPhysicalContainedIn": 0}

        target, source = _attribution(second, [first, second], item)

        assert target.pk == second.pk
        assert source == "default"

    def test_a_descendant_local_position_does_not_override_parent_member(self):
        """A hardware-local child position must inherit its parent's VC member."""
        page, member = _vc_members(["100010", "100011"])
        inventory = [
            {
                "entPhysicalIndex": 120,
                "entPhysicalClass": "module",
                "entPhysicalName": "1/FPC0",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 121,
                "entPhysicalClass": "fan",
                "entPhysicalName": "Fan 2",
                "entPhysicalParentRelPos": 2,
                "entPhysicalContainedIn": 120,
            },
        ]

        owner = _owners(page, [page, member], inventory)

        assert owner[120] == page
        assert owner[121] == page

    def test_a_chassis_can_resolve_below_an_unattributed_stack_root(self):
        """A generic stack root must not suppress a chassis member position."""
        from netbox_librenms_plugin.serial_identity import SerialRules

        page, member = _vc_members(["100012", "100013"])
        inventory = [
            {
                "entPhysicalIndex": 130,
                "entPhysicalClass": "stack",
                "entPhysicalName": "Switch stack",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 131,
                "entPhysicalClass": "chassis",
                "entPhysicalName": "Chassis 2",
                "entPhysicalParentRelPos": 2,
                "entPhysicalContainedIn": 130,
            },
            {
                "entPhysicalIndex": 132,
                "entPhysicalClass": "module",
                "entPhysicalName": "2/FPC0",
                "entPhysicalContainedIn": 131,
            },
        ]

        owners = attribute_inventory(inventory, [page, member], SerialRules.load().for_device(page))

        assert owners[0] == (None, "default")
        assert owners[1][0] == member
        assert owners[2][0] == member

    @pytest.mark.parametrize("root_class", ["stack", "chassis"])
    @pytest.mark.parametrize("root_name", ["Switch stack", "StackSub-0/0"])
    def test_a_vc_root_at_position_zero_does_not_claim_member_zero(self, root_class, root_name):
        """The root holds the members; its own position 0 or "0/0" name is not member 0."""
        from netbox_librenms_plugin.serial_identity import SerialRules

        first, second = _zero_based_members(f"root-zero-{root_class}-{len(root_name)}")
        inventory = [
            {
                "entPhysicalIndex": 140,
                "entPhysicalClass": root_class,
                "entPhysicalName": root_name,
                "entPhysicalParentRelPos": 0,
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 141,
                "entPhysicalClass": "chassis",
                "entPhysicalName": "Chassis",
                "entPhysicalParentRelPos": 1,
                "entPhysicalContainedIn": 140,
            },
            {
                "entPhysicalIndex": 142,
                "entPhysicalClass": "powerSupply",
                "entPhysicalName": "PSU",
                "entPhysicalContainedIn": 141,
            },
        ]

        # The page is member 1, so a stack child takes its own position 1 and a chassis root's child stays on the page.
        owners = attribute_inventory(inventory, [first, second], SerialRules.load().for_device(second))

        assert owners[0] == (None, "default")
        assert (owners[1][0] or second) == second
        assert (owners[2][0] or second) == second

    def test_a_generic_container_root_at_position_zero_does_not_claim_member_zero(self):
        """An unrecognized shape keeps the base rule: position 0 is no member, so the chassis child uses its own."""
        first, second = _zero_based_members("container-root-zero")
        inventory = [
            _row(170, "container", 0, position=0),
            _row(171, "chassis", 170, position=1),
            _row(172, "powerSupply", 171, entPhysicalModelName="PSU-1"),
        ]

        owner = _owners(first, [first, second], inventory)

        assert owner[171] == second
        assert owner[172] == second

    def test_a_lone_serial_less_chassis_at_position_one_keeps_the_base_attribution(self):
        """One chassis is no stack, so the base rule applies: its position 1 names member 1, not the page."""
        first, second = _zero_based_members("lone-chassis")
        inventory = [
            _row(190, "chassis", 0, position=1),
            _row(191, "powerSupply", 190, entPhysicalModelName="PSU"),
        ]

        owner = _owners(first, [first, second], inventory)

        assert owner[190] == second
        assert owner[191] == second


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

    def test_a_named_junos_psu_follows_its_fpc_serial_over_the_fpc_position(self):
        """NetBox numbers the members the other way round; the FPC serial decides, and its PSU follows it."""
        master, member = make_junos_vc_members("junos-swapped")
        # FPC 1 (slot 1, position 1) carries the serial of the NetBox device at position 0, and the reverse.
        master.serial, member.serial = "12350", "12345"
        for device in (master, member):
            device.save(update_fields=["serial"])

        owner = _owners(member, [master, member], junos_vc_inventory())

        assert owner[121] == master
        assert owner[4] == master
        assert owner[2] == member

    @pytest.mark.parametrize("reported", [False, True], ids=["row-order", "reported"])
    @pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
    def test_a_row_order_position_does_not_attribute_a_member_row_with_a_short_serial(self, reported, reverse):
        """Row order names no member of a serial without an identity, so its subtree is unattributed in any order."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        first, second = _vc_members(["100031", "100032"])
        members = [
            _row(191, "chassis", 190, position=1 if reported else -1, entPhysicalSerialNum="AB-12"),
            _row(192, "chassis", 190, position=2 if reported else -1, entPhysicalSerialNum="CD-34"),
        ]
        inventory = [
            _row(190, "stack", 0),
            *(members[::-1] if reverse else members),
            _row(193, "powerSupply", 191, entPhysicalModelName="PSU-1"),
            _row(194, "powerSupply", 192, entPhysicalModelName="PSU-2"),
        ]

        owners = attribute_inventory(inventory, [first, second], SerialRules.load().for_device(first))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        if reported:
            assert (owner[191], owner[193]) == ((first, "position"), (first, "parent-context"))
            assert (owner[192], owner[194]) == ((second, "position"), (second, "parent-context"))
        else:
            assert {owner[index] for index in (191, 192, 193, 194)} == {(None, UNATTRIBUTED_SOURCE)}

    @pytest.mark.parametrize(
        ("serials", "second"),
        [(("100042", "NEW123"), None), (("100042", "AB-12"), None), (("100041", ""), "position")],
        ids=["unowned-identity-serial", "short-serial", "blank-serial"],
    )
    @pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
    def test_a_row_order_position_attributes_only_a_member_row_with_a_blank_serial(self, serials, second, reverse):
        """Switch 1 owns its member by serial; row order names a member only for a Switch 2 that reports no serial."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        first, last = _vc_members(["100041", "100042"])
        owner_of = {"100041": first, "100042": last}
        members = [
            _row(201, "chassis", 200, position=-1, entPhysicalSerialNum=serials[0]),
            _row(202, "chassis", 200, position=-1, entPhysicalSerialNum=serials[1]),
        ]
        inventory = [
            _row(200, "stack", 0),
            *(members[::-1] if reverse else members),
            _row(203, "powerSupply", 201, entPhysicalModelName="PSU-1"),
            _row(204, "module", 202, entPhysicalModelName="CARD", entPhysicalSerialNum="12"),
            _row(205, "powerSupply", 202, entPhysicalModelName="PSU-2"),
        ]

        owners = attribute_inventory(inventory, [first, last], SerialRules.load().for_device(first))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert (owner[201], owner[203]) == ((owner_of[serials[0]], "serial"), (owner_of[serials[0]], "ancestor-serial"))
        if second is None:
            assert {owner[index] for index in (202, 204, 205)} == {(None, UNATTRIBUTED_SOURCE)}
        elif reverse:
            # Row order gives Switch 2 position 1, whose member Switch 1 owns by serial.
            assert owner[202] == (None, UNATTRIBUTED_SOURCE)
        else:
            # A blank serial keeps the row-order position, as before.
            assert owner[202] == (last, "position")

    @pytest.mark.parametrize(
        ("serial", "position"), [("NEW123", 3), ("", 3), ("100052", 3)], ids=["new", "blank", "owned"]
    )
    def test_a_member_row_that_names_no_netbox_member_is_unattributed(self, serial, position):
        """Member 3 is new to NetBox, so neither it nor its card belongs to the page device."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        first, second = _vc_members(["100051", "100052"])
        inventory = [
            _row(210, "stack", 0),
            _row(211, "chassis", 210, position=1, entPhysicalSerialNum="100051"),
            _row(212, "chassis", 210, position=2, entPhysicalSerialNum="CD-34"),
            _row(213, "chassis", 210, position=position, entPhysicalSerialNum=serial),
            _row(214, "powerSupply", 211, entPhysicalModelName="PSU-1"),
            _row(215, "module", 213, entPhysicalModelName="CARD", entPhysicalSerialNum="CARD123"),
        ]

        owners = attribute_inventory(inventory, [first, second], SerialRules.load().for_device(first))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert owner[214] == (first, "ancestor-serial")
        if serial == "100052":
            assert (owner[213], owner[215]) == ((second, "serial"), (second, "ancestor-serial"))
        else:
            assert {owner[213], owner[215]} == {(None, UNATTRIBUTED_SOURCE)}

    @pytest.mark.parametrize(
        ("first_serial", "second_serial", "positions", "withheld"),
        [
            ("100062", "12", (1, 2), True),
            ("100062", "", (-1, -1), True),
            ("100061", "12", (1, 2), False),
            ("100061", "", (-1, -1), False),
        ],
        ids=["reported-owned", "row-order-owned", "reported-free", "row-order-free"],
    )
    def test_a_position_names_no_member_that_another_member_row_owns_by_serial(
        self, first_serial, second_serial, positions, withheld
    ):
        """Switch 1 reports member 2's serial, so position 2 can not give Switch 2 that member too."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        first, second = _vc_members(["100061", "100062"])
        inventory = [
            _row(220, "stack", 0),
            _row(221, "chassis", 220, position=positions[0], entPhysicalSerialNum=first_serial),
            _row(222, "chassis", 220, position=positions[1], entPhysicalSerialNum=second_serial),
            _row(223, "module", 222, entPhysicalModelName="CARD", entPhysicalSerialNum="CARD123"),
        ]

        owners = attribute_inventory(inventory, [first, second], SerialRules.load().for_device(first))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert owner[221] == ({"100061": first, "100062": second}[first_serial], "serial")
        if withheld:
            assert {owner[222], owner[223]} == {(None, UNATTRIBUTED_SOURCE)}
        else:
            assert (owner[222], owner[223]) == ((second, "position"), (second, "parent-context"))

    @pytest.mark.parametrize(("fpc_serial", "inherits"), [("12", False), ("ZZZ999", True)], ids=["short", "identity"])
    def test_a_row_inherits_no_member_across_a_junos_fpc_with_a_short_serial(self, fpc_serial, inherits):
        """FPC 1 reports "12", which turns off the stack shape and could be a member row, so the root's serial stops there."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members(f"junos-short-fpc-{fpc_serial}")
        inventory = [
            {**row, "entPhysicalSerialNum": fpc_serial} if row["entPhysicalIndex"] == 121 else row
            for row in junos_vc_inventory()
        ]

        owners = attribute_inventory(inventory, [master, member], SerialRules.load().for_device(master))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert owner[120] == (master, "serial")
        if inherits:
            assert (owner[121][0], owner[1210][0]) == (member, member)
        else:
            assert {owner[121], owner[1210]} == {(None, UNATTRIBUTED_SOURCE)}

    @pytest.mark.parametrize(("fpc_serial", "linked"), [("12", False), ("12350", True)], ids=["short", "unchanged"])
    def test_a_junos_row_that_names_an_fpc_with_a_short_serial_is_unattributed(self, fpc_serial, linked):
        """The PSU and fan name FPC 1, which reports "12", so they do not fall through to the root's master serial."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members(f"junos-short-link-{fpc_serial}")
        inventory = [
            *(
                {**row, "entPhysicalSerialNum": fpc_serial} if row["entPhysicalIndex"] == 121 else row
                for row in junos_vc_inventory()
            ),
            _row(41, "fan", 4, entPhysicalModelName="PSU-FAN"),
        ]

        owners = attribute_inventory(inventory, [master, member], SerialRules.load().for_device(master))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert owner[2][0] == master
        if linked:
            assert (owner[4], owner[30]) == ((member, "junos-member"), (member, "junos-member"))
            assert owner[41] == (member, "parent-context")
        else:
            # The PSU's fan crosses the PSU's link on its way to the root's serial.
            assert {owner[4], owner[30], owner[41]} == {(None, UNATTRIBUTED_SOURCE)}

    @pytest.mark.parametrize("padded", [False, True], ids=["plain", "padded"])
    def test_a_padded_fpc_description_keeps_the_stack(self, padded):
        """FPC 0's description carries spaces at both ends; the stack keeps both FPCs and the members stay the same."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import extract_vc_members
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members(f"junos-padded-fpc-{padded}")
        inventory = [
            {**row, "entPhysicalDescr": f"  {row['entPhysicalDescr']}  "}
            if padded and row["entPhysicalIndex"] == 120
            else row
            for row in junos_vc_inventory()
        ]
        context = SerialRules.load().for_device(master)

        owners = attribute_inventory(inventory, [master, member], context)

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert [found["index"] for found in extract_vc_members(inventory, [context])] == [120, 121]
        assert (owner[120], owner[121], owner[2], owner[4]) == (
            (master, "serial"),
            (member, "serial"),
            (master, "junos-member"),
            (member, "junos-member"),
        )

    def test_a_container_described_as_only_its_fpc_slot_is_an_fpc(self):
        """A container described "FPC 1" is FPC 1 itself, so the stack keeps both FPCs and its PSU follows FPC 1."""
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members("junos-slot-only-fpc")
        descrs = {120: "FPC 0", 121: "FPC 1"}
        inventory = [
            {**row, "entPhysicalDescr": descrs[row["entPhysicalIndex"]]} if row["entPhysicalIndex"] in descrs else row
            for row in junos_vc_inventory()
        ]

        owners = attribute_inventory(inventory, [master, member], SerialRules.load().for_device(master))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert (owner[120], owner[121]) == ((master, "serial"), (member, "serial"))
        assert owner[4] == (member, "junos-member")

    @pytest.mark.parametrize(
        "descr",
        ["FPC 1 Power Supply 0", " FPC 1 Power Supply 0 ", "FPC 1 ", "FPC 1"],
        ids=["plain", "padded", "slot-only-padded", "slot-only"],
    )
    def test_a_padded_fpc_link_names_the_slot_as_the_anonymizer_reads_it(self, descr):
        """The anonymizer trims the description, so a padded "FPC 1 ..." names FPC 1, whose "12" gives no member."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members(f"junos-padded-link-{len(descr)}")
        changes = {121: {"entPhysicalSerialNum": "12"}, 4: {"entPhysicalDescr": descr}}
        inventory = [{**row, **changes.get(row["entPhysicalIndex"], {})} for row in junos_vc_inventory()]

        owners = attribute_inventory(inventory, [master, member], SerialRules.load().for_device(master))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert owner[4] == (None, UNATTRIBUTED_SOURCE)

    @pytest.mark.parametrize(("other_serial", "unattributed"), [("", False), ("SN-BBB", True)], ids=["blank", "mixed"])
    def test_a_link_to_a_slot_of_blank_fpcs_only_gives_no_link(self, other_serial, unattributed):
        """Both FPCs report slot 1; when all of them are blank the PSU resolves as develop does, by parent context."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members(f"junos-blank-slot-{other_serial or 'blank'}")
        inventory = [
            {**row, "entPhysicalSerialNum": "", "entPhysicalParentRelPos": 1}
            if row["entPhysicalIndex"] == 120
            else {**row, "entPhysicalSerialNum": other_serial if row["entPhysicalIndex"] == 121 else ""}
            for row in junos_vc_inventory()
        ]

        owners = attribute_inventory(inventory, [master, member], SerialRules.load().for_device(master))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert owner[4] == ((None, UNATTRIBUTED_SOURCE) if unattributed else (None, "parent-context"))

    @pytest.mark.parametrize("bay", [True, False], ids=["named-bay", "no-bay"])
    def test_a_container_that_names_an_fpc_is_no_fpc(self, bay):
        """ "FPC 1 Power Supply Bay" names FPC 1; it is no FPC row, so its PSU follows FPC 1 and not the root's serial."""
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members(f"junos-fpc-bay-{bay}")
        inventory = junos_vc_inventory()
        if bay:
            inventory += [
                _row(140, "container", 1, position=5, entPhysicalDescr="FPC 1 Power Supply Bay"),
                _row(141, "powerSupply", 140, entPhysicalDescr="Power Supply 1", entPhysicalSerialNum="PSU141X"),
            ]

        owners = attribute_inventory(inventory, [master, member], SerialRules.load().for_device(master))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert (owner[120], owner[121], owner[4]) == ((master, "serial"), (member, "serial"), (member, "junos-member"))
        if bay:
            assert (owner[140], owner[141]) == ((member, "junos-member"), (member, "parent-context"))

    @pytest.mark.parametrize("fpc_reported", [False, True], ids=["no-fpc-rows", "fpc-1-reported"])
    def test_a_junos_row_that_names_an_fpc_no_row_reports_is_unattributed(self, fpc_reported):
        """The root reports the master's serial, but "FPC 1 Power Supply 0" names FPC 1, which no row reports."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members(f"junos-no-fpc-{fpc_reported}")
        inventory = [
            row
            for row in junos_vc_inventory()
            if row["entPhysicalIndex"] in {1, 4} | ({121} if fpc_reported else set())
        ]

        owners = attribute_inventory(inventory, [master, member], SerialRules.load().for_device(master))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert owner[4] == ((member, "junos-member") if fpc_reported else (None, UNATTRIBUTED_SOURCE))

    @pytest.mark.parametrize("stack", [False, True], ids=["unrecognized", "recognized"])
    def test_a_junos_fpc_link_is_read_from_the_description_only(self, stack):
        """The anonymizer keeps "FPC <n>" only in descriptions, so a name-only link must give what replay gives."""
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members(f"junos-name-link-{stack}")
        inventory = junos_vc_inventory()
        if not stack:
            fpc = {"entPhysicalName": "EX4400-24X-S", "entPhysicalModelName": "650-151094"}
            descr = "FPC: EX4400-24X @ 2/*/*"
            inventory.append(
                _row(122, "container", 1, position=2, entPhysicalDescr=descr, entPhysicalSerialNum="12", **fpc)
            )
        live = [
            {**row, "entPhysicalName": "FPC 1 Power Supply 0", "entPhysicalDescr": "Power Supply 0"}
            if row["entPhysicalIndex"] == 4
            else row
            for row in inventory
        ]
        replayed = [{**row, "entPhysicalName": "NAME-0004"} if row["entPhysicalIndex"] == 4 else row for row in live]
        rules = SerialRules.load()

        def owner_of_the_psu(rows):
            owners = attribute_inventory(rows, [master, member], rules.for_device(master))
            return next(found for row, found in zip(rows, owners, strict=True) if row["entPhysicalIndex"] == 4)

        assert owner_of_the_psu(live) == owner_of_the_psu(replayed)

    @pytest.mark.parametrize("case", ["short-fpc-2", "short-fpc-1-without-slot", "blank-serials"])
    def test_a_junos_row_that_names_an_fpc_follows_only_that_fpc(self, case):
        """Outside a recognized stack, "FPC 1 ..." rows resolve through FPC 1 and never through the root's serial."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules

        master, member = make_junos_vc_members(f"junos-fpc-link-{case}")
        inventory = junos_vc_inventory()
        if case == "short-fpc-2":
            fpc = {"entPhysicalName": "EX4400-24X-S", "entPhysicalModelName": "650-151094"}
            inventory.append(
                _row(
                    122,
                    "container",
                    1,
                    position=2,
                    entPhysicalDescr="FPC: EX4400-24X @ 2/*/*",
                    entPhysicalSerialNum="12",
                    **fpc,
                )
            )
        elif case == "short-fpc-1-without-slot":
            inventory = [
                {**row, "entPhysicalSerialNum": "12", "entPhysicalParentRelPos": -1}
                if row["entPhysicalIndex"] == 121
                else row
                for row in inventory
            ]
        else:
            inventory = [{**row, "entPhysicalSerialNum": ""} for row in inventory]

        owners = attribute_inventory(inventory, [master, member], SerialRules.load().for_device(master))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        if case == "short-fpc-2":
            assert owner[121] == (member, "serial")
            assert owner[122] == (None, UNATTRIBUTED_SOURCE)
            assert (owner[4], owner[30]) == ((member, "junos-member"), (member, "junos-member"))
        elif case == "short-fpc-1-without-slot":
            assert {owner[121], owner[4], owner[30], owner[1210]} == {(None, UNATTRIBUTED_SOURCE)}
        else:
            # A blank FPC serial gives no link, so the rows resolve as develop does: by parent context, on the page.
            assert {owner[index] for index in (2, 4, 30, 120, 121, 1210)} == {(None, "parent-context")}

    @pytest.mark.parametrize("serial", ["12", "0", "CARD999"], ids=["short", "zero", "identity"])
    def test_a_non_member_row_with_a_short_serial_keeps_its_member_row(self, serial):
        """The stack shape names the member rows, so a carrier below Switch 1 that reports "12" is no member."""
        from netbox_librenms_plugin.serial_identity import SerialRules

        first, second = _vc_members(["100071", "100072"])
        inventory = [
            _row(230, "stack", 0),
            _row(231, "chassis", 230, position=1, entPhysicalSerialNum="SW-ONE-1"),
            _row(232, "chassis", 230, position=2, entPhysicalSerialNum="SW-TWO-2"),
            _row(233, "module", 231, entPhysicalModelName="CARRIER", entPhysicalSerialNum=serial),
            _row(234, "module", 233, entPhysicalModelName="CARD", entPhysicalSerialNum="CARD123"),
        ]

        owners = attribute_inventory(inventory, [first, second], SerialRules.load().for_device(first))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert owner[231] == (first, "position")
        assert (owner[233], owner[234]) == ((first, "parent-context"), (first, "parent-context"))

    @pytest.mark.parametrize("stored", [("", ""), ("SN-AAA", "SN-BBB")], ids=["blank", "fpc-serials"])
    def test_junos_fpcs_sharing_a_slot_follow_only_their_serials(self, stored):
        """Two FPCs that both report slot 1 get positions from row order, which names no member of a serial FPC."""
        from netbox_librenms_plugin.import_utils.virtual_chassis import UNATTRIBUTED_SOURCE
        from netbox_librenms_plugin.serial_identity import SerialRules
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

        _chassis, (first, second) = make_virtual_chassis_members("junos-shared-slot", count=2)
        for device, serial in zip((first, second), stored, strict=True):
            device.serial = serial
            device.save(update_fields=["serial"])
        fpc = {"entPhysicalDescr": "FPC: EX4400-24X @ 1/*/*", "entPhysicalName": "EX4400-24X-S"}
        inventory = [
            _row(1, "chassis", 0, entPhysicalDescr="Juniper Virtual Chassis Switch", entPhysicalSerialNum="SN-AAA"),
            _row(120, "container", 1, position=1, entPhysicalSerialNum="SN-AAA", **fpc),
            _row(121, "container", 1, position=1, entPhysicalSerialNum="SN-BBB", **fpc),
            _row(1210, "module", 121, position=2, entPhysicalModelName="PIC"),
            # "FPC 1" names a slot that two FPCs share, so no FPC gives it a member.
            _row(4, "powerSupply", 1, position=2, entPhysicalDescr="FPC 1 Power Supply 0", entPhysicalModelName="PSU"),
        ]

        owners = attribute_inventory(inventory, [first, second], SerialRules.load().for_device(second))

        owner = {row["entPhysicalIndex"]: found for row, found in zip(inventory, owners, strict=True)}
        assert owner[4] == (None, UNATTRIBUTED_SOURCE)
        if stored[0]:
            assert (owner[120], owner[121], owner[1210]) == (
                (first, "serial"),
                (second, "serial"),
                (second, "ancestor-serial"),
            )
        else:
            assert {owner[index] for index in (120, 121, 1210)} == {(None, UNATTRIBUTED_SOURCE)}


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
    row = _row(5, "module", 0, entPhysicalSerialNum="S/N ABC-SMD")
    policy = load_inventory_policy(page.pk)

    _default, contexts = BaseModuleTableView._build_inventory_ignore_contexts(
        policy.page_device, [row], policy.vc_members, policy
    )

    context = contexts[("index", 5)]
    assert context["selected_device"].pk == member.pk
    assert _check_ignore_rules(row, None, context["ignore_rules"], {5: row}, context["device_identity"]) == "skip"


def test_serial_matches_device_keys_a_masterless_member_serial_under_its_own_rules():
    """Only the page maker strips "TAG ", so the member's own rules keep "TAG ABC" and "ABC" apart."""
    from dcim.models import Device, DeviceType, Manufacturer

    from netbox_librenms_plugin.inventory_policy import load_inventory_policy
    from netbox_librenms_plugin.models import InventoryIgnoreRule, NormalizationRule
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView, _check_ignore_rules

    page, member = _vc_members(["PAGE-MASTERLESS", "ABC-MASTERLESS"])
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG (.+)$", replacement=r"\1", manufacturer=page.device_type.manufacturer
    )
    other_maker = Manufacturer.objects.create(name="Masterless member maker", slug="masterless-member-maker")
    member_type = DeviceType.objects.create(
        manufacturer=other_maker, model="Masterless member", slug="masterless-member"
    )
    Device.objects.filter(pk=member.pk).update(device_type=member_type)
    InventoryIgnoreRule.objects.all().delete()
    InventoryIgnoreRule.objects.create(
        name="Hide the member board", match_type="serial_matches_device", pattern="", action="skip"
    )
    row = _row(5, "module", 0, position=2, entPhysicalSerialNum="TAG ABC-MASTERLESS")
    policy = load_inventory_policy(page.pk)

    _default, contexts = BaseModuleTableView._build_inventory_ignore_contexts(
        policy.page_device, [row], policy.vc_members, policy
    )

    context = contexts[("index", 5)]
    assert context["selected_device"].pk == member.pk
    assert _check_ignore_rules(row, None, context["ignore_rules"], {5: row}, context["device_identity"]) is None


def test_a_masterless_member_serial_names_its_owner_only_under_its_own_rules():
    """Only the page maker strips "TAG ", so "TAG ABC" names no member that stores "ABC", and the position decides."""
    from dcim.models import Device, DeviceType, Manufacturer

    from netbox_librenms_plugin.inventory_policy import load_inventory_policy
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    page, member = _vc_members(["PAGE-POSN", "ABC-POSN"])
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG (.+)$", replacement=r"\1", manufacturer=page.device_type.manufacturer
    )
    other_maker = Manufacturer.objects.create(name="Positioned member maker", slug="positioned-member-maker")
    member_type = DeviceType.objects.create(
        manufacturer=other_maker, model="Positioned member", slug="positioned-member"
    )
    Device.objects.filter(pk=member.pk).update(device_type=member_type)
    row = _row(5, "module", 0, position=1, entPhysicalSerialNum="TAG ABC-POSN")
    policy = load_inventory_policy(page.pk)

    _default, contexts = BaseModuleTableView._build_inventory_ignore_contexts(
        policy.page_device, [row], policy.vc_members, policy
    )

    assert contexts[("index", 5)]["selected_device"].pk == page.pk
    assert contexts[("index", 5)]["resolution_source"] == "position"


def test_a_masterless_junos_stack_is_detected_under_the_rules_of_any_member():
    """The page maker strips "TAG-" to a short value; the member's rules keep FPC 1's serial, so its PSU follows it."""
    from dcim.models import Device, DeviceType, Manufacturer

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.serial_identity import SerialRules

    page, member = make_junos_vc_members("junos-masterless-admits")
    # FPC 1 carries the serial of the NetBox device at position 0, so only the stack rows, not the position, name it.
    maker = Manufacturer.objects.create(name="Junos masterless member maker", slug="junos-masterless-member-maker")
    member_type = DeviceType.objects.create(manufacturer=maker, model="Junos masterless", slug="junos-masterless")
    Device.objects.filter(pk=page.pk).update(vc_position=9, serial="12345")
    Device.objects.filter(pk=member.pk).update(vc_position=0, serial="TAG-AB", device_type=member_type)
    Device.objects.filter(pk=page.pk).update(vc_position=1)
    page.refresh_from_db()
    member.refresh_from_db()
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG-(.+)$", replacement=r"\1", manufacturer=page.device_type.manufacturer
    )
    inventory = [
        {**row, "entPhysicalSerialNum": "TAG-AB"} if row["entPhysicalSerialNum"] == "12350" else row
        for row in junos_vc_inventory()
    ]
    rules = SerialRules.load()

    owners = attribute_inventory(inventory, [page, member], rules.for_device(page), member_context=rules.for_device)

    owner = {row["entPhysicalIndex"]: found or page for row, (found, _source) in zip(inventory, owners, strict=True)}
    assert (owner[121], owner[4]) == (member, member)
