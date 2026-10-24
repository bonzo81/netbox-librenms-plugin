"""
Module writers act only on the rows the Modules table rendered, under the policy it rendered them with.

Each test renders the real Modules tab over HTTP from a recording, takes the grant the rendered page
carries, changes one input, and posts the form. A writer must refuse a changed input before any write.
"""

import json
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from netbox_librenms_plugin.tests.test_inventory_rule_filters import (
    FRONT_PANEL,
    MODEL_RULE,
    REAL_NIM,
    SERVICE_MODULE,
    _entities,
    c8300_device,
    contrib_rule,
    messages_of,
    written_state,
)
from netbox_librenms_plugin.tests.view_test_helpers import (
    module_tab_forms,
    rendered_module_form,
    rendered_preview_url,
)

pytestmark = pytest.mark.django_db

SERVER_KEY = "grants"
OLD_SALT = "netbox_librenms_plugin.module_inventory_binding"


def _recording():
    from netbox_librenms_plugin.data_shapes.recordings_store import load_recording

    return deepcopy(load_recording("iosxe-c8300"))


@pytest.fixture
def ui(client):
    from netbox_librenms_plugin.tests.conftest import make_superuser

    client.force_login(make_superuser("module-grants-ui"))
    return client


@pytest.fixture
def c8300(settings, recording_server):
    """Map a C8300 to a recording server whose built-in units the seeded model rule hides."""
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

    recording = _recording()
    server, _api = recording_server(recording, server_key=SERVER_KEY)
    configure_librenms_servers(
        settings, {SERVER_KEY: {"librenms_url": server.url, "api_token": "grants-token", "verify_ssl": False}}
    )
    device = c8300_device("c8300-grants", recording, SERVER_KEY)
    contrib_rule(MODEL_RULE)
    return device, _entities(recording)


def render(client, device):
    """Refresh the Modules tab through its real HTMX endpoint and return the rendered fragment."""
    from django.urls import reverse

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    return response.content.decode()


def post(client, action, fields):
    return client.post(action, fields)


def install_module(device, bay_name, model, serial):
    """Install a module the way an operator would have before the page was rendered."""
    from dcim.models import Module, ModuleBay, ModuleType

    return Module.objects.create(
        device=device,
        module_bay=ModuleBay.objects.get(device=device, name=bay_name),
        module_type=ModuleType.objects.get(model=model),
        serial=serial,
    )


def cache_key(device):
    from netbox_librenms_plugin.views.mixins import CacheMixin

    return CacheMixin().get_cache_key(device, "inventory", SERVER_KEY)


def edit_cached_row(device, index, **fields):
    """Change one row of the cached snapshot, as the next refresh would after LibreNMS reported it."""
    from django.core.cache import cache

    payload = cache.get(cache_key(device))
    for item in payload["inventory"]:
        if item["entPhysicalIndex"] == index:
            item.update(fields)
    cache.set(cache_key(device), payload)


def _old_digest(value):
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def old_format_token(device, writer, fields):
    """Sign, for the current state, the payload a page rendered before this change carried."""
    from django.core import signing
    from django.core.cache import cache

    inventory = cache.get(cache_key(device))["inventory"]
    action, target_fields, row_bound = OLD_ACTIONS[writer]
    index = fields.get("ent_index") or fields.get("parent_index")
    ent_index = int(index) if index else None
    if row_bound:
        digest = _old_digest(next(item for item in inventory if item["entPhysicalIndex"] == ent_index))
    else:
        digest = _old_digest({"inventory": inventory})
    payload = {
        "device_id": int(fields.get("selected_device_id") or device.pk),
        "server_key": SERVER_KEY,
        "action": action,
        "action_target": {name: int(fields[name]) for name in sorted(target_fields)},
        "ent_index": ent_index,
        "inventory_digest": digest,
    }
    if action == "install_branch":
        payload["action_target"] = {"parent_index": ent_index}
    return signing.dumps(payload, salt=OLD_SALT)


