"""
Tests for module sync views and BaseModuleTableView bay matching logic.

Covers: InstallModuleView/InstallBranchView wiring, branch collection, cycle guards,
bay matching by name/mapping/position, serial comparison, status determination,
and depth tracking.  inventory-rebased branch only.
"""

import re
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@contextmanager
def _patch_build_row_deps(view, match_bay_return=None):
    """Patch all utility imports used by _build_row to isolate bay/type matching tests."""
    _utils = "netbox_librenms_plugin.utils"
    with (
        patch.object(view, "_match_module_bay", return_value=match_bay_return),
        patch(f"{_utils}.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)),
        patch(f"{_utils}.has_nested_name_conflict", return_value=False),
    ):
        yield


def _make_install_branch_view():
    from netbox_librenms_plugin.views.sync.modules import InstallBranchView

    view = object.__new__(InstallBranchView)
    view._librenms_api = None
    return view


class TestInstallBranchViewCollectBranch:
    """_collect_branch correctly collects parent + children depth-first."""

    def _make_inventory(self, items):
        """Helper to build a list of inventory dicts."""
        return items

    def test_collect_parent_with_model(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "WS-C4500X", "entPhysicalContainedIn": 0},
        ]
        result = view._collect_branch(1, inventory)
        assert len(result) == 1
        assert result[0]["entPhysicalIndex"] == 1

    def test_collect_parent_without_model_excluded(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "", "entPhysicalContainedIn": 0},
        ]
        result = view._collect_branch(1, inventory)
        assert result == []

    def test_collect_children_included_with_models(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "CHILD-A", "entPhysicalContainedIn": 1},
            {"entPhysicalIndex": 3, "entPhysicalModelName": "CHILD-B", "entPhysicalContainedIn": 1},
        ]
        result = view._collect_branch(1, inventory)
        indices = [item["entPhysicalIndex"] for item in result]
        assert 1 in indices
        assert 2 in indices
        assert 3 in indices

    def test_parent_comes_before_children(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "CHILD", "entPhysicalContainedIn": 1},
        ]
        result = view._collect_branch(1, inventory)
        indices = [item["entPhysicalIndex"] for item in result]
        assert indices.index(1) < indices.index(2)

    def test_deep_nesting_collected(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "ROOT", "entPhysicalContainedIn": 0},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "MID", "entPhysicalContainedIn": 1},
            {"entPhysicalIndex": 3, "entPhysicalModelName": "LEAF", "entPhysicalContainedIn": 2},
        ]
        result = view._collect_branch(1, inventory)
        assert len(result) == 3

    def test_unknown_parent_returns_empty(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "ITEM", "entPhysicalContainedIn": 0},
        ]
        result = view._collect_branch(999, inventory)
        assert result == []


class TestInstallBranchViewCollectChildrenCycleGuard:
    """_collect_children must not loop on cyclic entPhysicalContainedIn links."""

    def test_cycle_does_not_cause_infinite_recursion(self):
        view = _make_install_branch_view()
        # A ↔ B cycle (A contains B, B contains A)
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "A", "entPhysicalContainedIn": 2},
            {"entPhysicalIndex": 2, "entPhysicalModelName": "B", "entPhysicalContainedIn": 1},
        ]
        items = []
        # Should terminate without RecursionError
        view._collect_children(1, inventory, items, visited={1})

    def test_self_reference_does_not_loop(self):
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 5, "entPhysicalModelName": "SELF", "entPhysicalContainedIn": 5},
        ]
        items = []
        view._collect_children(5, inventory, items, visited={5})
        # No infinite recursion — length may be 0 (self is excluded by visited)
        assert len(items) == 0


@pytest.mark.django_db
class TestGetModuleTypesIndexed:
    """get_module_types_indexed keys real ModuleTypes by model/part-number with ModuleTypeMappings applied."""

    @staticmethod
    def _mfr(name):
        from dcim.models import Manufacturer

        return Manufacturer.objects.create(name=name, slug=name.lower())

    @staticmethod
    def _mtype(mfr, model, part_number=""):
        from dcim.models import ModuleType

        return ModuleType.objects.create(manufacturer=mfr, model=model, part_number=part_number)

    @staticmethod
    def _mapping(librenms_model, netbox_module_type, manufacturer=None):
        from netbox_librenms_plugin.models import ModuleTypeMapping

        return ModuleTypeMapping.objects.create(
            librenms_model=librenms_model, netbox_module_type=netbox_module_type, manufacturer=manufacturer
        )

    def test_indexes_by_model_and_part_number(self):
        """ModuleTypes are keyed by model and part_number, and a global mapping adds its librenms_model key."""
        from netbox_librenms_plugin.utils import get_module_types_indexed

        mfr = self._mfr("IdxA")
        mt1 = self._mtype(mfr, "WS-X4748-IDX", "ALT-PART-4748-IDX")
        mt2 = self._mtype(mfr, "WS-X4516-IDX", "WS-X4516-IDX")  # part_number == model → single key
        self._mapping("libre-model-a-idx", mt1)

        result = get_module_types_indexed()

        assert result["WS-X4748-IDX"] == mt1
        assert result["ALT-PART-4748-IDX"] == mt1
        assert result["WS-X4516-IDX"] == mt2
        assert result["libre-model-a-idx"] == mt1

    def test_mapping_overrides_ambiguous_base_key(self):
        """A unique ModuleTypeMapping survives even though the colliding base model key is dropped."""
        from netbox_librenms_plugin.utils import get_module_types_indexed

        # Two ModuleTypes (distinct manufacturers, since (manufacturer, model) is unique) share a
        # model name → that base key collides and is dropped from the index.
        mt1 = self._mtype(self._mfr("AmbA"), "SFP-1G-LX-IDX")
        self._mtype(self._mfr("AmbB"), "SFP-1G-LX-IDX")
        self._mapping("SFP-1G-LX-EXPLICIT-IDX", mt1)

        result = get_module_types_indexed()

        assert "SFP-1G-LX-IDX" not in result  # ambiguous base key dropped
        assert result["SFP-1G-LX-EXPLICIT-IDX"] == mt1  # explicit mapping still resolves

    def test_manufacturer_scoped_mapping_kept_separate_from_base_index(self):
        """A manufacturer-scoped mapping lives only in mfr_mappings, never the global key space."""
        from netbox_librenms_plugin.utils import get_module_types_indexed

        mfr = self._mfr("JuniperIdx")
        mt = self._mtype(mfr, "QSFP-100G-LR4-JUNIPER-IDX")
        self._mapping("1F3QAA-IDX", mt, manufacturer=mfr)

        result = get_module_types_indexed()

        assert "1F3QAA-IDX" not in result  # vendor-scoped key must not leak globally
        assert result.mfr_mappings[(mfr.pk, "1F3QAA-IDX")] == mt


class TestResolveModuleTypeManufacturerScope:
    """resolve_module_type must prefer manufacturer-scoped mappings when available."""

    def _index(self, base=None, mfr=None):
        from netbox_librenms_plugin.utils import _ModuleTypeIndex

        return _ModuleTypeIndex(base or {}, mfr_mappings=mfr or {})

    def test_manufacturer_scoped_wins_over_global(self):
        from netbox_librenms_plugin.utils import resolve_module_type

        global_mt = MagicMock(name="global_mt")
        scoped_mt = MagicMock(name="scoped_mt")
        idx = self._index(base={"X": global_mt}, mfr={(7, "X"): scoped_mt})
        manufacturer = MagicMock(pk=7)

        assert resolve_module_type("X", idx, manufacturer=manufacturer) is scoped_mt

    def test_falls_back_to_global_when_no_scoped_match(self):
        from netbox_librenms_plugin.utils import resolve_module_type

        global_mt = MagicMock(name="global_mt")
        idx = self._index(base={"X": global_mt}, mfr={(7, "OTHER"): MagicMock()})
        manufacturer = MagicMock(pk=7)

        assert resolve_module_type("X", idx, manufacturer=manufacturer) is global_mt

    def test_other_vendor_does_not_see_scoped_mapping(self):
        from netbox_librenms_plugin.utils import resolve_module_type

        scoped_mt = MagicMock(name="scoped_mt")
        idx = self._index(base={}, mfr={(7, "X"): scoped_mt})
        other_mfr = MagicMock(pk=99)

        with patch("netbox_librenms_plugin.utils.apply_normalization_rules", side_effect=lambda v, *a, **k: v):
            assert resolve_module_type("X", idx, manufacturer=other_mfr) is None

    def test_no_manufacturer_falls_back_to_global_only(self):
        from netbox_librenms_plugin.utils import resolve_module_type

        scoped_mt = MagicMock(name="scoped_mt")
        idx = self._index(base={}, mfr={(7, "X"): scoped_mt})

        with patch("netbox_librenms_plugin.utils.apply_normalization_rules", side_effect=lambda v, *a, **k: v):
            assert resolve_module_type("X", idx, manufacturer=None) is None


class TestInstallModuleViewWiring:
    """InstallModuleView must have correct mixins and attributes."""

    def test_has_librenms_permission_mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        assert LibreNMSPermissionMixin in InstallModuleView.__mro__

    def test_has_netbox_object_permission_mixin(self):
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        assert NetBoxObjectPermissionMixin in InstallModuleView.__mro__

    def test_install_module_view_not_in_base(self):
        """InstallModuleView is importable from the public sync module, not views/base."""
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        assert callable(InstallModuleView), "InstallModuleView must be a callable class"
        assert InstallModuleView.__module__ == "netbox_librenms_plugin.views.sync.modules", (
            "InstallModuleView must be defined in views/sync/modules.py, not views/base/"
        )

    def test_has_librenms_api_mixin(self):
        """InstallModuleView needs LibreNMSAPIMixin so a blank posted server_key can fall back to the client key (like InstallBranchView)."""
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        assert LibreNMSAPIMixin in InstallModuleView.__mro__


class TestInstallBranchViewWiring:
    """InstallBranchView must have CacheMixin for cache key generation."""

    def test_has_cache_mixin(self):
        from netbox_librenms_plugin.views.mixins import CacheMixin
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        assert CacheMixin in InstallBranchView.__mro__

    def test_has_netbox_object_permission_mixin(self):
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        assert NetBoxObjectPermissionMixin in InstallBranchView.__mro__


# ---------------------------------------------------------------------------
# Helper: build a BaseModuleTableView instance without __init__
# ---------------------------------------------------------------------------


def _make_base_view():
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    view = object.__new__(BaseModuleTableView)
    view._device_manufacturer = None
    return view


_bay_counter = 0


def _bay(name, installed_module=None, pk=None):
    """Quick MagicMock module bay."""
    global _bay_counter
    _bay_counter += 1
    bay = MagicMock()
    bay.name = name
    bay.pk = pk or _bay_counter
    bay.installed_module = installed_module
    bay.get_absolute_url.return_value = f"/dcim/module-bays/{bay.pk}/"
    return bay


def _module(serial="SN001", module_type_id=1):
    mod = MagicMock()
    mod.serial = serial
    mod.module_type_id = module_type_id
    mod.get_absolute_url.return_value = "/dcim/modules/1/"
    return mod


# ---------------------------------------------------------------------------
# _determine_status
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Serial comparison inside _build_row
# ---------------------------------------------------------------------------


class TestBuildRowSerialComparison:
    """_build_row sets 'Installed' or 'Serial Mismatch' based on installed module serial."""

    def _make_item(self, model_name, serial):
        return {
            "entPhysicalModelName": model_name,
            "entPhysicalSerialNum": serial,
            "entPhysicalName": model_name,
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalIndex": 10,
            "entPhysicalContainedIn": 0,
        }

    def _make_matched_type(self, model="WS-X4748"):
        mt = MagicMock()
        mt.model = model
        mt.pk = 1
        mt.get_absolute_url.return_value = "/dcim/module-types/1/"
        # Make uses-module-path/token checks return False so badges don't appear
        mt.interfacetemplates = MagicMock()
        mt.interfacetemplates.all.return_value = []
        return mt

    def test_matching_serial_gives_installed_status(self):
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN-ABC-123")
        mt = self._make_matched_type()
        installed = _module(serial="SN-ABC-123")
        bay = _bay("Slot 1", installed_module=installed)

        with _patch_build_row_deps(view, match_bay_return=bay):
            row = view._build_row(item, {10: item}, {"Slot 1": bay}, {"WS-X4748": mt}, depth=0)

        assert row["status"] == "Installed"
        assert "row_class" not in row

    def test_serial_mismatch_gives_danger_status(self):
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN-NEW-999")
        mt = self._make_matched_type()
        installed = _module(serial="SN-OLD-111")
        bay = _bay("Slot 1", installed_module=installed)

        with _patch_build_row_deps(view, match_bay_return=bay):
            row = view._build_row(item, {10: item}, {"Slot 1": bay}, {"WS-X4748": mt}, depth=0)

        assert row["status"] == "Serial Mismatch"
        assert "row_class" not in row

    def test_no_bay_gives_no_bay_status(self):
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN1")
        mt = self._make_matched_type()

        with _patch_build_row_deps(view, match_bay_return=None):
            row = view._build_row(item, {10: item}, {}, {"WS-X4748": mt}, depth=0)

        assert row["status"] == "No Bay"

    def test_no_type_gives_no_type_status(self):
        view = _make_base_view()
        item = self._make_item("UNKNOWN-MODEL", "SN1")
        bay = _bay("Slot 1")

        with _patch_build_row_deps(view, match_bay_return=bay):
            row = view._build_row(item, {10: item}, {"Slot 1": bay}, {}, depth=0)

        assert row["status"] == "No Type"

    def test_can_install_set_when_bay_free_and_type_matched(self):
        """can_install=True only when bay exists, type matched, and bay is empty."""
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN1")
        mt = self._make_matched_type()
        bay = _bay("Slot 1", installed_module=None)
        bay.installed_module = None

        with _patch_build_row_deps(view, match_bay_return=bay):
            row = view._build_row(item, {10: item}, {"Slot 1": bay}, {"WS-X4748": mt}, depth=0)

        assert row["can_install"] is True

    def test_can_install_false_when_bay_occupied(self):
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN1")
        mt = self._make_matched_type()
        installed = _module(serial="SN1")
        bay = _bay("Slot 1", installed_module=installed)

        with _patch_build_row_deps(view, match_bay_return=bay):
            row = view._build_row(item, {10: item}, {"Slot 1": bay}, {"WS-X4748": mt}, depth=0)

        assert row["can_install"] is False

    def test_librenms_dash_serial_with_empty_installed_gives_installed(self):
        """LibreNMS serial '-' normalizes to empty; both empty -> Installed, not mismatch."""
        view = _make_base_view()
        item = self._make_item("WS-X4748", "-")
        mt = self._make_matched_type()
        installed = _module(serial="")
        bay = _bay("Slot 1", installed_module=installed)

        with _patch_build_row_deps(view, match_bay_return=bay):
            row = view._build_row(item, {10: item}, {"Slot 1": bay}, {"WS-X4748": mt}, depth=0)

        assert row["status"] == "Installed"
        assert "row_class" not in row

    def test_librenms_dash_serial_with_real_installed_gives_installed(self):
        """LibreNMS serial '-' normalizes to empty; only NetBox has serial -> no mismatch."""
        view = _make_base_view()
        item = self._make_item("WS-X4748", "-")
        mt = self._make_matched_type()
        installed = _module(serial="REAL123")
        bay = _bay("Slot 1", installed_module=installed)

        with _patch_build_row_deps(view, match_bay_return=bay):
            row = view._build_row(item, {10: item}, {"Slot 1": bay}, {"WS-X4748": mt}, depth=0)

        assert row["status"] == "Installed"
        assert "row_class" not in row


# ---------------------------------------------------------------------------
# Depth tracking in render_name
# ---------------------------------------------------------------------------


class TestRenderNameDepth:
    """render_name applies tree indentation based on depth."""

    def test_depth_zero_returns_plain_value(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = LibreNMSModuleTable([])
        result = table.render_name("Supervisor", {"depth": 0})
        assert "padding-left" not in str(result)
        assert "Supervisor" in str(result)

    def test_depth_one_adds_padding(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = LibreNMSModuleTable([])
        result = str(table.render_name("Line Card", {"depth": 1}))
        assert "padding-left" in result
        assert "20px" in result

    def test_depth_two_doubles_padding(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = LibreNMSModuleTable([])
        result = str(table.render_name("SFP", {"depth": 2}))
        assert "40px" in result

    def test_depth_renders_tree_prefix(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = LibreNMSModuleTable([])
        result = str(table.render_name("Port 1", {"depth": 1}))
        assert "└─" in result


# ---------------------------------------------------------------------------
# _match_bay_by_position
# ---------------------------------------------------------------------------


class TestMatchBayByPosition:
    """_match_bay_by_position resolves position-based bay names for SFPs in converters."""

    def test_matches_sfp_slot_by_sibling_order(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        # Build an inventory: parent (model) → container1 → item1, container2 → item2
        parent_item = {
            "entPhysicalIndex": 1,
            "entPhysicalModelName": "CONVERTER",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
            "entPhysicalParentRelPos": 0,
        }
        container1 = {
            "entPhysicalIndex": 2,
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 1,
        }
        container2 = {
            "entPhysicalIndex": 3,
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 2,
        }
        sfp1 = {
            "entPhysicalIndex": 4,
            "entPhysicalModelName": "SFP-10G-LR",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 2,
            "entPhysicalParentRelPos": 1,
        }
        sfp2 = {
            "entPhysicalIndex": 5,
            "entPhysicalModelName": "SFP-10G-SR",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 3,
            "entPhysicalParentRelPos": 1,
        }

        index_map = {1: parent_item, 2: container1, 3: container2, 4: sfp1, 5: sfp2}
        bays = {"SFP 1": _bay("SFP 1"), "SFP 2": _bay("SFP 2")}

        result1 = BaseModuleTableView._match_bay_by_position(sfp1, index_map, bays)
        result2 = BaseModuleTableView._match_bay_by_position(sfp2, index_map, bays)

        assert result1 is bays["SFP 1"]
        assert result2 is bays["SFP 2"]

    def test_returns_none_when_no_modelless_container(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        # Item directly under parent with model (no modelless container)
        parent = {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0}
        item = {"entPhysicalIndex": 2, "entPhysicalModelName": "CHILD", "entPhysicalContainedIn": 1}
        index_map = {1: parent, 2: item}
        bays = {"Slot 1": _bay("Slot 1")}

        result = BaseModuleTableView._match_bay_by_position(item, index_map, bays)
        assert result is None

    def test_returns_none_when_no_bays_match_pattern(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        parent = {
            "entPhysicalIndex": 1,
            "entPhysicalModelName": "M",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
            "entPhysicalParentRelPos": 0,
        }
        container = {
            "entPhysicalIndex": 2,
            "entPhysicalModelName": "",
            "entPhysicalClass": "container",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 1,
        }
        item = {
            "entPhysicalIndex": 3,
            "entPhysicalModelName": "X",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 2,
            "entPhysicalParentRelPos": 1,
        }
        index_map = {1: parent, 2: container, 3: item}
        bays = {"InterfaceA": _bay("InterfaceA")}  # no "SFP 1"/"Slot 1"/etc.

        result = BaseModuleTableView._match_bay_by_position(item, index_map, bays)
        assert result is None


class TestInterfacePortIndexExtraction:
    """Port index extraction should handle common vendor interface label formats."""

    def test_cisco_style_coordinates_prefer_last_segment(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"_librenms_ifname": "GigabitEthernet5/1/24"}

        result = BaseModuleTableView._extract_interface_port_indices(item)

        assert result
        assert result[0] == 24

    def test_juniper_zero_based_labels_include_one_based_fallback(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"_librenms_ifname": "xe-2/1/0"}

        result = BaseModuleTableView._extract_interface_port_indices(item)

        assert 1 in result

    def test_match_bay_by_interface_label_uses_zero_based_fallback(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        item = {"_librenms_ifname": "xe-2/1/0"}
        bay = _bay("SFP 1")
        bays = {"SFP 1": bay}

        result = BaseModuleTableView._match_bay_by_interface_label(item, bays)

        assert result is bay


# ---------------------------------------------------------------------------
# _match_module_bay — exact name fallback
# ---------------------------------------------------------------------------


class TestMatchModuleBayExactFallback:
    """When no ModuleBayMapping exists, exact parent/item/descr name is tried."""

    def test_exact_parent_name_match(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = _make_base_view()
        parent = {
            "entPhysicalIndex": 1,
            "entPhysicalModelName": "PARENT",
            "entPhysicalContainedIn": 0,
            "entPhysicalName": "Slot 1",
        }
        item = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Linecard A",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 1,
        }
        index_map = {1: parent, 2: item}
        bay = _bay("Slot 1")
        bays = {"Slot 1": bay}

        with patch(
            "netbox_librenms_plugin.utils.apply_normalization_rules", side_effect=lambda name, scope, **kw: name
        ):
            with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mbm:
                mock_mbm.objects.filter.return_value.first.return_value = None

                # Also patch _lookup_regex_bay_mapping to return None
                with patch.object(BaseModuleTableView, "_lookup_regex_bay_mapping", return_value=None):
                    with patch.object(BaseModuleTableView, "_match_bay_by_position", return_value=None):
                        result = view._match_module_bay(item, index_map, bays)

        assert result is bay

    def test_item_name_used_when_no_parent_name(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = _make_base_view()
        item = {
            "entPhysicalIndex": 1,
            "entPhysicalName": "Module Bay 3",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
        }
        index_map = {1: item}
        bay = _bay("Module Bay 3")
        bays = {"Module Bay 3": bay}

        with patch(
            "netbox_librenms_plugin.utils.apply_normalization_rules", side_effect=lambda name, scope, **kw: name
        ):
            with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mbm:
                mock_mbm.objects.filter.return_value.first.return_value = None
                with patch.object(BaseModuleTableView, "_lookup_regex_bay_mapping", return_value=None):
                    with patch.object(BaseModuleTableView, "_match_bay_by_position", return_value=None):
                        result = view._match_module_bay(item, index_map, bays)

        assert result is bay

    def test_returns_none_when_no_match(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = _make_base_view()
        item = {
            "entPhysicalIndex": 1,
            "entPhysicalName": "Unknown-X",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
        }
        index_map = {1: item}
        bays = {"Slot 1": _bay("Slot 1")}

        with patch(
            "netbox_librenms_plugin.utils.apply_normalization_rules", side_effect=lambda name, scope, **kw: name
        ):
            with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mbm:
                mock_mbm.objects.filter.return_value.first.return_value = None
                with patch.object(BaseModuleTableView, "_lookup_regex_bay_mapping", return_value=None):
                    with patch.object(BaseModuleTableView, "_match_bay_by_position", return_value=None):
                        result = view._match_module_bay(item, index_map, bays)

        assert result is None


# ---------------------------------------------------------------------------
# _install_single — status codes
# ---------------------------------------------------------------------------


class TestInstallSingleStatus:
    """_install_single returns the correct status dict in each path."""

    def _make_args(self):
        """Return (device, item, index_map, module_types, ModuleBay, ModuleType, Module)."""
        device = MagicMock()
        device.device_type.manufacturer = None

        item = {
            "entPhysicalIndex": 10,
            "entPhysicalModelName": "WS-X4748",
            "entPhysicalSerialNum": "SN123",
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
        }

        mt = MagicMock()
        mt.model = "WS-X4748"
        mt.pk = 1
        mt.interfacetemplates.all.return_value = []

        bay = _bay("Slot 1")
        bay.installed_module = None
        bay.module_id = None

        index_map = {10: item}
        module_types = {"WS-X4748": mt}

        ModuleBay = MagicMock()
        ModuleBay.objects.filter.return_value.select_related.return_value = [bay]
        # Support select_for_update chain used in _install_single
        locked_bay = _bay("Slot 1")
        locked_bay.installed_module = None
        locked_bay.pk = bay.pk
        locked_bay.module_id = None
        ModuleBay.objects.select_for_update.return_value.select_related.return_value.get.return_value = locked_bay
        ModuleType = MagicMock()
        Module = MagicMock()

        return device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt

    def test_returns_installed_on_success(self):
        from contextlib import contextmanager

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
        module_instance = MagicMock()
        module_instance.pk = 123
        Module.return_value = module_instance

        @contextmanager
        def noop_atomic():
            yield

        with patch("netbox_librenms_plugin.views.sync.modules.transaction.atomic", noop_atomic):
            with patch("netbox_librenms_plugin.utils.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)):
                with patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])):
                    with patch.object(InstallBranchView, "_find_parent_module_id", return_value=None):
                        with patch.object(InstallBranchView, "_match_bay", return_value=bay):
                            result = view._install_single(
                                device, item, index_map, module_types, ModuleBay, ModuleType, Module
                            )

        assert result["status"] == "installed"
        assert "WS-X4748" in result["name"]
        assert result["module_pk"] == 123
        assert module_instance._adopt_components is True

    def test_returns_skipped_when_no_type(self):
        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()

        with patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=None):
            result = view._install_single(
                device,
                item,
                index_map,
                {},  # empty module_types → no match
                ModuleBay,
                ModuleType,
                Module,
            )

        assert result["status"] == "skipped"
        assert "no matching type" in result["reason"]

    def test_returns_skipped_when_no_bay(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()

        with patch("netbox_librenms_plugin.utils.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)):
            with patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])):
                with patch.object(InstallBranchView, "_find_parent_module_id", return_value=None):
                    with patch.object(InstallBranchView, "_match_bay", return_value=None):
                        result = view._install_single(
                            device, item, index_map, module_types, ModuleBay, ModuleType, Module
                        )

        assert result["status"] == "skipped"
        assert "no matching bay" in result["reason"]

    def test_returns_skipped_when_bay_already_occupied(self):
        from contextlib import contextmanager

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
        occupied_module = _module()
        bay.installed_module = occupied_module
        # Update the locked bay to also appear occupied
        locked_bay = MagicMock()
        locked_bay.installed_module = occupied_module
        locked_bay.pk = bay.pk
        ModuleBay.objects.select_for_update.return_value.select_related.return_value.get.return_value = locked_bay

        @contextmanager
        def noop_atomic():
            yield

        with patch("netbox_librenms_plugin.views.sync.modules.transaction.atomic", noop_atomic):
            with patch("netbox_librenms_plugin.utils.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)):
                with patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])):
                    with patch.object(InstallBranchView, "_find_parent_module_id", return_value=None):
                        with patch.object(InstallBranchView, "_match_bay", return_value=bay):
                            result = view._install_single(
                                device, item, index_map, module_types, ModuleBay, ModuleType, Module
                            )

        assert result["status"] == "skipped"
        assert "already occupied" in result["reason"]
        assert result["module_pk"] == occupied_module.pk

    def test_infers_port_bay_from_interface_label_suffix(self):
        """_install_single should install port rows by inferring bay index from interface labels."""
        from contextlib import contextmanager

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()

        # Force a port-class row where positional slot differs from interface suffix.
        item.update(
            {
                "entPhysicalClass": "port",
                "entPhysicalName": "Te1/1/1",
                "entPhysicalDescr": "TenGigabitEthernet1/1/1",
                "entPhysicalContainedIn": 105,
            }
        )

        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalModelName": "REAL-CHASSIS",
                "entPhysicalClass": "chassis",
                "entPhysicalContainedIn": 0,
                "entPhysicalParentRelPos": 0,
            },
        ]
        for n in range(1, 6):
            inventory.append(
                {
                    "entPhysicalIndex": 100 + n,
                    "entPhysicalModelName": "",
                    "entPhysicalClass": "container",
                    "entPhysicalContainedIn": 1,
                    "entPhysicalParentRelPos": n,
                }
            )
        inventory.append(item)
        index_map = {entry["entPhysicalIndex"]: entry for entry in inventory}

        bay.name = "SFP 1"
        bay.module_id = None
        ModuleBay.objects.filter.return_value.select_related.return_value = [bay]

        locked_bay = _bay("SFP 1")
        locked_bay.pk = bay.pk
        locked_bay.module_id = None
        locked_bay.installed_module = None
        ModuleBay.objects.select_for_update.return_value.select_related.return_value.get.return_value = locked_bay

        module_instance = MagicMock()
        module_instance.pk = 321
        Module.return_value = module_instance

        @contextmanager
        def noop_atomic():
            yield

        with patch("netbox_librenms_plugin.views.sync.modules.transaction.atomic", noop_atomic):
            with patch("netbox_librenms_plugin.utils.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)):
                with patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])):
                    with patch.object(InstallBranchView, "_find_parent_module_id", return_value=None):
                        result = view._install_single(
                            device,
                            item,
                            index_map,
                            module_types,
                            ModuleBay,
                            ModuleType,
                            Module,
                        )

        assert result["status"] == "installed"
        assert "SFP 1" in result["name"]
        assert result["module_pk"] == 321
        assert module_instance._adopt_components is True

    def test_missing_port_child_bay_returns_no_matching_bay(self):
        """Port rows under installed parents should not auto-create child bays when missing."""
        from contextlib import contextmanager

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()

        item.update(
            {
                "entPhysicalClass": "port",
                "entPhysicalName": "Te1/1/2",
                "entPhysicalDescr": "TenGigabitEthernet1/1/2",
            }
        )

        # No bays under parent module scope.
        ModuleBay.objects.filter.return_value.select_related.return_value = []
        ModuleBay.objects.filter.return_value.first.return_value = None

        @contextmanager
        def noop_atomic():
            yield

        with patch("netbox_librenms_plugin.views.sync.modules.transaction.atomic", noop_atomic):
            with patch("netbox_librenms_plugin.utils.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)):
                with patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])):
                    with patch.object(InstallBranchView, "_find_parent_module_id", return_value=999):
                        with patch.object(InstallBranchView, "_match_bay", return_value=None):
                            result = view._install_single(
                                device,
                                item,
                                index_map,
                                module_types,
                                ModuleBay,
                                ModuleType,
                                Module,
                            )

        assert result["status"] == "skipped"
        assert result["reason"] == "no matching bay"
        ModuleBay.assert_not_called()

    def test_installed_name_includes_adoption_count(self):
        """Install result should include adoption summary when standalone interfaces are claimed."""
        from contextlib import contextmanager

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
        module_instance = MagicMock()
        module_instance.pk = 222
        Module.return_value = module_instance

        @contextmanager
        def noop_atomic():
            yield

        with patch("netbox_librenms_plugin.views.sync.modules.transaction.atomic", noop_atomic):
            with patch("netbox_librenms_plugin.utils.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)):
                with patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])):
                    with patch("netbox_librenms_plugin.views.sync.modules._count_adoptable_interfaces", return_value=3):
                        with patch.object(InstallBranchView, "_find_parent_module_id", return_value=None):
                            with patch.object(InstallBranchView, "_match_bay", return_value=bay):
                                result = view._install_single(
                                    device, item, index_map, module_types, ModuleBay, ModuleType, Module
                                )

        assert result["status"] == "installed"
        assert "adopted 3 existing interface(s)" in result["name"]
        assert result["adopted_interfaces"] == 3

    def test_returns_failed_on_exception(self):
        from contextlib import contextmanager

        from django.db import IntegrityError

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
        Module.side_effect = IntegrityError("DB error")

        @contextmanager
        def noop_atomic():
            yield

        with patch("netbox_librenms_plugin.views.sync.modules.transaction.atomic", noop_atomic):
            with patch("netbox_librenms_plugin.utils.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)):
                with patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])):
                    with patch.object(InstallBranchView, "_find_parent_module_id", return_value=None):
                        with patch.object(InstallBranchView, "_match_bay", return_value=bay):
                            result = view._install_single(
                                device, item, index_map, module_types, ModuleBay, ModuleType, Module
                            )

        assert result["status"] == "failed"

    def test_dash_serial_normalized_to_empty_on_install(self):
        """When LibreNMS reports serial '-', _install_single normalizes it to '' before Module()."""
        from contextlib import contextmanager

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
        # Override the serial to "-"
        item["entPhysicalSerialNum"] = "-"
        module_instance = MagicMock()
        Module.return_value = module_instance

        @contextmanager
        def noop_atomic():
            yield

        with patch("netbox_librenms_plugin.views.sync.modules.transaction.atomic", noop_atomic):
            with patch("netbox_librenms_plugin.utils.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)):
                with patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])):
                    with patch.object(InstallBranchView, "_find_parent_module_id", return_value=None):
                        with patch.object(InstallBranchView, "_match_bay", return_value=bay):
                            result = view._install_single(
                                device, item, index_map, module_types, ModuleBay, ModuleType, Module
                            )

        assert result["status"] == "installed"
        # Verify Module was constructed with serial="" (not "-")
        Module.assert_called_once()
        assert Module.call_args.kwargs["serial"] == "", (
            f"Expected serial='' but Module was called with serial={Module.call_args.kwargs['serial']!r}"
        )


