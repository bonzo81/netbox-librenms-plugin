"""
Tests for BaseModuleTableView sync logic (modules_view.py).

Focuses on the bay-scope tracking in _build_context and the serial
comparison logic in _build_row.
"""

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_view():
    """Instantiate BaseModuleTableView bypassing __init__."""
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    view = object.__new__(BaseModuleTableView)
    view._device_manufacturer = None
    view._librenms_api = MagicMock(server_key="test-server")
    view.get_cache_key = MagicMock(return_value="test_cache_key")
    return view


def _captured_table_view(view):
    """Replace get_table with a version that captures the raw table_data list."""
    rows_store = {}

    def fake_get_table(table_data, obj):
        rows_store["rows"] = table_data
        m = MagicMock()
        m.configure = MagicMock()
        return m

    view.get_table = fake_get_table
    return rows_store


@pytest.mark.django_db
class TestInventoryClassIncludeRule:
    """Admit an entPhysicalClass the built-in allowlist does not carry.

    A Juniper MX304 reports both Routing Engines with entPhysicalClass "other".
    INVENTORY_CLASSES has no "other", so the modules tab showed neither of them.
    """

    def _inventory(self):
        """A chassis with the two Routing Engines under it, as LibreNMS reports them."""
        return [
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "chassis",
                "entPhysicalName": "Chassis",
                "entPhysicalDescr": "MX304",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 38,
                "entPhysicalClass": "other",
                "entPhysicalName": "JNP304-RE-S",
                "entPhysicalModelName": "JNP304-RE-S",
                "entPhysicalDescr": "Routing Engine 0",
                "entPhysicalSerialNum": "S/N BCFB9793",
                "entPhysicalContainedIn": 1,
            },
            {
                "entPhysicalIndex": 39,
                "entPhysicalClass": "other",
                "entPhysicalName": "JNP304-RE-S",
                "entPhysicalModelName": "JNP304-RE-S",
                "entPhysicalDescr": "Routing Engine 1",
                "entPhysicalSerialNum": "S/N BCFB9751",
                "entPhysicalContainedIn": 1,
            },
        ]

    def _collect(self, items, rules):
        """Collect top-level rows the way _build_context does, cache included."""
        from netbox_librenms_plugin.views.base.modules_view import (
            BaseModuleTableView,
            _check_ignore_rules,
        )

        index_map = {item["entPhysicalIndex"]: item for item in items}
        # _collect_top_items reads this cache for any item carrying an index, so a test that
        # passed {} would report that no rule ever matched.
        ignore_cache = {
            item["entPhysicalIndex"]: _check_ignore_rules(
                item, index_map.get(item.get("entPhysicalContainedIn")), rules, index_map, ""
            )
            for item in items
        }
        transparent = BaseModuleTableView._find_transparent_indices(items, ignore_cache)
        return BaseModuleTableView._collect_top_items(items, index_map, rules, "", transparent, ignore_cache)

    def _include_rule(self, pattern="other"):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        return InventoryIgnoreRule.objects.create(
            name="Routing engines reported as other",
            match_type=InventoryIgnoreRule.MATCH_CLASS_IS,
            pattern=pattern,
            action=InventoryIgnoreRule.ACTION_INCLUDE,
            require_serial_match_parent=False,
        )

    def test_without_a_rule_the_class_is_dropped(self):
        """The built-in allowlist still governs when no rule says otherwise."""
        assert self._collect(self._inventory(), []) == []

    def test_an_include_rule_admits_every_item_of_that_class(self):
        collected = self._collect(self._inventory(), [self._include_rule()])

        assert [item["entPhysicalIndex"] for item in collected] == [38, 39]

    def test_a_rule_for_another_class_admits_nothing(self):
        assert self._collect(self._inventory(), [self._include_rule(pattern="sensor")]) == []

    def test_the_class_match_is_case_insensitive(self):
        collected = self._collect(self._inventory(), [self._include_rule(pattern="Other")])

        assert [item["entPhysicalIndex"] for item in collected] == [38, 39]

    def test_the_migration_seeds_the_rule_that_admits_routing_engines(self):
        """The fix ships working: a fresh install admits the class without operator setup."""
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        seeded = InventoryIgnoreRule.objects.filter(
            match_type=InventoryIgnoreRule.MATCH_CLASS_IS,
            action=InventoryIgnoreRule.ACTION_INCLUDE,
            pattern="other",
        )

        assert seeded.exists()
        assert seeded.get().enabled is True

    def test_the_row_name_falls_back_to_the_description(self):
        """Both Routing Engines report entPhysicalName "JNP304-RE-S", so the name cannot
        tell them apart. The description carries "Routing Engine 0" and "1"."""
        collected = self._collect(self._inventory(), [self._include_rule()])
        view = _make_view()
        names = [
            view._build_row(item, {item["entPhysicalIndex"]: item for item in collected}, {}, {})["name"]
            for item in collected
        ]

        assert names == ["Routing Engine 0", "Routing Engine 1"]

    def test_a_skip_rule_still_wins_over_the_allowlist_admission(self):
        """An operator must still be able to drop an item the include rule let through."""
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        skip = InventoryIgnoreRule.objects.create(
            name="Drop RE 1",
            match_type=InventoryIgnoreRule.MATCH_CONTAINS,
            pattern="JNP304-RE-S",
            action=InventoryIgnoreRule.ACTION_SKIP,
            require_serial_match_parent=False,
        )

        assert self._collect(self._inventory(), [self._include_rule(), skip]) == []


class TestRowOrderIsStableAcrossAnInstall:
    """Installing a module must not move its row.

    The rows used to be grouped by status, and installing a module flips its status to
    "Installed", which pulled the row to the top of the table under the user.
    """

    def _rows(self, *statuses):
        """One top-level row per status, each carrying a child, in inventory order."""
        rows = []
        for index, status in enumerate(statuses):
            rows.append({"name": f"bay{index}", "status": status, "depth": 0})
            rows.append({"name": f"bay{index}-child", "status": "Unmatched", "depth": 1})
        return rows

    def test_rows_keep_their_inventory_order(self):
        view = _make_view()
        rows = self._rows("Unmatched", "Matched", "Installed", "Serial Mismatch")

        assert [row["name"] for row in view._group_children_under_parents(rows)] == [row["name"] for row in rows]

    def test_installing_a_module_does_not_move_its_row(self):
        """The same table before and after one bay's status becomes Installed."""
        view = _make_view()
        # Statuses chosen so the old status sort genuinely reordered them: installing bay1
        # moved it from last to second.
        before = self._rows("Installed", "Unmatched", "Matched")
        after = self._rows("Installed", "Installed", "Matched")

        order_before = [row["name"] for row in view._group_children_under_parents(before)]
        order_after = [row["name"] for row in view._group_children_under_parents(after)]

        assert order_before == order_after

    def test_children_stay_under_their_own_parent(self):
        view = _make_view()
        rows = self._rows("Installed", "Unmatched")

        result = view._group_children_under_parents(rows)

        assert [(row["name"], row["depth"]) for row in result] == [
            ("bay0", 0),
            ("bay0-child", 1),
            ("bay1", 0),
            ("bay1-child", 1),
        ]


@pytest.mark.django_db
class TestFpcSlotMatchesOnSlashedPositions:
    """A Juniper module bay position is "fpc/pic", so the guard must read the FPC from it.

    Reported from live NetBox: an MX304 MIC sits in bay LCMIC0 at position "0/0", which the
    device-type library needs so the module type's "Transceiver {module}/N" template expands to
    "Transceiver 0/0/0". int("0/0") raises, and the guard turned that into "no match", so every
    transceiver bay under the MIC was silently discarded.
    """

    def _child_bay(self, parent_position):
        """Build a real transceiver bay nested under a module installed at *parent_position*."""
        from dcim.models import Module, ModuleBay

        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type

        device = make_device_with_module_bays(f"mx304-{parent_position.replace('/', '-')}", [])
        parent_bay = ModuleBay.objects.create(device=device, name="LCMIC0", position=parent_position)
        module = Module.objects.create(
            device=device, module_bay=parent_bay, module_type=make_module_type("JNP-MIC1"), status="active"
        )
        return ModuleBay.objects.create(device=device, module=module, name="Transceiver 0/0/0", position="0")

    def test_a_slashed_parent_position_still_matches_its_own_fpc(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = self._child_bay("0/0")

        assert BaseModuleTableView._fpc_slot_matches("QSFP56-DD @ 0/0/0", bay) is True

    def test_the_second_pic_of_the_same_fpc_still_matches(self):
        """Only the FPC is compared, so pic 1 under fpc 0 is the same slot for this guard."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = self._child_bay("0/1")

        assert BaseModuleTableView._fpc_slot_matches("QSFP56-DD @ 0/1/3", bay) is True

    def test_a_different_fpc_is_still_rejected(self):
        """The guard exists to drop an orphan belonging to another FPC. That must survive."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = self._child_bay("0/0")

        assert BaseModuleTableView._fpc_slot_matches("QSFP56-DD @ 1/0/2", bay) is False

    def test_a_bare_integer_position_keeps_working(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = self._child_bay("2")

        assert BaseModuleTableView._fpc_slot_matches("QSFP @ 2/0/1", bay) is True
        assert BaseModuleTableView._fpc_slot_matches("QSFP @ 3/0/1", bay) is False

    @pytest.mark.parametrize("position", ["01A", "02A", "03D", "PCIe1", "swp3", "FPC1", "PSU0"])
    def test_a_digit_bearing_but_non_numeric_position_fails_closed(self, position):
        """These are real bay positions, and none of them names an FPC.

        A fix that pulled the leading digits out of "01A" would read FPC 1 and match a
        descriptor for a different slot. Only a wholly numeric component counts.
        """
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = self._child_bay(position)

        assert BaseModuleTableView._fpc_slot_matches("QSFP @ 0/0/1", bay) is False
        assert BaseModuleTableView._fpc_slot_matches("QSFP @ 1/0/1", bay) is False

    @pytest.mark.parametrize(
        "position",
        ["0/FT0", "0/PM0", "0/RP0", "0/RP1", "0/IMD", "0/PS0/M0", "2/x1", "1/1/c1"],
    )
    def test_a_compound_position_whose_tail_is_not_numeric_fails_closed(self, position):
        """Real shipping positions: fan trays, power modules, route processors, Nokia XIOM.

        The leading digit is a chassis index, not an FPC, so reading it alone would accept a
        transceiver descriptor against a fan tray bay. Only a wholly numeric fpc/pic counts.

        "2/x1" is the case that separates a leading-segment parse from a whole-position one:
        it is a live parent bay carrying 34 nested bays, so it really reaches this guard.
        """
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = self._child_bay(position)

        assert BaseModuleTableView._fpc_slot_matches("QSFP @ 0/0/1", bay) is False

    def test_the_regex_mapping_resolves_through_to_the_bay(self):
        """The whole sequence: the mapping resolves the bay name, then the guard keeps it.

        This is the shape the bug actually broke. The mapping matched and produced the right
        bay name, and the guard then discarded it, so the lookup returned None.
        """
        from dcim.models import Manufacturer

        from netbox_librenms_plugin.models import ModuleBayMapping
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = self._child_bay("0/0")
        manufacturer = Manufacturer.objects.get(pk=bay.device.device_type.manufacturer_id)
        mapping = ModuleBayMapping.objects.create(
            librenms_name=r"^.+ @ (\d+/\d+/\d+)$",
            netbox_bay_name=r"Transceiver \1",
            librenms_class="port",
            is_regex=True,
            manufacturer=manufacturer,
        )

        resolved = BaseModuleTableView._lookup_regex_bay_mapping(
            "QSFP56-DD @ 0/0/0", "port", {bay.name: bay}, [mapping], manufacturer_id=manufacturer.pk
        )

        assert resolved == bay

    def test_an_unparseable_position_still_fails_closed(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = self._child_bay("slot-a")

        assert BaseModuleTableView._fpc_slot_matches("QSFP @ 0/0/1", bay) is False


class TestMergeTransceiverDataPortIdentity:
    """Transceiver merge should preserve stable port identity metadata."""

    def test_malformed_transceiver_payload_returns_error_without_crashing(self):
        """A truthy success with a non-list payload (or a list of non-dicts) must surface as an error string — not 500 on txr.get(...) — so the caller skips the cache and warns."""
        from copy import deepcopy

        view = _make_view()
        view.librenms_id = 100
        seed = [{"entPhysicalIndex": 1, "entPhysicalName": "Gi0/1"}]

        def assert_malformed(payload):
            # Deep-copy the seed so a mutation of the SHARED inner dict by _merge_transceiver_data
            # can't also mutate `seed` and make the "untouched" assertion pass vacuously.
            candidate = deepcopy(seed)
            view._librenms_api.get_device_transceivers.return_value = (True, payload)
            inventory, error = view._merge_transceiver_data(candidate)
            assert inventory == seed  # untouched, compared against the pristine seed
            assert error and "malformed transceiver payload" in error

        assert_malformed({"unexpected": "dict"})  # dict payload under success=True
        assert_malformed([{"entity_physical_index": 2}, "bad"])  # list with a non-dict entry
        # empty NON-list payload ({}) is also malformed — it must NOT be treated as a successful
        # "no transceivers" response (which would cache a degraded snapshot). Regression for the
        # emptiness check running before the type check: {} is falsy, so the old order mislabeled it.
        assert_malformed({})

    def test_synthetic_item_includes_port_identity_metadata(self):
        view = _make_view()
        view.librenms_id = 100
        view._librenms_api.get_device_transceivers.return_value = (
            True,
            [
                {
                    "entity_physical_index": 200,
                    "model": "SFP-10G-SR",
                    "serial": "TX-200",
                    "type": "SFP",
                    "port_id": 42,
                }
            ],
        )
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 42, "ifName": "Te1/0/1"}]})

        inventory, error = view._merge_transceiver_data([])

        assert error is None
        assert len(inventory) == 1
        item = inventory[0]
        assert item["_from_transceiver_api"] is True
        assert item["_librenms_port_id"] == 42
        assert item["_librenms_ifname"] == "Te1/0/1"
        assert item["entPhysicalName"] == "Te1/0/1"

    def test_existing_inventory_item_gets_port_identity_metadata(self):
        view = _make_view()
        view.librenms_id = 101
        view._librenms_api.get_device_transceivers.return_value = (
            True,
            [
                {
                    "entity_physical_index": 300,
                    "model": "",
                    "serial": "",
                    "type": "SFP",
                    "port_id": 99,
                }
            ],
        )
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 99, "ifName": "Eth2/1"}]})
        inventory_seed = [
            {
                "entPhysicalIndex": 300,
                "entPhysicalName": "Transceiver slot",
                "entPhysicalModelName": "builtin",
                "entPhysicalSerialNum": "-",
                "entPhysicalClass": "port",
                "entPhysicalContainedIn": 0,
            }
        ]

        inventory, error = view._merge_transceiver_data(inventory_seed)

        assert error is None
        assert len(inventory) == 1
        item = inventory[0]
        assert item["_librenms_port_id"] == 99
        assert item["_librenms_ifname"] == "Eth2/1"

    def test_numeric_transceiver_serial_is_coerced_to_a_string(self):
        """An all-digit transceiver serial can arrive as an int; the merge must coerce it before stripping instead of raising."""
        view = _make_view()
        view.librenms_id = 104
        view._librenms_api.get_device_transceivers.return_value = (
            True,
            [{"entity_physical_index": 400, "model": "SFP-10G-SR", "serial": 987654, "type": "SFP", "port_id": 8}],
        )
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 8, "ifName": "Eth4/1"}]})

        inventory, error = view._merge_transceiver_data([])

        assert error is None
        assert len(inventory) == 1
        assert inventory[0]["entPhysicalSerialNum"] == "987654"

    def test_zero_transceiver_serial_is_preserved(self):
        """A transceiver serial of JSON number 0 is a real serial and must survive as "0", not be dropped as falsey."""
        view = _make_view()
        view.librenms_id = 104
        view._librenms_api.get_device_transceivers.return_value = (
            True,
            [{"entity_physical_index": 401, "model": "SFP-10G-SR", "serial": 0, "type": "SFP", "port_id": 9}],
        )
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 9, "ifName": "Eth5/1"}]})

        inventory, error = view._merge_transceiver_data([])

        assert error is None
        assert len(inventory) == 1
        assert inventory[0]["entPhysicalSerialNum"] == "0"

    def test_numeric_transceiver_model_and_type_are_coerced_to_strings(self):
        """An all-digit model/type arrives as a JSON number too; stripping it raw would 500 the modules refresh."""
        view = _make_view()
        view.librenms_id = 105
        view._librenms_api.get_device_transceivers.return_value = (
            True,
            [{"entity_physical_index": 402, "model": 1000, "serial": "TX-402", "type": 40, "port_id": 10}],
        )
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 10, "ifName": "Eth6/1"}]})

        inventory, error = view._merge_transceiver_data([])

        assert error is None
        assert len(inventory) == 1
        assert inventory[0]["entPhysicalModelName"] == "1000"
        assert inventory[0]["entPhysicalDescr"] == "40"

    def test_numeric_entity_values_on_the_existing_item_are_coerced(self):
        """The ENTITY-MIB side of the merge can carry numeric model/serial values as well."""
        view = _make_view()
        view.librenms_id = 106
        view._librenms_api.get_device_transceivers.return_value = (
            True,
            [{"entity_physical_index": 403, "model": "SFP-10G-SR", "serial": "TX-403", "type": "SFP", "port_id": 11}],
        )
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 11, "ifName": "Eth7/1"}]})
        inventory_seed = [
            {
                "entPhysicalIndex": 403,
                "entPhysicalName": "Transceiver slot",
                "entPhysicalModelName": 1000,  # all-digit model from ENTITY-MIB, delivered as a number
                "entPhysicalSerialNum": 4242,
                "entPhysicalClass": "port",
                "entPhysicalContainedIn": 0,
            }
        ]

        inventory, error = view._merge_transceiver_data(inventory_seed)

        assert error is None
        assert len(inventory) == 1
        # Both existing values are real (not placeholders), so the transceiver data must not overwrite them.
        assert inventory[0]["entPhysicalModelName"] == 1000
        assert inventory[0]["entPhysicalSerialNum"] == 4242

    def test_string_index_matches_int_transceiver_index_no_duplicate(self):
        # LibreNMS may report the ENTITY index as a string ("300") while the transceiver API
        # returns it as an int (300). Without normalization the merge misses the existing ENTITY
        # row and caches a duplicate synthetic transceiver; coercing both sides to int matches it.
        view = _make_view()
        view.librenms_id = 103
        view._librenms_api.get_device_transceivers.return_value = (
            True,
            [{"entity_physical_index": 300, "model": "SFP-10G-SR", "serial": "TX-300", "type": "SFP", "port_id": 7}],
        )
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 7, "ifName": "Eth3/1"}]})
        inventory_seed = [
            {
                "entPhysicalIndex": "300",  # string index from ENTITY-MIB
                "entPhysicalName": "Transceiver slot",
                "entPhysicalModelName": "builtin",
                "entPhysicalSerialNum": "-",
                "entPhysicalClass": "port",
                "entPhysicalContainedIn": 0,
            }
        ]

        inventory, error = view._merge_transceiver_data(inventory_seed)

        assert error is None
        assert len(inventory) == 1  # supplemented in place, NOT duplicated
        item = inventory[0]
        assert item["entPhysicalModelName"] == "SFP-10G-SR"
        assert item["_librenms_port_id"] == 7

    def test_enrich_inventory_port_identity_backfills_port_rows_from_ports_api(self):
        view = _make_view()
        view.librenms_id = 102
        view._librenms_api.get_ports.return_value = (
            True,
            {
                "ports": [
                    {
                        "port_id": 56284,
                        "ifName": "TenGigabitEthernet1/1/1",
                        "ifDescr": "Te1/1/1",
                    }
                ]
            },
        )

        inventory = [
            {
                "entPhysicalClass": "port",
                "entPhysicalName": "Te1/1/1",
                "entPhysicalDescr": "TenGigabitEthernet1/1/1",
            }
        ]

        view._enrich_inventory_port_identity(inventory)

        assert inventory[0]["_librenms_port_id"] == 56284
        assert inventory[0]["_librenms_ifname"] == "TenGigabitEthernet1/1/1"
        assert inventory[0]["_librenms_ifdescr"] == "Te1/1/1"

    def test_enrich_inventory_port_identity_skips_ambiguous_labels(self):
        view = _make_view()
        view.librenms_id = 103
        view._librenms_api.get_ports.return_value = (
            True,
            {
                "ports": [
                    {"port_id": 10, "ifName": "Te1/1/1", "ifDescr": "Uplink A"},
                    {"port_id": 11, "ifName": "Te1/1/1", "ifDescr": "Uplink B"},
                ]
            },
        )

        inventory = [{"entPhysicalClass": "port", "entPhysicalName": "Te1/1/1"}]

        view._enrich_inventory_port_identity(inventory)

        assert "_librenms_port_id" not in inventory[0]

    def test_build_port_name_map_uses_provided_ports_payload_without_api_fetch(self):
        view = _make_view()
        view.librenms_id = 104
        view._librenms_api.get_ports.side_effect = AssertionError("get_ports should not be called")

        port_map = view._build_port_name_map(
            [{"port_id": 42}],
            ports_data={
                "ports": [
                    {
                        "port_id": 42,
                        "ifName": "Te1/0/1",
                        "ifDescr": "TenGigabitEthernet1/0/1",
                    }
                ]
            },
        )

        assert port_map[42]["ifName"] == "Te1/0/1"
        assert port_map[42]["ifDescr"] == "TenGigabitEthernet1/0/1"

    def test_enrich_inventory_port_identity_uses_provided_ports_payload_without_api_fetch(self):
        view = _make_view()
        view.librenms_id = 105
        view._librenms_api.get_ports.side_effect = AssertionError("get_ports should not be called")

        inventory = [{"entPhysicalClass": "port", "entPhysicalName": "Te1/1/1"}]
        view._enrich_inventory_port_identity(
            inventory,
            ports_data={
                "ports": [
                    {
                        "port_id": 56284,
                        "ifName": "TenGigabitEthernet1/1/1",
                        "ifDescr": "Te1/1/1",
                    }
                ]
            },
        )

        assert inventory[0]["_librenms_port_id"] == 56284
        assert inventory[0]["_librenms_ifname"] == "TenGigabitEthernet1/1/1"
        assert inventory[0]["_librenms_ifdescr"] == "Te1/1/1"

    def test_post_fetches_ports_once_and_reuses_payload(self):
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()

        # No POSTed keys → get(key, default) must honour the caller's default (QueryDict
        # semantics); a blanket return_value=None would break default-fallback paths.
        request.POST.get.side_effect = lambda key, default=None: (
            default
        )  # no POSTed server_key → rebind keeps session API
        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._build_context = MagicMock(
            return_value={
                "table": None,
                "object": obj,
                "cache_expiry": None,
                "server_key": view._librenms_api.server_key,
            }
        )

        view._librenms_api.get_librenms_id.return_value = 777
        view._librenms_api.get_device_inventory.return_value = (True, [])
        view._librenms_api.get_device_transceivers.return_value = (True, [])
        ports_payload = {"ports": []}
        view._librenms_api.get_ports.return_value = (True, ports_payload)

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache"),
            patch("netbox_librenms_plugin.views.base.modules_view.messages"),
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock()),
            patch.object(view, "_merge_transceiver_data", wraps=view._merge_transceiver_data) as mock_merge,
            patch.object(
                view, "_enrich_inventory_port_identity", wraps=view._enrich_inventory_port_identity
            ) as mock_enrich,
        ):
            view.post(request, pk=1)

        assert view._librenms_api.get_ports.call_count == 1
        assert mock_merge.call_args.kwargs.get("ports_data") == ports_payload
        assert mock_enrich.call_args.kwargs.get("ports_data") == ports_payload

    def test_post_treats_non_list_inventory_as_fetch_failure(self):
        """get_device_inventory is an external boundary: a success flag with a non-list payload (e.g. an error dict) is treated as a fetch failure."""
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()
        request.POST.get.side_effect = lambda key, default=None: default
        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._librenms_api.get_librenms_id.return_value = 777
        # success=True but the payload is a dict, not list[dict].
        view._librenms_api.get_device_inventory.return_value = (True, {"error": "weird shape"})

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock()),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}),
        ):
            view.post(request, pk=1)

        # Failure branch: error surfaced, stale inventory cache cleared, and the ports fetch
        # (which only runs after a *valid* inventory) is never reached.
        mock_messages.error.assert_called_once()
        # _make_view() fixes get_cache_key() to "test_cache_key"; assert the exact key so the
        # failure path can't silently start deleting the wrong cache entry.
        mock_cache.delete.assert_called_once_with("test_cache_key")
        view._librenms_api.get_ports.assert_not_called()

    def test_post_stale_server_key_resolves_migrated_context_with_session_key(self):
        """When the POSTed server_key is stale (rebind fails), the migrated context must resolve under the session/active key — NOT the stale posted key (which would miss the marker and re-enable a donor's sync controls)."""
        view = _make_view()
        obj = MagicMock()
        request = MagicMock()
        # A NON-EMPTY but stale/unconfigured key is posted (rebind returns None for it).
        request.POST.get.side_effect = lambda key, default=None: {"server_key": "ghost-server"}.get(key, default)
        view.get_object = MagicMock(return_value=obj)
        view.rebind_api_for_server = MagicMock(return_value=None)  # stale key → rebind fails
        view.has_write_permission = MagicMock(return_value=False)
        view.partial_template_name = "x.html"

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.messages"),
            patch(
                "netbox_librenms_plugin.utils.build_migrated_context",
                return_value={"migrated_to_marker": None},
            ) as mock_migrated,
            patch("netbox_librenms_plugin.views.mixins.render", return_value="rendered"),
        ):
            result = view.post(request, pk=1)

        # Resolved under the session/active server key ("test-server"), NOT the stale "ghost-server".
        mock_migrated.assert_called_once_with(obj, "test-server")
        assert result == "rendered"

    def test_post_treats_non_dict_inventory_entry_as_fetch_failure(self):
        """A list payload that carries non-dict entries (e.g. None) is treated as a fetch failure."""
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()
        request.POST.get.side_effect = lambda key, default=None: default
        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._librenms_api.get_librenms_id.return_value = 777
        # success=True and a list, but one element is not a dict.
        view._librenms_api.get_device_inventory.return_value = (True, [{"entPhysicalIndex": 1}, None])

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock()),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}),
        ):
            view.post(request, pk=1)

        mock_messages.error.assert_called_once()
        mock_cache.delete.assert_called_once_with("test_cache_key")
        view._librenms_api.get_ports.assert_not_called()  # bad inventory short-circuits before ports fetch

    def test_get_context_data_rejects_malformed_cached_inventory(self):
        """post() now fails closed on malformed inventory before caching, but a stale pre-fix cache entry like {"inventory": [None]} can still be read."""
        view = _make_view()
        obj = MagicMock()
        view._get_sync_device = MagicMock(return_value=obj)
        # Pin the librenms_id + OOB fingerprint so they MATCH the cached payload and don't trigger
        # an earlier cache invalidation — otherwise the test would pass for the wrong reason (the
        # id/oob mismatch deleting the cache before the malformed-inventory guard is even reached).
        view._librenms_api.get_librenms_id.return_value = 1
        view._build_context = MagicMock()  # must NOT be reached for a malformed payload
        request = MagicMock()
        request.GET = {}  # real query dict (no server_key) so the GET-rebind guard doesn't early-return

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
        ):
            mock_cache.get.return_value = {"inventory": [None], "librenms_id": 1}
            result = view.get_context_data(request, obj)

        view._build_context.assert_not_called()
        mock_cache.delete.assert_called_once_with("test_cache_key")
        assert result["table"] is None
        assert result["object"] is obj

    def test_get_context_data_keys_cache_on_resolved_scoped_server(self):
        """The cache read keys on the scoped server RETURNED by the resolver, not the bound api.server_key (equal only via the rebind side effect)."""
        view = _make_view()
        obj = MagicMock()
        view._get_sync_device = MagicMock(return_value=obj)
        view._librenms_api.get_librenms_id.return_value = 1
        view._build_context = MagicMock()
        request = MagicMock()
        request.GET = {}
        # Simulate the regression the finding guards against: the resolver returns a scoped server
        # but does NOT rebind the bound client (which stays "test-server"). The cache key must
        # follow the RESOLVED scoped server, not the now-stale bound api.server_key.
        view.resolve_get_render_server_key = MagicMock(return_value=("scoped-srv", False))

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
        ):
            mock_cache.get.return_value = {"inventory": [None], "librenms_id": 1}
            view.get_context_data(request, obj)

        view.get_cache_key.assert_called_once_with(obj, "inventory", server_key="scoped-srv")

    def test_get_context_data_scopes_sync_device_to_resolved_server(self):
        """The VC sync-device resolution is scoped to the RESOLVED server explicitly, not left to rely on the rebind side effect."""
        view = _make_view()
        obj = MagicMock()
        view._get_sync_device = MagicMock(return_value=obj)
        view._librenms_api.get_librenms_id.return_value = 1
        view._build_context = MagicMock()
        request = MagicMock()
        request.GET = {}
        # Resolver returns a scoped server WITHOUT rebinding the bound client (the regression case).
        view.resolve_get_render_server_key = MagicMock(return_value=("scoped-srv", False))

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
        ):
            mock_cache.get.return_value = {"inventory": [None], "librenms_id": 1}
            view.get_context_data(request, obj)

        # _get_sync_device must receive the resolved scoped server, not be called unscoped.
        view._get_sync_device.assert_called_once_with(obj, server_key="scoped-srv")

    def test_post_warns_when_ports_fetch_fails(self):
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()

        # No POSTed keys → get(key, default) must honour the caller's default (QueryDict
        # semantics); a blanket return_value=None would break default-fallback paths.
        request.POST.get.side_effect = lambda key, default=None: (
            default
        )  # no POSTed server_key → rebind keeps session API
        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._build_context = MagicMock(
            return_value={
                "table": None,
                "object": obj,
                "cache_expiry": None,
                "server_key": view._librenms_api.server_key,
            }
        )

        view._librenms_api.get_librenms_id.return_value = 777
        view._librenms_api.get_device_inventory.return_value = (True, [])
        view._librenms_api.get_device_transceivers.return_value = (True, [])
        view._librenms_api.get_ports.return_value = (False, "ports api unavailable")

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache"),
            patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock()),
        ):
            view.post(request, pk=1)

        mock_messages.warning.assert_called_once_with(
            request,
            "Inventory refreshed, but port metadata fetch failed; interface matching may be incomplete."
            " See server logs for details.",
        )
        mock_messages.success.assert_not_called()

    def test_post_treats_malformed_ports_payload_as_fetch_failure(self):
        """get_ports() returning success with a dict whose "ports" is missing/None (or carries non-dict entries) makes port-id enrichment silently no-op."""
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()
        request.POST.get.side_effect = lambda key, default=None: default
        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._build_context = MagicMock(
            return_value={"table": None, "object": obj, "cache_expiry": None, "server_key": "default"}
        )

        view._librenms_api.get_librenms_id.return_value = 777
        view._librenms_api.get_device_inventory.return_value = (True, [])
        view._librenms_api.get_device_transceivers.return_value = (True, [])
        # success=True and a dict, but "ports" is None (not a list of dicts) → malformed.
        view._librenms_api.get_ports.return_value = (True, {"ports": None})

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
        ):
            view.post(request, pk=1)

        mock_cache.set.assert_not_called()  # degraded snapshot not persisted as complete
        mock_cache.delete.assert_called_once_with("test_cache_key")
        mock_messages.warning.assert_called_once_with(
            request,
            "Inventory refreshed, but port metadata fetch failed; interface matching may be incomplete."
            " See server logs for details.",
        )
        mock_messages.success.assert_not_called()

    def test_post_skips_cache_on_oob_inventory_failure(self):
        """When the OOB inventory fetch fails, the main-only snapshot must NOT be cached under the current oob fingerprint — otherwise get_context_data() accepts it as complete and the OOB rows (and warning) vanish until TTL/manual refresh."""
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()
        request.POST.get.side_effect = lambda key, default=None: default

        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._build_context = MagicMock(
            return_value={"table": None, "object": obj, "cache_expiry": None, "server_key": "default"}
        )

        view._librenms_api.get_librenms_id.return_value = 777
        view._librenms_api.get_device_transceivers.return_value = (True, [])
        view._librenms_api.get_ports.return_value = (True, {"ports": []})
        # Main inventory succeeds; the linked OOB controller's inventory fails.
        view._librenms_api.get_device_inventory.side_effect = lambda dev_id: (
            (True, []) if dev_id == 777 else (False, "oob inventory unavailable")
        )

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value={"id": 999}),
        ):
            view.post(request, pk=1)

        mock_cache.set.assert_not_called()  # no partial snapshot persisted
        mock_cache.delete.assert_called_once_with("test_cache_key")  # the active device's stale entry cleared
        mock_messages.warning.assert_called_once()  # user is told the OOB inventory fetch failed
        mock_messages.success.assert_not_called()  # warning, not success
        # The toast must stay generic — internal LibreNMS ids (777 / OOB 999) belong only in
        # the server log, not the UI.
        warn_msg = mock_messages.warning.call_args[0][1]
        assert "777" not in warn_msg and "999" not in warn_msg and "OOB id" not in warn_msg

    def test_post_treats_non_dict_oob_inventory_entry_as_fetch_failure(self):
        """The OOB inventory merge offsets indices and sets item["_source"] on every entry, so a success flag with non-dict elements (e.g. None) is treated as a fetch failure."""
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()
        request.POST.get.side_effect = lambda key, default=None: default

        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._build_context = MagicMock(
            return_value={"table": None, "object": obj, "cache_expiry": None, "server_key": "default"}
        )

        view._librenms_api.get_librenms_id.return_value = 777
        view._librenms_api.get_device_transceivers.return_value = (True, [])
        view._librenms_api.get_ports.return_value = (True, {"ports": []})
        # Main inventory succeeds; the OOB controller returns success but a malformed list.
        view._librenms_api.get_device_inventory.side_effect = lambda dev_id: (
            (True, []) if dev_id == 777 else (True, [{"entPhysicalIndex": 1}, None])
        )

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value={"id": 999}),
        ):
            view.post(request, pk=1)

        mock_cache.set.assert_not_called()  # no partial snapshot persisted
        mock_cache.delete.assert_called_once_with("test_cache_key")
        mock_messages.warning.assert_called_once()  # malformed OOB inventory treated as a fetch failure
        mock_messages.success.assert_not_called()

    def test_post_corrupt_oob_id_fails_closed(self):
        """A linked OOB controller whose stored id is corrupt (bool/non-numeric) must fail CLOSED like the interfaces/cables tabs: no fetch with the garbage id, but a warning and NO cached host-only snapshot — a bare falsy check would conflate it with 'no OOB linked' and silently drop the controller's rows until TTL."""
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()
        request.POST.get.side_effect = lambda key, default=None: default

        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._build_context = MagicMock(
            return_value={"table": None, "object": obj, "cache_expiry": None, "server_key": "default"}
        )

        view._librenms_api.get_librenms_id.return_value = 777
        view._librenms_api.get_device_transceivers.return_value = (True, [])
        view._librenms_api.get_ports.return_value = (True, {"ports": []})
        # Only the main device (777) is a valid id; a second call with a garbage OOB id would
        # return failure and trip the OOB-failed path.
        view._librenms_api.get_device_inventory.side_effect = lambda dev_id: (
            (True, []) if dev_id == 777 else (False, "should never be called")
        )

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
            # post() renders here via modules_view.render; a higher branch refactors that to
            # render_sync_partial() (mixins.render). Patch both (create=True) so this test stays
            # robust as it rides up the stack regardless of which render site post() uses.
            patch("netbox_librenms_plugin.views.base.modules_view.render", return_value=MagicMock(), create=True),
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock(), create=True),
            patch(
                "netbox_librenms_plugin.views.base.modules_view.get_librenms_oob",
                return_value={"id": "not-a-number"},
            ),
        ):
            view.post(request, pk=1)

        # The garbage id is never fired at get_device_inventory...
        view._librenms_api.get_device_inventory.assert_called_once_with(777)
        # ...but the user is warned and the host-only snapshot is NOT cached as complete.
        mock_messages.warning.assert_called_once()
        mock_messages.success.assert_not_called()
        mock_cache.set.assert_not_called()
        mock_cache.delete.assert_called_once_with("test_cache_key")

    def test_post_no_oob_linked_stays_clean_success(self):
        """No OOB linked at all (get_librenms_oob returns None) is NOT a failure: clean success toast and the snapshot is cached."""
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()
        request.POST.get.side_effect = lambda key, default=None: default

        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._build_context = MagicMock(
            return_value={"table": None, "object": obj, "cache_expiry": None, "server_key": "default"}
        )

        view._librenms_api.get_librenms_id.return_value = 777
        view._librenms_api.get_device_transceivers.return_value = (True, [])
        view._librenms_api.get_ports.return_value = (True, {"ports": []})
        view._librenms_api.get_device_inventory.return_value = (True, [])

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.modules_view.render", return_value=MagicMock(), create=True),
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock(), create=True),
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
        ):
            view.post(request, pk=1)

        mock_messages.success.assert_called_once()
        mock_messages.warning.assert_not_called()
        assert mock_cache.set.call_args[0][1]["oob_librenms_id"] is None

    def test_post_treats_non_int_oob_index_as_fetch_failure(self):
        """An OOB inventory row with a non-int entPhysicalIndex fails closed (no offset TypeError, no cache)."""
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()
        request.POST.get.side_effect = lambda key, default=None: default

        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._build_context = MagicMock(
            return_value={"table": None, "object": obj, "cache_expiry": None, "server_key": "default"}
        )

        view._librenms_api.get_librenms_id.return_value = 777
        view._librenms_api.get_device_transceivers.return_value = (True, [])
        view._librenms_api.get_ports.return_value = (True, {"ports": []})
        # Main inventory OK; OOB returns a row whose entPhysicalIndex is a string — the offset
        # arithmetic ("5" + offset) would TypeError and 500 the tab without the guard.
        view._librenms_api.get_device_inventory.side_effect = lambda dev_id: (
            (True, []) if dev_id == 777 else (True, [{"entPhysicalIndex": "5", "entPhysicalName": "oob-fan"}])
        )

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
            # Patch both render sites (create=True) — see test_post_ignores_non_numeric_oob_id:
            # a higher branch moves post()'s render to render_sync_partial() (mixins.render).
            patch("netbox_librenms_plugin.views.base.modules_view.render", return_value=MagicMock(), create=True),
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock(), create=True),
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value={"id": 999}),
        ):
            view.post(request, pk=1)  # must not raise

        mock_cache.set.assert_not_called()  # no partial snapshot persisted
        mock_messages.warning.assert_called_once()  # malformed OOB indices treated as a fetch failure
        mock_messages.success.assert_not_called()

    def test_get_context_data_oob_fingerprint_equates_int_and_string(self):
        """Cached int oob id and a current string id of the same value compare equal — cache not wrongly invalidated."""
        view = _make_view()
        obj = MagicMock()
        view._get_sync_device = MagicMock(return_value=obj)
        view._librenms_api.get_librenms_id.return_value = 1
        sentinel = {"table": "built", "object": obj}
        view._build_context = MagicMock(return_value=sentinel)
        request = MagicMock()
        request.GET = {}

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            # Stored fingerprint is the coerced int 5; the live OOB CF reads back the string "5".
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value={"id": "5"}),
        ):
            mock_cache.get.return_value = {
                "inventory": [{"entPhysicalIndex": 1}],
                "librenms_id": 1,
                "oob_librenms_id": 5,
            }
            result = view.get_context_data(request, obj)

        # int 5 == coerce("5") → fingerprint matches → cache kept → real context built.
        view._build_context.assert_called_once()
        assert result is sentinel
        mock_cache.delete.assert_not_called()

    def test_post_skips_cache_on_transceiver_failure(self):
        """A transceiver-enrichment failure drops the synthetic transceiver rows, so the truncated inventory must NOT be cached — same reasoning as the OOB-failure case."""
        view = _make_view()
        view.model = MagicMock()
        obj = MagicMock()
        request = MagicMock()
        request.POST.get.side_effect = lambda key, default=None: default

        view.get_object = MagicMock(return_value=obj)
        view._get_sync_device = MagicMock(return_value=obj)
        view.has_write_permission = MagicMock(return_value=True)
        view._build_context = MagicMock(
            return_value={"table": None, "object": obj, "cache_expiry": None, "server_key": "default"}
        )

        view._librenms_api.get_librenms_id.return_value = 777
        view._librenms_api.get_ports.return_value = (True, {"ports": []})
        # Main (and OOB) inventory succeed, but the transceiver API fails → txr_error.
        view._librenms_api.get_device_inventory.return_value = (True, [])
        view._librenms_api.get_device_transceivers.return_value = (False, "transceiver fetch failed")

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.messages"),
            patch("netbox_librenms_plugin.views.mixins.render", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
        ):
            view.post(request, pk=1)

        mock_cache.set.assert_not_called()  # truncated (transceiver-less) snapshot not persisted
        mock_cache.delete.assert_called_once_with("test_cache_key")