# The old action name, the posted fields it bound, and whether it bound one row or the snapshot.
OLD_ACTIONS = {
    "install": ("install_module", ("module_bay_id", "module_type_id"), True),
    "branch": ("install_branch", (), False),
    "selected": ("install_selected", (), False),
    "serial": ("update_module_serial", ("module_id",), True),
    "interface": ("update_module_interface", ("module_id",), True),
    "replace": ("replace_module", ("module_id",), True),
}


# --- The six writers -------------------------------------------------------------------------------


def _install_form(client, device, entities):
    return rendered_module_form(render(client, device), "install-module", REAL_NIM)


def _branch_form(client, device, entities):
    return rendered_module_form(render(client, device), "install-branch", FRONT_PANEL)


def _selected_form(client, device, entities):
    action, fields = rendered_module_form(render(client, device), "install-selected")
    return action, {**fields, "select": [str(REAL_NIM)]}


def _serial_form(client, device, entities):
    install_module(
        device, entities[entities[REAL_NIM]["entPhysicalContainedIn"]]["entPhysicalName"], *_nim(entities, "OLD")
    )
    return rendered_module_form(render(client, device), "update-module-serial", REAL_NIM)


def _interface_form(client, device, entities):
    from dcim.models import Interface, InterfaceTemplate, ModuleType

    model, serial = _nim(entities)
    InterfaceTemplate.objects.create(
        module_type=ModuleType.objects.get(model=model), name="grant-uplink", type="1000base-t"
    )
    module = install_module(
        device, entities[entities[REAL_NIM]["entPhysicalContainedIn"]]["entPhysicalName"], model, serial
    )
    # A standalone interface of the template name is what Update Interface adopts.
    Interface.objects.filter(module=module).update(module=None)
    return rendered_module_form(render(client, device), "update-module-interface", REAL_NIM)


def _replace_form(client, device, entities):
    model, serial = _nim(entities, "OLD")
    install_module(
        device,
        entities[entities[REAL_NIM]["entPhysicalContainedIn"]]["entPhysicalName"],
        entities[SERVICE_MODULE]["entPhysicalModelName"],
        serial,
    )
    preview = client.get(rendered_preview_url(render(client, device), REAL_NIM), HTTP_HX_REQUEST="true")
    assert preview.status_code == 200, preview.content
    return rendered_module_form(preview.content.decode(), "replace-module", REAL_NIM)


def _nim(entities, serial=None):
    return entities[REAL_NIM]["entPhysicalModelName"], serial or entities[REAL_NIM]["entPhysicalSerialNum"]


WRITERS = {
    "install": _install_form,
    "branch": _branch_form,
    "selected": _selected_form,
    "serial": _serial_form,
    "interface": _interface_form,
    "replace": _replace_form,
}


def _mutate_rule(device, entities, form, writer):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    InventoryIgnoreRule.objects.create(
        name="Unrelated skip", match_type="contains", pattern="no-such-entity", action="skip"
    )
    return "Inventory rules or device identity changed. Refresh Modules and try again."


def _mutate_serial(device, entities, form, writer):
    from dcim.models import Device

    Device.objects.filter(pk=device.pk).update(serial="NEW-CHASSIS-SERIAL")
    return "Inventory rules or device identity changed. Refresh Modules and try again."


def _mutate_ancestor(device, entities, form, writer):
    # Only an ancestor of the acted-on rows changes: the chassis above every row.
    edit_cached_row(device, 1, entPhysicalDescr="replaced chassis description")
    return "The LibreNMS inventory changed since the Modules table was rendered. Refresh Modules and try again."


def _mutate_old_token(device, entities, form, writer):
    _action, fields = form
    fields["inventory_binding"] = old_format_token(device, writer, fields)
    return "Inventory action is stale or does not match this row. Refresh Modules and try again."


MUTATIONS = {
    "rule-added": _mutate_rule,
    "device-serial": _mutate_serial,
    "ancestor-row": _mutate_ancestor,
    "old-token": _mutate_old_token,
}


def _changed(before, after):
    return before != after


@pytest.mark.parametrize("writer", list(WRITERS))
def test_each_writer_acts_on_the_state_the_table_rendered(ui, c8300, writer):
    device, entities = c8300
    action, fields = WRITERS[writer](ui, device, entities)
    before = written_state(device)

    response = post(ui, action, fields)

    assert response.status_code == 302
    assert _changed(before, written_state(device)), messages_of(response)


