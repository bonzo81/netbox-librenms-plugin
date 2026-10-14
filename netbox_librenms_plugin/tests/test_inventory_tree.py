"""Dangling inventory parents: the Juniper MX optic repair, its warning, and the writers that read it."""

from copy import deepcopy
import re

import pytest

from netbox_librenms_plugin.data_shapes.recordings_store import iter_recordings
from netbox_librenms_plugin.tests.test_inventory_class_admission import _inventory, _mapped_device, _recording, _refresh
from netbox_librenms_plugin.tests.test_modules_view import _two_server_keys

pytestmark = pytest.mark.django_db

RECORDING = "junos-mx-dangling"
# Optic index: the PIC that its "@ F/P/N" locator names. LibreNMS reports 71, 72, 95 and 96.
REPAIRED = {889: 67, 892: 67, 984: 68, 985: 68, 986: 68, 987: 68, 3191: 79, 3287: 80}
REPORTED = {889: 71, 892: 71, 984: 72, 985: 72, 986: 72, 987: 72, 3191: 95, 3287: 96}
# FPC 0 optics point to their PICs as reported.
FPC0_OPTICS = {119: 63, 215: 64, 216: 64, 311: 65, 312: 65, 407: 66, 408: 66}
# [entPhysicalIndex, depth, rendered parent index] in table order after the repair. A PIC is built in
# (model BUILTIN) and the table hides it, so an optic renders below the FPC that holds its PIC.
EXPECTED_ROWS = [
    [2, 0, None],
    [3, 0, None],
    [4, 0, None],
    [5, 0, None],
    [21, 0, None],
    [119, 1, 21],
    [215, 1, 21],
    [216, 1, 21],
    [311, 1, 21],
    [312, 1, 21],
    [407, 1, 21],
    [408, 1, 21],
    [22, 0, None],
    [889, 1, 22],
    [892, 1, 22],
    [984, 1, 22],
    [985, 1, 22],
    [986, 1, 22],
    [987, 1, 22],
    [25, 0, None],
    [3191, 1, 25],
    [3287, 1, 25],
    [111, 0, None],
    [112, 0, None],
    [113, 0, None],
    [114, 0, None],
    [115, 0, None],
]


@pytest.fixture
def server_key(settings, librenms_server):
    return _two_server_keys(settings, librenms_server, "dangling-parents")[0]


def _rows_with_parents(rows):
    """Return [index, depth, parent index] per rendered row; the parent is the nearest earlier row one level up."""
    stack = []
    result = []
    for row in rows:
        depth = row["depth"]
        del stack[depth:]
        result.append([row["ent_physical_index"], depth, stack[-1] if stack else None])
        stack.append(row["ent_physical_index"])
    return result


def _row_html(content, index):
    match = re.search(rf'<tr[^>]*data-ent-index="{index}"[^>]*>.*?</tr>', content, re.DOTALL)
    assert match, f"row {index} is not rendered"
    return match.group(0)


def _cached_inventory(device, server_key):
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import cached_module_inventory
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    key = DeviceModuleTableView().get_cache_key(device, "inventory", server_key=server_key)
    return {row["entPhysicalIndex"]: row for row in cached_module_inventory(cache.get(key))}


def _render(recording, librenms_server, server_key, tag):
    librenms_server.load_recording(recording)
    device = _mapped_device(f"dangling-{tag}", recording, server_key)
    response, module_sync = _refresh(device, server_key)
    return device, response.content.decode(), list(module_sync["table"].data)


def _dangling_badge(index):
    return f"LibreNMS reports parent index {index}, which is not in the inventory."


