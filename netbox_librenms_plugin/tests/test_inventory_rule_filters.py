"""Filters on skip and transparent inventory rules: the matcher, the IOS XE recordings, the writers and failures."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.django_db

RECORDING = "iosxe-c8300"
FRONT_PANEL_RULE = "Cisco IOS XE built-in front-panel modules"
MODEL_RULE = "Fixed-platform built-in units"
# Row indices in the C8300 capture: built-in NIM controller, front-panel module, its optics, real modules.
MODULE_0, FRONT_PANEL, OPTICS, REAL_NIM, SERVICE_MODULE = 1000, 1015, (1131, 1141), 1245, 3015
# Built-in units that report the chassis PID: module 1 (SM controller), R0 and F0.
OTHER_BUILT_INS = (3000, 7000, 9000)


def _rule(**fields):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return InventoryIgnoreRule(**{"name": "Test", "require_serial_match_parent": False, **fields})


def _check(item, rules, parent=None, index_map=None, *, serial="", models=()):
    from netbox_librenms_plugin.views.base.modules_view import DeviceIdentity, _check_ignore_rules

    return _check_ignore_rules(item, parent, rules, index_map, DeviceIdentity(serial, frozenset(models)))


_CHASSIS = {"entPhysicalIndex": 1, "entPhysicalClass": "chassis", "entPhysicalContainedIn": 0}
_FRONT_PANEL_ITEM = {
    "entPhysicalIndex": 2,
    "entPhysicalClass": "module",
    "entPhysicalContainedIn": 1,
    "entPhysicalName": "NIM subslot 0/0",
    "entPhysicalDescr": "Front Panel 6 ports Gigabitethernet Module",
    "entPhysicalModelName": "C8300-1N1S-6T",
    "entPhysicalSerialNum": "SN-CHASSIS",
}
_REAL_NIM_ITEM = {
    **_FRONT_PANEL_ITEM,
    "entPhysicalName": "NIM subslot 0/1",
    "entPhysicalDescr": "16 ports Async Serial NIM",
}


class TestMatcher:
    """_check_ignore_rules(): a class branch, filters on every branch, and include rules never hide a row."""

    @pytest.mark.parametrize("action", ["skip", "transparent"])
    def test_a_class_rule_hides_the_rows_of_its_class_that_its_filters_match(self, action):
        rule = _rule(match_type="class_is", pattern="Module", action=action, description_pattern="^Front Panel")
        port = {**_FRONT_PANEL_ITEM, "entPhysicalClass": "port"}

        assert _check(_FRONT_PANEL_ITEM, [rule], _CHASSIS) == action
        assert _check(_REAL_NIM_ITEM, [rule], _CHASSIS) is None
        assert _check(port, [rule], _CHASSIS) is None

    @pytest.mark.parametrize(
        ("item_serial", "parent_serial", "matched"),
        [("", "", False), ("SN-1", "SN-1", True), ("SN-1", "SN-2", False), ("SN-1", "", False)],
    )
    def test_a_class_rule_honours_the_serial_guard(self, item_serial, parent_serial, matched):
        rule = _rule(match_type="class_is", pattern="module", action="skip", require_serial_match_parent=True)
        parent = {**_CHASSIS, "entPhysicalSerialNum": parent_serial}
        item = {**_FRONT_PANEL_ITEM, "entPhysicalSerialNum": item_serial}

        assert _check(item, [rule], parent, {1: parent, 2: item}) == ("skip" if matched else None)
        assert _check(item, [_rule(match_type="class_is", pattern="module", action="skip")], parent) == "skip"

    def test_an_include_rule_never_hides_a_row(self):
        rule = _rule(match_type="class_is", pattern="module", action="include", description_pattern="^Front")

        assert rule.matches_inventory_item(_FRONT_PANEL_ITEM) is True
        assert _check(_FRONT_PANEL_ITEM, [rule], _CHASSIS) is None

    @pytest.mark.parametrize(
        ("match_type", "pattern"),
        [
            ("contains", "subslot"),
            ("regex", r"^NIM subslot \d/\d$"),
            ("serial_matches_device", ""),
            ("model_matches_device_type", ""),
            ("class_is", "module"),
        ],
    )
    @pytest.mark.parametrize("action", ["skip", "transparent"])
    def test_every_match_type_needs_its_filters(self, match_type, pattern, action):
        """A blank filter keeps today's result; a non-blank one must match too."""
        fields = {"match_type": match_type, "pattern": pattern, "action": action}
        identity = {"serial": "SN-CHASSIS", "models": ("c8300-1n1s-6t",)}

        assert _check(_FRONT_PANEL_ITEM, [_rule(**fields)], _CHASSIS, **identity) == action
        assert _check(_REAL_NIM_ITEM, [_rule(**fields)], _CHASSIS, **identity) == action
        filtered = _rule(**fields, description_pattern="^Front Panel", model_pattern="^C8300")
        assert _check(_FRONT_PANEL_ITEM, [filtered], _CHASSIS, **identity) == action
        assert _check(_REAL_NIM_ITEM, [filtered], _CHASSIS, **identity) is None