@pytest.mark.parametrize("mutation", list(MUTATIONS))
@pytest.mark.parametrize("writer", list(WRITERS))
def test_each_writer_refuses_a_changed_input_before_any_write(ui, c8300, writer, mutation):
    device, entities = c8300
    form = WRITERS[writer](ui, device, entities)
    expected = MUTATIONS[mutation](device, entities, form, writer)
    before = written_state(device)

    response = post(ui, *form)

    assert expected in messages_of(response)
    assert written_state(device) == before


# --- Rows the table does not render ----------------------------------------------------------------

BUILT_IN_UNIT = 1000  # module 0: the seeded model rule makes it transparent, so the table hides it.


def test_a_selected_install_naming_a_hidden_row_refuses_every_row(ui, c8300):
    device, entities = c8300
    action, fields = _selected_form(ui, device, entities)
    before = written_state(device)

    response = post(ui, action, {**fields, "select": [str(REAL_NIM), str(BUILT_IN_UNIT)]})

    assert any(
        text.startswith("Nothing was installed: the Modules table does not offer") for text in messages_of(response)
    )
    assert written_state(device) == before


def test_a_mismatch_preview_of_a_hidden_row_signs_nothing(ui, c8300):
    device, entities = c8300
    install_module(
        device, entities[entities[REAL_NIM]["entPhysicalContainedIn"]]["entPhysicalName"], *_nim(entities, "OLD")
    )
    url = rendered_preview_url(render(ui, device), REAL_NIM)
    query = parse_qs(urlsplit(url).query)
    query["ent_index"] = [str(BUILT_IN_UNIT)]

    response = ui.get(urlsplit(url).path, {key: values[0] for key, values in query.items()}, HTTP_HX_REQUEST="true")

    assert response.status_code == 400
    assert "inventory_binding" not in response.content.decode()


def test_an_invalid_destination_refuses_instead_of_using_the_page_device(ui, c8300):
    device, entities = c8300
    action, fields = _install_form(ui, device, entities)
    before = written_state(device)

    response = post(ui, action, {**fields, "selected_device_id": "not-a-device"})

    assert "Inventory destination changed. Refresh Modules and try again." in messages_of(response)
    assert written_state(device) == before


# --- Rules that do not work, and an ambiguous snapshot ---------------------------------------------


def _break_primary_regex():
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    rule = InventoryIgnoreRule.objects.create(
        name="Broken regex", match_type="regex", pattern="^nothing$", action="skip", require_serial_match_parent=False
    )
    InventoryIgnoreRule.objects.filter(pk=rule.pk).update(pattern="[")
    return rule, "pattern"


def _break_serial_replacement():
    from netbox_librenms_plugin.models import NormalizationRule

    rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N (\S+)$", replacement=r"\1")
    NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"\2")
    return rule, "replacement"


BREAKAGES = {"primary-regex": _break_primary_regex, "serial-replacement": _break_serial_replacement}


@pytest.mark.parametrize("breakage", list(BREAKAGES))
def test_a_rule_that_does_not_work_renders_no_table_and_no_grant(ui, c8300, breakage):
    device, _entities = c8300
    rule, field = BREAKAGES[breakage]()

    content = render(ui, device)

    assert 'id="module-inventory-rule-error"' in content
    assert rule.get_absolute_url() in content and f"<code>{field}</code>" in content
    assert "inventory_binding" not in content


def test_a_broken_optic_serial_rule_discards_the_old_snapshot_and_names_the_rule(ui, c8300):
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, SyncTabState

    device, _entities = c8300
    render(ui, device)
    assert cache.get(cache_key(device)) is not None
    rule, field = _break_serial_replacement()

    content = render(ui, device)

    assert 'id="module-inventory-rule-error"' in content
    assert rule.get_absolute_url() in content and f"<code>{field}</code>" in content
    assert "inventory_binding" not in content
    assert "Inventory data refreshed successfully." not in content
    assert cache.get(cache_key(device)) is None
    state = cache.get(SyncCacheConsistency(device).state_key(SyncTab.MODULES, SERVER_KEY))
    assert state["state"] == SyncTabState.REFRESH_FAILED.value