class TestBindInterfaceLibrenmsId:
    """Covers post-install interface librenms_id binding helper behaviour."""

    def test_returns_none_without_port_identity(self):
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = MagicMock()
        device.pk = 1
        result = _bind_interface_librenms_id(device, {"entPhysicalName": "SFP"}, module_pk=10, server_key="default")
        assert result is None


class TestAdoptExistingTemplateInterfaces:
    """Covers adopting standalone interfaces into already-installed modules."""

    def test_adopts_matching_standalone_interfaces(self):
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        device = MagicMock()
        module = MagicMock()
        module.module_type.interfacetemplates.all.return_value = [MagicMock(), MagicMock()]

        instantiated_a = MagicMock()
        instantiated_a.name = "Te1/1/1"
        instantiated_b = MagicMock()
        instantiated_b.name = "Te1/1/2"
        module.module_type.interfacetemplates.all.return_value[0].instantiate.return_value = instantiated_a
        module.module_type.interfacetemplates.all.return_value[1].instantiate.return_value = instantiated_b

        iface_a = MagicMock()
        iface_a.name = "Te1/1/1"
        iface_b = MagicMock()
        iface_b.name = "Te1/1/2"

        @contextmanager
        def noop_atomic():
            yield

        with (
            patch("dcim.models.Interface") as mock_interface_model,
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
        ):
            mock_tx.atomic = noop_atomic
            mock_interface_model.objects.filter.return_value = [iface_a, iface_b]
            result = _adopt_existing_template_interfaces(device, module)

        assert result["status"] == "bound"
        assert result["adopted_count"] == 2
        assert iface_a.module is module
        assert iface_b.module is module
        iface_a.save.assert_called_once_with(update_fields=["module"])
        iface_b.save.assert_called_once_with(update_fields=["module"])

    def test_adoption_runs_inside_atomic_transaction(self):
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        device = MagicMock()
        module = MagicMock()
        module.module_type.interfacetemplates.all.return_value = [MagicMock(), MagicMock()]

        instantiated_a = MagicMock()
        instantiated_a.name = "Te1/1/1"
        instantiated_b = MagicMock()
        instantiated_b.name = "Te1/1/2"
        module.module_type.interfacetemplates.all.return_value[0].instantiate.return_value = instantiated_a
        module.module_type.interfacetemplates.all.return_value[1].instantiate.return_value = instantiated_b

        iface_a = MagicMock()
        iface_a.name = "Te1/1/1"
        iface_b = MagicMock()
        iface_b.name = "Te1/1/2"
        iface_a.save.side_effect = RuntimeError("boom")

        atomic_events = []

        @contextmanager
        def tracking_atomic():
            atomic_events.append("enter")
            try:
                yield
            finally:
                atomic_events.append("exit")

        with (
            patch("dcim.models.Interface") as mock_interface_model,
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
        ):
            mock_tx.atomic = tracking_atomic
            mock_interface_model.objects.filter.return_value = [iface_a, iface_b]

            try:
                _adopt_existing_template_interfaces(device, module)
                assert False, "Expected adoption failure to propagate"
            except RuntimeError as exc:
                assert str(exc) == "boom"

        assert atomic_events == ["enter", "exit"]
        iface_a.save.assert_called_once_with(update_fields=["module"])
        iface_b.save.assert_not_called()

    def test_adopts_matching_vc_rewritten_template_interfaces(self):
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        device = MagicMock()
        device.vc_position = 3
        device.virtual_chassis_id = 11
        device.virtual_chassis = MagicMock()
        device.virtual_chassis.members.values_list.return_value = [1, 2, 3]

        module = MagicMock()
        template = MagicMock()
        instantiated = MagicMock()
        instantiated.name = "TenGigabitEthernet1/1/1"
        template.instantiate.return_value = instantiated
        module.module_type.interfacetemplates.all.return_value = [template]

        iface = MagicMock()
        iface.name = "TenGigabitEthernet3/1/1"

        @contextmanager
        def noop_atomic():
            yield

        with (
            patch("dcim.models.Interface") as mock_interface_model,
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
        ):
            mock_tx.atomic = noop_atomic
            mock_interface_model.objects.filter.return_value = [iface]
            result = _adopt_existing_template_interfaces(device, module)

        assert result["status"] == "bound"
        assert result["adopted_count"] == 1
        assert result["interfaces"] == ["TenGigabitEthernet3/1/1"]
        # The standalone lookup is still issued (the function now also queries the module's
        # own interfaces to detect raw duplicates, so it is no longer the only filter call).
        mock_interface_model.objects.filter.assert_any_call(
            device=device,
            module__isnull=True,
            name__in=["TenGigabitEthernet3/1/1"],
        )

    def test_binds_unique_module_interface(self):
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = MagicMock()
        device.pk = 1

        candidate = MagicMock()
        candidate.name = "Te1/0/1"
        candidate.module_id = 123

        by_name_qs = MagicMock()
        by_name_qs.first.return_value = None

        module_qs = MagicMock()
        module_qs.filter.return_value.first.return_value = candidate

        with (
            patch("dcim.models.Interface") as mock_interface_model,
            patch("netbox_librenms_plugin.views.sync.modules.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.set_librenms_device_id") as mock_set,
        ):
            mock_interface_model.objects.filter.side_effect = [by_name_qs, module_qs]
            result = _bind_interface_librenms_id(
                device,
                {"_librenms_port_id": 42, "_librenms_ifname": "Te1/0/1"},
                module_pk=123,
                server_key="default",
            )

        assert result["status"] == "bound"
        assert result["port_id"] == 42
        assert result["interface"] == "Te1/0/1"
        mock_set.assert_called_once_with(candidate, 42, "default")
        candidate.save.assert_called_once_with(update_fields=["custom_field_data"])

    def test_binds_single_iterable_module_interface(self):
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = MagicMock()
        device.pk = 1

        candidate = MagicMock()
        candidate.name = "uplink-a"
        candidate.module_id = 123

        by_name_qs = MagicMock()
        by_name_qs.first.return_value = None

        module_qs = MagicMock()
        module_qs.filter.return_value.first.return_value = None
        module_qs.__iter__.return_value = iter([candidate])

        with (
            patch("dcim.models.Interface") as mock_interface_model,
            patch("netbox_librenms_plugin.views.sync.modules.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.set_librenms_device_id") as mock_set,
        ):
            mock_interface_model.objects.filter.side_effect = [by_name_qs, module_qs]
            result = _bind_interface_librenms_id(
                device,
                {"_librenms_port_id": 43, "_librenms_ifname": "Unknown-Port"},
                module_pk=123,
                server_key="default",
            )

        assert result["status"] == "bound"
        assert result["interface"] == "uplink-a"
        mock_set.assert_called_once_with(candidate, 43, "default")
        candidate.save.assert_called_once_with(update_fields=["custom_field_data"])

    def test_conflict_when_port_id_owned_by_other_device(self):
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = MagicMock()
        device.pk = 1

        existing_owner = MagicMock()
        existing_owner.device_id = 2
        existing_owner.name = "Eth1/1"
        existing_owner.device.name = "other-device"

        with patch("netbox_librenms_plugin.views.sync.modules.find_by_librenms_id", return_value=existing_owner):
            result = _bind_interface_librenms_id(
                device,
                {"_librenms_port_id": 55, "_librenms_ifname": "Te1/0/2"},
                module_pk=999,
                server_key="default",
            )

        assert result["status"] == "conflict"
        assert "not reassigning" in result["reason"]

    def test_reparents_standalone_interface_when_module_known(self):
        """When the resolved interface has no module, bind should attach it to installed module."""
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = MagicMock()
        device.pk = 1

        candidate = MagicMock()
        candidate.name = "Te1/0/1"
        candidate.device_id = 1
        candidate.module_id = None

        with (
            patch("netbox_librenms_plugin.views.sync.modules.find_by_librenms_id", return_value=candidate),
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.set_librenms_device_id") as mock_set,
        ):
            result = _bind_interface_librenms_id(
                device,
                {"_librenms_port_id": 77, "_librenms_ifname": "Te1/0/1"},
                module_pk=555,
                server_key="default",
            )

        assert result["status"] == "bound"
        assert candidate.module_id == 555
        mock_set.assert_called_once_with(candidate, 77, "default")
        candidate.save.assert_called_once_with(update_fields=["custom_field_data", "module"])

    def test_uses_ifdescr_when_ifname_differs(self):
        """Name fallback should include long-form ifDescr values when matching interfaces."""
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = MagicMock()
        device.pk = 1

        candidate = MagicMock()
        candidate.name = "TenGigabitEthernet1/0/1"
        candidate.module_id = None

        by_name_qs = MagicMock()
        by_name_qs.first.return_value = candidate

        with (
            patch("dcim.models.Interface") as mock_interface_model,
            patch("netbox_librenms_plugin.views.sync.modules.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.set_librenms_device_id") as mock_set,
        ):
            mock_interface_model.objects.filter.return_value = by_name_qs
            result = _bind_interface_librenms_id(
                device,
                {
                    "_librenms_port_id": 88,
                    "_librenms_ifname": "Te1/0/1",
                    "_librenms_ifdescr": "TenGigabitEthernet1/0/1",
                    "entPhysicalName": "Unknown-Port",
                },
                module_pk=None,
                server_key="default",
            )

        assert result["status"] == "bound"
        call_kwargs = mock_interface_model.objects.filter.call_args.kwargs
        assert "TenGigabitEthernet1/0/1" in call_kwargs["name__in"]
        mock_set.assert_called_once_with(candidate, 88, "default")

    def test_uses_coordinate_fallback_when_multiple_module_interfaces_exist(self):
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = MagicMock()
        device.pk = 1
        device.vc_position = None

        iface_a = MagicMock()
        iface_a.name = "GigabitEthernet1/1/23"
        iface_a.module_id = 123

        iface_b = MagicMock()
        iface_b.name = "GigabitEthernet1/1/24"
        iface_b.module_id = 123

        by_name_qs = MagicMock()
        by_name_qs.first.return_value = None

        module_qs = MagicMock()
        module_qs.filter.return_value.first.return_value = None
        module_qs.__iter__.return_value = iter([iface_a, iface_b])

        with (
            patch("dcim.models.Interface") as mock_interface_model,
            patch("netbox_librenms_plugin.views.sync.modules.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.set_librenms_device_id") as mock_set,
        ):
            mock_interface_model.objects.filter.side_effect = [by_name_qs, module_qs]
            result = _bind_interface_librenms_id(
                device,
                {
                    "_librenms_port_id": 4242,
                    "_librenms_ifname": "GigabitEthernet5/1/24",
                    "_librenms_ifdescr": "Gi5/1/24",
                },
                module_pk=123,
                server_key="default",
            )

        assert result["status"] == "bound"
        assert result["interface"] == "GigabitEthernet1/1/24"
        mock_set.assert_called_once_with(iface_b, 4242, "default")

    def test_coordinate_fallback_skips_when_top_score_is_ambiguous(self):
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device = MagicMock()
        device.pk = 1
        device.vc_position = None

        iface_a = MagicMock()
        iface_a.name = "TenGigabitEthernet1/1/24"
        iface_a.module_id = 123

        iface_b = MagicMock()
        iface_b.name = "HundredGigE2/1/24"
        iface_b.module_id = 123

        by_name_qs = MagicMock()
        by_name_qs.first.return_value = None

        module_qs = MagicMock()
        module_qs.filter.return_value.first.return_value = None
        module_qs.__iter__.return_value = iter([iface_a, iface_b])

        with (
            patch("dcim.models.Interface") as mock_interface_model,
            patch("netbox_librenms_plugin.views.sync.modules.find_by_librenms_id", return_value=None),
        ):
            mock_interface_model.objects.filter.side_effect = [by_name_qs, module_qs]
            result = _bind_interface_librenms_id(
                device,
                {
                    "_librenms_port_id": 4343,
                    "_librenms_ifname": "Port5/0/24",
                    "_librenms_ifdescr": "Port5/0/24",
                },
                module_pk=123,
                server_key="default",
            )

        assert result["status"] == "skipped"
        assert "multiple module interfaces found" in result["reason"]


