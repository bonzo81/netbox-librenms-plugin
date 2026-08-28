"""
Tests for BaseModuleTableView sync logic (modules_view.py).

Focuses on the bay-scope tracking in _build_context and the serial
comparison logic in _build_row.
"""

from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def librenms_server(monkeypatch):
    """Provide a real HTTP LibreNMS server with test-specific responses."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        yield server


def configure_servers(settings, servers):
    """Replace the configured servers and preserve the remaining plugin settings."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_settings = plugin_config["netbox_librenms_plugin"]
    plugin_settings["servers"] = servers
    plugin_settings.pop("librenms_url", None)
    plugin_settings.pop("api_token", None)
    settings.PLUGINS_CONFIG = plugin_config


def _real_api_view(settings, server, *, librenms_id):
    """Build a table view that uses a real LibreNMS HTTP client."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    configure_servers(
        settings,
        {
            "test-server": {
                "librenms_url": server.url,
                "api_token": "test-token",
                "verify_ssl": False,
            }
        },
    )
    view = object.__new__(BaseModuleTableView)
    view._device_manufacturer = None
    view.librenms_id = librenms_id
    view._librenms_api = LibreNMSAPI(server_key="test-server")
    return view


def _count_port_requests(server, librenms_id):
    """Register a ports route that records every hit so a test can prove no fetch happened."""
    hits = []

    def route(**request):
        hits.append(request)
        return 404, {"status": "error", "message": "No ports response configured"}

    server.register(f"/api/v0/devices/{librenms_id}/ports", route, method="GET")
    return hits


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

    def test_a_numeric_description_still_names_the_rule_admitted_row(self):
        """LibreNMS sends an all-digit entPhysicalDescr as a JSON number, which the name
        fallback stripped directly."""
        inventory = self._inventory()
        inventory[1]["entPhysicalDescr"] = 20250907
        collected = self._collect(inventory, [self._include_rule()])
        view = _make_view()

        row = view._build_row(collected[0], {item["entPhysicalIndex"]: item for item in collected}, {}, {})

        assert row["name"] == "20250907"
        assert row["description"] == "20250907"

    def test_a_child_of_a_rule_admitted_parent_is_not_also_a_top_row(self):
        """The ancestor walk recognised only built-in classes.

        A standard-class child of a rule-admitted parent therefore reached top level while
        _get_sub_components() also rendered it under that parent: one duplicated row, and one
        duplicated install candidate.
        """
        items = [
            {
                "entPhysicalIndex": 38,
                "entPhysicalClass": "other",
                "entPhysicalName": "JNP304-RE-S",
                "entPhysicalModelName": "JNP304-RE-S",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 50,
                "entPhysicalClass": "module",
                "entPhysicalName": "RE daughter card",
                "entPhysicalModelName": "RE-DAUGHTER",
                "entPhysicalContainedIn": 38,
            },
        ]

        collected = self._collect(items, [self._include_rule()])

        assert [item["entPhysicalIndex"] for item in collected] == [38]

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


@pytest.mark.django_db
class TestSerialRulesFollowTheTargetDevice:
    """Serial rules are manufacturer-scoped, so the device a row targets picks them.

    A virtual-chassis row can resolve to a member whose manufacturer differs from the page
    device's. Normalizing the whole snapshot up front with the page manufacturer gave those
    rows a serial normalized under the wrong vendor's rules.
    """

    def _rule_for(self, manufacturer):
        from netbox_librenms_plugin.models import NormalizationRule

        return NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_SERIAL,
            manufacturer=manufacturer,
            match_pattern=r"^BBB-(.+)$",
            replacement=r"\1",
            priority=10,
        )

    def _item(self):
        return {
            "entPhysicalIndex": 5,
            "entPhysicalClass": "module",
            "entPhysicalName": "Slot 1",
            "entPhysicalModelName": "WS-X4748",
            "entPhysicalSerialNum": "BBB-12345",
            "entPhysicalContainedIn": 0,
        }

    def _serial_for(self, target_manufacturer, page_manufacturer=None):
        view = _make_view()
        view._current_manufacturer = target_manufacturer
        return view._normalized_item_serial(self._item(), page_manufacturer)

    def test_the_targets_own_rule_normalizes_the_serial(self):
        from dcim.models import Manufacturer

        owner = Manufacturer.objects.create(name="Serial Rule Owner", slug="serial-rule-owner")
        self._rule_for(owner)

        assert self._serial_for(owner) == "12345"

    def test_the_page_manufacturers_rule_does_not_reach_another_members_row(self):
        """The page device owns the rule; a VC row resolving to another vendor keeps the raw value."""
        from dcim.models import Manufacturer

        page = Manufacturer.objects.create(name="Page Vendor", slug="page-vendor")
        member = Manufacturer.objects.create(name="Member Vendor", slug="member-vendor")
        self._rule_for(page)

        assert self._serial_for(member, page_manufacturer=page) == "BBB-12345"

    def test_module_type_matching_uses_the_targets_manufacturer(self):
        """Type mappings are manufacturer-scoped too.

        The member vendor maps this LibreNMS model to its own type; the page device has no such
        mapping. Matching against the page manufacturer therefore resolves nothing at all, and a
        virtual-chassis row lost the type its own member declares.
        """
        from dcim.models import Manufacturer, ModuleType

        from netbox_librenms_plugin.models import ModuleTypeMapping
        from netbox_librenms_plugin.utils import get_module_types_indexed

        page = Manufacturer.objects.create(name="Page Vendor MT", slug="page-vendor-mt")
        member = Manufacturer.objects.create(name="Member Vendor MT", slug="member-vendor-mt")
        member_type = ModuleType.objects.create(manufacturer=member, model="MEMBER-TYPE")
        ModuleTypeMapping.objects.create(
            librenms_model="RAW-MODEL",
            netbox_module_type=member_type,
            manufacturer=member,
        )
        module_types = get_module_types_indexed()

        item = {
            "entPhysicalIndex": 7,
            "entPhysicalClass": "module",
            "entPhysicalName": "Slot 1",
            "entPhysicalModelName": "RAW-MODEL",
            "entPhysicalContainedIn": 0,
        }

        def row_for(target_manufacturer):
            view = _make_view()
            view._current_manufacturer = target_manufacturer
            return view._build_row(item, {item["entPhysicalIndex"]: item}, {}, module_types, manufacturer=page)

        assert row_for(member)["module_type_id"] == member_type.pk
        # The control: the page vendor declares no mapping, so nothing should resolve for it.
        assert row_for(page)["module_type_id"] is None

    def test_a_member_rule_applies_even_though_the_preload_used_the_page_manufacturer(self):
        """_build_context preloads for the page device, and rows resolve per target member.

        apply_normalization_rules loads a manufacturer it was not preloaded for and caches it
        into the same dict, so the member's rule still applies and costs one query per vendor.
        """
        from dcim.models import Manufacturer

        from netbox_librenms_plugin.utils import preload_normalization_rules

        page = Manufacturer.objects.create(name="Preload Page Vendor", slug="preload-page-vendor")
        member = Manufacturer.objects.create(name="Preload Member Vendor", slug="preload-member-vendor")
        self._rule_for(member)
        preloaded = preload_normalization_rules("serial", manufacturer=page)
        assert ("serial", member.pk) not in preloaded, "the member was never preloaded"

        view = _make_view()
        view._current_manufacturer = member
        view._norm_rules_serial = preloaded

        serial = view._normalized_item_serial(self._item(), page)

        assert serial == "12345"
        assert ("serial", member.pk) in preloaded, "the member's rules were not cached for reuse"

    def test_build_row_reports_the_serial_it_is_given(self):
        """_build_row must stay free of database access, so the caller resolves the serial."""
        view = _make_view()
        item = self._item()

        row = view._build_row(item, {item["entPhysicalIndex"]: item}, {}, {}, normalized_serial="12345")

        assert row["serial"] == "12345"


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


@pytest.mark.django_db
class TestMergeTransceiverDataPortIdentity:
    """Transceiver merge should preserve stable port identity metadata."""

    def test_invalid_transceiver_http_bodies_return_errors_without_mutating_inventory(self, settings, librenms_server):
        """Surface a malformed transceiver body as an error string so the caller skips the cache and warns."""
        view = _real_api_view(settings, librenms_server, librenms_id=100)
        seed = [{"entPhysicalIndex": 1, "entPhysicalName": "Gi0/1"}]

        def assert_malformed(body, expected_error):
            # Deep-copy the seed so a mutation of the SHARED inner dict by _merge_transceiver_data
            # can't also mutate `seed` and make the "untouched" assertion pass vacuously.
            candidate = deepcopy(seed)
            librenms_server.register(
                "/api/v0/devices/100/transceivers",
                body,
                method="GET",
            )
            inventory, error = view._merge_transceiver_data(candidate)
            assert inventory == seed
            assert error == expected_error

        unexpected_format = "Unexpected transceivers response format for device 100"
        assert_malformed(
            {"status": "ok", "transceivers": {"unexpected": "dict"}},
            unexpected_format,
        )
        assert_malformed(
            {
                "status": "ok",
                "transceivers": [{"entity_physical_index": 2}, "bad"],
            },
            "Malformed transceiver entry in response for device 100",
        )
        assert_malformed({"status": "ok"}, unexpected_format)

    def test_synthetic_item_includes_port_identity_metadata(self, settings, librenms_server):
        view = _real_api_view(settings, librenms_server, librenms_id=100)
        librenms_server.register(
            "/api/v0/devices/100/transceivers",
            {
                "status": "ok",
                "transceivers": [
                    {
                        "entity_physical_index": 200,
                        "model": "SFP-10G-SR",
                        "serial": "TX-200",
                        "type": "SFP",
                        "port_id": 42,
                    }
                ],
            },
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/100/ports",
            {"status": "ok", "ports": [{"port_id": 42, "ifName": "Te1/0/1"}]},
            method="GET",
        )

        inventory, error = view._merge_transceiver_data([])

        assert error is None
        assert len(inventory) == 1
        item = inventory[0]
        assert item["_from_transceiver_api"] is True
        assert item["_librenms_port_id"] == 42
        assert item["_librenms_ifname"] == "Te1/0/1"
        assert item["entPhysicalName"] == "Te1/0/1"

    def test_existing_inventory_item_gets_port_identity_metadata(self, settings, librenms_server):
        view = _real_api_view(settings, librenms_server, librenms_id=101)
        librenms_server.register(
            "/api/v0/devices/101/transceivers",
            {
                "status": "ok",
                "transceivers": [
                    {
                        "entity_physical_index": 300,
                        "model": "",
                        "serial": "",
                        "type": "SFP",
                        "port_id": 99,
                    }
                ],
            },
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/101/ports",
            {"status": "ok", "ports": [{"port_id": 99, "ifName": "Eth2/1"}]},
            method="GET",
        )
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

    def test_numeric_transceiver_serial_is_coerced_to_a_string(self, settings, librenms_server):
        """An all-digit transceiver serial arrives as an int; the merge must coerce it before stripping instead of raising."""
        view = _real_api_view(settings, librenms_server, librenms_id=104)
        librenms_server.register(
            "/api/v0/devices/104/transceivers",
            {
                "status": "ok",
                "transceivers": [
                    {
                        "entity_physical_index": 400,
                        "model": "SFP-10G-SR",
                        "serial": 987654,
                        "type": "SFP",
                        "port_id": 8,
                    }
                ],
            },
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/104/ports",
            {"status": "ok", "ports": [{"port_id": 8, "ifName": "Eth4/1"}]},
            method="GET",
        )

        inventory, error = view._merge_transceiver_data([])

        assert error is None
        assert len(inventory) == 1
        assert inventory[0]["entPhysicalSerialNum"] == "987654"

    def test_zero_transceiver_serial_is_preserved(self, settings, librenms_server):
        """A transceiver serial of JSON number 0 is a real serial and must survive as "0", not be dropped as falsey."""
        view = _real_api_view(settings, librenms_server, librenms_id=104)
        librenms_server.register(
            "/api/v0/devices/104/transceivers",
            {
                "status": "ok",
                "transceivers": [
                    {
                        "entity_physical_index": 401,
                        "model": "SFP-10G-SR",
                        "serial": 0,
                        "type": "SFP",
                        "port_id": 9,
                    }
                ],
            },
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/104/ports",
            {"status": "ok", "ports": [{"port_id": 9, "ifName": "Eth5/1"}]},
            method="GET",
        )

        inventory, error = view._merge_transceiver_data([])

        assert error is None
        assert len(inventory) == 1
        assert inventory[0]["entPhysicalSerialNum"] == "0"

    def test_numeric_transceiver_model_and_type_are_coerced_to_strings(self, settings, librenms_server):
        """An all-digit model/type arrives as a JSON number too; stripping it raw would 500 the modules refresh."""
        view = _real_api_view(settings, librenms_server, librenms_id=105)
        librenms_server.register(
            "/api/v0/devices/105/transceivers",
            {
                "status": "ok",
                "transceivers": [
                    {
                        "entity_physical_index": 402,
                        "model": 1000,
                        "serial": "TX-402",
                        "type": 40,
                        "port_id": 10,
                    }
                ],
            },
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/105/ports",
            {"status": "ok", "ports": [{"port_id": 10, "ifName": "Eth6/1"}]},
            method="GET",
        )

        inventory, error = view._merge_transceiver_data([])

        assert error is None
        assert len(inventory) == 1
        assert inventory[0]["entPhysicalModelName"] == "1000"
        assert inventory[0]["entPhysicalDescr"] == "40"

    def test_numeric_entity_values_on_the_existing_item_are_coerced(self, settings, librenms_server):
        """The ENTITY-MIB side of the merge can carry numeric model/serial values as well."""
        view = _real_api_view(settings, librenms_server, librenms_id=106)
        librenms_server.register(
            "/api/v0/devices/106/transceivers",
            {
                "status": "ok",
                "transceivers": [
                    {
                        "entity_physical_index": 403,
                        "model": "SFP-10G-SR",
                        "serial": "TX-403",
                        "type": "SFP",
                        "port_id": 11,
                    }
                ],
            },
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/106/ports",
            {"status": "ok", "ports": [{"port_id": 11, "ifName": "Eth7/1"}]},
            method="GET",
        )
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

    def test_string_index_matches_int_transceiver_index_no_duplicate(self, settings, librenms_server):
        """LibreNMS may report the ENTITY index as a string while the transceiver API returns an int; coercing both sides matches the existing row instead of caching a duplicate."""
        view = _real_api_view(settings, librenms_server, librenms_id=103)
        librenms_server.register(
            "/api/v0/devices/103/transceivers",
            {
                "status": "ok",
                "transceivers": [
                    {
                        "entity_physical_index": 300,
                        "model": "SFP-10G-SR",
                        "serial": "TX-300",
                        "type": "SFP",
                        "port_id": 7,
                    }
                ],
            },
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/103/ports",
            {"status": "ok", "ports": [{"port_id": 7, "ifName": "Eth3/1"}]},
            method="GET",
        )
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

    def test_enrich_inventory_port_identity_backfills_port_rows_from_ports_api(self, settings, librenms_server):
        view = _real_api_view(settings, librenms_server, librenms_id=102)
        librenms_server.register(
            "/api/v0/devices/102/ports",
            {
                "status": "ok",
                "ports": [
                    {
                        "port_id": 56284,
                        "ifName": "TenGigabitEthernet1/1/1",
                        "ifDescr": "Te1/1/1",
                    }
                ],
            },
            method="GET",
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

    def test_enrich_inventory_port_identity_skips_ambiguous_labels(self, settings, librenms_server):
        view = _real_api_view(settings, librenms_server, librenms_id=103)
        librenms_server.register(
            "/api/v0/devices/103/ports",
            {
                "status": "ok",
                "ports": [
                    {"port_id": 10, "ifName": "Te1/1/1", "ifDescr": "Uplink A"},
                    {"port_id": 11, "ifName": "Te1/1/1", "ifDescr": "Uplink B"},
                ],
            },
            method="GET",
        )

        inventory = [{"entPhysicalClass": "port", "entPhysicalName": "Te1/1/1"}]

        view._enrich_inventory_port_identity(inventory)

        assert "_librenms_port_id" not in inventory[0]

    def test_build_port_name_map_uses_provided_ports_payload_without_api_fetch(self, settings, librenms_server):
        view = _real_api_view(settings, librenms_server, librenms_id=104)
        port_requests = _count_port_requests(librenms_server, 104)

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
        assert port_requests == []

    def test_enrich_inventory_port_identity_uses_provided_ports_payload_without_api_fetch(
        self, settings, librenms_server
    ):
        view = _real_api_view(settings, librenms_server, librenms_id=105)
        port_requests = _count_port_requests(librenms_server, 105)

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
        assert port_requests == []


def _two_server_keys(settings, server, prefix):
    """Configure two real servers on the stub and return their keys."""
    config = {"librenms_url": server.url, "api_token": "test-token", "verify_ssl": False}
    primary, secondary = f"{prefix}-primary", f"{prefix}-secondary"
    configure_servers(settings, {primary: dict(config), secondary: dict(config)})
    return primary, secondary


def _mapped_device(name, server_key, librenms_id=777):
    """Create a real device carrying a real LibreNMS mapping under *server_key*."""
    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.utils import set_librenms_device_id

    device = make_device(name)
    set_librenms_device_id(device, librenms_id, server_key)
    device.save(update_fields=["custom_field_data"])
    return device


def _seed_snapshot(view, device, server_key, *, inventory=None, librenms_id=777, oob_librenms_id=None):
    """Store a real inventory snapshot in the real cache and return its key and payload."""
    from django.core.cache import cache

    from netbox_librenms_plugin.tests.view_test_helpers import trusted_module_inventory_payload

    payload = trusted_module_inventory_payload(
        device,
        [{"entPhysicalIndex": 12}] if inventory is None else inventory,
        server_key=server_key,
        librenms_id=librenms_id,
    )
    payload["oob_librenms_id"] = oob_librenms_id
    cache_key = view.get_cache_key(device, "inventory", server_key=server_key)
    cache.set(cache_key, payload)
    return cache_key, payload


@pytest.mark.django_db
class TestPostInventoryRefresh:
    """Inventory refreshes must persist only complete snapshots for the active server."""

    @pytest.fixture
    def server_keys(self, settings, librenms_server):
        return _two_server_keys(settings, librenms_server, "inventory")

    @staticmethod
    def _register_successful_refresh(librenms_server, inventory=None, transceivers=None, ports=None):
        librenms_server.register(
            "/api/v0/inventory/777/all",
            {"status": "ok", "inventory": inventory if inventory is not None else []},
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/777/transceivers",
            {"status": "ok", "transceivers": transceivers if transceivers is not None else []},
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/777/ports",
            {"status": "ok", "ports": ports if ports is not None else []},
            method="GET",
        )

    def test_post_fetches_ports_once_and_reuses_payload(self, librenms_server, server_keys):
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-port-reuse", server_key)
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Gi0/1",
                "entPhysicalDescr": "Host port",
                "entPhysicalClass": "port",
                "entPhysicalModelName": "HOST-PORT",
                "entPhysicalSerialNum": "PORT-1",
                "entPhysicalContainedIn": 0,
            }
        ]
        transceivers = [
            {
                "entity_physical_index": 2,
                "model": "SFP-10G-SR",
                "serial": "TX-2",
                "type": "SFP",
                "port_id": 42,
            }
        ]
        port_requests = []

        def ports_route(**request_data):
            port_requests.append(request_data)
            return 200, {
                "status": "ok",
                "ports": [
                    {"port_id": 10, "ifName": "Gi0/1", "ifDescr": "Host port"},
                    {"port_id": 42, "ifName": "Gi0/2", "ifDescr": "Transceiver port"},
                ],
            }

        librenms_server.register(
            "/api/v0/inventory/777/all",
            {"status": "ok", "inventory": inventory},
            method="GET",
        )
        librenms_server.register(
            "/api/v0/devices/777/transceivers",
            {"status": "ok", "transceivers": transceivers},
            method="GET",
        )
        librenms_server.register("/api/v0/devices/777/ports", ports_route, method="GET")

        view = DeviceModuleTableView()
        request = make_request("post", {"server_key": server_key})
        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert len(port_requests) == 1
        cached = cache.get(view.get_cache_key(device, "inventory", server_key=server_key))
        main_item = next(item for item in cached["inventory"] if item["entPhysicalIndex"] == 1)
        transceiver_item = next(item for item in cached["inventory"] if item["entPhysicalIndex"] == 2)
        assert main_item["_librenms_port_id"] == 10
        assert transceiver_item["_librenms_port_id"] == 42
        assert transceiver_item["_librenms_ifname"] == "Gi0/2"
        assert message_texts(request, "success") == ["Inventory data refreshed successfully."]

    def test_post_treats_non_list_inventory_as_fetch_failure(self, librenms_server, server_keys):
        """get_device_inventory is an external boundary. A non-list inventory body is a fetch failure."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-non-list-inventory", server_key)
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key)
        port_requests = _count_port_requests(librenms_server, 777)
        librenms_server.register(
            "/api/v0/inventory/777/all",
            {"status": "ok", "inventory": {"error": "weird shape"}},
            method="GET",
        )

        request = make_request("post", {"server_key": server_key})
        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert message_texts(request, "error") == [
            "Failed to fetch inventory from LibreNMS; see server logs for details."
        ]
        assert cache.get(cache_key) is None
        assert port_requests == []

    def test_post_stale_server_key_resolves_migrated_context_with_session_key(self, server_keys):
        """When the POSTed server_key is stale, resolve migrated context under the active session key. Using the stale key would miss the marker and re-enable a donor's sync controls."""
        from unittest.mock import patch

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.utils import build_migrated_context, mark_librenms_migrated
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        active_key, _ = server_keys
        donor = make_device("module-refresh-stale-server-donor")
        winner = make_device("module-refresh-stale-server-winner")
        mark_librenms_migrated(donor, winner.pk, active_key)
        donor.save(update_fields=["custom_field_data"])
        view = DeviceModuleTableView()
        view._librenms_api = LibreNMSAPI(server_key=active_key)
        request = make_request("post", {"server_key": "retired-inventory-server"})

        # The empty-table fragment does not render the migrated marker. Observe only the pure
        # context builder call while the real resolver, view, request, device, and renderer run.
        with patch(
            "netbox_librenms_plugin.utils.build_migrated_context",
            wraps=build_migrated_context,
        ) as migrated_context_spy:
            response = post(view, request, pk=donor.pk)

        assert response.status_code == 200
        migrated_context_spy.assert_called_once_with(donor, active_key)
        assert view.active_server_key == active_key
        assert message_texts(request, "error") == ["Selected LibreNMS server is no longer configured."]

    def test_post_treats_non_dict_inventory_entry_as_fetch_failure(self, librenms_server, server_keys):
        """A list payload that carries non-dict entries, such as None, is a fetch failure."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-non-dict-inventory", server_key)
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key)
        port_requests = _count_port_requests(librenms_server, 777)
        librenms_server.register(
            "/api/v0/inventory/777/all",
            {"status": "ok", "inventory": [{"entPhysicalIndex": 1}, None]},
            method="GET",
        )

        request = make_request("post", {"server_key": server_key})
        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert message_texts(request, "error") == [
            "Failed to fetch inventory from LibreNMS; see server logs for details."
        ]
        assert cache.get(cache_key) is None
        assert port_requests == []

    def test_get_context_data_rejects_malformed_cached_inventory(self, server_keys):
        """post() now fails closed on malformed inventory before caching, but a stale pre-fix cache entry like {"inventory": [None]} can still be read."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import bind_and_call, make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-cache-malformed-inventory", server_key, librenms_id=1)
        view = DeviceModuleTableView()
        cache_key = view.get_cache_key(device, "inventory", server_key=server_key)
        # Keep both identity fingerprints valid. This makes the malformed inventory guard the
        # only reason the snapshot is removed.
        cache.set(cache_key, {"inventory": [None], "librenms_id": 1, "oob_librenms_id": None})
        request = make_request("get", {"server_key": server_key})

        result = bind_and_call(view, request, "get_context_data", obj=device)

        assert cache.get(cache_key) is None
        assert result["table"] is None
        assert result["object"].pk == device.pk
        assert result["server_key"] == server_key

    def test_get_context_data_keys_cache_on_resolved_scoped_server(self, server_keys):
        """The cache read keys on the scoped server returned by the resolver, not the previously bound API server."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import (
            bind_and_call,
            make_request,
            trusted_module_inventory_payload,
        )
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        bound_key, scoped_key = server_keys
        device = _mapped_device("module-cache-scoped-server", scoped_key, librenms_id=1)
        view = DeviceModuleTableView()
        view._librenms_api = LibreNMSAPI(server_key=bound_key)
        payload = trusted_module_inventory_payload(device, [], server_key=scoped_key, librenms_id=1)
        scoped_cache_key = view.get_cache_key(device, "inventory", server_key=scoped_key)
        cache.set(scoped_cache_key, payload)
        request = make_request("get", {"server_key": scoped_key})

        result = bind_and_call(view, request, "get_context_data", obj=device)

        assert result["table"] is not None
        assert result["server_key"] == scoped_key
        assert view.librenms_api.server_key == scoped_key
        assert cache.get(scoped_cache_key) == payload

    def test_get_context_data_scopes_sync_device_to_resolved_server(self, server_keys):
        """The VC sync-device resolution uses the resolved server explicitly. It must not rely on the prior API binding."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.tests.view_test_helpers import (
            bind_and_call,
            make_request,
            trusted_module_inventory_payload,
        )
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        bound_key, scoped_key = server_keys
        _, members = make_virtual_chassis_members("module-cache-scoped-vc", count=2)
        bound_device, scoped_device = members
        set_librenms_device_id(bound_device, 701, bound_key)
        bound_device.save(update_fields=["custom_field_data"])
        payload = trusted_module_inventory_payload(scoped_device, [], server_key=scoped_key, librenms_id=777)
        view = DeviceModuleTableView()
        view._librenms_api = LibreNMSAPI(server_key=bound_key)
        scoped_cache_key = view.get_cache_key(scoped_device, "inventory", server_key=scoped_key)
        cache.set(scoped_cache_key, payload)
        request = make_request("get", {"server_key": scoped_key})

        result = bind_and_call(view, request, "get_context_data", obj=bound_device)

        assert result["table"] is not None
        assert result["server_key"] == scoped_key
        assert cache.get(scoped_cache_key) == payload
        assert cache.get(view.get_cache_key(bound_device, "inventory", server_key=scoped_key)) is None

    def test_post_warns_when_ports_fetch_fails(self, librenms_server, server_keys):
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-ports-failure", server_key)
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key)
        self._register_successful_refresh(librenms_server)
        librenms_server.register(
            "/api/v0/devices/777/ports",
            {"status": "error", "message": "ports api unavailable"},
            status=503,
            method="GET",
        )
        request = make_request("post", {"server_key": server_key})

        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert cache.get(cache_key) is None
        assert message_texts(request, "warning") == [
            "Inventory refresh was incomplete: port metadata fetch failed, so no module rows were"
            " loaded. Refresh Modules to try again. See server logs for details."
        ]
        assert message_texts(request, "success") == []

    def test_post_treats_malformed_ports_payload_as_fetch_failure(self, librenms_server, server_keys):
        """get_ports() can return success with a dict whose "ports" is missing, None, or contains non-dict entries. Port-id enrichment would otherwise silently do nothing."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-malformed-ports", server_key)
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key)
        self._register_successful_refresh(librenms_server)
        # get_ports() returns the decoded JSON body verbatim. This malformed payload reaches post().
        librenms_server.register(
            "/api/v0/devices/777/ports",
            {"status": "ok", "ports": None},
            method="GET",
        )
        request = make_request("post", {"server_key": server_key})

        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert cache.get(cache_key) is None
        assert message_texts(request, "warning") == [
            "Inventory refresh was incomplete: port metadata fetch failed, so no module rows were"
            " loaded. Refresh Modules to try again. See server logs for details."
        ]
        assert message_texts(request, "success") == []

    def test_post_skips_cache_on_oob_inventory_failure(self, librenms_server, server_keys):
        """When the OOB inventory fetch fails, do not cache the main-only snapshot under the current OOB fingerprint. Otherwise get_context_data() accepts it as complete, and the OOB rows and warning vanish until TTL or manual refresh."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.utils import set_librenms_oob
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-oob-failure", server_key)
        set_librenms_oob(device, 999, server_key, oob_type="idrac9")
        device.save(update_fields=["custom_field_data"])
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key, oob_librenms_id=999)
        self._register_successful_refresh(librenms_server)
        librenms_server.register(
            "/api/v0/inventory/999/all",
            {"status": "error", "message": "oob inventory unavailable"},
            status=503,
            method="GET",
        )
        request = make_request("post", {"server_key": server_key})

        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert cache.get(cache_key) is None
        warnings = message_texts(request, "warning")
        assert warnings == [
            "Inventory refresh was incomplete: OOB controller inventory fetch failed, so no module rows"
            " were loaded. Refresh Modules to try again. See server logs for details."
        ]
        # The toast must stay generic. Internal LibreNMS ids belong only in the server log.
        warning = warnings[0]
        assert "777" not in warning and "999" not in warning and "OOB id" not in warning
        assert message_texts(request, "success") == []

    def test_post_treats_non_dict_oob_inventory_entry_as_fetch_failure(self, librenms_server, server_keys):
        """The OOB inventory merge offsets indices and sets item["_source"] on every entry. A response with non-dict elements, such as None, must fail before that merge."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.utils import set_librenms_oob
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-oob-non-dict", server_key)
        set_librenms_oob(device, 999, server_key, oob_type="idrac9")
        device.save(update_fields=["custom_field_data"])
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key, oob_librenms_id=999)
        self._register_successful_refresh(librenms_server)
        librenms_server.register(
            "/api/v0/inventory/999/all",
            {"status": "ok", "inventory": [{"entPhysicalIndex": 1}, None]},
            method="GET",
        )
        request = make_request("post", {"server_key": server_key})

        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert cache.get(cache_key) is None
        assert message_texts(request, "warning") == [
            "Inventory refresh was incomplete: OOB controller inventory fetch failed, so no module rows"
            " were loaded. Refresh Modules to try again. See server logs for details."
        ]
        assert message_texts(request, "success") == []

    def test_post_corrupt_oob_id_fails_closed(self, librenms_server, server_keys):
        """A linked OOB controller whose stored id is Boolean or non-numeric must fail closed like the interfaces and cables tabs. Do not fetch the garbage id or cache a host-only snapshot. Warn the user instead. A falsy check would conflate this state with no OOB link and silently drop the controller rows until TTL."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-corrupt-oob", server_key)
        view = DeviceModuleTableView()
        cache_key, stale_payload = _seed_snapshot(view, device, server_key)
        # The custom field is user-editable through the NetBox UI and API, so a corrupt value is real state.
        device.custom_field_data["librenms_id"][server_key] = {
            "id": 777,
            "oob": {"id": "not-a-number", "type": "oob"},
        }
        device.save(update_fields=["custom_field_data"])
        cache.set(cache_key, stale_payload)
        self._register_successful_refresh(librenms_server)
        corrupt_oob_requests = []

        def corrupt_oob_route(**request_data):
            corrupt_oob_requests.append(request_data)
            return 200, {"status": "ok", "inventory": []}

        librenms_server.register(
            "/api/v0/inventory/not-a-number/all",
            corrupt_oob_route,
            method="GET",
        )
        request = make_request("post", {"server_key": server_key})

        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert corrupt_oob_requests == []
        assert cache.get(cache_key) is None
        assert message_texts(request, "warning") == [
            "Inventory refresh was incomplete: OOB controller inventory fetch failed, so no module rows"
            " were loaded. Refresh Modules to try again. See server logs for details."
        ]
        assert message_texts(request, "success") == []

    def test_post_poisoned_bool_librenms_id_fails_closed(self, librenms_server, server_keys):
        """A poisoned Boolean device id must never be fired at LibreNMS. post() fails closed and drops the stale snapshot."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = make_device("module-refresh-poisoned-bool")
        api = LibreNMSAPI(server_key=server_key)
        # The device-id cache path returns its value verbatim, which is why the Boolean guard exists.
        cache.set(api._get_cache_key(device, server_key=server_key), True)
        view = DeviceModuleTableView()
        cache_key = view.get_cache_key(device, "inventory", server_key=server_key)
        cache.set(cache_key, {"inventory": [], "librenms_id": 1, "oob_librenms_id": None})
        inventory_requests = []

        def any_inventory_route(**request_data):
            inventory_requests.append(request_data)
            return 200, {"status": "ok", "inventory": []}

        for poisoned_path in ("/api/v0/inventory/True/all", "/api/v0/inventory/1/all"):
            librenms_server.register(poisoned_path, any_inventory_route, method="GET")
        request = make_request("post", {"server_key": server_key})

        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert inventory_requests == []
        assert cache.get(cache_key) is None
        assert message_texts(request) == ["Device not found in LibreNMS."]

    def test_post_no_oob_linked_stays_clean_success(self, librenms_server, server_keys):
        """No OOB link means get_librenms_oob returns None. This is not a failure. Show a clean success toast and cache the snapshot."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-no-oob", server_key)
        self._register_successful_refresh(librenms_server)
        view = DeviceModuleTableView()
        request = make_request("post", {"server_key": server_key})

        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert message_texts(request, "success") == ["Inventory data refreshed successfully."]
        assert message_texts(request, "warning") == []
        assert cache.get(view.get_cache_key(device, "inventory", server_key=server_key)) == {
            "inventory": [],
            "librenms_id": 777,
            "oob_librenms_id": None,
        }

    def test_post_treats_non_int_oob_index_as_fetch_failure(self, librenms_server, server_keys):
        """An OOB inventory row with a non-int entPhysicalIndex fails closed. It must not raise an offset TypeError or cache a partial snapshot."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.utils import set_librenms_oob
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-oob-string-index", server_key)
        set_librenms_oob(device, 999, server_key, oob_type="idrac9")
        device.save(update_fields=["custom_field_data"])
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key, oob_librenms_id=999)
        self._register_successful_refresh(librenms_server)
        # The offset arithmetic would evaluate "5" + offset and return HTTP 500 without the guard.
        librenms_server.register(
            "/api/v0/inventory/999/all",
            {
                "status": "ok",
                "inventory": [{"entPhysicalIndex": "5", "entPhysicalName": "oob-fan"}],
            },
            method="GET",
        )
        request = make_request("post", {"server_key": server_key})

        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert cache.get(cache_key) is None
        assert message_texts(request, "warning") == [
            "Inventory refresh was incomplete: OOB controller inventory fetch failed, so no module rows"
            " were loaded. Refresh Modules to try again. See server logs for details."
        ]
        assert message_texts(request, "success") == []

    def test_get_context_data_oob_fingerprint_equates_int_and_string(self, server_keys):
        """A cached int OOB id and a current string id with the same value compare equal. Do not invalidate the snapshot."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import (
            bind_and_call,
            make_request,
            trusted_module_inventory_payload,
        )
        from netbox_librenms_plugin.utils import set_librenms_oob
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-cache-oob-string-id", server_key, librenms_id=1)
        set_librenms_oob(device, 5, server_key, oob_type="idrac9")
        device.save(update_fields=["custom_field_data"])
        payload = trusted_module_inventory_payload(device, [], server_key=server_key, librenms_id=1)
        payload["oob_librenms_id"] = 5
        view = DeviceModuleTableView()
        cache_key = view.get_cache_key(device, "inventory", server_key=server_key)
        cache.set(cache_key, payload)
        # The custom field is user-editable, and the UI or API can store the numeric id as a string.
        device.custom_field_data["librenms_id"][server_key]["oob"]["id"] = "5"
        device.save(update_fields=["custom_field_data"])
        cache.set(cache_key, payload)
        request = make_request("get", {"server_key": server_key})

        result = bind_and_call(view, request, "get_context_data", obj=device)

        assert result["table"] is not None
        assert result["server_key"] == server_key
        assert cache.get(cache_key) == payload

    def test_post_skips_cache_on_transceiver_failure(self, librenms_server, server_keys):
        """A transceiver-enrichment failure drops synthetic transceiver rows. Do not cache the truncated inventory, for the same reason as an OOB failure."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-refresh-transceiver-failure", server_key)
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key)
        self._register_successful_refresh(librenms_server)
        librenms_server.register(
            "/api/v0/devices/777/transceivers",
            {"status": "error", "message": "transceiver fetch failed"},
            status=503,
            method="GET",
        )
        request = make_request("post", {"server_key": server_key})

        response = post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert cache.get(cache_key) is None
        assert message_texts(request, "warning") == [
            "Inventory refresh was incomplete: transceiver fetch failed, so no module rows were loaded."
            " Refresh Modules to try again. See server logs for details."
        ]
        assert message_texts(request, "success") == []


# ---------------------------------------------------------------------------
# Inventory data factories
# ---------------------------------------------------------------------------


def _linecard_inventory():
    """Return linecard inventory with installed and uninstalled sibling converters."""
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
    """Verify an uninstalled converter clears stale child-bay scope between sibling converters."""

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
        """Verify an uninstalled converter yields "No Bay" instead of a stale "Serial Mismatch"."""
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
        """Verify child-bay scope resets for each sibling converter."""
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
    """Return Cisco WS-X4908-10GE inventory that exercises regex and positional bay matching."""
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
        """Verify positional fallback maps the first converter child to SFP 1 by sibling order."""
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
        """Verify a matched bay without an installed converter leaves its child transceiver with no bay."""
        rows = self._build_rows(cvr_installed=False)
        row = self._row(rows, "GigabitEthernet3/11")
        assert row is not None, "GigabitEthernet3/11 row not found"
        assert row["module_bay"] == "-", (
            f"Expected no bay match (got {row['module_bay']!r}) — "
            "without an installed CVR there is no SFP scope for positional fallback"
        )
        assert row["status"] == "No Bay", f"Expected status='No Bay' but got {row['status']!r}"

    def test_no_cvr_entry_does_not_match_via_grandparent_walking(self):
        """Verify a transceiver does not match its converter bay through model-less ancestor containers."""
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
    """Verify positional fallback stops at model-less module scaffolding before chassis bays."""

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
            patch(
                "netbox_librenms_plugin.utils.preload_normalization_rules",
                side_effect=lambda scope, manufacturer=None: {(scope, None): []},
            ),
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
        """Verify TenGigE ports below model-less modules do not match a chassis bay."""
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
        """Verify transceivers below model-less module scaffolding show "No Bay"."""
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
        """Verify sibling TenGigE ports do not collapse to duplicate bay assignments."""
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


@pytest.mark.django_db
class TestBuildRowSerialMismatch:
    """Tests for serial mismatch detection and can_update_serial flag in _build_row."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        return view

    def _make_installed_rows(
        self,
        installed_serial,
        *,
        installed_model="XCM-7s-b",
        matched_model="XCM-7s-b",
    ):
        """Create a real device-level bay, installed module, and matched module type."""
        from netbox_librenms_plugin.tests.conftest import (
            install_module,
            make_device_with_module_bays,
            make_module_type,
        )

        device = make_device_with_module_bays("build-row-serial", ["Slot 1"])
        installed = install_module(device, "Slot 1", installed_model, serial=installed_serial)
        if installed_model == matched_model:
            matched_type = installed.module_type
        else:
            matched_type = make_module_type(matched_model)
        module_bays = {bay.name: bay for bay in device.modulebays.filter(module__isnull=True)}
        module_types = {matched_type.model: matched_type}
        return installed, module_bays, module_types

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
        _installed, module_bays, module_types = self._make_installed_rows("NS225161205")

        row = view._build_row(
            self._make_item(serial="NS225161205"),
            {},
            module_bays,
            module_types,
        )

        assert row["status"] == "Installed"
        assert "row_class" not in row
        assert not row.get("can_update_serial")

    def test_installed_row_sets_update_interface_when_template_matches_exist(self):
        """Installed non-port rows expose Update Interface when standalone template matches exist."""
        from dcim.models import Interface, InterfaceTemplate

        view = self._view()
        installed, module_bays, module_types = self._make_installed_rows("NS225161205")
        for name in ("TenGigabitEthernet1/1/1", "TenGigabitEthernet1/1/2"):
            InterfaceTemplate.objects.create(module_type=installed.module_type, name=name, type="other")
            Interface.objects.create(device=installed.device, name=name, type="other")

        row = view._build_row(
            self._make_item(serial="NS225161205"),
            {},
            module_bays,
            module_types,
        )

        assert row["status"] == "Installed"
        assert row["can_update_interface_binding"] is True
        assert row["adoptable_interface_count"] == 2

    def test_count_adoptable_template_interfaces_uses_vc_aware_names(self):
        from dcim.models import Interface, InterfaceTemplate

        from netbox_librenms_plugin.tests.conftest import (
            install_module,
            make_module_bay,
            make_virtual_chassis_members,
        )

        view = self._view()
        _virtual_chassis, members = make_virtual_chassis_members("build-row-vc", count=3)
        device = members[2]
        make_module_bay(device, "Slot 1")
        module = install_module(device, "Slot 1", "VC-AWARE-MODULE")
        InterfaceTemplate.objects.create(
            module_type=module.module_type,
            name="TenGigabitEthernet1/1/1",
            type="other",
        )
        Interface.objects.create(device=device, name="TenGigabitEthernet3/1/1", type="other")

        result = view._count_adoptable_template_interfaces(module)

        assert result == 1

    def test_serial_mismatch_sets_can_update_serial(self):
        """When serials differ, can_update_serial=True and installed_module_id set."""
        view = self._view()
        installed, module_bays, module_types = self._make_installed_rows("TESTSRL")

        row = view._build_row(
            self._make_item(serial="NS225161205"),
            {},
            module_bays,
            module_types,
        )

        assert row["status"] == "Serial Mismatch"
        assert "row_class" not in row
        assert row.get("can_update_serial") is True
        assert row.get("installed_module_id") == installed.pk

    def test_empty_netbox_serial_flags_mismatch(self):
        """When NetBox serial is empty but LibreNMS has one, status is Serial Mismatch."""
        view = self._view()
        _installed, module_bays, module_types = self._make_installed_rows("")

        row = view._build_row(
            self._make_item(serial="NS225161205"),
            {},
            module_bays,
            module_types,
        )

        assert row["status"] == "Serial Mismatch"
        assert row.get("can_update_serial")
        assert row.get("can_replace")

    def test_oob_row_short_circuits_before_host_matching(self):
        """An OOB-controller item whose model/name WOULD match a host bay+type must not be compared against the host: it short-circuits to a neutral read-only row (status 'OOB', '-' bay/type, no action flags) before any bay/type/status resolution runs."""
        view = self._view()
        # This bay+type would produce a "Serial Mismatch"/"Installed" match for a host row,
        # so a regression that drops the early return would surface a host status here.
        _installed, module_bays, module_types = self._make_installed_rows("TESTSRL")

        oob_item = self._make_item(serial="NS225161205")
        oob_item["_source"] = "oob"
        assert view._match_module_bay(oob_item, {}, module_bays) is module_bays["Slot 1"]

        row = view._build_row(
            oob_item,
            {},
            module_bays,
            module_types,
        )

        # Host matching never ran, so the row carries neutral bay/type/status and no actions.
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
        oob_item["entPhysicalContainedIn"] = 1
        ancestor = {
            "entPhysicalName": "P",
            "entPhysicalIndex": 1,
            "entPhysicalClass": "xioModule",
            "entPhysicalModelName": "XCM-7s-b",
            "entPhysicalSerialNum": "NS225161205",
            "entPhysicalContainedIn": 0,
        }
        index_map = {1: ancestor, 100: oob_item}
        assert view._find_integrating_ancestor(oob_item, index_map) is ancestor

        row = view._build_row(oob_item, index_map, {}, {})

        assert row["status"] == "OOB"
        assert row["_source"] == "oob"
        # OOB returns before the integrated-child check even consults the ancestor.

    def test_type_mismatch_sets_type_mismatch_status(self):
        """When installed module type differs from LibreNMS type, status is Type Mismatch."""
        view = self._view()
        # The installed and matched rows use different real module types.
        _installed, module_bays, module_types = self._make_installed_rows(
            "S1",
            installed_model="INSTALLED-XCM-7S-B",
            matched_model="XCM-7s-b",
        )

        row = view._build_row(
            self._make_item(model_name="XCM-7s-b", serial="NS225161205"),
            {},
            module_bays,
            module_types,
        )

        assert row["status"] == "Type Mismatch"
        assert "row_class" not in row

    def test_type_mismatch_sets_can_replace(self):
        """Type Mismatch row has can_replace=True and installed_module_id set."""
        view = self._view()
        installed, module_bays, module_types = self._make_installed_rows(
            "S1",
            installed_model="INSTALLED-XCM-7S-B",
            matched_model="XCM-7s-b",
        )

        row = view._build_row(
            self._make_item(model_name="XCM-7s-b", serial="NS225161205"),
            {},
            module_bays,
            module_types,
        )

        assert row.get("can_replace") is True
        assert row.get("installed_module_id") == installed.pk

    def test_serial_mismatch_also_sets_can_replace(self):
        """Serial Mismatch rows also get can_replace=True (same type)."""
        view = self._view()
        _installed, module_bays, module_types = self._make_installed_rows("TESTSRL")

        row = view._build_row(
            self._make_item(serial="NS225161205"),
            {},
            module_bays,
            module_types,
        )

        assert row["status"] == "Serial Mismatch"
        assert row.get("can_replace") is True
        assert row.get("can_update_serial") is True

    def test_same_type_same_serial_no_replace(self):
        """Clean Installed row has neither can_replace nor can_update_serial."""
        view = self._view()
        _installed, module_bays, module_types = self._make_installed_rows("NS225161205")

        row = view._build_row(
            self._make_item(serial="NS225161205"),
            {},
            module_bays,
            module_types,
        )

        assert row["status"] == "Installed"
        assert not row.get("can_replace")
        assert not row.get("can_update_serial")

    def test_librenms_dash_serial_with_empty_installed_gives_installed(self):
        """LibreNMS serial '-' normalizes to empty; both empty -> Installed, not mismatch."""
        view = self._view()
        _installed, module_bays, module_types = self._make_installed_rows("")

        row = view._build_row(
            self._make_item(serial="-"),
            {},
            module_bays,
            module_types,
        )

        assert row["status"] == "Installed"
        assert "row_class" not in row
        assert not row.get("can_update_serial")

    def test_librenms_dash_serial_with_real_installed_gives_installed(self):
        """LibreNMS serial '-' normalizes to empty; only NetBox has serial -> no mismatch."""
        view = self._view()
        _installed, module_bays, module_types = self._make_installed_rows("REAL123")

        row = view._build_row(
            self._make_item(serial="-"),
            {},
            module_bays,
            module_types,
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
        """Verify serial_matches_device does not promote an item below a container to chassis-level matching."""
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
    """Verify positional fallback only matches bay names that suit the hardware class."""

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
        """Verify inherited bays at the wrong hierarchy level do not produce mapping suggestions."""
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
        """Verify a letter trail maps Slot A to CPM A even when the prefixes differ."""
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
    """Verify description-based regex suggestions target existing bays when item names lack position data."""

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
        """Verify description fallback works for module items whose names contain only model data."""
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
        """Verify a fan description's trailing number suggests the matching fan tray bay."""
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
        """Verify description-trail fallback stays off when the description equals the authoritative name."""
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
        """Verify description-trail fallback does not map a fan to a line-card bay."""
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


@pytest.mark.django_db
class TestBuildRowModelWarning:
    """`_build_row` populates `model_warning` for No Bay / No Type rows."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        v = object.__new__(BaseModuleTableView)
        v._device_manufacturer = None
        return v

    def _make_rows(self, *, bay_names=(), model_names=()):
        """Create real device-level bays and module types for one row-building test."""
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type

        device = make_device_with_module_bays("build-row-model-warning", bay_names)
        module_bays = {bay.name: bay for bay in device.modulebays.filter(module__isnull=True)}
        module_types = {
            model: make_module_type(model, manufacturer=device.device_type.manufacturer) for model in model_names
        }
        return device, module_bays, module_types

    def test_no_bay_row_gets_model_warning(self):
        view = self._view()
        _device, module_bays, module_types = self._make_rows(
            bay_names=("Slot 1",),
            model_names=("ASR-FAN",),
        )
        item = {"entPhysicalName": "0/FT0", "entPhysicalClass": "fan", "entPhysicalModelName": "ASR-FAN"}
        row = view._build_row(item, {}, module_bays, module_types)
        assert row["status"] == "No Bay"
        assert "model_warning" in row
        assert row["model_warning"], "expected non-empty hint"

    def test_no_type_row_gets_model_warning(self):
        view = self._view()
        _device, module_bays, _module_types = self._make_rows(bay_names=("Slot 1",))
        item = {
            "entPhysicalName": "Slot 1",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "UNKNOWN-MODEL",
        }
        row = view._build_row(item, {}, module_bays, {})
        assert row["status"] == "No Type"
        assert "UNKNOWN-MODEL" in row.get("model_warning", "")

    def test_no_type_row_carries_module_type_create_prefill(self):
        """Verify a "No Type" row includes data for the NetBox module-type creation form."""
        view = self._view()
        device, module_bays, _module_types = self._make_rows(bay_names=("Slot 1",))
        manufacturer = device.device_type.manufacturer
        item = {
            "entPhysicalName": "Slot 1",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "X2-10GB-LR",
            "entPhysicalDescr": "10Gbase-LR",
        }
        row = view._build_row(item, {}, module_bays, {}, manufacturer=manufacturer)
        assert row["status"] == "No Type"
        create = row.get("module_type_create")
        assert create is not None
        assert create["model"] == "X2-10GB-LR"
        assert create["part_number"] == "X2-10GB-LR"
        assert create["manufacturer"] == manufacturer.pk
        assert create["description"] == "10Gbase-LR"

    def test_matched_row_has_no_model_warning(self):
        view = self._view()
        _device, module_bays, module_types = self._make_rows(
            bay_names=("Slot 1",),
            model_names=("M",),
        )
        item = {"entPhysicalName": "Slot 1", "entPhysicalClass": "module", "entPhysicalModelName": "M"}
        row = view._build_row(item, {}, module_bays, module_types)
        assert row["status"] == "Matched"
        assert "model_warning" not in row

    def test_no_bay_row_carries_model_suggestion_when_trailing_number_matches(self):
        """Verify a "No Bay" row includes a mapping suggestion when its number matches a bay."""
        view = self._view()
        _device, module_bays, module_types = self._make_rows(
            bay_names=("Slot 0",),
            model_names=("X",),
        )
        item = {"entPhysicalName": "0/0", "entPhysicalClass": "module", "entPhysicalModelName": "X"}
        row = view._build_row(item, {}, module_bays, module_types)
        assert row["status"] == "No Bay"
        sug = row.get("model_suggestion")
        assert sug is not None
        assert sug["librenms_name"] == r"^0/(\d+)$"
        assert sug["netbox_bay_name"] == r"Slot \1"

    def test_scope_uninstalled_no_bay_row_recommends_install_parent(self):
        """Verify an empty scope from an uninstalled ancestor recommends installing the parent module."""
        view = self._view()
        _device, module_bays, module_types = self._make_rows(model_names=("SFP-X",))
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-X",
        }
        row = view._build_row(item, {}, module_bays, module_types, scope_uninstalled=True)
        assert row["status"] == "No Bay"
        assert "install the parent module first" in row.get("model_warning", "").lower()

    def test_no_bay_empty_parent_bays_sets_no_bay_reason(self):
        """Verify an installed parent without bay templates sets no_bay_reason to empty_parent_bays."""
        view = self._view()
        _device, module_bays, module_types = self._make_rows(model_names=("SFP-10G-SR",))
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-10G-SR",
        }
        # scope_empty_installed_bays=True: installed parent has no bay templates
        row = view._build_row(
            item,
            {},
            module_bays,
            module_types,
            scope_empty_installed_bays=True,
        )
        assert row["status"] == "No Bay"
        assert row.get("no_bay_reason") == "empty_parent_bays"

    def test_no_bay_empty_parent_bays_through_intermediate_container(self):
        """Verify preserved scope still records an installed parent's missing bay templates."""
        view = self._view()
        _device, module_bays, module_types = self._make_rows(model_names=("SFP-10G-SR",))
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-10G-SR",
        }
        row = view._build_row(
            item,
            {},
            module_bays,
            module_types,
            scope_preserved=True,
            scope_empty_installed_bays=True,
        )
        assert row["status"] == "No Bay"
        assert row.get("no_bay_reason") == "empty_parent_bays"

    def test_no_bay_port_child_uses_interface_child_reason(self):
        """Port-class child rows under empty installed-parent scope should not be tagged as missing bay templates."""
        view = self._view()
        _device, module_bays, module_types = self._make_rows(model_names=("SFP-10G-SR",))
        item = {
            "entPhysicalName": "Ethernet1/1",
            "entPhysicalClass": "port",
            "entPhysicalModelName": "SFP-10G-SR",
        }
        row = view._build_row(
            item,
            {},
            module_bays,
            module_types,
            scope_empty_installed_bays=True,
        )
        assert row["status"] == "No Bay"
        assert row.get("no_bay_reason") == "interface_child"
        assert "matching child bay is missing in netbox" in row.get("model_warning", "").lower()

    def test_port_row_sets_can_install_and_interface_hint_when_bay_matches(self):
        """Matched port rows expose install action and preserve best interface label hint."""
        view = self._view()
        _device, module_bays, module_types = self._make_rows(
            bay_names=("SFP 1",),
            model_names=("SFP-10G-SR",),
        )

        item = {
            "entPhysicalName": "SFP 1",
            "entPhysicalClass": "port",
            "entPhysicalModelName": "SFP-10G-SR",
            "_librenms_ifname": "Te1/1/1",
            "_librenms_ifdescr": "TenGigabitEthernet1/1/1",
            "_librenms_port_id": 1234,
        }

        row = view._build_row(item, {}, module_bays, module_types)

        assert row["can_install"] is True
        assert row["interface_name_hint"] == "Te1/1/1"
        assert row["librenms_port_id"] == 1234
        assert row["librenms_ifname"] == "Te1/1/1"
        assert row["librenms_ifdescr"] == "TenGigabitEthernet1/1/1"

    def test_no_bay_default_scope_empty_flag_does_not_set_reason(self):
        """Without scope_empty_installed_bays, plain empty scope gives no reason tag."""
        view = self._view()
        _device, module_bays, module_types = self._make_rows(model_names=("SFP-10G-SR",))
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-10G-SR",
        }
        # Default scope_empty_installed_bays=False could be an unmatched ancestor.
        row = view._build_row(item, {}, module_bays, module_types)
        assert row["status"] == "No Bay"
        assert "no_bay_reason" not in row

    def test_no_bay_with_bays_in_scope_does_not_set_no_bay_reason(self):
        """When module_bays is non-empty (just no match), no_bay_reason is absent."""
        view = self._view()
        _device, module_bays, module_types = self._make_rows(
            bay_names=("Slot 1",),
            model_names=("X",),
        )
        item = {
            "entPhysicalName": "0/5",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "X",
        }
        row = view._build_row(item, {}, module_bays, module_types)
        assert row["status"] == "No Bay"
        assert "no_bay_reason" not in row

    def test_no_bay_scope_uninstalled_does_not_set_no_bay_reason(self):
        """scope_uninstalled=True is a different root cause; no_bay_reason absent."""
        view = self._view()
        _device, module_bays, module_types = self._make_rows(model_names=("SFP-X",))
        item = {
            "entPhysicalName": "TenGigE0/0/0/0",
            "entPhysicalClass": "module",
            "entPhysicalModelName": "SFP-X",
        }
        row = view._build_row(item, {}, module_bays, module_types, scope_uninstalled=True)
        assert row["status"] == "No Bay"
        assert "no_bay_reason" not in row


