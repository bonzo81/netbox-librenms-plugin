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


from netbox_librenms_plugin.tests.view_test_helpers import get as _get, post as _post


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


def _run_install_single(device, item, index_map, module_types, **kwargs):
    """Run the shared installer against real NetBox models and querysets."""
    from dcim.models import Interface, ModuleBay

    from netbox_librenms_plugin.views.sync.modules import InstallBranchView, _module_component_specs

    allowed_type_ids = {module_type.pk for module_type in module_types.values()}
    changeable_components = {model: model.objects.all() for _, _, model in _module_component_specs()}
    return InstallBranchView._install_single(
        device,
        item,
        index_map,
        module_types,
        module_bays=ModuleBay.objects.all(),
        allowed_module_type_ids=allowed_type_ids,
        changeable_components=changeable_components,
        changeable_interfaces=Interface.objects.all(),
        deletable_interfaces=Interface.objects.all(),
        **kwargs,
    )


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
        """LibreNMSAPIMixin must stay in the MRO so resolve_posted_server_key scopes the port_id bind."""
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        assert LibreNMSAPIMixin in InstallModuleView.__mro__


class TestUpdateModuleInterfaceViewWiring:
    """UpdateModuleInterfaceView must resolve server_key through LibreNMSAPIMixin (mirror of Install)."""

    def test_has_librenms_permission_mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        assert LibreNMSPermissionMixin in UpdateModuleInterfaceView.__mro__

    def test_has_librenms_api_mixin(self):
        """LibreNMSAPIMixin must stay in the MRO so a blank/forged server_key degrades, not skips binding."""
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        assert LibreNMSAPIMixin in UpdateModuleInterfaceView.__mro__


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


class TestCandidateBaysForItem:
    """InstallBranchView._candidate_bays_for_item selects the bay set to match against."""

    @staticmethod
    def _b(name, module_id=None, pk=None):
        bay = MagicMock()
        bay.name = name
        bay.module_id = module_id
        bay.pk = pk if pk is not None else abs(hash((name, module_id))) % 100000
        return bay

    def test_orphan_item_includes_module_scoped_bays(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        top = self._b("Slot 1", module_id=None)
        txr = self._b("Transceiver 0/19", module_id=942)  # module-scoped, never top-level
        result = InstallBranchView._candidate_bays_for_item([top, txr], parent_module_id=None)

        # The module-scoped transceiver bay must be reachable (old code returned top-level only).
        assert result.get("Transceiver 0/19") is txr
        assert result.get("Slot 1") is top

    def test_parent_scoped_returns_only_that_modules_bays(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        a = self._b("Transceiver 0/19", module_id=942)
        b = self._b("Transceiver 0/19", module_id=943)
        top = self._b("Slot 1", module_id=None)
        result = InstallBranchView._candidate_bays_for_item([a, b, top], parent_module_id=942)

        assert result == {"Transceiver 0/19": a}  # only module 942's bays, nothing else

    def test_device_bay_wins_on_name_collision(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device_bay = self._b("Bay 1", module_id=None)
        scoped_bay = self._b("Bay 1", module_id=942)
        result = InstallBranchView._candidate_bays_for_item([scoped_bay, device_bay], parent_module_id=None)

        # Device-level bay takes precedence over a same-named module-scoped bay (mirrors all_bays).
        assert result["Bay 1"] is device_bay

    def test_ambiguous_cross_module_bay_name_is_dropped(self):
        """A bay name defined by TWO different installed modules is not an install target: picking the lowest-PK module's bay would install the item into the wrong sibling line card — the row must skip as 'no matching bay' (develop behaviour) instead."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        a = self._b("Transceiver 1", module_id=942)
        b = self._b("Transceiver 1", module_id=943)  # same child-bay name on a sibling module
        unique = self._b("Transceiver 2", module_id=943)
        result = InstallBranchView._candidate_bays_for_item([a, b, unique], parent_module_id=None)

        assert "Transceiver 1" not in result
        # Unique module-scoped names stay reachable — the fallback's whole purpose.
        assert result.get("Transceiver 2") is unique

    def test_ambiguous_name_overridden_by_device_bay_is_kept(self):
        """A device-level bay sharing the ambiguous name IS the unambiguous target (device bays win the merge, matching the pre-fallback behaviour)."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        a = self._b("Bay 9", module_id=942)
        b = self._b("Bay 9", module_id=943)
        dev = self._b("Bay 9", module_id=None)
        result = InstallBranchView._candidate_bays_for_item([a, b, dev], parent_module_id=None)

        assert result["Bay 9"] is dev


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
                mock_mbm.objects.restrict.return_value = mock_mbm.objects
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
                mock_mbm.objects.restrict.return_value = mock_mbm.objects
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
                mock_mbm.objects.restrict.return_value = mock_mbm.objects
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

    def test_component_scope_tracks_netbox_module_adoption_models(self):
        """Fail when a NetBox upgrade adds an adoption path that this scope does not cover."""
        import ast
        import inspect
        import textwrap

        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import _module_component_specs

        # NetBox 4.7 moved the replication loop into Module._save_new().
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(Module, "_save_new", Module.save))))
        component_loop = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Tuple)
            and [element.id for element in node.target.elts if isinstance(element, ast.Name)]
            == ["templates", "component_attribute", "component_model"]
        )
        netbox_specs = [
            (entry.elts[0].value, entry.elts[1].value, entry.elts[2].id) for entry in component_loop.iter.elts
        ]
        scoped_specs = [
            (template_attribute, component_attribute, component_model.__name__)
            for template_attribute, component_attribute, component_model in _module_component_specs()
        ]

        assert scoped_specs == netbox_specs, f"NetBox replicates {netbox_specs}, the plugin scopes {scoped_specs}"

    @pytest.mark.django_db
    def test_returns_installed_on_success(self):
        """A real Module is created in the matched bay and persisted (the create was faked before)."""
        from dcim.models import Module

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("install-ok-dev")
        bay = make_module_bay(device, "Slot 1")
        mt = make_module_type("WS-X4748")
        item = {
            "entPhysicalIndex": 10,
            "entPhysicalModelName": "WS-X4748",
            "entPhysicalSerialNum": "SN123",
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
        }

        with (
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch.object(InstallBranchView, "_find_parent_module_id", return_value=None),
            patch.object(InstallBranchView, "_match_bay", return_value=bay),
        ):
            result = _run_install_single(device, item, {10: item}, {"WS-X4748": mt})

        assert result["status"] == "installed"
        assert "WS-X4748" in result["name"]
        # A real Module now occupies the bay with the LibreNMS serial.
        module = Module.objects.get(pk=result["module_pk"])
        assert module.module_bay_id == bay.pk
        assert module.module_type_id == mt.pk
        assert module.serial == "SN123"

    @pytest.mark.django_db
    def test_returns_skipped_when_no_type(self):
        """No module type matches the model → skipped, no Module created."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay

        device = make_device("install-notype-dev")
        make_module_bay(device, "Slot 1")
        item = {
            "entPhysicalIndex": 10,
            "entPhysicalModelName": "WS-X4748",
            "entPhysicalSerialNum": "SN123",
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
        }

        result = _run_install_single(device, item, {10: item}, {})

        assert result["status"] == "skipped"
        assert "no matching type" in result["reason"]

    @pytest.mark.django_db
    def test_oob_sourced_item_is_never_installed(self):
        """An OOB-sourced inventory row must be skipped (read-only), even with a matching type+bay — a crafted POST must not install OOB inventory onto the host."""
        from dcim.models import Module

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type

        device = make_device("install-oob-dev")
        make_module_bay(device, "Slot 1")
        mt = make_module_type("WS-X4748")
        # A fully installable row (matching type + free bay) — only _source="oob" must block it.
        item = {
            "entPhysicalIndex": 1010,
            "entPhysicalModelName": "WS-X4748",
            "entPhysicalSerialNum": "SN123",
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
            "_source": "oob",
        }

        result = _run_install_single(device, item, {1010: item}, {"WS-X4748": mt})

        assert result["status"] == "skipped"
        assert "OOB controller inventory is read-only" in result["reason"]
        assert not Module.objects.filter(device=device).exists()

    @pytest.mark.django_db
    def test_returns_skipped_when_no_bay(self):
        """A matching type but no matching bay → skipped, no Module created."""
        from dcim.models import Module

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("install-nobay-dev")
        make_module_bay(device, "Slot 1")
        mt = make_module_type("WS-X4748")
        item = {
            "entPhysicalIndex": 10,
            "entPhysicalModelName": "WS-X4748",
            "entPhysicalSerialNum": "SN123",
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
        }

        with (
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch.object(InstallBranchView, "_find_parent_module_id", return_value=None),
            patch.object(InstallBranchView, "_match_bay", return_value=None),
        ):
            result = _run_install_single(device, item, {10: item}, {"WS-X4748": mt})

        assert result["status"] == "skipped"
        assert "no matching bay" in result["reason"]
        assert not Module.objects.filter(device=device).exists()

    @pytest.mark.django_db
    def test_returns_skipped_when_bay_already_occupied(self):
        """The matched bay already holds a real module → skipped, returns the occupant's pk."""
        from dcim.models import Module

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("install-occupied-dev")
        bay = make_module_bay(device, "Slot 1")
        mt = make_module_type("WS-X4748")
        # Pre-install a real module so the locked bay reads as occupied.
        occupied = Module.objects.create(device=device, module_bay=bay, module_type=mt, status="active")
        item = {
            "entPhysicalIndex": 10,
            "entPhysicalModelName": "WS-X4748",
            "entPhysicalSerialNum": "SN123",
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
        }

        with (
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch.object(InstallBranchView, "_find_parent_module_id", return_value=None),
            patch.object(InstallBranchView, "_match_bay", return_value=bay),
        ):
            result = _run_install_single(device, item, {10: item}, {"WS-X4748": mt})

        assert result["status"] == "skipped"
        assert "already occupied" in result["reason"]
        assert result["module_pk"] == occupied.pk

    @pytest.mark.django_db
    def test_installed_name_includes_real_adoption_count(self):
        from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay, ModuleBayTemplate

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("install-adoption-count")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("ADOPT-MODULE")
        InterfaceTemplate.objects.create(module_type=module_type, name="Te1/1/1", type="10gbase-x-sfpp")
        ModuleBayTemplate.objects.create(module_type=module_type, name="Nested Bay")
        standalone_interface = Interface.objects.create(device=device, name="Te1/1/1", type="10gbase-x-sfpp")
        standalone_bay = ModuleBay.objects.create(device=device, name="Nested Bay")
        item = {
            "entPhysicalIndex": 10,
            "entPhysicalModelName": module_type.model,
            "entPhysicalSerialNum": "SN-ADOPT",
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
        }

        with (
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch.object(InstallBranchView, "_find_parent_module_id", return_value=None),
            patch.object(InstallBranchView, "_match_bay", return_value=bay),
        ):
            result = _run_install_single(device, item, {10: item}, {module_type.model: module_type})

        assert result["status"] == "installed"
        assert result["adopted_components"] == 2
        standalone_interface.refresh_from_db()
        standalone_bay.refresh_from_db()
        assert standalone_interface.module_id == result["module_pk"]
        assert standalone_bay.module_id == result["module_pk"]
        assert Module.objects.filter(pk=result["module_pk"]).exists()

    @pytest.mark.django_db
    def test_validation_error_returns_failed_without_creating_a_module(self):
        from dcim.models import Module

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("install-validation-failure")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("INVALID-SERIAL-MODULE")
        item = {
            "entPhysicalIndex": 10,
            "entPhysicalModelName": module_type.model,
            "entPhysicalSerialNum": "x" * 500,
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
        }

        with (
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch.object(InstallBranchView, "_find_parent_module_id", return_value=None),
            patch.object(InstallBranchView, "_match_bay", return_value=bay),
        ):
            result = _run_install_single(device, item, {10: item}, {module_type.model: module_type})

        assert result["status"] == "failed"
        assert not Module.objects.filter(device=device).exists()

    @pytest.mark.django_db
    def test_dash_serial_is_persisted_as_empty(self):
        from dcim.models import Module

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("install-empty-serial")
        bay = make_module_bay(device, "Slot 1")
        module_type = make_module_type("EMPTY-SERIAL-MODULE")
        item = {
            "entPhysicalIndex": 10,
            "entPhysicalModelName": module_type.model,
            "entPhysicalSerialNum": "-",
            "entPhysicalName": "Line Card",
            "entPhysicalContainedIn": 0,
        }

        with (
            patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
            patch.object(InstallBranchView, "_find_parent_module_id", return_value=None),
            patch.object(InstallBranchView, "_match_bay", return_value=bay),
        ):
            result = _run_install_single(device, item, {10: item}, {module_type.model: module_type})

        assert result["status"] == "installed"
        assert Module.objects.get(pk=result["module_pk"]).serial == ""