@pytest.mark.django_db
class TestAdoptMergesRawDuplicateInterface:
    """Real-DB: adopting folds the module's raw twin into the externally-created interface.

    Reproduces the Nokia/INR scenario: the module owns a raw interface (``2/x1/1/c28``)
    bound to a LibreNMS port, while a separate standalone interface already carries the
    INR-renamed name (``2/x1/1/c28/1``) plus a cable — so INR's rename was skipped on the
    name collision. Adopting must claim the standalone (keeping its cable), move the
    LibreNMS port binding off the raw twin, and delete the now-redundant raw twin.
    """

    @pytest.fixture(autouse=True)
    def _configure_production_server(self):
        # These tests post server_key="production"; resolve_posted_server_key now validates the posted
        # key against the configured servers before honouring it, so "production" must be configured or
        # it degrades to the active server. Configuring it mirrors a real multi-server deployment.
        # Patch get_plugin_config (the seam BOTH LibreNMSAPI.__init__ and get_available_servers read),
        # not just the get_available_servers classmethod: the panel-render path in some of these tests
        # constructs a real LibreNMSAPI("production"), which __init__ validates against get_plugin_config
        # — so "production" must be a fully configured server (url+token), else the render KeyErrors.
        servers = {
            "default": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"},
            "production": {"librenms_url": "https://prod.example.com", "api_token": "prod-token"},
        }
        with patch("netbox_librenms_plugin.librenms_api.get_plugin_config", return_value=servers):
            yield

    def _build(self):
        from dcim.models import (
            Cable,
            Device,
            DeviceRole,
            DeviceType,
            Interface,
            InterfaceTemplate,
            Manufacturer,
            Module,
            ModuleBay,
            ModuleType,
            Site,
        )

        mfr, _ = Manufacturer.objects.get_or_create(name="Nokia-adopt", slug="nokia-adopt")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="7750-adopt", slug="7750-adopt")
        role, _ = DeviceRole.objects.get_or_create(name="rtr-adopt", slug="rtr-adopt")
        site, _ = Site.objects.get_or_create(name="site-adopt", slug="site-adopt")
        device = Device.objects.create(name="adopt-host", device_type=dt, role=role, site=site, status="active")

        mt = ModuleType.objects.create(manufacturer=mfr, model="QSFP-DD-adopt")
        InterfaceTemplate.objects.create(module_type=mt, name="2/x1/1/c28", type="other")
        bay = ModuleBay.objects.create(device=device, name="2/x1/1/c28")
        module = Module.objects.create(device=device, module_bay=bay, module_type=mt)

        # Deterministic interface set: clear any auto-instantiated module components.
        Interface.objects.filter(device=device).delete()

        # Raw module interface (template name) bound to LibreNMS port 627.
        raw = Interface.objects.create(device=device, module=module, name="2/x1/1/c28", type="other")
        raw.custom_field_data["librenms_id"] = {"production": 627}
        raw.save(update_fields=["custom_field_data"])

        # Externally-created standalone with the INR-renamed name, carrying a cable.
        standalone = Interface.objects.create(device=device, name="2/x1/1/c28/1", type="other")
        peer = Interface.objects.create(device=device, name="peer", type="other")
        cable = Cable(a_terminations=[standalone], b_terminations=[peer])
        cable.save()

        return device, module, raw, standalone, cable

    def test_adopt_merges_raw_twin_preserving_cable_and_binding(self):
        from django.dispatch import receiver

        from dcim.models import Interface

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        device, module, raw, standalone, cable = self._build()

        @receiver(predict_module_interface_names)
        def _append_channel(sender, device, module, names, **kwargs):  # noqa: ARG001
            # Mirrors the real INR rule '/QSFP-DD.*/ [timos] → {base}/1'.
            return [f"{n}/1" for n in names]

        try:
            result = _adopt_existing_template_interfaces(device, module, "production")
        finally:
            predict_module_interface_names.disconnect(_append_channel)

        # The externally-created standalone is now the module's interface...
        standalone.refresh_from_db()
        assert standalone.module_id == module.pk
        # ...and keeps its cable (its external relations survive the adopt).
        assert standalone.cable_id == cable.pk
        # The raw template twin is folded in, not left as a duplicate.
        assert not Interface.objects.filter(pk=raw.pk).exists()
        # The LibreNMS port binding moved from the raw twin onto the adopted interface.
        assert get_librenms_device_id(standalone, "production") == 627

        assert result["status"] == "bound"
        assert result["adopted_count"] == 1
        assert result["merged_count"] == 1
        assert result["interfaces"] == ["2/x1/1/c28/1"]

    def test_cabled_raw_twin_is_left_in_place(self):
        """A raw twin that itself carries a cable is not deleted (no silent connection loss)."""
        from django.dispatch import receiver

        from dcim.models import Cable, Interface

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        device, module, raw, standalone, _cable = self._build()
        # Give the raw twin its own cable so deleting it would drop a connection.
        raw_peer = Interface.objects.create(device=device, name="raw-peer", type="other")
        raw_cable = Cable(a_terminations=[raw], b_terminations=[raw_peer])
        raw_cable.save()

        @receiver(predict_module_interface_names)
        def _append_channel(sender, device, module, names, **kwargs):  # noqa: ARG001
            return [f"{n}/1" for n in names]

        try:
            result = _adopt_existing_template_interfaces(device, module, "production")
        finally:
            predict_module_interface_names.disconnect(_append_channel)

        # The standalone is still adopted, but the cabled raw twin survives and is reported.
        standalone.refresh_from_db()
        assert standalone.module_id == module.pk
        assert Interface.objects.filter(pk=raw.pk).exists()
        assert result.get("merged_count") is None
        assert result["cabled_duplicates"] == ["2/x1/1/c28"]

    def test_update_interface_view_reconciles_when_port_bind_is_noop(self):
        """The reported bug: the view must adopt/merge even when the LibreNMS port-bind is a no-op.

        Posting the port id that already sits on the module's raw interface makes
        ``_bind_interface_librenms_id`` return a no-op "bound" result. Previously that
        short-circuited the adopt; now the adopt/merge still runs end-to-end.
        """
        from django.dispatch import receiver

        from dcim.models import Interface

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module, raw, standalone, _cable = self._build()

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        request = _make_request(
            "POST",
            data={
                "module_id": str(module.pk),
                "server_key": "production",
                # Port 627 already sits on the module's raw interface → the port-bind no-ops.
                "librenms_port_id": "627",
                "librenms_ifname": "2/x1/1/c28",
            },
        )

        @receiver(predict_module_interface_names)
        def _append_channel(sender, device, module, names, **kwargs):  # noqa: ARG001
            return [f"{n}/1" for n in names]

        try:
            with (
                patch.object(view, "require_all_permissions", return_value=None),
                patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            ):
                view.post(request, pk=device.pk)
        finally:
            predict_module_interface_names.disconnect(_append_channel)

        # The standalone is adopted and the raw twin folded in (was a silent no-op before).
        standalone.refresh_from_db()
        assert standalone.module_id == module.pk
        assert not Interface.objects.filter(pk=raw.pk).exists()
        # The success message reports the real outcome, not a misleading no-op "bound".
        assert mock_msg.success.called
        success_text = mock_msg.success.call_args[0][1]
        assert "adopted 1" in success_text
        assert "removed 1 duplicate" in success_text

    def test_real_port_bind_confirmation_survives_concurrent_adopt(self):
        """A click that BOTH (re)binds a port AND adopts standalones must surface the port-bind
        confirmation, not let the adopt summary swallow it.

        Before the fix, ``bind_result = adopt_result`` discarded the port-bind's
        interface/port_id detail whenever adoption also reported "bound", so the user never
        saw which port_id was bound.
        """
        from django.dispatch import receiver

        from dcim.models import Interface

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module, raw, standalone, _cable = self._build()
        # A separate module interface that is not yet bound: the POSTed port 999 REALLY binds it
        # (update_fields set → changed=True), distinct from the adopt of the template twin.
        Interface.objects.create(device=device, module=module, name="extra-port", type="other")

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        request = _make_request(
            "POST",
            data={
                "module_id": str(module.pk),
                "server_key": "production",
                "librenms_port_id": "999",
                "librenms_ifname": "extra-port",
            },
        )

        @receiver(predict_module_interface_names)
        def _append_channel(sender, device, module, names, **kwargs):  # noqa: ARG001
            return [f"{n}/1" for n in names]

        try:
            with (
                patch.object(view, "require_all_permissions", return_value=None),
                patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            ):
                view.post(request, pk=device.pk)
        finally:
            predict_module_interface_names.disconnect(_append_channel)

        success_texts = [call.args[1] for call in mock_msg.success.call_args_list]
        # The real port-bind confirmation is present (dropped before the fix)...
        assert any("extra-port" in t and "999" in t for t in success_texts), success_texts
        # ...and the adopt summary is still present.
        assert any("adopted 1" in t for t in success_texts), success_texts

    def test_adopt_moves_assigned_ips_to_adopted_interface(self):
        """An IP assigned to the raw twin (even the device's primary IP) must be MOVED to the adopted interface — Interface.ip_addresses cascade-deletes on interface delete, so the old code silently destroyed it and SET_NULL wiped Device.primary_ip4."""
        from django.dispatch import receiver

        from dcim.models import Interface
        from ipam.models import IPAddress

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        device, module, raw, standalone, _cable = self._build()
        ip = IPAddress.objects.create(address="192.0.2.10/24", assigned_object=raw)
        device.primary_ip4 = ip
        device.save(update_fields=["primary_ip4"])

        @receiver(predict_module_interface_names)
        def _append_channel(sender, device, module, names, **kwargs):  # noqa: ARG001
            return [f"{n}/1" for n in names]

        try:
            result = _adopt_existing_template_interfaces(device, module, "production")
        finally:
            predict_module_interface_names.disconnect(_append_channel)

        # The raw twin is still folded in...
        assert not Interface.objects.filter(pk=raw.pk).exists()
        assert result.get("merged_count") == 1
        # ...but its IP survives, reassigned to the authoritative adopted interface...
        ip.refresh_from_db()
        assert ip.assigned_object_id == standalone.pk
        # ...so the device's primary IP is intact (on_delete=SET_NULL would have nulled it).
        device.refresh_from_db()
        assert device.primary_ip4_id == ip.pk

    def test_blank_server_key_never_deletes_a_bound_raw_twin(self):
        """With no server scope the raw twin's LibreNMS binding cannot be transferred; the twin must be left in place for review (fail closed), not deleted with its binding silently destroyed."""
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        device, module, raw, standalone, _cable = self._build()

        @receiver(predict_module_interface_names)
        def _append_channel(sender, device, module, names, **kwargs):  # noqa: ARG001
            return [f"{n}/1" for n in names]

        try:
            result = _adopt_existing_template_interfaces(device, module, "")
        finally:
            predict_module_interface_names.disconnect(_append_channel)

        # The standalone is still adopted...
        standalone.refresh_from_db()
        assert standalone.module_id == module.pk
        # ...but the bound raw twin survives with its binding untouched, reported for review.
        raw.refresh_from_db()
        assert raw.custom_field_data.get("librenms_id") == {"production": 627}
        assert result.get("merged_count") is None
        assert result["cabled_duplicates"] == ["2/x1/1/c28"]

    def test_update_interface_view_blank_server_key_falls_back_and_transfers_binding(self):
        """e2e: a POST without server_key (stale tab / fallback-rendered pane) must resolve the active server so the merge still transfers the raw twin's binding — the unfixed view passed the blank key straight through and the binding was destroyed with the twin."""
        from django.dispatch import receiver

        from dcim.models import Interface

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module, raw, standalone, _cable = self._build()

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="production")  # blank posted server_key falls back to this
        request = _make_request("POST", data={"module_id": str(module.pk)})  # NO server_key posted

        @receiver(predict_module_interface_names)
        def _append_channel(sender, device, module, names, **kwargs):  # noqa: ARG001
            return [f"{n}/1" for n in names]

        try:
            with (
                patch.object(view, "require_all_permissions", return_value=None),
                patch("netbox_librenms_plugin.views.sync.modules.messages"),
            ):
                view.post(request, pk=device.pk)
        finally:
            predict_module_interface_names.disconnect(_append_channel)

        # The merge ran under the resolved active server: twin folded, binding transferred.
        standalone.refresh_from_db()
        assert standalone.module_id == module.pk
        assert not Interface.objects.filter(pk=raw.pk).exists()
        assert get_librenms_device_id(standalone, "production") == 627

    def test_failed_port_bind_reason_survives_successful_adoption(self):
        """A non-bound port-bind outcome (conflict/skipped/failed) must be surfaced even when adoption succeeds — the adopt summary used to silently replace it, hiding e.g. a port-conflict from the user."""
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module, raw, standalone, _cable = self._build()

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        request = _make_request(
            "POST",
            data={
                "module_id": str(module.pk),
                "server_key": "production",
                # Port 641 resolves to the module's sole raw interface (single-module-interface
                # fallback), which is already mapped to 627 → deterministic bind CONFLICT.
                "librenms_port_id": "641",
                "librenms_ifname": "no-such-interface",
            },
        )

        @receiver(predict_module_interface_names)
        def _append_channel(sender, device, module, names, **kwargs):  # noqa: ARG001
            return [f"{n}/1" for n in names]

        try:
            with (
                patch.object(view, "require_all_permissions", return_value=None),
                patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            ):
                view.post(request, pk=device.pk)
        finally:
            predict_module_interface_names.disconnect(_append_channel)

        # The bind conflict is surfaced (silently dropped before the fix)...
        warning_texts = [call.args[1] for call in mock_msg.warning.call_args_list]
        assert any("Could not update interface association" in t and "627" in t for t in warning_texts), warning_texts
        # ...and the adoption summary is still reported.
        success_texts = [call.args[1] for call in mock_msg.success.call_args_list]
        assert any("adopted 1" in t for t in success_texts), success_texts

    def test_merge_locks_raw_twin_before_delete(self):
        """The destructive delete must re-verify its preconditions from a locked row (select_for_update) so a concurrent cabling between the caller's read and the delete is not cascaded away."""
        from django.db import connection
        from django.dispatch import receiver
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        device, module, _raw, _standalone, _cable = self._build()

        @receiver(predict_module_interface_names)
        def _append_channel(sender, device, module, names, **kwargs):  # noqa: ARG001
            return [f"{n}/1" for n in names]

        try:
            with CaptureQueriesContext(connection) as ctx:
                result = _adopt_existing_template_interfaces(device, module, "production")
        finally:
            predict_module_interface_names.disconnect(_append_channel)

        assert result.get("merged_count") == 1
        assert any(
            "for update" in q["sql"].lower() and "dcim_interface" in q["sql"].lower() for q in ctx.captured_queries
        ), "raw twin was deleted without a select_for_update re-verify"

    def test_htmx_swap_renders_page_device_panel_not_vc_member(self):
        """The HTMX table swap replaces the PAGE device's module panel, so it must re-render for the page device even when the row action targeted a VC member — rendering the member's panel flips unrelated rows' bay matching (the page's own bays vanish)."""
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from dcim.models import Device, Module, ModuleBay, VirtualChassis

        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module, _raw, _standalone, _cable = self._build()
        # Make the page device a VC master with a member that owns its own module.
        vc = VirtualChassis.objects.create(name="vc-panel-scope")
        Device.objects.filter(pk=device.pk).update(virtual_chassis=vc, vc_position=1)
        member = Device.objects.create(
            name="panel-member",
            device_type=device.device_type,
            role=device.role,
            site=device.site,
            status="active",
            virtual_chassis=vc,
            vc_position=2,
        )
        member_bay = ModuleBay.objects.create(device=member, name="member-bay")
        member_module = Module.objects.create(device=member, module_bay=member_bay, module_type=module.module_type)

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="production")
        view.has_write_permission = MagicMock(return_value=True)

        request = RequestFactory().post(
            "/",
            data={
                "module_id": str(member_module.pk),
                "selected_device_id": str(member.pk),
                "server_key": "production",
            },
            HTTP_HX_REQUEST="true",
        )
        request.user = AnonymousUser()
        request.session = "session"
        request._messages = FallbackStorage(request)

        rendered_for = []
        original_get_context = DeviceModuleTableView.get_context_data

        def _spy(self, request, obj, *args, **kwargs):
            rendered_for.append(obj)
            return original_get_context(self, request, obj, *args, **kwargs)

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch.object(DeviceModuleTableView, "get_context_data", _spy),
        ):
            response = view.post(request, pk=device.pk)

        assert response.status_code == 200
        # The action ran against the member, but the swapped panel is the page device's.
        assert [d.pk for d in rendered_for] == [device.pk]

    def test_install_bay_occupied_swaps_page_device_panel_not_vc_member(self):
        """InstallModuleView's bay-occupied branch swaps the PAGE device's panel, not the targeted VC member's."""
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from dcim.models import Device, Module, ModuleBay, ModuleType, VirtualChassis

        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device, module, _raw, _standalone, _cable = self._build()
        # Page device is a VC master; the row action targets a member whose bay is already occupied.
        vc = VirtualChassis.objects.create(name="vc-install-scope")
        Device.objects.filter(pk=device.pk).update(virtual_chassis=vc, vc_position=1)
        member = Device.objects.create(
            name="install-member",
            device_type=device.device_type,
            role=device.role,
            site=device.site,
            status="active",
            virtual_chassis=vc,
            vc_position=2,
        )
        mt = ModuleType.objects.create(manufacturer=device.device_type.manufacturer, model="occupied-mt")
        member_bay = ModuleBay.objects.create(device=member, name="occupied-bay")
        Module.objects.create(device=member, module_bay=member_bay, module_type=mt)  # occupy the bay

        view = object.__new__(InstallModuleView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="production")
        view.has_write_permission = MagicMock(return_value=True)

        request = RequestFactory().post(
            "/",
            data={
                "module_bay_id": str(member_bay.pk),
                "module_type_id": str(mt.pk),
                "selected_device_id": str(member.pk),
                "server_key": "production",
            },
            HTTP_HX_REQUEST="true",
        )
        request.user = AnonymousUser()
        request.session = "session"
        request._messages = FallbackStorage(request)

        rendered_for = []
        original_get_context = DeviceModuleTableView.get_context_data

        def _spy(self, request, obj, *args, **kwargs):
            rendered_for.append(obj)
            return original_get_context(self, request, obj, *args, **kwargs)

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch.object(DeviceModuleTableView, "get_context_data", _spy),
        ):
            response = view.post(request, pk=device.pk)

        assert response.status_code == 200
        # Bay was occupied → the swapped panel must be the PAGE device's, not the member's (target_device).
        assert [d.pk for d in rendered_for] == [device.pk]

    def test_bind_success_survives_adopt_failure(self):
        """B2: a committed port-bind is still reported even if the later adoption step raises (not masked)."""
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device, module, _raw, _standalone, _cable = self._build()

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="production")
        view.has_write_permission = MagicMock(return_value=True)

        request = RequestFactory().post(
            "/",
            data={"module_id": str(module.pk), "server_key": "production"},
            HTTP_HX_REQUEST="true",
        )
        request.user = AnonymousUser()
        request.session = "session"
        request._messages = FallbackStorage(request)

        # The port-bind commits; the subsequent adoption raises (its own transaction rolls back).
        bound = {"status": "bound", "changed": True, "interface": "eth-bind", "port_id": 42}
        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules._resolve_single_install_binding_item",
                return_value={"ent_physical_index": 1},
            ),
            patch("netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id", return_value=bound),
            patch(
                "netbox_librenms_plugin.views.sync.modules._adopt_existing_template_interfaces",
                side_effect=RuntimeError("adopt boom"),
            ),
            # Sentinel the swap so it doesn't consume the flash messages we assert on.
            patch(
                "netbox_librenms_plugin.views.sync.modules._render_modules_partial_after_action",
                return_value="PARTIAL",
            ),
        ):
            view.post(request, pk=device.pk)

        joined = " | ".join(m.message for m in request._messages)
        # The bind that DID commit is still reported...
        assert "eth-bind" in joined
        # ...and is NOT hidden behind the old generic "associating" failure that swallowed it.
        assert "unexpected error while associating" not in joined


class TestPredictModuleInterfaceRenameLengthGuard:
    """predict_module_interface_rename enforces the 1:1, order-preserving receiver contract."""

    def test_misaligned_receiver_result_is_ignored(self):
        """A receiver returning fewer names than asked is rejected; identity names are kept."""
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import predict_module_interface_rename

        @receiver(predict_module_interface_names)
        def _drop_one(sender, device, module, names, **kwargs):  # noqa: ARG001
            return list(names)[:-1]  # misaligned: one fewer than the input

        try:
            result = predict_module_interface_rename(object(), object(), ["a", "b", "c"])
        finally:
            predict_module_interface_names.disconnect(_drop_one)

        assert result == ["a", "b", "c"]

    def test_aligned_receiver_result_is_applied(self):
        """Positive control: a correctly-sized receiver result IS used (guard isn't over-broad)."""
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import predict_module_interface_rename

        @receiver(predict_module_interface_names)
        def _rename(sender, device, module, names, **kwargs):  # noqa: ARG001
            return [f"{n}/1" for n in names]

        try:
            result = predict_module_interface_rename(object(), object(), ["a", "b"])
        finally:
            predict_module_interface_names.disconnect(_rename)

        assert result == ["a/1", "b/1"]


class TestSingleInstallInterfaceBinding:
    """Single-row install should resolve inventory identity and bind interfaces."""

    @pytest.fixture(autouse=True)
    def _configure_production_server(self):
        # These tests post server_key="production"; resolve_posted_server_key now validates it against
        # the configured servers before honouring it, so "production" must be configured or it degrades
        # to the active server. Configuring it mirrors a real multi-server deployment.
        # Patch get_plugin_config (the seam BOTH LibreNMSAPI.__init__ and get_available_servers read),
        # not just the get_available_servers classmethod: the panel-render path in some of these tests
        # constructs a real LibreNMSAPI("production"), which __init__ validates against get_plugin_config
        # — so "production" must be a fully configured server (url+token), else the render KeyErrors.
        servers = {
            "default": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"},
            "production": {"librenms_url": "https://prod.example.com", "api_token": "prod-token"},
        }
        with patch("netbox_librenms_plugin.librenms_api.get_plugin_config", return_value=servers):
            yield

    def test_resolve_single_install_binding_item_uses_cache_row_by_ent_index(self):
        from netbox_librenms_plugin.views.sync.modules import _resolve_single_install_binding_item

        request = _make_request(
            "POST",
            data={
                "ent_index": "77",
                "server_key": "production",
            },
        )
        device = _make_device()

        with patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache:
            mock_cache.get.return_value = {
                "inventory": [
                    {
                        "entPhysicalIndex": 77,
                        "_librenms_port_id": 42,
                        "_librenms_ifname": "Te1/1/1",
                    }
                ]
            }
            get_cache_key = MagicMock(return_value="inventory-key")
            item = _resolve_single_install_binding_item(request, device, "production", get_cache_key)

        assert item["_librenms_port_id"] == 42
        assert item["_librenms_ifname"] == "Te1/1/1"
        assert item["_binding_source"] == "cache"
        get_cache_key.assert_called_once()

    def test_resolve_single_install_binding_item_falls_back_to_posted_hidden_fields(self):
        from netbox_librenms_plugin.views.sync.modules import _resolve_single_install_binding_item

        request = _make_request(
            "POST",
            data={
                "librenms_port_id": "56284",
                "librenms_ifname": "TenGigabitEthernet1/1/1",
                "librenms_ifdescr": "Te1/1/1",
                "inventory_name": "Te1/1/1",
                "inventory_descr": "10G transceiver",
            },
        )
        device = _make_device()

        with patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache:
            get_cache_key = MagicMock(return_value="inventory-key")
            item = _resolve_single_install_binding_item(request, device, "production", get_cache_key)

        assert item["_librenms_port_id"] == 56284
        assert item["_librenms_ifname"] == "TenGigabitEthernet1/1/1"
        assert item["_librenms_ifdescr"] == "Te1/1/1"
        assert item["entPhysicalName"] == "Te1/1/1"
        assert item["entPhysicalDescr"] == "10G transceiver"
        assert item["_binding_source"] == "post_fallback"
        mock_cache.get.assert_not_called()

    def test_resolve_single_install_binding_item_falls_back_when_cached_device_context_mismatch(self):
        from netbox_librenms_plugin.views.sync.modules import _resolve_single_install_binding_item

        request = _make_request(
            "POST",
            data={
                "ent_index": "77",
                "server_key": "production",
                "librenms_port_id": "56284",
                "librenms_ifname": "TenGigabitEthernet1/1/1",
                "inventory_name": "Te1/1/1",
            },
        )
        device = _make_device()

        with (
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=999),
        ):
            mock_cache.get.return_value = {
                "librenms_id": 555,
                "inventory": [
                    {
                        "entPhysicalIndex": 77,
                        "_librenms_port_id": 42,
                        "_librenms_ifname": "Te1/1/1",
                    }
                ],
            }
            get_cache_key = MagicMock(return_value="inventory-key")
            item = _resolve_single_install_binding_item(request, device, "production", get_cache_key)

        assert item["_librenms_port_id"] == 56284
        assert item["_librenms_ifname"] == "TenGigabitEthernet1/1/1"
        assert item["_binding_source"] == "post_fallback"

    def test_install_module_view_warns_when_binding_uses_post_fallback(self):
        from contextlib import contextmanager

        from dcim.models import ModuleBay

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        view = object.__new__(InstallModuleView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="default")  # blank posted server_key falls back to this
        device = _make_device()

        module_bay = MagicMock()
        module_bay.name = "Slot 1"
        module_bay.installed_module = None

        module_type = MagicMock()
        module_type.pk = 5
        module_type.model = "SFP-10G-SR"

        new_module = MagicMock()
        new_module.pk = 321

        request = _make_request(
            "POST",
            data={
                "module_bay_id": "10",
                "module_type_id": "5",
                "serial": "SN1",
                "server_key": "production",
                "ent_index": "77",
                "librenms_port_id": "42",
                "librenms_ifname": "Te1/1/1",
            },
        )

        @contextmanager
        def noop_atomic():
            yield

        mock_qs = MagicMock()
        mock_qs.get.return_value = module_bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module_bay, module_type],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
            patch("dcim.models.Module") as mock_module_cls,
            patch.object(ModuleBay, "objects") as mock_objects,
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=999),
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value={"status": "bound", "interface": "Te1/1/1", "port_id": 42},
            ),
        ):
            mock_tx.atomic = noop_atomic
            mock_module_cls.return_value = new_module
            mock_objects.select_for_update.return_value = mock_qs
            # Mismatched cache context triggers posted fallback path.
            mock_cache.get.return_value = {
                "librenms_id": 555,
                "inventory": [
                    {
                        "entPhysicalIndex": 77,
                        "_librenms_port_id": 42,
                        "_librenms_ifname": "Te1/1/1",
                    }
                ],
            }
            view.post(request, pk=24)

        assert any(
            "Interface identity fallback used posted row metadata" in str(call)
            for call in mock_messages.warning.call_args_list
        )

    def test_install_module_view_binds_interface_after_install(self):
        from contextlib import contextmanager

        from dcim.models import ModuleBay

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        view = object.__new__(InstallModuleView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="default")  # blank posted server_key falls back to this
        device = _make_device()

        module_bay = MagicMock()
        module_bay.name = "Slot 1"
        module_bay.installed_module = None

        module_type = MagicMock()
        module_type.pk = 5
        module_type.model = "SFP-10G-SR"

        new_module = MagicMock()
        new_module.pk = 321

        request = _make_request(
            "POST",
            data={
                "module_bay_id": "10",
                "module_type_id": "5",
                "serial": "SN1",
                "server_key": "production",
                "ent_index": "77",
            },
        )

        @contextmanager
        def noop_atomic():
            yield

        mock_qs = MagicMock()
        mock_qs.get.return_value = module_bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module_bay, module_type],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
            patch("dcim.models.Module") as mock_module_cls,
            patch.object(ModuleBay, "objects") as mock_objects,
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value={"status": "bound", "interface": "Te1/1/1", "port_id": 42, "changed": True},
            ) as mock_bind,
        ):
            mock_tx.atomic = noop_atomic
            mock_module_cls.return_value = new_module
            mock_objects.select_for_update.return_value = mock_qs
            mock_cache.get.return_value = {
                "inventory": [
                    {
                        "entPhysicalIndex": 77,
                        "_librenms_port_id": 42,
                        "_librenms_ifname": "Te1/1/1",
                    }
                ]
            }
            view.post(request, pk=24)

        mock_bind.assert_called_once()
        bind_call = mock_bind.call_args
        assert bind_call.args[0] is device
        assert bind_call.args[2] == 321
        assert bind_call.args[3] == "production"
        assert bind_call.args[1]["_librenms_port_id"] == 42
        mock_messages.info.assert_called()

    def test_install_module_view_binds_with_blank_server_key_via_client_fallback(self):
        """A blank posted server_key falls back to the resolved client server so the interface still binds.

        The install form posts server_key="{{ module_sync.server_key|default:'' }}", so a fallback
        render sends an empty server_key. Without LibreNMSAPIMixin + the `or self.librenms_api.server_key`
        fallback (like InstallBranchView), `if bind_item and server_key` was skipped and the module
        installed with its interface silently never bound. This pins the fallback.
        """
        from contextlib import contextmanager

        from dcim.models import ModuleBay

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        view = object.__new__(InstallModuleView)
        view.required_object_permissions = {}
        # No POST server_key → must fall back to the bound client's server ("default").
        view._librenms_api = MagicMock(server_key="default")
        device = _make_device()

        module_bay = MagicMock()
        module_bay.name = "Slot 1"
        module_bay.installed_module = None

        module_type = MagicMock()
        module_type.pk = 5
        module_type.model = "SFP-10G-SR"

        new_module = MagicMock()
        new_module.pk = 321

        request = _make_request(
            "POST",
            data={
                "module_bay_id": "10",
                "module_type_id": "5",
                "serial": "SN1",
                "server_key": "",  # blank — as a fallback render would post
                "ent_index": "77",
            },
        )

        @contextmanager
        def noop_atomic():
            yield

        mock_qs = MagicMock()
        mock_qs.get.return_value = module_bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module_bay, module_type],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
            patch("dcim.models.Module") as mock_module_cls,
            patch.object(ModuleBay, "objects") as mock_objects,
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules._get_sync_device_for_inventory", return_value=device),
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value={"status": "bound", "interface": "Te1/1/1", "port_id": 42},
            ) as mock_bind,
        ):
            mock_tx.atomic = noop_atomic
            mock_module_cls.return_value = new_module
            mock_objects.select_for_update.return_value = mock_qs
            mock_cache.get.return_value = {
                "inventory": [
                    {
                        "entPhysicalIndex": 77,
                        "_librenms_port_id": 42,
                        "_librenms_ifname": "Te1/1/1",
                    }
                ]
            }
            view.post(request, pk=24)

        # The bind ran under the fallback server key, not the blank posted one.
        mock_bind.assert_called_once()
        assert mock_bind.call_args.args[3] == "default"

    def test_install_module_view_rejects_missing_bay_id_for_interface_child(self):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        view = object.__new__(InstallModuleView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="default")  # blank posted server_key falls back to this
        device = _make_device()

        request = _make_request(
            "POST",
            data={
                "module_bay_id": "",
                "module_type_id": "5",
                "serial": "SN1",
                "server_key": "production",
                "ent_index": "77",
            },
        )

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.post(request, pk=24)

        mock_messages.error.assert_called_once()
        assert "invalid module bay/module type id" in mock_messages.error.call_args[0][1].lower()
        mock_redirect.assert_called_once()

    def test_update_module_interface_view_binds_existing_interface(self):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        device = _make_device()

        module = MagicMock()
        module.pk = 321
        module.module_type.model = "SFP-10G-SR"
        module.module_bay.name = "SFP 1"

        request = _make_request(
            "POST",
            data={
                "module_id": "321",
                "server_key": "production",
                "ent_index": "77",
            },
        )

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=999),
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                # Real _bind_interface_librenms_id always returns "changed" (bool(update_fields));
                # binding an unbound interface actually writes, so changed=True. The success message
                # now gates on it, so the mock must carry the field the production branch reads.
                return_value={"status": "bound", "interface": "Te1/1/1", "port_id": 42, "changed": True},
            ) as mock_bind,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="redirected"),
        ):
            mock_cache.get.return_value = {
                "inventory": [{"entPhysicalIndex": 77, "_librenms_port_id": 42, "_librenms_ifname": "Te1/1/1"}],
                "librenms_id": 999,
            }
            response = view.post(request, pk=24)

        mock_bind.assert_called_once()
        mock_messages.success.assert_called_once()
        assert response is not None

    def test_update_module_interface_view_adopts_template_interfaces_when_no_port_binding_exists(self):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        device = _make_device()

        module = MagicMock()
        module.pk = 321
        module.module_type.model = "Linecard-24x10G"
        module.module_bay.name = "Slot 1"

        request = _make_request(
            "POST",
            data={
                "module_id": "321",
                "server_key": "production",
                "ent_index": "77",
                "inventory_name": "Slot 1",
            },
        )

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=999),
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value=None,
            ) as mock_bind,
            patch(
                "netbox_librenms_plugin.views.sync.modules._adopt_existing_template_interfaces",
                return_value={"status": "bound", "adopted_count": 2, "interfaces": ["Te1/1/1", "Te1/1/2"]},
            ) as mock_adopt,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="redirected"),
        ):
            mock_cache.get.return_value = {
                "inventory": [{"entPhysicalIndex": 77, "entPhysicalName": "Slot 1"}],
                "librenms_id": 999,
            }
            response = view.post(request, pk=24)

        mock_bind.assert_called_once()
        mock_adopt.assert_called_once_with(device, module, "production")
        mock_messages.success.assert_called_once()
        assert "adopted 2 existing standalone interface(s)" in mock_messages.success.call_args[0][1]
        assert response is not None

    def test_replace_module_view_binds_interface_after_replace(self):
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        view = object.__new__(ReplaceModuleView)
        view.required_object_permissions = {}
        device = _make_device()

        target_bay = MagicMock()
        target_bay.name = "SFP 1"

        installed_module = MagicMock()
        installed_module.pk = 321
        installed_module.module_type.model = "OLD-SFP"
        installed_module.module_bay = target_bay

        matched_type = MagicMock()
        matched_type.model = "NEW-SFP"

        new_module = MagicMock()
        new_module.pk = 654

        request = _make_request(
            "POST",
            data={
                "module_id": "321",
                "ent_index": "77",
                "server_key": "production",
            },
        )

        @contextmanager
        def noop_atomic():
            yield

        installed_filter_qs = MagicMock()
        installed_filter_qs.select_related.return_value.first.return_value = installed_module

        conflict_filter_qs = MagicMock()
        conflict_filter_qs.exclude.return_value.select_related.return_value = []

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, installed_module],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="redirected"),
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_module_types_indexed",
                return_value={"NEW-SFP": matched_type},
            ),
            patch("netbox_librenms_plugin.utils.resolve_module_type", return_value=matched_type),
            patch(
                "netbox_librenms_plugin.views.sync.modules._count_adoptable_interfaces", return_value=2
            ) as mock_count,
            patch(
                "netbox_librenms_plugin.views.sync.modules._normalize_module_interface_names_for_vc_member",
                return_value={"renamed": 1, "adopted": 0, "removed": 0, "skipped": 0},
            ) as mock_normalize,
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value={"status": "bound", "interface": "Te1/1/1", "port_id": 42, "changed": True},
            ) as mock_bind,
            patch("dcim.models.Module") as mock_module_cls,
        ):
            mock_tx.atomic = noop_atomic
            mock_cache.get.return_value = {
                "inventory": [
                    {
                        "entPhysicalIndex": 77,
                        "entPhysicalModelName": "NEW-SFP",
                        "entPhysicalSerialNum": "SN-42",
                        "_librenms_port_id": 42,
                        "_librenms_ifname": "Te1/1/1",
                    }
                ]
            }
            mock_module_cls.return_value = new_module
            mock_module_cls.objects.select_related.return_value = MagicMock()
            mock_module_cls.objects.select_for_update.return_value.filter.side_effect = [
                installed_filter_qs,
                conflict_filter_qs,
            ]

            response = view.post(request, pk=24)

        mock_count.assert_called_once_with(device, new_module)
        assert new_module._adopt_components is True
        mock_normalize.assert_called_once_with(device, new_module)
        mock_bind.assert_called_once()
        bind_call = mock_bind.call_args
        assert bind_call.args[0] is device
        assert bind_call.args[1]["_librenms_port_id"] == 42
        assert bind_call.args[2] == 654
        assert bind_call.args[3] == "production"
        mock_messages.success.assert_called_once()
        warning_messages = [call.args[1] for call in mock_messages.warning.call_args_list]
        assert (
            "Module sync authority applied: adopted 2 existing standalone interface(s) into the module."
            in warning_messages
        )
        assert "VC member interface normalization applied: renamed 1." in warning_messages
        mock_messages.info.assert_called()
        assert response == "redirected"