def _recording():
    from netbox_librenms_plugin.data_shapes.recordings_store import load_recording

    return deepcopy(load_recording(RECORDING))


def _inventory(recording):
    return recording["responses"][f"GET /api/v0/inventory/{recording['device_id']}/all"]["inventory"]


def _entities(recording):
    return {entity["entPhysicalIndex"]: entity for entity in _inventory(recording)}


def _cisco():
    from dcim.models import Manufacturer

    return Manufacturer.objects.get_or_create(slug="cisco", defaults={"name": "Cisco"})[0]


def contrib_rule(name, **overrides):
    """Store the rule *name* of contrib/inventory_ignore_rules.yaml, its manufacturer resolved by name."""
    from dcim.models import Manufacturer

    from netbox_librenms_plugin.models import InventoryIgnoreRule

    contrib = Path(__file__).resolve().parents[2] / "contrib" / "inventory_ignore_rules.yaml"
    (record,) = [rule for rule in yaml.safe_load(contrib.read_text(encoding="utf-8")) if rule["name"] == name]
    record = {**record, **overrides}
    manufacturer = record.pop("manufacturer", "")
    record["manufacturer"] = Manufacturer.objects.get(name=manufacturer) if manufacturer else None
    return InventoryIgnoreRule.objects.create(**record)


def c8300_device(name, recording, server_key):
    """Map a Cisco device of the recorded chassis model, with device bays named like the optic and module holders."""
    from netbox_librenms_plugin.tests.conftest import (
        make_device_with_module_bays,
        make_module_type_with_bays,
        map_device_to_librenms,
    )

    entities = _entities(recording)
    holders = {optic: entities[optic]["entPhysicalContainedIn"] for optic in OPTICS}
    holders.update({module: entities[module]["entPhysicalContainedIn"] for module in (REAL_NIM, SERVICE_MODULE)})
    bays = [entities[holder]["entPhysicalName"] for holder in holders.values()]
    cisco = _cisco()
    device = make_device_with_module_bays(name, [*bays, entities[FRONT_PANEL]["entPhysicalName"]], manufacturer=cisco)
    device.device_type.model = entities[1]["entPhysicalModelName"]
    device.device_type.save()
    for index in (FRONT_PANEL, *OPTICS, REAL_NIM, SERVICE_MODULE):
        make_module_type_with_bays(entities[index]["entPhysicalModelName"], manufacturer=cisco)
    map_device_to_librenms(device, recording["device_id"], server_key=server_key)
    return device


def refresh(device, server_key):
    """POST a Modules refresh through the real view; return the response and the module context."""
    from netbox_librenms_plugin.tests.test_inventory_class_admission import _refresh

    return _refresh(device, server_key)


def rows_by_index(device, server_key):
    _response, module_sync = refresh(device, server_key)
    return {row["ent_physical_index"]: row for row in module_sync["table"].data}


@pytest.fixture
def server_key(settings, librenms_server):
    from netbox_librenms_plugin.tests.test_modules_view import _two_server_keys

    return _two_server_keys(settings, librenms_server, "rule-filters")[0]


class TestC8300Recording:
    """AC1: the contrib Front Panel rule on the real C8300 capture."""

    def test_the_rule_hides_the_built_in_module_and_its_optics_take_device_bays(self, librenms_server, server_key):
        recording = _recording()
        entities = _entities(recording)
        librenms_server.load_recording(recording)
        device = c8300_device("c8300-both-rules", recording, server_key)
        contrib_rule(MODEL_RULE)
        contrib_rule(FRONT_PANEL_RULE)

        rows = rows_by_index(device, server_key)

        assert not {MODULE_0, FRONT_PANEL, *OTHER_BUILT_INS} & set(rows)
        for optic in OPTICS:
            holder = entities[entities[optic]["entPhysicalContainedIn"]]["entPhysicalName"]
            assert (rows[optic]["depth"], rows[optic]["module_bay"]) == (0, holder)
        assert {REAL_NIM, SERVICE_MODULE} <= set(rows)

    def test_without_the_model_rule_the_optics_stay_below_module_0(self, librenms_server, server_key):
        recording = _recording()
        librenms_server.load_recording(recording)
        device = c8300_device("c8300-front-panel-only", recording, server_key)
        contrib_rule(FRONT_PANEL_RULE)

        rows = rows_by_index(device, server_key)

        assert FRONT_PANEL not in rows
        assert rows[MODULE_0]["depth"] == 0
        assert {rows[optic]["depth"] for optic in OPTICS} == {1}
        order = list(rows)
        assert all(order.index(MODULE_0) < order.index(optic) for optic in OPTICS)
        assert {REAL_NIM, SERVICE_MODULE} <= set(rows)

    def test_without_the_front_panel_rule_the_built_in_module_is_a_row(self, librenms_server, server_key):
        recording = _recording()
        librenms_server.load_recording(recording)
        device = c8300_device("c8300-model-only", recording, server_key)
        contrib_rule(MODEL_RULE)

        rows = rows_by_index(device, server_key)

        assert rows[FRONT_PANEL]["depth"] == 0
        assert {rows[optic]["depth"] for optic in OPTICS} == {1}

    def test_the_rule_is_scoped_to_cisco(self, librenms_server, server_key):
        from dcim.models import Manufacturer

        recording = _recording()
        librenms_server.load_recording(recording)
        device = c8300_device("c8300-other-vendor", recording, server_key)
        other = Manufacturer.objects.create(name="Other Vendor", slug="other-vendor")
        contrib_rule(FRONT_PANEL_RULE, manufacturer=other.name)

        assert FRONT_PANEL in rows_by_index(device, server_key)


