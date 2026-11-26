"""End-to-end tests for the module install actions and their inventory ignore rules."""

import re

import pytest

from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.tests.cache_test_helpers import seed_inventory
from netbox_librenms_plugin.tests.conftest import (
    install_module,
    make_device,
    make_device_with_module_bays,
    make_interface,
    make_module_bay,
    make_module_type,
    make_superuser,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.views.sync.modules import SELECTED_ROWS_REFUSED_MESSAGE
from netbox_librenms_plugin.tests.view_test_helpers import make_request, message_texts, module_tab_forms
from netbox_librenms_plugin.tests.view_test_helpers import post as view_post
from netbox_librenms_plugin.tests.view_test_helpers import render_module_tab, rendered_module_form

pytestmark = pytest.mark.django_db


def _item(index, model, name, *, parent=0, phys_class="module", **extra):
    row = {
        "entPhysicalIndex": index,
        "entPhysicalModelName": model,
        "entPhysicalName": name,
        "entPhysicalDescr": name,
        "entPhysicalClass": phys_class,
        "entPhysicalContainedIn": parent,
        "entPhysicalSerialNum": "",
    }
    row.update(extra)
    return row


def _drive(view_class, device, data, live_librenms):
    """POST into a bound view with a real request and the loopback LibreNMS client."""
    request = make_request("post", data, user=make_superuser(), path="/modules/")
    view = view_class()
    view._librenms_api = live_librenms.api
    return view, request, view_post(view, request, pk=device.pk)


def _table_rows(device):
    """Return the rows the Modules tab renders from the cached snapshot of *device*."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    request = make_request("get", {"server_key": "default"}, user=make_superuser(), path="/modules/")
    view = DeviceModuleTableView()
    view.setup(request, pk=device.pk)
    view.cache_only = True
    return list(view.get_context_data(request, device, server_key="default")["table"].data)


def _branch_post(device, parent_index):
    """Return the fields of the Install Branch form that the cached Modules tab renders for row *parent_index*, or None."""
    forms = [
        form["fields"]
        for form in module_tab_forms(render_module_tab(device)).forms
        if "install-branch" in form["action"] and form["fields"].get("parent_index") == str(parent_index)
    ]
    return forms[0] if forms else None


def _with_installable_child(rows, parent_index, tag):
    """Add an optic with a module type below row *parent_index*, so the table renders that row's branch button."""
    optic = make_module_type(f"{tag.upper()}-CHILD-OPTIC")
    return [*rows, _item(parent_index + 1, optic.model, f"{tag} optic", parent=parent_index)]


def _branch_names(rows_by_index, parent_index):
    """Return the names of the rows that the branch list of top-level row *parent_index* holds."""
    return {rows_by_index[index]["name"] for index, _target in rows_by_index[parent_index]["branch_rows"]}


def _post_branch(device, rows, parent_index, librenms_id, live_librenms):
    """POST the rendered Install Branch form of *parent_index* over a seeded snapshot of *rows*."""
    from netbox_librenms_plugin.views.sync.modules import InstallBranchView

    view = InstallBranchView()
    view._librenms_api = live_librenms.api
    seed_inventory(view, device, rows, librenms_id=librenms_id)
    data = _branch_post(device, parent_index)
    assert data is not None, f"the table renders no branch for row {parent_index}"
    request = make_request("post", data, user=make_superuser(), path="/modules/")
    response = view_post(view, request, pk=device.pk)
    assert response.status_code == 302
    return request


def _post_selected(device, rows, indices, librenms_id, live_librenms):
    """POST the rendered Install Selected form for *indices* over a seeded snapshot of *rows*."""
    from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

    view = InstallSelectedView()
    view._librenms_api = live_librenms.api
    seed_inventory(view, device, rows, librenms_id=librenms_id)
    _action, fields = rendered_module_form(render_module_tab(device), "install-selected")
    request = make_request(
        "post", {**fields, "select": [str(index) for index in indices]}, user=make_superuser(), path="/modules/"
    )
    view_post(view, request, pk=device.pk)
    return request


def _seed_pre_version_snapshot(view, device, rows, librenms_id):
    """Cache *rows* exactly as the writer stored them before the payload carried a schema version."""
    from django.core.cache import cache

    key = view.get_cache_key(device, "inventory", server_key="default")
    cache.set(key, {"inventory": rows, "librenms_id": librenms_id, "oob_librenms_id": None}, timeout=300)
    return key


# A labelled serial-less cage and a placeholder-model container both only hold the optic.
SKIPPED_HOLDER_SHAPES = pytest.mark.parametrize(
    ("holder_class", "holder_model"), [("port", "Linecard-Port"), ("container", "")], ids=["label", "placeholder"]
)


def _optic_below_a_skipped_holder(tag, holder_class, holder_model, librenms_id):
    """Seed chassis, holder and optic rows, a bay the optic matches, and a skip rule on the holder only."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    device = make_device_with_module_bays(f"{tag}-{holder_class}", ["Cage 1"])
    seed_mapping(device, "default", own=librenms_id)
    module_type = make_module_type(f"{tag.upper()}-{holder_class.upper()}-SFP")
    rows = [
        _item(1, "CHASSIS-X", "Chassis", phys_class="chassis", entPhysicalSerialNum="SN-CHASSIS"),
        _item(10, holder_model, "Cage 1", parent=1, phys_class=holder_class),
        _item(20, module_type.model, "Optic 1", parent=10, entPhysicalSerialNum="SN-OPTIC-1"),
    ]
    InventoryIgnoreRule.objects.create(
        name=f"{tag} skip the holder",
        match_type=InventoryIgnoreRule.MATCH_ENDS_WITH,
        pattern="Cage 1",
        action=InventoryIgnoreRule.ACTION_SKIP,
        require_serial_match_parent=False,
    )
    return device, rows


class TestInstallModuleView:
    """The single-row install reads its identity from the cached row, never from the post."""

    @pytest.mark.parametrize("broken_field", ["module_bay_id", "module_type_id"])
    def test_a_non_numeric_bay_or_type_is_rejected(self, live_librenms, broken_field):
        """Each id is parsed separately, so each one has to be rejected on its own."""
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device(f"install-bad-{broken_field}", librenms_cf={"default": 61})
        bay = make_module_bay(device, "Bad Input Bay")
        module_type = make_module_type(f"BAD-INPUT-CARD-{broken_field}")
        data = {
            "module_bay_id": str(bay.pk),
            "module_type_id": str(module_type.pk),
            "server_key": "default",
        }
        data[broken_field] = "not-a-pk"

        _view, request, response = _drive(InstallModuleView, device, data, live_librenms)

        assert response.status_code == 302
        assert "Missing or invalid module bay/module type ID." in message_texts(request, "error")
        assert not Module.objects.filter(device=device).exists()

    @pytest.mark.parametrize("placeholder", ["N/A", "BUILTIN", "null"])
    def test_a_placeholder_serial_installs_as_blank(self, live_librenms, placeholder):
        """The serial comes from the cached row, so a placeholder there must land as blank."""
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device_with_module_bays(f"install-placeholder-{len(placeholder)}", ["Slot 1"])
        seed_mapping(device, "default", own=68)
        module_type = make_module_type(f"INSTALL-PLACEHOLDER-CARD-{len(placeholder)}")
        rows = [_item(11, module_type.model, "Slot 1", entPhysicalSerialNum=placeholder)]
        view = InstallModuleView()
        view._librenms_api = live_librenms.api
        seed_inventory(view, device, rows, librenms_id=68)
        _action, fields = rendered_module_form(render_module_tab(device), "install-module", 11)
        request = make_request("post", fields, user=make_superuser(), path="/modules/")

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert Module.objects.get(device=device, module_type=module_type).serial == ""

    def test_posted_row_metadata_binds_nothing_without_an_ent_index(self, live_librenms):
        """With no index there is no cached row, so posted identity and serial must not be read."""
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallModuleView

        device = make_device("install-post-fallback", librenms_cf={"default": 62})
        bay = make_module_bay(device, "Fallback Bay")
        module_type = make_module_type("FALLBACK-CARD")
        interface = make_interface(device, "Te1/1/1")

        _view, request, response = _drive(
            InstallModuleView,
            device,
            {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
                "serial": "FORGED-SN",
                "server_key": "default",
                "librenms_port_id": "6201",
                "librenms_ifname": interface.name,
            },
            live_librenms,
        )

        interface.refresh_from_db()
        assert response.status_code == 302
        assert interface.module_id is None
        assert read_mapping(interface).own_id("default") is None
        assert message_texts(request, "info") == []
        # The carrier install itself still runs; only the posted metadata is discarded.
        assert Module.objects.get(device=device, module_bay=bay).serial == ""
        assert any("serial: N/A" in text for text in message_texts(request, "success"))


class TestInstallBranchView:
    """The branch install validates its parent index, needs a snapshot, and reports each row."""

    def test_a_non_numeric_parent_index_is_rejected(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("branch-bad-index", librenms_cf={"default": 63})

        _view, request, response = _drive(
            InstallBranchView,
            device,
            {"parent_index": "not-a-number", "server_key": "default"},
            live_librenms,
        )

        assert response.status_code == 302
        assert "Invalid parent inventory index." in message_texts(request, "error")

    def test_without_a_snapshot_the_user_is_told_to_refresh(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("branch-no-cache", librenms_cf={"default": 64})

        _view, request, response = _drive(
            InstallBranchView,
            device,
            {"parent_index": "10", "server_key": "default"},
            live_librenms,
        )

        assert response.status_code == 302
        assert "No cached inventory data. Please refresh modules first." in message_texts(request, "error")

    def test_a_branch_install_creates_the_module_and_binds_its_port(self, live_librenms):
        from dcim.models import Module

        device = make_device_with_module_bays("branch-install", ["Slot 1"])
        seed_mapping(device, "default", own=65)
        module_type = make_module_type("BRANCH-CARD")
        interface = make_interface(device, "Te1/1/1")
        rows = [
            _item(
                10,
                module_type.model,
                "Slot 1",
                _librenms_port_id=6501,
                _librenms_ifname=interface.name,
            )
        ]

        request = _post_branch(device, _with_installable_child(rows, 10, "branch-install"), 10, 65, live_librenms)

        interface.refresh_from_db()
        assert Module.objects.filter(device=device, module_type=module_type).exists()
        assert read_mapping(interface).own_id("default") == 6501
        assert any("Installed" in text for text in message_texts(request, "success"))

    def test_a_bind_failure_is_reported_next_to_the_install(self, live_librenms):
        from dcim.models import Module

        device = make_device_with_module_bays("branch-bind-miss", ["Slot 1"])
        seed_mapping(device, "default", own=66)
        module_type = make_module_type("BRANCH-MISS-CARD")
        rows = [_item(10, module_type.model, "Slot 1", _librenms_port_id=6601, _librenms_ifname="No Such Interface")]

        request = _post_branch(device, _with_installable_child(rows, 10, "branch-bind-miss"), 10, 66, live_librenms)

        assert Module.objects.filter(device=device, module_type=module_type).exists()
        assert any("no matching interface found for port_id 6601" in text for text in message_texts(request))

    def test_the_branch_install_uses_the_bay_the_table_shows(self, live_librenms):
        """The table and the installer run one matcher, so a trimmed class takes the same mapping on both sides."""
        from dcim.models import Module

        from netbox_librenms_plugin.models import ModuleBayMapping
        from netbox_librenms_plugin.tests.test_modules_view import _make_view, _run_build_context_real

        # The decoy bay carries the item's own name, so a class mismatch falls through to it.
        device = make_device_with_module_bays("branch-same-bay", ["PSU Bay A", "Power Supply 1"])
        seed_mapping(device, "default", own=69)
        module_type = make_module_type("BRANCH-SAME-BAY-PSU")
        ModuleBayMapping.objects.create(
            librenms_name="Power Supply 1", librenms_class="powerSupply", netbox_bay_name="PSU Bay A"
        )
        rows = _with_installable_child(
            [_item(10, module_type.model, "Power Supply 1", phys_class=" powerSupply ")], 10, "branch-same-bay"
        )

        table_row = next(row for row in _run_build_context_real(_make_view(), rows, device) if row["depth"] == 0)
        _post_branch(device, rows, 10, 69, live_librenms)

        assert table_row["module_bay"] == "PSU Bay A"
        assert Module.objects.get(device=device, module_type=module_type).module_bay.name == "PSU Bay A"

    def test_the_branch_of_a_fixed_nxos_line_card_holds_no_port_cage(self, live_librenms):
        """The serial-less cages only hold the optics, so the branch list holds the card and the optics only."""
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        from netbox_librenms_plugin.data_shapes.recordings_store import load_recording

        recording = load_recording("nxos-fixed-linecard")
        rows = recording["responses"][f"GET /api/v0/inventory/{recording['device_id']}/all"]["inventory"]
        (chassis,) = [row for row in rows if row["entPhysicalClass"] == "chassis"]
        (line_card,) = [
            row
            for row in rows
            if row["entPhysicalClass"] == "module" and row["entPhysicalModelName"] == chassis["entPhysicalModelName"]
        ]
        cages = [row for row in rows if row["entPhysicalContainedIn"] == line_card["entPhysicalIndex"]]
        cage_names = {row["entPhysicalName"] for row in cages if row["entPhysicalClass"] == "port"}
        cage_indexes = {row["entPhysicalIndex"] for row in cages}
        optic_names = {row["entPhysicalName"] for row in rows if row["entPhysicalContainedIn"] in cage_indexes}
        assert (len(cage_names), len(optic_names)) == (54, 31)
        assert not cage_names & optic_names
        device = make_device("branch-nxos-line-card")
        seed_mapping(device, "default", own=recording["device_id"])
        seed_inventory(InstallBranchView(), device, rows, librenms_id=recording["device_id"])

        rendered = {row["ent_physical_index"]: row for row in _table_rows(device)}

        assert _branch_names(rendered, line_card["entPhysicalIndex"]) == {line_card["entPhysicalName"]} | optic_names

    def test_the_branch_holds_no_placeholder_model_the_table_hides(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("branch-placeholder-models")
        seed_mapping(device, "default", own=70)
        rows = [
            _item(10, "BRANCH-PLACEHOLDER-CARD", "Card"),
            _item(20, "BUILTIN", "Built-in unit", parent=10),
            _item(21, "N/A", "Unknown unit", parent=10),
            _item(30, "BRANCH-PLACEHOLDER-SFP", "Optic", parent=20),
        ]
        seed_inventory(InstallBranchView(), device, rows, librenms_id=70)

        rendered = {row["ent_physical_index"]: row for row in _table_rows(device)}

        assert {row["name"] for row in rendered.values()} == _branch_names(rendered, 10) == {"Card", "Optic"}

    def test_a_snapshot_cached_before_the_schema_version_is_a_missing_snapshot(self, live_librenms):
        """Rows from an older writer lack markers the readers rely on, so the branch install must not use them."""
        from dcim.models import Module
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device_with_module_bays("branch-pre-version", ["Slot 1"])
        seed_mapping(device, "default", own=72)
        module_type = make_module_type("BRANCH-PRE-VERSION-CARD")
        rows = _with_installable_child([_item(10, module_type.model, "Slot 1")], 10, "branch-pre-version")
        view = InstallBranchView()
        view._librenms_api = live_librenms.api
        # The page renders from the current payload; an older writer then replaces it.
        seed_inventory(view, device, rows, librenms_id=72)
        request = make_request("post", _branch_post(device, 10), user=make_superuser(), path="/modules/")
        key = _seed_pre_version_snapshot(view, device, rows, 72)
        cached = cache.get(key)

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert message_texts(request, "error") == ["No cached inventory data. Please refresh modules first."]
        assert not Module.objects.filter(device=device).exists()
        # A refresh can store a current snapshot under the key after the read, so the reader deletes nothing.
        assert cache.get(key) == cached

    @SKIPPED_HOLDER_SHAPES
    def test_a_row_below_a_skipped_holder_has_no_branch(self, live_librenms, holder_class, holder_model):
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device, rows = _optic_below_a_skipped_holder("branch-skipped-holder", holder_class, holder_model, 74)
        seed_inventory(InstallBranchView(), device, rows, librenms_id=74)

        assert 20 not in {row["ent_physical_index"] for row in _table_rows(device)}
        assert _branch_post(device, 20) is None


class TestInstallSelectedView:
    """A tampered selection list is refused rather than partially installed."""

    def test_a_non_numeric_selection_is_rejected(self, live_librenms):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        device = make_device("selected-bad-index", librenms_cf={"default": 67})
        request = make_request(
            "post",
            {"select": ["not-a-number"], "server_key": "default"},
            user=make_superuser(),
            path="/modules/",
        )
        view = InstallSelectedView()
        view._librenms_api = live_librenms.api
        seed_inventory(view, device, [_item(10, "ANY-CARD", "Slot 1")], librenms_id=67)

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        assert "Invalid selection." in message_texts(request, "error")
        assert not Module.objects.filter(device=device).exists()

    def test_a_posted_holder_index_installs_nothing(self, live_librenms):
        """The table never renders a serial-less labelled container, so a crafted selection must not install it."""
        from dcim.models import Module

        device = make_device_with_module_bays("selected-holder", ["Holder Slot"])
        seed_mapping(device, "default", own=71)
        # A module type and a bay match the holder, so only the holder rule can stop the install.
        module_type = make_module_type("SELECTED-HOLDER-LABEL")
        rows = [_item(10, module_type.model, "Holder Slot", phys_class="container")]

        request = _post_selected(device, rows, [10], 71, live_librenms)

        assert not Module.objects.filter(device=device).exists()
        assert message_texts(request, "error") == [SELECTED_ROWS_REFUSED_MESSAGE.format(rows="Holder Slot")]

    def test_a_snapshot_cached_before_the_schema_version_is_a_missing_snapshot(self, live_librenms):
        from dcim.models import Module
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        device = make_device_with_module_bays("selected-pre-version", ["Slot 1"])
        seed_mapping(device, "default", own=73)
        module_type = make_module_type("SELECTED-PRE-VERSION-CARD")
        rows = [_item(10, module_type.model, "Slot 1")]
        view = InstallSelectedView()
        view._librenms_api = live_librenms.api
        # The page renders from the current payload; an older writer then replaces it.
        seed_inventory(view, device, rows, librenms_id=73)
        _action, fields = rendered_module_form(render_module_tab(device), "install-selected")
        request = make_request("post", {**fields, "select": ["10"]}, user=make_superuser(), path="/modules/")
        key = _seed_pre_version_snapshot(view, device, rows, 73)
        cached = cache.get(key)

        view_post(view, request, pk=device.pk)

        assert message_texts(request, "error") == ["No cached inventory data. Please refresh modules first."]
        assert not Module.objects.filter(device=device).exists()
        # A refresh can store a current snapshot under the key after the read, so the reader deletes nothing.
        assert cache.get(key) == cached

    @SKIPPED_HOLDER_SHAPES
    def test_a_selected_row_below_a_skipped_holder_installs_nothing(self, live_librenms, holder_class, holder_model):
        from dcim.models import Module

        device, rows = _optic_below_a_skipped_holder("selected-skipped-holder", holder_class, holder_model, 75)

        request = _post_selected(device, rows, [20], 75, live_librenms)

        assert message_texts(request, "error") == [SELECTED_ROWS_REFUSED_MESSAGE.format(rows="Optic 1")]
        assert not Module.objects.filter(device=device).exists()


@pytest.mark.parametrize(("action", "occupied"), [("branch", True), ("selected", True), ("selected", False)])
def test_batch_install_checks_serial_length_only_for_creation(live_librenms, action, occupied):
    from dcim.models import Interface, Module

    from netbox_librenms_plugin.models import NormalizationRule

    device = make_device_with_module_bays("batch-serial-length", ["Slot 1"])
    seed_mapping(device, "default", own=74)
    module_type = make_module_type("BATCH-SERIAL-LENGTH-CARD")
    NormalizationRule.objects.create(
        scope="serial",
        match_pattern=r"^TAG (ABC123) .+$",
        replacement=r"\1",
        manufacturer=device.device_type.manufacturer,
    )
    occupant = None
    if occupied:
        occupant = install_module(device, "Slot 1", module_type.model, serial="ABC123", child_bays=["Child Bay"])
    interface = make_interface(device, "Te1/1/1")
    if occupant:
        Interface.objects.filter(pk=interface.pk).update(module=occupant)
    serial = "TAG ABC123 " + "X" * Module._meta.get_field("serial").max_length
    rows = [
        _item(
            10,
            module_type.model,
            "Slot 1",
            entPhysicalSerialNum=serial,
            _librenms_port_id=7401,
            _librenms_ifname=interface.name,
        )
    ]

    if action == "branch":
        child_type = make_module_type("BATCH-SERIAL-LENGTH-CHILD")
        rows.append(_item(11, child_type.model, "Child Bay", parent=10))
        _post_branch(device, rows, 10, 74, live_librenms)
    else:
        request = _post_selected(device, rows, [10], 74, live_librenms)

    interface.refresh_from_db()
    if occupant:
        occupant.refresh_from_db()
        assert occupant.serial == "ABC123"
        assert occupant.module_bay.name == "Slot 1"
        assert read_mapping(interface).own_id("default") == 7401
    else:
        # The table offers no install for a serial that does not fit, so the post names a row it does not offer.
        assert message_texts(request, "error") == [SELECTED_ROWS_REFUSED_MESSAGE.format(rows="Slot 1")]
        assert not Module.objects.filter(device=device).exists()
        assert read_mapping(interface).own_id("default") is None


@pytest.mark.parametrize(("held", "offered"), [("S/N ABC123", False), ("OTHER123", True)])
def test_an_empty_bay_offers_no_install_beside_a_legacy_row_that_stores_the_rule_output(live_librenms, held, offered):
    """An earlier release stored "S/N S/N ABC123" as its rule output "S/N ABC123"; the install refuses beside it."""
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.get_or_create(
        scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
    )
    device = make_device_with_module_bays(f"install-legacy-{offered}", ["Slot 1", "Slot 2"])
    seed_mapping(device, "default", own=77)
    module_type = make_module_type(f"INSTALL-LEGACY-CARD-{offered}")
    install_module(device, "Slot 2", f"INSTALL-LEGACY-HOLDER-{offered}", serial=held)
    child_type = make_module_type(f"INSTALL-LEGACY-CHILD-{offered}")
    rows = [
        _item(10, module_type.model, "Slot 1", entPhysicalSerialNum="S/N S/N ABC123"),
        _item(11, child_type.model, "Child Bay", parent=10),
    ]
    seed_inventory(_view_for_seed(), device, rows, librenms_id=77)

    html = render_module_tab(device)

    forms = module_tab_forms(html).forms
    install_forms = [
        form for form in forms if "install-module" in form["action"] and form["fields"].get("ent_index") == "10"
    ]
    assert len(install_forms) == (1 if offered else 0)
    # Install Selected and Install Branch create the module too, so the row is neither selectable nor a branch.
    checkbox = re.search(r'<input type="checkbox" name="select" value="10"[^>]*>', html).group(0)
    assert ("disabled" not in checkbox) is offered
    branch_forms = [
        form for form in forms if "install-branch" in form["action"] and form["fields"].get("parent_index") == "10"
    ]
    assert len(branch_forms) == (1 if offered else 0)


@pytest.mark.parametrize("fits", [True, False])
def test_an_empty_bay_offers_no_install_for_a_serial_that_does_not_fit(live_librenms, fits):
    """The serial field holds 50 characters; Install, Install Selected and Install Branch create the module."""
    import re

    from django.utils.html import escape

    device = make_device_with_module_bays(f"install-fit-{fits}", ["Slot 1"])
    seed_mapping(device, "default", own=76)
    module_type = make_module_type(f"INSTALL-FIT-CARD-{fits}")
    child_type = make_module_type(f"INSTALL-FIT-CHILD-{fits}")
    serial = "N" * (50 if fits else 51)
    rows = [
        _item(10, module_type.model, "Slot 1", entPhysicalSerialNum=serial),
        _item(11, child_type.model, "Child Bay", parent=10),
    ]
    seed_inventory(_view_for_seed(), device, rows, librenms_id=76)

    html = render_module_tab(device)

    actions = [form["action"] for form in module_tab_forms(html).forms]
    assert any("install-module" in action for action in actions) is fits
    assert any("install-branch" in action for action in actions) is fits
    checkbox = re.search(r'<input type="checkbox" name="select" value="10"[^>]*>', html).group(0)
    assert ("disabled" not in checkbox) is fits
    reason = "The LibreNMS serial has 51 characters; the field holds at most 50."
    assert (escape(reason) in html) is (not fits)


def _view_for_seed():
    from netbox_librenms_plugin.views.sync.modules import InstallModuleView

    return InstallModuleView()


class TestUpdateModuleSerialView:
    """The serial update validates its inputs before touching the module."""

    def test_an_invalid_selected_device_is_refused_without_a_fallback(self, live_librenms):
        from netbox_librenms_plugin.module_grants import GrantRefusal
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("serial-bad-inputs", librenms_cf={"default": 68})

        _view, request, response = _drive(
            UpdateModuleSerialView,
            device,
            {"module_id": "", "selected_device_id": "not-a-pk", "server_key": "default"},
            live_librenms,
        )

        assert response.status_code == 302
        assert message_texts(request, "error") == [GrantRefusal.DESTINATION.message]
        assert message_texts(request, "warning") == []

    @pytest.mark.parametrize("placeholder", ["N/A", "BUILTIN", "null"])
    def test_a_placeholder_serial_keeps_the_stored_serial(self, live_librenms, placeholder):
        """An update never clears a stored serial because LibreNMS reports none."""
        from netbox_librenms_plugin.inventory_policy import load_inventory_policy
        from netbox_librenms_plugin.module_grants import (
            ACTION_UPDATE_SERIAL,
            RENDER_ATTRIBUTED,
            installed_module_target,
            module_inventory_snapshot_digest,
            sign_module_grant,
        )
        from netbox_librenms_plugin.sync_cache import cached_module_inventory
        from netbox_librenms_plugin.views.sync.modules import (
            NO_INCOMING_SERIAL_MESSAGE,
            UpdateModuleSerialView,
            _get_cached_module_snapshot,
        )

        device = make_device_with_module_bays(f"serial-placeholder-{len(placeholder)}", ["Slot 1"])
        seed_mapping(device, "default", own=68)
        module = install_module(device, "Slot 1", f"SERIAL-PLACEHOLDER-CARD-{len(placeholder)}", serial="OLD")
        view = UpdateModuleSerialView()
        view._librenms_api = live_librenms.api
        rows = [_item(11, module.module_type.model, "Slot 1", entPhysicalSerialNum=placeholder)]
        seed_inventory(view, device, rows, librenms_id=68)
        inventory = cached_module_inventory(_get_cached_module_snapshot(device, "default", view.get_cache_key))
        grant = sign_module_grant(
            page_device_id=device.pk,
            device_id=device.pk,
            server_key="default",
            action=ACTION_UPDATE_SERIAL,
            inventory_digest=module_inventory_snapshot_digest(inventory),
            policy_fingerprint=load_inventory_policy(device.pk).fingerprint,
            render_mode=RENDER_ATTRIBUTED,
            ent_index=11,
            action_target=installed_module_target(module.pk, module.module_bay_id),
        )
        data = {"module_id": module.pk, "ent_index": 11, "server_key": "default", "inventory_binding": grant}
        request = make_request("post", data, user=make_superuser(), path="/modules/")

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        # A stale grant also redirects and keeps the serial, so the refusal must be the missing serial.
        assert message_texts(request, "error") == [NO_INCOMING_SERIAL_MESSAGE]
        module.refresh_from_db()
        assert module.serial == "OLD"

    def test_a_snapshot_merged_under_another_serial_context_refuses_the_update(self, live_librenms):
        """The grant signs the policy and the rows; the snapshot stamp must also be the policy's context."""
        from django.core.cache import cache

        from netbox_librenms_plugin.inventory_policy import load_inventory_policy
        from netbox_librenms_plugin.module_grants import (
            ACTION_UPDATE_SERIAL,
            RENDER_ATTRIBUTED,
            GrantRefusal,
            installed_module_target,
            module_inventory_snapshot_digest,
            sign_module_grant,
        )
        from netbox_librenms_plugin.serial_identity import SerialContext
        from netbox_librenms_plugin.sync_cache import module_inventory_cache_payload
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device_with_module_bays("serial-other-stamp", ["Slot 1"])
        seed_mapping(device, "default", own=68)
        module = install_module(device, "Slot 1", "SERIAL-OTHER-STAMP-CARD", serial="OLD-SERIAL")
        view = UpdateModuleSerialView()
        view._librenms_api = live_librenms.api
        rows = [_item(11, module.module_type.model, "Slot 1", entPhysicalSerialNum="NEW-SERIAL")]
        key = view.get_cache_key(device, "inventory", server_key="default")
        other = SerialContext(None, (), unavailable="another context")
        cache.set(key, module_inventory_cache_payload(rows, 68, None, serial_context=other), timeout=300)
        grant = sign_module_grant(
            page_device_id=device.pk,
            device_id=device.pk,
            server_key="default",
            action=ACTION_UPDATE_SERIAL,
            inventory_digest=module_inventory_snapshot_digest(rows),
            policy_fingerprint=load_inventory_policy(device.pk).fingerprint,
            render_mode=RENDER_ATTRIBUTED,
            ent_index=11,
            action_target=installed_module_target(module.pk, module.module_bay_id),
        )
        data = {"module_id": module.pk, "ent_index": 11, "server_key": "default", "inventory_binding": grant}
        request = make_request("post", data, user=make_superuser(), path="/modules/")

        try:
            response = view_post(view, request, pk=device.pk)
        finally:
            cache.delete(key)

        assert response.status_code == 302
        assert message_texts(request, "error") == [GrantRefusal.SNAPSHOT.message]
        module.refresh_from_db()
        assert module.serial == "OLD-SERIAL"

    def test_an_invalid_module_id_is_reported(self, live_librenms):
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        device = make_device("serial-bad-module", librenms_cf={"default": 68})

        _view, request, response = _drive(
            UpdateModuleSerialView, device, {"module_id": "", "server_key": "default"}, live_librenms
        )

        assert response.status_code == 302
        assert "Missing or invalid module ID." in message_texts(request, "error")

    @pytest.mark.parametrize(("other_serial", "refused"), [("S/N LEGACY-SN1", True), ("OTHER-SN2", False)])
    def test_a_legacy_row_that_stores_the_rule_output_refuses_the_update(self, live_librenms, other_serial, refused):
        """An earlier release stored "S/N S/N LEGACY-SN1" as its rule output "S/N LEGACY-SN1", which keys again."""
        from dcim.models import Module

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

        NormalizationRule.objects.get_or_create(
            scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
        )
        device = make_device_with_module_bays(f"serial-legacy-holder-{refused}", ["Slot 1", "Slot 2"])
        seed_mapping(device, "default", own=69)
        module = install_module(device, "Slot 1", f"SERIAL-LEGACY-CARD-{refused}", serial="OLD-SN1")
        other = install_module(device, "Slot 2", f"SERIAL-LEGACY-OTHER-{refused}", serial="PRE-RENDER-SN")
        view = UpdateModuleSerialView()
        view._librenms_api = live_librenms.api
        rows = [_item(11, module.module_type.model, "Slot 1", entPhysicalSerialNum="S/N S/N LEGACY-SN1")]
        seed_inventory(view, device, rows, librenms_id=69)
        _action, data = rendered_module_form(render_module_tab(device), "update-module-serial", 11)
        # The table offers no update beside the legacy row, so its serial lands after the render.
        Module.objects.filter(pk=other.pk).update(serial=other_serial)
        request = make_request("post", data, user=make_superuser(), path="/modules/")

        response = view_post(view, request, pk=device.pk)

        assert response.status_code == 302
        module.refresh_from_db()
        other.refresh_from_db()
        if refused:
            assert message_texts(request, "error") == [
                "Serial 'S/N S/N LEGACY-SN1' is already installed on this device. Nothing was changed."
            ]
            assert module.serial == "OLD-SN1"
        else:
            assert message_texts(request, "error") == []
            assert module.serial == "S/N S/N LEGACY-SN1"
        assert other.serial == other_serial


class TestInventoryIgnoreRulesDuringCollection:
    """The branch list is the table's own: it honours the skip and transparent rules the table renders with."""

    @staticmethod
    def _rule(name, pattern, action):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        return InventoryIgnoreRule.objects.create(
            name=name,
            match_type=InventoryIgnoreRule.MATCH_CONTAINS,
            pattern=pattern,
            action=action,
            require_serial_match_parent=False,
        )

    @staticmethod
    def _rows(rows, tag):
        from netbox_librenms_plugin.tests.test_modules_view import _make_view, _run_build_context_real

        return {row["ent_physical_index"]: row for row in _run_build_context_real(_make_view(), rows, make_device(tag))}

    @staticmethod
    def _branch(rows, index):
        return [entry_index for entry_index, _target in rows[index]["branch_rows"]]

    def test_a_skip_rule_drops_the_item_and_its_subtree(self):
        rows = [
            _item(1, "PARENT", "Slot 1"),
            _item(2, "SKIP-ME", "FT-IDPROM", parent=1),
            _item(3, "DEEP-SKIP", "FT-IDPROM-child", parent=2),
            _item(4, "KEEP-ME", "NormalChild", parent=1),
        ]
        self._rule("idprom-skip", "IDPROM", "skip")

        assert self._branch(self._rows(rows, "branch-skip"), 1) == [1, 4]

    def test_a_transparent_rule_promotes_the_children(self):
        rows = [
            _item(1, "PARENT", "Slot 1"),
            _item(2, "TRANSPARENT", "T-IDPROM", parent=1),
            _item(3, "GRANDCHILD", "Optic 1", parent=2),
        ]
        self._rule("idprom-transparent", "IDPROM", "transparent")

        assert self._branch(self._rows(rows, "branch-transparent"), 1) == [1, 3]

    def test_a_transparent_root_is_replaced_by_its_children(self):
        rows = [
            _item(1, "ROOT", "RP-IDPROM"),
            _item(2, "CHILD", "Optic 1", parent=1),
        ]
        self._rule("root-transparent", "IDPROM", "transparent")

        rendered = self._rows(rows, "branch-transparent-root")

        assert set(rendered) == {2}
        assert self._branch(rendered, 2) == [2]

    def test_a_skipped_root_collects_nothing(self):
        rows = [
            _item(1, "ROOT", "RP-IDPROM"),
            _item(2, "CHILD", "Optic 1", parent=1),
        ]
        self._rule("root-skip", "IDPROM", "skip")

        assert self._rows(rows, "branch-skip-root") == {}


@pytest.mark.parametrize(("held", "offered"), [("S/N ABC123", False), ("OTHER123", True)])
def test_an_occupied_bay_offers_no_serial_update_beside_a_legacy_row(live_librenms, held, offered):
    """An earlier release stored "S/N S/N ABC123" as its rule output "S/N ABC123"; the update refuses beside it."""
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.get_or_create(
        scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
    )
    device = make_device_with_module_bays(f"update-legacy-{offered}", ["Slot 1", "Slot 2"])
    seed_mapping(device, "default", own=78)
    module = install_module(device, "Slot 1", f"UPDATE-LEGACY-CARD-{offered}", serial="OLD-SN1")
    install_module(device, "Slot 2", f"UPDATE-LEGACY-HOLDER-{offered}", serial=held)
    rows = [_item(10, module.module_type.model, "Slot 1", entPhysicalSerialNum="S/N S/N ABC123")]
    seed_inventory(_view_for_seed(), device, rows, librenms_id=78)

    html = render_module_tab(device)

    update_forms = [
        form
        for form in module_tab_forms(html).forms
        if "update-module-serial" in form["action"] and form["fields"].get("ent_index") == "10"
    ]
    assert len(update_forms) == (1 if offered else 0)
    # A batch install of an occupied bay creates no module and writes no serial, so the row stays selectable.
    checkbox = re.search(r'<input type="checkbox" name="select" value="10"[^>]*>', html).group(0)
    assert "disabled" not in checkbox


def test_a_lone_legacy_module_keeps_its_own_serial_update(live_librenms):
    """The module stores the rule output "S/N ABC123"; the update writer leaves that module out, so it heals it."""
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.views.sync.modules import UpdateModuleSerialView

    NormalizationRule.objects.get_or_create(
        scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
    )
    device = make_device_with_module_bays("update-lone-legacy", ["Slot 1"])
    seed_mapping(device, "default", own=79)
    module = install_module(device, "Slot 1", "UPDATE-LONE-LEGACY-CARD", serial="S/N ABC123")
    view = UpdateModuleSerialView()
    view._librenms_api = live_librenms.api
    rows = [_item(10, module.module_type.model, "Slot 1", entPhysicalSerialNum="S/N S/N ABC123")]
    seed_inventory(view, device, rows, librenms_id=79)

    _action, data = rendered_module_form(render_module_tab(device), "update-module-serial", 10)
    request = make_request("post", data, user=make_superuser(), path="/modules/")
    response = view_post(view, request, pk=device.pk)

    assert response.status_code == 302
    assert message_texts(request, "error") == []
    module.refresh_from_db()
    assert module.serial == "S/N S/N ABC123"


@pytest.mark.parametrize(("held", "offered"), [("NEW123", False), ("OTHER123", True)])
def test_an_occupied_bay_offers_no_serial_update_beside_a_confirmed_holder(live_librenms, held, offered):
    """Slot 2 already stores the reported serial, so the update writer refuses it for Slot 1."""
    device = make_device_with_module_bays(f"update-holder-{offered}", ["Slot 1", "Slot 2"])
    seed_mapping(device, "default", own=80)
    module = install_module(device, "Slot 1", f"UPDATE-HOLDER-CARD-{offered}", serial="OLD123")
    install_module(device, "Slot 2", f"UPDATE-HOLDER-OTHER-{offered}", serial=held)
    rows = [_item(10, module.module_type.model, "Slot 1", entPhysicalSerialNum="NEW123")]
    seed_inventory(_view_for_seed(), device, rows, librenms_id=80)

    html = render_module_tab(device)

    update_forms = [
        form
        for form in module_tab_forms(html).forms
        if "update-module-serial" in form["action"] and form["fields"].get("ent_index") == "10"
    ]
    assert len(update_forms) == (1 if offered else 0)


@pytest.mark.parametrize("fits", [True, False])
def test_a_bayless_parent_with_a_long_serial_keeps_the_branch_of_its_children(live_librenms, fits):
    """The parent matches no bay, so its children install into the device's own bays without it."""
    from dcim.models import Module

    device = make_device_with_module_bays(f"branch-bayless-{fits}", ["Slot 1"])
    seed_mapping(device, "default", own=81)
    parent_type = make_module_type(f"BRANCH-BAYLESS-PARENT-{fits}")
    child_type = make_module_type(f"BRANCH-BAYLESS-CHILD-{fits}")
    rows = [
        _item(10, parent_type.model, "Chassis Carrier", entPhysicalSerialNum="N" * (50 if fits else 51)),
        _item(11, child_type.model, "Slot 1", parent=10, entPhysicalSerialNum="CHILD-SN1"),
    ]
    seed_inventory(_view_for_seed(), device, rows, librenms_id=81)

    html = render_module_tab(device)

    branch_forms = [
        form
        for form in module_tab_forms(html).forms
        if "install-branch" in form["action"] and form["fields"].get("parent_index") == "10"
    ]
    assert len(branch_forms) == 1

    request = _post_branch(device, rows, 10, 81, live_librenms)

    assert message_texts(request, "error") == []
    installed = Module.objects.filter(device=device)
    assert [(module.module_bay.name, module.serial) for module in installed] == [("Slot 1", "CHILD-SN1")]
    assert not Module.objects.filter(module_type=parent_type).exists()


@pytest.mark.parametrize("held_here", [True, False])
def test_the_serial_update_reads_its_device_again_when_the_lookup_stops_early(live_librenms, held_here):
    """Three lower-pk holders elsewhere fill the bounded read, so a holder on the row's device needs its own read."""
    for number in range(3):
        elsewhere = make_device_with_module_bays(f"update-early-other-{held_here}-{number}", ["Slot 1"])
        install_module(elsewhere, "Slot 1", f"UPDATE-EARLY-OTHER-{held_here}-{number}", serial="NEW123")
    device = make_device_with_module_bays(f"update-early-{held_here}", ["Slot 1", "Slot 2"])
    seed_mapping(device, "default", own=82)
    module = install_module(device, "Slot 1", f"UPDATE-EARLY-CARD-{held_here}", serial="OLD123")
    install_module(device, "Slot 2", f"UPDATE-EARLY-HOLDER-{held_here}", serial="NEW123" if held_here else "OTHER123")
    rows = [_item(10, module.module_type.model, "Slot 1", entPhysicalSerialNum="NEW123")]
    seed_inventory(_view_for_seed(), device, rows, librenms_id=82)

    html = render_module_tab(device)

    update_forms = [
        form
        for form in module_tab_forms(html).forms
        if "update-module-serial" in form["action"] and form["fields"].get("ent_index") == "10"
    ]
    assert len(update_forms) == (0 if held_here else 1)