class TestVCMemberInterfaceNormalization:
    """Covers VC member-aware module interface renaming/adoption helpers."""

    def test_rewrite_interface_name_for_vc_member(self):
        from netbox_librenms_plugin.views.sync.modules import _rewrite_interface_name_for_vc_member

        assert _rewrite_interface_name_for_vc_member("TenGigabitEthernet1/1/1", 3) == "TenGigabitEthernet3/1/1"
        assert _rewrite_interface_name_for_vc_member("Te1/0/24", 5) == "Te5/0/24"
        assert _rewrite_interface_name_for_vc_member("Port-channel10", 3) is None

    def test_rewrite_skips_when_prefix_number_not_known_member(self):
        from netbox_librenms_plugin.views.sync.modules import _rewrite_interface_name_for_vc_member

        result = _rewrite_interface_name_for_vc_member(
            "TenGigabitEthernet9/1/1",
            3,
            member_positions={1, 2, 3, 4},
        )
        assert result is None

    def test_normalize_skips_non_vc_device(self):
        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device = MagicMock()
        device.vc_position = None
        device.virtual_chassis_id = None

        result = _normalize_module_interface_names_for_vc_member(device, MagicMock())
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 0}

    def test_normalize_renames_module_interfaces_for_vc_member(self):
        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device = MagicMock()
        device.vc_position = 3
        device.virtual_chassis_id = 11
        device.virtual_chassis.members.values_list.return_value = [1, 2, 3]
        module = MagicMock()

        iface = MagicMock()
        iface.pk = 1
        iface.name = "TenGigabitEthernet1/1/1"

        module_qs = MagicMock()
        module_qs.order_by.return_value = [iface]

        conflict_qs = MagicMock()
        conflict_qs.exclude.return_value.first.return_value = None

        with patch("dcim.models.Interface") as mock_interface:
            mock_interface.objects.filter.side_effect = [module_qs, conflict_qs]
            result = _normalize_module_interface_names_for_vc_member(device, module)

        assert result["renamed"] == 1
        assert iface.name == "TenGigabitEthernet3/1/1"
        iface.save.assert_called_once_with(update_fields=["name"])

    def test_normalize_adopts_existing_standalone_conflict(self):
        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device = MagicMock()
        device.vc_position = 5
        device.virtual_chassis_id = 44
        device.virtual_chassis.members.values_list.return_value = [1, 2, 5]
        module = MagicMock()

        created_iface = MagicMock()
        created_iface.pk = 5
        created_iface.name = "TenGigabitEthernet1/1/1"

        existing_iface = MagicMock()
        existing_iface.module_id = None

        module_qs = MagicMock()
        module_qs.order_by.return_value = [created_iface]

        conflict_qs = MagicMock()
        conflict_qs.exclude.return_value.first.return_value = existing_iface

        with patch("dcim.models.Interface") as mock_interface:
            mock_interface.objects.filter.side_effect = [module_qs, conflict_qs]
            result = _normalize_module_interface_names_for_vc_member(device, module)

        assert result["adopted"] == 1
        assert result["removed"] == 1
        existing_iface.save.assert_called_once_with(update_fields=["module"])
        created_iface.delete.assert_called_once()

    def test_normalize_skips_non_member_prefixed_interfaces(self):
        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device = MagicMock()
        device.vc_position = 3
        device.virtual_chassis_id = 11
        device.virtual_chassis.members.values_list.return_value = [1, 2, 3]
        module = MagicMock()

        iface = MagicMock()
        iface.pk = 1
        iface.name = "TenGigabitEthernet9/1/1"

        module_qs = MagicMock()
        module_qs.order_by.return_value = [iface]

        with patch("dcim.models.Interface") as mock_interface:
            mock_interface.objects.filter.side_effect = [module_qs]
            result = _normalize_module_interface_names_for_vc_member(device, module)

        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 0}
        iface.save.assert_not_called()


class TestResolveTargetDevice:
    """Target device selection must remain constrained to VC membership."""

    def test_non_vc_device_ignores_selected_member(self):
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device

        page_device = MagicMock()
        page_device.virtual_chassis = None

        result = _resolve_target_device(page_device, "123")

        assert result is page_device

    def test_vc_member_selection_accepts_valid_member(self):
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device

        page_device = MagicMock()
        member = MagicMock()
        page_device.virtual_chassis.members.filter.return_value.first.return_value = member

        result = _resolve_target_device(page_device, "55")

        assert result is member

    def test_vc_member_selection_falls_back_to_page_device(self):
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device

        page_device = MagicMock()
        page_device.virtual_chassis.members.filter.return_value.first.return_value = None

        result = _resolve_target_device(page_device, "55")

        assert result is page_device

    def test_invalid_selected_device_id_falls_back(self):
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device

        page_device = MagicMock()

        result = _resolve_target_device(page_device, "not-an-int")

        assert result is page_device

    def test_resolve_target_device_with_validation_marks_invalid_for_non_vc_selection(self):
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device_with_validation

        page_device = MagicMock()
        page_device.pk = 7
        page_device.virtual_chassis = None

        resolved, invalid = _resolve_target_device_with_validation(page_device, "123")

        assert resolved is page_device
        assert invalid is True

    def test_resolve_target_device_with_validation_accepts_page_device_id(self):
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device_with_validation

        page_device = MagicMock()
        page_device.pk = 7

        resolved, invalid = _resolve_target_device_with_validation(page_device, "7")

        assert resolved is page_device
        assert invalid is False


class TestNoBayWarningCopy:
    """No-bay warning for interface children should explain the missing NetBox bay."""

    def test_interface_child_warning_mentions_missing_child_bay(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        msg = BaseModuleTableView._build_no_bay_warning(
            {"entPhysicalClass": "port"},
            {},
            scope_empty_installed_bays=True,
        )
        assert "matching child bay is missing in netbox" in msg.lower()
        assert "modulebaymapping" in msg.lower()


# ---------------------------------------------------------------------------
# Regression: ToggleColumn accessor for per-row checkboxes
# ---------------------------------------------------------------------------


class TestToggleColumnAccessor:
    """ToggleColumn must have accessor='ent_physical_index' so per-row checkboxes render."""

    def test_selection_column_has_correct_accessor(self):
        """Regression: without accessor='ent_physical_index' checkboxes are empty."""
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        col = LibreNMSModuleTable.base_columns["selection"]
        assert col.accessor == "ent_physical_index", (
            "ToggleColumn must use accessor='ent_physical_index'; "
            "otherwise the column value resolves to '' and render() is never called"
        )

    def test_selection_column_renders_checkbox_for_record_with_index(self):
        """Per-row checkbox renders when ent_physical_index is present in record."""
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        record = {
            "ent_physical_index": 42,
            "name": "Slot 1",
            "model": "WS-X4748",
            "depth": 0,
        }
        table = LibreNMSModuleTable([record])
        rows = list(table.rows)
        assert len(rows) == 1
        # The cell value should render a checkbox HTML input element
        cell_val = rows[0].get_cell("selection")
        assert "<input" in str(cell_val), (
            "Checkbox cell must render an HTML input element for a record with ent_physical_index"
        )


# ---------------------------------------------------------------------------
# Regression: ancestor walk skips containers with N/A model (Cisco 8201 style)
# ---------------------------------------------------------------------------


class TestAncestorWalkGenericContainerModel:
    """Top-level items under containers with 'N/A' model should not be excluded."""

    @staticmethod
    def _run_top_items(inventory_data):
        """Delegate to the real implementation so tests validate actual behavior."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        index_map = {
            item["entPhysicalIndex"]: item for item in inventory_data if item.get("entPhysicalIndex") is not None
        }
        return BaseModuleTableView._collect_top_items(
            inventory_data,
            index_map,
            ignore_rules=[],
            device_serial="",
            transparent_indices=set(),
            ignore_cache={},
        )

    def test_item_under_container_with_na_model_is_top_level(self):
        """Module under a container with model='N/A' must appear as top-level item."""
        inventory = [
            # chassis (not in INVENTORY_CLASSES, so ignored in ancestor walk)
            {
                "entPhysicalIndex": 9000,
                "entPhysicalClass": "chassis",
                "entPhysicalModelName": "8201-SYS",
                "entPhysicalContainedIn": 0,
            },
            # container with model='N/A' inside chassis — generic slot
            {
                "entPhysicalIndex": 8000,
                "entPhysicalClass": "container",
                "entPhysicalModelName": "N/A",
                "entPhysicalContainedIn": 9000,
            },
            # real module inside the N/A container — should be top-level
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "8201-SYS",
                "entPhysicalContainedIn": 8000,
            },
        ]
        top = self._run_top_items(inventory)
        indices = [i["entPhysicalIndex"] for i in top]
        assert 1 in indices, "Module inside N/A container must be a top-level item (Cisco 8201 regression)"

    def test_item_under_container_with_empty_model_is_top_level(self):
        """Legacy: module under container with empty model still works."""
        inventory = [
            {
                "entPhysicalIndex": 9000,
                "entPhysicalClass": "chassis",
                "entPhysicalModelName": "8201-SYS",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 8000,
                "entPhysicalClass": "container",
                "entPhysicalModelName": "",
                "entPhysicalContainedIn": 9000,
            },
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "8201-SYS",
                "entPhysicalContainedIn": 8000,
            },
        ]
        top = self._run_top_items(inventory)
        indices = [i["entPhysicalIndex"] for i in top]
        assert 1 in indices

    def test_item_under_real_module_is_excluded(self):
        """Module inside another real (non-generic) module stays a descendant."""
        inventory = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "PARENT-MODULE",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 2,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "CHILD-MODULE",
                "entPhysicalContainedIn": 1,
            },
        ]
        top = self._run_top_items(inventory)
        indices = [i["entPhysicalIndex"] for i in top]
        assert 1 in indices
        assert 2 not in indices, "Child module under real parent must remain a descendant"


# ---------------------------------------------------------------------------
# Regression: parent_row_idx (table index) must not alias entPhysicalIndex
# ---------------------------------------------------------------------------


class TestParentRowIdxVsEntityIndex:
    """
    Regression: parent_row_idx must be used for table_data access, not parent_ent_idx.

    Bug: parent_idx was first set to len(table_data) (a small row index), then
    overwritten with item.get("entPhysicalIndex") (which can be millions).
    table_data[parent_idx] then indexed the list with the large entity value,
    causing IndexError or wrong-row mutations.
    """

    def test_has_installable_children_set_on_correct_row(self):
        """has_installable_children must land on table row 0, not on entity index 8_000_000."""
        import importlib
        from unittest.mock import MagicMock, patch

        mod = importlib.import_module("netbox_librenms_plugin.views.base.modules_view")
        BaseModuleTableView = mod.BaseModuleTableView

        LARGE_IDX = 8_000_000  # >> any table_data list length
        CHILD_IDX = 8_000_001

        inventory = [
            {
                "entPhysicalIndex": LARGE_IDX,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "BIG-MODULE",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "SN1",
                "entPhysicalName": "Big Module",
            },
            {
                "entPhysicalIndex": CHILD_IDX,
                "entPhysicalClass": "port",
                "entPhysicalModelName": "SFP-X",
                "entPhysicalContainedIn": LARGE_IDX,
                "entPhysicalSerialNum": "SN2",
                "entPhysicalName": "Port 1",
            },
        ]

        view = object.__new__(BaseModuleTableView)
        view._device_manufacturer = None
        view._librenms_api = MagicMock(server_key="test-server")

        captured_table_data = []

        def fake_build_row(
            item,
            index_map,
            bays,
            module_types,
            depth=0,
            manufacturer=None,
            sibling_counts=None,
            scope_uninstalled=False,
            scope_preserved=False,
            scope_empty_installed_bays=False,
        ):
            if item.get("entPhysicalIndex") == LARGE_IDX:
                return {"ent_physical_index": LARGE_IDX, "can_install": False, "depth": 0}
            # child returns can_install=True to trigger the has_installable_children path
            return {"ent_physical_index": CHILD_IDX, "can_install": True, "depth": 1}

        def fake_get_table(table_data, obj):
            captured_table_data.extend(table_data)
            return MagicMock()

        request = MagicMock()
        obj = MagicMock()
        obj.device_type.manufacturer = None

        with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping:
            mock_mapping.objects.all.return_value = []
            with patch("netbox_librenms_plugin.models.InventoryIgnoreRule") as mock_ignore:
                mock_ignore.objects.filter.return_value.order_by.return_value = []
                with patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}):
                    with patch.object(view, "_get_module_bays", return_value=({}, {})):
                        with patch.object(view, "_get_module_types", return_value={}):
                            with patch.object(view, "_get_generic_module_types", return_value={}):
                                with patch.object(view, "_get_module_type_ambiguities", return_value={}):
                                    with patch.object(view, "_get_carrier_install_rules", return_value=[]):
                                        with patch.object(view, "_build_row", side_effect=fake_build_row):
                                            with patch.object(view, "get_table", side_effect=fake_get_table):
                                                with patch.object(
                                                    view, "_sort_with_hierarchy", side_effect=lambda x: x
                                                ):
                                                    with patch(
                                                        "netbox_librenms_plugin.views.base.modules_view.cache"
                                                    ) as mock_cache:
                                                        mock_cache.ttl = lambda k: None
                                                        # Old bug: IndexError when large entity index used as list index
                                                        view._build_context(request, obj, inventory)

        assert len(captured_table_data) >= 1, "table_data must contain the parent row"
        assert captured_table_data[0].get("has_installable_children") is True, (
            "has_installable_children must be set on table row 0 (parent_row_idx), "
            "not at entity index 8_000_000 which would cause IndexError"
        )


# ---------------------------------------------------------------------------
# Regression: install views must NOT delete the LibreNMS inventory cache
# ---------------------------------------------------------------------------


class TestInstallViewsDoNotDeleteCache:
    """
    Install views must not call cache.delete after a successful install.

    The LibreNMS inventory cache stores what LibreNMS reports (hardware list).
    It is unaffected by NetBox module installs; _get_module_bays() is a live DB
    query so the next render correctly shows the "Installed" state without any
    cache invalidation.  Deleting the cache after install caused an empty modules
    tab (regression).

    Each test exercises the view's success path and asserts that cache.delete
    was never called.
    """

    def test_install_module_view_no_cache_delete(self):
        """InstallModuleView.post success path must not call cache.delete."""
        from contextlib import contextmanager

        from dcim.models import ModuleBay

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        view = object.__new__(InstallModuleView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="default")  # blank posted server_key falls back to this
        device = _make_device()

        module_bay = MagicMock()
        module_bay.name = "Slot 1"
        module_bay.installed_module = None

        module_type = MagicMock()
        module_type.pk = 5
        module_type.model = "XCM-7s"

        new_module = MagicMock()
        request = _make_request("POST", data={"module_bay_id": "10", "module_type_id": "5", "serial": "SN1"})

        @contextmanager
        def noop_atomic():
            yield

        mock_qs = MagicMock()
        mock_qs.get.return_value = module_bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module_bay, module_type],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
            patch("dcim.models.Module") as mock_module_cls,
            patch.object(ModuleBay, "objects") as mock_objects,
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
        ):
            mock_tx.atomic = noop_atomic
            mock_module_cls.return_value = new_module
            mock_objects.select_for_update.return_value = mock_qs
            view.post(request, pk=24)

        mock_messages.success.assert_called_once()
        mock_cache.delete.assert_not_called()

    def test_install_branch_view_no_cache_delete(self):
        """InstallBranchView.post success path must not call cache.delete."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = object.__new__(InstallBranchView)
        view.required_object_permissions = {}
        device = _make_device()

        request = _make_request("POST", data={"parent_index": "100", "server_key": "default"})

        cached_inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "MOD-A",
                "entPhysicalContainedIn": 0,
                "entPhysicalName": "Slot 0",
            },
        ]

        install_result = {"status": "installed", "name": "Slot 0"}

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(InstallBranchView, "_collect_branch", return_value=cached_inventory),
            patch.object(InstallBranchView, "_install_single", return_value=install_result),
            patch("netbox_librenms_plugin.views.sync.modules.get_module_types_indexed", return_value={}),
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
        ):
            mock_cache.get.return_value = {"inventory": cached_inventory, "librenms_id": "test"}
            mock_tx.atomic = lambda *a, **kw: MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))
            view.post(request, pk=24)

        mock_messages.success.assert_called_once()
        mock_cache.delete.assert_not_called()

    def test_install_selected_view_no_cache_delete(self):
        """InstallSelectedView.post success path must not call cache.delete."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, InstallSelectedView

        view = object.__new__(InstallSelectedView)
        view.required_object_permissions = {}
        device = _make_device()

        request = _make_request("POST", data={"server_key": "default"})
        post_mock = MagicMock()
        post_mock.get = MagicMock(side_effect=lambda k, d=None: {"server_key": "default"}.get(k, d))
        post_mock.getlist = MagicMock(return_value=["100"])
        request.POST = post_mock

        cached_inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "MOD-A",
                "entPhysicalContainedIn": 0,
                "entPhysicalName": "Slot 0",
            },
        ]

        install_result = {"status": "installed", "name": "Slot 0"}

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(InstallBranchView, "_install_single", return_value=install_result),
            patch("netbox_librenms_plugin.views.sync.modules.get_module_types_indexed", return_value={}),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
        ):
            mock_cache.get.return_value = {"inventory": cached_inventory, "librenms_id": "test"}
            mock_tx.atomic = lambda *a, **kw: MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))
            view.post(request, pk=24)

        mock_messages.success.assert_called_once()
        mock_cache.delete.assert_not_called()

    def test_install_selected_success_path_uses_htmx_partial_swap(self):
        """B1: InstallSelectedView's success path swaps the HTMX partial, not a full-page HX-Redirect (matches InstallBranchView + its own early exits)."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, InstallSelectedView

        view = object.__new__(InstallSelectedView)
        view.required_object_permissions = {}
        view.has_write_permission = MagicMock(return_value=True)
        device = _make_device()

        request = _make_request("POST", data={"server_key": "default"})
        post_mock = MagicMock()
        post_mock.get = MagicMock(side_effect=lambda k, d=None: {"server_key": "default"}.get(k, d))
        post_mock.getlist = MagicMock(return_value=["100"])
        request.POST = post_mock

        cached_inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "MOD-A",
                "entPhysicalContainedIn": 0,
                "entPhysicalName": "Slot 0",
            },
        ]
        install_result = {"status": "installed", "name": "Slot 0"}

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(InstallBranchView, "_install_single", return_value=install_result),
            patch("netbox_librenms_plugin.views.sync.modules.get_module_types_indexed", return_value={}),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch(
                "netbox_librenms_plugin.views.sync.modules._render_modules_partial_after_action",
                return_value="PARTIAL",
            ) as mock_partial,
            patch(
                "netbox_librenms_plugin.views.sync.modules._modules_redirect_response",
                return_value="REDIRECT",
            ) as mock_redirect,
        ):
            mock_cache.get.return_value = {"inventory": cached_inventory, "librenms_id": "test"}
            mock_tx.atomic = lambda *a, **kw: MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))
            result = view.post(request, pk=24)

        assert result == "PARTIAL"  # not the full-page HX-Redirect
        mock_partial.assert_called_once()
        mock_redirect.assert_not_called()

    def test_install_branch_rejects_stale_cached_inventory_context(self):
        """Branch install should fail closed when cached inventory librenms_id mismatches target device context."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = object.__new__(InstallBranchView)
        view.required_object_permissions = {}
        device = _make_device()

        request = _make_request("POST", data={"parent_index": "100", "server_key": "default"})

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=999),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
            patch.object(InstallBranchView, "_collect_branch") as mock_collect,
        ):
            mock_cache.get.return_value = {"inventory": [{"entPhysicalIndex": 100}], "librenms_id": 555}
            view.post(request, pk=24)

        mock_collect.assert_not_called()
        mock_messages.error.assert_called_once()

    def test_install_selected_rejects_stale_cached_inventory_context(self):
        """Selected install should fail closed when cached inventory context mismatches device librenms_id."""
        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        view = object.__new__(InstallSelectedView)
        view.required_object_permissions = {}
        device = _make_device()

        request = _make_request("POST", data={"server_key": "default"})
        post_mock = MagicMock()
        post_mock.get = MagicMock(side_effect=lambda k, d=None: {"server_key": "default"}.get(k, d))
        post_mock.getlist = MagicMock(return_value=["100"])
        request.POST = post_mock

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=999),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
            patch("netbox_librenms_plugin.views.sync.modules.InstallBranchView._install_single") as mock_install,
        ):
            mock_cache.get.return_value = {"inventory": [{"entPhysicalIndex": 100}], "librenms_id": 555}
            view.post(request, pk=24)

        mock_install.assert_not_called()
        mock_messages.error.assert_called_once()

    def test_install_branch_skipped_rows_do_not_bind_interfaces(self):
        """Skipped branch rows must not trigger interface binding side effects."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = object.__new__(InstallBranchView)
        view.required_object_permissions = {}
        device = _make_device()

        request = _make_request("POST", data={"parent_index": "100", "server_key": "default"})
        cached_inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "MOD-A",
                "entPhysicalContainedIn": 0,
                "entPhysicalName": "Slot 0",
            },
        ]
        install_result = {"status": "skipped", "name": "Slot 0", "reason": "no matching bay", "module_pk": 999}

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(InstallBranchView, "_collect_branch", return_value=cached_inventory),
            patch.object(InstallBranchView, "_install_single", return_value=install_result),
            patch("netbox_librenms_plugin.views.sync.modules.get_module_types_indexed", return_value={}),
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch("netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id") as mock_bind,
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
        ):
            mock_cache.get.return_value = {"inventory": cached_inventory, "librenms_id": "test"}
            mock_tx.atomic = lambda: MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))
            view.post(request, pk=24)

        mock_bind.assert_not_called()

    def test_install_selected_skipped_rows_do_not_bind_interfaces(self):
        """Skipped selected rows must not trigger interface binding side effects."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, InstallSelectedView

        view = object.__new__(InstallSelectedView)
        view.required_object_permissions = {}
        device = _make_device()

        request = _make_request("POST", data={"server_key": "default"})
        post_mock = MagicMock()
        post_mock.get = MagicMock(side_effect=lambda k, d=None: {"server_key": "default"}.get(k, d))
        post_mock.getlist = MagicMock(return_value=["100"])
        request.POST = post_mock

        cached_inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "MOD-A",
                "entPhysicalContainedIn": 0,
                "entPhysicalName": "Slot 0",
            },
        ]
        install_result = {"status": "skipped", "name": "Slot 0", "reason": "no matching bay", "module_pk": 999}

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(InstallBranchView, "_install_single", return_value=install_result),
            patch("netbox_librenms_plugin.views.sync.modules.get_module_types_indexed", return_value={}),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch("netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id") as mock_bind,
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
        ):
            mock_cache.get.return_value = {"inventory": cached_inventory, "librenms_id": "test"}
            mock_tx.atomic = lambda: MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))
            view.post(request, pk=24)

        mock_bind.assert_not_called()

    def test_install_branch_occupied_rows_attempt_bind_interfaces(self):
        """When bay is already occupied and module_pk is known, branch install still attempts bind."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = object.__new__(InstallBranchView)
        view.required_object_permissions = {}
        device = _make_device()

        request = _make_request("POST", data={"parent_index": "100", "server_key": "default"})
        cached_inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "MOD-A",
                "entPhysicalContainedIn": 0,
                "entPhysicalName": "Slot 0",
                "_librenms_port_id": 42,
            },
        ]
        install_result = {
            "status": "skipped",
            "name": "Slot 0",
            "reason": "bay already occupied",
            "module_pk": 999,
        }

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(InstallBranchView, "_collect_branch", return_value=cached_inventory),
            patch.object(InstallBranchView, "_install_single", return_value=install_result),
            patch("netbox_librenms_plugin.views.sync.modules.get_module_types_indexed", return_value={}),
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value={"status": "bound"},
            ) as mock_bind,
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
        ):
            mock_cache.get.return_value = {"inventory": cached_inventory, "librenms_id": "test"}
            mock_tx.atomic = lambda: MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))
            view.post(request, pk=24)

        mock_bind.assert_called_once()

    def test_install_selected_occupied_rows_attempt_bind_interfaces(self):
        """When selected install hits occupied bay with module_pk, binding still runs."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, InstallSelectedView

        view = object.__new__(InstallSelectedView)
        view.required_object_permissions = {}
        device = _make_device()

        request = _make_request("POST", data={"server_key": "default"})
        post_mock = MagicMock()
        post_mock.get = MagicMock(side_effect=lambda k, d=None: {"server_key": "default"}.get(k, d))
        post_mock.getlist = MagicMock(return_value=["100"])
        request.POST = post_mock

        cached_inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": "MOD-A",
                "entPhysicalContainedIn": 0,
                "entPhysicalName": "Slot 0",
                "_librenms_port_id": 42,
            },
        ]
        install_result = {
            "status": "skipped",
            "name": "Slot 0",
            "reason": "bay already occupied",
            "module_pk": 999,
        }

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(InstallBranchView, "_install_single", return_value=install_result),
            patch("netbox_librenms_plugin.views.sync.modules.get_module_types_indexed", return_value={}),
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value={"status": "bound"},
            ) as mock_bind,
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
        ):
            mock_cache.get.return_value = {"inventory": cached_inventory, "librenms_id": "test"}
            mock_tx.atomic = lambda: MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))
            view.post(request, pk=24)

        mock_bind.assert_called_once()


# ---------------------------------------------------------------------------
# _collect_children with ignore rules (Item 4)
# ---------------------------------------------------------------------------


def _make_rule(pattern="IDPROM", action="skip"):
    """Create a lightweight InventoryIgnoreRule-like object for testing."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    rule = InventoryIgnoreRule.__new__(InventoryIgnoreRule)
    rule.match_type = "ends_with"
    rule.pattern = pattern
    rule.action = action
    rule.require_serial_match_parent = False
    rule.enabled = True
    return rule