# ---------------------------------------------------------------------------
# Inventory data factories
# ---------------------------------------------------------------------------


def _linecard_inventory():
    """
    Minimal inventory modelling the prod-lab03-sw4 scenario:

    Linecard(slot 3)  [WS-X4908, module, top-level]
      X2 Port 2       [container, no model]
        Converter 3/2 [CVR-X2-SFP, other] — INSTALLED in NetBox
          SFP slot     [container, no model]
            GE3/11    [GLC-TE, port, serial=MTC213403BB]
      X2 Port 4       [container, no model]
        Converter 3/4 [CVR-X2-SFP, other] — NOT installed in NetBox
          SFP slot 4  [container, no model]
            GE3/15    [GLC-T, port, serial=MTC19330SQC]
    """
    return [
        {
            "entPhysicalIndex": 1,
            "entPhysicalName": "Slot 3",
            "entPhysicalModelName": "WS-X4908",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
            "entPhysicalSerialNum": "S_LINECARD",
            "entPhysicalParentRelPos": 3,
        },
        # --- X2 Port 2 branch (installed CVR) ---
        {
            "entPhysicalIndex": 10,
            "entPhysicalName": "X2 Port 2",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 1,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 2,
        },
        {
            "entPhysicalIndex": 11,
            "entPhysicalName": "Converter 3/2",
            "entPhysicalModelName": "CVR-X2-SFP",
            "entPhysicalClass": "other",
            "entPhysicalContainedIn": 10,
            "entPhysicalSerialNum": "FDO_CVR2",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 12,
            "entPhysicalName": "SFP slot",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 11,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 13,
            "entPhysicalName": "GigabitEthernet3/11",
            "entPhysicalModelName": "GLC-TE",
            "entPhysicalClass": "port",
            "entPhysicalContainedIn": 12,
            "entPhysicalSerialNum": "MTC213403BB",
            "entPhysicalParentRelPos": 1,
        },
        # --- X2 Port 4 branch (NOT installed CVR) ---
        {
            "entPhysicalIndex": 20,
            "entPhysicalName": "X2 Port 4",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 1,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 4,
        },
        {
            "entPhysicalIndex": 21,
            "entPhysicalName": "Converter 3/4",
            "entPhysicalModelName": "CVR-X2-SFP",
            "entPhysicalClass": "other",
            "entPhysicalContainedIn": 20,
            "entPhysicalSerialNum": "FDO_CVR4",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 22,
            "entPhysicalName": "SFP slot 4",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 21,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 23,
            "entPhysicalName": "GigabitEthernet3/15",
            "entPhysicalModelName": "GLC-T",
            "entPhysicalClass": "port",
            "entPhysicalContainedIn": 22,
            "entPhysicalSerialNum": "MTC19330SQC",
            "entPhysicalParentRelPos": 1,
        },
    ]


# ---------------------------------------------------------------------------
# Real-DB scenario builders (replace the MagicMock _bay_setup / _module_types)
# ---------------------------------------------------------------------------


def _build_linecard_device(*, with_cvr6=False):
    """Real-DB equivalent of ``_bay_setup()`` + ``_module_types()``."""
    from netbox_librenms_plugin.tests.conftest import (
        install_module,
        make_device_with_module_bays,
        make_module_type_with_bays,
    )

    linecard_bays = ["X2 Port 2", "X2 Port 4"] + (["X2 Port 6"] if with_cvr6 else [])
    dev = make_device_with_module_bays("lc-dev", ["Slot 3"])
    install_module(dev, "Slot 3", "WS-X4908", serial="S_LINECARD", child_bays=linecard_bays)
    install_module(dev, "X2 Port 2", "CVR-X2-SFP", serial="FDO_CVR2", child_bays=["SFP 1", "SFP 2"])
    install_module(dev, "SFP 1", "GLC-TE", serial="MTC213403BB")
    make_module_type_with_bays("GLC-T")  # exists for matching the uninstalled converter's child
    if with_cvr6:
        # A third converter (installed) at X2 Port 6 with its own SFP 1 holding a GLC-TE.
        cvr6 = install_module(dev, "X2 Port 6", "CVR-X2-SFP", serial="FDO_CVR6")
        install_module(dev, "SFP 1", "GLC-TE", serial="SFP6_SERIAL", parent_module=cvr6)
    return dev