class TestRepairOnTheRecording:
    """AC1: the FPC 1 and FPC 4 optics take the PIC that their locator names and render inside its FPC."""

    def test_the_optics_render_inside_the_fpc_of_their_pic(self, librenms_server, server_key):
        recording = _recording(RECORDING)
        entities = {row["entPhysicalIndex"]: row for row in _inventory(recording)}
        assert {idx: entities[idx]["entPhysicalContainedIn"] for idx in REPORTED} == REPORTED
        assert not set(REPORTED.values()) & set(entities)

        _device, _content, rows = _render(recording, librenms_server, server_key, "ac1")

        assert _rows_with_parents(rows) == EXPECTED_ROWS

    def test_the_snapshot_stores_the_repaired_parent_and_the_reported_one(self, librenms_server, server_key):
        """AC3: the writers read the cached snapshot, so they see the parent the table shows."""
        device, content, _rows = _render(_recording(RECORDING), librenms_server, server_key, "snapshot")

        cached = _cached_inventory(device, server_key)

        assert {idx: cached[idx]["entPhysicalContainedIn"] for idx in REPAIRED} == REPAIRED
        assert {idx: cached[idx]["_reported_parent"] for idx in REPAIRED} == REPORTED
        assert {idx: cached[idx]["entPhysicalContainedIn"] for idx in FPC0_OPTICS} == FPC0_OPTICS
        assert not any("_reported_parent" in cached[idx] for idx in FPC0_OPTICS)
        assert not any("_dangling_parent" in row for row in cached.values())
        assert "LibreNMS reports parent index 71; the PIC locator" in _row_html(content, 889)
        assert "LibreNMS reports parent index" not in _row_html(content, 119)

    def test_a_fixed_agent_renders_the_tree_the_repair_builds_with_no_report(self, librenms_server, server_key):
        """A Junos agent that reports each PIC parent itself gives the same rows, and no warning or note shows."""
        fixed = _recording(RECORDING)
        for row in _inventory(fixed):
            row["entPhysicalContainedIn"] = REPAIRED.get(row["entPhysicalIndex"], row["entPhysicalContainedIn"])

        _device, _content, repaired_rows = _render(_recording(RECORDING), librenms_server, server_key, "as-reported")
        device, content, fixed_rows = _render(fixed, librenms_server, server_key, "fixed-agent")

        def rendered(rows):
            fields = [(row["name"], row["item_class"], row["status"], row["module_bay"]) for row in rows]
            return [[*place, *field] for place, field in zip(_rows_with_parents(rows), fields, strict=True)]

        assert rendered(fixed_rows) == rendered(repaired_rows)
        assert _rows_with_parents(fixed_rows) == EXPECTED_ROWS
        cached = _cached_inventory(device, server_key)
        assert not any("_dangling_parent" in row or "_reported_parent" in row for row in cached.values())
        assert "LibreNMS reports parent index" not in content
        assert "Parent Missing" not in content


def _without_pic_67(recording):
    rows = _inventory(recording)
    rows[:] = [row for row in rows if row["entPhysicalIndex"] != 67]


def _duplicate_pic_67(recording):
    rows = _inventory(recording)
    pic = next(row for row in rows if row["entPhysicalIndex"] == 67)
    rows.append({**pic, "entPhysicalIndex": 69, "entPhysicalSerialNum": "SN-DUPLICATE"})


def _malformed_locators(recording):
    for row in _inventory(recording):
        if row["entPhysicalIndex"] in {889, 892}:
            row["entPhysicalDescr"] = row["entPhysicalDescr"].replace(" @ 1/0/", " @ 1/0-")


def _fpc_1_below_its_optic(recording):
    """The cycle shape: a repair of 889 to PIC 67 would close 889 -> 67 -> 22 -> 889."""
    for row in _inventory(recording):
        if row["entPhysicalIndex"] == 22:
            row["entPhysicalContainedIn"] = 889


class TestBranchInstall:
    """AC3: a branch install reads the repaired snapshot, so a PIC brings its optics."""

    def test_installing_pic_67_also_installs_its_optics(self, librenms_server, server_key):
        """PIC 67 is built in (model BUILTIN), so its optics go to the bays of the installed FPC 1 module."""
        from dcim.models import Module

        from netbox_librenms_plugin.models import ModuleBayMapping
        from netbox_librenms_plugin.tests.conftest import (
            install_module,
            make_module_bay,
            make_module_type,
            make_superuser,
        )
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, post
        from netbox_librenms_plugin.utils import module_inventory_binding_token, module_inventory_snapshot_digest
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        recording = _recording(RECORDING)
        entities = {row["entPhysicalIndex"]: row for row in _inventory(recording)}
        fpc, optics = entities[22], [entities[889], entities[892]]
        for optic in optics:
            make_module_type(optic["entPhysicalModelName"])
        ModuleBayMapping.objects.create(
            librenms_name=fpc["entPhysicalDescr"], librenms_class="container", netbox_bay_name="FPC1"
        )
        ModuleBayMapping.objects.create(
            librenms_name=r"^entity-[0-9a-f]{6} @ 1/0/(\d+)$",
            librenms_class="port",
            netbox_bay_name=r"Port \1",
            is_regex=True,
        )
        librenms_server.load_recording(recording)
        device = _mapped_device("dangling-branch", recording, server_key)
        make_module_bay(device, "FPC1", position="1")
        fpc_module = install_module(device, "FPC1", fpc["entPhysicalModelName"], child_bays=("Port 2", "Port 5"))
        _refresh(device, server_key)
        rows = list(_cached_inventory(device, server_key).values())
        request = make_request(
            "post",
            {
                "parent_index": "67",
                "server_key": server_key,
                "inventory_binding": module_inventory_binding_token(
                    device.pk,
                    server_key,
                    "install_branch",
                    {"parent_index": 67},
                    67,
                    module_inventory_snapshot_digest(rows),
                ),
            },
            user=make_superuser(),
            path="/modules/",
        )

        response = post(InstallBranchView(), request, pk=device.pk)

        assert response.status_code == 302
        installed = Module.objects.filter(device=device).exclude(pk=fpc_module.pk)
        assert {(module.module_bay.name, module.module_bay.module_id) for module in installed} == {
            ("Port 2", fpc_module.pk),
            ("Port 5", fpc_module.pk),
        }, message_texts(request)