class TestCollectChildrenIgnoreRules:
    """_collect_children respects ignore rules when provided."""

    def test_skip_rule_excludes_item_and_subtree(self):
        """Child matching a 'skip' rule is excluded along with its descendants."""
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0},
            {
                "entPhysicalIndex": 2,
                "entPhysicalModelName": "SKIP-ME",
                "entPhysicalName": "FT-IDPROM",
                "entPhysicalContainedIn": 1,
            },
            {
                "entPhysicalIndex": 3,
                "entPhysicalModelName": "DEEP-SKIP",
                "entPhysicalName": "DEEP",
                "entPhysicalContainedIn": 2,
            },
            {
                "entPhysicalIndex": 4,
                "entPhysicalModelName": "KEEP-ME",
                "entPhysicalName": "NormalChild",
                "entPhysicalContainedIn": 1,
            },
        ]
        index_map = {i["entPhysicalIndex"]: i for i in inventory}
        rule = _make_rule(pattern="IDPROM", action="skip")
        items = []
        view._collect_children(
            1, inventory, items, visited={1}, ignore_rules=[rule], device_serial="", index_map=index_map
        )
        indices = [i["entPhysicalIndex"] for i in items]
        assert 2 not in indices  # skip rule matched
        assert 3 not in indices  # descendant of skip-matched
        assert 4 in indices  # not matched

    def test_transparent_rule_collects_children_not_item(self):
        """Child matching a 'transparent' rule is excluded but its children are collected."""
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0},
            {
                "entPhysicalIndex": 2,
                "entPhysicalModelName": "TRANSPARENT",
                "entPhysicalName": "T-IDPROM",
                "entPhysicalContainedIn": 1,
            },
            {
                "entPhysicalIndex": 3,
                "entPhysicalModelName": "GRANDCHILD",
                "entPhysicalName": "GrandChild",
                "entPhysicalContainedIn": 2,
            },
        ]
        index_map = {i["entPhysicalIndex"]: i for i in inventory}
        rule = _make_rule(pattern="IDPROM", action="transparent")
        items = []
        view._collect_children(
            1, inventory, items, visited={1}, ignore_rules=[rule], device_serial="", index_map=index_map
        )
        indices = [i["entPhysicalIndex"] for i in items]
        assert 2 not in indices  # transparent item excluded
        assert 3 in indices  # grandchild promoted

    def test_no_ignore_rules_includes_all(self):
        """Passing ignore_rules=None preserves existing behaviour (all items collected)."""
        view = _make_install_branch_view()
        inventory = [
            {"entPhysicalIndex": 1, "entPhysicalModelName": "PARENT", "entPhysicalContainedIn": 0},
            {
                "entPhysicalIndex": 2,
                "entPhysicalModelName": "CHILD-IDPROM",
                "entPhysicalName": "X-IDPROM",
                "entPhysicalContainedIn": 1,
            },
        ]
        items = []
        view._collect_children(1, inventory, items, visited={1})
        assert any(i["entPhysicalIndex"] == 2 for i in items)


# ---------------------------------------------------------------------------
# _find_parent_module_id with regex mappings (Item 6)
# ---------------------------------------------------------------------------


class TestFindParentModuleIdRegex:
    """_find_parent_module_id applies regex ModuleBayMappings in ancestor walk."""

    def _make_bay(self, name, installed_module_id=None):
        bay = MagicMock()
        bay.name = name
        if installed_module_id is not None:
            bay.installed_module = MagicMock()
            bay.installed_module.pk = installed_module_id
        else:
            bay.installed_module = None
        return bay

    def _make_regex_mapping(self, pattern, netbox_bay_name):
        m = MagicMock()
        m.is_regex = True
        m.librenms_name = pattern
        m.netbox_bay_name = netbox_bay_name
        m.librenms_class = ""
        m._compiled_pattern = re.compile(pattern)
        return m

    def _make_exact_mapping(self, librenms_name, netbox_bay_name):
        m = MagicMock()
        m.is_regex = False
        m.librenms_name = librenms_name
        m.netbox_bay_name = netbox_bay_name
        m.librenms_class = ""
        return m

    def test_regex_mapping_matches_ancestor_name(self):
        """A regex mapping on an ancestor name resolves to the installed module id."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        parent = {
            "entPhysicalIndex": 10,
            "entPhysicalName": "Slot 3/0",
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 0,
        }
        child = {"entPhysicalIndex": 20, "entPhysicalModelName": "SFP-X", "entPhysicalContainedIn": 10}
        index_map = {10: parent, 20: child}

        bay = self._make_bay("Slot3", installed_module_id=77)
        regex_mapping = self._make_regex_mapping(r"Slot \d+/\d+", "Slot3")
        device_bays = [bay]

        result = InstallBranchView._find_parent_module_id(child, index_map, device_bays, [], [regex_mapping])
        assert result == 77

    def test_exact_mapping_still_works(self):
        """Exact mappings continue to work alongside regex mappings."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        parent = {
            "entPhysicalIndex": 10,
            "entPhysicalName": "ExactSlot",
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 0,
        }
        child = {"entPhysicalIndex": 20, "entPhysicalModelName": "MOD-A", "entPhysicalContainedIn": 10}
        index_map = {10: parent, 20: child}

        bay = self._make_bay("Bay-1", installed_module_id=42)
        exact_mapping = self._make_exact_mapping("ExactSlot", "Bay-1")
        device_bays = [bay]

        result = InstallBranchView._find_parent_module_id(child, index_map, device_bays, [exact_mapping], [])
        assert result == 42

    def test_no_match_returns_none(self):
        """Returns None when no exact or regex mapping matches the ancestor."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        parent = {
            "entPhysicalIndex": 10,
            "entPhysicalName": "UnknownSlot",
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 0,
        }
        child = {"entPhysicalIndex": 20, "entPhysicalModelName": "MOD-B", "entPhysicalContainedIn": 10}
        index_map = {10: parent, 20: child}

        bay = self._make_bay("Bay-2", installed_module_id=99)
        regex_mapping = self._make_regex_mapping(r"Slot \d+", "Bay-2")
        device_bays = [bay]

        # "UnknownSlot" does not match pattern "Slot \d+"
        result = InstallBranchView._find_parent_module_id(child, index_map, device_bays, [], [regex_mapping])
        assert result is None


# ---------------------------------------------------------------------------
# _find_parent_module_id — cycle detection and ancestor walk (Item 16)
# ---------------------------------------------------------------------------


class TestFindParentModuleIdAncestorWalk:
    """_find_parent_module_id walks up the hierarchy and handles edge cases."""

    def _make_bay(self, name, installed_module_id=None):
        bay = MagicMock()
        bay.name = name
        if installed_module_id is not None:
            bay.installed_module = MagicMock()
            bay.installed_module.pk = installed_module_id
        else:
            bay.installed_module = None
        return bay

    def test_direct_parent_matches_bay_name(self):
        """Parent whose entPhysicalName matches a bay with an installed module."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        parent = {
            "entPhysicalIndex": 10,
            "entPhysicalName": "Slot 1",
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 0,
        }
        child = {"entPhysicalIndex": 20, "entPhysicalContainedIn": 10}
        index_map = {10: parent, 20: child}
        bay = self._make_bay("Slot 1", installed_module_id=55)

        result = InstallBranchView._find_parent_module_id(child, index_map, [bay], [], [])
        assert result == 55

    def test_parent_matched_by_descr_field(self):
        """Parent matched via entPhysicalDescr when entPhysicalName doesn't match."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        parent = {
            "entPhysicalIndex": 10,
            "entPhysicalName": "SomethingElse",
            "entPhysicalDescr": "Slot 1",
            "entPhysicalContainedIn": 0,
        }
        child = {"entPhysicalIndex": 20, "entPhysicalContainedIn": 10}
        index_map = {10: parent, 20: child}
        bay = self._make_bay("Slot 1", installed_module_id=66)

        result = InstallBranchView._find_parent_module_id(child, index_map, [bay], [], [])
        assert result == 66

    def test_grandparent_walk(self):
        """Walk two levels up to find an installed ancestor."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        grandparent = {
            "entPhysicalIndex": 1,
            "entPhysicalName": "FPC 0",
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 0,
        }
        parent = {
            "entPhysicalIndex": 10,
            "entPhysicalName": "Container",
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 1,
        }
        child = {"entPhysicalIndex": 20, "entPhysicalContainedIn": 10}
        index_map = {1: grandparent, 10: parent, 20: child}
        bay = self._make_bay("FPC 0", installed_module_id=33)

        result = InstallBranchView._find_parent_module_id(child, index_map, [bay], [], [])
        assert result == 33

    def test_cycle_returns_none(self):
        """Cycle in the hierarchy terminates without infinite loop."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        a = {"entPhysicalIndex": 1, "entPhysicalName": "A", "entPhysicalDescr": "", "entPhysicalContainedIn": 2}
        b = {"entPhysicalIndex": 2, "entPhysicalName": "B", "entPhysicalDescr": "", "entPhysicalContainedIn": 1}
        child = {"entPhysicalIndex": 3, "entPhysicalContainedIn": 1}
        index_map = {1: a, 2: b, 3: child}

        result = InstallBranchView._find_parent_module_id(child, index_map, [], [], [])
        assert result is None

    def test_missing_parent_index_returns_none(self):
        """ContainedIn references a non-existent index."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        child = {"entPhysicalIndex": 20, "entPhysicalContainedIn": 999}
        index_map = {20: child}

        result = InstallBranchView._find_parent_module_id(child, index_map, [], [], [])
        assert result is None

    def test_root_item_returns_none(self):
        """Item at root (containedIn=0) has no parent."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        root = {"entPhysicalIndex": 1, "entPhysicalContainedIn": 0}
        index_map = {1: root}

        result = InstallBranchView._find_parent_module_id(root, index_map, [], [], [])
        assert result is None

    def test_uninstalled_bay_is_skipped(self):
        """Bay matches parent name but has no installed module — walk continues to grandparent."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        grandparent = {
            "entPhysicalIndex": 5,
            "entPhysicalName": "Chassis",
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 0,
        }
        parent = {
            "entPhysicalIndex": 10,
            "entPhysicalName": "Slot 1",
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 5,
        }
        child = {"entPhysicalIndex": 20, "entPhysicalContainedIn": 10}
        index_map = {5: grandparent, 10: parent, 20: child}
        empty_bay = self._make_bay("Slot 1", installed_module_id=None)
        grandparent_bay = self._make_bay("Chassis", installed_module_id=99)

        result = InstallBranchView._find_parent_module_id(child, index_map, [empty_bay, grandparent_bay], [], [])
        assert result == 99


# ---------------------------------------------------------------------------
# _match_bay — exact mapping, regex mapping, and fallbacks (Item 16)
# ---------------------------------------------------------------------------


class TestMatchBayLogic:
    """InstallBranchView._match_bay matches items via mappings or name fallback."""

    def _make_exact_mapping(self, librenms_name, netbox_bay_name, librenms_class=""):
        m = MagicMock()
        m.is_regex = False
        m.librenms_name = librenms_name
        m.netbox_bay_name = netbox_bay_name
        m.librenms_class = librenms_class
        m.manufacturer_id = None
        return m

    def _make_regex_mapping(self, pattern, netbox_bay_name, librenms_class=""):
        import re

        m = MagicMock()
        m.is_regex = True
        m.librenms_name = pattern
        m.netbox_bay_name = netbox_bay_name
        m.librenms_class = librenms_class
        m._compiled_pattern = re.compile(pattern)
        m.manufacturer_id = None
        return m

    def test_exact_mapping_matches_parent_name(self):
        """Exact mapping on the parent container name."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        parent = {"entPhysicalIndex": 1, "entPhysicalName": "Rack 0-Slot 3", "entPhysicalContainedIn": 0}
        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "SFP-1",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 1,
        }
        index_map = {1: parent, 2: child}

        bay = MagicMock()
        bay.name = "Slot 3"
        module_bays = {"Slot 3": bay}
        exact = [self._make_exact_mapping("Rack 0-Slot 3", "Slot 3")]

        result = InstallBranchView._match_bay(child, index_map, module_bays, exact, [])
        assert result is bay

    def test_exact_mapping_matches_item_name(self):
        """Exact mapping on the item's own name (when parent has no name)."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        parent = {"entPhysicalIndex": 1, "entPhysicalName": "", "entPhysicalContainedIn": 0}
        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "PSU 0",
            "entPhysicalDescr": "",
            "entPhysicalClass": "powerSupply",
            "entPhysicalContainedIn": 1,
        }
        index_map = {1: parent, 2: child}

        bay = MagicMock()
        bay.name = "PSU-Slot-0"
        module_bays = {"PSU-Slot-0": bay}
        exact = [self._make_exact_mapping("PSU 0", "PSU-Slot-0")]

        result = InstallBranchView._match_bay(child, index_map, module_bays, exact, [])
        assert result is bay

    def test_exact_mapping_matches_interface_label_candidate(self):
        """Exact mapping may target ifDescr/ifName label candidates for port rows."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        parent = {"entPhysicalIndex": 1, "entPhysicalName": "", "entPhysicalContainedIn": 0}
        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Port-Unknown",
            "entPhysicalDescr": "",
            "entPhysicalClass": "port",
            "entPhysicalContainedIn": 1,
            "_librenms_ifdescr": "TenGigabitEthernet1/1/1",
        }
        index_map = {1: parent, 2: child}

        bay = MagicMock()
        bay.name = "SFP 1"
        module_bays = {"SFP 1": bay}
        exact = [self._make_exact_mapping("TenGigabitEthernet1/1/1", "SFP 1", librenms_class="port")]

        result = InstallBranchView._match_bay(child, index_map, module_bays, exact, [])
        assert result is bay

    def test_class_scoped_mapping_preferred(self):
        """Mapping with matching librenms_class preferred over classless mapping."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Fan Tray 1",
            "entPhysicalDescr": "",
            "entPhysicalClass": "fan",
            "entPhysicalContainedIn": 0,
        }
        index_map = {2: child}

        bay_generic = MagicMock()
        bay_generic.name = "FanGeneric"
        bay_fan = MagicMock()
        bay_fan.name = "Fan-1"
        module_bays = {"FanGeneric": bay_generic, "Fan-1": bay_fan}
        exact = [
            self._make_exact_mapping("Fan Tray 1", "FanGeneric", librenms_class=""),
            self._make_exact_mapping("Fan Tray 1", "Fan-1", librenms_class="fan"),
        ]

        result = InstallBranchView._match_bay(child, index_map, module_bays, exact, [])
        assert result is bay_fan

        # Order-independent: class-scoped mapping wins regardless of list order
        result_reversed = InstallBranchView._match_bay(child, index_map, module_bays, list(reversed(exact)), [])
        assert result_reversed is bay_fan

    def test_regex_mapping_with_backreference(self):
        """Regex mapping with capture group + backreference in netbox_bay_name."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Optics 0/0/0/5",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
        }
        index_map = {2: child}

        bay = MagicMock()
        bay.name = "Optics0/0/0/5"
        bay.module = None
        module_bays = {"Optics0/0/0/5": bay}
        regex = [self._make_regex_mapping(r"Optics (\d+/\d+/\d+/\d+)", r"Optics\1")]

        with patch(
            "netbox_librenms_plugin.views.base.modules_view.BaseModuleTableView._fpc_slot_matches", return_value=True
        ):
            result = InstallBranchView._match_bay(child, index_map, module_bays, [], regex)

        assert result is bay

    def test_vendor_two_segment_regex_does_not_grab_three_segment_path(self):
        """Regression for #59: a narrow 2-segment vendor regex must NOT swallow a 3-segment
        transceiver name; the more-specific 3-segment generic mapping must win.

        ``_lookup_regex_bay_mapping`` uses ``fullmatch``, so a pattern with only two
        slash-segments cannot consume a name with three.  Both mappings are passed in
        together (mirroring real callers that have already merged scoped + global
        regex mappings via ``_filter_mappings_by_manufacturer``).
        """
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Optics 0/0/5",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
        }
        index_map = {2: child}

        # The two-segment bay must NOT be selected for a three-segment LibreNMS name
        narrow_bay = MagicMock()
        narrow_bay.name = "Optics0/0"
        narrow_bay.module = None
        wide_bay = MagicMock()
        wide_bay.name = "Optics0/0/5"
        wide_bay.module = None
        module_bays = {"Optics0/0": narrow_bay, "Optics0/0/5": wide_bay}

        # Vendor-scoped narrow regex first (would mis-fire if iteration order
        # alone decided the winner) + generic wider regex second.
        narrow = self._make_regex_mapping(r"Optics (\d+/\d+)", r"Optics\1")
        narrow.manufacturer_id = 7  # manufacturer-scoped
        wide = self._make_regex_mapping(r"Optics (\d+/\d+/\d+)", r"Optics\1")
        wide.manufacturer_id = None  # global

        with patch(
            "netbox_librenms_plugin.views.base.modules_view.BaseModuleTableView._fpc_slot_matches", return_value=True
        ):
            result = InstallBranchView._match_bay(child, index_map, module_bays, [], [narrow, wide])

        assert result is wide_bay, "narrow 2-segment vendor regex must not grab a 3-segment transceiver path"

    def test_fallback_exact_name_match(self):
        """Falls back to direct name-in-bays-dict when no mappings match."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "0/FT0",
            "entPhysicalDescr": "",
            "entPhysicalClass": "fan",
            "entPhysicalContainedIn": 0,
        }
        index_map = {2: child}

        bay = MagicMock()
        bay.name = "0/FT0"
        module_bays = {"0/FT0": bay}

        with patch(
            "netbox_librenms_plugin.views.base.modules_view.BaseModuleTableView._match_bay_by_position",
            return_value=None,
        ):
            result = InstallBranchView._match_bay(child, index_map, module_bays, [], [])

        assert result is bay

    def test_fallback_exact_match_uses_interface_label_candidate(self):
        """Direct bay-name fallback should consider ifName/ifDescr candidates."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Unknown",
            "entPhysicalDescr": "",
            "entPhysicalClass": "port",
            "entPhysicalContainedIn": 0,
            "_librenms_ifname": "Te1/1/1",
        }
        index_map = {2: child}

        bay = MagicMock()
        bay.name = "Te1/1/1"
        bay.module = None
        module_bays = {"Te1/1/1": bay}

        with patch(
            "netbox_librenms_plugin.views.base.modules_view.BaseModuleTableView._match_bay_by_position",
            return_value=None,
        ):
            result = InstallBranchView._match_bay(child, index_map, module_bays, [], [])

        assert result is bay

    def test_no_match_returns_none(self):
        """Returns None when nothing matches."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Unknown",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
        }
        index_map = {2: child}

        with patch(
            "netbox_librenms_plugin.views.base.modules_view.BaseModuleTableView._match_bay_by_position",
            return_value=None,
        ):
            result = InstallBranchView._match_bay(child, index_map, {}, [], [])

        assert result is None

    def test_normalized_candidate_matches_when_rules_preloaded(self):
        """When module_bay normalization rules are supplied, _match_bay considers
        normalized candidate names too — mirroring the table/UI matcher so installs
        don't skip bays that appear matched in the UI."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Slot 0/1",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
        }
        index_map = {2: child}

        # Bay only matches the normalized form ("Slot 1"), not the raw name.
        bay = MagicMock()
        bay.name = "Slot 1"
        module_bays = {"Slot 1": bay}
        exact = [self._make_exact_mapping("Slot 1", "Slot 1")]

        # A normalization rule that strips the "0/" prefix: "Slot 0/1" -> "Slot 1".
        rule = MagicMock()
        rule.match_pattern = r"Slot 0/"
        rule.replacement = "Slot "
        norm_rules_bay = {("module_bay", None): [rule]}

        # Without rules the raw name doesn't match the mapping -> no bay.
        assert InstallBranchView._match_bay(child, index_map, module_bays, exact, []) is None

        # With preloaded rules the normalized candidate matches the mapping.
        result = InstallBranchView._match_bay(child, index_map, module_bays, exact, [], norm_rules_bay=norm_rules_bay)
        assert result is bay


# ---------------------------------------------------------------------------
# _lookup_regex_bay_mapping — stale resolved_bay regression (Item 1)
# ---------------------------------------------------------------------------


class TestLookupRegexBayMappingStaleResolvedBay:
    """Regression: a failed expand() must not use resolved_bay from a prior iteration."""

    def _make_mapping(self, pattern, netbox_bay_name):
        import re

        m = MagicMock()
        m._compiled_pattern = re.compile(pattern)
        m.netbox_bay_name = netbox_bay_name
        m.librenms_class = ""
        m.manufacturer_id = None
        return m

    def test_failed_expand_does_not_match_stale_bay(self):
        """If expand() raises, the stale resolved_bay from a prior mapping must not be used."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        # Mapping A: matches, expand succeeds, but bay name not in module_bays.
        # Defined for documentation but replaced by mapping_a_no_bay below.
        # Mapping B: matches, but expand raises IndexError (bad backreference)
        mapping_b = self._make_mapping(r"Port-\d+", r"Bay-\2")  # \2 doesn't exist

        # Wrap mapping_b's compiled pattern so we can confirm the loop actually
        # reached mapping_b (and it wasn't short-circuited by a stale match from A).
        original_fullmatch = mapping_b._compiled_pattern.fullmatch
        mapping_b._compiled_pattern = MagicMock()
        mapping_b._compiled_pattern.fullmatch.side_effect = original_fullmatch

        stale_bay = MagicMock()
        stale_bay.name = "Bay-5"
        module_bays = {"Bay-5": stale_bay}

        # Before the fix, mapping_a sets resolved_bay="Bay-5" (no match in bays? let's say it does),
        # then mapping_b fails expand but the outer `if match:` used stale resolved_bay="Bay-5".
        # To properly trigger the bug: mapping_a's expand result is NOT in module_bays,
        # then mapping_b's expand fails, and the stale value from A should NOT be used.
        mapping_a_no_bay = self._make_mapping(r"Port-(\d+)", r"NoSuchBay-\1")

        with patch.object(BaseModuleTableView, "_fpc_slot_matches", return_value=True):
            result = BaseModuleTableView._lookup_regex_bay_mapping(
                "Port-5", "", module_bays, [mapping_a_no_bay, mapping_b]
            )

        assert result is None, "Failed expand() must not fall through to stale resolved_bay"
        # Confirm the loop actually iterated to mapping_b — otherwise the regression
        # test would silently pass even if the bug were re-introduced via short-circuit.
        mapping_b._compiled_pattern.fullmatch.assert_called_once_with("Port-5")

    def test_successful_expand_matches_bay(self):
        """Happy path: expand succeeds and bay exists."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        mapping = self._make_mapping(r"Optics(\d+/\d+/\d+/\d+)", r"Optics\1")
        bay = MagicMock()
        bay.name = "Optics0/0/0/3"
        module_bays = {"Optics0/0/0/3": bay}

        with patch.object(BaseModuleTableView, "_fpc_slot_matches", return_value=True):
            result = BaseModuleTableView._lookup_regex_bay_mapping("Optics0/0/0/3", "", module_bays, [mapping])

        assert result is bay

    def test_no_match_returns_none(self):
        """No mapping matches the name."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        mapping = self._make_mapping(r"Slot-\d+", r"Bay-\1")
        result = BaseModuleTableView._lookup_regex_bay_mapping("Port-5", "", {}, [mapping])
        assert result is None

    def test_class_scoped_mapping_preferred(self):
        """Mappings with matching class are tried before classless fallback."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        # Class-scoped mapping (matches)
        m_class = self._make_mapping(r"Fan-(\d+)", r"FanBay-\1")
        m_class.librenms_class = "fan"
        # Classless mapping (would also match but lower priority)
        m_generic = self._make_mapping(r"Fan-(\d+)", r"GenericBay-\1")
        m_generic.librenms_class = ""

        fan_bay = MagicMock()
        fan_bay.name = "FanBay-3"
        module_bays = {"FanBay-3": fan_bay, "GenericBay-3": MagicMock()}

        with patch.object(BaseModuleTableView, "_fpc_slot_matches", return_value=True):
            result = BaseModuleTableView._lookup_regex_bay_mapping("Fan-3", "fan", module_bays, [m_generic, m_class])

        assert result is fan_bay


class TestFilterMappingsByManufacturer:
    """_filter_mappings_by_manufacturer scopes ModuleBayMapping entries by vendor."""

    @staticmethod
    def _mk(manufacturer_id):
        m = MagicMock()
        m.manufacturer_id = manufacturer_id
        return m

    def test_global_mapping_used_when_device_has_no_manufacturer(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        glob = self._mk(None)
        scoped = self._mk(7)
        result = BaseModuleTableView._filter_mappings_by_manufacturer([glob, scoped], None)
        assert result == [glob]

    def test_scoped_mapping_preferred_over_global_for_matching_vendor(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        glob = self._mk(None)
        scoped = self._mk(7)
        result = BaseModuleTableView._filter_mappings_by_manufacturer([glob, scoped], 7)
        assert result == [scoped, glob]

    def test_other_vendor_scoped_mapping_skipped(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        glob = self._mk(None)
        other = self._mk(99)
        result = BaseModuleTableView._filter_mappings_by_manufacturer([glob, other], 7)
        assert result == [glob]


# ---------------------------------------------------------------------------
# PK validation error paths (finding 14)
# ---------------------------------------------------------------------------


def _make_request(method="GET", data=None):
    req = MagicMock()
    req.method = method
    if method == "GET":
        req.GET = data or {}
    else:
        req.POST = data or {}
    return req


def _make_device(pk=24, name="test-device"):
    d = MagicMock()
    d.pk = pk
    d.name = name
    d.device_type = MagicMock()
    d.device_type.manufacturer = None
    return d


class TestPKValidationErrorPaths:
    """Views must reject non-numeric PK values with an error message and redirect."""

    # -- InstallModuleView.post: non-numeric module_bay_id -----------------

    def test_install_module_non_numeric_bay_id(self):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        view = object.__new__(InstallModuleView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="default")  # blank posted server_key falls back to this
        device = _make_device()
        request = _make_request(
            "POST",
            data={
                "module_bay_id": "not-a-number",
                "module_type_id": "5",
                "serial": "SN1",
            },
        )

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.post(request, pk=24)

        mock_msg.error.assert_called_once()
        assert "invalid" in mock_msg.error.call_args[0][1].lower()
        mock_redirect.assert_called_once()

    # -- InstallBranchView.post: non-numeric parent_index ------------------

    def test_install_branch_non_numeric_parent_index(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = object.__new__(InstallBranchView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        device = _make_device()
        request = _make_request(
            "POST",
            data={
                "parent_index": "abc",
            },
        )

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.post(request, pk=24)

        mock_msg.error.assert_called_once()
        assert "invalid" in mock_msg.error.call_args[0][1].lower()
        mock_redirect.assert_called_once()

    # -- UpdateModuleSerialView.post: non-numeric module_id ----------------

    def test_update_serial_non_numeric_module_id(self):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        view = object.__new__(UpdateModuleSerialView)
        view.required_object_permissions = {}
        device = _make_device()
        request = _make_request(
            "POST",
            data={
                "module_id": "xyz",
                "serial": "NEW-SN",
            },
        )

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.post(request, pk=24)

        mock_msg.error.assert_called_once()
        assert "invalid" in mock_msg.error.call_args[0][1].lower()
        mock_redirect.assert_called_once()

    # -- InstallSelectedView.post: non-numeric select values ---------------

    def test_install_selected_non_numeric_select(self):
        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        view = object.__new__(InstallSelectedView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        device = _make_device()
        # POST.getlist must return a list with non-numeric values
        request = _make_request("POST", data={})
        post_data = {}
        post_mock = MagicMock()
        post_mock.get = MagicMock(side_effect=lambda k, d=None: post_data.get(k, d))
        post_mock.getlist = MagicMock(return_value=["a", "b"])
        request.POST = post_mock

        cached = [{"entPhysicalIndex": 1, "entPhysicalModelName": "M1"}]

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch.object(view, "get_cache_key", return_value="ck"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            mock_cache.get.return_value = {"inventory": cached, "librenms_id": "test"}
            view.post(request, pk=24)

        mock_msg.error.assert_called_once()
        assert "invalid" in mock_msg.error.call_args[0][1].lower()
        mock_redirect.assert_called_once()


# ---------------------------------------------------------------------------
# Basic behavioral tests — InstallModuleView (finding 15)
# ---------------------------------------------------------------------------


class TestInstallModuleViewBehavior:
    """Behavioral tests for InstallModuleView.post happy and occupied paths."""

    def _view(self):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        v = object.__new__(InstallModuleView)
        v.required_object_permissions = {}
        v._librenms_api = MagicMock(server_key="default")  # blank posted server_key falls back to this
        return v

    def test_bay_already_occupied_warns(self):
        """POST where module_bay has installed_module produces a warning and redirects."""
        from contextlib import contextmanager

        from dcim.models import ModuleBay

        view = self._view()
        device = _make_device()

        module_bay = MagicMock()
        module_bay.name = "Slot 1"
        module_bay.installed_module = MagicMock()  # occupied

        module_type = MagicMock()
        module_type.pk = 5

        request = _make_request(
            "POST",
            data={
                "module_bay_id": "10",
                "module_type_id": "5",
                "serial": "SN1",
            },
        )

        @contextmanager
        def noop_atomic():
            yield

        mock_qs = MagicMock()
        mock_qs.get.return_value = module_bay  # locked re-fetch returns same occupied bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module_bay, module_type],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
            patch.object(ModuleBay, "objects") as mock_objects,
        ):
            mock_tx.atomic = noop_atomic
            mock_objects.select_for_update.return_value = mock_qs
            view.post(request, pk=24)

        mock_msg.warning.assert_called_once()
        assert "already has a module" in mock_msg.warning.call_args[0][1]
        mock_redirect.assert_called_once()

    def test_successful_install(self):
        """POST happy path: module is created and success message is shown."""
        from contextlib import contextmanager

        from dcim.models import ModuleBay

        view = self._view()
        device = _make_device()

        module_bay = MagicMock()
        module_bay.name = "Slot 1"
        module_bay.installed_module = None  # not occupied

        module_type = MagicMock()
        module_type.pk = 5
        module_type.model = "XCM-7s"

        new_module = MagicMock()

        request = _make_request(
            "POST",
            data={
                "module_bay_id": "10",
                "module_type_id": "5",
                "serial": "SN123",
            },
        )

        @contextmanager
        def noop_atomic():
            yield

        mock_qs = MagicMock()
        mock_qs.get.return_value = module_bay  # locked re-fetch returns same bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module_bay, module_type],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
            patch("dcim.models.Module") as mock_module_cls,
            patch.object(ModuleBay, "objects") as mock_objects,
        ):
            mock_tx.atomic = noop_atomic
            mock_module_cls.return_value = new_module
            mock_objects.select_for_update.return_value = mock_qs
            view.post(request, pk=24)

        new_module.full_clean.assert_called_once()
        new_module.save.assert_called_once()
        mock_msg.success.assert_called_once()
        assert "XCM-7s" in mock_msg.success.call_args[0][1]
        mock_redirect.assert_called_once()


# ---------------------------------------------------------------------------
# Basic behavioral tests — UpdateModuleSerialView (finding 15)
# ---------------------------------------------------------------------------


class TestUpdateModuleSerialViewBehavior:
    """Behavioral tests for UpdateModuleSerialView.post happy path."""

    def _view(self):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        v = object.__new__(UpdateModuleSerialView)
        v.required_object_permissions = {}
        return v

    def test_updates_serial_successfully(self):
        """POST with valid module_id and new serial updates the module and shows success."""
        from contextlib import contextmanager

        from dcim.models import Module

        view = self._view()
        device = _make_device()

        module = MagicMock()
        module.pk = 42
        module.serial = "OLD-SN"
        module.module_type = MagicMock()
        module.module_type.model = "XCM-7s"
        module.module_bay = MagicMock()
        module.module_bay.name = "Slot 1"

        request = _make_request(
            "POST",
            data={
                "module_id": "42",
                "serial": "NEW-SN",
            },
        )

        @contextmanager
        def noop_atomic():
            yield

        mock_qs = MagicMock()
        mock_qs.select_related.return_value.filter.return_value.first.return_value = module

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
            patch.object(Module, "objects") as mock_objects,
        ):
            mock_tx.atomic = noop_atomic
            mock_objects.select_for_update.return_value = mock_qs
            view.post(request, pk=24)

        assert module.serial == "NEW-SN"
        module.full_clean.assert_called_once()
        module.save.assert_called_once()
        mock_msg.success.assert_called_once()
        assert "NEW-SN" in mock_msg.success.call_args[0][1]
        mock_redirect.assert_called_once()


# =============================================================================
# TestBuildTableRowsBayCollisionDetection (Issue #65)
# =============================================================================


class TestBuildTableRowsBayCollisionDetection:
    """_build_table_rows merges module-scoped bays into a deterministic flat dict.

    When two modules expose bays with the same name the module with the lower PK
    wins (first-match-wins with sorted module IDs).  Device-level bays always
    take precedence over module-scoped bays.
    """

    def _make_bay(self, name, pk):
        from unittest.mock import MagicMock

        bay = MagicMock()
        bay.name = name
        bay.pk = pk
        return bay

    def test_no_collision_passthrough(self):
        """When bay names are unique across modules, all bays are in all_bays."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = BaseModuleTableView.__new__(BaseModuleTableView)
        bay_a = self._make_bay("Slot 1", 10)
        bay_b = self._make_bay("Slot 2", 11)

        device_bays = {}
        module_scoped_bays = {
            1: {"Slot 1": bay_a},
            2: {"Slot 2": bay_b},
        }

        all_bays = view._compute_all_bays(device_bays, module_scoped_bays)
        assert "Slot 1" in all_bays
        assert "Slot 2" in all_bays
        assert all_bays["Slot 1"] is bay_a
        assert all_bays["Slot 2"] is bay_b

    def test_collision_lower_pk_wins(self):
        """When two modules share a bay name, the one with the lower module PK wins."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = BaseModuleTableView.__new__(BaseModuleTableView)
        bay_low = self._make_bay("Slot 1", 5)
        bay_high = self._make_bay("Slot 1", 99)

        device_bays = {}
        module_scoped_bays = {
            10: {"Slot 1": bay_high},  # lower module PK gets processed first
            2: {"Slot 1": bay_low},
        }

        all_bays = view._compute_all_bays(device_bays, module_scoped_bays)
        # Module PK 2 < 10 → bay_low wins
        assert all_bays["Slot 1"] is bay_low

    def test_device_bays_take_precedence_over_module_bays(self):
        """Device-level bays always override same-named module-scoped bays."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = BaseModuleTableView.__new__(BaseModuleTableView)
        device_bay = self._make_bay("Slot 1", 1)
        module_bay = self._make_bay("Slot 1", 2)

        device_bays = {"Slot 1": device_bay}
        module_scoped_bays = {1: {"Slot 1": module_bay}}

        all_bays = view._compute_all_bays(device_bays, module_scoped_bays)
        assert all_bays["Slot 1"] is device_bay

    def test_collision_logs_debug(self, caplog):
        """Collision names are logged at DEBUG level."""
        import logging

        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = BaseModuleTableView.__new__(BaseModuleTableView)
        bay_a = self._make_bay("Slot 1", 1)
        bay_b = self._make_bay("Slot 1", 2)

        device_bays = {}
        module_scoped_bays = {1: {"Slot 1": bay_a}, 2: {"Slot 1": bay_b}}

        with caplog.at_level(logging.DEBUG, logger="netbox_librenms_plugin.views.base.modules_view"):
            view._compute_all_bays(device_bays, module_scoped_bays)

        assert any("Slot 1" in msg for msg in caplog.messages)