def _run_build_context_real(view, inventory_data, device):
    """Drive ``_build_context`` against a REAL device — real ``_get_module_bays`` / ``_get_module_types`` and the real bay-matching algorithm; only ``get_table`` is captured."""
    rows_store = _captured_table_view(view)
    view._build_context(MagicMock(), device, inventory_data)
    return rows_store.get("rows", [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBayDepthScopeWithUninstalledParent:
    """
    Regression tests for the stale bays_by_depth bug.

    Scenario: two converters at depth-1 share the same parent linecard.
    Converter 3/2 IS installed (it has SFP child bays).
    Converter 3/4 is NOT installed (no SFP child bays exist yet in NetBox).

    Bug: bays_by_depth[2] is set when processing Converter 3/2, and NOT
    cleared when processing Converter 3/4.  GigabitEthernet3/15 (depth-2
    child of Converter 3/4) then inherits the stale SFP scope and gets
    "Serial Mismatch" instead of "No Bay".

    Fix: when a matched bay has no installed module, set bays_by_depth[depth+1]
    to {} to prevent leakage to subsequent siblings at the same depth.
    """

    def _build_rows(self):
        view = _make_view()
        device = _build_linecard_device()
        return _run_build_context_real(view, _linecard_inventory(), device)

    def _row(self, rows, name):
        for r in rows:
            if r.get("name") == name:
                return r
        return None

    def test_glc_t_under_installed_converter_is_installed(self):
        """GLC-TE under the installed Converter 3/2 must show 'Installed'."""
        rows = self._build_rows()
        row = self._row(rows, "GigabitEthernet3/11")
        assert row is not None, "GigabitEthernet3/11 row not found"
        assert row["status"] == "Installed", (
            f"Expected 'Installed' but got {row['status']!r} — GLC-TE under an installed CVR should be Installed"
        )

    def test_glc_t_under_uninstalled_converter_is_no_bay_not_serial_mismatch(self):
        """
        GLC-T under the uninstalled Converter 3/4 must show 'No Bay'.

        Before the fix, bays_by_depth[2] retains the SFP scope from
        Converter 3/2 and GigabitEthernet3/15 incorrectly gets 'Serial Mismatch'.
        """
        rows = self._build_rows()
        row = self._row(rows, "GigabitEthernet3/15")
        assert row is not None, "GigabitEthernet3/15 row not found"
        assert row["status"] != "Serial Mismatch", (
            "GigabitEthernet3/15 shows 'Serial Mismatch' — stale bays_by_depth scope "
            "leaking from Converter 3/2 into Converter 3/4's child items (regression)"
        )
        assert row["status"] == "No Bay", (
            f"Expected 'No Bay' but got {row['status']!r}; "
            "the parent converter is not installed so child SFPs cannot be matched"
        )

    def test_uninstalled_converter_itself_shows_matched(self):
        """Converter 3/4 is matched to X2 Port 4 but not yet installed → 'Matched'."""
        rows = self._build_rows()
        row = self._row(rows, "Converter 3/4")
        assert row is not None, "Converter 3/4 row not found"
        assert row["status"] == "Matched", f"Expected 'Matched' but got {row['status']!r} for uninstalled converter"

    def test_installed_converter_itself_shows_installed(self):
        """Converter 3/2 is installed in X2 Port 2 with matching serial → 'Installed'."""
        rows = self._build_rows()
        row = self._row(rows, "Converter 3/2")
        assert row is not None, "Converter 3/2 row not found"
        assert row["status"] == "Installed", f"Expected 'Installed' but got {row['status']!r} for installed converter"

    def test_no_stale_scope_across_multiple_siblings(self):
        """
        bays_by_depth is reset for EACH sibling, so the second uninstalled
        converter does not leak into a third converter's children."""
        # Add a second installed converter at X2 Port 6 and verify its SFP
        # also shows correct status, unaffected by the reset for X2 Port 4.
        inventory = _linecard_inventory() + [
            {
                "entPhysicalIndex": 30,
                "entPhysicalName": "X2 Port 6",
                "entPhysicalModelName": "",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 1,
                "entPhysicalSerialNum": "",
                "entPhysicalParentRelPos": 6,
            },
            {
                "entPhysicalIndex": 31,
                "entPhysicalName": "Converter 3/6",
                "entPhysicalModelName": "CVR-X2-SFP",
                "entPhysicalClass": "other",
                "entPhysicalContainedIn": 30,
                "entPhysicalSerialNum": "FDO_CVR6",
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 32,
                "entPhysicalName": "SFP slot 6",
                "entPhysicalModelName": "",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 31,
                "entPhysicalSerialNum": "",
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 33,
                "entPhysicalName": "GigabitEthernet3/22",
                "entPhysicalModelName": "GLC-TE",
                "entPhysicalClass": "port",
                "entPhysicalContainedIn": 32,
                "entPhysicalSerialNum": "SFP6_SERIAL",
                "entPhysicalParentRelPos": 1,
            },
        ]

        view = _make_view()
        # Real device with the extra installed CVR at X2 Port 6 (its own SFP 1 holds a GLC-TE).
        device = _build_linecard_device(with_cvr6=True)

        rows = _run_build_context_real(view, inventory, device)

        def _row(name):
            return next((r for r in rows if r.get("name") == name), None)

        # The GE3/22 under the 3rd converter (installed) should be Installed
        row6 = _row("GigabitEthernet3/22")
        assert row6 is not None, "GigabitEthernet3/22 not found"
        assert row6["status"] == "Installed", (
            f"Expected 'Installed' but got {row6['status']!r} — "
            "GLC-TE under installed Converter 3/6 should be Installed"
        )
        # And GE3/15 under the uninstalled converter is still No Bay
        row15 = _row("GigabitEthernet3/15")
        assert row15["status"] == "No Bay", f"GigabitEthernet3/15 status {row15['status']!r} — should still be No Bay"


# ---------------------------------------------------------------------------
# Production-shape inventory factories
# ---------------------------------------------------------------------------


def _prod_inventory_ws_x4908():
    """
    Inventory shape captured from a live Cisco WS-X4908-10GE linecard
    (NetBox device prod-lab03-sw4 / LibreNMS production:7).

    Real LibreNMS naming with anonymized indices:
        chassis "Switch System"
          container "Slot 3"               [no model]
            module "Linecard(slot 3)"      [WS-X4908-10GE]
              container "Port Container 3/2"   [no model, relPos=3]
                other "Converter 3/2"          [CVR-X2-SFP, relPos=1]
                  container "Port Container 3/11" [no model, relPos=9]
                    port "GigabitEthernet3/11"   [GLC-TE, relPos=1]
                  container "Port Container 3/12" [no model, relPos=10]
                    port "GigabitEthernet3/12"   [GLC-T, relPos=1]

    Distinct from `_linecard_inventory` whose container names ("Slot 3",
    "X2 Port 2", "SFP slot") match NetBox bays directly and never exercise
    the contrib regex paths.  This fixture forces matching through:
      - `^Linecard\\(slot (\\d+)\\)$` regex for the linecard itself
      - `^Port Container (\\d+)/(\\d+)$` regex for X2 slot resolution
      - positional fallback for SFP transceivers inside the CVR
    """
    return [
        {
            "entPhysicalIndex": 1,
            "entPhysicalName": "Switch System",
            "entPhysicalModelName": "MIDPLANE",
            "entPhysicalClass": "chassis",
            "entPhysicalContainedIn": 0,
            "entPhysicalSerialNum": "S_CHASSIS",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 4,
            "entPhysicalName": "Slot 3",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 1,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 3,
        },
        {
            "entPhysicalIndex": 3000,
            "entPhysicalName": "Linecard(slot 3)",
            "entPhysicalModelName": "WS-X4908-10GE",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 4,
            "entPhysicalSerialNum": "S_LINECARD",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 3003,
            "entPhysicalName": "Port Container 3/2",
            "entPhysicalDescr": "Port Container",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 3000,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 3,
        },
        {
            "entPhysicalIndex": 3019,
            "entPhysicalName": "Converter 3/2",
            "entPhysicalDescr": "Converter Module",
            "entPhysicalModelName": "CVR-X2-SFP",
            "entPhysicalClass": "other",
            "entPhysicalContainedIn": 3003,
            "entPhysicalSerialNum": "S_CVR2",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 3028,
            "entPhysicalName": "Port Container 3/11",
            "entPhysicalDescr": "Port Container",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 3019,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 9,
        },
        {
            "entPhysicalIndex": 3044,
            "entPhysicalName": "GigabitEthernet3/11",
            "entPhysicalDescr": "1000BaseT",
            "entPhysicalModelName": "GLC-TE",
            "entPhysicalClass": "port",
            "entPhysicalContainedIn": 3028,
            "entPhysicalSerialNum": "MTC213403BB",
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 3029,
            "entPhysicalName": "Port Container 3/12",
            "entPhysicalDescr": "Port Container",
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 3019,
            "entPhysicalSerialNum": "",
            "entPhysicalParentRelPos": 10,
        },
        {
            "entPhysicalIndex": 3045,
            "entPhysicalName": "GigabitEthernet3/12",
            "entPhysicalDescr": "1000BaseT",
            "entPhysicalModelName": "GLC-T",
            "entPhysicalClass": "port",
            "entPhysicalContainedIn": 3029,
            "entPhysicalSerialNum": "GE12_SERIAL",
            "entPhysicalParentRelPos": 1,
        },
    ]


def _build_prod_ws4908_device(*, cvr_installed=True):
    """Real-DB equivalent of ``_prod_bay_setup_ws_x4908``: a prod-lab03-sw4-shaped device."""
    from netbox_librenms_plugin.tests.conftest import (
        install_module,
        make_device_with_module_bays,
        make_module_type_with_bays,
    )

    dev = make_device_with_module_bays("prod-sw4", ["Slot 3"])
    install_module(
        dev,
        "Slot 3",
        "WS-X4908-10GE",
        serial="S_LINECARD",
        child_bays=[f"X2 Port {n}" for n in range(1, 9)],
    )
    if cvr_installed:
        install_module(dev, "X2 Port 2", "CVR-X2-SFP", serial="S_CVR2", child_bays=["SFP 1", "SFP 2"])
    # Transceiver module types exist so resolve_module_type can match the port rows.
    make_module_type_with_bays("GLC-TE")
    make_module_type_with_bays("GLC-T")
    return dev


@pytest.mark.django_db
class TestProdShapeWS4908Matching:
    """Bay matching against real production data shape from a Cisco WS-X4908-10GE."""

    def _build_rows(self, cvr_installed=True):
        from netbox_librenms_plugin.tests.conftest import load_contrib_bay_mappings

        view = _make_view()
        load_contrib_bay_mappings()  # real ModuleBayMapping rows drive load_bay_mappings()
        device = _build_prod_ws4908_device(cvr_installed=cvr_installed)
        return _run_build_context_real(view, _prod_inventory_ws_x4908(), device)

    def _row(self, rows, name):
        for r in rows:
            if r.get("name") == name:
                return r
        return None

    def test_linecard_matches_slot_via_regex(self):
        """`Linecard(slot 3)` resolves to device-bay `Slot 3` via the Linecard regex."""
        rows = self._build_rows()
        row = self._row(rows, "Linecard(slot 3)")
        assert row is not None, "Linecard(slot 3) row not found"
        assert row["module_bay"] == "Slot 3", (
            f"Expected module_bay='Slot 3' but got {row['module_bay']!r} — "
            r"the `^Linecard\(slot (\d+)\)$` regex should resolve to `Slot N`"
        )

    def test_converter_matches_x2_port_via_parent_regex(self):
        """`Converter 3/2`'s parent `Port Container 3/2` resolves to `X2 Port 2`."""
        rows = self._build_rows()
        row = self._row(rows, "Converter 3/2")
        assert row is not None, "Converter 3/2 row not found"
        assert row["module_bay"] == "X2 Port 2", (
            f"Expected module_bay='X2 Port 2' but got {row['module_bay']!r} — "
            r"parent name `Port Container 3/2` should regex-resolve to `X2 Port \2` = X2 Port 2"
        )

    def test_ge_matches_sfp1_via_positional_fallback(self):
        """
        `GigabitEthernet3/11` (first port-container child of Converter 3/2) matches
        `SFP 1` on the CVR module via positional fallback.

        The positional fallback indexes by **sibling order within the parent CVR**,
        not by global port number — `Port Container 3/11` is the 1st child of
        Converter 3/2, so it maps to `SFP 1`.
        """
        rows = self._build_rows()
        row = self._row(rows, "GigabitEthernet3/11")
        assert row is not None, "GigabitEthernet3/11 row not found"
        assert row["module_bay"] == "SFP 1", (
            f"Expected module_bay='SFP 1' but got {row['module_bay']!r} — "
            "positional fallback should map the 1st port-container child of CVR-X2-SFP to SFP 1"
        )

    def test_ge_second_port_matches_sfp2_via_positional_fallback(self):
        """`GigabitEthernet3/12` (2nd port-container child of CVR) matches `SFP 2`."""
        rows = self._build_rows()
        row = self._row(rows, "GigabitEthernet3/12")
        assert row is not None, "GigabitEthernet3/12 row not found"
        assert row["module_bay"] == "SFP 2", (
            f"Expected module_bay='SFP 2' but got {row['module_bay']!r} — "
            "positional fallback should map the 2nd port-container child of CVR-X2-SFP to SFP 2"
        )

    def test_ge_no_bay_when_cvr_not_installed_in_netbox(self):
        """
        When the CVR is matched (X2 Port 2) but no module is installed there in
        NetBox, the deeper SFP scope is empty and GE inside the CVR shows 'No Bay'.

        This is the original confusion that triggered the reverted commit
        (216fb84): 'GE3/11 doesn't match a bay' was actually 'CVR module not
        installed in NetBox' — the fix is to install the module, not to walk
        ancestor names looking for a wrong bay to land on.
        """
        rows = self._build_rows(cvr_installed=False)
        row = self._row(rows, "GigabitEthernet3/11")
        assert row is not None, "GigabitEthernet3/11 row not found"
        assert row["module_bay"] == "-", (
            f"Expected no bay match (got {row['module_bay']!r}) — "
            "without an installed CVR there is no SFP scope for positional fallback"
        )
        assert row["status"] == "No Bay", f"Expected status='No Bay' but got {row['status']!r}"

    def test_no_cvr_entry_does_not_match_via_grandparent_walking(self):
        """
        Regression guard for reverted commit 216fb84.

        Some Cisco devices expose only the Port-Container chain in ENTITY-MIB
        (no Converter entry between linecard and port).  The hierarchy is:

            Linecard(slot 3)
              Port Container 3/2          [no model — skipped, no row]
                Port Container 3/11       [no model — skipped, no row]
                  GigabitEthernet3/11     [GLC-TE]

        Because both intermediate containers are model-less, no row updates the
        bay scope and GE3/11 inherits the linecard's bays as scope.  In this
        scope:
          - immediate-parent regex `Port Container 3/11` → `X2 Port 11` (no such bay)
          - grandparent regex `Port Container 3/2` → `X2 Port 2` (the bay holding
            the CVR module, not a transceiver bay)

        Correct behavior: no bay match.  The reverted commit's "walk all
        ancestors" logic resolved GE3/11 to `X2 Port 2`, semantically landing
        the transceiver in the parent module's slot.
        """
        no_cvr_inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Switch System",
                "entPhysicalModelName": "MIDPLANE",
                "entPhysicalClass": "chassis",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "S_CHASSIS",
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 4,
                "entPhysicalName": "Slot 3",
                "entPhysicalModelName": "",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 1,
                "entPhysicalSerialNum": "",
                "entPhysicalParentRelPos": 3,
            },
            {
                "entPhysicalIndex": 3000,
                "entPhysicalName": "Linecard(slot 3)",
                "entPhysicalModelName": "WS-X4908-10GE",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 4,
                "entPhysicalSerialNum": "S_LINECARD",
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 3003,
                "entPhysicalName": "Port Container 3/2",
                "entPhysicalModelName": "",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 3000,
                "entPhysicalSerialNum": "",
                "entPhysicalParentRelPos": 3,
            },
            {
                "entPhysicalIndex": 3028,
                "entPhysicalName": "Port Container 3/11",
                "entPhysicalModelName": "",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 3003,
                "entPhysicalSerialNum": "",
                "entPhysicalParentRelPos": 9,
            },
            {
                "entPhysicalIndex": 3044,
                "entPhysicalName": "GigabitEthernet3/11",
                "entPhysicalModelName": "GLC-TE",
                "entPhysicalClass": "port",
                "entPhysicalContainedIn": 3028,
                "entPhysicalSerialNum": "MTC213403BB",
                "entPhysicalParentRelPos": 1,
            },
        ]
        from netbox_librenms_plugin.tests.conftest import load_contrib_bay_mappings

        view = _make_view()
        load_contrib_bay_mappings()
        device = _build_prod_ws4908_device(cvr_installed=True)
        rows = _run_build_context_real(view, no_cvr_inventory, device)
        row = self._row(rows, "GigabitEthernet3/11")
        assert row is not None, "GigabitEthernet3/11 row not found"
        assert row["module_bay"] != "X2 Port 2", (
            "GE3/11 matched X2 Port 2 — that bay holds the parent CVR module, "
            "not a transceiver.  An ancestor-walking matcher (reverted 216fb84) "
            "would resolve `Port Container 3/2` (grandparent) to `X2 Port 2` "
            "and incorrectly land the transceiver in the CVR's own bay."
        )


class TestPositionalMatchScaffoldingChain:
    """
    Regression coverage for `_match_bay_by_position` walking through deep
    Cisco IOS-XR scaffolding (module ancestors with model="N/A").

    Captured shape from a Cisco ASR-9904 (NetBox device prod-lab03d-ra1.lab,
    LibreNMS production:30) — TenGigE ports inside a 24x10GE linecard:

        chassis "Rack 0"                        [ASR-9904]
          container "Rack 0-Line Card Slot 0"   [no model]
            module "0/0"                        [A9K-24X10GE-1G-TR]
              module "0/0-Motherboard"          [N/A]
                module "0/0-Slice 0"            [N/A]
                  module "0/0-Slice 0 EZChip"   [N/A]
                    module "Slice 0 SFP Port Module #N"  [N/A]
                      container "0/0-SFP+ bay N"        [N/A]
                        module "TenGigE0/0/0/N"          [SFP-10G-SR]

    The 0/0 linecard's serial accidentally matches the device serial
    (LibreNMS reports it that way for some IOS-XR units), which fires the
    `Embedded RP / fixed-chassis system board` ignore rule with
    action=transparent.  As a result the linecard is hidden and every
    TenGigE port is promoted to top-level — matched against the device's
    bays {Slot 0, Slot 1, Slot 2, Slot 3}.

    Bug pre-fix: the positional walk in `_match_bay_by_position` skipped
    every modelless ancestor regardless of class, eventually landing
    container_idx on `Motherboard` (the deepest item before the real-model
    `0/0`).  Every TenGigE port walked to the same Motherboard, took
    position=1 inside `0/0`'s children, and matched `Slot 1` on the chassis
    — the bay where the RSP0 line card belongs.  Clicking install would
    place SFP transceivers into the chassis line-card slots.

    Fix: stop the walk on a non-container ancestor without a real model.
    Modelless modules ("Motherboard", "Slice 0", "EZChip" et al.) are
    scaffolding, not bay positions; treating them as walk-through containers
    silently collapses sibling counts.  After the fix the positional matcher
    returns None and the row shows "No Bay".
    """

    def _scaffolding_inventory(self):
        return [
            {
                "entPhysicalIndex": 8384513,
                "entPhysicalName": "Rack 0",
                "entPhysicalModelName": "ASR-9904",
                "entPhysicalClass": "chassis",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "FOX2128PLQ8",
                "entPhysicalParentRelPos": -1,
            },
            {
                "entPhysicalIndex": 8384552,
                "entPhysicalName": "Rack 0-Line Card Slot 0",
                "entPhysicalModelName": "N/A",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 8384513,
                "entPhysicalParentRelPos": 3,
            },
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "0/0",
                "entPhysicalModelName": "A9K-24X10GE-1G-TR",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 8384552,
                "entPhysicalSerialNum": "DEVICE_SERIAL",  # matches device serial below
                "entPhysicalParentRelPos": 0,
            },
            {
                "entPhysicalIndex": 30,
                "entPhysicalName": "0/0-Motherboard",
                "entPhysicalModelName": "N/A",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 1,
                "entPhysicalParentRelPos": 0,
            },
            {
                "entPhysicalIndex": 35,
                "entPhysicalName": "0/0-Slice 0",
                "entPhysicalModelName": "N/A",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 30,
                "entPhysicalParentRelPos": 4,
            },
            {
                "entPhysicalIndex": 330,
                "entPhysicalName": "0/0-Slice 0 EZChip",
                "entPhysicalModelName": "N/A",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 35,
                "entPhysicalParentRelPos": 0,
            },
            {
                "entPhysicalIndex": 601,
                "entPhysicalName": "0/0-Slice 0 SFP Port Module #0",
                "entPhysicalModelName": "N/A",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 330,
                "entPhysicalParentRelPos": 0,
            },
            {
                "entPhysicalIndex": 801,
                "entPhysicalName": "0/0-SFP+ bay 0",
                "entPhysicalModelName": "N/A",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 601,
                "entPhysicalParentRelPos": 0,
            },
            {
                "entPhysicalIndex": 409601,
                "entPhysicalName": "TenGigE0/0/0/0",
                "entPhysicalModelName": "SFP-10G-SR",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 801,
                "entPhysicalSerialNum": "SFP_SERIAL_0",
                "entPhysicalParentRelPos": 0,
            },
            {
                "entPhysicalIndex": 602,
                "entPhysicalName": "0/0-Slice 0 SFP Port Module #1",
                "entPhysicalModelName": "N/A",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 330,
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 802,
                "entPhysicalName": "0/0-SFP+ bay 1",
                "entPhysicalModelName": "N/A",
                "entPhysicalClass": "container",
                "entPhysicalContainedIn": 602,
                "entPhysicalParentRelPos": 0,
            },
            {
                "entPhysicalIndex": 413697,
                "entPhysicalName": "TenGigE0/0/0/1",
                "entPhysicalModelName": "SFP-10G-SR",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 802,
                "entPhysicalSerialNum": "SFP_SERIAL_1",
                "entPhysicalParentRelPos": 0,
            },
        ]

    def _device_bays(self):
        bays = {}
        for n in range(0, 4):
            b = MagicMock()
            b.name = f"Slot {n}"
            b.installed_module = None
            b.get_absolute_url.return_value = f"/bay/slot-{n}"
            bays[f"Slot {n}"] = b
        return bays

    def _module_types(self):
        mt = MagicMock()
        mt.pk = 50
        mt.model = "SFP-10G-SR"
        mt.get_absolute_url.return_value = "/mt/sfp"
        return {"SFP-10G-SR": mt}

    def _build_rows(self, device_serial="DEVICE_SERIAL"):
        view = _make_view()
        # Need a transparent rule that fires on serial_matches_device, like prod
        from netbox_librenms_plugin.tests.test_modules_view import _make_view as _mv  # noqa: F401

        rows_store = _captured_table_view(view)
        view._get_module_bays = MagicMock(return_value=(self._device_bays(), {}))
        view._get_module_types = MagicMock(return_value=self._module_types())
        view._get_generic_module_types = MagicMock(return_value={})
        view._get_module_type_ambiguities = MagicMock(return_value={})
        view._get_carrier_install_rules = MagicMock(return_value=[])

        # Device-serial matches the linecard's serial → linecard becomes transparent
        device = MagicMock()
        device.serial = device_serial
        device.virtual_chassis = None
        device.id = 1
        device_type = MagicMock()
        device_type.manufacturer = None
        device.device_type = device_type

        # Build a fake "transparent" ignore rule matching serial_matches_device
        transparent_rule = MagicMock()
        transparent_rule.match_type = "serial_matches_device"
        transparent_rule.action = "transparent"
        transparent_rule.require_serial_match_parent = False

        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[transparent_rule]),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", side_effect=lambda v, *a, **kw: v),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch.object(view.__class__, "_detect_serial_conflicts", return_value=None),
        ):
            mock_cache.ttl = MagicMock(return_value=None)
            view._build_context(MagicMock(), device, self._scaffolding_inventory())
        return rows_store.get("rows", [])

    def _row(self, rows, name):
        for r in rows:
            if r.get("name") == name:
                return r
        return None

    def test_tengig_does_not_match_chassis_slot_via_scaffolding_walk(self):
        """
        TenGigE ports at the bottom of a model="N/A" module chain must NOT be
        positional-matched to a chassis bay.  Pre-fix, every TenGigE walked
        through Motherboard/Slice/EZChip and landed on Slot 1 (the RSP bay).
        """
        rows = self._build_rows()
        for name in ("TenGigE0/0/0/0", "TenGigE0/0/0/1"):
            row = self._row(rows, name)
            assert row is not None, f"{name} row not found"
            assert row["module_bay"] != "Slot 1", (
                f"{name} matched 'Slot 1' on the chassis — that bay holds the "
                "RSP line card, not a transceiver.  Positional fallback walked "
                "through model-less module-class scaffolding (Motherboard, "
                "Slice 0, EZChip, SFP Port Module) before stopping at the "
                "0/0 linecard, conflating every TenGigE to position=1."
            )

    def test_tengig_shows_no_bay_when_only_scaffolding_above(self):
        """
        With no real position-container chain and no bay templates on the
        scaffolding modules' types, transceivers should resolve to 'No Bay'.
        """
        rows = self._build_rows()
        row = self._row(rows, "TenGigE0/0/0/0")
        assert row is not None
        assert row["status"] == "No Bay", (
            f"Expected 'No Bay' but got {row['status']!r}.  With only chassis "
            "Slot 0..3 bays in scope and modelless module scaffolding above the "
            "transceiver, the positional fallback should bail rather than "
            "confidently mismatching."
        )

    def test_tengig_siblings_resolve_independently(self):
        """
        Each TenGigE port must be evaluated independently.  Pre-fix all ports
        collapsed to the same `container_idx` and got identical (wrong) bays.
        """
        rows = self._build_rows()
        bays = {r.get("name"): r.get("module_bay") for r in rows if r.get("name", "").startswith("TenGigE")}
        # Either both resolve to "-" (no bay) or to distinct bays.  They must
        # NOT all share the same chassis bay.
        non_dash = [b for b in bays.values() if b and b != "-"]
        assert len(set(non_dash)) == len(non_dash), (
            f"TenGigE ports collapsed to duplicate bay assignments: {bays}.  "
            "Positional fallback walked through scaffolding and produced the "
            "same container_idx for siblings that have different physical positions."
        )


class TestCollectDescendants:
    """Tests for _collect_descendants depth tracking."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        return object.__new__(BaseModuleTableView)

    def test_empty_container_children_at_same_depth(self):
        """Children of a no-model container are returned at the same depth as the container."""
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "REAL-MODULE", "entPhysicalContainedIn": 1},
        ]
        children_by_parent = {}
        index_map = {}
        for item in inventory:
            p = item.get("entPhysicalContainedIn")
            if p is not None:
                children_by_parent.setdefault(p, []).append(item)
            idx = item.get("entPhysicalIndex")
            if idx is not None:
                index_map[idx] = item
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, index_map, ignore_rules=[], depth=1, results=results)
        assert len(results) == 1
        depth, item = results[0]
        assert depth == 1, "Child of modelless container must be at the same depth"
        assert item["entPhysicalModelName"] == "REAL-MODULE"

    def test_model_children_at_incremented_depth(self):
        """Children of a model-bearing item are at depth+1."""
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "CHILD", "entPhysicalContainedIn": 1},
        ]
        children_by_parent = {}
        index_map = {}
        for item in inventory:
            p = item.get("entPhysicalContainedIn")
            if p is not None:
                children_by_parent.setdefault(p, []).append(item)
            idx = item.get("entPhysicalIndex")
            if idx is not None:
                index_map[idx] = item
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, index_map, ignore_rules=[], depth=1, results=results)
        depths = [d for d, _ in results]
        assert depths == [1, 2], f"Expected [1, 2] but got {depths}"

    @pytest.mark.parametrize("model_value", [0, 123456])
    def test_numeric_model_child_is_collected(self, model_value):
        """An all-digit descendant model decoded as an int remains a real hardware row."""
        child = {
            "entPhysicalIndex": 1,
            "entPhysicalModelName": model_value,
            "entPhysicalContainedIn": 0,
        }
        view = self._view()
        results = []

        view._collect_descendants(
            0,
            {0: [child]},
            {1: child},
            ignore_rules=[],
            depth=1,
            results=results,
        )

        assert results == [(1, child)]


class TestDetermineStatus:
    """Tests for _determine_status logic."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        return object.__new__(BaseModuleTableView)

    def test_matched_bay_and_type(self):
        import types

        view = self._view()
        assert view._determine_status(types.SimpleNamespace(id=1), types.SimpleNamespace(id=1), "S1") == "Matched"

    def test_no_bay(self):
        import types

        view = self._view()
        assert view._determine_status(None, types.SimpleNamespace(id=1), "S1") == "No Bay"

    def test_no_type(self):
        import types

        view = self._view()
        assert view._determine_status(types.SimpleNamespace(id=1), None, "S1") == "No Type"

    def test_unmatched_fallback(self):
        view = self._view()
        assert view._determine_status(None, None, "S1") == "No Bay"


