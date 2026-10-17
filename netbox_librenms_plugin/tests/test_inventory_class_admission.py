"""Inventory class admission through include rules: the seeded classes, filters, display flag and failures."""

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from netbox_librenms_plugin.tests.test_modules_view import (
    _make_view,
    _run_build_context_real,
    _two_server_keys,
)

pytestmark = pytest.mark.django_db

# Rows [entPhysicalIndex, depth, name, class] per recording, rendered by the class constant before the rules.
# The two ArcOS supplies are the exception: that constant hid a model-less entity with a serial by mistake.
_ROWS_BEFORE_RULES = json.loads(Path(__file__).with_name("module_rows_by_recording.json").read_text(encoding="utf-8"))

SCB_RULE_NAME = "Juniper MX switch control boards"


def _recording(name):
    from netbox_librenms_plugin.data_shapes.recordings_store import load_recording

    return deepcopy(load_recording(name))


def _inventory(recording):
    return recording["responses"][f"GET /api/v0/inventory/{recording['device_id']}/all"]["inventory"]


def _mapped_device(name, recording, server_key, bay_names=()):
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, map_device_to_librenms

    device = make_device_with_module_bays(name, list(bay_names))
    map_device_to_librenms(device, recording["device_id"], server_key=server_key)
    return device


def _refresh(device, server_key):
    """POST a Modules refresh through the real view; return the response and the rendered module context."""
    from django.test.signals import template_rendered

    from netbox_librenms_plugin.tests.view_test_helpers import make_request, post
    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    view = DeviceModuleTableView()
    contexts = []

    def capture(sender, template, context, **kwargs):
        if template.name == view.partial_template_name:
            contexts.append(context["module_sync"])

    template_rendered.connect(capture)
    try:
        response = post(view, make_request("post", {"server_key": server_key}), pk=device.pk)
    finally:
        template_rendered.disconnect(capture)
    assert response.status_code == 200
    (module_sync,) = contexts
    return response, module_sync


def _rows(device, server_key):
    _response, module_sync = _refresh(device, server_key)
    return list(module_sync["table"].data)


def _entity(index, entity_class, name, descr, *, model="750-000001", serial=None, contained_in=1, position=0):
    return {
        "entPhysicalIndex": index,
        "entPhysicalClass": entity_class,
        "entPhysicalName": name,
        "entPhysicalDescr": descr,
        "entPhysicalModelName": model,
        "entPhysicalSerialNum": f"SN{index}" if serial is None else serial,
        "entPhysicalContainedIn": contained_in,
        "entPhysicalParentRelPos": position,
    }


def _chassis():
    return _entity(1, "chassis", "", "Juniper chassis", model="CHAS-TEST", contained_in=0)


@pytest.fixture
def server_key(settings, librenms_server):
    return _two_server_keys(settings, librenms_server, "class-admission")[0]


@pytest.mark.parametrize("name", sorted(_ROWS_BEFORE_RULES))
def test_the_seeded_rules_render_the_rows_the_class_constant_rendered(name, librenms_server, server_key):
    """AC1: after migration 0025 every bundled recording renders the same rows, with no operator action."""
    recording = _recording(name)
    librenms_server.load_recording(recording)
    device = _mapped_device(f"ac1-{name}", recording, server_key)

    rows = _rows(device, server_key)

    rendered = sorted([row["ent_physical_index"], row["depth"], row["name"], row["item_class"]] for row in rows)
    assert rendered == _ROWS_BEFORE_RULES[name]