class TestUnrepairedRowsWarn:
    """AC2: a row that the rule cannot repair keeps its reported parent and shows a warning badge."""

    @pytest.mark.parametrize(
        ("edit", "dangling"),
        [
            (_without_pic_67, {889, 892}),
            (_duplicate_pic_67, {889, 892}),
            (_malformed_locators, {889, 892}),
            (_fpc_1_below_its_optic, {889}),
        ],
        ids=["pic-removed", "duplicate-pic-locator", "malformed-locator", "cycle"],
    )
    def test_the_row_keeps_its_parent_and_shows_the_badge(self, librenms_server, server_key, edit, dangling):
        recording = _recording(RECORDING)
        edit(recording)

        device, content, _rows = _render(recording, librenms_server, server_key, edit.__name__)

        cached = _cached_inventory(device, server_key)
        for idx in dangling:
            assert cached[idx]["entPhysicalContainedIn"] == REPORTED[idx]
            assert cached[idx]["_dangling_parent"] == REPORTED[idx]
            assert "_reported_parent" not in cached[idx]
            row_html = _row_html(content, idx)
            assert _dangling_badge(REPORTED[idx]) in row_html
            assert 'class="badge bg-warning text-dark"' in row_html
        # The other FPCs still repair, and they show no warning.
        assert cached[984]["entPhysicalContainedIn"] == 68
        assert _dangling_badge(72) not in _row_html(content, 984)

    def test_the_badge_text_is_escaped(self):
        from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable

        html = LibreNMSModuleTable([])._parent_report_html({"dangling_parent": "<b>9</b>"})

        assert "&lt;b&gt;9&lt;/b&gt;" in html
        assert "<b>" not in html


class TestValidTreesDoNotChange:
    """AC4: the repair changes no row of a recording whose parents all resolve."""

    @pytest.mark.parametrize(
        "name",
        sorted(
            recording["name"]
            for recording in iter_recordings()
            if recording["name"] != RECORDING
            and f"GET /api/v0/inventory/{recording['device_id']}/all" in recording["responses"]
        ),
    )
    def test_a_bundled_recording_is_left_as_it_is(self, name):
        from netbox_librenms_plugin.inventory_tree import repair_dangling_parents

        rows = _inventory(_recording(name))
        before = deepcopy(rows)

        repair_dangling_parents(rows)

        assert rows == before

    def test_a_port_with_a_valid_parent_is_not_moved(self):
        """The locator names PIC 20, but the reported parent row exists, so it stays."""
        from netbox_librenms_plugin.inventory_tree import repair_dangling_parents

        rows = [
            {"entPhysicalIndex": 10, "entPhysicalClass": "container", "entPhysicalDescr": "FPC: X @ 1/*/*"},
            {"entPhysicalIndex": 20, "entPhysicalClass": "module", "entPhysicalDescr": "PIC: Y @ 1/0/*"},
            {"entPhysicalIndex": 30, "entPhysicalClass": "port", "entPhysicalDescr": "SFP @ 1/0/2"},
        ]
        rows[1]["entPhysicalContainedIn"] = 10
        rows[2]["entPhysicalContainedIn"] = 10
        before = deepcopy(rows)

        repair_dangling_parents(rows)

        assert rows == before