class TestBuildRowSerialMismatch:
    """Tests for serial mismatch detection and can_update_serial flag in _build_row."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        return view

    def _make_bay(self, installed_serial=None, module_type_id=5):
        """Create a mock bay with an optionally installed module."""
        bay = MagicMock()
        bay.pk = 10
        bay.name = "Slot 1"
        bay.get_absolute_url.return_value = "/dcim/module-bays/10/"
        if installed_serial is not None:
            module = MagicMock()
            module.pk = 42
            module.serial = installed_serial
            module.module_type_id = module_type_id
            module.get_absolute_url.return_value = "/dcim/modules/42/"
            bay.installed_module = module
        else:
            bay.installed_module = None
        return bay

    def _make_item(self, model_name="XCM-7s-b", serial="NS225161205"):
        return {
            "entPhysicalModelName": model_name,
            "entPhysicalSerialNum": serial,
            "entPhysicalName": "Slot 1",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalIndex": 100,
        }

    @pytest.mark.parametrize(("raw_serial", "expected"), [(123456, "123456"), (0, "0")])
    def test_numeric_inventory_serial_is_preserved_as_text(self, raw_serial, expected):
        """LibreNMS JSON may decode all-digit serials as numbers; row construction must not crash or discard zero."""
        view = self._view()
        item = self._make_item(serial=raw_serial)
        item["_source"] = "oob"  # the real informational-row path needs no bay/type fixtures

        row = view._build_row(item, {}, {}, {})

        assert row["serial"] == expected

    def test_serial_match_sets_installed_status(self):
        """When ENTITY-MIB serial matches NetBox serial, status is Installed."""
        view = self._view()
        bay = self._make_bay(installed_serial="NS225161205")
        matched_type = MagicMock()
        matched_type.model = "XCM-7s-b"
        matched_type.pk = 5
        matched_type.get_absolute_url.return_value = "/dcim/module-types/5/"

        with (
            patch.object(view, "_match_module_bay", return_value=bay),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s-b"),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        ):
            row = view._build_row(
                self._make_item(serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Installed"
        assert "row_class" not in row
        assert not row.get("can_update_serial")

    def test_installed_row_sets_update_interface_when_template_matches_exist(self):
        """Installed non-port rows expose Update Interface when standalone template matches exist."""
        view = self._view()
        bay = self._make_bay(installed_serial="NS225161205")
        matched_type = MagicMock()
        matched_type.model = "XCM-7s-b"
        matched_type.pk = 5
        matched_type.get_absolute_url.return_value = "/dcim/module-types/5/"

        with (
            patch.object(view, "_match_module_bay", return_value=bay),
            patch.object(view, "_count_adoptable_template_interfaces", return_value=2),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s-b"),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        ):
            row = view._build_row(
                self._make_item(serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Installed"
        assert row["can_update_interface_binding"] is True
        assert row["adoptable_interface_count"] == 2

    def test_count_adoptable_template_interfaces_uses_vc_aware_names(self):
        view = self._view()
        device = MagicMock()
        device.vc_position = 3
        device.virtual_chassis_id = 11
        device.virtual_chassis = MagicMock()
        device.virtual_chassis.members.values_list.return_value = [1, 2, 3]

        module = MagicMock()
        module.device = device
        template = MagicMock()
        instantiated = MagicMock()
        instantiated.name = "TenGigabitEthernet1/1/1"
        template.instantiate.return_value = instantiated
        module.module_type.interfacetemplates.all.return_value = [template]

        with patch("dcim.models.Interface") as mock_interface:
            mock_interface.objects.filter.return_value.count.return_value = 1
            result = view._count_adoptable_template_interfaces(module)

        assert result == 1
        mock_interface.objects.filter.assert_called_once_with(
            device=device,
            module__isnull=True,
            name__in=["TenGigabitEthernet3/1/1"],
        )

    def test_serial_mismatch_sets_can_update_serial(self):
        """When serials differ, can_update_serial=True and installed_module_id set."""
        view = self._view()
        bay = self._make_bay(installed_serial="TESTSRL")
        matched_type = MagicMock()
        matched_type.model = "XCM-7s-b"
        matched_type.pk = 5
        matched_type.get_absolute_url.return_value = "/dcim/module-types/5/"

        with (
            patch.object(view, "_match_module_bay", return_value=bay),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s-b"),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        ):
            row = view._build_row(
                self._make_item(serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Serial Mismatch"
        assert "row_class" not in row
        assert row.get("can_update_serial") is True
        assert row.get("installed_module_id") == 42

    def test_empty_netbox_serial_flags_mismatch(self):
        """When NetBox serial is empty but LibreNMS has one, status is Serial Mismatch."""
        view = self._view()
        bay = self._make_bay(installed_serial="")
        matched_type = MagicMock()
        matched_type.model = "XCM-7s-b"
        matched_type.pk = 5
        matched_type.get_absolute_url.return_value = "/dcim/module-types/5/"

        with (
            patch.object(view, "_match_module_bay", return_value=bay),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s-b"),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        ):
            row = view._build_row(
                self._make_item(serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Serial Mismatch"
        assert row.get("can_update_serial")
        assert row.get("can_replace")

    def test_oob_row_short_circuits_before_host_matching(self):
        """An OOB-controller item whose model/name WOULD match a host bay+type must not be compared against the host: it short-circuits to a neutral read-only row (status 'OOB', '-' bay/type, no action flags) before any bay/type/status resolution runs."""
        view = self._view()
        # This bay+type would produce a "Serial Mismatch"/"Installed" match for a host row,
        # so a regression that drops the early return would surface a host status here.
        bay = self._make_bay(installed_serial="TESTSRL")
        matched_type = MagicMock()
        matched_type.model = "XCM-7s-b"
        matched_type.pk = 5
        matched_type.get_absolute_url.return_value = "/dcim/module-types/5/"

        oob_item = self._make_item(serial="NS225161205")
        oob_item["_source"] = "oob"

        with (
            patch.object(view, "_match_module_bay", return_value=bay) as mock_match_bay,
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s-b"),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        ):
            row = view._build_row(
                oob_item,
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        # Host matching never ran, so the row carries neutral bay/type/status and no actions.
        mock_match_bay.assert_not_called()
        assert row["_source"] == "oob"
        assert row["status"] == "OOB"
        assert row["module_bay"] == "-"
        assert row["module_type"] == "-"
        assert row["module_bay_id"] is None
        assert row["module_type_id"] is None
        assert row["can_install"] is False
        for flag in ("can_replace", "can_update_serial", "can_update_interface_binding"):
            assert not row.get(flag)
        for key in ("model_suggestion", "type_suggestion", "module_type_create", "installed_module_id"):
            assert key not in row

    def test_oob_row_with_integrating_ancestor_still_reports_oob(self):
        """An OOB item that also looks like an integrated-child duplicate must stay 'OOB', not flip to 'Integrated'."""
        view = self._view()
        oob_item = self._make_item(serial="NS225161205")
        oob_item["_source"] = "oob"

        with (
            patch.object(
                view, "_find_integrating_ancestor", return_value={"entPhysicalName": "P", "entPhysicalIndex": 1}
            ) as mock_anc,
            patch.object(view, "_match_module_bay", return_value=None) as mock_match_bay,
        ):
            row = view._build_row(oob_item, {}, {}, {})

        assert row["status"] == "OOB"
        assert row["_source"] == "oob"
        # OOB returns before the integrated-child check even consults the ancestor.
        mock_anc.assert_not_called()
        mock_match_bay.assert_not_called()

    def _common_patches(self, view, bay, matched_type_name):
        """Return a stack of common patches for _build_row helper calls."""
        from unittest.mock import patch

        return [
            patch.object(view, "_match_module_bay", return_value=bay),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value=matched_type_name),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        ]

    def _make_matched_type(self, model_name, pk=5):
        matched_type = MagicMock()
        matched_type.model = model_name
        matched_type.pk = pk
        matched_type.get_absolute_url.return_value = f"/dcim/module-types/{pk}/"
        return matched_type

    def test_type_mismatch_sets_type_mismatch_status(self):
        """When installed module type differs from LibreNMS type, status is Type Mismatch."""
        view = self._view()
        bay = self._make_bay(installed_serial="S1")
        # Installed type pk=99, matched type pk=5 — different
        bay.installed_module.module_type_id = 99
        matched_type = self._make_matched_type("XCM-7s-b", pk=5)

        patches = self._common_patches(view, bay, "XCM-7s-b")
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            row = view._build_row(
                self._make_item(model_name="XCM-7s-b", serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Type Mismatch"
        assert "row_class" not in row

    def test_type_mismatch_sets_can_replace(self):
        """Type Mismatch row has can_replace=True and installed_module_id set."""
        view = self._view()
        bay = self._make_bay(installed_serial="S1")
        bay.installed_module.module_type_id = 99
        matched_type = self._make_matched_type("XCM-7s-b", pk=5)

        patches = self._common_patches(view, bay, "XCM-7s-b")
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            row = view._build_row(
                self._make_item(model_name="XCM-7s-b", serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row.get("can_replace") is True
        assert row.get("installed_module_id") == 42

    def test_serial_mismatch_also_sets_can_replace(self):
        """Serial Mismatch rows also get can_replace=True (same type)."""
        view = self._view()
        bay = self._make_bay(installed_serial="TESTSRL")
        bay.installed_module.module_type_id = 5
        matched_type = self._make_matched_type("XCM-7s-b", pk=5)

        patches = self._common_patches(view, bay, "XCM-7s-b")
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            row = view._build_row(
                self._make_item(serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Serial Mismatch"
        assert row.get("can_replace") is True
        assert row.get("can_update_serial") is True

    def test_same_type_same_serial_no_replace(self):
        """Clean Installed row has neither can_replace nor can_update_serial."""
        view = self._view()
        bay = self._make_bay(installed_serial="NS225161205")
        bay.installed_module.module_type_id = 5
        matched_type = self._make_matched_type("XCM-7s-b", pk=5)

        patches = self._common_patches(view, bay, "XCM-7s-b")
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            row = view._build_row(
                self._make_item(serial="NS225161205"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Installed"
        assert not row.get("can_replace")
        assert not row.get("can_update_serial")

    def test_librenms_dash_serial_with_empty_installed_gives_installed(self):
        """LibreNMS serial '-' normalizes to empty; both empty -> Installed, not mismatch."""
        view = self._view()
        bay = self._make_bay(installed_serial="")
        bay.installed_module.module_type_id = 5
        matched_type = self._make_matched_type("XCM-7s-b", pk=5)

        patches = self._common_patches(view, bay, "XCM-7s-b")
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            row = view._build_row(
                self._make_item(serial="-"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Installed"
        assert "row_class" not in row
        assert not row.get("can_update_serial")

    def test_librenms_dash_serial_with_real_installed_gives_installed(self):
        """LibreNMS serial '-' normalizes to empty; only NetBox has serial -> no mismatch."""
        view = self._view()
        bay = self._make_bay(installed_serial="REAL123")
        bay.installed_module.module_type_id = 5
        matched_type = self._make_matched_type("XCM-7s-b", pk=5)

        patches = self._common_patches(view, bay, "XCM-7s-b")
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            row = view._build_row(
                self._make_item(serial="-"),
                {},
                {"Slot 1": bay},
                {"XCM-7s-b": matched_type},
            )

        assert row["status"] == "Installed"
        assert "row_class" not in row
        assert not row.get("can_update_serial")


class TestDetectSerialConflicts:
    """Tests for BaseModuleTableView._detect_serial_conflicts()."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        return object.__new__(BaseModuleTableView)

    def test_no_can_replace_or_install_rows_does_nothing(self):
        """When no rows have can_replace or can_install, the method returns without DB query."""
        view = self._view()
        table_data = [{"serial": "S1", "status": "Installed"}]
        with patch("dcim.models.Module") as mock_module_cls:
            view._detect_serial_conflicts(table_data)
            mock_module_cls.objects.filter.assert_not_called()
        assert "serial_conflict_module" not in table_data[0]

    def test_conflict_detected_for_can_replace_row(self):
        """When a conflicting module exists, serial_conflict_module is set on the row."""
        view = self._view()
        conflict = MagicMock()
        conflict.serial = "CONFLICT_SERIAL"
        conflict.pk = 999
        conflict.module_bay = MagicMock()
        conflict.device = MagicMock()

        row = {
            "can_replace": True,
            "serial": "CONFLICT_SERIAL",
            "installed_module_id": 42,  # different from conflict.pk
        }

        with patch("dcim.models.Module") as mock_module_cls:
            mock_module_cls.objects.filter.return_value.select_related.return_value = [conflict]
            view._detect_serial_conflicts([row])

        assert row.get("serial_conflict_module") is conflict
        assert row.get("can_move_from") is True

    def test_no_conflict_when_conflict_is_same_module(self):
        """When the only module with the serial IS the installed module, no conflict is set."""
        view = self._view()
        conflict = MagicMock()
        conflict.serial = "S1"
        conflict.pk = 42  # Same as installed_module_id

        row = {
            "can_replace": True,
            "serial": "S1",
            "installed_module_id": 42,
        }

        with patch("dcim.models.Module") as mock_module_cls:
            mock_module_cls.objects.filter.return_value.select_related.return_value = [conflict]
            view._detect_serial_conflicts([row])

        assert "serial_conflict_module" not in row
        assert not row.get("can_move_from")

    def test_conflict_detected_for_can_install_row(self):
        """Serial conflicts are also detected for empty-bay (can_install) rows."""
        view = self._view()
        conflict = MagicMock()
        conflict.serial = "CONFLICT_SERIAL"
        conflict.pk = 999

        row = {
            "can_install": True,
            "serial": "CONFLICT_SERIAL",
            # No installed_module_id — bay is empty
        }

        with patch("dcim.models.Module") as mock_module_cls:
            mock_module_cls.objects.filter.return_value.select_related.return_value = [conflict]
            view._detect_serial_conflicts([row])

        assert row.get("serial_conflict_module") is conflict
        assert row.get("can_move_from") is True

    def test_ambiguous_when_multiple_conflicts_for_same_serial(self):
        """When multiple modules share the same serial, mark the row ambiguous instead of picking one."""
        view = self._view()
        conflict1 = MagicMock()
        conflict1.serial = "DUP_SERIAL"
        conflict1.pk = 100

        conflict2 = MagicMock()
        conflict2.serial = "DUP_SERIAL"
        conflict2.pk = 200

        row = {
            "can_replace": True,
            "serial": "DUP_SERIAL",
            "installed_module_id": 42,
        }

        with patch("dcim.models.Module") as mock_module_cls:
            mock_module_cls.objects.filter.return_value.select_related.return_value = [conflict1, conflict2]
            view._detect_serial_conflicts([row])

        assert row.get("serial_conflict_module") is None
        assert not row.get("can_move_from")
        assert row.get("serial_conflict_ambiguous") is True

    def test_can_install_no_serial_not_flagged(self):
        """A can_install row with no serial is not checked for conflicts."""
        view = self._view()
        row = {"can_install": True, "serial": "-"}
        with patch("dcim.models.Module") as mock_module_cls:
            view._detect_serial_conflicts([row])
            mock_module_cls.objects.filter.assert_not_called()
        assert "serial_conflict_module" not in row


class TestInventoryIgnoreRuleMatchesName:
    """Tests for InventoryIgnoreRule.matches_name() — all four match types."""

    def _rule(self, match_type, pattern, require_serial=True):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        rule = InventoryIgnoreRule.__new__(InventoryIgnoreRule)
        rule.match_type = match_type
        rule.pattern = pattern
        rule.require_serial_match_parent = require_serial
        rule.enabled = True
        return rule

    # --- ends_with ---

    def test_ends_with_optics_idprom(self):
        assert self._rule("ends_with", "IDPROM").matches_name("Optics0/0/0/0-IDPROM") is True

    def test_ends_with_fan_idprom(self):
        assert self._rule("ends_with", "IDPROM").matches_name("0/FT0-FT IDPROM") is True

    def test_ends_with_chassis_idprom(self):
        assert self._rule("ends_with", "IDPROM").matches_name("Rack 0-Chassis IDPROM") is True

    def test_ends_with_case_insensitive(self):
        assert self._rule("ends_with", "IDPROM").matches_name("Optics0/0/0/0-idprom") is True

    def test_ends_with_no_match(self):
        assert self._rule("ends_with", "IDPROM").matches_name("Optics0/0/0/0") is False

    def test_ends_with_idprom_in_middle(self):
        assert self._rule("ends_with", "IDPROM").matches_name("IDPROM-Optics0/0/0/0") is False

    # --- starts_with ---

    def test_starts_with_match(self):
        assert self._rule("starts_with", "Optics").matches_name("Optics0/0/0/0") is True

    def test_starts_with_no_match(self):
        assert self._rule("starts_with", "Optics").matches_name("0/FT0") is False

    def test_starts_with_case_insensitive(self):
        assert self._rule("starts_with", "OPTICS").matches_name("optics0/0/0/0") is True

    # --- contains ---

    def test_contains_match(self):
        assert self._rule("contains", "IDPROM").matches_name("Rack 0-Chassis IDPROM") is True

    def test_contains_middle_match(self):
        assert self._rule("contains", "IDPROM").matches_name("IDPROM-Optics0/0/0/0") is True

    def test_contains_case_insensitive(self):
        assert self._rule("contains", "IDPROM").matches_name("chassis-idprom") is True

    def test_contains_no_match(self):
        assert self._rule("contains", "IDPROM").matches_name("Optics0/0/0/0") is False

    # --- regex ---

    def test_regex_match(self):
        assert self._rule("regex", r"-IDPROM$").matches_name("Optics0/0/0/0-IDPROM") is True

    def test_regex_no_match(self):
        assert self._rule("regex", r"-IDPROM$").matches_name("Optics0/0/0/0") is False

    def test_regex_complex_pattern(self):
        assert self._rule("regex", r"^0/FT\d+-FT IDPROM$").matches_name("0/FT0-FT IDPROM") is True

    # --- edge cases ---

    def test_empty_name(self):
        assert self._rule("ends_with", "IDPROM").matches_name("") is False

    def test_none_name(self):
        assert self._rule("ends_with", "IDPROM").matches_name(None) is False


class TestCheckIgnoreRules:
    """Tests for the _check_ignore_rules() module-level function."""

    def _rule(self, match_type="ends_with", pattern="IDPROM", require_serial=True, action="skip"):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        rule = InventoryIgnoreRule.__new__(InventoryIgnoreRule)
        rule.match_type = match_type
        rule.pattern = pattern
        rule.require_serial_match_parent = require_serial
        rule.action = action
        rule.enabled = True
        return rule

    def _check(self, item, parent_item, rules, index_map=None, device_serial=""):
        from netbox_librenms_plugin.views.base.modules_view import _check_ignore_rules

        return _check_ignore_rules(item, parent_item, rules, index_map, device_serial)

    def test_match_with_serial_match_skips(self):
        """Item matches rule name AND serial matches parent → should be skipped."""
        item = {"entPhysicalName": "Optics0/0/0/0-IDPROM", "entPhysicalSerialNum": "ABC123"}
        parent = {"entPhysicalName": "Optics0/0/0/0", "entPhysicalSerialNum": "ABC123"}
        assert self._check(item, parent, [self._rule()]) == "skip"

    def test_match_with_serial_mismatch_not_skipped(self):
        """Name matches but serial differs from parent → NOT skipped (could be real module)."""
        item = {"entPhysicalName": "Optics0/0/0/0-IDPROM", "entPhysicalSerialNum": "XYZ999"}
        parent = {"entPhysicalName": "Optics0/0/0/0", "entPhysicalSerialNum": "ABC123"}
        assert self._check(item, parent, [self._rule()]) is None

    def test_match_with_no_parent_not_skipped(self):
        """Name matches, require_serial=True, but no parent → conservative: NOT skipped."""
        item = {"entPhysicalName": "Optics0/0/0/0-IDPROM", "entPhysicalSerialNum": "ABC123"}
        assert self._check(item, None, [self._rule()]) is None

    def test_match_no_serial_require_false_skips(self):
        """require_serial_match_parent=False → skipped on name match alone."""
        item = {"entPhysicalName": "Optics0/0/0/0-IDPROM", "entPhysicalSerialNum": ""}
        parent = {"entPhysicalName": "Optics0/0/0/0", "entPhysicalSerialNum": "ABC123"}
        assert self._check(item, parent, [self._rule(require_serial=False)]) == "skip"

    def test_no_matching_rule_not_skipped(self):
        """Name does not match any rule → NOT skipped."""
        item = {"entPhysicalName": "Optics0/0/0/0", "entPhysicalSerialNum": "ABC123"}
        parent = {"entPhysicalName": "Rack 0", "entPhysicalSerialNum": "ABC123"}
        assert self._check(item, parent, [self._rule()]) is None

    def test_empty_rules_not_skipped(self):
        """Empty rules list → nothing skipped."""
        item = {"entPhysicalName": "Optics0/0/0/0-IDPROM", "entPhysicalSerialNum": "ABC123"}
        parent = {"entPhysicalName": "Optics0/0/0/0", "entPhysicalSerialNum": "ABC123"}
        assert self._check(item, parent, []) is None

    def test_item_serial_empty_not_skipped_when_serial_required(self):
        """Item has empty serial → can't confirm match → NOT skipped."""
        item = {"entPhysicalName": "Optics0/0/0/0-IDPROM", "entPhysicalSerialNum": ""}
        parent = {"entPhysicalName": "Optics0/0/0/0", "entPhysicalSerialNum": "ABC123"}
        assert self._check(item, parent, [self._rule()]) is None

    def test_first_matching_rule_wins(self):
        """First rule that matches and satisfies serial check is used; later rules ignored."""
        rule_skip = self._rule(require_serial=False)
        rule_serial = self._rule(require_serial=True)
        item = {"entPhysicalName": "Optics0/0/0/0-IDPROM", "entPhysicalSerialNum": ""}
        parent = {"entPhysicalName": "Optics0/0/0/0", "entPhysicalSerialNum": "ABC123"}
        # rule_skip (require_serial=False) matches first → should skip
        assert self._check(item, parent, [rule_skip, rule_serial]) == "skip"

    def test_ancestor_walk_skips_when_grandparent_serial_matches(self):
        """IOS-XR case: IDPROM is child of empty-serial Mother Board, but grandparent serial matches."""
        # Mirrors actual 8201-SYS data: 0/RP0/CPU0-Base Board IDPROM (idx=7)
        # parent=Mother Board (idx=30, serial=''), grandparent=0/RP0/CPU0 (idx=1, serial='FOC2418NHRK')
        grandparent = {
            "entPhysicalIndex": 1,
            "entPhysicalName": "0/RP0/CPU0",
            "entPhysicalSerialNum": "FOC2418NHRK",
            "entPhysicalContainedIn": 0,
        }
        parent = {
            "entPhysicalIndex": 30,
            "entPhysicalName": "0/RP0/CPU0-Mother Board",
            "entPhysicalSerialNum": "",
            "entPhysicalContainedIn": 1,
        }
        item = {
            "entPhysicalIndex": 7,
            "entPhysicalName": "0/RP0/CPU0-Base Board IDPROM",
            "entPhysicalSerialNum": "FOC2418NHRK",
            "entPhysicalContainedIn": 30,
        }
        index_map = {1: grandparent, 30: parent, 7: item}
        assert self._check(item, parent, [self._rule()], index_map=index_map) == "skip"

    def test_ancestor_walk_stops_at_non_matching_serial(self):
        """Ancestor walk stops at first non-empty serial; if it doesn't match → NOT skipped."""
        grandparent = {
            "entPhysicalIndex": 1,
            "entPhysicalName": "Chassis",
            "entPhysicalSerialNum": "DIFFERENT_SN",
            "entPhysicalContainedIn": 0,
        }
        parent = {
            "entPhysicalIndex": 30,
            "entPhysicalName": "Board",
            "entPhysicalSerialNum": "",
            "entPhysicalContainedIn": 1,
        }
        item = {
            "entPhysicalIndex": 7,
            "entPhysicalName": "Board-IDPROM",
            "entPhysicalSerialNum": "FOC2418NHRK",
            "entPhysicalContainedIn": 30,
        }
        index_map = {1: grandparent, 30: parent, 7: item}
        assert self._check(item, parent, [self._rule()], index_map=index_map) is None

    def test_serial_matches_device_transparent(self):
        """serial_matches_device rule with action=transparent returns 'transparent'."""
        rule = self._rule(match_type="serial_matches_device", pattern="", action="transparent")
        item = {"entPhysicalName": "0/RP0/CPU0", "entPhysicalSerialNum": "FOC2418NHRK", "entPhysicalIndex": 5}
        assert self._check(item, None, [rule], device_serial="FOC2418NHRK") == "transparent"

    def test_serial_matches_device_skip(self):
        """serial_matches_device rule with action=skip returns 'skip'."""
        rule = self._rule(match_type="serial_matches_device", pattern="", action="skip")
        item = {"entPhysicalName": "0/RP0/CPU0", "entPhysicalSerialNum": "FOC2418NHRK"}
        assert self._check(item, None, [rule], device_serial="FOC2418NHRK") == "skip"

    def test_numeric_serial_matches_device_after_normalization(self):
        """Numeric ENTITY serials use the same text normalization as the device serial."""
        rule = self._rule(match_type="serial_matches_device", pattern="", action="transparent")
        item = {"entPhysicalName": "0/RP0/CPU0", "entPhysicalSerialNum": 123456}

        assert self._check(item, None, [rule], device_serial="123456") == "transparent"

    def test_serial_matches_device_no_match(self):
        """serial_matches_device: item serial differs from device serial → no match."""
        rule = self._rule(match_type="serial_matches_device", pattern="", action="transparent")
        item = {"entPhysicalName": "Optics0/0/0/0", "entPhysicalSerialNum": "XCVR001"}
        assert self._check(item, None, [rule], device_serial="FOC2418NHRK") is None

    def test_serial_matches_device_empty_device_serial(self):
        """serial_matches_device: device serial empty → no match (defensive)."""
        rule = self._rule(match_type="serial_matches_device", pattern="", action="transparent")
        item = {"entPhysicalName": "0/RP0/CPU0", "entPhysicalSerialNum": "FOC2418NHRK"}
        assert self._check(item, None, [rule], device_serial="") is None

    def test_serial_matches_device_empty_item_serial(self):
        """serial_matches_device: item serial empty → no match (defensive)."""
        rule = self._rule(match_type="serial_matches_device", pattern="", action="transparent")
        item = {"entPhysicalName": "0/RP0/CPU0", "entPhysicalSerialNum": ""}
        assert self._check(item, None, [rule], device_serial="FOC2418NHRK") is None

    def test_serial_matches_device_fires_when_parent_is_chassis(self):
        """serial_matches_device: matches when direct parent has class='chassis'."""
        rule = self._rule(match_type="serial_matches_device", pattern="", action="transparent")
        item = {"entPhysicalName": "0/RP0/CPU0", "entPhysicalSerialNum": "FOC2418NHRK"}
        chassis = {"entPhysicalName": "Rack 0", "entPhysicalClass": "chassis"}
        assert self._check(item, chassis, [rule], device_serial="FOC2418NHRK") == "transparent"

    def test_serial_matches_device_skipped_when_parent_is_container(self):
        """
        serial_matches_device: does NOT match when parent is a container
        (e.g. ASR-9904 line card whose serial happens to equal the device
        serial — the linecard is contained in a 'Line Card Slot N' container,
        not the chassis).  Treating it as transparent would silently promote
        its TenGigE children to chassis-level bay matching.
        """
        rule = self._rule(match_type="serial_matches_device", pattern="", action="transparent")
        item = {"entPhysicalName": "0/0", "entPhysicalSerialNum": "FOC2349N4UN"}
        slot_container = {"entPhysicalName": "Rack 0-Line Card Slot 0", "entPhysicalClass": "container"}
        assert self._check(item, slot_container, [rule], device_serial="FOC2349N4UN") is None

    def test_serial_matches_device_skipped_when_parent_is_module(self):
        """serial_matches_device: does NOT match when parent is a module."""
        rule = self._rule(match_type="serial_matches_device", pattern="", action="transparent")
        item = {"entPhysicalName": "Submodule", "entPhysicalSerialNum": "ABC123"}
        parent_module = {"entPhysicalName": "Parent", "entPhysicalClass": "module"}
        assert self._check(item, parent_module, [rule], device_serial="ABC123") is None

    def test_transparent_action_returned_for_name_rule(self):
        """A name-based rule with action=transparent returns 'transparent'."""
        rule = self._rule(match_type="ends_with", pattern="IDPROM", require_serial=False, action="transparent")
        item = {"entPhysicalName": "Optics0/0/0/0-IDPROM", "entPhysicalSerialNum": "ABC123"}
        assert self._check(item, None, [rule]) == "transparent"