@pytest.mark.parametrize("breakage", list(BREAKAGES))
@pytest.mark.parametrize("writer", list(WRITERS))
def test_a_rule_broken_after_render_refuses_every_writer(ui, c8300, writer, breakage):
    device, entities = c8300
    form = WRITERS[writer](ui, device, entities)
    BREAKAGES[breakage]()
    before = written_state(device)

    response = post(ui, *form)

    assert any(
        text.startswith("An Inventory Ignore Rule or a serial Normalization Rule") for text in messages_of(response)
    )
    assert written_state(device) == before


@pytest.fixture
def ambiguous_c8300(settings, recording_server):
    """The C8300 recording with one sensor index reported twice."""
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

    recording = _recording()
    inventory = recording["responses"][f"GET /api/v0/inventory/{recording['device_id']}/all"]["inventory"]
    inventory.append({**next(item for item in inventory if item["entPhysicalIndex"] == 4), "entPhysicalName": "twin"})
    server, _api = recording_server(recording, server_key=SERVER_KEY)
    configure_librenms_servers(
        settings, {SERVER_KEY: {"librenms_url": server.url, "api_token": "grants-token", "verify_ssl": False}}
    )
    device = c8300_device("c8300-ambiguous", recording, SERVER_KEY)
    contrib_rule(MODEL_RULE)
    return device


def test_a_duplicate_index_in_the_snapshot_issues_no_grant(ui, ambiguous_c8300):
    content = render(ui, ambiguous_c8300)
    parsed = module_tab_forms(content)

    assert 'id="module-inventory-index-ambiguous"' in content
    assert all(not form["fields"].get("inventory_binding") for form in parsed.forms)
    assert not any("module-mismatch-preview" in link.get("hx-get", "") for link in parsed.links)
    assert 'id="install-selected-btn" disabled' in content


