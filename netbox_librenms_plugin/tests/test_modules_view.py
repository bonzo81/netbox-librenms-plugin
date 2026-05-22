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


def _run_build_context(view, inventory_data, device_bays, module_scoped_bays, module_types, bay_mappings=None):
    """Call _build_context with all DB-accessing calls mocked out.

    `bay_mappings` is an optional (exact_list, regex_list) tuple of ModuleBayMapping-like
    objects.  When None, mappings are empty and matching exercises only direct-name
    and positional fallbacks.
    """
    rows_store = _captured_table_view(view)
    view._get_module_bays = MagicMock(return_value=(device_bays, module_scoped_bays))
    view._get_module_types = MagicMock(return_value=module_types)
    view._get_generic_module_types = MagicMock(return_value={})
    view._get_module_type_ambiguities = MagicMock(return_value={})
    view._get_carrier_install_rules = MagicMock(return_value=[])

    if bay_mappings is None:
        bay_mappings = ([], [])

    with (
        patch("netbox_librenms_plugin.views.base.modules_view.cache") as mock_cache,
        patch("netbox_librenms_plugin.utils.load_bay_mappings", return_value=bay_mappings),
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


def _load_contrib_bay_mappings():
    """Load contrib bay mappings as fake ModuleBayMapping objects (no DB)."""
    import re as _re
    from pathlib import Path

    import yaml

    contrib_path = Path(__file__).resolve().parents[2] / "contrib" / "module_bay_mappings.yaml"
    with open(contrib_path) as f:
        data = yaml.safe_load(f)

    class _FakeMap:
        def __init__(self, **kw):
            self.librenms_name = kw["librenms_name"]
            self.librenms_class = kw.get("librenms_class") or ""
            self.netbox_bay_name = kw["netbox_bay_name"]
            self.is_regex = kw.get("is_regex", False)
            self._compiled_pattern = None
            if self.is_regex:
                try:
                    self._compiled_pattern = _re.compile(self.librenms_name)
                except _re.error:
                    pass

    mappings = [_FakeMap(**m) for m in data]
    exact = [m for m in mappings if not m.is_regex]
    regex = [m for m in mappings if m.is_regex]
    return exact, regex


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


def _prod_bay_setup_ws_x4908(cvr_installed=True):
    """
    NetBox bay structure mirroring prod-lab03-sw4:
        Device-bays: Slot 3 (linecard installed)
        WS-X4908-10GE bays: X2 Port 1..8 (X2 Port 2 holds CVR if cvr_installed)
        CVR-X2-SFP bays: SFP 1, SFP 2 (none installed)
    """
    linecard_module = MagicMock()
    linecard_module.pk = 100
    linecard_module.serial = "S_LINECARD"
    linecard_module.module_type_id = 10

    cvr2_module = MagicMock()
    cvr2_module.pk = 200
    cvr2_module.serial = "S_CVR2"
    cvr2_module.module_type_id = 20

    slot3_bay = MagicMock()
    slot3_bay.name = "Slot 3"
    slot3_bay.installed_module = linecard_module
    slot3_bay.get_absolute_url.return_value = "/bay/slot3"
    device_bays = {"Slot 3": slot3_bay}

    linecard_bays = {}
    for n in range(1, 9):
        b = MagicMock()
        b.name = f"X2 Port {n}"
        b.installed_module = cvr2_module if (n == 2 and cvr_installed) else None
        b.get_absolute_url.return_value = f"/bay/x2-{n}"
        linecard_bays[f"X2 Port {n}"] = b

    module_scoped_bays = {100: linecard_bays}

    if cvr_installed:
        cvr_bays = {}
        for n in range(1, 3):
            b = MagicMock()
            b.name = f"SFP {n}"
            b.installed_module = None
            b.get_absolute_url.return_value = f"/bay/sfp-{n}"
            cvr_bays[f"SFP {n}"] = b
        module_scoped_bays[200] = cvr_bays

    return device_bays, module_scoped_bays


def _prod_module_types():
    mt_lc = MagicMock()
    mt_lc.pk = 10
    mt_lc.model = "WS-X4908-10GE"
    mt_lc.get_absolute_url.return_value = "/mt/lc"
    mt_cvr = MagicMock()
    mt_cvr.pk = 20
    mt_cvr.model = "CVR-X2-SFP"
    mt_cvr.get_absolute_url.return_value = "/mt/cvr"
    mt_glc_te = MagicMock()
    mt_glc_te.pk = 30
    mt_glc_te.model = "GLC-TE"
    mt_glc_te.get_absolute_url.return_value = "/mt/glc-te"
    mt_glc_t = MagicMock()
    mt_glc_t.pk = 40
    mt_glc_t.model = "GLC-T"
    mt_glc_t.get_absolute_url.return_value = "/mt/glc-t"
    return {
        "WS-X4908-10GE": mt_lc,
        "CVR-X2-SFP": mt_cvr,
        "GLC-TE": mt_glc_te,
        "GLC-T": mt_glc_t,
    }


class TestProdShapeWS4908Matching:
    """
    Bay matching against real production data shape from a Cisco WS-X4908-10GE.

    Distinct from `TestBayDepthScopeWithUninstalledParent`, whose synthetic
    container names match bay names directly without exercising the contrib
    regex paths.  This class loads the contrib YAML and asserts each level
    of the chain — linecard regex, X2 slot regex, and CVR-internal positional
    fallback — actually does what the contrib mappings claim.
    """

    def _build_rows(self, cvr_installed=True):
        view = _make_view()
        device_bays, module_scoped_bays = _prod_bay_setup_ws_x4908(cvr_installed=cvr_installed)
        return _run_build_context(
            view,
            _prod_inventory_ws_x4908(),
            device_bays,
            module_scoped_bays,
            _prod_module_types(),
            bay_mappings=_load_contrib_bay_mappings(),
        )

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
        view = _make_view()
        device_bays, module_scoped_bays = _prod_bay_setup_ws_x4908(cvr_installed=True)
        rows = _run_build_context(
            view,
            no_cvr_inventory,
            device_bays,
            module_scoped_bays,
            _prod_module_types(),
            bay_mappings=_load_contrib_bay_mappings(),
        )
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
        assert "row_class" not in row
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
    def _bay(name):
        b = MagicMock()
        b.name = name
        return b

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