class TestC8500LSyntheticCopy:
    """AC2, synthetic: the C8300 payload with both subslot modules relabelled as C8500L-8S4X built-in ones."""

    def test_both_built_in_subslot_modules_are_hidden(self, librenms_server, server_key):
        recording = _recording()
        entities = _entities(recording)
        entities[FRONT_PANEL]["entPhysicalDescr"] = "Front Panel 8 port Gigabitethernet Module"
        entities[REAL_NIM].update(
            {"entPhysicalDescr": "Front Panel 4 port Ten Gigabit ethernet Module", "entPhysicalSerialNum": ""}
        )
        librenms_server.load_recording(recording)
        device = c8300_device("c8500l-synthetic", recording, server_key)
        contrib_rule(MODEL_RULE)
        contrib_rule(FRONT_PANEL_RULE)

        rows = rows_by_index(device, server_key)

        assert not {FRONT_PANEL, REAL_NIM} & set(rows)
        assert SERVICE_MODULE in rows
        assert {rows[optic]["depth"] for optic in OPTICS} == {0}


def cached_inventory(device, server_key):
    from django.core.cache import cache

    from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

    return cache.get(DeviceModuleTableView().get_cache_key(device, "inventory", server_key=server_key))["inventory"]


def written_state(device):
    """Return the modules, module components and interfaces of *device*, to compare before and after a refusal."""
    from dcim.models import Interface, Module

    from netbox_librenms_plugin.views.sync.modules import _module_component_specs

    components = {
        model.__name__: list(model.objects.filter(device=device).order_by("pk").values())
        for _, _, model in _module_component_specs()
    }
    return {
        "modules": list(Module.objects.filter(device=device).values_list("pk", "module_bay_id", "serial")),
        "interfaces": list(Interface.objects.filter(device=device).order_by("pk").values()),
        "components": components,
    }


def post_selected(client, device, server_key, indices):
    from django.urls import reverse

    from netbox_librenms_plugin.utils import module_inventory_binding_token, module_inventory_snapshot_digest

    digest = module_inventory_snapshot_digest(cached_inventory(device, server_key))
    binding = module_inventory_binding_token(device.pk, server_key, "install_selected", {}, None, digest)
    return client.post(
        reverse("plugins:netbox_librenms_plugin:install_selected", kwargs={"pk": device.pk}),
        {"select": [str(index) for index in indices], "server_key": server_key, "inventory_binding": binding},
    )


def post_branch(client, device, server_key, parent_index):
    from django.urls import reverse

    from netbox_librenms_plugin.utils import module_inventory_binding_token, module_inventory_snapshot_digest

    digest = module_inventory_snapshot_digest(cached_inventory(device, server_key))
    binding = module_inventory_binding_token(
        device.pk, server_key, "install_branch", {"parent_index": parent_index}, parent_index, digest
    )
    return client.post(
        reverse("plugins:netbox_librenms_plugin:install_branch", kwargs={"pk": device.pk}),
        {"parent_index": str(parent_index), "server_key": server_key, "inventory_binding": binding},
    )


def messages_of(response):
    from django.contrib.messages import get_messages

    return [str(message.message) for message in get_messages(response.wsgi_request)]


@pytest.fixture
def ui(client):
    from netbox_librenms_plugin.tests.conftest import make_superuser

    client.force_login(make_superuser("rule-filters-ui"))
    return client