class TestJuniperSwitchControlBoards:
    """AC2: the seeded SCB rule admits the MX480 boards and no other Juniper backplane row."""

    def test_the_mx480_recording_shows_both_boards_named_by_their_slot(self, librenms_server, server_key):
        recording = _recording("junos-mx-scb")
        entities = {entity["entPhysicalIndex"]: entity for entity in _inventory(recording)}
        boards = {idx for idx, entity in entities.items() if entity["entPhysicalName"].startswith("SCB")}
        backplanes = {idx for idx, entity in entities.items() if entity["entPhysicalClass"] == "backplane"}
        assert len(boards) == 2 and boards == backplanes
        librenms_server.load_recording(recording)
        device = _mapped_device("mx480-scb", recording, server_key)

        rows = {row["ent_physical_index"]: row for row in _rows(device, server_key)}

        assert {idx for idx, row in rows.items() if row["item_class"] == "backplane"} == boards
        for idx in boards:
            assert (rows[idx]["depth"], rows[idx]["name"]) == (0, entities[idx]["entPhysicalDescr"])

    def test_the_boards_match_their_bays_through_the_contrib_mapping(self, librenms_server, server_key):
        """The anonymizer hashes the slot label, so the test puts the real public labels back."""
        from netbox_librenms_plugin.tests.conftest import load_contrib_bay_mappings, make_module_type_with_bays

        recording = _recording("junos-mx-scb")
        boards = sorted(
            (e for e in _inventory(recording) if e["entPhysicalName"].startswith("SCB")),
            key=lambda e: e["entPhysicalParentRelPos"],
        )
        for slot, board in enumerate(boards):
            board["entPhysicalDescr"] = f"CB {slot}"
            make_module_type_with_bays(board["entPhysicalModelName"])
        load_contrib_bay_mappings()
        librenms_server.load_recording(recording)
        device = _mapped_device("mx480-scb-bays", recording, server_key, bay_names=("CB0", "CB1"))

        rows = {row["ent_physical_index"]: row for row in _rows(device, server_key)}

        assert [(rows[b["entPhysicalIndex"]]["name"], rows[b["entPhysicalIndex"]]["module_bay"]) for b in boards] == [
            ("CB 0", "CB0"),
            ("CB 1", "CB1"),
        ]

    @pytest.mark.parametrize(
        ("name", "descr", "model"),
        [
            ("JNP10K-RE1", "CB 0", "750-000002"),
            ("JNP10K-RE1-E", "CB 1", "750-000003"),
            ("PTX10002-36QDD-S", "CB 0", "750-000004"),
            ("", "Backplane", "Default"),
            ("JNP10008-CHAS", "Backplane", "750-000005"),
            ("JNP7100-32C-CHAS", "CB 0", "750-000006"),
            ("ACX7024-CB", "CB 0", "750-000007"),
            ("JNP304-CHAS", "CB 0", "750-123404"),
            ("", "CB 0", ""),
        ],
    )
    def test_another_juniper_backplane_shape_adds_no_row(self, name, descr, model):
        from netbox_librenms_plugin.tests.conftest import make_device

        noise = _entity(20, "backplane", name, descr, model=model)
        board = _entity(21, "backplane", "SCBE2-MX-S", "CB 0", position=1)

        rows = _run_build_context_real(_make_view(), [_chassis(), noise, board], make_device(f"noise-{name}-{descr}"))

        # The board is the positive control: the same rule set admits it, so the absence is the filter's.
        assert [(row["ent_physical_index"], row["name"]) for row in rows] == [(21, "CB 0")]


class TestBothReadersUseTheRules:
    """AC4: the row collector and the visible-ancestor walk read one rule predicate."""

    def test_the_class_constant_is_gone(self):
        from netbox_librenms_plugin.views.base import modules_view

        assert not hasattr(modules_view, "INVENTORY_CLASSES")
        assert not hasattr(modules_view, "_class_is_included")

    @pytest.mark.parametrize(("parent_name", "child_depth"), [("SCBE2-MX-S", 1), ("JNP10K-RE1", 0)])
    def test_a_filtered_parent_decides_whether_its_card_nests(self, parent_name, child_depth):
        """An admitted board parents its card; a board the name filter refuses leaves the card at top level."""
        from netbox_librenms_plugin.tests.conftest import make_device

        board = _entity(20, "backplane", parent_name, "CB 0")
        card = _entity(30, "module", "Mezzanine", "Mezzanine card", model="750-000010", contained_in=20)

        rows = _run_build_context_real(_make_view(), [_chassis(), board, card], make_device(f"nest-{parent_name}"))

        assert {row["ent_physical_index"]: row["depth"] for row in rows}[30] == child_depth


