"""
Module-sync member attribution through its module interface, ``attribute_inventory``.

Plain ENTITY-MIB rows and real NetBox member Devices; the modules-tab tests in
``test_modules_view.py`` cover the same rules end to end.
"""

import pytest

from netbox_librenms_plugin.import_utils.virtual_chassis import attribute_inventory, chassis_serial_key

pytestmark = pytest.mark.django_db


def _owners(page, members, inventory):
    """Return ``{entPhysicalIndex: owning Device}``; an unowned row stays on *page*, as the modules tab shows it."""
    owners = attribute_inventory(inventory, members, chassis_serial_key(page))
    return {row["entPhysicalIndex"]: member or page for row, (member, _source) in zip(inventory, owners, strict=True)}


def _attribution(page, members, row):
    """Return ``(owning Device, source)`` for one row on its own."""
    member, source = attribute_inventory([row], members, chassis_serial_key(page))[0]
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


def _junos_vc_inventory():
    """Return the reporter's EX4400 VC rows plus one synthetic PIC under FPC 1."""

    def row(index, entity_class, descr, *, serial="", position=0, contained_in=1, name="", model=""):
        return {
            "entPhysicalIndex": index,
            "entPhysicalClass": entity_class,
            "entPhysicalDescr": descr,
            "entPhysicalName": name,
            "entPhysicalModelName": model,
            "entPhysicalSerialNum": serial,
            "entPhysicalParentRelPos": position,
            "entPhysicalContainedIn": contained_in,
        }

    psu = {"name": "JPSU-550-C-DC-AFO", "model": "640-107104"}
    fpc = {"name": "EX4400-24X-S", "model": "650-151094"}
    return [
        row(1, "chassis", "Juniper Virtual Chassis Switch", serial="12345", contained_in=0),
        row(2, "powerSupply", "FPC 0 Power Supply 0", serial="12346", position=0, **psu),
        row(4, "powerSupply", "FPC 1 Power Supply 0", serial="12348", position=2, **psu),
        row(30, "fan", "FPC 1 Fan Tray 0", position=10, name="Fan Module, Airflow Out (AFO)"),
        row(120, "container", "FPC: EX4400-24X @ 0/*/*", serial="12345", position=0, **fpc),
        row(121, "container", "FPC: EX4400-24X @ 1/*/*", serial="12350", position=1, **fpc),
        row(1210, "module", "PIC: 4x10G SFP+ @ 1/2/*", position=2, contained_in=121, name="PIC 2", model="PIC-4X10G"),
        row(271, "other", "Routing Engine 1", serial="12351", position=1, name="EX4400-24X-S", model="BUILTIN"),
    ]


def _junos_vc_members(tag):
    """Return a real two-member VC numbered like Junos: master at 0, member at 1."""
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

    _chassis, (master, member) = make_virtual_chassis_members(tag, count=2)
    for device, position, serial in ((master, 0, "12345"), (member, 1, "12350")):
        device.vc_position = position
        device.serial = serial
        device.save(update_fields=["vc_position", "serial"])
    return master, member


class TestSerialEvidence:
    """A row's own serial decides first; serials can arrive as JSON numbers."""

    def test_numeric_item_serial_matches_the_member_stored_as_text(self):
        """An all-digit ENTITY serial arriving as an int must still resolve to its VC member instead of raising."""
        master, member2 = _vc_members(["100001", "100002"])
        item = {"entPhysicalIndex": 1, "entPhysicalSerialNum": 100002, "entPhysicalContainedIn": 0}

        target, source = _attribution(master, [master, member2], item)

        assert target.pk == member2.pk
        assert source == "serial"

    def test_zero_item_serial_is_not_dropped_as_falsey(self):
        """A serial of JSON number 0 is real; dropping it silently attributes the item to the wrong VC member."""
        master, member2 = _vc_members(["0", "100003"])
        item = {"entPhysicalIndex": 2, "entPhysicalSerialNum": 0, "entPhysicalContainedIn": 0}

        target, source = _attribution(member2, [master, member2], item)

        assert target.pk == master.pk
        assert source == "serial"

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
    """An inventory that is no recognized stack keeps the position and name heuristic from 1."""

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

        owners = attribute_inventory(inventory, [page, member], chassis_serial_key(page))

        assert owners[0] == (None, "default")
        assert owners[1][0] == member
        assert owners[2][0] == member

    @pytest.mark.parametrize("root_class", ["stack", "chassis"])
    @pytest.mark.parametrize("root_name", ["Switch stack", "StackSub-0/0"])
    def test_a_vc_root_at_position_zero_does_not_claim_member_zero(self, root_class, root_name):
        """The root holds the members; its own position 0 or "0/0" name is not member 0."""
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
        owners = attribute_inventory(inventory, [first, second], chassis_serial_key(second))

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
        master, member = _junos_vc_members("junos-attribution")

        owner = _owners(master, [master, member], _junos_vc_inventory())

        # Fan tray parent positions are 10 and 11 for member 1, so only the description names it.
        assert owner[30] == member
        assert owner[121] == member
        assert owner[271] == master

    def test_a_junos_row_inherits_the_nearer_serial_over_its_fpc(self):
        """A module below FPC 0 that carries member 1's serial takes its serial-less child to member 1."""
        master, member = _junos_vc_members("junos-nearer-serial")
        inventory = [
            *_junos_vc_inventory(),
            _row(1201, "module", 120, position=0, entPhysicalSerialNum="12350", entPhysicalModelName="MOVED"),
            _row(1202, "powerSupply", 1201, entPhysicalModelName="PSU"),
        ]

        owner = _owners(master, [master, member], inventory)

        assert owner[1201] == member
        assert owner[1202] == member

    def test_a_named_junos_psu_follows_its_fpc_serial_over_the_fpc_position(self):
        """NetBox numbers the members the other way round; the FPC serial decides, and its PSU follows it."""
        master, member = _junos_vc_members("junos-swapped")
        # FPC 1 (slot 1, position 1) carries the serial of the NetBox device at position 0, and the reverse.
        master.serial, member.serial = "12350", "12345"
        for device in (master, member):
            device.save(update_fields=["serial"])

        owner = _owners(member, [master, member], _junos_vc_inventory())

        assert owner[121] == master
        assert owner[4] == master
        assert owner[2] == member

    def test_junos_fpcs_sharing_a_slot_follow_the_detected_positions(self):
        """Two FPCs that both report slot 1 get positions 1 and 2 from detection; module sync must agree."""
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

        _chassis, (first, second) = make_virtual_chassis_members("junos-shared-slot", count=2)
        for device in (first, second):
            device.serial = ""
            device.save(update_fields=["serial"])
        fpc = {"entPhysicalDescr": "FPC: EX4400-24X @ 1/*/*", "entPhysicalName": "EX4400-24X-S"}
        inventory = [
            _row(1, "chassis", 0, entPhysicalDescr="Juniper Virtual Chassis Switch", entPhysicalSerialNum="S-A"),
            _row(120, "container", 1, position=1, entPhysicalSerialNum="S-A", **fpc),
            _row(121, "container", 1, position=1, entPhysicalSerialNum="S-B", **fpc),
            _row(1210, "module", 121, position=2, entPhysicalModelName="PIC"),
            # "FPC 1" names a slot that two FPCs share, so it names no member.
            _row(4, "powerSupply", 1, position=2, entPhysicalDescr="FPC 1 Power Supply 0", entPhysicalModelName="PSU"),
        ]

        owner = _owners(second, [first, second], inventory)

        assert owner[120] == first
        assert (owner[121], owner[1210]) == (second, second)
        assert owner[4] == second