class TestModulesRedirectResponse:
    """_modules_redirect_response: HX-Request → HX-Redirect; classic → redirect()."""

    def test_classic_request_uses_redirect(self):
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        req = MagicMock()
        req.headers = {}
        with patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect:
            mock_redirect.return_value = "REDIRECT"
            result = _modules_redirect_response(req, "/sync/")
        mock_redirect.assert_called_once_with("/sync/?tab=modules#librenms-module-table")
        assert result == "REDIRECT"

    def test_htmx_request_returns_hx_redirect_header(self):
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        req = MagicMock()
        req.headers = {"HX-Request": "true"}
        response = _modules_redirect_response(req, "/sync/")
        assert response.status_code == 204
        assert response["HX-Redirect"] == "/sync/?tab=modules#librenms-module-table"


class TestAddBayTemplateViewWiring:
    """AddBayTemplateView must have the right mixins and target kinds."""

    def test_inherits_required_mixins(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin, NetBoxObjectPermissionMixin
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        assert LibreNMSPermissionMixin in AddBayTemplateView.__mro__
        assert NetBoxObjectPermissionMixin in AddBayTemplateView.__mro__

    def test_target_kinds_are_device_type_and_module_type(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        assert AddBayTemplateView.TARGET_KINDS == ("device_type", "module_type")


class TestAddBayTemplateViewPostValidation:
    """POST input validation: target_kind, target_pk, name."""

    def _make_view(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        view = object.__new__(AddBayTemplateView)
        # Bypass perm checks: require_all_permissions returns None on success.
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def _make_request(self, post_data, htmx=False):
        req = MagicMock()
        req.method = "POST"
        req.POST = post_data
        req.headers = {"HX-Request": "true"} if htmx else {}
        return req

    def test_invalid_target_kind_returns_redirect(self):
        view = self._make_view()
        req = self._make_request({"target_kind": "bogus", "target_pk": "1", "name": "Slot 1"})
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch(
                "netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="REDIR"
            ) as mock_redir,
        ):
            result = view.post(req, pk=1)
        assert result == "REDIR"
        assert mock_messages.error.called
        assert "Invalid target_kind" in mock_messages.error.call_args[0][1]
        mock_redir.assert_called_once()

    def test_missing_target_pk_returns_redirect(self):
        view = self._make_view()
        req = self._make_request({"target_kind": "device_type", "target_pk": "", "name": "Slot 1"})
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="REDIR"),
        ):
            view.post(req, pk=1)
        assert "target_pk" in mock_messages.error.call_args[0][1]

    def test_missing_name_returns_redirect(self):
        view = self._make_view()
        req = self._make_request({"target_kind": "module_type", "target_pk": "5", "name": "  "})
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="REDIR"),
        ):
            view.post(req, pk=1)
        assert "name is required" in mock_messages.error.call_args[0][1]


class TestAddBayTemplateViewGetValidation:
    """GET modal renderer validates query-string inputs."""

    def _make_view(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        view = object.__new__(AddBayTemplateView)
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_invalid_target_kind_returns_400(self):
        view = self._make_view()
        req = MagicMock()
        req.GET = {"target_kind": "nope", "target_pk": "1"}
        with patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=MagicMock()):
            response = view.get(req, pk=1)
        assert response.status_code == 400

    def test_invalid_target_pk_returns_400(self):
        view = self._make_view()
        req = MagicMock()
        req.GET = {"target_kind": "device_type", "target_pk": "abc"}
        with patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=MagicMock()):
            response = view.get(req, pk=1)
        assert response.status_code == 400

    def test_valid_request_renders_modal_with_suggestions(self):
        view = self._make_view()
        req = MagicMock()
        req.GET = {
            "target_kind": "device_type",
            "target_pk": "7",
            "suggested_name": "Fan Tray 0",
            "suggested_position": "0",
            "suggested_label": "Fan Controller",
        }
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="RENDERED") as mock_render,
        ):
            response = view.get(req, pk=42)
        assert response == "RENDERED"
        ctx = mock_render.call_args[0][2]
        assert ctx["device_pk"] == 42
        assert ctx["target_kind"] == "device_type"
        assert ctx["target_pk"] == 7
        assert ctx["suggested_name"] == "Fan Tray 0"
        assert ctx["suggested_position"] == "0"
        assert ctx["suggested_label"] == "Fan Controller"
        # Without a librenms_name in the GET, the auto-mapping checkbox is
        # never offered (there's nothing to map from).
        assert ctx["offer_mapping_checkbox"] is False