@pytest.mark.django_db
class TestModuleInterfaceHelpers:
    """Exercise module interface adoption and binding against the real ORM."""

    @staticmethod
    def _module(suffix):
        from dcim.models import Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device(f"module-helper-{suffix}")
        bay = ModuleBay.objects.create(device=device, name=f"Helper Bay {suffix}")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model=f"Helper Module {suffix}",
        )
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=module_type,
            status="active",
        )
        return device, module

    def test_bind_returns_none_without_port_identity(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device, module = self._module("no-port")
        result = _bind_interface_librenms_id(
            device,
            {"entPhysicalName": "SFP"},
            module.pk,
            "default",
            Interface.objects.all(),
        )

        assert result is None

    def test_adopts_matching_standalone_interfaces(self):
        from dcim.models import Interface, InterfaceTemplate

        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        device, module = self._module("adopt")
        InterfaceTemplate.objects.create(
            module_type=module.module_type,
            name="Te1/1/1",
            type="10gbase-x-sfpp",
        )
        InterfaceTemplate.objects.create(
            module_type=module.module_type,
            name="Te1/1/2",
            type="10gbase-x-sfpp",
        )
        first = Interface.objects.create(device=device, name="Te1/1/1", type="10gbase-x-sfpp")
        second = Interface.objects.create(device=device, name="Te1/1/2", type="10gbase-x-sfpp")

        result = _adopt_existing_template_interfaces(device, module, Interface.objects.all())

        assert result["status"] == "bound"
        assert result["adopted_count"] == 2
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.module_id == module.pk
        assert second.module_id == module.pk

    def test_adopts_vc_rewritten_template_interface(self):
        from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.views.sync.modules import _adopt_existing_template_interfaces

        _vc, (_page, device) = make_virtual_chassis_members("module-helper-vc-adopt")
        bay = ModuleBay.objects.create(device=device, name="Helper VC Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Helper VC Module",
        )
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=module_type,
            status="active",
        )
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name="TenGigabitEthernet1/1/1",
            type="10gbase-x-sfpp",
        )
        interface = Interface.objects.create(
            device=device,
            name=f"TenGigabitEthernet{device.vc_position}/1/1",
            type="10gbase-x-sfpp",
        )

        result = _adopt_existing_template_interfaces(device, module, Interface.objects.all())

        assert result["status"] == "bound"
        interface.refresh_from_db()
        assert interface.module_id == module.pk

    def test_binds_unique_module_interface(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device, module = self._module("bind-name")
        interface = Interface.objects.create(
            device=device,
            module=module,
            name="Te1/0/1",
            type="10gbase-x-sfpp",
        )

        result = _bind_interface_librenms_id(
            device,
            {"_librenms_port_id": 42, "_librenms_ifname": interface.name},
            module.pk,
            "default",
            Interface.objects.all(),
        )

        assert result == {
            "status": "bound",
            "interface": interface.name,
            "port_id": 42,
            "changed": True,
        }
        interface.refresh_from_db()
        assert get_librenms_device_id(interface, "default") == 42

    def test_binds_only_module_interface_when_names_do_not_match(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device, module = self._module("bind-only")
        interface = Interface.objects.create(
            device=device,
            module=module,
            name="uplink-a",
            type="10gbase-x-sfpp",
        )

        result = _bind_interface_librenms_id(
            device,
            {"_librenms_port_id": 43, "_librenms_ifname": "Unknown-Port"},
            module.pk,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "bound"
        assert result["interface"] == interface.name
        interface.refresh_from_db()
        assert get_librenms_device_id(interface, "default") == 43

    def test_reports_conflict_when_port_id_belongs_to_another_device(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device, module = self._module("bind-conflict")
        other = make_device("module-helper-bind-other")
        owner = make_interface(other, "Eth1/1")
        set_librenms_device_id(owner, 55, "default")
        owner.save()

        result = _bind_interface_librenms_id(
            device,
            {"_librenms_port_id": 55, "_librenms_ifname": "Te1/0/2"},
            module.pk,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "conflict"
        assert "not reassigning" in result["reason"]

    def test_reparents_standalone_interface_when_module_is_known(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device, module = self._module("bind-reparent")
        interface = make_interface(device, "Te1/0/1")
        set_librenms_device_id(interface, 77, "default")
        interface.save()

        result = _bind_interface_librenms_id(
            device,
            {"_librenms_port_id": 77, "_librenms_ifname": interface.name},
            module.pk,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "bound"
        interface.refresh_from_db()
        assert interface.module_id == module.pk
        assert get_librenms_device_id(interface, "default") == 77

    def test_uses_ifdescr_when_ifname_differs(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device, _module = self._module("bind-ifdescr")
        interface = make_interface(device, "TenGigabitEthernet1/0/1")

        result = _bind_interface_librenms_id(
            device,
            {
                "_librenms_port_id": 88,
                "_librenms_ifname": "Te1/0/1",
                "_librenms_ifdescr": interface.name,
                "entPhysicalName": "Unknown-Port",
            },
            None,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "bound"
        interface.refresh_from_db()
        assert get_librenms_device_id(interface, "default") == 88

    def test_uses_coordinate_fallback_for_multiple_module_interfaces(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device, module = self._module("bind-coordinate")
        first = Interface.objects.create(
            device=device,
            module=module,
            name="GigabitEthernet1/1/23",
            type="1000base-t",
        )
        second = Interface.objects.create(
            device=device,
            module=module,
            name="GigabitEthernet1/1/24",
            type="1000base-t",
        )

        result = _bind_interface_librenms_id(
            device,
            {
                "_librenms_port_id": 4242,
                "_librenms_ifname": "GigabitEthernet5/1/24",
                "_librenms_ifdescr": "Gi5/1/24",
            },
            module.pk,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "bound"
        assert result["interface"] == second.name
        first.refresh_from_db()
        second.refresh_from_db()
        assert get_librenms_device_id(first, "default") is None
        assert get_librenms_device_id(second, "default") == 4242

    def test_coordinate_fallback_skips_an_ambiguous_top_score(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

        device, module = self._module("bind-ambiguous")
        Interface.objects.create(
            device=device,
            module=module,
            name="TenGigabitEthernet1/1/24",
            type="10gbase-x-sfpp",
        )
        Interface.objects.create(
            device=device,
            module=module,
            name="HundredGigE2/1/24",
            type="100gbase-x-qsfp28",
        )

        result = _bind_interface_librenms_id(
            device,
            {
                "_librenms_port_id": 4343,
                "_librenms_ifname": "Port5/0/24",
                "_librenms_ifdescr": "Port5/0/24",
            },
            module.pk,
            "default",
            Interface.objects.all(),
        )

        assert result["status"] == "skipped"
        assert "multiple module interfaces found" in result["reason"]


class TestSingleInstallInterfaceBinding:
    """Single-row install should resolve inventory identity and bind interfaces."""

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
        view._librenms_api = MagicMock(server_key="production")
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
        mock_qs.filter.return_value.first.return_value = module_bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
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
            # The locked re-fetch goes through restrict(user, ...), so hand back the same manager.
            mock_objects.restrict.return_value = mock_objects
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
            view.request = request
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
        view._librenms_api = MagicMock(server_key="production")
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
        mock_qs.filter.return_value.first.return_value = module_bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
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
                return_value={"status": "bound", "interface": "Te1/1/1", "port_id": 42},
            ) as mock_bind,
        ):
            mock_tx.atomic = noop_atomic
            mock_module_cls.return_value = new_module
            # The locked re-fetch goes through restrict(user, ...), so hand back the same manager.
            mock_objects.restrict.return_value = mock_objects
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
            view.request = request
            view.post(request, pk=24)

        mock_bind.assert_called_once()
        bind_call = mock_bind.call_args
        assert bind_call.args[0] is device
        assert bind_call.args[2] == 321
        assert bind_call.args[3] == "production"
        assert bind_call.args[1]["_librenms_port_id"] == 42
        mock_messages.info.assert_called()

    def test_install_module_view_binds_with_blank_server_key_via_active_fallback(self):
        """A blank posted server_key falls back to the active server so the port_id bind still runs."""
        from contextlib import contextmanager

        from dcim.models import ModuleBay

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        view = object.__new__(InstallModuleView)
        view.required_object_permissions = {}
        # The active client server that resolve_posted_server_key falls back to for a blank posted key.
        view._librenms_api = MagicMock(server_key="production")
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
                # Blank — e.g. a fallback render where module_sync.server_key is empty. Pre-fix this
                # stayed "" and `if bind_item and server_key` below silently skipped the bind.
                "server_key": "",
                "ent_index": "77",
            },
        )

        @contextmanager
        def noop_atomic():
            yield

        mock_qs = MagicMock()
        mock_qs.filter.return_value.first.return_value = module_bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
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
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value={"status": "bound", "interface": "Te1/1/1", "port_id": 42},
            ) as mock_bind,
        ):
            mock_tx.atomic = noop_atomic
            mock_module_cls.return_value = new_module
            # The locked re-fetch goes through restrict(user, ...), so hand back the same manager.
            mock_objects.restrict.return_value = mock_objects
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
            view.request = request
            view.post(request, pk=24)

        # The bind must run and be scoped to the active server the blank key fell back to.
        mock_bind.assert_called_once()
        assert mock_bind.call_args.args[3] == "production"

    def test_install_module_view_rejects_missing_bay_id_for_interface_child(self):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        view = object.__new__(InstallModuleView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="production")
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.request = request
            view.post(request, pk=24)

        mock_messages.error.assert_called_once()
        assert "invalid module bay/module type id" in mock_messages.error.call_args[0][1].lower()
        mock_redirect.assert_called_once()

    def test_update_module_interface_view_binds_existing_interface(self):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="production")
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
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, module],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=999),
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                # changed=True: a real (re)bind wrote update_fields, so the success toast fires;
                # the message is gated on `changed` (develop's _bind_interface_librenms_id contract).
                return_value={"status": "bound", "interface": "Te1/1/1", "port_id": 42, "changed": True},
            ) as mock_bind,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="redirected"),
        ):
            mock_cache.get.return_value = {
                "inventory": [{"entPhysicalIndex": 77, "_librenms_port_id": 42, "_librenms_ifname": "Te1/1/1"}],
                "librenms_id": 999,
            }
            view.request = request
            response = view.post(request, pk=24)

        mock_bind.assert_called_once()
        mock_messages.success.assert_called_once()
        assert response is not None

    def test_update_module_interface_view_reports_when_no_bind_or_adoption_is_needed(self):
        """A duplicate request still gets the helper's explicit no-op success message."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="production")
        device = _make_device()

        module = MagicMock()
        module.pk = 322
        module.module_type.model = "SFP-10G-SR"
        module.module_bay.name = "SFP 2"
        request = _make_request(
            "POST",
            data={"module_id": "322", "server_key": "production", "ent_index": "78"},
        )

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, module],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=999),
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value=None,
            ),
            patch(
                "netbox_librenms_plugin.views.sync.modules._adopt_existing_template_interfaces",
                return_value={"status": "bound", "adopted_count": 0, "interfaces": []},
            ),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="redirected"),
        ):
            mock_cache.get.return_value = {
                "inventory": [{"entPhysicalIndex": 78, "_librenms_port_id": 43, "_librenms_ifname": "Te1/1/2"}],
                "librenms_id": 999,
            }
            view.request = request
            response = view.post(request, pk=24)

        mock_messages.success.assert_called_once()
        assert "No interface changes were needed" in mock_messages.success.call_args.args[1]
        assert response == "redirected"

    @pytest.mark.django_db
    def test_update_module_interface_view_adopts_real_template_interfaces(self):
        from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("update-module-interface-adopt")
        bay = ModuleBay.objects.create(device=device, name="Update Interface Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Update Interface Module",
        )
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=module_type,
            status="active",
        )
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name="Te1/1/1",
            type="10gbase-x-sfpp",
        )
        standalone = Interface.objects.create(
            device=device,
            name="Te1/1/1",
            type="10gbase-x-sfpp",
        )
        user = make_user_with_perms(
            "update-module-interface-adopt",
            [("view", Device), ("view", Module), ("change", Interface)],
        )
        request = make_request(
            "post",
            {"module_id": str(module.pk), "server_key": "default"},
            user=user,
        )
        view = UpdateModuleInterfaceView()
        view._librenms_api = MagicMock(server_key="default")

        _post(view, request, pk=device.pk)

        standalone.refresh_from_db()
        assert standalone.module_id == module.pk

    def test_update_module_interface_view_no_server_key_does_not_fake_adoption_success(self):
        """bind_item resolves but server_key degrades to blank (no active server), so the bind is skipped."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="")
        device = _make_device()

        module = MagicMock()
        module.pk = 321
        module.module_type.model = "SFP-10G-SR"
        module.module_bay.name = "SFP 1"

        # No server_key posted AND the active server resolves to blank → bind cannot be attempted
        # (post-fix, a blank posted key alone falls back to the active server; only a blank active
        # server leaves server_key empty and reaches this fail-closed "no server context" branch).
        request = _make_request("POST", data={"module_id": "321", "ent_index": "77"})

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, module],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            # bind_item resolves (a primary identity exists) even though server_key is blank.
            patch(
                "netbox_librenms_plugin.views.sync.modules._resolve_single_install_binding_item",
                return_value={"entPhysicalName": "Te1/1/1", "_librenms_port_id": 42},
            ),
            patch("netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id") as mock_bind,
            patch("netbox_librenms_plugin.views.sync.modules._adopt_existing_template_interfaces") as mock_adopt,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="redirected"),
        ):
            mock_cache.get.return_value = {"inventory": [], "librenms_id": 999}
            view.request = request
            response = view.post(request, pk=24)

        mock_bind.assert_not_called()  # no server context → bind never attempted
        mock_adopt.assert_not_called()  # and we must NOT adopt-and-succeed instead
        mock_messages.success.assert_not_called()
        mock_messages.warning.assert_called_once()
        assert "server context" in mock_messages.warning.call_args[0][1].lower()
        assert response is not None

    @pytest.mark.django_db
    def test_update_module_interface_view_adopts_templates_after_a_real_bind_noop(self):
        from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay, ModuleType
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("update-module-interface-noop")
        bay = ModuleBay.objects.create(device=device, name="Update No-op Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Update No-op Module",
        )
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=module_type,
            status="active",
        )
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name="Te1/1/2",
            type="10gbase-x-sfpp",
        )
        primary = Interface.objects.create(
            device=device,
            module=module,
            name="Te1/1/1",
            type="10gbase-x-sfpp",
        )
        set_librenms_device_id(primary, 587, "default")
        primary.save()
        standalone = Interface.objects.create(
            device=device,
            name="Te1/1/2",
            type="10gbase-x-sfpp",
        )
        user = make_user_with_perms(
            "update-module-interface-noop",
            [("view", Device), ("view", Module), ("change", Interface)],
        )
        request = make_request(
            "post",
            {"module_id": str(module.pk), "server_key": "default", "ent_index": "77"},
            user=user,
        )
        view = UpdateModuleInterfaceView()
        view.setup(request, pk=device.pk)
        view._librenms_api = MagicMock(server_key="default")
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(
            cache_key,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 77,
                        "_librenms_port_id": 587,
                        "_librenms_ifname": primary.name,
                    }
                ]
            },
            timeout=300,
        )
        try:
            view.post(request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        primary.refresh_from_db()
        standalone.refresh_from_db()
        assert get_librenms_device_id(primary, "default") == 587
        assert standalone.module_id == module.pk

    def test_update_module_interface_view_skips_adoption_on_bind_conflict(self):
        """A hard bind conflict must NOT trigger template adoption — we don't mutate past an unresolved problem (e.g. an interface bound to another module)."""
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        view = object.__new__(UpdateModuleInterfaceView)
        view.required_object_permissions = {}
        view._librenms_api = MagicMock(server_key="production")
        device = _make_device()

        module = MagicMock()
        module.pk = 967
        module.module_type.model = "QSFP-DD-400G-ZR+"
        module.module_bay.name = "2/x1/1/c2"

        request = _make_request(
            "POST",
            data={"module_id": "967", "server_key": "production", "ent_index": "77"},
        )

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, module],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch.object(view, "get_cache_key", return_value="inv-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.get_librenms_device_id", return_value=999),
            patch(
                "netbox_librenms_plugin.views.sync.modules._bind_interface_librenms_id",
                return_value={"status": "conflict", "reason": "port_id 587 already assigned elsewhere"},
            ),
            patch(
                "netbox_librenms_plugin.views.sync.modules._adopt_existing_template_interfaces",
            ) as mock_adopt,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="redirected"),
        ):
            mock_cache.get.return_value = {
                "inventory": [{"entPhysicalIndex": 77, "_librenms_port_id": 587, "_librenms_ifname": "2/x1/1/c2"}],
                "librenms_id": 999,
            }
            view.request = request
            view.post(request, pk=24)

        mock_adopt.assert_not_called()
        mock_messages.warning.assert_called_once()

    @pytest.mark.django_db
    def test_replace_module_view_binds_interface_after_replace(self):
        from dcim.models import Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import (
            make_device,
            make_interface,
            make_module_bay,
            make_module_type,
        )
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view, message_texts
        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("replace-bind-device")
        old_type = make_module_type("OLD-SFP-BIND")
        new_type = make_module_type("NEW-SFP-BIND")
        target_bay = make_module_bay(device, "SFP 1")
        installed_module = Module.objects.create(
            device=device,
            module_bay=target_bay,
            module_type=old_type,
            serial="SN-OLD-BIND",
        )
        interface = make_interface(device, "Te1/1/1")
        request = make_request(
            "post",
            {
                "module_id": str(installed_module.pk),
                "ent_index": "77",
                "server_key": "default",
            },
        )
        view = make_view(ReplaceModuleView, request, librenms_api=MagicMock(server_key="default"))
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(
            cache_key,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 77,
                        "entPhysicalModelName": new_type.model,
                        "entPhysicalSerialNum": "SN-NEW-BIND",
                        "_librenms_port_id": 42,
                        "_librenms_ifname": interface.name,
                    }
                ],
                "librenms_id": 1,
            },
        )
        try:
            response = _post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert not Module.objects.filter(pk=installed_module.pk).exists()
        replacement = Module.objects.get(device=device, module_bay=target_bay)
        assert replacement.module_type == new_type
        assert replacement.serial == "SN-NEW-BIND"
        interface.refresh_from_db()
        assert interface.module_id == replacement.pk
        assert get_librenms_device_id(interface, "default") == 42
        assert any("Replaced OLD-SFP-BIND with NEW-SFP-BIND" in text for text in message_texts(request, "success"))
        assert any("Bound Te1/1/1 to LibreNMS port_id 42" in text for text in message_texts(request, "info"))