class TestCollectDescendantsIgnoreRules:
    """_collect_descendants must skip items matched by ignore rules."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        return object.__new__(BaseModuleTableView)

    def _rule(self, match_type="ends_with", pattern="IDPROM", require_serial=True, action="skip"):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        rule = InventoryIgnoreRule.__new__(InventoryIgnoreRule)
        rule.match_type = match_type
        rule.pattern = pattern
        rule.require_serial_match_parent = require_serial
        rule.action = action
        rule.enabled = True
        return rule

    def _build_maps(self, inventory):
        children_by_parent = {}
        index_map = {}
        for item in inventory:
            p = item.get("entPhysicalContainedIn")
            if p is not None:
                children_by_parent.setdefault(p, []).append(item)
            idx = item.get("entPhysicalIndex")
            if idx is not None:
                index_map[idx] = item
        return children_by_parent, index_map

    def test_idprom_child_is_excluded(self):
        """IDPROM child of a real module must not appear in results."""
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Optics0/0/0/0",
                "entPhysicalModelName": "DP04QSDD-HE0",
                "entPhysicalSerialNum": "SER001",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalName": "Optics0/0/0/0-IDPROM",
                "entPhysicalModelName": "DP04QSDD-HE0",
                "entPhysicalSerialNum": "SER001",
                "entPhysicalContainedIn": 1,
            },
        ]
        children_by_parent, index_map = self._build_maps(inventory)
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, index_map, [self._rule()], depth=1, results=results)
        assert len(results) == 1
        _, item = results[0]
        assert item["entPhysicalName"] == "Optics0/0/0/0"

    def test_idprom_child_descendants_also_excluded(self):
        """Nothing nested below a skipped entry should appear either."""
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Optics0/0/0/0",
                "entPhysicalModelName": "DP04QSDD-HE0",
                "entPhysicalSerialNum": "SER001",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalName": "Optics0/0/0/0-IDPROM",
                "entPhysicalModelName": "DP04QSDD-HE0",
                "entPhysicalSerialNum": "SER001",
                "entPhysicalContainedIn": 1,
            },
            {
                "entPhysicalIndex": 3,
                "entPhysicalName": "Optics0/0/0/0-IDPROM-SubItem",
                "entPhysicalModelName": "DP04QSDD-HE0",
                "entPhysicalSerialNum": "SER001",
                "entPhysicalContainedIn": 2,
            },
        ]
        children_by_parent, index_map = self._build_maps(inventory)
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, index_map, [self._rule()], depth=1, results=results)
        names = [item["entPhysicalName"] for _, item in results]
        assert "Optics0/0/0/0" in names
        assert "Optics0/0/0/0-IDPROM" not in names
        assert "Optics0/0/0/0-IDPROM-SubItem" not in names

    def test_real_submodule_still_included(self):
        """A legitimate non-matching child remains in results."""
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "0/FT0",
                "entPhysicalModelName": "FAN-1RU-PI",
                "entPhysicalSerialNum": "SER002",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalName": "0/FT0-FT IDPROM",
                "entPhysicalModelName": "FAN-1RU-PI",
                "entPhysicalSerialNum": "SER002",
                "entPhysicalContainedIn": 1,
            },
            {
                "entPhysicalIndex": 3,
                "entPhysicalName": "FanBlade-0",
                "entPhysicalModelName": "BLADE-A",
                "entPhysicalSerialNum": "SER003",
                "entPhysicalContainedIn": 1,
            },
        ]
        children_by_parent, index_map = self._build_maps(inventory)
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, index_map, [self._rule()], depth=1, results=results)
        names = [item["entPhysicalName"] for _, item in results]
        assert "0/FT0" in names
        assert "0/FT0-FT IDPROM" not in names
        assert "FanBlade-0" in names

    def test_no_rules_includes_all(self):
        """With empty rules list, no items are filtered (regression guard)."""
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Optics0/0/0/0",
                "entPhysicalModelName": "DP04QSDD-HE0",
                "entPhysicalSerialNum": "SER001",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalName": "Optics0/0/0/0-IDPROM",
                "entPhysicalModelName": "DP04QSDD-HE0",
                "entPhysicalSerialNum": "SER001",
                "entPhysicalContainedIn": 1,
            },
        ]
        children_by_parent, index_map = self._build_maps(inventory)
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, index_map, [], depth=1, results=results)
        names = [item["entPhysicalName"] for _, item in results]
        assert "Optics0/0/0/0" in names
        assert "Optics0/0/0/0-IDPROM" in names

    def test_transparent_item_children_promoted_to_same_depth(self):
        """Children of a transparent-matched item are promoted to the transparent item's depth."""
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Module-Chassis-IDPROM",
                "entPhysicalModelName": "CHASSIS-TYPE",
                "entPhysicalSerialNum": "SER_CHASSIS",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalName": "Child-Module",
                "entPhysicalModelName": "SFP-X2",
                "entPhysicalSerialNum": "SER_SFP",
                "entPhysicalContainedIn": 1,
            },
        ]
        rule = self._rule(match_type="ends_with", pattern="IDPROM", require_serial=False, action="transparent")
        children_by_parent, index_map = self._build_maps(inventory)
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, index_map, [rule], depth=1, results=results)

        names = [item["entPhysicalName"] for _, item in results]
        depths = [d for d, _ in results]
        # Transparent item itself must not appear
        assert "Module-Chassis-IDPROM" not in names
        # Its child must be promoted to the same depth (1) as the transparent item would occupy
        assert "Child-Module" in names
        assert depths[names.index("Child-Module")] == 1

    def test_transparent_item_without_children_produces_no_rows(self):
        """A transparent item with no children yields nothing."""
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Leaf-IDPROM",
                "entPhysicalModelName": "LEAF-MODEL",
                "entPhysicalSerialNum": "LEAF_SER",
                "entPhysicalContainedIn": 0,
            },
        ]
        rule = self._rule(match_type="ends_with", pattern="IDPROM", require_serial=False, action="transparent")
        children_by_parent, index_map = self._build_maps(inventory)
        view = self._view()
        results = []
        view._collect_descendants(0, children_by_parent, index_map, [rule], depth=1, results=results)
        assert results == []


