"""
Tests for BaseModuleTableView sync logic (modules_view.py).

Focuses on the bay-scope tracking in _build_context and the serial
comparison logic in _build_row.
"""

from unittest.mock import MagicMock, patch


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


def _run_build_context(view, inventory_data, device_bays, module_scoped_bays, module_types):
    """Call _build_context with all DB-accessing calls mocked out."""
    rows_store = _captured_table_view(view)
    view._get_module_bays = MagicMock(return_value=(device_bays, module_scoped_bays))
    view._get_module_types = MagicMock(return_value=module_types)

    with (
        patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
        patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=([], [])),
        patch("netbox_librenms_plugin.utils.get_enabled_ignore_rules", return_value=[]),
        patch("netbox_librenms_plugin.utils.apply_normalization_rules", side_effect=lambda v, *a, **kw: v),
        patch("netbox_librenms_plugin.utils.preload_normalization_rules", return_value={}),
        patch("netbox_librenms_plugin.utils.has_nested_name_conflict", return_value=False),
        # _detect_serial_conflicts makes a real DB query; mock it out for unit tests
        patch.object(view.__class__, "_detect_serial_conflicts", return_value=None),
    ):
        mock_cache.ttl = MagicMock(return_value=None)

        # Inline import: patch ModuleBayMapping inside models module
        view._build_context(MagicMock(), MagicMock(), inventory_data)

    return rows_store.get("rows", [])


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


def _bay_setup():
    """Build mock device_bays and module_scoped_bays matching _linecard_inventory."""
    # --- module instances (NetBox Module objects) ---
    linecard_module = MagicMock()
    linecard_module.pk = 100
    linecard_module.serial = "S_LINECARD"
    linecard_module.module_type_id = 10  # matches mt_linecard.pk

    cvr2_module = MagicMock()
    cvr2_module.pk = 200
    cvr2_module.serial = "FDO_CVR2"
    cvr2_module.module_type_id = 20  # matches mt_cvr.pk

    glc_te_installed = MagicMock()
    glc_te_installed.serial = "MTC213403BB"
    glc_te_installed.get_absolute_url.return_value = "/modules/99/"
    glc_te_installed.module_type_id = 30  # matches mt_glc_te.pk

    # --- device-level bays ---
    slot3_bay = MagicMock()
    slot3_bay.name = "Slot 3"
    slot3_bay.installed_module = linecard_module
    device_bays = {"Slot 3": slot3_bay}

    # --- module-scoped bays created by the linecard ---
    x2p2_bay = MagicMock()
    x2p2_bay.name = "X2 Port 2"
    x2p2_bay.installed_module = cvr2_module  # INSTALLED

    x2p4_bay = MagicMock()
    x2p4_bay.name = "X2 Port 4"
    x2p4_bay.installed_module = None  # NOT installed

    # --- module-scoped bays created by the installed CVR at X2 Port 2 ---
    sfp1_bay = MagicMock()
    sfp1_bay.name = "SFP 1"
    sfp1_bay.installed_module = glc_te_installed

    sfp2_bay = MagicMock()
    sfp2_bay.name = "SFP 2"
    sfp2_bay.installed_module = None

    module_scoped_bays = {
        100: {"X2 Port 2": x2p2_bay, "X2 Port 4": x2p4_bay},
        200: {"SFP 1": sfp1_bay, "SFP 2": sfp2_bay},
    }

    return device_bays, module_scoped_bays


def _module_types():
    """Minimal module-type dict for the test scenario."""
    mt_linecard = MagicMock()
    mt_linecard.pk = 10
    mt_linecard.model = "WS-X4908"
    mt_cvr = MagicMock()
    mt_cvr.pk = 20
    mt_cvr.model = "CVR-X2-SFP"
    mt_glc_te = MagicMock()
    mt_glc_te.pk = 30
    mt_glc_te.model = "GLC-TE"
    mt_glc_t = MagicMock()
    mt_glc_t.pk = 40
    mt_glc_t.model = "GLC-T"
    return {
        "WS-X4908": mt_linecard,
        "CVR-X2-SFP": mt_cvr,
        "GLC-TE": mt_glc_te,
        "GLC-T": mt_glc_t,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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
        device_bays, module_scoped_bays = _bay_setup()
        module_types = _module_types()
        return _run_build_context(view, _linecard_inventory(), device_bays, module_scoped_bays, module_types)

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
        device_bays, module_scoped_bays = _bay_setup()
        module_types = _module_types()

        # Add a third installed CVR at X2 Port 6 with its own SFP 1 bay
        cvr6_module = MagicMock()
        cvr6_module.pk = 300
        cvr6_module.serial = "FDO_CVR6"
        cvr6_module.module_type_id = 20  # matches mt_cvr.pk

        sfp1_bay_6 = MagicMock()
        sfp1_bay_6.name = "SFP 1"
        sfp6_installed = MagicMock()
        sfp6_installed.serial = "SFP6_SERIAL"
        sfp6_installed.get_absolute_url.return_value = "/modules/199/"
        sfp6_installed.module_type_id = 30  # matches mt_glc_te.pk
        sfp1_bay_6.installed_module = sfp6_installed

        x2p6_bay = MagicMock()
        x2p6_bay.name = "X2 Port 6"
        x2p6_bay.installed_module = cvr6_module

        module_scoped_bays[100]["X2 Port 6"] = x2p6_bay
        module_scoped_bays[300] = {"SFP 1": sfp1_bay_6}

        rows = _run_build_context(view, inventory, device_bays, module_scoped_bays, module_types)

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
        assert row["row_class"] == "table-success"
        assert not row.get("can_update_serial")

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
        assert row["row_class"] == "table-danger"
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
        assert row["row_class"] == "table-warning"

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
        assert row["row_class"] == "table-success"
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
        assert row["row_class"] == "table-success"
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