class TestBulkWriters:
    """AC4: the selected and branch installs read the same rules as the table."""

    def test_a_selected_install_of_the_built_in_module_refuses_it(self, ui, librenms_server, server_key):
        """The action is rendered before the rule exists, so only the writer's own check can refuse it."""
        from dcim.models import Module

        recording = _recording()
        librenms_server.load_recording(recording)
        device = c8300_device("c8300-selected", recording, server_key)
        contrib_rule(MODEL_RULE)
        rendered = rows_by_index(device, server_key)
        assert rendered[FRONT_PANEL]["can_install"] and rendered[REAL_NIM]["can_install"]
        contrib_rule(FRONT_PANEL_RULE)

        response = post_selected(ui, device, server_key, [FRONT_PANEL, REAL_NIM])

        assert response.status_code == 302
        installed = set(Module.objects.filter(device=device).values_list("module_bay__name", flat=True))
        assert installed == {rendered[REAL_NIM]["module_bay"]}
        assert any("matched ignore rule" in text for text in messages_of(response))

    def test_a_branch_install_skips_the_built_in_module_and_installs_its_optics(self, ui, librenms_server, server_key):
        from dcim.models import Module

        recording = _recording()
        entities = _entities(recording)
        librenms_server.load_recording(recording)
        device = c8300_device("c8300-branch", recording, server_key)
        contrib_rule(MODEL_RULE)
        contrib_rule(FRONT_PANEL_RULE)
        rendered = rows_by_index(device, server_key)

        response = post_branch(ui, device, server_key, MODULE_0)

        assert response.status_code == 302
        installed = dict(Module.objects.filter(device=device).values_list("module_bay__name", "module_type__model"))
        assert installed == {
            rendered[index]["module_bay"]: entities[index]["entPhysicalModelName"] for index in (*OPTICS, REAL_NIM)
        }


def _break_the_rule(rule):
    """Store an invalid regex the way only a direct database edit can."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    InventoryIgnoreRule.objects.filter(pk=rule.pk).update(description_pattern="(")


class TestInvalidStoredFilter:
    """AC6: a corrupt filter on a skip or transparent rule fails visibly and every bulk install refuses."""

    @pytest.mark.parametrize("action", ["skip", "transparent"])
    def test_the_modules_tab_names_the_rule(self, librenms_server, server_key, action):
        recording = _recording()
        librenms_server.load_recording(recording)
        device = c8300_device(f"c8300-broken-tab-{action}", recording, server_key)
        rule = contrib_rule(FRONT_PANEL_RULE, action=action)
        _break_the_rule(rule)

        response, module_sync = refresh(device, server_key)

        content = response.content.decode()
        assert module_sync["table"] is None
        assert (module_sync["inventory_rule_error"].rule_pk, module_sync["inventory_rule_error"].field) == (
            rule.pk,
            "description_pattern",
        )
        assert 'id="module-inventory-rule-error"' in content
        assert rule.get_absolute_url() in content

    @pytest.mark.parametrize("writer", ["selected", "branch"])
    @pytest.mark.parametrize("action", ["skip", "transparent"])
    def test_a_bulk_install_refuses_with_no_write(self, ui, librenms_server, server_key, action, writer):
        from netbox_librenms_plugin.views.sync.modules import INVALID_INVENTORY_RULE_MESSAGE

        recording = _recording()
        librenms_server.load_recording(recording)
        device = c8300_device(f"c8300-broken-{writer}-{action}", recording, server_key)
        rows_by_index(device, server_key)
        rule = contrib_rule(FRONT_PANEL_RULE, action=action)
        _break_the_rule(rule)
        before = written_state(device)

        if writer == "selected":
            response = post_selected(ui, device, server_key, [REAL_NIM])
        else:
            response = post_branch(ui, device, server_key, MODULE_0)

        assert INVALID_INVENTORY_RULE_MESSAGE in messages_of(response)
        assert written_state(device) == before


def test_the_contrib_front_panel_rule_imports_as_yaml(ui):
    """AC5: the contrib example passes the real YAML import."""
    from django.urls import reverse

    from netbox_librenms_plugin.models import InventoryIgnoreRule

    _cisco()
    contrib = Path(__file__).resolve().parents[2] / "contrib" / "inventory_ignore_rules.yaml"
    (record,) = [r for r in yaml.safe_load(contrib.read_text(encoding="utf-8")) if r["name"] == FRONT_PANEL_RULE]

    response = ui.post(
        reverse("plugins:netbox_librenms_plugin:inventoryignorerule_bulk_import"),
        {"data": yaml.safe_dump([record]), "format": "yaml", "csv_delimiter": ","},
    )

    assert response.status_code == 302, response.content.decode()[:2000]
    rule = InventoryIgnoreRule.objects.get(name=FRONT_PANEL_RULE)
    assert (rule.match_type, rule.pattern, rule.action, rule.description_pattern) == (
        "class_is",
        "module",
        "transparent",
        "^Front Panel",
    )
    assert (rule.require_serial_match_parent, rule.manufacturer.slug, rule.enabled) == (False, "cisco", True)