class TestAddBayTemplateViewMappingCheckbox:
    """GET threads librenms_name/class into context and decides whether to
    show the auto-create-mapping checkbox.  POST creates the mapping when the
    user opts in and the NetBox name differs from the LibreNMS one."""

    def _make_view(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        view = object.__new__(AddBayTemplateView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._instantiate_template_on_existing = MagicMock(return_value=0)
        return view

    def _device_with_manufacturer(self, manufacturer=None):
        device = MagicMock()
        device.device_type.manufacturer = manufacturer
        return device

    def test_get_offers_checkbox_when_libre_name_present_and_no_existing_mapping(self):
        view = self._make_view()
        manufacturer = MagicMock()
        manufacturer.__str__ = lambda s: "Nokia"
        device = self._device_with_manufacturer(manufacturer)
        req = MagicMock()
        req.GET = {
            "target_kind": "device_type",
            "target_pk": "7",
            "suggested_name": "SFM 1",
            "librenms_name": "Sfm 1",
            "librenms_class": "fabricModule",
        }
        req.user.has_perm.return_value = True
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            view.get(req, pk=42)
        ctx = mock_render.call_args[0][2]
        assert ctx["librenms_name"] == "Sfm 1"
        assert ctx["librenms_class"] == "fabricModule"
        assert ctx["manufacturer_label"] == "Nokia"
        assert ctx["offer_mapping_checkbox"] is True
        assert ctx["mapping_exists"] is False

    def test_get_suppresses_checkbox_when_existing_mapping(self):
        view = self._make_view()
        device = self._device_with_manufacturer(MagicMock())
        req = MagicMock()
        req.GET = {
            "target_kind": "device_type",
            "target_pk": "7",
            "librenms_name": "Sfm 1",
            "librenms_class": "fabricModule",
        }
        req.user.has_perm.return_value = True
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = True
            view.get(req, pk=42)
        ctx = mock_render.call_args[0][2]
        assert ctx["mapping_exists"] is True
        assert ctx["offer_mapping_checkbox"] is False

    def test_get_suppresses_checkbox_when_user_lacks_add_mapping_perm(self):
        view = self._make_view()
        device = self._device_with_manufacturer(MagicMock())
        req = MagicMock()
        req.GET = {
            "target_kind": "device_type",
            "target_pk": "7",
            "librenms_name": "Sfm 1",
        }
        req.user.has_perm.return_value = False  # lacks add_modulebaymapping
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            view.get(req, pk=42)
        ctx = mock_render.call_args[0][2]
        assert ctx["offer_mapping_checkbox"] is False

    def test_post_creates_mapping_when_checkbox_set_and_names_differ(self):
        view = self._make_view()
        manufacturer = MagicMock()
        device = self._device_with_manufacturer(manufacturer)
        req = MagicMock()
        req.method = "POST"
        req.POST = {
            "target_kind": "device_type",
            "target_pk": "7",
            "name": "SFM 1",
            "librenms_name": "Sfm 1",
            "librenms_class": "fabricModule",
            "also_create_mapping": "1",
        }
        req.user.has_perm.return_value = True
        target = MagicMock()
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", side_effect=[device, target]),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="REDIR"),
            patch("dcim.models.ModuleBayTemplate") as mock_bt_cls,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            mock_bt_cls.return_value = MagicMock()
            # No existing mapping
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mapping_instance = MagicMock()
            mock_mapping_cls.return_value = mapping_instance
            view.post(req, pk=1)
        # ModuleBayMapping was constructed with the right field values
        call_kwargs = mock_mapping_cls.call_args.kwargs
        assert call_kwargs["librenms_name"] == "Sfm 1"
        assert call_kwargs["netbox_bay_name"] == "SFM 1"
        assert call_kwargs["librenms_class"] == "fabricModule"
        assert call_kwargs["is_regex"] is False
        assert call_kwargs["manufacturer"] is manufacturer
        mapping_instance.full_clean.assert_called_once()
        mapping_instance.save.assert_called_once()
        # Success message mentions both bay and mapping
        assert any("ModuleBayMapping" in c.args[1] for c in mock_msg.success.call_args_list)

    def test_post_skips_mapping_when_names_match(self):
        view = self._make_view()
        device = self._device_with_manufacturer(MagicMock())
        req = MagicMock()
        req.method = "POST"
        req.POST = {
            "target_kind": "device_type",
            "target_pk": "7",
            "name": "Sfm 1",  # same as LibreNMS — no mapping needed
            "librenms_name": "Sfm 1",
            "also_create_mapping": "1",
        }
        req.user.has_perm.return_value = True
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", side_effect=[device, MagicMock()]),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="REDIR"),
            patch("dcim.models.ModuleBayTemplate") as mock_bt_cls,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            mock_bt_cls.return_value = MagicMock()
            view.post(req, pk=1)
        # Mapping must not be instantiated when names match
        mock_mapping_cls.assert_not_called()

    def test_post_does_not_create_mapping_without_checkbox(self):
        view = self._make_view()
        device = self._device_with_manufacturer(MagicMock())
        req = MagicMock()
        req.method = "POST"
        req.POST = {
            "target_kind": "device_type",
            "target_pk": "7",
            "name": "SFM 1",
            "librenms_name": "Sfm 1",
            # also_create_mapping omitted
        }
        req.user.has_perm.return_value = True
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", side_effect=[device, MagicMock()]),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="REDIR"),
            patch("dcim.models.ModuleBayTemplate") as mock_bt_cls,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            mock_bt_cls.return_value = MagicMock()
            view.post(req, pk=1)
        mock_mapping_cls.assert_not_called()


class TestAddBayTemplateViewInstantiation:
    """After saving a ModuleBayTemplate, the view materialises it onto every
    existing Device/Module of the target so the resolver can match the new bay
    immediately (NetBox only auto-creates bays from templates at first-create
    time)."""

    def test_instantiate_on_existing_device_type_creates_missing_bays(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        device_type = MagicMock()
        bay_template = MagicMock()
        bay_template.instantiate.side_effect = lambda device: MagicMock(name=f"bay-for-{id(device)}")
        dev_a, dev_b = MagicMock(), MagicMock()
        with (
            patch("dcim.models.Device") as mock_device_cls,
            patch("dcim.models.Module") as mock_module_cls,
            patch("dcim.models.ModuleBay") as mock_bay_cls,
        ):
            mock_device_cls.objects.filter.return_value = [dev_a, dev_b]
            mock_bay_cls.objects.filter.return_value.exists.return_value = False
            count = AddBayTemplateView._instantiate_template_on_existing(bay_template, "device_type", device_type)
        assert count == 2
        mock_device_cls.objects.filter.assert_called_once_with(device_type=device_type)
        mock_module_cls.objects.filter.assert_not_called()
        assert bay_template.instantiate.call_count == 2

    def test_instantiate_skips_devices_that_already_have_the_bay(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        device_type = MagicMock()
        bay_template = MagicMock()
        existing_bay = MagicMock()
        existing_bay.name = "Fan Tray 0"
        existing_bay.full_clean = MagicMock()
        existing_bay.save = MagicMock()
        bay_template.instantiate.return_value = existing_bay
        with (
            patch("dcim.models.Device") as mock_device_cls,
            patch("dcim.models.Module"),
            patch("dcim.models.ModuleBay") as mock_bay_cls,
        ):
            mock_device_cls.objects.filter.return_value = [MagicMock()]
            # Bay already exists on the device
            mock_bay_cls.objects.filter.return_value.exists.return_value = True
            count = AddBayTemplateView._instantiate_template_on_existing(bay_template, "device_type", device_type)
        assert count == 0
        existing_bay.save.assert_not_called()

    def test_instantiate_on_existing_module_type_uses_module_queryset(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        module_type = MagicMock()
        module_a = MagicMock()
        bay_template = MagicMock()
        bay_template.instantiate.return_value = MagicMock(name="bay")
        with (
            patch("dcim.models.Device") as mock_device_cls,
            patch("dcim.models.Module") as mock_module_cls,
            patch("dcim.models.ModuleBay") as mock_bay_cls,
        ):
            mock_module_cls.objects.filter.return_value.select_related.return_value = [module_a]
            mock_bay_cls.objects.filter.return_value.exists.return_value = False
            count = AddBayTemplateView._instantiate_template_on_existing(bay_template, "module_type", module_type)
        assert count == 1
        mock_module_cls.objects.filter.assert_called_once_with(module_type=module_type)
        mock_device_cls.objects.filter.assert_not_called()
        bay_template.instantiate.assert_called_once_with(device=module_a.device, module=module_a)

    def test_instantiate_unknown_target_kind_is_noop(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        bay_template = MagicMock()
        with (
            patch("dcim.models.Device") as mock_device_cls,
            patch("dcim.models.Module") as mock_module_cls,
            patch("dcim.models.ModuleBay"),
        ):
            count = AddBayTemplateView._instantiate_template_on_existing(bay_template, "bogus", MagicMock())
        assert count == 0
        mock_device_cls.objects.filter.assert_not_called()
        mock_module_cls.objects.filter.assert_not_called()
        bay_template.instantiate.assert_not_called()

    """_derive_bay_template_suggestion infers sane defaults from item dicts."""

    def _helper(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        return BaseModuleTableView._derive_bay_template_suggestion

    def test_extracts_trailing_digit_position(self):
        result = self._helper()({"entPhysicalName": "Slot 3", "entPhysicalDescr": "Line Card"})
        assert result["name"] == "Slot 3"
        assert result["position"] == "3"
        assert result["label"] == "Line Card"

    def test_extracts_trailing_letter_position(self):
        result = self._helper()({"entPhysicalName": "CMA-A"})
        assert result["name"] == "CMA-A"
        assert result["position"] == "A"

    def test_falls_back_to_class_placeholder_for_fan(self):
        result = self._helper()({"entPhysicalName": "", "entPhysicalClass": "fan"})
        assert "Fan Tray" in result["name"]
        assert result["position"] == "1"

    def test_falls_back_to_class_placeholder_for_powersupply(self):
        result = self._helper()({"entPhysicalName": "", "entPhysicalClass": "powerSupply"})
        assert "Power Supply" in result["name"]

    def test_falls_back_to_slot_for_unknown_class(self):
        result = self._helper()({"entPhysicalName": "", "entPhysicalClass": "module"})
        assert result["name"] == "Slot 1"


class TestDeriveMappingPattern:
    """_derive_mapping_pattern maps each distinct LibreNMS digit value to a
    capture group; the NetBox replacement may use any literals as long as
    every NetBox digit value is present on the LibreNMS side and the
    pattern round-trips."""

    def _fn(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        return AddBayTemplateView._derive_mapping_pattern

    def test_case_only_difference_with_one_digit(self):
        result = self._fn()("Sfm 1", "SFM 1")
        assert result is not None
        assert result["librenms_pattern"] == r"^Sfm\ (\d+)$"
        assert result["netbox_replacement"] == r"SFM \1"
        assert result["digit_count"] == 1

    def test_identical_names_with_digit(self):
        result = self._fn()("Slot 0", "Slot 0")
        assert result is not None
        assert result["digit_count"] == 1

    def test_multi_digit_skeleton_collapses_repeat_to_backref(self):
        # All four digits have the same value '0', so they collapse to a
        # single group with back-references rather than four groups.
        result = self._fn()("TenGigE0/0/0/0", "TenGigE0/0/0/0")
        assert result is not None
        assert result["digit_count"] == 1
        assert result["librenms_pattern"] == r"^TenGigE(\d+)/\1/\1/\1$"
        assert result["netbox_replacement"] == r"TenGigE\1/\1/\1/\1"

    def test_libre_and_nb_have_different_skeletons(self):
        # 0/FT0 → Fan Tray 0: both libre digits are '0' so they collapse to
        # one group, and NetBox '0' references that group.
        result = self._fn()("0/FT0", "Fan Tray 0")
        assert result is not None
        assert result["librenms_pattern"] == r"^(\d+)/FT\1$"
        assert result["netbox_replacement"] == r"Fan Tray \1"

    def test_literal_difference_with_shared_digit(self):
        # Literals on libre and NetBox are unrelated — that's fine, they
        # don't need to match. Digit '1' is shared.
        result = self._fn()("Sfm 1", "Card 1")
        assert result is not None
        assert result["netbox_replacement"] == r"Card \1"

    def test_nb_digit_absent_from_libre_returns_none(self):
        # NetBox uses digit '2' which is nowhere on the libre side.
        assert self._fn()("Slot 1", "Slot 2") is None

    def test_no_digit_run_returns_none(self):
        assert self._fn()("Slot A", "Slot A") is None

    def test_extra_nb_digit_absent_from_libre_returns_none(self):
        # NetBox has an extra digit '0' that libre never produces.
        assert self._fn()("Slot 1", "Slot 1/0") is None

    def test_empty_input_returns_none(self):
        assert self._fn()("", "Slot 1") is None
        assert self._fn()("Slot 1", "") is None


class TestAddBayTemplateViewRegexMapping:
    """GET surfaces a derived regex pattern; POST stores it as is_regex=True."""

    def _make_view(self):
        from netbox_librenms_plugin.views.sync.modules import AddBayTemplateView

        view = object.__new__(AddBayTemplateView)
        view.require_all_permissions = MagicMock(return_value=None)
        view._instantiate_template_on_existing = MagicMock(return_value=0)
        return view

    def _device(self, manufacturer=None):
        device = MagicMock()
        device.device_type.manufacturer = manufacturer
        return device

    def test_get_includes_mapping_pattern_when_skeleton_matches(self):
        view = self._make_view()
        manufacturer = MagicMock()
        manufacturer.__str__ = lambda s: "Nokia"
        device = self._device(manufacturer)
        req = MagicMock()
        req.GET = {
            "target_kind": "device_type",
            "target_pk": "7",
            "suggested_name": "SFM 1",
            "librenms_name": "Sfm 1",
            "librenms_class": "fabricModule",
        }
        req.user.has_perm.return_value = True
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = []
            view.get(req, pk=42)
        ctx = mock_render.call_args[0][2]
        assert ctx["mapping_pattern"] is not None
        assert ctx["mapping_default_kind"] == "regex"
        assert ctx["mapping_pattern"]["librenms_pattern"].startswith("^Sfm")

    def test_get_omits_mapping_pattern_when_no_digit(self):
        view = self._make_view()
        device = self._device(MagicMock())
        req = MagicMock()
        req.GET = {
            "target_kind": "device_type",
            "target_pk": "7",
            "suggested_name": "Card A",
            "librenms_name": "card a",
            "librenms_class": "module",
        }
        req.user.has_perm.return_value = True
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = []
            view.get(req, pk=42)
        ctx = mock_render.call_args[0][2]
        assert ctx["mapping_pattern"] is None
        assert ctx["mapping_default_kind"] == "exact"

    def test_get_skips_checkbox_when_existing_regex_covers_name(self):
        view = self._make_view()
        device = self._device(MagicMock())
        existing = MagicMock()
        existing.librenms_name = r"^Sfm (\d+)$"
        req = MagicMock()
        req.GET = {
            "target_kind": "device_type",
            "target_pk": "7",
            "suggested_name": "SFM 1",
            "librenms_name": "Sfm 1",
            "librenms_class": "fabricModule",
        }
        req.user.has_perm.return_value = True
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            # No exact mapping, but a covering regex row exists.
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = [existing]
            view.get(req, pk=42)
        ctx = mock_render.call_args[0][2]
        assert ctx["mapping_exists"] is True
        assert ctx["offer_mapping_checkbox"] is False

    def _post_helper(self, post_data):
        view = self._make_view()
        manufacturer = MagicMock()
        device = self._device(manufacturer)
        req = MagicMock()
        req.method = "POST"
        req.POST = post_data
        req.user.has_perm.return_value = True
        target = MagicMock()
        return view, req, device, target, manufacturer

    def test_post_creates_regex_mapping_when_kind_regex_and_pattern_derives(self):
        view, req, device, target, manufacturer = self._post_helper(
            {
                "target_kind": "device_type",
                "target_pk": "7",
                "name": "SFM 1",
                "librenms_name": "Sfm 1",
                "librenms_class": "fabricModule",
                "also_create_mapping": "1",
                "mapping_kind": "regex",
            }
        )
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", side_effect=[device, target]),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="R"),
            patch("dcim.models.ModuleBayTemplate") as mock_bt_cls,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            mock_bt_cls.return_value = MagicMock()
            # No existing exact or regex coverage.
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = []
            mock_mapping_cls.return_value = MagicMock()
            view.post(req, pk=1)
        kwargs = mock_mapping_cls.call_args.kwargs
        assert kwargs["is_regex"] is True
        assert kwargs["librenms_name"] == r"^Sfm\ (\d+)$"
        assert kwargs["netbox_bay_name"] == r"SFM \1"
        assert kwargs["manufacturer"] is manufacturer

    def test_post_falls_back_to_exact_when_pattern_does_not_derive(self):
        # mapping_kind=regex requested but server-side rule says no.
        view, req, device, target, _m = self._post_helper(
            {
                "target_kind": "device_type",
                "target_pk": "7",
                "name": "Card B",
                "librenms_name": "card a",  # No digit run → no pattern.
                "librenms_class": "module",
                "also_create_mapping": "1",
                "mapping_kind": "regex",
            }
        )
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", side_effect=[device, target]),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="R"),
            patch("dcim.models.ModuleBayTemplate") as mock_bt_cls,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            mock_bt_cls.return_value = MagicMock()
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = []
            mock_mapping_cls.return_value = MagicMock()
            view.post(req, pk=1)
        kwargs = mock_mapping_cls.call_args.kwargs
        assert kwargs["is_regex"] is False
        assert kwargs["librenms_name"] == "card a"
        assert kwargs["netbox_bay_name"] == "Card B"

    def test_post_kind_exact_overrides_derivable_pattern(self):
        # Pattern would derive, but user explicitly chose exact.
        view, req, device, target, _m = self._post_helper(
            {
                "target_kind": "device_type",
                "target_pk": "7",
                "name": "SFM 1",
                "librenms_name": "Sfm 1",
                "librenms_class": "fabricModule",
                "also_create_mapping": "1",
                "mapping_kind": "exact",
            }
        )
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", side_effect=[device, target]),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="R"),
            patch("dcim.models.ModuleBayTemplate") as mock_bt_cls,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            mock_bt_cls.return_value = MagicMock()
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = []
            mock_mapping_cls.return_value = MagicMock()
            view.post(req, pk=1)
        kwargs = mock_mapping_cls.call_args.kwargs
        assert kwargs["is_regex"] is False
        assert kwargs["librenms_name"] == "Sfm 1"

    def test_post_skips_when_existing_regex_covers(self):
        view, req, device, target, _m = self._post_helper(
            {
                "target_kind": "device_type",
                "target_pk": "7",
                "name": "SFM 1",
                "librenms_name": "Sfm 1",
                "librenms_class": "fabricModule",
                "also_create_mapping": "1",
                "mapping_kind": "regex",
            }
        )
        existing = MagicMock()
        existing.librenms_name = r"^Sfm (\d+)$"
        with (
            patch("netbox_librenms_plugin.views.sync.modules.get_object_or_404", side_effect=[device, target]),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages"),
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="R"),
            patch("dcim.models.ModuleBayTemplate") as mock_bt_cls,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            mock_bt_cls.return_value = MagicMock()
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = [existing]
            view.post(req, pk=1)
        # Coverage already exists → no new ModuleBayMapping instantiated.
        mock_mapping_cls.assert_not_called()

    """_render_fix_bay_template_badge emits an HTMX modal trigger when device + target_pk known."""

    def _table_with_device(self, device_pk=99, can_add_module_bay_template=True):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        table.device = MagicMock(pk=device_pk)
        table.can_add_module_bay_template = can_add_module_bay_template
        return table

    def _table_without_device(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        table = object.__new__(LibreNMSModuleTable)
        # Intentionally unset device — _render_fix_bay_template_badge handles via getattr.
        return table

    def test_renders_htmx_button_when_device_and_target_pk_present(self):
        table = self._table_with_device(device_pk=42)
        with patch(
            "netbox_librenms_plugin.tables.modules.reverse",
            return_value="/plugins/librenms_plugin/devices/42/add-bay-template/",
        ):
            html = str(
                table._render_fix_bay_template_badge(
                    title="some title",
                    target_kind="module_type",
                    target_pk=5,
                    target_label="A9K",
                    suggestion={"name": "Slot 1", "position": "1", "label": "Line"},
                    fallback_url="/dcim/module-types/5/",
                    label="Fix Model",
                )
            )
        assert "hx-get=" in html
        assert "target_kind=module_type" in html
        assert "target_pk=5" in html
        assert "suggested_name=Slot+1" in html or "suggested_name=Slot%201" in html
        assert "Fix Model" in html

    def test_falls_back_to_link_when_device_missing(self):
        table = self._table_without_device()
        html = str(
            table._render_fix_bay_template_badge(
                title="t",
                target_kind="device_type",
                target_pk=7,
                target_label="ASR",
                suggestion={},
                fallback_url="/dcim/device-types/7/",
                label="Fix Device Type",
            )
        )
        assert "hx-get" not in html
        assert '<a href="/dcim/device-types/7/"' in html
        assert "Fix Device Type" in html

    def test_falls_back_to_span_when_no_url_and_no_device(self):
        table = self._table_without_device()
        html = str(
            table._render_fix_bay_template_badge(
                title="t",
                target_kind="device_type",
                target_pk=None,
                target_label="ASR",
                suggestion={},
                fallback_url="",
                label="Fix Device Type",
            )
        )
        assert "<span" in html
        assert "Fix Device Type" in html

    def test_returns_empty_when_device_present_but_lacks_add_modulebaytemplate_perm(self):
        """When a viewer can't add bay templates, the badge is hidden so it doesn't
        act as a dead-end control. The HTMX modal would only return 403 for them."""
        table = self._table_with_device(device_pk=42, can_add_module_bay_template=False)
        html = str(
            table._render_fix_bay_template_badge(
                title="t",
                target_kind="module_type",
                target_pk=5,
                target_label="A9K",
                suggestion={"name": "Slot 1"},
                fallback_url="/dcim/module-types/5/",
                label="Fix Model",
            )
        )
        assert html == ""


# ---------------------------------------------------------------------------
# predict_module_interface_names signal hook
# ---------------------------------------------------------------------------


class TestVCNormalizationReportView:
    """VCNormalizationReportView returns 400 when there's nothing to report, HTML when there is."""

    def test_get_returns_400_when_module_id_missing(self):
        from netbox_librenms_plugin.views.sync.modules import VCNormalizationReportView

        view = object.__new__(VCNormalizationReportView)
        view.required_object_permissions = {}
        device = _make_device()
        request = _make_request("GET", data={})

        with (
            patch.object(view, "require_object_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                return_value=device,
            ),
        ):
            response = view.get(request, pk=24)

        assert response.status_code == 400
        assert b"module_id" in response.content

    def test_get_returns_400_when_no_noop_detected(self):
        from netbox_librenms_plugin.views.sync.modules import VCNormalizationReportView

        view = object.__new__(VCNormalizationReportView)
        view.required_object_permissions = {}
        device = _make_device()
        module = MagicMock()
        request = _make_request("GET", data={"module_id": "321"})

        with (
            patch.object(view, "require_object_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module],
            ),
            patch(
                "netbox_librenms_plugin.utils.detect_vc_normalization_noop",
                return_value=None,
            ),
        ):
            response = view.get(request, pk=24)

        assert response.status_code == 400
        assert b"nothing to report" in response.content.lower()

    def test_get_renders_template_when_noop_detected(self):
        from netbox_librenms_plugin.views.sync.modules import VCNormalizationReportView

        view = object.__new__(VCNormalizationReportView)
        view.required_object_permissions = {}
        device = _make_device()
        module = MagicMock()
        request = _make_request("GET", data={"module_id": "321"})

        diagnostic = {
            "manufacturer_slug": "nokia",
            "device_type_model": "7250-IXR",
            "module_type_model": "QSFP-DD",
            "module_bay_name": "Bay c9",
            "vc_position": 3,
            "vc_member_positions": [1, 2, 3, 4],
            "template_pairs": [("{module}", "2/x1/1/c9")],
            "regex": "x",
        }

        with (
            patch.object(view, "require_object_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module],
            ),
            patch(
                "netbox_librenms_plugin.utils.detect_vc_normalization_noop",
                return_value=diagnostic,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.modules.render",
                return_value="rendered",
            ) as mock_render,
        ):
            response = view.get(request, pk=24)

        assert response == "rendered"
        ctx = mock_render.call_args[0][2]
        assert "**VC interface normalization — no match**" in ctx["report_markdown"]
        assert "nokia" in ctx["report_markdown"]

    def test_get_warns_on_invalid_selected_device_id(self):
        """Invalid selected_device_id triggers the standard warn helper but still proceeds."""
        from netbox_librenms_plugin.views.sync.modules import VCNormalizationReportView

        view = object.__new__(VCNormalizationReportView)
        view.required_object_permissions = {}
        device = _make_device()
        module = MagicMock()
        request = _make_request("GET", data={"module_id": "321", "selected_device_id": "bogus"})

        with (
            patch.object(view, "require_object_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module],
            ),
            patch(
                "netbox_librenms_plugin.views.sync.modules._resolve_target_device_with_validation",
                return_value=(device, True),
            ),
            patch(
                "netbox_librenms_plugin.views.sync.modules._warn_invalid_selected_device",
            ) as mock_warn,
            patch(
                "netbox_librenms_plugin.utils.detect_vc_normalization_noop",
                return_value=None,
            ),
        ):
            response = view.get(request, pk=24)

        mock_warn.assert_called_once_with(request)
        assert response.status_code == 400

    def test_get_returns_400_when_module_id_non_numeric(self):
        """Non-numeric module_id is treated the same as missing — returns 400."""
        from netbox_librenms_plugin.views.sync.modules import VCNormalizationReportView

        view = object.__new__(VCNormalizationReportView)
        view.required_object_permissions = {}
        device = _make_device()
        request = _make_request("GET", data={"module_id": "not-a-number"})

        with (
            patch.object(view, "require_object_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                return_value=device,
            ),
        ):
            response = view.get(request, pk=24)

        assert response.status_code == 400


class TestVCNormalizationE2E:
    """End-to-end: production code path (get_module_template_interface_names → detect_vc_normalization_noop).

    Exercises the real regex against vendor-realistic name shapes without DB
    fixtures. Catches regressions if either piece changes how it processes names.
    """

    @staticmethod
    def _device(vc_position=3, vc_id=11, member_positions=(1, 2, 3, 4)):
        d = MagicMock()
        d.vc_position = vc_position
        d.virtual_chassis_id = vc_id
        d.virtual_chassis = MagicMock()
        d.virtual_chassis.members.values_list.return_value = list(member_positions)
        d.device_type = MagicMock()
        return d

    @staticmethod
    def _module(instantiated_names):
        m = MagicMock()
        templates = []
        for name in instantiated_names:
            tmpl = MagicMock()
            tmpl.name = "{module}"
            inst = MagicMock()
            inst.name = name
            tmpl.instantiate.return_value = inst
            templates.append(tmpl)
        m.module_type.interfacetemplates.all.return_value = templates
        m.module_type.manufacturer.slug = "vendor"
        m.module_type.model = "MOD"
        m.module_bay.name = "Bay X"
        return m

    def test_cisco_shape_does_not_trigger_diagnostic(self):
        """A Cisco-style name (TenGigabitEthernet1/1/1) matches the regex → no diagnostic."""
        from netbox_librenms_plugin.utils import (
            detect_vc_normalization_noop,
            get_module_template_interface_names,
        )

        device = self._device(vc_position=3)
        module = self._module(["TenGigabitEthernet1/1/1"])

        # Production path: prediction returns the VC-rewritten name.
        names = get_module_template_interface_names(device, module)
        assert names == ["TenGigabitEthernet3/1/1"]

        # Detector sees the (pre-rewrite) instantiated name, which DOES match the regex.
        assert detect_vc_normalization_noop(device, module) is None

    def test_nokia_shape_triggers_diagnostic(self):
        """A Nokia-style name (2/x1/1/c9) doesn't match the regex → diagnostic returned."""
        from netbox_librenms_plugin.utils import (
            detect_vc_normalization_noop,
            get_module_template_interface_names,
        )

        device = self._device(vc_position=2, member_positions=(1, 2))
        module = self._module(["2/x1/1/c9"])

        # Production path: prediction returns the name unchanged (no rewrite applied).
        names = get_module_template_interface_names(device, module)
        assert names == ["2/x1/1/c9"]

        # Detector flags this as a vendor-support issue worth reporting.
        diag = detect_vc_normalization_noop(device, module)
        assert diag is not None
        assert diag["vc_position"] == 2
        assert diag["template_pairs"] == [("{module}", "2/x1/1/c9")]

    def test_juniper_shape_triggers_diagnostic(self):
        """Juniper xe-0/0/0 names don't match the regex (prefix breaks on '-') → diagnostic."""
        from netbox_librenms_plugin.utils import detect_vc_normalization_noop

        device = self._device(vc_position=3, member_positions=(1, 2, 3, 4))
        module = self._module(["xe-0/0/0", "xe-0/0/1"])

        diag = detect_vc_normalization_noop(device, module)
        assert diag is not None
        assert {pair[1] for pair in diag["template_pairs"]} == {"xe-0/0/0", "xe-0/0/1"}

    def test_mixed_shapes_with_one_matching_returns_none(self):
        """If at least one template name matches the regex, the row is not flagged."""
        from netbox_librenms_plugin.utils import detect_vc_normalization_noop

        device = self._device()
        # One Cisco-shaped name (matches regex) alongside one Nokia-shaped (doesn't).
        # Detector should NOT flag — at least one rewrite path is working.
        module = self._module(["Te1/0/1", "2/x1/1/c9"])

        assert detect_vc_normalization_noop(device, module) is None


class TestPredictModuleInterfaceNamesSignal:
    """get_module_template_interface_names invokes the predict signal and honors receiver overrides."""

    def _make_module(self, template_names):
        module = MagicMock()
        template_manager = MagicMock()
        templates = []
        for name in template_names:
            tmpl = MagicMock()
            tmpl.instantiate.return_value = MagicMock(name=name)
            tmpl.instantiate.return_value.name = name
            templates.append(tmpl)
        template_manager.all.return_value = templates
        module.module_type.interfacetemplates = template_manager
        return module

    def test_no_receivers_returns_raw_template_names(self):
        from netbox_librenms_plugin.utils import get_module_template_interface_names

        device = MagicMock()
        module = self._make_module(["Gi1/0/1", "Gi1/0/2"])
        assert get_module_template_interface_names(device, module) == ["Gi1/0/1", "Gi1/0/2"]

    def test_receiver_can_rewrite_names(self):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_names

        @receiver(predict_module_interface_names)
        def rewrite(sender, device, module, names, **kwargs):
            return [f"{n}/1" for n in names]

        try:
            device = MagicMock()
            module = self._make_module(["2/x1/1/c9"])
            assert get_module_template_interface_names(device, module) == ["2/x1/1/c9/1"]
        finally:
            predict_module_interface_names.disconnect(rewrite)

    def test_receiver_returning_none_leaves_names_unchanged(self):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_names

        @receiver(predict_module_interface_names)
        def noop(sender, device, module, names, **kwargs):
            return None

        try:
            device = MagicMock()
            module = self._make_module(["Gi1/0/1"])
            assert get_module_template_interface_names(device, module) == ["Gi1/0/1"]
        finally:
            predict_module_interface_names.disconnect(noop)

    def test_receiver_can_override_to_empty_list(self):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_names

        @receiver(predict_module_interface_names)
        def suppress(sender, device, module, names, **kwargs):
            return []

        try:
            device = MagicMock()
            module = self._make_module(["Gi1/0/1"])
            assert get_module_template_interface_names(device, module) == []
        finally:
            predict_module_interface_names.disconnect(suppress)

    def test_last_receiver_returning_list_wins(self):
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_names

        @receiver(predict_module_interface_names)
        def first(sender, device, module, names, **kwargs):
            return ["first"]

        @receiver(predict_module_interface_names)
        def second(sender, device, module, names, **kwargs):
            return ["second"]

        try:
            device = MagicMock()
            module = self._make_module(["raw"])
            # Django Signal.send invokes receivers in connection order; the last
            # non-None return wins per the documented contract.
            result = get_module_template_interface_names(device, module)
            assert result == ["second"]
        finally:
            predict_module_interface_names.disconnect(first)
            predict_module_interface_names.disconnect(second)

    def test_failing_receiver_is_isolated(self, caplog):
        """send_robust must isolate a raising receiver so adoption isn't broken.

        A buggy third-party receiver that raises is logged and skipped; a later
        well-behaved receiver still applies, and the raw names survive if none do.
        """
        import logging

        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_names

        @receiver(predict_module_interface_names)
        def boom(sender, device, module, names, **kwargs):
            raise RuntimeError("third-party receiver blew up")

        @receiver(predict_module_interface_names)
        def good(sender, device, module, names, **kwargs):
            return ["override"]

        try:
            device = MagicMock()
            module = self._make_module(["raw"])
            # The raising receiver must not propagate; the good receiver still wins.
            with caplog.at_level(logging.WARNING, logger="netbox_librenms_plugin.utils"):
                assert get_module_template_interface_names(device, module) == ["override"]
            # The isolated failure is logged (warning), not silently swallowed.
            assert any("receiver failed" in msg for msg in caplog.messages)
        finally:
            predict_module_interface_names.disconnect(boom)
            predict_module_interface_names.disconnect(good)

    def test_only_failing_receiver_falls_back_to_raw_names(self):
        """If the sole receiver raises, the raw template names are returned unchanged."""
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.utils import get_module_template_interface_names

        @receiver(predict_module_interface_names)
        def boom(sender, device, module, names, **kwargs):
            raise RuntimeError("third-party receiver blew up")

        try:
            device = MagicMock()
            module = self._make_module(["Gi1/0/1"])
            assert get_module_template_interface_names(device, module) == ["Gi1/0/1"]
        finally:
            predict_module_interface_names.disconnect(boom)


def _make_install_device():
    """Create a real device with one empty module bay and a matching module type."""
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, ModuleBay, ModuleType, Site

    mfr, _ = Manufacturer.objects.get_or_create(name="HTMX-Mfr", slug="htmx-mfr")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="HTMX-DT", slug="htmx-dt")
    role, _ = DeviceRole.objects.get_or_create(name="HTMX-Role", slug="htmx-role")
    site, _ = Site.objects.get_or_create(name="HTMX-Site", slug="htmx-site")
    device = Device.objects.create(name="htmx-install-dev", device_type=dt, role=role, site=site, status="active")
    bay = ModuleBay.objects.create(device=device, name="Slot1")
    mtype = ModuleType.objects.create(manufacturer=mfr, model="HTMX-SFP")
    return device, bay, mtype


@pytest.mark.django_db
class TestInstallModuleHTMXSwap:
    """InstallModuleView returns an in-place table partial for HTMX, a full redirect otherwise."""

    def _login(self, client):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser(username="htmx-admin", email="a@b.c", password="x")
        client.force_login(user)

    def test_htmx_install_returns_table_partial_not_redirect(self, client):
        """An HX-Request install installs the module and swaps back the table partial (200), not a redirect."""
        from django.urls import reverse
        from dcim.models import Module

        device, bay, mtype = _make_install_device()
        self._login(client)

        url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})
        resp = client.post(
            url,
            {"module_bay_id": bay.pk, "module_type_id": mtype.pk, "server_key": "", "serial": "SN-HTMX-1"},
            HTTP_HX_REQUEST="true",
        )

        # In-place swap: a 200 carrying the rendered table partial + its flash message,
        # NOT the 204/HX-Redirect (which would full-reload the whole sync page).
        assert resp.status_code == 200
        assert "HX-Redirect" not in resp
        assert b"Installed" in resp.content
        assert Module.objects.filter(module_bay=bay, module_type=mtype).exists()

    def test_non_htmx_install_still_redirects(self, client):
        """A classic (non-HTMX) install still 302-redirects, so the button degrades without JS."""
        from django.urls import reverse
        from dcim.models import Module

        device, bay, mtype = _make_install_device()
        self._login(client)

        url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": device.pk})
        resp = client.post(
            url,
            {"module_bay_id": bay.pk, "module_type_id": mtype.pk, "server_key": "", "serial": "SN-HTMX-2"},
        )

        assert resp.status_code == 302
        assert Module.objects.filter(module_bay=bay, module_type=mtype).exists()


@pytest.mark.django_db
class TestInstallBranchSelectedHTMXSwap:
    """InstallBranchView/InstallSelectedView also swap the table in place for HTMX, redirect otherwise."""

    def _login(self, client):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser(username="htmx-admin2", email="a@b.c", password="x")
        client.force_login(user)

    def test_branch_htmx_returns_partial_not_redirect(self, client):
        """A branch install via HX-Request renders the table partial (200), not a 204/redirect."""
        from django.urls import reverse

        device = _make_install_device()[0]
        self._login(client)
        url = reverse("plugins:netbox_librenms_plugin:install_branch", kwargs={"pk": device.pk})
        # Missing parent_index → early exit, routed through the partial helper.
        resp = client.post(url, {"server_key": ""}, HTTP_HX_REQUEST="true")
        assert resp.status_code == 200
        assert "HX-Redirect" not in resp
        assert b"parent inventory index" in resp.content

    def test_branch_non_htmx_redirects(self, client):
        """A classic branch install still 302-redirects."""
        from django.urls import reverse

        device = _make_install_device()[0]
        self._login(client)
        url = reverse("plugins:netbox_librenms_plugin:install_branch", kwargs={"pk": device.pk})
        resp = client.post(url, {"server_key": ""})
        assert resp.status_code == 302

    def test_selected_htmx_returns_partial_not_redirect(self, client):
        """An install-selected via HX-Request renders the table partial (200), not a 204/redirect."""
        from django.urls import reverse

        device = _make_install_device()[0]
        self._login(client)
        url = reverse("plugins:netbox_librenms_plugin:install_selected", kwargs={"pk": device.pk})
        # No selection → early exit, routed through the partial helper.
        resp = client.post(url, {"server_key": ""}, HTTP_HX_REQUEST="true")
        assert resp.status_code == 200
        assert "HX-Redirect" not in resp
        assert b"No modules selected" in resp.content

    def test_selected_non_htmx_redirects(self, client):
        """A classic install-selected still 302-redirects."""
        from django.urls import reverse

        device = _make_install_device()[0]
        self._login(client)
        url = reverse("plugins:netbox_librenms_plugin:install_selected", kwargs={"pk": device.pk})
        resp = client.post(url, {"server_key": ""})
        assert resp.status_code == 302


def _make_device_with_installed_module(name_suffix, *, with_template_standalone=False):
    """Create a real device with one installed module.

    When ``with_template_standalone`` is set, give the module type one interface template
    and a matching standalone (module-less) interface so an Update-Interface adopt succeeds.
    """
    from dcim.models import (
        Device,
        DeviceRole,
        DeviceType,
        Interface,
        InterfaceTemplate,
        Manufacturer,
        Module,
        ModuleBay,
        ModuleType,
        Site,
    )

    mfr, _ = Manufacturer.objects.get_or_create(name=f"HMod-Mfr-{name_suffix}", slug=f"hmod-mfr-{name_suffix}")
    dt, _ = DeviceType.objects.get_or_create(
        manufacturer=mfr, model=f"HMod-DT-{name_suffix}", slug=f"hmod-dt-{name_suffix}"
    )
    role, _ = DeviceRole.objects.get_or_create(name=f"HMod-Role-{name_suffix}", slug=f"hmod-role-{name_suffix}")
    site, _ = Site.objects.get_or_create(name=f"HMod-Site-{name_suffix}", slug=f"hmod-site-{name_suffix}")
    device = Device.objects.create(
        name=f"hmod-dev-{name_suffix}", device_type=dt, role=role, site=site, status="active"
    )
    mtype = ModuleType.objects.create(manufacturer=mfr, model=f"HMod-MT-{name_suffix}")
    if with_template_standalone:
        InterfaceTemplate.objects.create(module_type=mtype, name="Gi0/1", type="1000base-t")
    bay = ModuleBay.objects.create(device=device, name="Slot1")
    module = Module.objects.create(device=device, module_bay=bay, module_type=mtype, serial="OLD-SN")
    if with_template_standalone:
        # Clear the auto-instantiated module component so the standalone owns the template name,
        # then create the module-less interface the adopt should claim.
        Interface.objects.filter(device=device).delete()
        Interface.objects.create(device=device, name="Gi0/1", type="1000base-t", module=None)
    return device, module


@pytest.mark.django_db
class TestUpdateModuleSerialHTMXSwap:
    """UpdateModuleSerialView returns an HX-Redirect for HTMX (the mismatch modal expects it), redirects otherwise."""

    def _login(self, client):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser(username="htmx-serial", email="a@b.c", password="x")
        client.force_login(user)

    def test_htmx_update_serial_returns_hx_redirect_not_table_partial(self, client):
        """An HX-Request serial update persists the serial and returns an empty HX-Redirect response.

        The 'Update Serial Only' modal form posts hx-swap='none', so a table partial would be
        thrown away, leaving the modal open on stale data — it needs the HX-Redirect to close and
        reload (like the sibling MoveModuleView).
        """
        from django.urls import reverse

        device, module = _make_device_with_installed_module("serial-hx")
        self._login(client)

        url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": device.pk})
        resp = client.post(
            url,
            {"module_id": module.pk, "serial": "NEW-SN", "selected_device_id": device.pk},
            HTTP_HX_REQUEST="true",
        )

        # Empty HX-Redirect response back to the modules tab, NOT an in-place 200 table partial.
        assert resp.status_code == 204
        assert "tab=modules" in resp["HX-Redirect"]
        module.refresh_from_db()
        assert module.serial == "NEW-SN"

    def test_non_htmx_update_serial_still_redirects(self, client):
        """A classic (non-HTMX) serial update still 302-redirects, so the button degrades without JS."""
        from django.urls import reverse

        device, module = _make_device_with_installed_module("serial-classic")
        self._login(client)

        url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": device.pk})
        resp = client.post(url, {"module_id": module.pk, "serial": "NEW-SN", "selected_device_id": device.pk})

        assert resp.status_code == 302
        module.refresh_from_db()
        assert module.serial == "NEW-SN"


@pytest.mark.django_db
class TestUpdateModuleInterfaceHTMXSwap:
    """UpdateModuleInterfaceView swaps the table partial in place for HTMX, redirects otherwise."""

    def _login(self, client):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser(username="htmx-iface", email="a@b.c", password="x")
        client.force_login(user)

    def test_htmx_update_interface_adopts_and_returns_table_partial(self, client):
        """An HX-Request interface update adopts the matching standalone and swaps back the partial (200)."""
        from django.urls import reverse

        from dcim.models import Interface

        device, module = _make_device_with_installed_module("iface-hx", with_template_standalone=True)
        self._login(client)

        url = reverse("plugins:netbox_librenms_plugin:update_module_interface", kwargs={"pk": device.pk})
        resp = client.post(
            url,
            {"module_id": module.pk, "server_key": "", "selected_device_id": device.pk},
            HTTP_HX_REQUEST="true",
        )

        assert resp.status_code == 200
        assert "HX-Redirect" not in resp
        assert b"adopted 1 existing standalone" in resp.content
        # The standalone "Gi0/1" is now owned by the module.
        assert Interface.objects.get(device=device, name="Gi0/1").module_id == module.pk

    def test_non_htmx_update_interface_still_redirects(self, client):
        """A classic (non-HTMX) interface update still 302-redirects."""
        from django.urls import reverse

        from dcim.models import Interface

        device, module = _make_device_with_installed_module("iface-classic", with_template_standalone=True)
        self._login(client)

        url = reverse("plugins:netbox_librenms_plugin:update_module_interface", kwargs={"pk": device.pk})
        resp = client.post(url, {"module_id": module.pk, "server_key": "", "selected_device_id": device.pk})

        assert resp.status_code == 302
        assert Interface.objects.get(device=device, name="Gi0/1").module_id == module.pk


@pytest.mark.django_db
class TestAdoptableCountStandaloneGate:
    """_count_adoptable_template_interfaces skips the INR prediction when nothing can be adopted."""

    def _view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        return object.__new__(DeviceModuleTableView)

    def _device_with_module(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Module, ModuleBay, ModuleType, Site

        mfr, _ = Manufacturer.objects.get_or_create(name="GateMfr", slug="gatemfr")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="GateDT", slug="gatedt")
        role, _ = DeviceRole.objects.get_or_create(name="GateRole", slug="gaterole")
        site, _ = Site.objects.get_or_create(name="GateSite", slug="gatesite")
        device = Device.objects.create(name="gate-dev", device_type=dt, role=role, site=site, status="active")
        bay = ModuleBay.objects.create(device=device, name="Slot1")
        mtype = ModuleType.objects.create(manufacturer=mfr, model="GateMT")
        module = Module.objects.create(device=device, module_bay=bay, module_type=mtype, status="active")
        return device, module

    def test_skips_prediction_when_no_standalone_interfaces(self):
        """A fully-adopted device (no module-less interfaces) returns 0 without calling the INR prediction."""
        from dcim.models import Interface

        device, module = self._device_with_module()
        # The only interface is already adopted into the module → no standalone interfaces remain.
        Interface.objects.create(device=device, name="Gi1/0/1", type="1000base-t", module=module)

        view = self._view()
        with patch("netbox_librenms_plugin.views.base.modules_view.get_module_template_interface_names") as mock_pred:
            count = view._count_adoptable_template_interfaces(module)

        assert count == 0
        mock_pred.assert_not_called()  # gated out before the per-row prediction signal

    def test_counts_when_standalone_interface_matches(self):
        """With a standalone interface matching a predicted template name, the count still works."""
        from dcim.models import Interface

        device, module = self._device_with_module()
        Interface.objects.create(device=device, name="Gi1/0/2", type="1000base-t")  # module=None → standalone

        view = self._view()
        with patch(
            "netbox_librenms_plugin.views.base.modules_view.get_module_template_interface_names",
            return_value=["Gi1/0/2"],
        ) as mock_pred:
            count = view._count_adoptable_template_interfaces(module)

        assert count == 1
        mock_pred.assert_called_once()


@pytest.mark.django_db
class TestModuleTypesIndexCache:
    """get_module_types_indexed caches the index and rebuilds only when its inputs change."""

    def test_repeated_calls_reuse_cached_index(self):
        """A second call returns the same cached index object instead of rebuilding it."""
        from netbox_librenms_plugin.utils import get_module_types_indexed

        idx1 = get_module_types_indexed()
        idx2 = get_module_types_indexed()
        assert idx1 is idx2  # served from cache, not rebuilt

    def test_module_type_change_invalidates_cache(self):
        """Creating a ModuleType changes the fingerprint, so the next call rebuilds and indexes it."""
        from dcim.models import Manufacturer, ModuleType

        from netbox_librenms_plugin.utils import get_module_types_indexed

        idx1 = get_module_types_indexed()
        mfr, _ = Manufacturer.objects.get_or_create(name="IdxMfr", slug="idxmfr")
        ModuleType.objects.create(manufacturer=mfr, model="IDX-NEW", part_number="IDX-NEW")
        idx2 = get_module_types_indexed()

        assert idx1 is not idx2  # rebuilt after the change
        assert idx2.get("IDX-NEW") is not None  # the new type is indexed

    def test_concurrent_cold_access_builds_index_once(self):
        """Under concurrent cold-cache access the index is built exactly once (lock-serialised) and every caller gets the same object."""
        import threading
        from unittest.mock import patch

        import netbox_librenms_plugin.utils as u

        sentinel = u._ModuleTypeIndex()
        # Cold cache + a pinned fingerprint so all threads race into the rebuild path together.
        u._MODULE_TYPES_INDEX_CACHE = (None, None)
        results = []
        barrier = threading.Barrier(8)

        with (
            patch.object(u, "_module_types_index_version", return_value=("v",)),
            patch.object(u, "_build_module_types_index", return_value=sentinel) as mock_build,
        ):

            def worker():
                barrier.wait(timeout=10)
                results.append(u.get_module_types_indexed())

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        # The lock prevents the redundant concurrent rebuilds the unsynchronised version allowed...
        assert mock_build.call_count == 1
        # ...and every caller observes the one consistent index object.
        assert len(results) == 8
        assert all(r is sentinel for r in results)


@pytest.mark.django_db
class TestAdoptableTemplateInterfaceQueryCost:
    """Real-DB: the module-sync adoptable-interface count keeps its per-row query cost low."""

    def _view(self):
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        return object.__new__(BaseModuleTableView)

    def _device_with_installed_module(self):
        from dcim.models import (
            Device,
            DeviceRole,
            DeviceType,
            Interface,
            InterfaceTemplate,
            Manufacturer,
            Module,
            ModuleBay,
            ModuleType,
            Site,
        )

        mfr, _ = Manufacturer.objects.get_or_create(name="qcost-mfr", slug="qcost-mfr")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="qcost-dt", slug="qcost-dt")
        role, _ = DeviceRole.objects.get_or_create(name="qcost-role", slug="qcost-role")
        site, _ = Site.objects.get_or_create(name="qcost-site", slug="qcost-site")
        device = Device.objects.create(name="qcost-dev", device_type=dt, role=role, site=site, status="active")
        mt = ModuleType.objects.create(manufacturer=mfr, model="qcost-mt")
        for i in range(1, 4):
            InterfaceTemplate.objects.create(module_type=mt, name=f"Gi0/{i}", type="1000base-t")
        bay = ModuleBay.objects.create(device=device, name="Bay 1")
        Module.objects.create(device=device, module_bay=bay, module_type=mt)
        Interface.objects.create(device=device, name="standalone0", type="1000base-t", module=None)
        return device

    def test_get_module_bays_prefetches_installed_module_interface_templates(self, django_assert_num_queries):
        """_get_module_bays prefetches the installed module's interface templates, so the per-row
        adoptable-interface count does not re-query them once per row."""
        device = self._device_with_installed_module()
        view = self._view()
        device_bays, _ = view._get_module_bays(device)
        bay = device_bays["Bay 1"]
        with django_assert_num_queries(0):
            list(bay.installed_module.module_type.interfacetemplates.all())


@pytest.mark.django_db
class TestModulePartialUsesPostedServerKey:
    """The post-action HTMX partial re-renders the module table for the POSTed server_key."""

    def test_partial_resolves_posted_server_key_not_global(self):
        """The partial builds its table view for the POSTed server_key ('alpha'), not the globally-selected one ('beta')."""
        from unittest.mock import patch

        from django.contrib.auth.models import AnonymousUser
        from django.http import HttpResponse
        from django.test import RequestFactory

        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.views.sync.modules import _render_modules_partial_after_action

        device = _make_install_device()[0]

        # Two configured servers; the GLOBAL active server is 'beta', but the form POSTs 'alpha'.
        multi = {
            "alpha": {"librenms_url": "http://alpha", "api_token": "ta", "cache_timeout": 300, "verify_ssl": True},
            "beta": {"librenms_url": "http://beta", "api_token": "tb", "cache_timeout": 300, "verify_ssl": True},
        }
        LibreNMSSettings.objects.create(selected_server="beta")

        request = RequestFactory().post("/x/", {"server_key": "alpha"}, HTTP_HX_REQUEST="true")
        request.user = AnonymousUser()

        captured = {}

        def fake_render(req, template, context):
            captured["context"] = context
            return HttpResponse("ok")

        # Patch only the external config boundary (server catalogue) and the template render
        # (to capture the resolved context); the server_key resolution itself is real.
        with (
            patch("netbox_librenms_plugin.librenms_api.get_plugin_config", return_value=multi),
            patch("netbox_librenms_plugin.views.sync.modules.render", side_effect=fake_render),
        ):
            _render_modules_partial_after_action(request, device, "/sync/", lambda: True)

        # get_context_data returns server_key from the table view's LibreNMSAPI; it must be the
        # POSTed 'alpha' (the cache namespace the install acted on), not the global 'beta'.
        assert captured["context"]["module_sync"]["server_key"] == "alpha"


@pytest.mark.django_db
class TestMergeRawDuplicatePreservesMac:
    """Folding a module's raw template interface into its adopted twin must not lose MAC data."""

    @staticmethod
    def _device():
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        mfr, _ = Manufacturer.objects.get_or_create(name="Merge-Mfr", slug="merge-mfr")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="Merge-DT", slug="merge-dt")
        role, _ = DeviceRole.objects.get_or_create(name="Merge-Role", slug="merge-role")
        site, _ = Site.objects.get_or_create(name="Merge-Site", slug="merge-site")
        return Device.objects.create(name="merge-dev", device_type=dt, role=role, site=site, status="active")

    def test_raw_twin_mac_is_reassigned_to_adopted_before_delete(self):
        """The raw twin's MAC (and primary-MAC) move to the adopted interface instead of being deleted with it."""
        from dcim.models import Interface, MACAddress

        from netbox_librenms_plugin.views.sync.modules import _merge_raw_duplicate_interface

        device = self._device()
        # Authoritative externally-named interface (adopted); no MAC of its own yet.
        adopted = Interface.objects.create(device=device, name="GigabitEthernet0/1", type="1000base-t")
        # Module's raw template twin that accumulated a MAC from a prior partial sync.
        raw = Interface.objects.create(device=device, name="1/1", type="1000base-t")
        mac = MACAddress.objects.create(mac_address="00:11:22:33:44:55", assigned_object=raw)
        raw.primary_mac_address = mac
        raw.save()

        result = _merge_raw_duplicate_interface(raw, adopted, server_key="default")

        assert result is True
        assert not Interface.objects.filter(pk=raw.pk).exists()
        # The MAC survives and is now assigned to the adopted interface...
        assert MACAddress.objects.filter(pk=mac.pk).exists()
        mac.refresh_from_db()
        assert mac.assigned_object_id == adopted.pk
        adopted.refresh_from_db()
        assert adopted.mac_addresses.filter(pk=mac.pk).exists()
        # ...and the adopted interface (which had no primary MAC) inherits it.
        assert adopted.primary_mac_address_id == mac.pk


@pytest.mark.django_db
class TestAdoptBatchesRenamePrediction:
    """_adopt_existing_template_interfaces resolves all raw names in ONE rename-prediction dispatch."""

    def test_rename_prediction_is_dispatched_once_for_all_raw_interfaces(self):
        """No per-interface (single-name) predict dispatch happens; raw names are resolved in one batched call."""
        from dcim.models import (
            Device,
            DeviceRole,
            DeviceType,
            InterfaceTemplate,
            Manufacturer,
            Module,
            ModuleBay,
            ModuleType,
            Site,
        )
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        mfr, _ = Manufacturer.objects.get_or_create(name="Batch-Mfr", slug="batch-mfr")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="Batch-DT", slug="batch-dt")
        role, _ = DeviceRole.objects.get_or_create(name="Batch-Role", slug="batch-role")
        site, _ = Site.objects.get_or_create(name="Batch-Site", slug="batch-site")
        device = Device.objects.create(name="batch-dev", device_type=dt, role=role, site=site, status="active")

        mtype = ModuleType.objects.create(manufacturer=mfr, model="Batch-MT")
        InterfaceTemplate.objects.create(module_type=mtype, name="1/1", type="1000base-t")
        InterfaceTemplate.objects.create(module_type=mtype, name="1/2", type="1000base-t")
        bay = ModuleBay.objects.create(device=device, name="Bay1")
        module = Module.objects.create(device=device, module_bay=bay, module_type=mtype)

        recorded = []

        @receiver(predict_module_interface_names)
        def rename(sender, device, module, names, **kwargs):
            recorded.append(list(names))
            return ["Gi" + n for n in names]

        try:
            # A standalone interface matching a predicted template name takes adoption past the
            # early "no matches" return so the rename-prediction loop actually runs.
            from dcim.models import Interface

            Interface.objects.create(device=device, name="Gi1/1", type="1000base-t")

            result = _adopt_existing_template_interfaces(device, module)
        finally:
            predict_module_interface_names.disconnect(rename)

        assert result["status"] == "bound"
        # The module's two raw interfaces ('1/1', '1/2') must be predicted in a single dispatch.
        # A per-interface implementation would emit single-name dispatches; assert none exist.
        single_name_dispatches = [names for names in recorded if len(names) == 1]
        assert single_name_dispatches == [], f"rename prediction was not batched: {recorded}"


class TestRenderModulesPartialServerKeyGuard:
    """_render_modules_partial_after_action must not 500 on a stale/forged POSTed server_key.

    In a multi-server config, passing the raw key straight into LibreNMSAPI(server_key=...) raises
    KeyError for an unconfigured key. The HTMX partial must validate membership first and leave the
    default-server API in place instead of crashing the in-place table swap.
    """

    def test_forged_server_key_does_not_500_the_partial(self):
        from django.http import HttpResponse
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView
        from netbox_librenms_plugin.views.sync import modules as modules_mod

        device = _make_device()
        # HX-Request → the in-place swap path (where the rebind happens).
        request = RequestFactory().post("/", {"server_key": "forged-key"}, HTTP_HX_REQUEST="true")

        servers = {
            "alpha": {"librenms_url": "https://a.example.com", "api_token": "t"},
            "beta": {"librenms_url": "https://b.example.com", "api_token": "t"},
        }
        with (
            patch(
                "netbox_librenms_plugin.librenms_api.get_plugin_config",
                side_effect=lambda app, key, default=None: servers if key == "servers" else default,
            ),
            patch.object(DeviceModuleTableView, "get_context_data", return_value={}),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value=HttpResponse("ok")),
        ):
            # On the unfixed code LibreNMSAPI(server_key="forged-key") raises KeyError here.
            resp = modules_mod._render_modules_partial_after_action(request, device, "/sync/", lambda: True)

        assert resp.status_code == 200


class TestModulesRedirectPreservesServerKey:
    """_modules_redirect_response must carry a configured POSTed server_key on the classic redirect."""

    def _request(self, server_key=None):
        from django.test import RequestFactory

        data = {"server_key": server_key} if server_key is not None else {}
        return RequestFactory().post("/", data)

    def test_classic_redirect_preserves_configured_server_key(self):
        """A non-HTMX action on a configured non-default server redirects back to that server's tab."""
        from netbox_librenms_plugin.views.sync import modules as modules_mod

        request = self._request("prod")
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"prod": "Prod"},
        ):
            resp = modules_mod._modules_redirect_response(request, "/sync/url")

        assert resp.status_code == 302
        assert "tab=modules" in resp.url
        assert "server_key=prod" in resp.url

    def test_classic_redirect_drops_unconfigured_server_key(self):
        """A stale/forged server_key is not forwarded on the redirect."""
        from netbox_librenms_plugin.views.sync import modules as modules_mod

        request = self._request("ghost")
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"prod": "Prod"},
        ):
            resp = modules_mod._modules_redirect_response(request, "/sync/url")

        assert "server_key" not in resp.url

    def test_htmx_redirect_preserves_configured_server_key(self):
        """The HTMX HX-Redirect target carries the configured server_key too."""
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.sync import modules as modules_mod

        request = RequestFactory().post("/", {"server_key": "prod"}, HTTP_HX_REQUEST="true")
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"prod": "Prod"},
        ):
            resp = modules_mod._modules_redirect_response(request, "/sync/url")

        assert resp.status_code == 204
        assert "server_key=prod" in resp["HX-Redirect"]