@pytest.mark.django_db
class TestModelIncompleteFlag:
    """Verify the real inventory path flags incomplete installed module types."""

    @staticmethod
    def _row(rows, name):
        return next(row for row in rows if row["name"] == name)

    def test_first_missing_child_bay_flags_the_installed_module_type(self, settings, librenms_server):
        """The first qualifying child supplies all incomplete-model metadata."""
        from netbox_librenms_plugin.tests.conftest import (
            install_module,
            make_device_with_module_bays,
            make_module_type,
        )

        device = make_device_with_module_bays("model-incomplete", ["Slot 1"])
        installed_module = install_module(device, "Slot 1", "PARENT-MODULE", serial="PARENT-SERIAL")
        make_module_type("CHILD-MODULE-A")
        make_module_type("CHILD-MODULE-B")
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Slot 1",
                "entPhysicalModelName": "PARENT-MODULE",
                "entPhysicalClass": "module",
                "entPhysicalDescr": "Installed parent module",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "PARENT-SERIAL",
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalName": "Daughter Bay 1",
                "entPhysicalModelName": "CHILD-MODULE-A",
                "entPhysicalClass": "module",
                "entPhysicalDescr": "First daughter module",
                "entPhysicalContainedIn": 1,
                "entPhysicalSerialNum": "CHILD-SERIAL-A",
                "entPhysicalParentRelPos": 1,
            },
            {
                "entPhysicalIndex": 3,
                "entPhysicalName": "Backup Socket B",
                "entPhysicalModelName": "CHILD-MODULE-B",
                "entPhysicalClass": "module",
                "entPhysicalDescr": "Second daughter module",
                "entPhysicalContainedIn": 1,
                "entPhysicalSerialNum": "CHILD-SERIAL-B",
                "entPhysicalParentRelPos": 2,
            },
        ]

        view = _real_api_view(settings, librenms_server, librenms_id=101)
        rows = _run_build_context_real(view, inventory, device)
        parent_row = self._row(rows, "Slot 1")
        first_child_row = self._row(rows, "Daughter Bay 1")
        second_child_row = self._row(rows, "Backup Socket B")
        module_type = installed_module.module_type

        assert first_child_row["no_bay_reason"] == "empty_parent_bays"
        assert second_child_row["no_bay_reason"] == "empty_parent_bays"
        assert parent_row["model_incomplete"] is True
        assert parent_row["model_incomplete_url"] == module_type.get_absolute_url()
        assert parent_row["model_incomplete_name"] == str(module_type)
        assert parent_row["model_incomplete_target_pk"] == module_type.pk
        assert parent_row["model_incomplete_suggestion"] == {
            "name": "Daughter Bay 1",
            "position": "1",
            "label": "First daughter module",
            "librenms_name": "Daughter Bay 1",
            "librenms_class": "module",
        }

    def test_nonqualifying_child_does_not_flag_the_installed_module_type(self, settings, librenms_server):
        """A child without the missing-parent-bays reason leaves the model unflagged."""
        from netbox_librenms_plugin.tests.conftest import (
            install_module,
            make_device_with_module_bays,
            make_module_type,
        )

        device = make_device_with_module_bays("model-complete-reason", ["Slot 1"])
        install_module(device, "Slot 1", "PARENT-MODULE", serial="PARENT-SERIAL")
        make_module_type("CHILD-OPTIC")
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Slot 1",
                "entPhysicalModelName": "PARENT-MODULE",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "PARENT-SERIAL",
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalName": "Ethernet1/1",
                "entPhysicalModelName": "CHILD-OPTIC",
                "entPhysicalClass": "port",
                "entPhysicalContainedIn": 1,
                "entPhysicalSerialNum": "CHILD-SERIAL",
            },
        ]

        view = _real_api_view(settings, librenms_server, librenms_id=102)
        rows = _run_build_context_real(view, inventory, device)
        parent_row = self._row(rows, "Slot 1")
        child_row = self._row(rows, "Ethernet1/1")

        # Port children use a separate reason because they need interface bay handling.
        assert child_row["no_bay_reason"] == "interface_child"
        assert not any(key.startswith("model_incomplete") for key in parent_row)

    def test_child_bay_templates_leave_the_installed_module_type_unflagged(self, settings, librenms_server):
        """An installed module type with child bay templates stays unflagged."""
        from netbox_librenms_plugin.tests.conftest import (
            install_module,
            make_device_with_module_bays,
            make_module_type,
        )

        device = make_device_with_module_bays("model-with-child-bays", ["Slot 1"])
        install_module(
            device,
            "Slot 1",
            "PARENT-MODULE",
            serial="PARENT-SERIAL",
            child_bays=["Child Bay 1"],
        )
        make_module_type("CHILD-MODULE")
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Slot 1",
                "entPhysicalModelName": "PARENT-MODULE",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "PARENT-SERIAL",
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalName": "Missing Bay 2",
                "entPhysicalModelName": "CHILD-MODULE",
                "entPhysicalClass": "module",
                "entPhysicalContainedIn": 1,
                "entPhysicalSerialNum": "CHILD-SERIAL",
            },
        ]

        view = _real_api_view(settings, librenms_server, librenms_id=103)
        rows = _run_build_context_real(view, inventory, device)
        parent_row = self._row(rows, "Slot 1")
        child_row = self._row(rows, "Missing Bay 2")

        assert child_row["status"] == "No Bay"
        assert "no_bay_reason" not in child_row
        assert not any(key.startswith("model_incomplete") for key in parent_row)


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