@pytest.mark.django_db
class TestVCMemberInterfaceNormalization:
    """Covers VC member-aware interface normalization against real rows."""

    @staticmethod
    def _module(suffix):
        from dcim.models import Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

        _vc, (_page, device) = make_virtual_chassis_members(f"vc-normalize-{suffix}")
        bay = ModuleBay.objects.create(device=device, name=f"VC Normalize Bay {suffix}")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model=f"VC Normalize Type {suffix}",
        )
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=module_type,
            status="active",
        )
        return device, module

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
        from dcim.models import Interface, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device = make_device("normalize-non-vc")
        bay = ModuleBay.objects.create(device=device, name="Normalize Non-VC Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Normalize Non-VC Type",
        )
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, status="active")

        result = _normalize_module_interface_names_for_vc_member(
            device,
            module,
            Interface.objects.all(),
            Interface.objects.all(),
        )

        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 0}

    def test_normalize_renames_module_interface_for_vc_member(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device, module = self._module("rename")
        interface = Interface.objects.create(
            device=device,
            module=module,
            name="TenGigabitEthernet1/1/1",
            type="10gbase-x-sfpp",
        )
        expected = f"TenGigabitEthernet{device.vc_position}/1/1"

        result = _normalize_module_interface_names_for_vc_member(
            device,
            module,
            Interface.objects.all(),
            Interface.objects.all(),
        )

        assert result["renamed"] == 1
        interface.refresh_from_db()
        assert interface.name == expected

    def test_normalize_adopts_existing_standalone_conflict(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device, module = self._module("adopt")
        created = Interface.objects.create(
            device=device,
            module=module,
            name="TenGigabitEthernet1/1/1",
            type="10gbase-x-sfpp",
        )
        existing = Interface.objects.create(
            device=device,
            name=f"TenGigabitEthernet{device.vc_position}/1/1",
            type="10gbase-x-sfpp",
        )

        result = _normalize_module_interface_names_for_vc_member(
            device,
            module,
            Interface.objects.all(),
            Interface.objects.all(),
        )

        assert result["adopted"] == 1
        assert result["removed"] == 1
        existing.refresh_from_db()
        assert existing.module_id == module.pk
        assert not Interface.objects.filter(pk=created.pk).exists()

    def test_normalize_skips_non_member_prefixed_interface(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        device, module = self._module("non-member")
        interface = Interface.objects.create(
            device=device,
            module=module,
            name="TenGigabitEthernet9/1/1",
            type="10gbase-x-sfpp",
        )

        result = _normalize_module_interface_names_for_vc_member(
            device,
            module,
            Interface.objects.all(),
            Interface.objects.all(),
        )

        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 0}
        interface.refresh_from_db()
        assert interface.name == "TenGigabitEthernet9/1/1"


@pytest.mark.django_db
class TestResolveTargetDevice:
    """Target device selection must remain constrained to visible VC members."""

    def test_non_vc_device_ignores_selected_member(self):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device

        page_device = make_device("target-non-vc")

        result = _resolve_target_device(page_device, "123", Device.objects.all())

        assert result == page_device

    def test_vc_member_selection_accepts_valid_member(self):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device

        _vc, (page_device, member) = make_virtual_chassis_members("target-valid-vc")

        result = _resolve_target_device(page_device, str(member.pk), Device.objects.all())

        assert result == member

    def test_vc_member_selection_falls_back_for_a_nonmember(self):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis_members
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device

        _vc, (page_device, _member) = make_virtual_chassis_members("target-wrong-vc")
        nonmember = make_device("target-nonmember")

        result = _resolve_target_device(page_device, str(nonmember.pk), Device.objects.all())

        assert result == page_device

    def test_invalid_selected_device_id_falls_back(self):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device

        page_device = make_device("target-invalid-id")

        result = _resolve_target_device(page_device, "not-an-int", Device.objects.all())

        assert result == page_device

    def test_validation_marks_non_vc_selection_invalid(self):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device_with_validation

        page_device = make_device("target-validation-non-vc")
        other = make_device("target-validation-other")

        resolved, invalid = _resolve_target_device_with_validation(
            page_device,
            str(other.pk),
            Device.objects.all(),
        )

        assert resolved == page_device
        assert invalid is True

    def test_validation_accepts_page_device_id(self):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.modules import _resolve_target_device_with_validation

        page_device = make_device("target-page-id")

        resolved, invalid = _resolve_target_device_with_validation(
            page_device,
            str(page_device.pk),
            Device.objects.all(),
        )

        assert resolved == page_device
        assert invalid is False


@pytest.mark.django_db
class TestModuleMutationScopes:
    """Module sync must scope every derived Device, bay, type, and Interface mutation."""

    @staticmethod
    def _module(device, suffix):
        from dcim.models import Module, ModuleBay, ModuleType

        bay = ModuleBay.objects.create(device=device, name=f"Scope Bay {suffix}")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model=f"Scope Module {suffix}",
        )
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=module_type,
            status="active",
        )
        return module

    def test_selected_vc_member_outside_device_view_grant_is_not_mutated(self):
        from dcim.models import Device, Module

        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        _vc, (page, hidden) = make_virtual_chassis_members("module-device-scope")
        module = self._module(hidden, "device")
        user = make_user_with_perms("module-device-scope", [])
        user = grant(user, "view", Device, constraints={"pk": page.pk})
        user = grant(user, "change", Module, constraints={"pk": module.pk})
        request = make_request(
            "post",
            {
                "selected_device_id": str(hidden.pk),
                "module_id": str(module.pk),
                "serial": "REPLACEMENT-SERIAL",
            },
            user=user,
        )
        view = UpdateModuleSerialView()

        _post(view, request, pk=page.pk)

        module.refresh_from_db()
        assert module.serial == ""

    def test_branch_install_does_not_use_hidden_bay_or_module_type(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("module-catalog-scope")
        hidden_bay = ModuleBay.objects.create(device=device, name="Hidden Scope Bay")
        allowed_bay = ModuleBay.objects.create(device=device, name="Allowed Scope Bay")
        hidden_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Hidden Scope Module Type",
        )
        allowed_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Allowed Scope Module Type",
        )
        user = make_user_with_perms(
            "module-catalog-scope",
            [("view", Device), ("add", Module), ("add", Interface), ("change", Interface), ("delete", Interface)],
        )
        user = grant(user, "view", ModuleBay, constraints={"pk": allowed_bay.pk})
        user = grant(user, "view", ModuleType, constraints={"pk": allowed_type.pk})
        request = make_request(
            "post",
            {"parent_index": "100", "server_key": "default"},
            user=user,
        )
        view = InstallBranchView()
        view.setup(request, pk=device.pk)
        view._librenms_api = SimpleNamespace(server_key="default")
        inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": hidden_type.model,
                "entPhysicalContainedIn": 0,
                "entPhysicalName": hidden_bay.name,
            }
        ]
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(cache_key, {"inventory": inventory}, timeout=300)
        try:
            view.post(request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert not Module.objects.filter(module_bay=hidden_bay).exists()

    def test_interface_outside_change_grant_is_not_bound_to_module(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("module-interface-scope")
        module = self._module(device, "interface")
        hidden = make_interface(device, "Te1/1/1")
        allowed = make_interface(device, "Te1/1/2")
        set_librenms_device_id(hidden, 42, "default")
        hidden.save()
        user = make_user_with_perms("module-interface-scope", [("view", Device), ("view", Module)])
        user = grant(user, "change", Interface, constraints={"pk": allowed.pk})
        request = make_request(
            "post",
            {"module_id": str(module.pk), "server_key": "default", "ent_index": "77"},
            user=user,
        )
        view = UpdateModuleInterfaceView()
        view.setup(request, pk=device.pk)
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(
            cache_key,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 77,
                        "_librenms_port_id": 42,
                        "_librenms_ifname": hidden.name,
                    }
                ]
            },
            timeout=300,
        )
        try:
            view.post(request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        hidden.refresh_from_db()
        assert hidden.module_id is None

    def test_install_does_not_adopt_an_interface_outside_change_grant(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("module-adoption-scope")
        bay = ModuleBay.objects.create(device=device, name="Adoption Scope Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Adoption Scope Type",
        )
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name="Te1/1/1",
            type="10gbase-x-sfpp",
        )
        hidden = make_interface(device, "Te1/1/1", iface_type="10gbase-x-sfpp")
        allowed = make_interface(device, "Te1/1/2", iface_type="10gbase-x-sfpp")
        user = make_user_with_perms(
            "module-adoption-scope",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("delete", Interface),
            ],
        )
        user = grant(user, "change", Interface, constraints={"pk": allowed.pk})
        request = make_request(
            "post",
            {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "server_key": "default",
            },
            user=user,
        )
        view = InstallModuleView()
        view._librenms_api = SimpleNamespace(server_key="default")

        _post(view, request, pk=device.pk)

        hidden.refresh_from_db()
        assert hidden.module_id is None
        assert not Module.objects.filter(device=device, module_bay=bay).exists()

    def test_vc_install_does_not_adopt_a_raw_name_outside_change_grant(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_interface, make_virtual_chassis_members
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        _vc, (_page, device) = make_virtual_chassis_members("module-vc-adoption-scope")
        bay = ModuleBay.objects.create(device=device, name="VC Adoption Scope Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="VC Adoption Scope Type",
        )
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name="TenGigabitEthernet1/1/1",
            type="10gbase-x-sfpp",
        )
        hidden = make_interface(device, "TenGigabitEthernet1/1/1", iface_type="10gbase-x-sfpp")
        allowed = make_interface(device, "TenGigabitEthernet2/1/2", iface_type="10gbase-x-sfpp")
        user = make_user_with_perms(
            "module-vc-adoption-scope",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("delete", Interface),
            ],
        )
        user = grant(user, "change", Interface, constraints={"pk": allowed.pk})
        request = make_request(
            "post",
            {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "server_key": "default",
            },
            user=user,
        )
        view = InstallModuleView()
        view._librenms_api = SimpleNamespace(server_key="default")

        _post(view, request, pk=device.pk)

        hidden.refresh_from_db()
        assert hidden.module_id is None
        assert not Module.objects.filter(device=device, module_bay=bay).exists()

    def test_install_checks_the_interface_adopted_before_prediction_hooks(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay, ModuleType
        from django.dispatch import receiver

        from netbox_librenms_plugin.signals import predict_module_interface_names
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("module-predicted-adoption-scope")
        bay = ModuleBay.objects.create(device=device, name="Predicted Adoption Scope Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Predicted Adoption Scope Type",
        )
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name="Te1/1/1",
            type="10gbase-x-sfpp",
        )
        hidden = make_interface(device, "Te1/1/1", iface_type="10gbase-x-sfpp")
        allowed = make_interface(device, "Ethernet1/1/1", iface_type="10gbase-x-sfpp")
        user = make_user_with_perms(
            "module-predicted-adoption-scope",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("delete", Interface),
            ],
        )
        user = grant(user, "change", Interface, constraints={"pk": allowed.pk})
        request = make_request(
            "post",
            {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "server_key": "default",
            },
            user=user,
        )
        view = InstallModuleView()
        view._librenms_api = SimpleNamespace(server_key="default")

        @receiver(predict_module_interface_names)
        def predict_name(sender, device, module, names, **kwargs):
            return ["Ethernet1/1/1"]

        try:
            _post(view, request, pk=device.pk)
        finally:
            predict_module_interface_names.disconnect(predict_name)

        hidden.refresh_from_db()
        assert hidden.module_id is None
        assert not Module.objects.filter(device=device, module_bay=bay).exists()

    def test_install_does_not_require_change_scope_for_a_new_template_interface(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("module-created-interface-scope")
        bay = ModuleBay.objects.create(device=device, name="Created Interface Scope Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Created Interface Scope Type",
        )
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name="Te1/1/1",
            type="10gbase-x-sfpp",
        )
        allowed = make_interface(device, "Te1/1/2", iface_type="10gbase-x-sfpp")
        user = make_user_with_perms(
            "module-created-interface-scope",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("delete", Interface),
            ],
        )
        user = grant(user, "change", Interface, constraints={"pk": allowed.pk})
        request = make_request(
            "post",
            {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "server_key": "default",
            },
            user=user,
        )
        view = InstallModuleView()
        view._librenms_api = SimpleNamespace(server_key="default")

        _post(view, request, pk=device.pk)

        module = Module.objects.get(device=device, module_bay=bay)
        assert Interface.objects.filter(device=device, module=module, name="Te1/1/1").exists()

    def test_install_authorizes_dependent_component_templates_without_instantiating_them(self):
        from types import SimpleNamespace

        from dcim.models import (
            Module,
            ModuleBay,
            ModuleType,
            PowerOutlet,
            PowerOutletTemplate,
            PowerPort,
            PowerPortTemplate,
        )

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("module-dependent-component-template")
        bay = ModuleBay.objects.create(device=device, name="Dependent Component Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Dependent Component Type",
        )
        power_port_template = PowerPortTemplate.objects.create(module_type=module_type, name="Power In")
        PowerOutletTemplate.objects.create(
            module_type=module_type,
            name="Power Out",
            power_port=power_port_template,
        )
        request = make_request(
            "post",
            {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "server_key": "default",
            },
        )
        view = InstallModuleView()
        view._librenms_api = SimpleNamespace(server_key="default")

        _post(view, request, pk=device.pk)

        module = Module.objects.get(device=device, module_bay=bay)
        power_port = PowerPort.objects.get(device=device, module=module, name="Power In")
        assert PowerOutlet.objects.filter(
            device=device,
            module=module,
            name="Power Out",
            power_port=power_port,
        ).exists()

    def test_install_does_not_adopt_a_module_bay_without_change_permission(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, Module, ModuleBay, ModuleBayTemplate, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("module-bay-adoption-scope")
        install_bay = ModuleBay.objects.create(device=device, name="Install Bay")
        hidden = ModuleBay.objects.create(device=device, name="Nested Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Module Bay Adoption Scope Type",
        )
        ModuleBayTemplate.objects.create(module_type=module_type, name=hidden.name)
        user = make_user_with_perms(
            "module-bay-adoption-scope",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ],
        )
        request = make_request(
            "post",
            {
                "module_bay_id": str(install_bay.pk),
                "module_type_id": str(module_type.pk),
                "server_key": "default",
            },
            user=user,
        )
        view = InstallModuleView()
        view._librenms_api = SimpleNamespace(server_key="default")

        _post(view, request, pk=device.pk)

        hidden.refresh_from_db()
        assert hidden.module_id is None
        assert not Module.objects.filter(device=device, module_bay=install_bay).exists()

    def test_install_checks_interface_change_scope_before_adoption(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("module-pre-adoption-scope")
        bay = ModuleBay.objects.create(device=device, name="Pre-adoption Scope Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Pre-adoption Scope Type",
        )
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name="Te1/1/1",
            type="10gbase-x-sfpp",
        )
        hidden = make_interface(device, "Te1/1/1", iface_type="10gbase-x-sfpp")
        user = make_user_with_perms(
            "module-pre-adoption-scope",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("delete", Interface),
            ],
        )
        user = grant(
            user,
            "change",
            Interface,
            constraints={"module__module_type_id": module_type.pk},
        )
        request = make_request(
            "post",
            {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "server_key": "default",
            },
            user=user,
        )
        view = InstallModuleView()
        view._librenms_api = SimpleNamespace(server_key="default")

        _post(view, request, pk=device.pk)

        hidden.refresh_from_db()
        assert hidden.module_id is None
        assert not Module.objects.filter(device=device, module_bay=bay).exists()

    @pytest.mark.parametrize("excluded_action", ["change_conflict", "delete_generated"])
    def test_vc_normalization_does_not_cross_interface_grants(self, excluded_action):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_interface, make_virtual_chassis_members
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import _normalize_module_interface_names_for_vc_member

        _vc, (_page, device) = make_virtual_chassis_members(f"module-normalize-{excluded_action}")
        module = self._module(device, excluded_action)
        generated = make_interface(device, "TenGigabitEthernet1/1/1", iface_type="10gbase-x-sfpp")
        generated.module = module
        generated.save(update_fields=["module"])
        conflict = make_interface(device, "TenGigabitEthernet2/1/1", iface_type="10gbase-x-sfpp")
        unrelated = make_interface(device, "TenGigabitEthernet2/1/2", iface_type="10gbase-x-sfpp")
        user = make_user_with_perms(f"module-normalize-{excluded_action}", [])
        user = grant(user, "change", Interface, constraints={"pk": generated.pk})
        if excluded_action == "delete_generated":
            user = grant(user, "change", Interface, constraints={"pk": conflict.pk})
            user = grant(user, "delete", Interface, constraints={"pk": unrelated.pk})
        else:
            user = grant(user, "delete", Interface, constraints={"pk": generated.pk})

        result = _normalize_module_interface_names_for_vc_member(
            device,
            module,
            Interface.objects.restrict(user, "change"),
            Interface.objects.restrict(user, "delete"),
        )

        generated.refresh_from_db()
        conflict.refresh_from_db()
        assert result == {"renamed": 0, "adopted": 0, "removed": 0, "skipped": 1}
        assert generated.name == "TenGigabitEthernet1/1/1"
        assert conflict.module_id is None


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

    def test_numeric_models_are_classified_without_crashing(self):
        """Numeric child and ancestor models still participate in top-level classification."""
        parent = {
            "entPhysicalIndex": 1,
            "entPhysicalClass": "module",
            "entPhysicalModelName": 123456,
            "entPhysicalContainedIn": 0,
        }
        child = {
            "entPhysicalIndex": 2,
            "entPhysicalClass": "module",
            "entPhysicalModelName": 654321,
            "entPhysicalContainedIn": 1,
        }

        top = self._run_top_items([parent, child])

        assert top == [parent]

    def test_unknown_model_remains_visible_for_no_type_diagnosis(self):
        """Unknown is a real ancestor model, so its child stays nested below it."""
        parent = {
            "entPhysicalIndex": 1,
            "entPhysicalClass": "module",
            "entPhysicalModelName": "Unknown",
            "entPhysicalContainedIn": 0,
        }
        child = {
            "entPhysicalIndex": 2,
            "entPhysicalClass": "module",
            "entPhysicalModelName": "CHILD-MODULE",
            "entPhysicalContainedIn": 1,
        }

        assert self._run_top_items([parent, child]) == [parent]


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
                mock_ignore.objects.restrict.return_value = mock_ignore.objects
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
                                                    view, "_group_children_under_parents", side_effect=lambda x: x
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