@pytest.mark.django_db
class TestModulePartialDegradesOnBrokenDefault:
    """_render_modules_partial_after_action degrades instead of 500ing when the default server is misconfigured."""

    def test_render_after_action_does_not_500(self):
        """With a broken default and no configured POST key, the committed action renders an empty panel (200)."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory, override_settings
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.modules import _render_modules_partial_after_action

        mfr, _ = Manufacturer.objects.get_or_create(name="Mfr-mpd", slug="mfr-mpd")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-mpd", slug="dt-mpd")
        role, _ = DeviceRole.objects.get_or_create(name="Role-mpd", slug="role-mpd")
        site, _ = Site.objects.get_or_create(name="Site-mpd", slug="site-mpd")
        device = Device.objects.create(name="host-mpd", device_type=dt, role=role, site=site, status="active")

        request = RequestFactory().post("/modules/action/", data={})
        request.headers = {"HX-Request": "true"}  # force the HTMX render branch
        request.user = AnonymousUser()  # the rendered partial's context processors read request.user
        sync_url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", kwargs={"pk": device.pk})

        with override_settings(PLUGINS_CONFIG={"netbox_librenms_plugin": {"servers": {"default": {}}}}):
            response = _render_modules_partial_after_action(request, device, sync_url, lambda: True)

        assert response.status_code == 200


@pytest.mark.django_db
class TestInstallModuleBindMessageGating:
    """InstallModuleView must not report a 'Bound ...' info message for a no-op bind (changed=False)."""

    def _build(self, prebind):
        from dcim.models import (
            Device,
            DeviceRole,
            DeviceType,
            Interface,
            InterfaceTemplate,
            Manufacturer,
            ModuleBay,
            ModuleType,
            Site,
        )

        tag = "prebound" if prebind else "fresh"
        mfr, _ = Manufacturer.objects.get_or_create(name=f"Mfr-bindmsg-{tag}", slug=f"mfr-bindmsg-{tag}")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"DT-bindmsg-{tag}", slug=f"dt-bindmsg-{tag}")
        role, _ = DeviceRole.objects.get_or_create(name=f"Role-bindmsg-{tag}", slug=f"role-bindmsg-{tag}")
        site, _ = Site.objects.get_or_create(name=f"Site-bindmsg-{tag}", slug=f"site-bindmsg-{tag}")
        device = Device.objects.create(
            name=f"host-bindmsg-{tag}", device_type=dt, role=role, site=site, status="active"
        )

        mt = ModuleType.objects.create(manufacturer=mfr, model=f"MT-bindmsg-{tag}")
        InterfaceTemplate.objects.create(module_type=mt, name="Gi0/1", type="other")
        bay = ModuleBay.objects.create(device=device, name="bay-bindmsg")

        # A standalone interface matching the module template name; the install adopts it (sets
        # module FK) before the port-bind runs. When it is ALSO already bound to the posted port_id,
        # the subsequent _bind_interface_librenms_id is a no-op (changed=False).
        Interface.objects.filter(device=device).delete()
        standalone = Interface.objects.create(device=device, name="Gi0/1", type="other")
        if prebind:
            standalone.custom_field_data["librenms_id"] = {"default": 42}
            standalone.save(update_fields=["custom_field_data"])
        return device, bay, mt

    def _install(self, device, bay, mt):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        view = object.__new__(InstallModuleView)
        request = _make_request(
            "POST",
            data={
                "module_bay_id": str(bay.pk),
                "module_type_id": str(mt.pk),
                "librenms_port_id": "42",
                "librenms_ifname": "Gi0/1",
                "server_key": "default",
            },
        )
        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch.object(view, "has_write_permission", return_value=True),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch(
                "netbox_librenms_plugin.views.sync.modules._render_modules_partial_after_action",
                return_value="RENDERED",
            ),
        ):
            view.post(request, pk=device.pk)
        return mock_msg

    def test_noop_bind_suppresses_bound_message(self):
        """A port already on the adopted interface (no-op bind) must NOT emit a 'Bound ...' info toast."""
        device, bay, mt = self._build(prebind=True)
        mock_msg = self._install(device, bay, mt)

        info_texts = [c.args[1] for c in mock_msg.info.call_args_list]
        assert not any("Bound" in t and "port_id 42" in t for t in info_texts), info_texts

    def test_real_bind_reports_bound_message(self):
        """A port newly bound to the adopted interface (changed=True) still reports the 'Bound ...' toast."""
        device, bay, mt = self._build(prebind=False)
        mock_msg = self._install(device, bay, mt)

        info_texts = [c.args[1] for c in mock_msg.info.call_args_list]
        assert any("Bound" in t and "port_id 42" in t for t in info_texts), info_texts