class TestClassRulesAreConfiguration:
    """AC3: a seeded class rule is an ordinary rule, so disabling it hides that class."""

    def test_disabling_the_module_class_hides_the_top_level_module_rows(self, librenms_server, server_key):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        recording = _recording("nxos-fixed-linecard")
        librenms_server.load_recording(recording)
        device = _mapped_device("nxos-no-module-class", recording, server_key)
        before = {row["ent_physical_index"]: row for row in _rows(device, server_key)}
        assert any(row["item_class"] == "module" and row["depth"] == 0 for row in before.values())

        InventoryIgnoreRule.objects.filter(name="Inventory class: module").update(enabled=False)
        after = {row["ent_physical_index"]: row for row in _rows(device, server_key)}

        assert {row["item_class"] for row in after.values()} == {"powerSupply", "fan"}
        assert set(after) == {idx for idx, row in before.items() if row["item_class"] in {"powerSupply", "fan"}}

    def test_a_module_below_another_admitted_row_stays_its_sub_component(self):
        """Descendants are not class-gated: the card still renders under its admitted carrier."""
        from netbox_librenms_plugin.models import InventoryIgnoreRule
        from netbox_librenms_plugin.tests.conftest import make_device

        carrier = _entity(20, "container", "FPC 0", "FPC 0 container", model="750-000020")
        nested = _entity(30, "module", "PIC 0", "PIC 0 card", model="750-000021", contained_in=20)
        loose = _entity(40, "module", "FPM", "Craft interface", model="750-000022")
        InventoryIgnoreRule.objects.filter(name="Inventory class: module").update(enabled=False)

        rows = _run_build_context_real(_make_view(), [_chassis(), carrier, nested, loose], make_device("nested-card"))

        assert {row["ent_physical_index"]: row["depth"] for row in rows} == {20: 0, 30: 1}


class TestModelLessHardware:
    """A missing model names no hardware, but a serial does: only a serial-less entity is a holder."""

    def test_the_arcos_supplies_without_a_model_render(self, librenms_server, server_key):
        recording = _recording("arcos-lag-transceivers")
        supplies = [entity for entity in _inventory(recording) if entity["entPhysicalClass"] == "powerSupply"]
        assert len(supplies) == 2
        assert all(not entity["entPhysicalModelName"] and entity["entPhysicalSerialNum"] for entity in supplies)
        librenms_server.load_recording(recording)
        device = _mapped_device("arcos-model-less-supplies", recording, server_key)

        depths = {row["ent_physical_index"]: row["depth"] for row in _rows(device, server_key)}

        assert {depths.get(entity["entPhysicalIndex"]) for entity in supplies} == {0}

    @pytest.mark.parametrize("entity_class", ["container", "port", "powerSupply", "fan"])
    def test_a_model_less_entity_without_a_serial_stays_a_holder(self, entity_class):
        from netbox_librenms_plugin.tests.conftest import make_device

        holder = _entity(20, entity_class, "Slot 0", "Slot 0", model="", serial="")
        card = _entity(30, "module", "Card", "Card", model="750-000030", contained_in=20)

        rows = _run_build_context_real(_make_view(), [_chassis(), holder, card], make_device(f"holder-{entity_class}"))

        # The card is the positive control: it reaches top level because its holder is no row.
        assert {row["ent_physical_index"]: row["depth"] for row in rows} == {30: 0}