@pytest.mark.django_db
class TestDeviceTypeIncompleteFlag:
    """Verify the real inventory path flags incomplete device types."""

    @staticmethod
    def _row(rows, name):
        return next(row for row in rows if row["name"] == name)

    def test_fan_without_a_candidate_bay_flags_the_device_type(self, settings, librenms_server):
        """A fan that cannot map to a slot bay supplies all device-type metadata."""
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type

        device = make_device_with_module_bays("device-type-incomplete", ["Slot 0"])
        make_module_type("FAN-MODULE")
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "0/FT0",
                "entPhysicalModelName": "FAN-MODULE",
                "entPhysicalClass": "fan",
                "entPhysicalDescr": "Primary fan tray",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "FAN-SERIAL",
            }
        ]

        view = _real_api_view(settings, librenms_server, librenms_id=201)
        rows = _run_build_context_real(view, inventory, device)
        row = self._row(rows, "0/FT0")
        device_type = device.device_type

        assert row["status"] == "No Bay"
        assert "model_suggestion" not in row
        assert row["device_type_incomplete"] is True
        assert row["device_type_incomplete_url"] == device_type.get_absolute_url()
        assert row["device_type_incomplete_name"] == str(device_type)
        assert row["device_type_incomplete_target_pk"] == device_type.pk
        assert row["device_type_incomplete_suggestion"] == {
            "name": "0/FT0",
            "position": "0",
            "label": "Primary fan tray",
            "librenms_name": "0/FT0",
            "librenms_class": "fan",
        }

    def test_fan_bay_suggestion_leaves_the_device_type_unflagged(self, settings, librenms_server):
        """A fan-named bay yields a mapping suggestion instead of an incomplete flag."""
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type

        device = make_device_with_module_bays("device-type-suggestion", ["Fan Tray 0"])
        make_module_type("FAN-MODULE")
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "0/FT0",
                "entPhysicalModelName": "FAN-MODULE",
                "entPhysicalClass": "fan",
                "entPhysicalDescr": "Primary fan tray",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "FAN-SERIAL",
            }
        ]

        view = _real_api_view(settings, librenms_server, librenms_id=202)
        rows = _run_build_context_real(view, inventory, device)
        row = self._row(rows, "0/FT0")

        assert row["status"] == "No Bay"
        assert row["model_suggestion"]["netbox_bay_name"] == r"Fan Tray \1"
        assert not any(key.startswith("device_type_incomplete") for key in row)

    def test_installed_fan_leaves_the_device_type_unflagged(self, settings, librenms_server):
        """An installed inventory row does not mark its device type incomplete."""
        from netbox_librenms_plugin.tests.conftest import install_module, make_device_with_module_bays

        device = make_device_with_module_bays("device-type-installed", ["Fan Tray 0"])
        install_module(device, "Fan Tray 0", "FAN-MODULE", serial="FAN-SERIAL")
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalName": "Fan Tray 0",
                "entPhysicalModelName": "FAN-MODULE",
                "entPhysicalClass": "fan",
                "entPhysicalDescr": "Primary fan tray",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "FAN-SERIAL",
            }
        ]

        view = _real_api_view(settings, librenms_server, librenms_id=203)
        rows = _run_build_context_real(view, inventory, device)
        row = self._row(rows, "Fan Tray 0")

        assert row["status"] == "Installed"
        assert not any(key.startswith("device_type_incomplete") for key in row)


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
    """Verify an integrated container passes its bay scope to children without marking the scope as preserved."""

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