@pytest.mark.django_db
class TestInstallViewsPreserveInventoryCache:
    """Install views preserve valid inventory and reject stale inventory."""

    @staticmethod
    def _objects(suffix):
        from dcim.models import ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device(f"cache-install-{suffix}")
        bay = ModuleBay.objects.create(device=device, name=f"Slot {suffix}")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model=f"Cache Module {suffix}",
        )
        inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": module_type.model,
                "entPhysicalContainedIn": 0,
                "entPhysicalName": bay.name,
            }
        ]
        return device, bay, module_type, inventory

    @staticmethod
    def _user(suffix):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        return make_user_with_perms(
            f"cache-install-{suffix}",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ],
        )

    def test_install_module_preserves_inventory_cache(self):
        from types import SimpleNamespace

        from dcim.models import Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device, bay, module_type, inventory = self._objects("single")
        request = make_request(
            "post",
            {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "serial": "TEST-SERIAL",
                "server_key": "default",
            },
            user=self._user("single"),
        )
        view = InstallModuleView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        payload = {"inventory": inventory}
        cache.set(cache_key, payload, timeout=300)
        try:
            response = _post(view, request, pk=device.pk)

            assert response.status_code == 302
            assert Module.objects.filter(device=device, module_bay=bay, module_type=module_type).exists()
            assert cache.get(cache_key) == payload
        finally:
            cache.delete(cache_key)

    @pytest.mark.parametrize(
        ("view_name", "request_data"),
        [
            ("branch", {"parent_index": "100", "server_key": "default"}),
            ("selected", {"select": ["100"], "server_key": "default"}),
        ],
    )
    def test_bulk_install_preserves_inventory_cache(self, view_name, request_data):
        from types import SimpleNamespace

        from dcim.models import Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, InstallSelectedView

        device, bay, module_type, inventory = self._objects(view_name)
        request = make_request("post", request_data, user=self._user(view_name))
        view_class = InstallBranchView if view_name == "branch" else InstallSelectedView
        view = view_class()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        payload = {"inventory": inventory}
        cache.set(cache_key, payload, timeout=300)
        try:
            response = _post(view, request, pk=device.pk)

            assert response.status_code == 302
            assert Module.objects.filter(device=device, module_bay=bay, module_type=module_type).exists()
            assert cache.get(cache_key) == payload
        finally:
            cache.delete(cache_key)

    @pytest.mark.parametrize(
        ("view_name", "request_data"),
        [
            ("branch-stale", {"parent_index": "100", "server_key": "default"}),
            ("selected-stale", {"select": ["100"], "server_key": "default"}),
        ],
    )
    def test_bulk_install_rejects_stale_inventory_and_preserves_cache(self, view_name, request_data):
        from types import SimpleNamespace

        from dcim.models import Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, InstallSelectedView

        device, _bay, _module_type, inventory = self._objects(view_name)
        set_librenms_device_id(device, 999, "default")
        device.save()
        request = make_request("post", request_data, user=self._user(view_name))
        view_class = InstallBranchView if view_name == "branch-stale" else InstallSelectedView
        view = view_class()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        payload = {"inventory": inventory, "librenms_id": 555}
        cache.set(cache_key, payload, timeout=300)
        try:
            response = _post(view, request, pk=device.pk)

            assert response.status_code == 302
            assert not Module.objects.filter(device=device).exists()
            assert cache.get(cache_key) == payload
            assert any("No cached inventory data" in text for text in message_texts(request, "error"))
        finally:
            cache.delete(cache_key)