class TestRepairRule:
    """The rule reads the same source list only, and only the class and locators the record names."""

    @staticmethod
    def _rows(port_class="port", pic_parent=10, fpc_descr="FPC: X @ 1/*/*", port_descr="SFP @ 1/0/2"):
        return [
            {"entPhysicalIndex": 10, "entPhysicalClass": "container", "entPhysicalDescr": fpc_descr},
            {
                "entPhysicalIndex": 20,
                "entPhysicalClass": "module",
                "entPhysicalDescr": "PIC: Y @ 1/0/*",
                "entPhysicalContainedIn": pic_parent,
            },
            {
                "entPhysicalIndex": 30,
                "entPhysicalClass": port_class,
                "entPhysicalDescr": port_descr,
                "entPhysicalContainedIn": 99,
            },
        ]

    def test_a_port_moves_below_the_one_pic_its_locator_names(self):
        from netbox_librenms_plugin.inventory_tree import repair_dangling_parents

        rows = self._rows()

        repair_dangling_parents(rows)

        assert rows[2]["entPhysicalContainedIn"] == 20
        assert rows[2]["_reported_parent"] == 99

    @pytest.mark.parametrize(
        "rows_kwargs",
        [
            {"port_class": "module"},
            {"pic_parent": 0},
            {"fpc_descr": "FPC: X @ 2/*/*"},
            {"fpc_descr": "Backplane"},
            {"port_descr": "SFP @ 1/1/2"},
            {"port_descr": "SFP 1/0/2"},
        ],
        ids=["not-a-port", "pic-not-in-an-fpc", "fpc-of-another-slot", "parent-not-an-fpc", "no-such-pic", "no-at"],
    )
    def test_no_single_candidate_marks_the_row_dangling(self, rows_kwargs):
        from netbox_librenms_plugin.inventory_tree import repair_dangling_parents

        rows = self._rows(**rows_kwargs)

        repair_dangling_parents(rows)

        if rows_kwargs.get("port_class") == "module":
            assert "_dangling_parent" not in rows[2]
        else:
            assert rows[2]["_dangling_parent"] == 99
        assert rows[2]["entPhysicalContainedIn"] == 99
        assert "_reported_parent" not in rows[2]

    @pytest.mark.parametrize("missing", ["absent", "null", "zero"])
    def test_a_pic_with_no_index_is_no_candidate(self, missing):
        """Index 0 is the parent value of a root row, so a row at index 0 is no parent either."""
        from netbox_librenms_plugin.inventory_tree import repair_dangling_parents

        rows = self._rows()
        if missing == "absent":
            del rows[1]["entPhysicalIndex"]
        else:
            rows[1]["entPhysicalIndex"] = None if missing == "null" else 0

        repair_dangling_parents(rows)

        assert rows[2]["entPhysicalContainedIn"] == 99
        assert rows[2]["_dangling_parent"] == 99
        assert "_reported_parent" not in rows[2]

    def test_a_repair_that_would_close_a_cycle_is_refused(self):
        """Container 10 sits below port 30, so a parent 20 for port 30 would close 30 -> 20 -> 10 -> 30."""
        from netbox_librenms_plugin.inventory_tree import repair_dangling_parents

        rows = self._rows()
        rows[0]["entPhysicalContainedIn"] = 30

        repair_dangling_parents(rows)

        assert rows[2]["entPhysicalContainedIn"] == 99
        assert rows[2]["_dangling_parent"] == 99


class TestOobInventory:
    """The OOB list is repaired on its own indices, and a row never takes a parent from the other list."""

    def test_an_oob_optic_takes_only_an_oob_pic(self, librenms_server, server_key):
        from netbox_librenms_plugin.constants import OOB_INVENTORY_SOURCE
        from netbox_librenms_plugin.server_mappings import attach_oob
        from netbox_librenms_plugin.tests.mapping_fixtures import apply_mapping_change

        recording = _recording(RECORDING)
        librenms_server.load_recording(recording)
        oob_rows = [
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "chassis",
                "entPhysicalDescr": "OOB",
                "entPhysicalContainedIn": 0,
            },
            {
                "entPhysicalIndex": 10,
                "entPhysicalClass": "container",
                "entPhysicalDescr": "FPC: X @ 1/*/*",
                "entPhysicalContainedIn": 1,
            },
            {
                "entPhysicalIndex": 20,
                "entPhysicalClass": "module",
                "entPhysicalDescr": "PIC: Y @ 1/0/*",
                "entPhysicalContainedIn": 10,
            },
            {
                "entPhysicalIndex": 30,
                "entPhysicalClass": "port",
                "entPhysicalDescr": "SFP @ 1/0/2",
                "entPhysicalContainedIn": 98,
            },
            # The host has PIC 68 at 1/1/*, but it is in the other list.
            {
                "entPhysicalIndex": 40,
                "entPhysicalClass": "port",
                "entPhysicalDescr": "SFP @ 1/1/2",
                "entPhysicalContainedIn": 99,
            },
        ]
        librenms_server.register("/api/v0/inventory/999/all", {"status": "ok", "inventory": oob_rows}, method="GET")
        device = _mapped_device("dangling-oob", recording, server_key)
        apply_mapping_change(device, attach_oob(device, server_key, 999, oob_type="idrac9"))
        device.save(update_fields=["custom_field_data"])

        _refresh(device, server_key)

        cached = _cached_inventory(device, server_key)
        offset = min(idx for idx, row in cached.items() if row["_source"] == OOB_INVENTORY_SOURCE) - 1
        assert cached[30 + offset]["entPhysicalContainedIn"] == 20 + offset
        assert cached[30 + offset]["_reported_parent"] == 98
        assert cached[40 + offset]["entPhysicalContainedIn"] == 99 + offset
        assert cached[40 + offset]["_dangling_parent"] == 99
        assert {idx: cached[idx]["entPhysicalContainedIn"] for idx in REPAIRED} == REPAIRED