class TestEmptyPolicy:
    """An empty table must not look like a device without hardware."""

    def test_no_enabled_include_rule_shows_an_alert_with_the_rule_list_link(self, librenms_server, server_key):
        from django.urls import reverse

        from netbox_librenms_plugin.models import InventoryIgnoreRule

        recording = _recording("nxos-fixed-linecard")
        librenms_server.load_recording(recording)
        device = _mapped_device("nxos-no-include-rule", recording, server_key)
        InventoryIgnoreRule.objects.filter(action=InventoryIgnoreRule.ACTION_INCLUDE).update(enabled=False)

        response, module_sync = _refresh(device, server_key)

        content = response.content.decode()
        assert module_sync["devices_without_inventory_class"] == [device]
        assert 'id="module-no-inventory-class"' in content
        assert reverse("plugins:netbox_librenms_plugin:inventoryignorerule_list") in content
        # Rows from the transceivers API keep their own path.
        assert all(row.get("item_class") not in {"module", "fan", "powerSupply"} for row in module_sync["table"].data)

    def test_the_seeded_rules_show_no_alert(self, librenms_server, server_key):
        recording = _recording("nxos-fixed-linecard")
        librenms_server.load_recording(recording)
        device = _mapped_device("nxos-seeded-rules", recording, server_key)

        response, module_sync = _refresh(device, server_key)

        assert module_sync["devices_without_inventory_class"] == []
        assert 'id="module-no-inventory-class"' not in response.content.decode()

    @staticmethod
    def _render_vc_modules_tab(client, settings, tag, inventory, admitted_manufacturer_of):
        """Render a mixed-vendor VC's Modules tab with one include rule scoped to one member's vendor."""
        from django.core.cache import cache
        from django.urls import reverse

        from netbox_librenms_plugin.models import InventoryIgnoreRule
        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.tests.test_modules_view import _make_mixed_manufacturer_chassis, configure_servers
        from netbox_librenms_plugin.tests.view_test_helpers import trusted_module_inventory_payload
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        configure_servers(
            settings, {"default": {"librenms_url": "https://librenms.example.com", "api_token": "test-token"}}
        )
        page, member, _member_manufacturer = _make_mixed_manufacturer_chassis(tag)
        admitting = page if admitted_manufacturer_of == "page" else member
        InventoryIgnoreRule.objects.filter(action=InventoryIgnoreRule.ACTION_INCLUDE).update(enabled=False)
        InventoryIgnoreRule.objects.create(
            name="Admit modules for one vendor",
            match_type=InventoryIgnoreRule.MATCH_CLASS_IS,
            pattern="module",
            action=InventoryIgnoreRule.ACTION_INCLUDE,
            require_serial_match_parent=False,
            manufacturer=admitting.device_type.manufacturer,
        )
        payload = trusted_module_inventory_payload(page, inventory, librenms_id=9304)
        cache.set(DeviceModuleTableView().get_cache_key(page, "inventory", server_key="default"), payload, 300)
        cache.set("librenms_device_info_default_9304", (True, {"device_id": 9304, "hostname": page.name}), 300)
        client.force_login(make_superuser(f"{tag}-user"))

        response = client.get(
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[page.pk]),
            {"tab": "modules", "server_key": "default"},
        )

        assert response.status_code == 200
        rows = list(response.context["module_sync"]["table"].data)
        alert = re.search(r'id="module-no-inventory-class".*?</div>', response.content.decode(), re.S)
        return page, member, rows, alert

    @pytest.mark.parametrize("admitted_manufacturer_of", ["page", "member"])
    def test_a_vc_alert_names_the_member_whose_rows_no_rule_admits(self, client, settings, admitted_manufacturer_of):
        """VC rows follow their member's policy, so the alert reads every policy that a row uses."""
        # Position 2 attributes a row to the member; a row without a position stays on the page.
        member_row = _entity(161, "module", "Member card", "Member card", contained_in=0, position=2)
        page_row = _entity(163, "module", "Page card", "Page card", contained_in=0)
        del page_row["entPhysicalParentRelPos"]

        page, member, rows, alert = self._render_vc_modules_tab(
            client, settings, f"vc-alert-{admitted_manufacturer_of}", [member_row, page_row], admitted_manufacturer_of
        )

        admitting, refused = (page, member) if admitted_manufacturer_of == "page" else (member, page)
        assert [row["selected_device_id"] for row in rows] == [admitting.pk]
        assert alert is not None
        assert refused.name in alert.group(0)
        assert admitting.name not in alert.group(0)

    def test_a_vc_shows_no_alert_when_no_row_uses_the_refusing_policy(self, client, settings):
        """The page policy alone must not raise the alert when every row belongs to an admitted member."""
        member_row = _entity(161, "module", "Member card", "Member card", contained_in=0, position=2)

        _page, member, rows, alert = self._render_vc_modules_tab(
            client, settings, "vc-alert-member-only", [member_row], "member"
        )

        assert [row["selected_device_id"] for row in rows] == [member.pk]
        assert alert is None