class TestShouldAttemptModuleInterfaceBind:
    """Interface binding runs only when the install result identifies a usable module."""

    @pytest.mark.parametrize(
        "result",
        [
            {"status": "installed", "module_pk": None},
            {"status": "skipped", "module_pk": 12, "reason": "no matching bay"},
            {"status": "failed", "module_pk": 12, "reason": "validation failed"},
        ],
    )
    def test_rejects_results_without_bindable_module_context(self, result):
        from netbox_librenms_plugin.views.sync.modules import _should_attempt_bind_for_result

        assert _should_attempt_bind_for_result(result) is False

    @pytest.mark.parametrize(
        "result",
        [
            {"status": "installed", "module_pk": 12},
            {"status": "skipped", "module_pk": 12, "reason": "bay already occupied"},
        ],
    )
    def test_accepts_installed_or_known_occupied_module(self, result):
        from netbox_librenms_plugin.views.sync.modules import _should_attempt_bind_for_result

        assert _should_attempt_bind_for_result(result) is True


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
        view._librenms_api = MagicMock(server_key="production")
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.request = request
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.request = request
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.request = request
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch.object(view, "get_cache_key", return_value="ck"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            mock_cache.get.return_value = {"inventory": cached, "librenms_id": "test"}
            view.request = request
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
        v._librenms_api = MagicMock(server_key="production")
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
        mock_qs.filter.return_value.first.return_value = module_bay  # locked re-fetch returns same occupied bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, module_bay, module_type],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
            patch.object(ModuleBay, "objects") as mock_objects,
        ):
            mock_tx.atomic = noop_atomic
            # Both reads go through restrict(user, ...), so hand back the same manager.
            mock_objects.restrict.return_value = mock_objects
            mock_objects.select_for_update.return_value = mock_qs
            view.request = request
            view.post(request, pk=24)

        mock_msg.warning.assert_called_once()
        assert "already has a module" in mock_msg.warning.call_args[0][1]
        mock_redirect.assert_called_once()

    def test_bay_deleted_before_lock_reports_error(self):
        """A bay removed after the first lookup must not cause an uncaught exception."""
        from contextlib import contextmanager

        from dcim.models import ModuleBay

        view = self._view()
        device = _make_device()
        module_bay = MagicMock()
        module_type = MagicMock()
        request = _make_request(
            "POST",
            data={"module_bay_id": "10", "module_type_id": "5", "serial": "SN1"},
        )

        @contextmanager
        def noop_atomic():
            yield

        mock_qs = MagicMock()
        mock_qs.filter.return_value.first.return_value = None

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, module_bay, module_type],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
            patch.object(ModuleBay, "objects") as mock_objects,
        ):
            mock_tx.atomic = noop_atomic
            mock_objects.restrict.return_value = mock_objects
            mock_objects.select_for_update.return_value = mock_qs
            view.request = request
            view.post(request, pk=24)

        mock_msg.error.assert_called_once_with(request, "Module bay no longer exists.")
        mock_qs.filter.assert_called_once_with(pk=10, device=device)
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
        mock_qs.filter.return_value.first.return_value = module_bay  # locked re-fetch returns same bay

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
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
            # The locked re-fetch goes through restrict(user, ...), so hand back the same manager.
            mock_objects.restrict.return_value = mock_objects
            mock_objects.select_for_update.return_value = mock_qs
            view.request = request
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
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
            patch.object(Module, "objects") as mock_objects,
        ):
            mock_tx.atomic = noop_atomic
            # Both reads go through restrict(user, ...), so hand back the same manager.
            mock_objects.restrict.return_value = mock_objects
            mock_objects.select_for_update.return_value = mock_qs
            view.request = request
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
        req.POST = {}
        req.GET = {}
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
        req.POST = {}
        req.GET = {}
        response = _modules_redirect_response(req, "/sync/")
        assert response.status_code == 204
        assert response["HX-Redirect"] == "/sync/?tab=modules#librenms-module-table"

    def test_explicit_server_key_is_appended(self):
        """A server-scoped action must keep the active server_key in the follow-up URL so the user returns to the same cache namespace this request mutated/read."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        req = MagicMock()
        req.headers = {}
        with patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect:
            _modules_redirect_response(req, "/sync/", server_key="prod server")
        # quote_plus encodes the value; the fragment stays last.
        mock_redirect.assert_called_once_with("/sync/?tab=modules&server_key=prod+server#librenms-module-table")

    def test_server_key_read_from_post_when_not_passed(self):
        """Bare call sites (in views that don't compute a resolved key) still propagate the server context — the helper reads server_key from the request itself."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.sync.modules import _modules_redirect_response

        req = MagicMock()
        req.headers = {}
        req.POST = {"server_key": "production"}
        req.GET = {}
        with patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect:
            _modules_redirect_response(req, "/sync/")
        mock_redirect.assert_called_once_with("/sync/?tab=modules&server_key=production#librenms-module-table")


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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch(
                "netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="REDIR"
            ) as mock_redir,
        ):
            view.request = req
            result = view.post(req, pk=1)
        assert result == "REDIR"
        assert mock_messages.error.called
        assert "Invalid target_kind" in mock_messages.error.call_args[0][1]
        mock_redir.assert_called_once()

    def test_missing_target_pk_returns_redirect(self):
        view = self._make_view()
        req = self._make_request({"target_kind": "device_type", "target_pk": "", "name": "Slot 1"})
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="REDIR"),
        ):
            view.request = req
            view.post(req, pk=1)
        assert "target_pk" in mock_messages.error.call_args[0][1]

    def test_missing_name_returns_redirect(self):
        view = self._make_view()
        req = self._make_request({"target_kind": "module_type", "target_pk": "5", "name": "  "})
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules._modules_redirect_response", return_value="REDIR"),
        ):
            view.request = req
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
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=MagicMock(),
        ):
            response = view.get(req, pk=1)
        assert response.status_code == 400

    def test_invalid_target_pk_returns_400(self):
        view = self._make_view()
        req = MagicMock()
        req.GET = {"target_kind": "device_type", "target_pk": "abc"}
        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=MagicMock(),
        ):
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(),
            ),
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, target],
            ),
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
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mapping_instance = MagicMock()
            mock_mapping_cls.return_value = mapping_instance
            view.request = req
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, MagicMock()],
            ),
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
            view.request = req
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, MagicMock()],
            ),
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
            view.request = req
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
            mock_device_cls.objects.restrict.return_value = mock_device_cls.objects
            mock_device_cls.objects.filter.return_value = [dev_a, dev_b]
            mock_bay_cls.objects.restrict.return_value = mock_bay_cls.objects
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
            mock_device_cls.objects.restrict.return_value = mock_device_cls.objects
            mock_device_cls.objects.filter.return_value = [MagicMock()]
            # Bay already exists on the device
            mock_bay_cls.objects.restrict.return_value = mock_bay_cls.objects
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
            mock_module_cls.objects.restrict.return_value = mock_module_cls.objects
            mock_module_cls.objects.filter.return_value.select_related.return_value = [module_a]
            mock_bay_cls.objects.restrict.return_value = mock_bay_cls.objects
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value="R") as mock_render,
            patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mapping_cls,
        ):
            # No exact mapping, but a covering regex row exists.
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, target],
            ),
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
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = []
            mock_mapping_cls.return_value = MagicMock()
            view.request = req
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, target],
            ),
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
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = []
            mock_mapping_cls.return_value = MagicMock()
            view.request = req
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, target],
            ),
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
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = []
            mock_mapping_cls.return_value = MagicMock()
            view.request = req
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
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, target],
            ),
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
            mock_mapping_cls.objects.restrict.return_value = mock_mapping_cls.objects
            mock_mapping_cls.objects.filter.return_value.filter.return_value.exists.return_value = False
            mock_mapping_cls.objects.filter.return_value.filter.return_value.only.return_value = [existing]
            view.request = req
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

    @staticmethod
    def _vc_member_with_module(template_name, *, members=4, vc_position=3, manufacturer="Nokia", model="7250-IXR"):
        """Build a real VC, return its *vc_position* member with a real Module installed whose single InterfaceTemplate instantiates to *template_name* verbatim."""
        from dcim.models import (
            InterfaceTemplate,
            Manufacturer,
            Module,
            ModuleBay,
            ModuleType,
            VirtualChassis,
        )

        from netbox_librenms_plugin.tests.conftest import make_device

        vc = VirtualChassis.objects.create(name="vc-norm")
        members_list = []
        for i in range(1, members + 1):
            dev = make_device(f"vc-norm-m{i}")
            dev.virtual_chassis = vc
            dev.vc_position = i
            dev.save()
            members_list.append(dev)
        device = members_list[vc_position - 1]

        mfr, _ = Manufacturer.objects.get_or_create(name=manufacturer, slug=manufacturer.lower())
        mtype = ModuleType.objects.create(manufacturer=mfr, model=model)
        InterfaceTemplate.objects.create(module_type=mtype, name=template_name, type="other")
        bay = ModuleBay.objects.create(device=device, name="Bay c9", position="c9")
        module = Module.objects.create(device=device, module_bay=bay, module_type=mtype, status="active")
        return device, module

    @pytest.mark.django_db
    def test_get_returns_400_when_module_id_missing(self):
        from dcim.models import Device, Module

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import VCNormalizationReportView

        device = make_device("vc-report-missing-id")
        user = make_user_with_perms("vc-report-missing-id", [("view", Device), ("view", Module)])
        request = make_request("get", user=user)

        response = _get(VCNormalizationReportView(), request, pk=device.pk)

        assert response.status_code == 400
        assert b"module_id" in response.content

    @pytest.mark.django_db
    def test_get_returns_400_when_no_noop_detected(self):
        """A module whose instantiated template name matches the VC member-position regex means rewriting works → the real detector returns None → 400 'nothing to report'."""
        from netbox_librenms_plugin.views.sync.modules import VCNormalizationReportView

        view = object.__new__(VCNormalizationReportView)
        view.required_object_permissions = {}
        # Cisco-style name matches the regex, so detect_vc_normalization_noop() returns None.
        device, module = self._vc_member_with_module("TenGigabitEthernet1/1/1")
        request = _make_request("GET", data={"module_id": str(module.pk)})

        with patch.object(view, "require_object_permissions", return_value=None):
            response = _get(view, request, pk=device.pk)

        assert response.status_code == 400
        assert b"nothing to report" in response.content.lower()

    @pytest.mark.django_db
    def test_get_renders_template_when_noop_detected(self):
        """A Nokia-shaped name that the regex can't rewrite → the real detector returns a diagnostic, and the real build_vc_normalization_report renders it through the view."""
        from netbox_librenms_plugin.views.sync.modules import VCNormalizationReportView

        view = object.__new__(VCNormalizationReportView)
        view.required_object_permissions = {}
        # "2/x1/1/c9" doesn't match the VC member-position regex → diagnostic produced.
        device, module = self._vc_member_with_module("2/x1/1/c9", manufacturer="Nokia")
        request = _make_request("GET", data={"module_id": str(module.pk)})

        with (
            patch.object(view, "require_object_permissions", return_value=None),
            # Keep render mocked (the MagicMock request can't drive real template rendering);
            # the assertion is on the real report markdown built from the real diagnostic.
            patch(
                "netbox_librenms_plugin.views.sync.modules.render",
                return_value="rendered",
            ) as mock_render,
        ):
            response = _get(view, request, pk=device.pk)

        assert response == "rendered"
        ctx = mock_render.call_args[0][2]
        md = ctx["report_markdown"]
        assert "**VC interface normalization — no match**" in md
        assert "nokia" in md  # real manufacturer slug
        assert "2/x1/1/c9" in md  # the real instantiated, non-matching template name

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
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
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
            response = _get(view, request, pk=24)

        mock_warn.assert_called_once_with(request)
        assert response.status_code == 400

    @pytest.mark.django_db
    def test_get_returns_400_when_module_id_non_numeric(self):
        """Non-numeric module_id is treated the same as missing — returns 400."""
        from dcim.models import Device, Module

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import VCNormalizationReportView

        device = make_device("vc-report-invalid-id")
        user = make_user_with_perms("vc-report-invalid-id", [("view", Device), ("view", Module)])
        request = make_request("get", {"module_id": "not-a-number"}, user=user)

        response = _get(VCNormalizationReportView(), request, pk=device.pk)

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