def test_the_table_reads_the_rules_and_serial_rules_once_per_render(ui, vc_grants):
    """The collector and the fingerprint share one policy load: one query per rule table."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    page, _member = vc_grants()
    with CaptureQueriesContext(connection) as captured:
        render(ui, page)

    statements = [query["sql"] for query in captured.captured_queries]
    rule_reads = [sql for sql in statements if 'FROM "netbox_librenms_plugin_inventoryignorerule"' in sql]
    serial_reads = [
        sql for sql in statements if 'FROM "netbox_librenms_plugin_normalizationrule"' in sql and "'serial'" in sql
    ]
    assert len(rule_reads) == 1, rule_reads
    assert len(serial_reads) == 1, serial_reads


# --- A Virtual Chassis: verify, selection overrides and the J1 shape -------------------------------

VC_LIBRENMS_ID = 4242
CARRIER, OPTIC_1, OPTIC_2, CARD = 10, 21, 23, 30


def _vc_rows(page_serial, member_serial, *, optic_1_serial="SN-O1"):
    def module(index, parent, name, model, serial):
        return {
            "entPhysicalIndex": index,
            "entPhysicalContainedIn": parent,
            "entPhysicalClass": "module",
            "entPhysicalName": name,
            "entPhysicalDescr": name,
            "entPhysicalModelName": model,
            "entPhysicalSerialNum": serial,
        }

    def container(index, parent, name):
        return {
            "entPhysicalIndex": index,
            "entPhysicalContainedIn": parent,
            "entPhysicalClass": "container",
            "entPhysicalName": name,
            "entPhysicalSerialNum": "",
        }

    return [
        {
            "entPhysicalIndex": 1,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "chassis",
            "entPhysicalName": "Chassis A",
            "entPhysicalSerialNum": page_serial,
        },
        {
            "entPhysicalIndex": 5,
            "entPhysicalContainedIn": 0,
            "entPhysicalClass": "chassis",
            "entPhysicalName": "Chassis B",
            "entPhysicalSerialNum": member_serial,
        },
        module(CARRIER, 1, "Carrier Slot", "GR-CARRIER", "SN-CARRIER"),
        container(20, CARRIER, "Cage 1"),
        module(OPTIC_1, 20, "Optic 1", "GR-OPTIC", optic_1_serial),
        container(22, CARRIER, "Cage 2"),
        module(OPTIC_2, 22, "Optic 2", "GR-OPTIC", "SN-O2"),
        module(CARD, 1, "Card Slot", "GR-CARD", "SN-CARD"),
    ]


@pytest.fixture
def vc_grants(settings, recording_server):
    """Return a builder of a two-member Virtual Chassis of two vendors, refreshed from a recording server."""
    from dcim.models import Manufacturer

    from netbox_librenms_plugin.tests.conftest import (
        configure_librenms_servers,
        make_device_with_module_bays,
        make_module_type_with_bays,
        make_virtual_chassis,
        map_device_to_librenms,
    )

    def build(*, optic_1_on_member=False):
        member_vendor = Manufacturer.objects.create(name="Grants Member Vendor", slug="grants-member-vendor")
        bays = ["Carrier Slot", "Cage 1", "Cage 2", "Card Slot"]
        page = make_device_with_module_bays("vc-grants-page", bays, serial="VC-A")
        member = make_device_with_module_bays("vc-grants-member", bays, manufacturer=member_vendor, serial="VC-B")
        make_virtual_chassis("vc-grants", page, member)
        make_module_type_with_bays("GR-CARRIER", bay_names=["Cage 1", "Cage 2"])
        make_module_type_with_bays("GR-OPTIC")
        make_module_type_with_bays("GR-CARD")
        rows = _vc_rows(page.serial, member.serial, optic_1_serial=member.serial if optic_1_on_member else "SN-O1")
        recording = {
            "device_id": VC_LIBRENMS_ID,
            "responses": {
                f"GET /api/v0/inventory/{VC_LIBRENMS_ID}/all": {"status": "ok", "inventory": rows},
                f"GET /api/v0/devices/{VC_LIBRENMS_ID}/ports": {"status": "ok", "ports": []},
                f"GET /api/v0/devices/{VC_LIBRENMS_ID}/transceivers": {"status": "ok", "transceivers": []},
            },
        }
        server, _api = recording_server(recording, server_key=SERVER_KEY)
        configure_librenms_servers(
            settings, {SERVER_KEY: {"librenms_url": server.url, "api_token": "grants-token", "verify_ssl": False}}
        )
        map_device_to_librenms(page, VC_LIBRENMS_ID, server_key=SERVER_KEY)
        return page, member

    return build


def _rule(name, pattern, manufacturer):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return InventoryIgnoreRule.objects.create(
        name=name,
        match_type="contains",
        pattern=pattern,
        action="skip",
        manufacturer=manufacturer,
        require_serial_match_parent=False,
    )


def verify(client, content, page, member, index):
    """POST the verify request the member select of row *index* sends when the user picks *member*."""
    from django.urls import reverse

    (select,) = [attrs for attrs in module_tab_forms(content).selects if attrs.get("data-module") == str(index)]
    return client.post(
        reverse("plugins:netbox_librenms_plugin:verify_module"),
        data=json.dumps(
            {
                "device_id": member.pk,
                "page_device_id": page.pk,
                "ent_physical_index": index,
                "depth": 0,
                "server_key": SERVER_KEY,
                "verify_binding": select.get("data-verify-binding"),
            }
        ),
        content_type="application/json",
    )


def _installed(device):
    from dcim.models import Module

    return set(Module.objects.filter(device=device).values_list("module_bay__name", "module_type__model"))


def test_a_verified_branch_installs_only_rows_both_renders_show(ui, vc_grants):
    """The table pinned to the member shows Optic 2, which a page-vendor rule hides; the branch leaves it out."""
    page, member = vc_grants()
    _rule("Page vendor hides optic 2", "Optic 2", page.device_type.manufacturer)
    content = render(ui, page)

    response = verify(ui, content, page, member, CARRIER)

    assert response.status_code == 200, response.content
    actions = response.json()["formatted_row"]["actions"]
    assert f"Install Branch on {member.name}" in actions
    action, fields = rendered_module_form(actions, "install-branch", CARRIER)
    assert fields["selected_device_id"] == str(member.pk)
    post(ui, action, fields)
    assert _installed(member) == {("Carrier Slot", "GR-CARRIER"), ("Cage 1", "GR-OPTIC")}
    assert _installed(page) == set()


def test_verify_issues_no_grant_for_a_row_the_member_table_hides(ui, vc_grants):
    page, member = vc_grants()
    _rule("Member vendor hides the card", "Card Slot", member.device_type.manufacturer)
    content = render(ui, page)

    response = verify(ui, content, page, member, CARD)

    assert response.status_code == 404
    assert "inventory_binding" not in response.content.decode()


def test_verify_builds_one_table_per_request(ui, vc_grants, monkeypatch):
    """The page table signed the verify grant, so verify builds only the table pinned to the member."""
    from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

    page, member = vc_grants()
    content = render(ui, page)
    build = BaseModuleTableView._build_context
    pinned_to = []

    def counted_build(self, *args, pin_to=None, **kwargs):
        pinned_to.append(getattr(pin_to, "pk", None))
        return build(self, *args, pin_to=pin_to, **kwargs)

    monkeypatch.setattr(BaseModuleTableView, "_build_context", counted_build)

    response = verify(ui, content, page, member, CARRIER)

    assert response.status_code == 200, response.content
    assert pinned_to == [member.pk]


def test_a_second_verify_keeps_the_rows_the_page_table_hides_out_of_the_branch(ui, vc_grants):
    """The verified row signs the page rows again, so a second member change still leaves Optic 2 out."""
    page, member = vc_grants()
    _rule("Page vendor hides optic 2", "Optic 2", page.device_type.manufacturer)
    first = verify(ui, render(ui, page), page, member, CARRIER).json()["formatted_row"]

    response = verify(ui, first["device_selection"], page, member, CARRIER)

    assert response.status_code == 200, response.content
    action, fields = rendered_module_form(response.json()["formatted_row"]["actions"], "install-branch", CARRIER)
    post(ui, action, fields)
    assert _installed(member) == {("Carrier Slot", "GR-CARRIER"), ("Cage 1", "GR-OPTIC")}


def test_verify_refuses_a_grant_of_another_snapshot(ui, vc_grants):
    from netbox_librenms_plugin.module_grants import GrantRefusal

    page, member = vc_grants()
    content = render(ui, page)
    edit_cached_row(page, OPTIC_2, entPhysicalSerialNum="SN-O2-REPLACED")

    response = verify(ui, content, page, member, CARRIER)

    assert response.status_code == 409
    assert response.json()["message"] == GrantRefusal.SNAPSHOT.message


def test_a_verify_grant_checks_its_state_like_every_other_grant(ui, vc_grants):
    """A verify row carries an index and no target, so the state check must not read a target from it."""
    from django.core.cache import cache

    from netbox_librenms_plugin.inventory_policy import load_inventory_policy
    from netbox_librenms_plugin.module_grants import ACTION_VERIFY_MODULE, read_module_grant

    page, _member = vc_grants()
    content = render(ui, page)
    (select,) = [attrs for attrs in module_tab_forms(content).selects if attrs.get("data-module") == str(CARRIER)]
    grant = read_module_grant(
        select["data-verify-binding"], action=ACTION_VERIFY_MODULE, page_device_id=page.pk, server_key=SERVER_KEY
    )
    assert grant.rows

    assert grant.check_state(cache.get(cache_key(page))["inventory"], load_inventory_policy(page.pk)) is grant


def test_a_selected_install_onto_another_member_needs_the_verified_override(ui, vc_grants):
    page, member = vc_grants()
    content = render(ui, page)
    action, fields = rendered_module_form(content, "install-selected")
    verified = verify(ui, content, page, member, CARD).json()["formatted_row"]
    grants = [
        link["value"] for link in module_tab_forms(verified["device_selection"]).links if "selection_grant" in link
    ]
    retarget = {**fields, "select": [str(CARD)], f"device_selection_{CARD}": str(member.pk)}

    refused = post(ui, action, retarget)
    assert any(text.startswith("Nothing was installed") for text in messages_of(refused))
    assert _installed(member) == set()
    forged = post(ui, action, {**retarget, f"selection_grant_{CARD}": fields["inventory_binding"]})
    assert any(text.startswith("Nothing was installed") for text in messages_of(forged))
    assert _installed(member) == set()

    assert grants and grants[0]
    post(ui, action, {**retarget, f"selection_grant_{CARD}": grants[0]})

    assert _installed(member) == {("Card Slot", "GR-CARD")}
    assert _installed(page) == set()


def test_a_selected_row_without_a_member_field_installs_onto_its_rendered_member(ui, vc_grants):
    page, member = vc_grants(optic_1_on_member=True)
    action, fields = rendered_module_form(render(ui, page), "install-selected")

    post(ui, action, {**fields, "select": [str(OPTIC_1)]})

    assert _installed(member) == {("Cage 1", "GR-OPTIC")}
    assert _installed(page) == set()


def test_a_child_attributed_to_another_member_installs_there_by_default(ui, vc_grants):
    """J1: the member-only rule names the parent, which the page device's rules judge."""
    page, member = vc_grants(optic_1_on_member=True)
    _rule("Member vendor hides carriers", "Carrier", member.device_type.manufacturer)
    content = render(ui, page)
    action, fields = rendered_module_form(content, "install-selected")

    # The browser posts the member the rendered select shows.
    post(ui, action, {**fields, "select": [str(OPTIC_1)], f"device_selection_{OPTIC_1}": str(member.pk)})

    assert _installed(member) == {("Cage 1", "GR-OPTIC")}
    assert _installed(page) == set()