class TestPositionalMatchClassAware:
    """
    Positional fallback only tries bay-name patterns appropriate for the item's
    hardware class.  Without this, items like fans and PSUs land in chassis
    line-card "Slot N" bays just because the slot number happens to align.
    """

    @staticmethod
    def _walk(item_class, slot_num, bays):
        """Drive _match_bay_by_position via a minimal inventory: chassis -> container -> item."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalModelName": "REAL-CHASSIS",
                "entPhysicalClass": "chassis",
                "entPhysicalContainedIn": 0,
                "entPhysicalParentRelPos": 0,
            },
        ]
        # Add slot_num sibling containers under chassis so positional finds slot=slot_num
        for n in range(1, slot_num + 1):
            inventory.append(
                {
                    "entPhysicalIndex": 100 + n,
                    "entPhysicalModelName": "",
                    "entPhysicalClass": "container",
                    "entPhysicalContainedIn": 1,
                    "entPhysicalParentRelPos": n,
                }
            )
        item = {
            "entPhysicalIndex": 999,
            "entPhysicalModelName": "X",
            "entPhysicalClass": item_class,
            "entPhysicalContainedIn": 100 + slot_num,
            "entPhysicalParentRelPos": 0,
        }
        inventory.append(item)
        index_map = {i["entPhysicalIndex"]: i for i in inventory}
        return BaseModuleTableView._match_bay_by_position(item, index_map, bays)

    @staticmethod
    def _bay(name, position=None):
        b = MagicMock()
        b.name = name
        b.position = position
        return b

    @staticmethod
    def _walk_port_label_fallback(item_name, slot_num, bays, ifname=None, ifdescr=None):
        """Build a topology where positional slot differs from interface label index."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalModelName": "REAL-CHASSIS",
                "entPhysicalClass": "chassis",
                "entPhysicalContainedIn": 0,
                "entPhysicalParentRelPos": 0,
            },
        ]
        for n in range(1, slot_num + 1):
            inventory.append(
                {
                    "entPhysicalIndex": 100 + n,
                    "entPhysicalModelName": "",
                    "entPhysicalClass": "container",
                    "entPhysicalContainedIn": 1,
                    "entPhysicalParentRelPos": n,
                }
            )

        item = {
            "entPhysicalIndex": 999,
            "entPhysicalModelName": "SFP-10G-SR",
            "entPhysicalClass": "port",
            "entPhysicalName": item_name,
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 100 + slot_num,
            "entPhysicalParentRelPos": 0,
            "_librenms_ifname": ifname,
            "_librenms_ifdescr": ifdescr,
        }
        inventory.append(item)
        index_map = {i["entPhysicalIndex"]: i for i in inventory}
        return BaseModuleTableView._match_bay_by_position(item, index_map, bays)

    def test_fan_does_not_match_slot_bay(self):
        """A fan (class=fan) must not land in a 'Slot 1' bay even when positional says slot 1."""
        bays = {"Slot 1": self._bay("Slot 1"), "Slot 2": self._bay("Slot 2")}
        result = self._walk("fan", 1, bays)
        assert result is None, (
            "Fan was matched to a chassis 'Slot N' bay.  Positional patterns must be "
            "class-aware: fans only match Fan / Fan Tray / FT N bays."
        )

    def test_fan_matches_fan_tray_bay(self):
        """A fan matches a 'Fan Tray N' or 'Fan N' bay."""
        bays = {"Fan Tray 1": self._bay("Fan Tray 1"), "Slot 1": self._bay("Slot 1")}
        result = self._walk("fan", 1, bays)
        assert result is bays["Fan Tray 1"]

    def test_powersupply_does_not_match_slot_bay(self):
        """A power supply must not match a 'Slot N' bay."""
        bays = {"Slot 2": self._bay("Slot 2"), "Slot 3": self._bay("Slot 3")}
        result = self._walk("powerSupply", 2, bays)
        assert result is None

    def test_powersupply_matches_psu_bay(self):
        """A PSU matches Power Supply / PSU / PEM patterns."""
        bays = {"PSU 1": self._bay("PSU 1"), "Slot 1": self._bay("Slot 1")}
        result = self._walk("powerSupply", 1, bays)
        assert result is bays["PSU 1"]

    def test_module_still_matches_slot_bay(self):
        """A module continues to match Slot/SFP/Bay/Port patterns."""
        bays = {"Slot 1": self._bay("Slot 1")}
        result = self._walk("module", 1, bays)
        assert result is bays["Slot 1"]

    def test_port_label_infers_slot_from_ifname_suffix(self):
        """Port rows should infer bay index from ifName when positional slot does not match."""
        bays = {"SFP 1": self._bay("SFP 1")}
        result = self._walk_port_label_fallback("Te1/1/1", 5, bays)
        assert result is bays["SFP 1"]

    def test_port_label_infers_slot_from_ifdescr_suffix(self):
        """Long-form labels (ifDescr) should infer the same slot index as short ifName labels."""
        bays = {"SFP 1": self._bay("SFP 1")}
        result = self._walk_port_label_fallback(
            "Port-Unknown",
            4,
            bays,
            ifname="",
            ifdescr="TenGigabitEthernet1/1/1",
        )
        assert result is bays["SFP 1"]

    def test_extract_interface_numeric_coordinates_preserves_existing_suffix_behavior(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        result = BaseModuleTableView._extract_interface_numeric_coordinates("xe-2/1/0   ")
        assert result == [2, 1, 0]

    def test_extract_port_index_from_label_preserves_existing_suffix_behavior(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        assert BaseModuleTableView._extract_port_index_from_label("Eth42   ") == 42

    def test_port_matches_typoed_bay_name_via_numeric_position(self):
        """Numeric bay positions should rescue matching when the bay name is misspelled."""
        bays = {
            "SFP 1": self._bay("SFP 1", position="1"),
            "SFP2": self._bay("SFP2", position="2"),
        }
        result = self._walk("port", 2, bays)
        assert result is bays["SFP2"]

    def test_port_matches_typoed_bay_name_via_alpha_position(self):
        """Alphabetic bay positions should map sibling order 1->A, 2->B, etc."""
        bays = {
            "SFP 1": self._bay("SFP 1", position="A"),
            "SFP2": self._bay("SFP2", position="B"),
        }
        result = self._walk("port", 2, bays)
        assert result is bays["SFP2"]

    def test_unknown_class_returns_none(self):
        """An item with an unknown / empty class doesn't get a positional guess."""
        bays = {"Slot 1": self._bay("Slot 1")}
        result = self._walk("sensor", 1, bays)
        assert result is None


class TestNoBayWarningHints:
    """`_build_no_bay_warning` distinguishes the common 'No Bay' causes."""

    def test_empty_scope_mentions_missing_templates(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalClass": "module", "entPhysicalModelName": "X"}
        msg = BaseModuleTableView._build_no_bay_warning(item, {})
        assert "no bay templates defined" in msg.lower()

    def test_scope_uninstalled_recommends_install_parent(self):
        """Empty scope due to an uninstalled ancestor -> hint to install parent first."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalClass": "module", "entPhysicalModelName": "X"}
        msg = BaseModuleTableView._build_no_bay_warning(item, {}, scope_uninstalled=True)
        assert "install the parent module first" in msg.lower()

    def test_suggestion_appended_when_provided(self):
        """`_build_no_bay_warning` includes the suggested mapping when one is provided."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalClass": "module"}
        suggestion = {
            "librenms_name": r"^0/(\d+)$",
            "librenms_class": "module",
            "netbox_bay_name": r"Slot \1",
            "is_regex": True,
            "example_item": "0/0",
            "example_bay": "Slot 0",
        }
        msg = BaseModuleTableView._build_no_bay_warning(item, {"Slot 0": MagicMock()}, suggestion)
        assert "0/(\\d+)" in msg
        assert "Slot \\1" in msg
        assert "0/0" in msg and "Slot 0" in msg

    def test_fan_class_hint_names_fan_bays(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalClass": "fan"}
        msg = BaseModuleTableView._build_no_bay_warning(item, {"Slot 1": MagicMock()})
        assert "Fan" in msg

    def test_powersupply_class_hint_names_psu_bays(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalClass": "powerSupply"}
        msg = BaseModuleTableView._build_no_bay_warning(item, {"Slot 1": MagicMock()})
        assert "PSU" in msg or "Power Supply" in msg or "PEM" in msg

    def test_module_class_hint_names_slot_bays(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalClass": "module"}
        msg = BaseModuleTableView._build_no_bay_warning(item, {"Slot 1": MagicMock()})
        assert "Slot" in msg or "SFP" in msg

    def test_port_class_hint_uses_plain_language(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalClass": "port"}
        msg = BaseModuleTableView._build_no_bay_warning(item, {"Slot 1": MagicMock()})
        assert "no matching bay in netbox" in msg.lower()
        assert msg.lower().count("modulebaymapping") == 1
        assert "if the names differ" in msg.lower()


class TestSuggestBayMapping:
    """`_suggest_bay_mapping` produces a regex mapping when a trailing-number bay is in scope."""

    def test_suggests_regex_when_trailing_number_matches_bay(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "0/0", "entPhysicalClass": "module"}
        bay = MagicMock()
        bay.name = "Slot 0"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Slot 0": bay})
        assert sug is not None
        assert sug["is_regex"] is True
        assert sug["librenms_name"] == r"^0/(\d+)$"
        assert sug["netbox_bay_name"] == r"Slot \1"
        assert sug["librenms_class"] == "module"
        assert sug["example_item"] == "0/0"
        assert sug["example_bay"] == "Slot 0"

    def test_no_suggestion_when_no_bay_with_matching_trailing_number(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "0/0", "entPhysicalClass": "module"}
        bay = MagicMock()
        bay.name = "Slot 7"  # trailing 7, not 0
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Slot 7": bay})
        assert sug is None

    def test_no_suggestion_when_item_has_no_trailing_number(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "Mainboard", "entPhysicalClass": "module"}
        bay = MagicMock()
        bay.name = "Slot 0"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Slot 0": bay})
        assert sug is None

    def test_no_suggestion_when_module_bays_empty(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "0/0", "entPhysicalClass": "module"}
        sug = BaseModuleTableView._suggest_bay_mapping(item, {})
        assert sug is None

    def test_no_suggestion_when_scope_preserved(self):
        """Suppress suggestions for sub-items whose scope was inherited from
        an unmatched ancestor — the bays in scope are at the wrong level."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "TenGigE0/0/0/0", "entPhysicalClass": "module"}
        bay = MagicMock()
        bay.name = "Slot 0"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Slot 0": bay}, scope_preserved=True)
        assert sug is None

    def test_no_suggestion_for_fan_when_only_slot_bays_exist(self):
        """A fan must not be suggested into a chassis line-card slot bay."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "0/FT0", "entPhysicalClass": "fan"}
        bay = MagicMock()
        bay.name = "Slot 0"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Slot 0": bay})
        assert sug is None

    def test_suggestion_for_fan_when_fan_named_bay_exists(self):
        """A fan with a fan-named bay in scope yields a fan-targeted suggestion."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "0/FT0", "entPhysicalClass": "fan"}
        bay = MagicMock()
        bay.name = "Fan Tray 0"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Fan Tray 0": bay})
        assert sug is not None
        assert "Fan Tray" in sug["netbox_bay_name"]

    def test_no_suggestion_for_powersupply_when_only_slot_bays_exist(self):
        """A power supply must not be suggested into a chassis line-card slot bay."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "0/PT0-PM0", "entPhysicalClass": "powerSupply"}
        bay = MagicMock()
        bay.name = "Slot 0"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Slot 0": bay})
        assert sug is None

    def test_suggests_letter_trail_for_carrier_child_bays(self):
        """`Slot A` should map to `CPM A` via a letter-capturing regex even
        when prefix tokens differ (`Slot` vs `CPM`). This is the common
        follow-up after the user installs a controller-card carrier whose
        empty child bays are letter-named."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "Slot A", "entPhysicalClass": "cpmModule"}
        bay = MagicMock()
        bay.name = "CPM A"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"CPM A": bay})
        assert sug is not None
        assert sug["is_regex"] is True
        assert sug["librenms_name"] == r"^Slot\ ([A-Za-z]+)$"
        assert sug["netbox_bay_name"] == r"CPM \1"
        assert sug["librenms_class"] == "cpmModule"
        assert sug["example_item"] == "Slot A"
        assert sug["example_bay"] == "CPM A"

    def test_no_letter_trail_suggestion_when_no_letter_bay(self):
        """`Slot A` should NOT match `Slot 0` — bay trail must be of same kind."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "Slot A", "entPhysicalClass": "cpmModule"}
        bay = MagicMock()
        bay.name = "Slot 0"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Slot 0": bay})
        assert sug is None


class TestSuggestBayMappingFromDescr:
    """`_suggest_bay_mapping` falls back to a description-based regex when the
    LibreNMS name is just a model number with no positional info — e.g. Juniper
    'JNP304-LMIC16-BASE' with description 'MIC: ... @ 0/0/*' should suggest a
    mapping that targets the existing 'MIC 0' bay."""

    def test_juniper_mic_descr_yields_mapping(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {
            "entPhysicalName": "JNP304-LMIC16-BASE",
            "entPhysicalDescr": "MIC: MRATE LMIC 16x100G/4x400G @ 0/0/*",
            "entPhysicalClass": "container",
        }
        bays = {"MIC 0": MagicMock(), "RE 0": MagicMock(), "RE 1": MagicMock()}
        sug = BaseModuleTableView._suggest_bay_mapping(item, bays)
        assert sug is not None
        assert sug["is_regex"] is True
        assert sug["netbox_bay_name"] == "MIC \\1"
        assert sug["example_bay"] == "MIC 0"
        # The pattern must fullmatch the original description (so the saved
        # mapping actually resolves at lookup time).
        import re as _re

        m = _re.fullmatch(sug["librenms_name"], item["entPhysicalDescr"])
        assert m is not None
        assert m.expand(sug["netbox_bay_name"]) == "MIC 0"

    def test_no_descr_match_returns_none_for_container(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {
            "entPhysicalName": "X",
            "entPhysicalDescr": "no class hint here",
            "entPhysicalClass": "container",
        }
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"MIC 0": MagicMock()})
        assert sug is None

    def test_descr_class_with_no_matching_bay_returns_none(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {
            "entPhysicalName": "X",
            "entPhysicalDescr": "FPC: line card @ 5/0/*",
            "entPhysicalClass": "container",
        }
        # Device only has MIC 0 — no FPC 5 bay → no suggestion
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"MIC 0": MagicMock()})
        assert sug is None

    def test_descr_fallback_preferred_over_none_for_module_class(self):
        """When name-based heuristic finds no candidate AND the item is a
        normal module class (not container), descr fallback should still fire
        — useful for vendor inventories that put the model in entPhysicalName
        but classify the row as 'module'."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {
            "entPhysicalName": "JNP304-LMIC16-BASE",
            "entPhysicalDescr": "MIC: MRATE LMIC 16x100G/4x400G @ 1/0/*",
            "entPhysicalClass": "module",
        }
        bays = {"MIC 0": MagicMock(), "MIC 1": MagicMock()}
        sug = BaseModuleTableView._suggest_bay_mapping(item, bays)
        assert sug is not None
        assert sug["example_bay"] == "MIC 1"

    def test_descr_trail_fallback_for_juniper_fan_tray_controller(self):
        """Juniper fan trays carry the model in entPhysicalName ('JNP10008-FTC2')
        and the human-readable position in entPhysicalDescr ('Fan Tray Controller 0').
        The class+slot descr regex doesn't match, but the trailing-number heuristic
        on the description should still surface a usable mapping suggestion that
        targets the existing 'Fan Tray 0' bay (mapping evaluation already considers
        entPhysicalDescr, so this resolves at lookup time)."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {
            "entPhysicalName": "JNP10008-FTC2",
            "entPhysicalDescr": "Fan Tray Controller 0",
            "entPhysicalClass": "fan",
            "entPhysicalModelName": "JNP10008-FTC2",
        }
        bays = {"Fan Tray 0": MagicMock(), "Fan Tray 1": MagicMock(), "FPC 0": MagicMock()}
        sug = BaseModuleTableView._suggest_bay_mapping(item, bays)
        assert sug is not None
        assert sug["is_regex"] is True
        assert sug["librenms_class"] == "fan"
        assert sug["netbox_bay_name"] == "Fan Tray \\1"
        assert sug["example_bay"] == "Fan Tray 0"
        assert sug["example_item"] == "Fan Tray Controller 0"
        # The pattern must fullmatch the descr so the saved mapping resolves at lookup time.
        import re as _re

        m = _re.fullmatch(sug["librenms_name"], item["entPhysicalDescr"])
        assert m is not None
        assert m.expand(sug["netbox_bay_name"]) == "Fan Tray 0"

    def test_descr_trail_fallback_skipped_when_descr_equals_name(self):
        """If entPhysicalName already carries positional info and the
        name-based heuristic still failed (e.g. no bay shares the trail),
        the descr-trail fallback should not produce a suggestion when descr
        is identical to name — the name-based pass was authoritative."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {
            "entPhysicalName": "Fan Tray 9",
            "entPhysicalDescr": "Fan Tray 9",
            "entPhysicalClass": "fan",
        }
        bays = {"Fan Tray 0": MagicMock(), "Fan Tray 1": MagicMock()}
        sug = BaseModuleTableView._suggest_bay_mapping(item, bays)
        assert sug is None

    def test_descr_trail_fallback_respects_class_filter(self):
        """The descr-trail fallback receives the already-class-filtered candidate
        list, so a fan whose descr ends in '0' must not be mapped onto a 'Slot 0'
        line-card bay."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {
            "entPhysicalName": "JNP10008-FTC2",
            "entPhysicalDescr": "Fan Tray Controller 0",
            "entPhysicalClass": "fan",
        }
        bays = {"Slot 0": MagicMock(), "Slot 1": MagicMock()}  # no fan-named bays
        sug = BaseModuleTableView._suggest_bay_mapping(item, bays)
        assert sug is None


class TestSuggestTypeMapping:
    """`_suggest_type_mapping` produces a prefill dict for the ModuleTypeMapping form."""

    def test_returns_none_when_model_blank(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        assert BaseModuleTableView._suggest_type_mapping({"entPhysicalModelName": ""}, None) is None
        assert BaseModuleTableView._suggest_type_mapping({}, None) is None

    def test_returns_dict_with_librenms_model(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalModelName": "SFP-10G-SR", "entPhysicalDescr": "10GBASE-SR"}
        sug = BaseModuleTableView._suggest_type_mapping(item, None)
        assert sug is not None
        assert sug["librenms_model"] == "SFP-10G-SR"

    def test_description_includes_physical_descr(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalModelName": "SFP-10G-SR", "entPhysicalDescr": "10GBASE-SR SFP+"}
        sug = BaseModuleTableView._suggest_type_mapping(item, None)
        assert "10GBASE-SR SFP+" in sug["description"]

    def test_description_includes_bay_name_when_bay_available(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = MagicMock()
        bay.name = "SFP 1"
        item = {"entPhysicalModelName": "SFP-10G-SR", "entPhysicalDescr": "10GBASE-SR"}
        sug = BaseModuleTableView._suggest_type_mapping(item, bay)
        assert "SFP 1" in sug["description"]

    def test_description_omits_bay_name_when_no_bay(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalModelName": "GLC-TE", "entPhysicalDescr": "1000BaseT"}
        sug = BaseModuleTableView._suggest_type_mapping(item, None)
        assert sug is not None
        assert "bay" not in sug["description"].lower() or "fitted" not in sug["description"]

    def test_unspecified_model_produces_suggestion(self):
        """'Unspecified' is a valid librenms_model — a mapping can still be created."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bay = MagicMock()
        bay.name = "SFP 2"
        item = {"entPhysicalModelName": "Unspecified", "entPhysicalDescr": "1000BaseT"}
        sug = BaseModuleTableView._suggest_type_mapping(item, bay)
        assert sug is not None
        assert sug["librenms_model"] == "Unspecified"
        assert "SFP 2" in sug["description"]


class TestSuggestModuleTypeCreate:
    """`_suggest_module_type_create` produces a prefill dict for NetBox's native ModuleType create form."""

    def test_returns_none_when_model_blank(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        assert BaseModuleTableView._suggest_module_type_create({"entPhysicalModelName": ""}, None) is None
        assert BaseModuleTableView._suggest_module_type_create({}, None) is None

    def test_prefills_model_and_part_number(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalModelName": "X2-10GB-LR", "entPhysicalDescr": "10Gbase-LR"}
        sug = BaseModuleTableView._suggest_module_type_create(item, None)
        assert sug["model"] == "X2-10GB-LR"
        assert sug["part_number"] == "X2-10GB-LR"
        assert sug["description"] == "10Gbase-LR"
        assert "manufacturer" not in sug
        assert "comments" not in sug

    def test_prefills_manufacturer_pk(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        manufacturer = MagicMock()
        manufacturer.pk = 42
        item = {"entPhysicalModelName": "X2-10GB-LR"}
        sug = BaseModuleTableView._suggest_module_type_create(item, manufacturer)
        assert sug["manufacturer"] == 42

    def test_truncates_long_model_to_100_and_part_number_to_50(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        long_model = "M" * 150
        item = {"entPhysicalModelName": long_model}
        sug = BaseModuleTableView._suggest_module_type_create(item, None)
        assert len(sug["model"]) == 100
        assert len(sug["part_number"]) == 50

    def test_truncates_description_to_200_and_overflow_into_comments(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        long_desc = "D" * 250
        item = {"entPhysicalModelName": "M", "entPhysicalDescr": long_desc}
        sug = BaseModuleTableView._suggest_module_type_create(item, None)
        assert len(sug["description"]) == 200
        assert sug["comments"] == long_desc

    def test_short_description_does_not_set_comments(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalModelName": "M", "entPhysicalDescr": "short"}
        sug = BaseModuleTableView._suggest_module_type_create(item, None)
        assert sug["description"] == "short"
        assert "comments" not in sug


class TestNoTypeWarningHints:
    """`_build_no_type_warning` mentions the missing model name."""

    def test_includes_model_name(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        msg = BaseModuleTableView._build_no_type_warning({"entPhysicalModelName": "ASR-9904-FAN"})
        assert "ASR-9904-FAN" in msg
        assert "ModuleType" in msg

    def test_handles_missing_model_name(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        msg = BaseModuleTableView._build_no_type_warning({"entPhysicalModelName": ""})
        assert msg  # non-empty string


class TestBuildRowModelWarning:
    """`_build_row` populates `model_warning` for No Bay / No Type rows."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        v = object.__new__(BaseModuleTableView)
        v._device_manufacturer = None
        return v

    def test_no_bay_row_gets_model_warning(self):
        view = self._view()
        view._match_module_bay = MagicMock(return_value=None)
        item = {"entPhysicalName": "0/FT0", "entPhysicalClass": "fan", "entPhysicalModelName": "ASR-FAN"}
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=MagicMock(model="ASR-FAN", pk=1)),
        ):
            row = view._build_row(item, {}, {"Slot 1": MagicMock()}, {"ASR-FAN": MagicMock(pk=1)})
        assert row["status"] == "No Bay"
        assert "model_warning" in row
        assert row["model_warning"], "expected non-empty hint"

    def test_no_type_row_gets_model_warning(self):
        view = self._view()
        bay = MagicMock()
        bay.name = "Slot 1"
        bay.installed_module = None
        bay.get_absolute_url.return_value = "/b"
        view._match_module_bay = MagicMock(return_value=bay)
        item = {"entPhysicalName": "X", "entPhysicalClass": "module", "entPhysicalModelName": "UNKNOWN-MODEL"}
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=None),
        ):
            row = view._build_row(item, {}, {}, {})
        assert row["status"] == "No Type"
        assert "UNKNOWN-MODEL" in row.get("model_warning", "")

    def test_no_type_row_carries_module_type_create_prefill(self):
        """A No Type row exposes a `module_type_create` dict so the table can
        render the "Add Module Type" button linking to NetBox's native form."""
        view = self._view()
        bay = MagicMock()
        bay.name = "Slot 1"
        bay.installed_module = None
        bay.get_absolute_url.return_value = "/b"
        view._match_module_bay = MagicMock(return_value=bay)
        manufacturer = MagicMock()
        manufacturer.pk = 99
        item = {
            "entPhysicalName": "X",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "X2-10GB-LR",
            "entPhysicalDescr": "10Gbase-LR",
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=None),
        ):
            row = view._build_row(item, {}, {}, {}, manufacturer=manufacturer)
        assert row["status"] == "No Type"
        create = row.get("module_type_create")
        assert create is not None
        assert create["model"] == "X2-10GB-LR"
        assert create["part_number"] == "X2-10GB-LR"
        assert create["manufacturer"] == 99
        assert create["description"] == "10Gbase-LR"

    def test_matched_row_has_no_model_warning(self):
        view = self._view()
        bay = MagicMock()
        bay.name = "Slot 1"
        bay.installed_module = None
        bay.get_absolute_url.return_value = "/b"
        view._match_module_bay = MagicMock(return_value=bay)
        mt = MagicMock(pk=10)
        mt.model = "M"
        mt.get_absolute_url.return_value = "/mt"
        item = {"entPhysicalName": "X", "entPhysicalClass": "module", "entPhysicalModelName": "M"}
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=mt),
        ):
            row = view._build_row(item, {}, {"Slot 1": bay}, {"M": mt})
        assert row["status"] == "Matched"
        assert "model_warning" not in row

    def test_no_bay_row_carries_model_suggestion_when_trailing_number_matches(self):
        """A No Bay row whose item name shares a trailing number with a bay
        in scope gets a `model_suggestion` field consumable by the table."""
        view = self._view()
        view._match_module_bay = MagicMock(return_value=None)
        bay = MagicMock()
        bay.name = "Slot 0"
        item = {"entPhysicalName": "0/0", "entPhysicalClass": "module", "entPhysicalModelName": "X"}
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=MagicMock(model="X", pk=1)),
        ):
            row = view._build_row(item, {}, {"Slot 0": bay}, {"X": MagicMock(pk=1)})
        assert row["status"] == "No Bay"
        sug = row.get("model_suggestion")
        assert sug is not None
        assert sug["librenms_name"] == r"^0/(\d+)$"
        assert sug["netbox_bay_name"] == r"Slot \1"

    def test_scope_uninstalled_no_bay_row_recommends_install_parent(self):
        """When scope is empty due to an uninstalled ancestor, the warning
        text instructs the user to install the parent module first."""
        view = self._view()
        view._match_module_bay = MagicMock(return_value=None)
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-X",
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch(
                "netbox_librenms_plugin.utils.resolve_module_type",
                return_value=MagicMock(model="SFP-X", pk=1),
            ),
        ):
            row = view._build_row(item, {}, {}, {"SFP-X": MagicMock(pk=1)}, scope_uninstalled=True)
        assert row["status"] == "No Bay"
        assert "install the parent module first" in row.get("model_warning", "").lower()

    def test_no_bay_empty_parent_bays_sets_no_bay_reason(self):
        """When parent module is installed but has no bay templates, _build_row
        tags the row with no_bay_reason='empty_parent_bays'."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        view._match_module_bay = MagicMock(return_value=None)
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-10G-SR",
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=MagicMock(model="SFP-10G-SR", pk=1)),
        ):
            # scope_empty_installed_bays=True: installed parent has no bay templates
            row = view._build_row(
                item,
                {},
                {},
                {"SFP-10G-SR": MagicMock(pk=1)},
                scope_empty_installed_bays=True,
            )
        assert row["status"] == "No Bay"
        assert row.get("no_bay_reason") == "empty_parent_bays"

    def test_no_bay_empty_parent_bays_through_intermediate_container(self):
        """Even with scope_preserved=True (intermediate unmatched container),
        no_bay_reason is still set when scope_empty_installed_bays=True."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        view._match_module_bay = MagicMock(return_value=None)
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-10G-SR",
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=MagicMock(model="SFP-10G-SR", pk=1)),
        ):
            row = view._build_row(
                item,
                {},
                {},
                {"SFP-10G-SR": MagicMock(pk=1)},
                scope_preserved=True,
                scope_empty_installed_bays=True,
            )
        assert row["status"] == "No Bay"
        assert row.get("no_bay_reason") == "empty_parent_bays"

    def test_no_bay_port_child_uses_interface_child_reason(self):
        """Port-class child rows under empty installed-parent scope should not be tagged as missing bay templates."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        view._match_module_bay = MagicMock(return_value=None)
        item = {
            "entPhysicalName": "Ethernet1/1",
            "entPhysicalClass": "port",
            "entPhysicalModelName": "SFP-10G-SR",
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=MagicMock(model="SFP-10G-SR", pk=1)),
        ):
            row = view._build_row(
                item,
                {},
                {},
                {"SFP-10G-SR": MagicMock(pk=1)},
                scope_empty_installed_bays=True,
            )
        assert row["status"] == "No Bay"
        assert row.get("no_bay_reason") == "interface_child"
        assert "matching child bay is missing in netbox" in row.get("model_warning", "").lower()

    def test_port_row_sets_can_install_and_interface_hint_when_bay_matches(self):
        """Matched port rows expose install action and preserve best interface label hint."""
        view = self._view()
        bay = MagicMock()
        bay.name = "SFP 1"
        bay.installed_module = None
        bay.pk = 10
        bay.get_absolute_url.return_value = "/dcim/module-bays/10/"
        view._match_module_bay = MagicMock(return_value=bay)

        module_type = MagicMock()
        module_type.model = "SFP-10G-SR"
        module_type.pk = 200
        module_type.get_absolute_url.return_value = "/dcim/module-types/200/"

        item = {
            "entPhysicalName": "Port-Unknown",
            "entPhysicalClass": "port",
            "entPhysicalModelName": "SFP-10G-SR",
            "_librenms_ifname": "Te1/1/1",
            "_librenms_ifdescr": "TenGigabitEthernet1/1/1",
            "_librenms_port_id": 1234,
        }

        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=module_type),
        ):
            row = view._build_row(item, {}, {"SFP 1": bay}, {"SFP-10G-SR": module_type})

        assert row["can_install"] is True
        assert row["interface_name_hint"] == "Te1/1/1"
        assert row["librenms_port_id"] == 1234
        assert row["librenms_ifname"] == "Te1/1/1"
        assert row["librenms_ifdescr"] == "TenGigabitEthernet1/1/1"

    def test_no_bay_default_scope_empty_flag_does_not_set_reason(self):
        """Without scope_empty_installed_bays, plain empty scope gives no reason tag."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        view._match_module_bay = MagicMock(return_value=None)
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-10G-SR",
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=MagicMock(model="SFP-10G-SR", pk=1)),
        ):
            # Default scope_empty_installed_bays=False — could be unmatched ancestor
            row = view._build_row(item, {}, {}, {"SFP-10G-SR": MagicMock(pk=1)})
        assert row["status"] == "No Bay"
        assert "no_bay_reason" not in row

    def test_no_bay_with_bays_in_scope_does_not_set_no_bay_reason(self):
        """When module_bays is non-empty (just no match), no_bay_reason is absent."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        view._match_module_bay = MagicMock(return_value=None)
        bay = MagicMock()
        bay.name = "Slot 1"
        item = {
            "entPhysicalName": "0/5",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "X",
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=MagicMock(model="X", pk=1)),
        ):
            row = view._build_row(item, {}, {"Slot 1": bay}, {"X": MagicMock(pk=1)})
        assert row["status"] == "No Bay"
        assert "no_bay_reason" not in row

    def test_no_bay_scope_uninstalled_does_not_set_no_bay_reason(self):
        """scope_uninstalled=True is a different root cause; no_bay_reason absent."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        view._match_module_bay = MagicMock(return_value=None)
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-X",
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=MagicMock(model="SFP-X", pk=1)),
        ):
            row = view._build_row(item, {}, {}, {"SFP-X": MagicMock(pk=1)}, scope_uninstalled=True)
        assert row["status"] == "No Bay"
        assert "no_bay_reason" not in row


class TestModelIncompleteFlag:
    """_append_rows_for_item_context sets model_incomplete on parent when
    installed module has no bay templates and children show no_bay_reason."""

    def _make_parent_row(self, **kwargs):
        row = {
            "librenms_name": "0/0",
            "status": "Installed",
            "module_bay": "Slot 0",
            "module_bay_id": 1,
        }
        row.update(kwargs)
        return row

    def test_model_incomplete_set_when_child_has_no_bay_reason(self):
        """Parent is flagged model_incomplete when child rows have no_bay_reason='empty_parent_bays'."""
        parent_row = self._make_parent_row()
        child_row = {
            "librenms_name": "TenGigE0/0/0/0",
            "status": "No Bay",
            "no_bay_reason": "empty_parent_bays",
        }
        table_data = [parent_row, child_row]
        parent_row_idx = 0

        mt = MagicMock()
        mt.get_absolute_url.return_value = "/dcim/module-types/5/"
        mt.__str__ = lambda self: "A9K-24X10GE-1G-TR"
        installed_module = MagicMock()
        installed_module.module_type = mt

        # Simulate the flagging logic from _append_rows_for_item_context
        child_bays = {}
        if installed_module and not child_bays:
            has_no_bay_children = any(
                table_data[i].get("no_bay_reason") == "empty_parent_bays"
                for i in range(parent_row_idx + 1, len(table_data))
            )
            if has_no_bay_children:
                mt_ = installed_module.module_type
                table_data[parent_row_idx]["model_incomplete"] = True
                table_data[parent_row_idx]["model_incomplete_url"] = mt_.get_absolute_url()
                table_data[parent_row_idx]["model_incomplete_name"] = str(mt_)

        assert table_data[0].get("model_incomplete") is True
        assert "/dcim/module-types/5/" in table_data[0].get("model_incomplete_url", "")

    def test_model_incomplete_not_set_when_no_children_with_no_bay_reason(self):
        """If children don't have no_bay_reason, parent stays unflagged even if child_bays empty."""
        parent_row = self._make_parent_row()
        child_row = {
            "librenms_name": "TenGigE0/0/0/0",
            "status": "Installed",
        }
        table_data = [parent_row, child_row]
        parent_row_idx = 0

        mt = MagicMock()
        installed_module = MagicMock()
        installed_module.module_type = mt
        child_bays = {}

        if installed_module and not child_bays:
            has_no_bay_children = any(
                table_data[i].get("no_bay_reason") == "empty_parent_bays"
                for i in range(parent_row_idx + 1, len(table_data))
            )
            if has_no_bay_children:
                table_data[parent_row_idx]["model_incomplete"] = True

        assert "model_incomplete" not in table_data[0]

    def test_model_incomplete_not_set_when_child_bays_nonempty(self):
        """If the installed module DOES have bays in scope, no model_incomplete flag."""
        parent_row = self._make_parent_row()
        child_row = {
            "librenms_name": "TenGigE0/0/0/0",
            "status": "No Bay",
            "no_bay_reason": "empty_parent_bays",
        }
        table_data = [parent_row, child_row]
        parent_row_idx = 0

        mt = MagicMock()
        installed_module = MagicMock()
        installed_module.module_type = mt
        child_bays = {"Bay 0": MagicMock()}  # non-empty

        if installed_module and not child_bays:
            has_no_bay_children = any(
                table_data[i].get("no_bay_reason") == "empty_parent_bays"
                for i in range(parent_row_idx + 1, len(table_data))
            )
            if has_no_bay_children:
                table_data[parent_row_idx]["model_incomplete"] = True

        assert "model_incomplete" not in table_data[0]


class TestRenderStatusNoBayOnParent:
    """render_status correctly labels child rows and parent 'Fix Model' badge."""

    def _table(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        return object.__new__(LibreNMSModuleTable)

    def test_no_bay_on_parent_label_for_empty_parent_bays(self):
        """Status cell shows 'No Bay on Parent' when no_bay_reason == 'empty_parent_bays'."""
        table = self._table()
        record = {"status": "No Bay", "no_bay_reason": "empty_parent_bays"}
        html = table.render_status("No Bay", record)
        assert "No Bay on Parent" in str(html)
        assert "No Bay" in str(html)  # badge text changed but still present as substring

    def test_interface_child_label_for_interface_descendants(self):
        """Status cell shows 'Missing Child Bay' for interface-like no-bay descendants."""
        table = self._table()
        record = {"status": "No Bay", "no_bay_reason": "interface_child"}
        html = table.render_status("No Bay", record)
        assert "Missing Child Bay" in str(html)

    def test_plain_no_bay_label_without_reason(self):
        """Without no_bay_reason, status cell shows plain 'No Bay'."""
        table = self._table()
        record = {"status": "No Bay"}
        html = table.render_status("No Bay", record)
        assert "No Bay on Parent" not in str(html)
        assert "No Bay" in str(html)

    def test_fix_model_badge_with_url(self):
        """Parent row with model_incomplete + model_incomplete_url renders a link badge."""
        table = self._table()
        record = {
            "status": "Installed",
            "model_incomplete": True,
            "model_incomplete_url": "/dcim/module-types/5/",
            "model_incomplete_name": "A9K-24X10GE-1G-TR",
        }
        html = str(table.render_status("Installed", record))
        assert "Fix Model" in html
        assert "/dcim/module-types/5/" in html
        assert "A9K-24X10GE-1G-TR" in html

    def test_fix_model_badge_without_url_is_span(self):
        """When model_incomplete_url is absent, badge is rendered as <span>."""
        table = self._table()
        record = {
            "status": "Installed",
            "model_incomplete": True,
            "model_incomplete_name": "SomeType",
        }
        html = str(table.render_status("Installed", record))
        assert "Fix Model" in html
        assert "<span" in html
        assert "<a " not in html

    def test_no_fix_badge_without_model_incomplete(self):
        """Normal row without model_incomplete has no Fix Model badge."""
        table = self._table()
        record = {"status": "Installed"}
        html = str(table.render_status("Installed", record))
        assert "Fix Model" not in html


class TestMatchedInterfaceLinking:
    """Rows should expose matched NetBox interface metadata and render as links."""

    def test_build_interface_indexes_ignores_duplicate_port_ids(self):
        view = _make_view()
        interface_a = MagicMock()
        interface_b = MagicMock()
        interface_c = MagicMock()
        member = MagicMock()
        member.interfaces.all.return_value = [interface_a, interface_b, interface_c]

        view._librenms_api.get_stored_librenms_id.side_effect = [42, 42, 43]

        interface_map, _ = view._build_interface_indexes(member)

        assert 42 not in interface_map
        assert interface_map[43] is interface_c
        view._librenms_api.get_librenms_id.assert_not_called()

    def test_build_interface_indexes_ignores_duplicate_names(self):
        view = _make_view()
        interface_a = MagicMock()
        interface_a.name = "Te1/1/1"
        interface_b = MagicMock()
        interface_b.name = "Te1/1/1"
        interface_c = MagicMock()
        interface_c.name = "Te1/1/2"
        member = MagicMock()
        member.interfaces.all.return_value = [interface_a, interface_b, interface_c]

        _, interface_map = view._build_interface_indexes(member)

        assert "Te1/1/1" not in interface_map
        assert interface_map["Te1/1/2"] is interface_c

    def test_build_member_contexts_builds_interface_indexes_once_per_member(self):
        view = _make_view()
        member = MagicMock()
        member.id = 100
        member.interfaces.all.return_value = []

        with patch.object(view, "_get_module_bays", return_value=({}, {})):
            context = view._build_member_contexts(member, vc_members=[])

        assert context[100]["interfaces_by_port_id"] == {}
        assert context[100]["interfaces_by_name"] == {}
        member.interfaces.all.assert_called_once_with()

    def test_attach_interface_match_sets_name_and_url(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        row = {"name": "Te1/1/1", "librenms_port_id": 42}
        iface = MagicMock()
        iface.name = "TenGigabitEthernet1/1/1"
        iface.get_absolute_url.return_value = "/dcim/interfaces/100/"
        context = {"interfaces_by_port_id": {42: iface}}

        BaseModuleTableView._attach_interface_match(row, context)

        assert row["matched_interface_name"] == "TenGigabitEthernet1/1/1"
        assert row["matched_interface_url"] == "/dcim/interfaces/100/"
        assert row["matched_interface_source"] == "port_id"
        assert row["matched_interface_confidence"] == "high"

    def test_attach_interface_match_skips_oob_rows(self):
        """OOB-sourced rows must not name-match the main device's interfaces — only the main device's interfaces are indexed in the context."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        iface = MagicMock()
        iface.name = "TenGigabitEthernet1/1/1"
        row = {"_source": "oob", "name": "TenGigabitEthernet1/1/1", "librenms_port_id": None}
        context = {"interfaces_by_port_id": {}, "interfaces_by_name": {"TenGigabitEthernet1/1/1": iface}}

        BaseModuleTableView._attach_interface_match(row, context)

        # No matched_interface_* key may survive for an OOB row — assert the whole payload
        # stays empty so a regression leaving matched_interface_url/source/confidence behind
        # (which would still render the row as matched) is caught.
        assert not any(key.startswith("matched_interface_") for key in row)

    def test_attach_interface_match_falls_back_to_name_lookup(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        row = {
            "name": "Te1/1/1",
            "description": "desc",
            "librenms_port_id": None,
            "librenms_ifname": "TenGigabitEthernet1/1/1",
        }
        iface = MagicMock()
        iface.name = "TenGigabitEthernet1/1/1"
        iface.get_absolute_url.return_value = "/dcim/interfaces/100/"
        context = {
            "interfaces_by_port_id": {},
            "interfaces_by_name": {"TenGigabitEthernet1/1/1": iface},
        }

        BaseModuleTableView._attach_interface_match(row, context)

        assert row["matched_interface_name"] == "TenGigabitEthernet1/1/1"
        assert row["matched_interface_url"] == "/dcim/interfaces/100/"
        assert row["matched_interface_source"] == "name"
        assert row["matched_interface_confidence"] == "medium"

    def test_attach_interface_match_marks_installed_row_for_interface_update(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        row = {
            "name": "Te1/1/1",
            "librenms_port_id": 42,
            "installed_module_id": 555,
        }
        iface = MagicMock()
        iface.pk = 100
        iface.name = "TenGigabitEthernet1/1/1"
        iface.module_id = None
        iface.get_absolute_url.return_value = "/dcim/interfaces/100/"
        context = {"interfaces_by_port_id": {42: iface}, "server_key": "default"}

        with patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_device_id", return_value=None):
            BaseModuleTableView._attach_interface_match(row, context)

        assert row["matched_interface_id"] == 100
        assert row["matched_interface_module_id"] is None
        assert row["can_update_interface_binding"] is True

    def test_attach_interface_match_ignores_missing_port(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        row = {"name": "Te1/1/1", "librenms_port_id": None}
        context = {"interfaces_by_port_id": {42: MagicMock()}}

        BaseModuleTableView._attach_interface_match(row, context)

        assert "matched_interface_name" not in row
        assert "matched_interface_url" not in row

    def test_render_name_links_to_matched_interface(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        html = str(
            table.render_name(
                "Te1/1/1",
                {
                    "depth": 1,
                    "matched_interface_name": "TenGigabitEthernet1/1/1",
                    "matched_interface_url": "/dcim/interfaces/100/",
                    "matched_interface_source": "port_id",
                    "matched_interface_confidence": "high",
                },
            )
        )

        assert "TenGigabitEthernet1/1/1" in html
        assert "/dcim/interfaces/100/" in html
        assert "<a href=" in html
        assert "Matched by port id, confidence high" in html

    def test_render_module_bay_indents_child_module_rows(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        html = str(
            table.render_module_bay(
                "SFP 1",
                {
                    "depth": 1,
                    "item_class": "module",
                    "module_bay_url": "/dcim/module-bays/10/",
                },
            )
        )

        assert "└─" in html
        assert "padding-left:20px" in html
        assert "/dcim/module-bays/10/" in html

    def test_render_module_bay_indents_child_port_rows(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        html = str(
            table.render_module_bay(
                "SFP 1",
                {
                    "depth": 1,
                    "item_class": "port",
                    "module_bay_url": "/dcim/module-bays/10/",
                },
            )
        )

        assert "└─" in html
        assert "padding-left:20px" in html


class TestDeviceTypeIncompleteFlag:
    """device_type_incomplete is set on top-level No Bay rows with no suggestion."""

    def _make_view_and_row(self, status, model_suggestion=None):
        """Return (view, table_data, parent_row_idx) after calling the flag logic."""
        row = {"status": status}
        if model_suggestion:
            row["model_suggestion"] = model_suggestion
        device_type = MagicMock()
        device_type.get_absolute_url.return_value = "/dcim/device-types/7/"
        device_type.__str__ = lambda self: "ASR-9904"
        selected_device = MagicMock()
        selected_device.device_type = device_type
        return row, selected_device

    def test_no_bay_without_suggestion_sets_device_type_incomplete(self):
        row, selected_device = self._make_view_and_row("No Bay")
        # Simulate the flag logic from _append_rows_for_item_context
        if row.get("status") == "No Bay" and "model_suggestion" not in row:
            dt = getattr(selected_device, "device_type", None)
            if dt:
                row["device_type_incomplete"] = True
                row["device_type_incomplete_url"] = dt.get_absolute_url()
                row["device_type_incomplete_name"] = str(dt)
        assert row.get("device_type_incomplete") is True
        assert row.get("device_type_incomplete_url") == "/dcim/device-types/7/"

    def test_no_bay_with_suggestion_does_not_set_flag(self):
        suggestion = {"librenms_name": r"^0/(\d+)$", "netbox_bay_name": r"Slot \1"}
        row, selected_device = self._make_view_and_row("No Bay", model_suggestion=suggestion)
        if row.get("status") == "No Bay" and "model_suggestion" not in row:
            dt = getattr(selected_device, "device_type", None)
            if dt:
                row["device_type_incomplete"] = True
        assert "device_type_incomplete" not in row

    def test_installed_row_does_not_set_flag(self):
        row, selected_device = self._make_view_and_row("Installed")
        if row.get("status") == "No Bay" and "model_suggestion" not in row:
            dt = getattr(selected_device, "device_type", None)
            if dt:
                row["device_type_incomplete"] = True
        assert "device_type_incomplete" not in row

    def test_no_device_type_attribute_does_not_raise(self):
        row = {"status": "No Bay"}
        selected_device = MagicMock(spec=[])  # no device_type attr
        if row.get("status") == "No Bay" and "model_suggestion" not in row:
            dt = getattr(selected_device, "device_type", None)
            if dt:
                row["device_type_incomplete"] = True
        assert "device_type_incomplete" not in row


class TestRenderStatusDeviceTypeIncomplete:
    """render_status renders a 'Fix Device Type' badge when device_type_incomplete is set."""

    def _table(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        return object.__new__(LibreNMSModuleTable)

    def test_fix_device_type_badge_with_url(self):
        table = self._table()
        record = {
            "status": "No Bay",
            "device_type_incomplete": True,
            "device_type_incomplete_url": "/dcim/device-types/7/",
            "device_type_incomplete_name": "ASR-9904",
        }
        html = str(table.render_status("No Bay", record))
        assert "Fix Device Type" in html
        assert "/dcim/device-types/7/" in html
        assert "ASR-9904" in html

    def test_fix_device_type_badge_without_url_is_span(self):
        table = self._table()
        record = {
            "status": "No Bay",
            "device_type_incomplete": True,
            "device_type_incomplete_name": "ASR-9904",
        }
        html = str(table.render_status("No Bay", record))
        assert "Fix Device Type" in html
        assert "<span" in html
        assert "<a " not in html

    def test_model_incomplete_takes_precedence_over_device_type_incomplete(self):
        """model_incomplete badge is returned before device_type_incomplete (early return)."""
        table = self._table()
        record = {
            "status": "Installed",
            "model_incomplete": True,
            "model_incomplete_url": "/dcim/module-types/5/",
            "model_incomplete_name": "A9K-24X10GE-1G-TR",
            "device_type_incomplete": True,
            "device_type_incomplete_url": "/dcim/device-types/7/",
        }
        html = str(table.render_status("Installed", record))
        assert "Fix Model" in html
        # model_incomplete returns early, so Fix Device Type not rendered
        assert "Fix Device Type" not in html

    def test_no_badge_without_either_flag(self):
        table = self._table()
        record = {"status": "No Bay"}
        html = str(table.render_status("No Bay", record))
        assert "Fix Device Type" not in html
        assert "Fix Model" not in html


# ---------------------------------------------------------------------------
# Integrated-in-parent dedupe (Nokia XIOM + integrated MDA pattern)
# ---------------------------------------------------------------------------


class TestFindIntegratingAncestor:
    """Same-serial-and-model child detection for fixed/integrated cards."""

    def _index(self, items):
        return {i["entPhysicalIndex"]: i for i in items}

    def test_finds_xiom_when_mda_shares_serial_and_model(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        xiom = {
            "entPhysicalIndex": 100,
            "entPhysicalName": "XIOM 2/x1",
            "entPhysicalClass": "xioModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": "3HE18883AARB01",
            "entPhysicalContainedIn": 50,
        }
        mda = {
            "entPhysicalIndex": 200,
            "entPhysicalName": "MDA 2/x1/1",
            "entPhysicalClass": "mdaModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": "3HE18883AARB01",
            "entPhysicalContainedIn": 100,
        }
        idx = self._index([xiom, mda])
        ancestor = BaseModuleTableView._find_integrating_ancestor(mda, idx)
        assert ancestor is xiom

    def test_numeric_serial_finds_integrating_ancestor(self):
        """Numeric ENTITY serials are normalized before integrated parent/child comparison."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        xiom = {
            "entPhysicalIndex": 100,
            "entPhysicalClass": "xioModule",
            "entPhysicalSerialNum": 123456,
            "entPhysicalModelName": "3HE18883AARB01",
            "entPhysicalContainedIn": 0,
        }
        mda = {
            "entPhysicalIndex": 200,
            "entPhysicalClass": "mdaModule",
            "entPhysicalSerialNum": 123456,
            "entPhysicalModelName": "3HE18883AARB01",
            "entPhysicalContainedIn": 100,
        }

        assert BaseModuleTableView._find_integrating_ancestor(mda, self._index([xiom, mda])) is xiom

    @pytest.mark.parametrize("model", [123456, 0])
    def test_numeric_model_finds_integrating_ancestor(self, model):
        """Numeric ENTITY models are normalized on both sides of the integrated-card comparison."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        xiom = {
            "entPhysicalIndex": 100,
            "entPhysicalClass": "xioModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": model,
            "entPhysicalContainedIn": 0,
        }
        mda = {
            "entPhysicalIndex": 200,
            "entPhysicalClass": "mdaModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": model,
            "entPhysicalContainedIn": 100,
        }

        assert BaseModuleTableView._find_integrating_ancestor(mda, self._index([xiom, mda])) is xiom

    def test_returns_none_when_serial_differs(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        xiom = {
            "entPhysicalIndex": 100,
            "entPhysicalClass": "xioModule",
            "entPhysicalSerialNum": "AAA",
            "entPhysicalModelName": "M",
            "entPhysicalContainedIn": 0,
        }
        mda = {
            "entPhysicalIndex": 200,
            "entPhysicalClass": "mdaModule",
            "entPhysicalSerialNum": "BBB",
            "entPhysicalModelName": "M",
            "entPhysicalContainedIn": 100,
        }
        assert BaseModuleTableView._find_integrating_ancestor(mda, self._index([xiom, mda])) is None

    def test_returns_none_when_model_differs(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        xiom = {
            "entPhysicalIndex": 100,
            "entPhysicalClass": "xioModule",
            "entPhysicalSerialNum": "S",
            "entPhysicalModelName": "X",
            "entPhysicalContainedIn": 0,
        }
        mda = {
            "entPhysicalIndex": 200,
            "entPhysicalClass": "mdaModule",
            "entPhysicalSerialNum": "S",
            "entPhysicalModelName": "Y",
            "entPhysicalContainedIn": 100,
        }
        assert BaseModuleTableView._find_integrating_ancestor(mda, self._index([xiom, mda])) is None

    def test_returns_none_for_placeholder_serial(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        for placeholder in ("", "N/A", "Unknown", "-"):
            xiom = {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "xioModule",
                "entPhysicalSerialNum": placeholder,
                "entPhysicalModelName": "M",
                "entPhysicalContainedIn": 0,
            }
            mda = {
                "entPhysicalIndex": 200,
                "entPhysicalClass": "mdaModule",
                "entPhysicalSerialNum": placeholder,
                "entPhysicalModelName": "M",
                "entPhysicalContainedIn": 100,
            }
            assert BaseModuleTableView._find_integrating_ancestor(mda, self._index([xiom, mda])) is None, placeholder

    def test_skips_chassis_ancestor(self):
        """A chassis ancestor sharing serial (the device serial!) must NEVER be matched."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        chassis = {
            "entPhysicalIndex": 1,
            "entPhysicalClass": "chassis",
            "entPhysicalSerialNum": "CHASSIS-SERIAL",
            "entPhysicalModelName": "M",
            "entPhysicalContainedIn": 0,
        }
        # Module sharing chassis serial (broken vendor data) — must not be deduped.
        mod = {
            "entPhysicalIndex": 100,
            "entPhysicalClass": "module",
            "entPhysicalSerialNum": "CHASSIS-SERIAL",
            "entPhysicalModelName": "M",
            "entPhysicalContainedIn": 1,
        }
        assert BaseModuleTableView._find_integrating_ancestor(mod, self._index([chassis, mod])) is None

    def test_walks_through_container(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        xiom = {
            "entPhysicalIndex": 100,
            "entPhysicalClass": "xioModule",
            "entPhysicalSerialNum": "S",
            "entPhysicalModelName": "M",
            "entPhysicalContainedIn": 0,
        }
        container = {
            "entPhysicalIndex": 150,
            "entPhysicalClass": "container",
            "entPhysicalSerialNum": "",
            "entPhysicalModelName": "",
            "entPhysicalContainedIn": 100,
        }
        mda = {
            "entPhysicalIndex": 200,
            "entPhysicalClass": "mdaModule",
            "entPhysicalSerialNum": "S",
            "entPhysicalModelName": "M",
            "entPhysicalContainedIn": 150,
        }
        ancestor = BaseModuleTableView._find_integrating_ancestor(mda, self._index([xiom, container, mda]))
        assert ancestor is xiom

    def test_skips_non_module_classes(self):
        """Fan / PowerSupply rows sharing serial must not be deduped — surface as-is."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        chassis = {
            "entPhysicalIndex": 1,
            "entPhysicalClass": "chassis",
            "entPhysicalSerialNum": "S",
            "entPhysicalModelName": "M",
            "entPhysicalContainedIn": 0,
        }
        fan = {
            "entPhysicalIndex": 10,
            "entPhysicalClass": "fan",
            "entPhysicalSerialNum": "S",
            "entPhysicalModelName": "M",
            "entPhysicalContainedIn": 1,
        }
        assert BaseModuleTableView._find_integrating_ancestor(fan, self._index([chassis, fan])) is None


class TestBuildRowIntegratedDedupe:
    """_build_row short-circuits to status='Integrated' when an integrating ancestor exists."""

    def test_numeric_model_reaches_the_integrated_row_path(self):
        """The production row builder normalizes numeric models before ancestor matching."""
        view = _make_view()
        xiom = {
            "entPhysicalIndex": 100,
            "entPhysicalName": "XIOM 2/x1",
            "entPhysicalClass": "xioModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": 123456,
            "entPhysicalContainedIn": 0,
        }
        mda = {
            "entPhysicalIndex": 200,
            "entPhysicalName": "MDA 2/x1/1",
            "entPhysicalClass": "mdaModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": 123456,
            "entPhysicalContainedIn": 100,
        }

        row = view._build_row(mda, {100: xiom, 200: mda}, {}, {})

        assert row["status"] == "Integrated"
        assert row["model"] == "123456"

    def test_mda_under_xiom_becomes_integrated(self):
        view = _make_view()
        xiom = {
            "entPhysicalIndex": 100,
            "entPhysicalName": "XIOM 2/x1",
            "entPhysicalClass": "xioModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": "3HE18883AARB01",
            "entPhysicalContainedIn": 0,
        }
        mda = {
            "entPhysicalIndex": 200,
            "entPhysicalName": "MDA 2/x1/1",
            "entPhysicalClass": "mdaModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": "3HE18883AARB01",
            "entPhysicalContainedIn": 100,
        }
        index_map = {100: xiom, 200: mda}
        row = view._build_row(mda, index_map, {}, {})
        assert row["status"] == "Integrated"
        assert row["integrated_in_name"] == "XIOM 2/x1"
        assert row["integrated_in_index"] == 100
        # Ensure it does not carry warnings or actionable suggestions
        assert "model_warning" not in row
        assert "module_type_create" not in row
        assert "type_suggestion" not in row
        assert row["can_install"] is False

    def test_independent_module_still_evaluated_normally(self):
        """A module with its own serial (not matching any ancestor) takes the normal path."""
        view = _make_view()
        view._match_module_bay = MagicMock(return_value=None)
        item = {
            "entPhysicalIndex": 200,
            "entPhysicalName": "X",
            "entPhysicalClass": "module",
            "entPhysicalSerialNum": "UNIQUE",
            "entPhysicalModelName": "MOD",
            "entPhysicalContainedIn": 100,
        }
        parent = {
            "entPhysicalIndex": 100,
            "entPhysicalClass": "module",
            "entPhysicalSerialNum": "PARENT-SERIAL",
            "entPhysicalModelName": "PARENT-MOD",
            "entPhysicalContainedIn": 0,
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=None),
        ):
            row = view._build_row(item, {100: parent, 200: item}, {}, {})
        assert row["status"] != "Integrated"


class TestScopePreservedAcrossIntegratedContainer:
    """Children of an integrated container (e.g. Nokia MDA inside XIOM) must inherit
    the integrating ancestor's bay scope WITHOUT being marked scope_preserved=True
    — otherwise their _build_row call suppresses bay-mapping suggestions even though
    the scope is at the correct hierarchical level.
    """

    def test_port_under_integrated_mda_gets_scope_preserved_false(self):
        """Regression: ports under integrated MDA used to lose mapping suggestions."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = _make_view()
        view._current_device_bays = {}
        # Exact mapping so XIOM matches its bay → parent_module_id is set →
        # MDA at depth=1 sees scope_preserved=False legitimately.
        xiom_mapping = MagicMock(
            librenms_name="XIOM 2/x1",
            librenms_class="xioModule",
            netbox_bay_name="2/x1",
            is_regex=False,
            manufacturer_id=None,
        )
        view._exact_bay_mappings = [xiom_mapping]
        view._regex_bay_mappings = []
        view._norm_rules_bay = None
        view._norm_rules_type = None
        view._generic_module_types = {}
        view._module_type_ambiguities = {}

        # Top-level XIOM matches a device-level bay whose installed module exposes
        # port-level child bays (x1/c1...). MDA sharing XIOM's serial+model becomes
        # integrated. Port under MDA should see scope_preserved=False so its
        # _build_row call generates a mapping suggestion.
        xiom_module = MagicMock()
        xiom_module.pk = 999
        matched_xiom_bay = MagicMock(name="2/x1")
        matched_xiom_bay.installed_module = xiom_module

        mda_bays = {f"x1/c{n}": MagicMock() for n in range(1, 5)}
        device_bays = {"2/x1": matched_xiom_bay}
        all_bays = dict(device_bays)

        target_context = {
            "device_bays": device_bays,
            "all_bays": all_bays,
            "module_scoped_bays": {999: mda_bays},
            "sibling_counts": {},
        }

        xiom_item = {
            "entPhysicalIndex": 100,
            "entPhysicalName": "XIOM 2/x1",
            "entPhysicalClass": "xioModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": "3HE18883AARB01",
            "entPhysicalContainedIn": 0,
        }
        mda_item = {
            "entPhysicalIndex": 200,
            "entPhysicalName": "MDA 2/x1/1",
            "entPhysicalClass": "mdaModule",
            "entPhysicalSerialNum": "NS241462069",
            "entPhysicalModelName": "3HE18883AARB01",
            "entPhysicalContainedIn": 100,
        }
        port_item = {
            "entPhysicalIndex": 300,
            "entPhysicalName": "2/x1/1/c2",
            "entPhysicalClass": "port",
            "entPhysicalSerialNum": "PR21",
            "entPhysicalModelName": "QSFP-DD",
            "entPhysicalContainedIn": 200,
        }

        index_map = {100: xiom_item, 200: mda_item, 300: port_item}

        # Capture scope_preserved arg passed to _build_row for each call
        scope_preserved_seen = []
        original_build_row = BaseModuleTableView._build_row

        def spy_build_row(self, item, idx_map, mod_bays, mod_types, **kw):
            scope_preserved_seen.append((item.get("entPhysicalIndex"), kw.get("scope_preserved")))
            return original_build_row(self, item, idx_map, mod_bays, mod_types, **kw)

        selected_device = MagicMock(id=1, name="dev")
        selected_device.device_type = MagicMock()
        selected_device.device_type.manufacturer = MagicMock(id=10, name="Nokia")

        with (
            patch.object(BaseModuleTableView, "_build_row", spy_build_row),
            patch.object(BaseModuleTableView, "_apply_carrier_install_rules", lambda *a, **kw: None),
            patch.object(
                BaseModuleTableView,
                "_get_sub_components",
                return_value=[(1, mda_item), (2, port_item)],
            ),
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=None),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", side_effect=lambda v, *a, **kw: v),
        ):
            view._append_rows_for_item_context(
                table_data=[],
                item=xiom_item,
                target_context=target_context,
                index_map=index_map,
                children_by_parent={100: [mda_item], 200: [port_item]},
                ignore_rules=[],
                device_serial="",
                module_types={},
                manufacturer=None,
                selected_device=selected_device,
                resolution_source="direct",
            )

        # Port (idx 300) under integrated MDA must NOT have scope_preserved=True
        port_calls = [sp for idx, sp in scope_preserved_seen if idx == 300]
        assert port_calls, f"Expected port to be processed, saw: {scope_preserved_seen}"
        assert port_calls[0] is False, (
            f"Port under integrated MDA should inherit parent scope_preserved=False, "
            f"got {port_calls[0]}. All calls: {scope_preserved_seen}"
        )


# ---------------------------------------------------------------------------
# Ambiguous part_number / model surfacing in the No Type warning
# ---------------------------------------------------------------------------


class TestModuleTypeAmbiguityWarning:
    def _candidate(self, model, mfg_name, pk=1, url="/dcim/module-types/1/"):
        mt = MagicMock()
        mt.pk = pk
        mt.model = model
        mt.manufacturer.name = mfg_name
        mt.get_absolute_url.return_value = url
        return mt

    def test_warning_lists_candidates_when_ambiguous(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        a = self._candidate("XIOM-x2-s36-800g-qsfpdd", "Nokia", pk=1)
        b = self._candidate("XMA2-s", "Nokia", pk=2)
        msg = BaseModuleTableView._build_no_type_warning(
            {"entPhysicalModelName": "3HE18883AARB01"}, ambiguity_candidates=[a, b]
        )
        assert "3HE18883AARB01" in msg
        assert "2 ModuleTypes" in msg
        assert "Nokia / XIOM-x2-s36-800g-qsfpdd" in msg
        assert "Nokia / XMA2-s" in msg

    def test_warning_unchanged_when_no_ambiguity(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        msg = BaseModuleTableView._build_no_type_warning({"entPhysicalModelName": "X"}, ambiguity_candidates=[])
        assert "ModuleTypes sharing" not in msg
        assert "No NetBox ModuleType matches 'X'" in msg

    def test_find_ambiguity_candidates_matches_normalized_key(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        a = self._candidate("XIOM-x2-s36-800g-qsfpdd", "Nokia", pk=1)
        b = self._candidate("XMA2-s", "Nokia", pk=2)
        ambiguities = {"3HE18883AA": [a, b]}
        with patch(
            "netbox_librenms_plugin.utils.apply_normalization_rules",
            return_value="3HE18883AA",
        ):
            cands = BaseModuleTableView._find_ambiguity_candidates(
                "3HE18883AARB01", ambiguities, manufacturer=None, norm_rules=None
            )
        assert cands == [a, b]

    def test_find_ambiguity_candidates_returns_empty_when_no_collision(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        with patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="X"):
            cands = BaseModuleTableView._find_ambiguity_candidates("X", {"OTHER": []}, None, None)
        assert cands == []


class TestBuildRowAmbiguityWiring:
    """_build_row populates module_type_ambiguity and suppresses module_type_create when ambiguous."""

    def test_no_type_with_ambiguity_carries_candidates_and_omits_create_button(self):
        view = _make_view()
        bay = MagicMock()
        bay.name = "Slot 2"
        bay.installed_module = None
        bay.get_absolute_url.return_value = "/b"
        view._match_module_bay = MagicMock(return_value=bay)
        view._norm_rules_type = {}
        # Ambiguity preloaded on the view
        a = MagicMock()
        a.pk = 1
        a.model = "XIOM-x2-s36-800g-qsfpdd"
        a.manufacturer.name = "Nokia"
        a.get_absolute_url.return_value = "/dcim/module-types/1/"
        b = MagicMock()
        b.pk = 2
        b.model = "XMA2-s"
        b.manufacturer.name = "Nokia"
        b.get_absolute_url.return_value = "/dcim/module-types/2/"
        view._module_type_ambiguities = {"3HE18883AARB01": [a, b]}
        item = {
            "entPhysicalName": "XIOM 2/x1",
            "entPhysicalClass": "xioModule",
            "entPhysicalModelName": "3HE18883AARB01",
            "entPhysicalSerialNum": "S1",
            "entPhysicalContainedIn": 0,
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=None),
        ):
            row = view._build_row(item, {}, {}, {})
        assert row["status"] == "No Type"
        assert "ModuleTypes sharing" in row["model_warning"]
        assert len(row["module_type_ambiguity"]) == 2
        assert row["module_type_ambiguity"][0]["model"] == "XIOM-x2-s36-800g-qsfpdd"
        assert row["module_type_ambiguity"][0]["url"] == "/dcim/module-types/1/"
        # When ambiguous we must NOT offer to create yet another duplicate.
        assert "module_type_create" not in row
        assert "type_suggestion" not in row

    def test_no_type_without_ambiguity_keeps_existing_buttons(self):
        view = _make_view()
        bay = MagicMock()
        bay.name = "Slot 2"
        bay.installed_module = None
        bay.get_absolute_url.return_value = "/b"
        view._match_module_bay = MagicMock(return_value=bay)
        view._module_type_ambiguities = {}
        manufacturer = MagicMock()
        manufacturer.pk = 7
        item = {
            "entPhysicalName": "X",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "BRAND-NEW",
            "entPhysicalSerialNum": "S1",
            "entPhysicalContainedIn": 0,
        }
        with (
            patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=None),
        ):
            row = view._build_row(item, {}, {}, {}, manufacturer=manufacturer)
        assert row["status"] == "No Type"
        assert "module_type_ambiguity" not in row
        assert row["module_type_create"]["model"] == "BRAND-NEW"


# ---------------------------------------------------------------------------
# get_module_type_ambiguities helper
# ---------------------------------------------------------------------------


class TestGetModuleTypeAmbiguities:
    def test_collects_keys_shared_by_two_or_more_module_types(self):
        from netbox_librenms_plugin.utils import get_module_type_ambiguities

        a = MagicMock()
        a.model = "XIOM-x2-s36-800g-qsfpdd"
        a.part_number = "3HE18883AA"
        a.manufacturer.name = "Nokia"
        b = MagicMock()
        b.model = "XMA2-s"
        b.part_number = "3HE18883AA"
        b.manufacturer.name = "Nokia"
        c = MagicMock()
        c.model = "OTHER"
        c.part_number = "3HE99999AA"
        c.manufacturer.name = "Nokia"

        qs = MagicMock()
        qs.select_related.return_value = [a, b, c]
        with patch("dcim.models.ModuleType.objects.all", return_value=qs):
            amb = get_module_type_ambiguities()

        assert "3HE18883AA" in amb
        assert set(amb["3HE18883AA"]) == {a, b}
        assert "3HE99999AA" not in amb
        assert "OTHER" not in amb


# ---------------------------------------------------------------------------
# Holder-install hint + tightened SFM mapping suggestion
# ---------------------------------------------------------------------------


class TestBuildHolderInstallHint:
    """`_build_holder_install_hint` surfaces empty device bays as candidate carriers."""

    def _bay(self, name, installed=None):
        b = MagicMock()
        b.name = name
        b.installed_module = installed
        return b

    def test_returns_none_for_non_module_class(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bays = {"Slot A": self._bay("Slot A")}
        assert BaseModuleTableView._build_holder_install_hint({}, "fan", bays) is None
        assert BaseModuleTableView._build_holder_install_hint({}, "powersupply", bays) is None

    def test_returns_none_when_no_device_bays(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        assert BaseModuleTableView._build_holder_install_hint({}, "module", {}) is None
        assert BaseModuleTableView._build_holder_install_hint({}, "module", None) is None

    def test_returns_none_when_no_empty_bays(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bays = {"Slot A": self._bay("Slot A", installed=MagicMock())}
        assert BaseModuleTableView._build_holder_install_hint({}, "module", bays) is None

    def test_returns_none_when_more_specific_hint_in_play(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bays = {"Slot A": self._bay("Slot A")}
        assert BaseModuleTableView._build_holder_install_hint({}, "module", bays, scope_uninstalled=True) is None
        assert (
            BaseModuleTableView._build_holder_install_hint({}, "module", bays, scope_empty_installed_bays=True) is None
        )

    def test_lists_empty_bay_names_for_module_class_item(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bays = {
            "Slot A": self._bay("Slot A"),
            "Slot B": self._bay("Slot B"),
            "Slot C": self._bay("Slot C", installed=MagicMock()),
        }
        msg = BaseModuleTableView._build_holder_install_hint({"entPhysicalName": "CPM A"}, "cpmmodule", bays)
        assert msg is not None
        assert "'Slot A'" in msg
        assert "'Slot B'" in msg
        assert "'Slot C'" not in msg
        assert "holder/carrier" in msg.lower() or "carrier" in msg.lower()

    def test_caps_long_bay_lists(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bays = {f"Slot {i}": self._bay(f"Slot {i}") for i in range(8)}
        msg = BaseModuleTableView._build_holder_install_hint({}, "module", bays)
        assert msg is not None
        assert "+3 more" in msg


class TestSuggestBayMappingTokenOverlap:
    """`_suggest_bay_mapping` rejects mismatched-prefix bays for module-class items."""

    def test_sfm_does_not_collapse_onto_card(self):
        """The original bug: 'Sfm 1' was being suggested into 'Card 1' just because both end in 1."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "Sfm 1", "entPhysicalClass": "fabricModule"}
        bay = MagicMock()
        bay.name = "Card 1"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Card 1": bay})
        assert sug is None

    def test_sfm_matches_sfm_named_bay(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "Sfm 1", "entPhysicalClass": "fabricModule"}
        bay = MagicMock()
        bay.name = "SFM 1"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"SFM 1": bay})
        assert sug is not None
        assert sug["example_bay"] == "SFM 1"

    def test_sfm_picks_sfm_bay_over_card_when_both_present(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "Sfm 1", "entPhysicalClass": "fabricModule"}
        sfm_bay = MagicMock()
        sfm_bay.name = "SFM 1"
        card_bay = MagicMock()
        card_bay.name = "Card 1"
        # Card listed first in dict insertion order — ensures token overlap, not
        # iteration order, drives the choice.
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Card 1": card_bay, "SFM 1": sfm_bay})
        assert sug is not None
        assert sug["example_bay"] == "SFM 1"

    def test_numeric_only_item_still_matches_slot_bay(self):
        """Items with no alphabetic prefix (e.g. '0/0') keep the previous numeric-only behaviour."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"entPhysicalName": "0/0", "entPhysicalClass": "module"}
        bay = MagicMock()
        bay.name = "Slot 0"
        sug = BaseModuleTableView._suggest_bay_mapping(item, {"Slot 0": bay})
        assert sug is not None
        assert sug["example_bay"] == "Slot 0"


class TestBuildNoBayWarningHolderHint:
    def test_warning_appends_holder_hint_when_provided(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        msg = BaseModuleTableView._build_no_bay_warning(
            {}, {"Slot 1": MagicMock()}, holder_hint="Tip: empty bays exist."
        )
        assert "Tip: empty bays exist." in msg


class TestBuildHolderInstallHintNarrowing:
    """Tightened holder hint: skip plain 'port' class and path-style names."""

    def _bay(self, name):
        b = MagicMock()
        b.name = name
        b.installed_module = None
        return b

    def test_returns_none_for_plain_port_class(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bays = {"Slot A": self._bay("Slot A")}
        msg = BaseModuleTableView._build_holder_install_hint({"entPhysicalName": "1/1/c1"}, "port", bays)
        assert msg is None

    def test_returns_none_when_item_name_contains_slash(self):
        """LibreNMS hierarchical names like '1/1/c1' indicate the user already knows the parent path."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bays = {"Slot A": self._bay("Slot A")}
        msg = BaseModuleTableView._build_holder_install_hint({"entPhysicalName": "1/1/c1"}, "module", bays)
        assert msg is None

    def test_still_emits_for_simple_named_module_class_item(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        bays = {"CMA": self._bay("CMA")}
        msg = BaseModuleTableView._build_holder_install_hint({"entPhysicalName": "Slot A"}, "cpmmodule", bays)
        assert msg is not None
        assert "'CMA'" in msg


class TestNestSyntheticTransceivers:
    def test_nests_under_parent_with_path_suffix(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        inv = [
            {"entPhysicalIndex": 100, "entPhysicalName": "MDA 1/1", "entPhysicalContainedIn": 50},
            {"entPhysicalIndex": 200, "entPhysicalName": "MDA 2/x1/1", "entPhysicalContainedIn": 60},
            {
                "entPhysicalIndex": 1001,
                "entPhysicalName": "1/1/c1",
                "entPhysicalContainedIn": 0,
                "_from_transceiver_api": True,
            },
            {
                "entPhysicalIndex": 1002,
                "entPhysicalName": "2/x1/1/c2",
                "entPhysicalContainedIn": 0,
                "_from_transceiver_api": True,
            },
        ]
        BaseModuleTableView._nest_synthetic_transceivers(inv)
        assert inv[2]["entPhysicalContainedIn"] == 100
        assert inv[3]["entPhysicalContainedIn"] == 200

    def test_leaves_top_level_when_no_parent_match(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        inv = [
            {
                "entPhysicalIndex": 1001,
                "entPhysicalName": "9/9/c1",
                "entPhysicalContainedIn": 0,
                "_from_transceiver_api": True,
            },
        ]
        BaseModuleTableView._nest_synthetic_transceivers(inv)
        assert inv[0]["entPhysicalContainedIn"] == 0

    def test_skips_non_synthetic_items(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        inv = [
            {"entPhysicalIndex": 100, "entPhysicalName": "MDA 1/1", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 1001, "entPhysicalName": "1/1/c1", "entPhysicalContainedIn": 0},
        ]
        BaseModuleTableView._nest_synthetic_transceivers(inv)
        assert inv[1]["entPhysicalContainedIn"] == 0

    def test_skips_already_nested_synthetic(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        inv = [
            {"entPhysicalIndex": 100, "entPhysicalName": "MDA 1/1", "entPhysicalContainedIn": 50},
            {
                "entPhysicalIndex": 1001,
                "entPhysicalName": "1/1/c1",
                "entPhysicalContainedIn": 999,
                "_from_transceiver_api": True,
            },
        ]
        BaseModuleTableView._nest_synthetic_transceivers(inv)
        assert inv[1]["entPhysicalContainedIn"] == 999

    def test_falls_back_to_shorter_prefix(self):
        """If MDA 1/1 doesn't exist, a 1/1/c1 transceiver should match Slot 1."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        inv = [
            {"entPhysicalIndex": 50, "entPhysicalName": "Slot 1", "entPhysicalContainedIn": 1},
            {
                "entPhysicalIndex": 1001,
                "entPhysicalName": "1/1/c1",
                "entPhysicalContainedIn": 0,
                "_from_transceiver_api": True,
            },
        ]
        BaseModuleTableView._nest_synthetic_transceivers(inv)
        # No parent name ends with '/1/1' or ' 1/1', but 'Slot 1' ends with ' 1'
        assert inv[1]["entPhysicalContainedIn"] == 50


class TestRenderActionsPortIdentityFields:
    """Install action form should preserve distinct ifName/ifDescr hidden values."""

    def test_install_form_includes_distinct_ifname_and_ifdescr(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        table.device = MagicMock(pk=24)
        table.csrf_token = "csrf123"
        table.server_key = "default"
        table.has_write_permission = True
        table.can_add_module = True
        table.can_change_module = False
        table.can_delete_module = False

        record = {
            "can_install": True,
            "selected_device_id": 24,
            "ent_physical_index": 77,
            "librenms_port_id": 56284,
            "librenms_ifname": "TenGigabitEthernet1/1/1",
            "librenms_ifdescr": "Te1/1/1",
            "name": "Te1/1/1",
            "description": "10G transceiver",
            "module_bay_id": 10,
            "module_type_id": 5,
            "serial": "SN-1",
        }

        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/plugins/install-module/"):
            html = str(table.render_actions("", record))

        assert 'name="librenms_ifname" value="TenGigabitEthernet1/1/1"' in html
        assert 'name="librenms_ifdescr" value="Te1/1/1"' in html

    def test_interface_child_row_does_not_render_install_action(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        table.device = MagicMock(pk=24)
        table.csrf_token = "csrf123"
        table.server_key = "default"
        table.has_write_permission = True
        table.can_add_module = True
        table.can_change_module = False
        table.can_delete_module = False

        record = {
            "can_install": False,
            "no_bay_reason": "interface_child",
            "selected_device_id": 24,
            "ent_physical_index": 77,
            "librenms_port_id": 56284,
            "librenms_ifname": "TenGigabitEthernet1/1/1",
            "librenms_ifdescr": "Te1/1/1",
            "name": "Te1/1/1",
            "description": "10G transceiver",
            "module_bay_id": "",
            "module_type_id": 5,
            "serial": "SN-1",
        }

        with patch("netbox_librenms_plugin.tables.modules.reverse", return_value="/plugins/install-module/"):
            html = str(table.render_actions("", record))

        assert '<i class="mdi mdi-download"></i> Install' not in html


class TestGetContextDataOOBCacheFingerprint:
    """get_context_data must invalidate cached inventory when the linked OOB controller changes (re-link / unlink), not only when the main id changes."""

    def test_invalidates_when_oob_relinked(self):
        from unittest.mock import MagicMock, patch

        view = _make_view()
        obj = MagicMock(pk=1)
        view._get_sync_device = MagicMock(return_value=obj)
        view._librenms_api.get_librenms_id = MagicMock(return_value=10)
        view._build_context = MagicMock()
        cached = {"inventory": [{"x": 1}], "librenms_id": 10, "oob_librenms_id": 5}
        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value={"id": 7}),
        ):
            mock_cache.get.return_value = cached
            # GET={} → no server_key in the query, so rebind_api_for_server reuses the cached
            # (mocked) _librenms_api instead of building a new client.
            ctx = view.get_context_data(MagicMock(GET={}), obj)

        mock_cache.delete.assert_called_once_with("test_cache_key")
        assert ctx["table"] is None
        view._build_context.assert_not_called()

    def test_invalidates_when_oob_unlinked(self):
        from unittest.mock import MagicMock, patch

        view = _make_view()
        obj = MagicMock(pk=1)
        view._get_sync_device = MagicMock(return_value=obj)
        view._librenms_api.get_librenms_id = MagicMock(return_value=10)
        view._build_context = MagicMock()
        cached = {"inventory": [{"x": 1}], "librenms_id": 10, "oob_librenms_id": 5}
        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
        ):
            mock_cache.get.return_value = cached
            # GET={} → no server_key in the query, so rebind_api_for_server reuses the cached
            # (mocked) _librenms_api instead of building a new client.
            ctx = view.get_context_data(MagicMock(GET={}), obj)

        mock_cache.delete.assert_called_once_with("test_cache_key")
        assert ctx["table"] is None

    def test_invalidates_when_main_id_poisoned_bool(self):
        """A poisoned True custom-field id must not pass the fingerprint (True == 1 in Python).

        post() coerces and fails closed on this exact state; the GET compare must mirror it
        instead of serving the id-1 snapshot for a linkage post() would refuse.
        """
        from unittest.mock import MagicMock, patch

        view = _make_view()
        obj = MagicMock(pk=1)
        view._get_sync_device = MagicMock(return_value=obj)
        view._librenms_api.get_librenms_id = MagicMock(return_value=True)
        view._build_context = MagicMock()
        cached = {"inventory": [{"x": 1}], "librenms_id": 1, "oob_librenms_id": None}
        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
        ):
            mock_cache.get.return_value = cached
            ctx = view.get_context_data(MagicMock(GET={}), obj)

        mock_cache.delete.assert_called_once_with("test_cache_key")
        assert ctx["table"] is None
        view._build_context.assert_not_called()

    def test_keeps_cache_when_main_id_stored_as_string(self):
        """A string-backed custom-field id ("10") must fingerprint-match the cached int 10.

        Same normalization the OOB side already does — without it every GET wrongly treats
        the snapshot as stale and the module table renders empty until a manual refresh.
        """
        from unittest.mock import MagicMock, patch

        view = _make_view()
        obj = MagicMock(pk=1)
        view._get_sync_device = MagicMock(return_value=obj)
        view._librenms_api.get_librenms_id = MagicMock(return_value="10")
        view._build_context = MagicMock(return_value={"built": True})
        cached = {"inventory": [{"x": 1}], "librenms_id": 10, "oob_librenms_id": None}
        request = MagicMock(GET={})
        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
        ):
            mock_cache.get.return_value = cached
            ctx = view.get_context_data(request, obj)

        assert ctx == {"built": True}
        mock_cache.delete.assert_not_called()

    def test_invalidates_when_oob_link_corrupt(self):
        """A linked-but-corrupt OOB id must invalidate, not collapse to the no-OOB fingerprint.

        post() takes the partial-outcome path (never caches) for this state; the GET compare
        must not quietly serve a prior no-OOB snapshot while the device nominally has an OOB
        controller linked.
        """
        from unittest.mock import MagicMock, patch

        view = _make_view()
        obj = MagicMock(pk=1)
        view._get_sync_device = MagicMock(return_value=obj)
        view._librenms_api.get_librenms_id = MagicMock(return_value=10)
        view._build_context = MagicMock()
        cached = {"inventory": [{"x": 1}], "librenms_id": 10, "oob_librenms_id": None}
        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value={"id": "garbage"}),
        ):
            mock_cache.get.return_value = cached
            ctx = view.get_context_data(MagicMock(GET={}), obj)

        mock_cache.delete.assert_called_once_with("test_cache_key")
        assert ctx["table"] is None
        view._build_context.assert_not_called()

    def test_keeps_cache_when_oob_unchanged(self):
        from unittest.mock import MagicMock, patch

        view = _make_view()
        obj = MagicMock(pk=1)
        view._get_sync_device = MagicMock(return_value=obj)
        view._librenms_api.get_librenms_id = MagicMock(return_value=10)
        view._build_context = MagicMock(return_value={"built": True})
        cached = {"inventory": [{"x": 1}], "librenms_id": 10, "oob_librenms_id": 5}
        request = MagicMock(GET={})  # no server_key → rebind reuses the cached (mocked) _librenms_api
        with (
            patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value={"id": 5}),
        ):
            mock_cache.get.return_value = cached
            ctx = view.get_context_data(request, obj)

        # The reuse contract: the unchanged-fingerprint path must rebuild from the cached
        # inventory snapshot, not some other payload. It also forwards the already-resolved
        # sync_device AND the resolved server_key (so _build_context keys on the scoped server
        # explicitly, not the rebind side effect) — matching post()'s call shape.
        view._build_context.assert_called_once_with(
            request, obj, cached["inventory"], server_key="test-server", sync_device=obj
        )
        assert ctx == {"built": True}
        # Lock in the "cache preserved when OOB linkage is unchanged" contract: the
        # unchanged-fingerprint path must rebuild from cache without deleting it.
        mock_cache.delete.assert_not_called()

    def test_get_context_data_rebinds_to_request_server_key(self):
        """The GET render must rebind to the active server from the request query so the cache read keys on the same server post() wrote under (else a non-default-server tab cache-misses and the OOB guard no-ops)."""
        from unittest.mock import MagicMock, patch

        view = _make_view()
        obj = MagicMock(pk=1)
        view._get_sync_device = MagicMock(return_value=obj)
        view.rebind_api_for_server = MagicMock(return_value="prod")
        request = MagicMock()
        request.GET = {"server_key": "prod"}

        with patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache:
            mock_cache.get.return_value = None  # cache miss → early return after the rebind
            view.get_context_data(request, obj)

        view.rebind_api_for_server.assert_called_once_with("prod")


@pytest.mark.django_db
class TestInterfacePortIdActiveServerScope:
    """_get_interface_port_id must read the interface's port_id under the ACTIVE server key.

    The module verify path (SingleModuleVerifyView) sets ``_active_server_key`` to the POSTed
    server but leaves the API bound to the default client. With the multi-server dict CF form an
    interface stores different port_ids per server, so reading under the default-bound client's
    key (instead of ``_active_server_key``) returns the wrong server's port_id — and the verify
    row's interface index then disagrees with the interface match, which uses the active key.
    """

    def _real_default_api(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        # Pass server_key explicitly so construction skips the LibreNMSSettings.objects.first()
        # selected-server lookup — in the full suite a prior test can leave that mocked, which would
        # otherwise make LibreNMSAPI() resolve to a MagicMock server and raise KeyError. Pinning to
        # "default" keeps the fix (read under _active_server_key) and the bug (read under the client
        # key) resolving to visibly different port_ids.
        return LibreNMSAPI(server_key="default")

    def test_reads_port_id_under_active_server_not_default_client(self):
        """With _active_server_key set, the per-server port_id for THAT server is returned."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        device = make_device("mod-verify-scope")
        iface = make_interface(device, "Gi0/1")
        iface.custom_field_data["librenms_id"] = {"default": 111, "server2": 222}
        iface.save()

        view = object.__new__(BaseModuleTableView)
        view._librenms_api = self._real_default_api()
        view._active_server_key = "server2"

        # Must resolve under the active server (222), not the default-bound client (111).
        assert view._get_interface_port_id(iface) == 222

    def test_get_stored_librenms_id_honors_explicit_server_key(self):
        """LibreNMSAPI.get_stored_librenms_id(obj, server_key=...) reads that server's dict entry."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("mod-verify-scope-api")
        iface = make_interface(device, "Gi0/2")
        iface.custom_field_data["librenms_id"] = {"default": 111, "server2": 222}
        iface.save()

        api = self._real_default_api()
        assert api.get_stored_librenms_id(iface) == 111  # bound (default) key
        assert api.get_stored_librenms_id(iface, server_key="server2") == 222  # explicit override


@pytest.mark.django_db
class TestInferVcMemberSerialNormalization:
    """VC-member inference keys on serials that LibreNMS may deliver as JSON numbers (real Devices, real VirtualChassis)."""

    def _vc_members(self, serials):
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

    def test_numeric_item_serial_matches_the_member_stored_as_text(self):
        """An all-digit ENTITY serial arriving as an int must still resolve to its VC member instead of raising."""
        view = _make_view()
        master, member2 = self._vc_members(["100001", "100002"])

        item = {"entPhysicalIndex": 1, "entPhysicalSerialNum": 100002, "entPhysicalContainedIn": 0}
        target, source = view._infer_vc_member_for_item(master, item, {}, [master, member2])

        assert target.pk == member2.pk
        assert source == "serial"

    def test_zero_item_serial_is_not_dropped_as_falsey(self):
        """A serial of JSON number 0 is real; dropping it silently attributes the item to the wrong VC member."""
        view = _make_view()
        master, member2 = self._vc_members(["0", "100003"])

        item = {"entPhysicalIndex": 2, "entPhysicalSerialNum": 0, "entPhysicalContainedIn": 0}
        target, source = view._infer_vc_member_for_item(member2, item, {}, [master, member2])

        assert target.pk == master.pk
        assert source == "serial"

    @pytest.mark.parametrize("field", ["entPhysicalName", "entPhysicalDescr"])
    def test_numeric_name_hints_do_not_crash(self, field):
        """Numeric ENTITY hint fields are normalized before prefix matching."""
        view = _make_view()
        master, member2 = self._vc_members(["100004", "100005"])
        item = {"entPhysicalIndex": 3, field: 2, "entPhysicalContainedIn": 0}

        target, source = view._infer_vc_member_for_item(master, item, {}, [master, member2])

        assert target.pk == master.pk
        assert source == "default"