class TestModuleInterfaceUpdateMessage:
    """_module_interface_update_message reports bind and adoption distinctly."""

    @staticmethod
    def _msg(bind_result):
        from netbox_librenms_plugin.views.sync.modules import _module_interface_update_message

        return _module_interface_update_message(bind_result, "QSFP-100G in Bay 1")

    def test_bind_only_names_the_interface(self):
        msg = self._msg({"status": "bound", "interface": "Et1/1", "port_id": 42})
        assert msg == "Updated interface Et1/1 for QSFP-100G in Bay 1."

    def test_adopt_only_reports_count(self):
        msg = self._msg({"status": "bound", "adopted_count": 3})
        assert msg == ("Updated interfaces for QSFP-100G in Bay 1: adopted 3 existing standalone interface(s).")

    def test_bind_and_adopt_reports_both(self):
        # The merged result keeps the interface name (not port_id) in the message;
        # both the bind and the adoption must remain visible to the user.
        msg = self._msg({"status": "bound", "interface": "Et1/1", "port_id": 42, "adopted_count": 2})
        assert "Updated interface Et1/1 for QSFP-100G in Bay 1" in msg
        assert "adopted 2 existing standalone interface(s)" in msg

    def test_zero_adopted_count_treated_as_bind_only(self):
        msg = self._msg({"status": "bound", "interface": "Et1/1", "adopted_count": 0})
        assert msg == "Updated interface Et1/1 for QSFP-100G in Bay 1."

    def test_no_bind_no_adopt_is_a_clean_no_op_message(self):
        # Pure-adoption path where a concurrent/duplicate request already adopted the interfaces:
        # neither an interface name nor a positive adopted_count is present. The message must NOT
        # fall through to "Updated interface None for ...".
        msg = self._msg({"status": "bound", "adopted_count": 0})
        assert "None" not in msg
        assert msg == "No interface changes were needed for QSFP-100G in Bay 1."

    def test_unchanged_interface_bind_is_a_clean_no_op_message(self):
        msg = self._msg(
            {
                "status": "bound",
                "interface": "Et1/1",
                "port_id": 42,
                "changed": False,
                "adopted_count": 0,
            }
        )
        assert msg == "No interface changes were needed for QSFP-100G in Bay 1."