def test_a_member_serial_change_after_render_refuses_the_member_row(ui, vc_grants):
    """The row belongs to the member by its serial, so a member serial edit changes the policy it was rendered under."""
    from dcim.models import Device

    page, member = vc_grants(optic_1_on_member=True)
    action, fields = rendered_module_form(render(ui, page), "install-selected")
    Device.objects.filter(pk=member.pk).update(serial="VC-B-REPLACED")

    response = post(ui, action, {**fields, "select": [str(OPTIC_1)], f"device_selection_{OPTIC_1}": str(member.pk)})

    assert "Inventory rules or device identity changed. Refresh Modules and try again." in messages_of(response)
    assert _installed(member) == set()


def test_a_grant_does_not_outlive_the_permission_it_was_rendered_for(client, c8300):
    """The grant authorizes the rendered row, never the writer: a revoked permission still refuses."""
    from dcim.models import Device, Interface, Module, ModuleBay, ModuleType
    from users.models import ObjectPermission

    from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

    device, entities = c8300
    perms = [
        ("view", Device),
        ("view", ModuleBay),
        ("view", ModuleType),
        ("add", Module),
        ("add", Interface),
        ("change", Interface),
        ("delete", Interface),
    ]
    client.force_login(make_user_with_perms("module-grants-revoked", perms))
    action, fields = _install_form(client, device, entities)
    ObjectPermission.objects.filter(name__startswith="module-grants-revoked-add-module").delete()
    before = written_state(device)

    response = post(client, action, fields)

    assert "Missing permissions: dcim.add_module" in messages_of(response)
    assert written_state(device) == before


def test_the_tab_parser_reads_a_form_the_way_a_browser_submits_it():
    html = """
    <form action="/install/">
      <input type="hidden" name="ent_index" value="7">
      <input type="checkbox" name="unchecked" value="1">
      <input type="checkbox" name="checked" checked>
      <input type="radio" name="kind" value="a">
      <input type="radio" name="kind" value="b" checked>
      <select name="first"><option value="x">X</option><option value="y">Y</option></select>
      <select name="picked"><option value="x">X</option><option value="y" selected>Y</option></select>
      <select name="text"><option selected> Bay 1 </option></select>
    </form>
    """

    (form,) = module_tab_forms(html).forms

    assert form["fields"] == {
        "ent_index": "7",
        "checked": "on",
        "kind": "b",
        "first": "x",
        "picked": "y",
        "text": "Bay 1",
    }