def _break_the_scb_filter():
    """Store an invalid regex the way only a direct database edit can."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    rule = InventoryIgnoreRule.objects.get(name=SCB_RULE_NAME, manufacturer__isnull=True)
    InventoryIgnoreRule.objects.filter(pk=rule.pk).update(name_pattern="(")
    return rule


class TestInvalidStoredFilter:
    """A stored include filter that does not compile fails explicitly; it never admits a partial table."""

    def test_the_modules_tab_names_the_rule_and_shows_no_rows(self, librenms_server, server_key):
        recording = _recording("junos-mx-scb")
        librenms_server.load_recording(recording)
        device = _mapped_device("mx480-broken-filter", recording, server_key)
        rule = _break_the_scb_filter()

        response, module_sync = _refresh(device, server_key)

        content = response.content.decode()
        assert module_sync["table"] is None
        assert (module_sync["inventory_rule_error"].rule_pk, module_sync["inventory_rule_error"].field) == (
            rule.pk,
            "name_pattern",
        )
        assert 'id="module-inventory-rule-error"' in content
        assert rule.get_absolute_url() in content
        assert "No inventory data loaded" not in content
        assert "Inventory data refreshed successfully." not in content
        from django.core.cache import cache
        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, SyncTabState

        state = cache.get(SyncCacheConsistency(device).state_key(SyncTab.MODULES, server_key))
        assert state["state"] == SyncTabState.REFRESH_FAILED.value

    def test_the_rule_loader_raises_a_typed_error_naming_rule_and_field(self):
        from netbox_librenms_plugin.inventory_policy import load_inventory_policy
        from netbox_librenms_plugin.models import RuleConfigurationError
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("broken-filter-loader")
        rule = _break_the_scb_filter()

        with pytest.raises(RuleConfigurationError) as excinfo:
            load_inventory_policy(device.pk)

        assert (excinfo.value.rule_pk, excinfo.value.field) == (rule.pk, "name_pattern")
        assert f"(ID {rule.pk})" in str(excinfo.value)

    @pytest.mark.parametrize("action", ["install_selected", "install_branch"])
    def test_an_install_refuses_with_a_message(self, action):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.cache_test_helpers import seed_inventory
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type_with_bays
        from netbox_librenms_plugin.module_grants import GrantRefusal
        from netbox_librenms_plugin.tests.view_test_helpers import (
            make_request,
            make_user_with_perms,
            make_view,
            message_texts,
            post,
            render_module_tab,
            rendered_module_form,
        )
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView, InstallSelectedView
        from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping

        device = make_device_with_module_bays(f"broken-filter-{action}", ["Slot 1"])
        seed_mapping(device, "default", own=7)
        make_module_type_with_bays("CARD-1")
        make_module_type_with_bays("CARD-1-OPTIC")
        # The optic below the card is what makes the table render the card's branch button.
        inventory = [
            _chassis(),
            _entity(2, "module", "Slot 1", "Line card", model="CARD-1"),
            _entity(3, "module", "Optic", "SFP", model="CARD-1-OPTIC", contained_in=2),
        ]
        user = make_user_with_perms(
            f"broken-filter-{action}",
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
        view_class = InstallSelectedView if action == "install_selected" else InstallBranchView
        seed_inventory(view_class(), device, inventory, librenms_id=7)
        content = render_module_tab(device, user=user)
        if action == "install_selected":
            _action, data = rendered_module_form(content, "install-selected")
            data["select"] = ["2"]
        else:
            _action, data = rendered_module_form(content, "install-branch", 2)
        request = make_request("post", data, user=user)
        view = make_view(view_class, request, librenms_api=SimpleNamespace(server_key="default"))
        _break_the_scb_filter()

        post(view, request, pk=device.pk)

        assert GrantRefusal.RULE_ERROR.message in message_texts(request)
        assert not Module.objects.filter(device=device).exists()
