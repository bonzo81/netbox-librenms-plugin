"""
Tests for module sync views and BaseModuleTableView bay matching logic.

Covers: InstallModuleView/InstallBranchView wiring, branch collection, cycle guards,
bay matching by name/mapping/position, serial comparison, status determination,
and depth tracking.  inventory-rebased branch only.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch


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


class TestGetModuleTypesIndexed:
    """get_module_types_indexed builds a dict keyed by model name, part number, and mappings."""

    def test_indexes_by_model_and_part_number(self):
        from netbox_librenms_plugin.utils import get_module_types_indexed

        mt1 = MagicMock()
        mt1.model = "WS-X4748"
        mt1.part_number = "ALT-PART-4748"

        mt2 = MagicMock()
        mt2.model = "WS-X4516"
        mt2.part_number = "WS-X4516"  # same as model → no extra key

        mock_mapping = MagicMock()
        mock_mapping.librenms_model = "libre-model-a"
        mock_mapping.netbox_module_type = mt1

        mock_mt_cls = MagicMock()
        mock_mt_cls.objects.all.return_value.select_related.return_value = [mt1, mt2]

        mock_map_cls = MagicMock()
        mock_map_cls.objects.select_related.return_value = [mock_mapping]

        with patch.dict(
            "sys.modules",
            {
                "dcim.models": type("m", (), {"ModuleType": mock_mt_cls})(),
            },
        ):
            with patch("netbox_librenms_plugin.models.ModuleTypeMapping", mock_map_cls):
                result = get_module_types_indexed()

        assert result["WS-X4748"] is mt1
        assert result["ALT-PART-4748"] is mt1
        assert result["WS-X4516"] is mt2
        assert result["libre-model-a"] is mt1


class TestInstallModuleViewWiring:
    """InstallModuleView must have correct mixins and attributes."""

    def test_has_librenms_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        assert LibreNMSPermissionMixin in InstallModuleView.__mro__

    def test_has_netbox_object_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.modules import InstallModuleView
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        assert NetBoxObjectPermissionMixin in InstallModuleView.__mro__

    def test_install_module_view_not_in_base(self):
        """InstallModuleView must NOT be defined in views/base anymore."""
        import importlib

        mod = importlib.import_module("netbox_librenms_plugin.views.base.modules_view")
        assert not hasattr(mod, "InstallModuleView"), (
            "InstallModuleView must have been moved out of views/base/modules_view.py"
        )


class TestInstallBranchViewWiring:
    """InstallBranchView must have CacheMixin for cache key generation."""

    def test_has_cache_mixin(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView
        from netbox_librenms_plugin.views.mixins import CacheMixin

        assert CacheMixin in InstallBranchView.__mro__

    def test_has_netbox_object_permission_mixin(self):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

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
        assert row["row_class"] == "table-success"

    def test_serial_mismatch_gives_danger_status(self):
        view = _make_base_view()
        item = self._make_item("WS-X4748", "SN-NEW-999")
        mt = self._make_matched_type()
        installed = _module(serial="SN-OLD-111")
        bay = _bay("Slot 1", installed_module=installed)

        with _patch_build_row_deps(view, match_bay_return=bay):
            row = view._build_row(item, {10: item}, {"Slot 1": bay}, {"WS-X4748": mt}, depth=0)

        assert row["status"] == "Serial Mismatch"
        assert row["row_class"] == "table-danger"

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
        assert row["row_class"] == "table-success"

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
        assert row["row_class"] == "table-success"


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
            "entPhysicalContainedIn": 0,
            "entPhysicalParentRelPos": 0,
        }
        container1 = {
            "entPhysicalIndex": 2,
            "entPhysicalModelName": "",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 1,
        }
        container2 = {
            "entPhysicalIndex": 3,
            "entPhysicalModelName": "",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 2,
        }
        sfp1 = {
            "entPhysicalIndex": 4,
            "entPhysicalModelName": "SFP-10G-LR",
            "entPhysicalContainedIn": 2,
            "entPhysicalParentRelPos": 1,
        }
        sfp2 = {
            "entPhysicalIndex": 5,
            "entPhysicalModelName": "SFP-10G-SR",
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
            "entPhysicalContainedIn": 0,
            "entPhysicalParentRelPos": 0,
        }
        container = {
            "entPhysicalIndex": 2,
            "entPhysicalModelName": "",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 1,
        }
        item = {
            "entPhysicalIndex": 3,
            "entPhysicalModelName": "X",
            "entPhysicalContainedIn": 2,
            "entPhysicalParentRelPos": 1,
        }
        index_map = {1: parent, 2: container, 3: item}
        bays = {"InterfaceA": _bay("InterfaceA")}  # no "SFP 1"/"Slot 1"/etc.

        result = BaseModuleTableView._match_bay_by_position(item, index_map, bays)
        assert result is None


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

        with patch("netbox_librenms_plugin.models.ModuleBayMapping") as mock_mbm:
            mock_mbm.objects.filter.return_value.first.return_value = None
            mock_mbm.objects.filter.return_value = MagicMock()
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

        bay = _bay("Slot 1")
        bay.installed_module = None

        index_map = {10: item}
        module_types = {"WS-X4748": mt}

        ModuleBay = MagicMock()
        ModuleBay.objects.filter.return_value.select_related.return_value = [bay]
        ModuleType = MagicMock()
        Module = MagicMock()

        return device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt

    def test_returns_installed_on_success(self):
        from contextlib import contextmanager

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
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
        assert "WS-X4748" in result["name"]

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
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        view = _make_install_branch_view()
        device, item, index_map, module_types, ModuleBay, ModuleType, Module, bay, mt = self._make_args()
        bay.installed_module = _module()  # occupied!

        with patch("netbox_librenms_plugin.utils.resolve_module_type", side_effect=lambda m, t, **kw: t.get(m)):
            with patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])):
                with patch.object(InstallBranchView, "_find_parent_module_id", return_value=None):
                    with patch.object(InstallBranchView, "_match_bay", return_value=bay):
                        result = view._install_single(
                            device, item, index_map, module_types, ModuleBay, ModuleType, Module
                        )

        assert result["status"] == "skipped"
        assert "already occupied" in result["reason"]

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
        # The cell value for 'selection' should be 42 (ent_physical_index), not ''
        cell_val = rows[0].get_cell("selection")
        assert str(cell_val) != "", "Checkbox cell must not be empty for a record with ent_physical_index"


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

        def fake_build_row(item, index_map, bays, module_types, depth=0, manufacturer=None):
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
                mock_ignore.objects.filter.return_value = []
                with patch.object(view, "_get_module_bays", return_value=({}, {})):
                    with patch.object(view, "_get_module_types", return_value={}):
                        with patch.object(view, "_build_row", side_effect=fake_build_row):
                            with patch.object(view, "get_table", side_effect=fake_get_table):
                                with patch.object(view, "_sort_with_hierarchy", side_effect=lambda x: x):
                                    with patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache:
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
            patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
        ):
            mock_cache.get.return_value = cached_inventory
            mock_tx.atomic = lambda: MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))
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
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
        ):
            mock_cache.get.return_value = cached_inventory
            mock_tx.atomic = lambda: MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))
            view.post(request, pk=24)

        mock_messages.success.assert_called_once()
        mock_cache.delete.assert_not_called()


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
        return m

    def _make_exact_mapping(self, librenms_name, netbox_bay_name):
        m = MagicMock()
        m.is_regex = False
        m.librenms_name = librenms_name
        m.netbox_bay_name = netbox_bay_name
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
        """Bay matches parent name but has no installed module — walk continues."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        parent = {
            "entPhysicalIndex": 10,
            "entPhysicalName": "Slot 1",
            "entPhysicalDescr": "",
            "entPhysicalContainedIn": 0,
        }
        child = {"entPhysicalIndex": 20, "entPhysicalContainedIn": 10}
        index_map = {10: parent, 20: child}
        empty_bay = self._make_bay("Slot 1", installed_module_id=None)

        result = InstallBranchView._find_parent_module_id(child, index_map, [empty_bay], [], [])
        assert result is None


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
        return m

    def _make_regex_mapping(self, pattern, netbox_bay_name, librenms_class=""):
        import re

        m = MagicMock()
        m.is_regex = True
        m.librenms_name = pattern
        m.netbox_bay_name = netbox_bay_name
        m.librenms_class = librenms_class
        m._compiled_pattern = re.compile(pattern)
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

    def test_regex_mapping_with_backreference(self):
        """Regex mapping with capture group + backreference in netbox_bay_name."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        child = {
            "entPhysicalIndex": 2,
            "entPhysicalName": "Optics0/0/0/5",
            "entPhysicalDescr": "",
            "entPhysicalClass": "module",
            "entPhysicalContainedIn": 0,
        }
        index_map = {2: child}

        bay = MagicMock()
        bay.name = "Optics0/0/0/5"
        bay.module = None
        module_bays = {"Optics0/0/0/5": bay}
        regex = [self._make_regex_mapping(r"Optics(\d+/\d+/\d+/\d+)", r"Optics\1")]

        with patch(
            "netbox_librenms_plugin.views.base.modules_view.BaseModuleTableView._fpc_slot_matches", return_value=True
        ):
            result = InstallBranchView._match_bay(child, index_map, module_bays, [], regex)

        assert result is bay

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
        return m

    def test_failed_expand_does_not_match_stale_bay(self):
        """If expand() raises, the stale resolved_bay from a prior mapping must not be used."""
        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        # Mapping A: matches, expand succeeds, but bay name not in module_bays.
        # Defined for documentation but replaced by mapping_a_no_bay below.
        # Mapping B: matches, but expand raises IndexError (bad backreference)
        mapping_b = self._make_mapping(r"Port-\d+", r"Bay-\2")  # \2 doesn't exist

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
            result = BaseModuleTableView._lookup_regex_bay_mapping("Fan-3", "fan", module_bays, [m_class, m_generic])

        assert result is fan_bay


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
            mock_cache.get.return_value = cached
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

        with (
            patch.object(view, "require_all_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_object_or_404",
                side_effect=[device, module],
            ),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            mock_tx.atomic = noop_atomic
            view.post(request, pk=24)

        assert module.serial == "NEW-SN"
        module.full_clean.assert_called_once()
        module.save.assert_called_once()
        mock_msg.success.assert_called_once()
        assert "NEW-SN" in mock_msg.success.call_args[0][1]
        mock_redirect.assert_called_once()