@pytest.mark.django_db
class TestReplaceModuleRedirectServerKey:
    """ReplaceModuleView keeps its active server on a real validation redirect."""

    def test_missing_module_id_preserves_fallback_server_key(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, Module, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("replace-redirect-invalid")
        user = make_user_with_perms(
            "replace-redirect-invalid",
            [
                ("view", Device),
                ("view", ModuleType),
                ("add", Module),
                ("change", Module),
                ("delete", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ],
        )
        request = make_request(
            "post",
            {},
            user=user,
            path="/replace-module/",
            HTTP_HX_REQUEST="true",
        )
        view = ReplaceModuleView()
        view._librenms_api = SimpleNamespace(server_key="prod")

        response = _post(view, request, pk=device.pk)

        assert response.status_code == 204
        assert "server_key=prod" in response["HX-Redirect"]


@pytest.mark.django_db
class TestUpdateModuleInterfaceRedirectServerKey:
    """UpdateModuleInterfaceView keeps its resolved server on real redirects."""

    @staticmethod
    def _request(user, data):
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        return make_request(
            "post",
            data,
            user=user,
            path="/update-interface/",
            HTTP_HX_REQUEST="true",
        )

    def test_invalid_module_id_preserves_fallback_server_key(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, Module

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("update-interface-redirect-invalid")
        user = make_user_with_perms(
            "update-interface-redirect-invalid",
            [("view", Device), ("view", Module), ("change", Interface)],
        )
        request = self._request(user, {"ent_index": "77"})
        view = UpdateModuleInterfaceView()
        view._librenms_api = SimpleNamespace(server_key="prod")

        response = _post(view, request, pk=device.pk)

        assert response.status_code == 204
        assert "server_key=prod" in response["HX-Redirect"]

    def test_success_preserves_fallback_server_key(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

        device = make_device("update-interface-redirect-success")
        bay = ModuleBay.objects.create(device=device, name="Redirect Bay")
        module_type = ModuleType.objects.create(
            manufacturer=device.device_type.manufacturer,
            model="Redirect Module",
        )
        module = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=module_type,
            status="active",
        )
        user = make_user_with_perms(
            "update-interface-redirect-success",
            [("view", Device), ("view", Module), ("change", Interface)],
        )
        request = self._request(user, {"module_id": str(module.pk), "ent_index": "77"})
        view = UpdateModuleInterfaceView()
        view._librenms_api = SimpleNamespace(server_key="prod")

        response = _post(view, request, pk=device.pk)

        assert response.status_code == 204
        assert "server_key=prod" in response["HX-Redirect"]


class TestStandaloneAdoptionAcrossEveryComponentType:
    """Every component NetBox can adopt must be authorized through the change-scoped queryset.

    The adoption helper walks eight component specs, and each one resolves its template name
    through ``_module_template_adoption_name``. Only interfaces and module bays were covered, so a
    regression in any of the other six -- or in the version-dependent name resolution -- went
    unnoticed. Drive all eight against the real ORM.
    """

    BAY_POSITION = "A1"

    @staticmethod
    def _couples_a_rear_port(model):
        """True while this NetBox still gives *model* a mandatory rear-port foreign key."""
        # NetBox 4.5 replaced the front-port rear_port FK with PortTemplateMapping, so the field
        # decides the shape and the test stays correct on both sides of that change.
        return any(field.name == "rear_port" for field in model._meta.get_fields())

    @staticmethod
    def _type_kwargs(model_name):
        """Return the mandatory ``type`` for the component models that require one."""
        if "Interface" in model_name:
            return {"type": "1000base-t"}
        if "FrontPort" in model_name or "RearPort" in model_name:
            return {"type": "8p8c"}
        return {}

    @classmethod
    def _module_with_one_template(cls, spec, name):
        """Build a real Device + ModuleType carrying exactly one template for *spec*."""
        from dcim.constants import MODULE_TOKEN
        from dcim.models import Manufacturer, Module, ModuleBay, ModuleType, RearPortTemplate

        from netbox_librenms_plugin.tests.conftest import make_device

        template_attribute, component_attribute, component_model = spec
        device = make_device(f"adopt-{name}")
        manufacturer = Manufacturer.objects.get_or_create(name="AdoptMfr", slug="adopt-mfr")[0]
        module_type = ModuleType.objects.create(manufacturer=manufacturer, model=f"AdoptType-{name}")

        template_model = getattr(ModuleType, template_attribute).rel.related_model
        # The {module} token makes the module argument load-bearing in the name resolution.
        template_kwargs = {"module_type": module_type, "name": f"{MODULE_TOKEN}-adopt-{name}-0"}
        template_kwargs |= cls._type_kwargs(template_model.__name__)
        if template_model.__name__ == "FrontPortTemplate" and cls._couples_a_rear_port(template_model):
            template_kwargs["rear_port"] = RearPortTemplate.objects.create(
                module_type=module_type, name=f"rear-for-{name}", type="8p8c"
            )
            template_kwargs["rear_port_position"] = 1
        template = template_model.objects.create(**template_kwargs)

        bay = ModuleBay.objects.create(device=device, name=f"bay-{name}", position=cls.BAY_POSITION)
        module = Module(device=device, module_bay=bay, module_type=module_type)
        return device, module, template, component_attribute, component_model

    @classmethod
    def _standalone_kwargs(cls, component_model, device, expected_name):
        """Return the kwargs a standalone *component_model* needs on the running NetBox."""
        from dcim.models import RearPort

        kwargs = {"device": device, "name": expected_name} | cls._type_kwargs(component_model.__name__)
        if component_model.__name__ == "FrontPort" and cls._couples_a_rear_port(component_model):
            kwargs["rear_port"] = RearPort.objects.create(device=device, name=f"rear-for-{expected_name}", type="8p8c")
            kwargs["rear_port_position"] = 1
        return kwargs

    @pytest.mark.django_db
    @pytest.mark.parametrize("spec_index", range(8))
    def test_a_standalone_component_is_authorized_for_adoption(self, spec_index):
        """A standalone component matching the template name is locked and authorized."""
        from netbox_librenms_plugin.views.sync.modules import (
            _authorize_adoptable_module_components,
            _module_component_specs,
            _module_template_adoption_name,
        )

        spec = _module_component_specs()[spec_index]
        template_attribute, _component_attribute, component_model = spec
        device, module, template, component_attribute, component_model = self._module_with_one_template(
            spec, component_model.__name__.lower()
        )

        # This is the call CodeRabbit questioned for NetBox 4.4/4.5: exercise it for every type.
        expected_name = _module_template_adoption_name(template_attribute, template, module)
        assert expected_name, f"{template_attribute} resolved an empty adoption name"
        # These seven go through resolve_name(module), where the token resolves only once the
        # module argument arrives. NetBox 4.4 module bays instantiate from the raw name instead.
        if template_attribute != "modulebaytemplates":
            assert expected_name == f"{self.BAY_POSITION}-adopt-{component_model.__name__.lower()}-0", (
                f"{template_attribute} did not resolve the module placeholder from the installed bay"
            )

        standalone = component_model.objects.create(**self._standalone_kwargs(component_model, device, expected_name))

        allowed = {model: model.objects.all() for _, _, model in _module_component_specs()}
        expected = _authorize_adoptable_module_components(module, allowed)

        assert expected.get(component_model) == {standalone.pk}, (
            f"{component_model.__name__} standalone adoption was not authorized"
        )

    @pytest.mark.django_db
    @pytest.mark.parametrize("spec_index", range(8))
    def test_an_unauthorized_standalone_component_is_refused_by_name(self, spec_index):
        """A component outside the change scope aborts the write and names the component."""
        from netbox_librenms_plugin.views.sync.modules import (
            _ModuleComponentAdoptionUnavailable,
            _authorize_adoptable_module_components,
            _module_component_specs,
            _module_template_adoption_name,
        )

        spec = _module_component_specs()[spec_index]
        template_attribute, _component_attribute, component_model = spec
        device, module, template, component_attribute, component_model = self._module_with_one_template(
            spec, f"deny-{component_model.__name__.lower()}"
        )
        expected_name = _module_template_adoption_name(template_attribute, template, module)

        component_model.objects.create(**self._standalone_kwargs(component_model, device, expected_name))

        # Every model is in scope EXCEPT the one under test.
        allowed = {
            model: (model.objects.none() if model is component_model else model.objects.all())
            for _, _, model in _module_component_specs()
        }

        with pytest.raises(_ModuleComponentAdoptionUnavailable) as exc:
            _authorize_adoptable_module_components(module, allowed)
        assert exc.value.component_label == component_model._meta.verbose_name, (
            "the refusal must name the component the caller could not adopt"
        )