@pytest.mark.django_db
class TestGetContextDataOOBCacheFingerprint:
    """get_context_data must invalidate cached inventory when the linked OOB controller changes (re-link / unlink), not only when the main id changes."""

    @pytest.fixture
    def server_keys(self, settings, librenms_server):
        return _two_server_keys(settings, librenms_server, "fingerprint")

    def test_invalidates_when_oob_relinked(self, server_keys):
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import bind_and_call, make_request
        from netbox_librenms_plugin.utils import set_librenms_oob
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-cache-oob-relinked", server_key)
        set_librenms_oob(device, 999, server_key, oob_type="idrac9")
        device.save(update_fields=["custom_field_data"])
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key, oob_librenms_id=998)
        request = make_request("get", {"server_key": server_key})

        context = bind_and_call(view, request, "get_context_data", obj=device)

        assert cache.get(cache_key) is None
        assert context["table"] is None
        assert context["server_key"] == server_key

    def test_invalidates_when_oob_unlinked(self, server_keys):
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import bind_and_call, make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-cache-oob-unlinked", server_key)
        view = DeviceModuleTableView()
        cache_key, _ = _seed_snapshot(view, device, server_key, oob_librenms_id=999)
        request = make_request("get", {"server_key": server_key})

        context = bind_and_call(view, request, "get_context_data", obj=device)

        assert cache.get(cache_key) is None
        assert context["table"] is None
        assert context["server_key"] == server_key

    def test_invalidates_when_main_id_poisoned_bool(self, server_keys):
        """Verify a Boolean main device ID invalidates the cache instead of matching integer 1."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import bind_and_call, make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = make_device("module-cache-poisoned-bool")
        api = LibreNMSAPI(server_key=server_key)
        # The device-id cache path returns its value verbatim, which is why the Boolean guard exists.
        cache.set(api._get_cache_key(device, server_key=server_key), True)
        view = DeviceModuleTableView()
        cache_key = view.get_cache_key(device, "inventory", server_key=server_key)
        cache.set(cache_key, {"inventory": [], "librenms_id": 1, "oob_librenms_id": None})
        request = make_request("get", {"server_key": server_key})

        context = bind_and_call(view, request, "get_context_data", obj=device)

        assert cache.get(cache_key) is None
        assert context["table"] is None
        assert context["server_key"] == server_key

    def test_keeps_cache_when_main_id_stored_as_string(self, server_keys):
        """Verify a string main device ID matches the equivalent cached integer ID."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import bind_and_call, make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-cache-string-main-id", server_key)
        view = DeviceModuleTableView()
        cache_key, payload = _seed_snapshot(view, device, server_key)
        device.custom_field_data["librenms_id"][server_key] = "777"
        device.save(update_fields=["custom_field_data"])
        cache.set(cache_key, payload)
        request = make_request("get", {"server_key": server_key})

        context = bind_and_call(view, request, "get_context_data", obj=device)

        assert context["table"] is not None
        assert context["server_key"] == server_key
        assert cache.get(cache_key) == payload

    def test_invalidates_when_oob_link_corrupt(self, server_keys):
        """Verify a corrupt linked OOB ID invalidates the cache instead of matching a missing OOB link."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import bind_and_call, make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-cache-corrupt-oob", server_key)
        view = DeviceModuleTableView()
        cache_key, stale_payload = _seed_snapshot(view, device, server_key)
        # The custom field is user-editable through the NetBox UI and API, so a corrupt value is real state.
        device.custom_field_data["librenms_id"][server_key] = {
            "id": 777,
            "oob": {"id": "garbage", "type": "oob"},
        }
        device.save(update_fields=["custom_field_data"])
        cache.set(cache_key, stale_payload)
        request = make_request("get", {"server_key": server_key})

        context = bind_and_call(view, request, "get_context_data", obj=device)

        assert cache.get(cache_key) is None
        assert context["table"] is None
        assert context["server_key"] == server_key

    def test_keeps_cache_when_oob_unchanged(self, server_keys):
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import bind_and_call, make_request
        from netbox_librenms_plugin.utils import set_librenms_oob
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        server_key, _ = server_keys
        device = _mapped_device("module-cache-oob-unchanged", server_key)
        set_librenms_oob(device, 999, server_key, oob_type="idrac9")
        device.save(update_fields=["custom_field_data"])
        view = DeviceModuleTableView()
        cache_key, payload = _seed_snapshot(view, device, server_key, oob_librenms_id=999)
        request = make_request("get", {"server_key": server_key})

        context = bind_and_call(view, request, "get_context_data", obj=device)

        # The unchanged fingerprint path must rebuild from the cached inventory snapshot. It
        # forwards the resolved sync device and server key, matching post()'s call shape.
        assert context["table"] is not None
        assert context["object"].pk == device.pk
        assert context["server_key"] == server_key
        # Preserve the cache when the OOB linkage is unchanged.
        assert cache.get(cache_key) == payload

    def test_get_context_data_rebinds_to_request_server_key(self, server_keys):
        """The GET render must rebind to the active server from the request query so the cache read keys on the same server post() wrote under. Otherwise a non-default-server tab cache-misses and the OOB guard does nothing."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import bind_and_call, make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        bound_key, requested_key = server_keys
        device = _mapped_device("module-cache-get-rebind", requested_key)
        view = DeviceModuleTableView()
        view._librenms_api = LibreNMSAPI(server_key=bound_key)
        request = make_request("get", {"server_key": requested_key})
        requested_cache_key = view.get_cache_key(device, "inventory", server_key=requested_key)
        assert cache.get(requested_cache_key) is None

        context = bind_and_call(view, request, "get_context_data", obj=device)

        assert view.librenms_api.server_key == requested_key
        assert context["table"] is None
        assert context["server_key"] == requested_key


@pytest.mark.django_db
class TestInterfacePortIdActiveServerScope:
    """Verify interface port IDs use the active server key during module verification."""

    @pytest.fixture(autouse=True)
    def _configure_default_server(self, settings):
        """Configure the bound API key without a suite-wide configuration mock."""
        from copy import deepcopy

        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {
            "default": {
                "librenms_url": "https://default.example.com",
                "api_token": "test-token",
            },
            "server2": {
                "librenms_url": "https://server2.example.com",
                "api_token": "test-token",
            },
        }
        settings.PLUGINS_CONFIG = plugin_config

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
