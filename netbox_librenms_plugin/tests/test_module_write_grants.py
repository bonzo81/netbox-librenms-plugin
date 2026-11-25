"""
Module writers act only on the rows the Modules table rendered, under the policy it rendered them with.

Each test renders the real Modules tab over HTTP from a recording, takes the grant the rendered page
carries, changes one input, and posts the form. A writer must refuse a changed input before any write.
"""

import json
import re
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


@pytest.mark.parametrize("writer", list(WRITERS))
def test_each_writer_refuses_a_row_its_include_rule_no_longer_admits(ui, c8300, writer):
    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.module_grants import GrantRefusal

    device, entities = c8300
    form = WRITERS[writer](ui, device, entities)
    assert InventoryIgnoreRule.objects.filter(name="Inventory class: module").update(enabled=False) == 1
    assert not any(
        str(REAL_NIM) in (rendered["fields"].get("ent_index"), rendered["fields"].get("parent_index"))
        for rendered in module_tab_forms(render(ui, device)).forms
    )
    before = written_state(device)

    response = post(ui, *form)

    assert GrantRefusal.POLICY.message in messages_of(response)
    assert written_state(device) == before


def test_a_grant_signed_before_serial_extraction_refuses_the_write(ui, c8300, monkeypatch):
    """Version 1 signed grants under substitution serial rules; their member attribution is not current."""
    from netbox_librenms_plugin import inventory_policy
    from netbox_librenms_plugin.module_grants import GrantRefusal

    device, entities = c8300
    with monkeypatch.context() as signing:
        signing.setattr(inventory_policy, "INVENTORY_POLICY_VERSION", 1)
        form = WRITERS["install"](ui, device, entities)
    before = written_state(device)

    response = post(ui, *form)

    assert GrantRefusal.POLICY.message in messages_of(response)
    assert written_state(device) == before


# --- Rows the table does not render ----------------------------------------------------------------

BUILT_IN_UNIT = 1000  # module 0: the seeded model rule makes it transparent, so the table hides it.


@pytest.mark.parametrize("change", ["module-type", "module-bay", "module-serial", "replacement-type-mapping"])
def test_replace_refuses_a_module_changed_after_its_preview(ui, c8300, change):
    from dcim.models import Module, ModuleBay, ModuleType

    from netbox_librenms_plugin.models import ModuleTypeMapping
    from netbox_librenms_plugin.module_grants import GrantRefusal

    device, entities = c8300
    form = _replace_form(ui, device, entities)
    module = Module.objects.get(device=device, pk=form[1]["module_id"])
    if change == "module-type":
        Module.objects.filter(pk=module.pk).update(module_type=ModuleType.objects.get(model=_nim(entities)[0]))
    elif change == "module-serial":
        Module.objects.filter(pk=module.pk).update(serial="CHANGED-AFTER-PREVIEW")
    elif change == "replacement-type-mapping":
        # The preview resolved the row to the NIM type; a new mapping now resolves it to another type.
        other = ModuleType.objects.create(manufacturer=module.module_type.manufacturer, model="GRANT-OTHER")
        ModuleTypeMapping.objects.create(librenms_model=_nim(entities)[0], netbox_module_type=other)
    else:
        free_bay = (
            ModuleBay.objects.filter(device=device, installed_module__isnull=True)
            .exclude(pk=module.module_bay_id)
            .first()
        )
        Module.objects.filter(pk=module.pk).update(module_bay=free_bay)
    before = written_state(device)

    response = post(ui, *form)

    assert GrantRefusal.STALE.message in messages_of(response)
    assert written_state(device) == before


def test_replace_refuses_a_serial_conflict_that_appeared_after_its_preview(ui, c8300):
    """The preview showed no conflict, so the replace must not delete a module that holds the serial since."""
    from dcim.models import Module

    from netbox_librenms_plugin.module_grants import GrantRefusal
    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

    device, entities = c8300
    form = _replace_form(ui, device, entities)
    elsewhere_device = make_device_with_module_bays("c8300-grants-elsewhere", ["Slot 1"])
    elsewhere = install_in_bay(elsewhere_device, "Slot 1", "GRANT-ELSEWHERE-CARD", serial=_nim(entities)[1])
    before = written_state(device)

    response = post(ui, *form)

    assert GrantRefusal.STALE.message in messages_of(response)
    assert written_state(device) == before
    assert Module.objects.filter(pk=elsewhere.pk, device=elsewhere_device).exists()


def _unavailable_holder_lookup(monkeypatch):
    """Error injection: no rule context gives the Replace holder lookup this state today."""
    from netbox_librenms_plugin.serial_identity import UNAVAILABLE_MASTER_LINK, Resolution, ResolutionKind

    unavailable = Resolution(ResolutionKind.UNAVAILABLE, reason=UNAVAILABLE_MASTER_LINK)
    monkeypatch.setattr(
        "netbox_librenms_plugin.views.sync.modules._resolve_replacement_holder", lambda *args, **kwargs: unavailable
    )


@pytest.mark.parametrize("lookup", ["unavailable", "available"])
def test_replace_refuses_an_unavailable_serial_holder_lookup(ui, c8300, monkeypatch, lookup):
    """A lookup that cannot tell which module holds the serial must not let Replace delete and install."""
    from dcim.models import Module

    from netbox_librenms_plugin.views.sync.modules import REPLACE_SERIAL_UNAVAILABLE

    device, entities = c8300
    action, fields = _replace_form(ui, device, entities)
    installed = Module.objects.get(pk=fields["module_id"])
    before = written_state(device)
    if lookup == "unavailable":
        _unavailable_holder_lookup(monkeypatch)

    response = post(ui, action, fields)

    refused = REPLACE_SERIAL_UNAVAILABLE.format(serial=_nim(entities)[1]) in messages_of(response)
    assert refused is (lookup == "unavailable")
    if lookup == "unavailable":
        assert written_state(device) == before
    else:
        replacement = Module.objects.get(device=device, module_bay_id=installed.module_bay_id)
        assert replacement.pk != installed.pk
        assert replacement.serial == _nim(entities)[1]


@pytest.mark.parametrize("lookup", ["unavailable", "available"])
def test_the_preview_withholds_replace_for_an_unavailable_serial_holder_lookup(ui, c8300, monkeypatch, lookup):
    """The Replace POST refuses this state, so the dialog must not offer it."""
    device, entities = c8300
    install_module(device, _bay_name(entities, REAL_NIM), entities[SERVICE_MODULE]["entPhysicalModelName"], "OLD")
    table = render(ui, device)
    if lookup == "unavailable":
        _unavailable_holder_lookup(monkeypatch)

    preview = ui.get(rendered_preview_url(table, REAL_NIM), HTTP_HX_REQUEST="true")

    assert preview.status_code == 200, preview.content
    assert len(_replace_forms(preview.content.decode())) == (0 if lookup == "unavailable" else 1)


@pytest.mark.parametrize(
    ("reported_serial", "admitted"),
    [("TAG 12", False), ("TAG CARD123", True)],
    ids=["unadmitted", "admitted"],
)
def test_replace_only_removes_a_holder_when_the_target_admits_the_serial(
    ui, settings, librenms_server, reported_serial, admitted
):
    from dcim.models import Module

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import (
        configure_librenms_servers,
        install_module as install_in_bay,
        make_device_with_module_bays,
    )

    recording = _recording()
    entities = _entities(recording)
    entities[REAL_NIM]["entPhysicalSerialNum"] = reported_serial
    librenms_server.load_recording(recording)
    configure_librenms_servers(
        settings, {SERVER_KEY: {"librenms_url": librenms_server.url, "api_token": "grants-token", "verify_ssl": False}}
    )
    device = c8300_device("c8300-replace-serial", recording, SERVER_KEY)
    contrib_rule(MODEL_RULE)
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG (.+)$", replacement=r"\1", manufacturer=device.device_type.manufacturer
    )
    remote = make_device_with_module_bays("replace-serial-holder", ["Slot 1"])
    holder = install_in_bay(remote, "Slot 1", "REMOTE-CARD", serial=reported_serial, child_bays=["Child"])
    child = install_in_bay(remote, "Child", "REMOTE-CHILD", serial="CHILD123", parent_module=holder)

    action, fields = _replace_form(ui, device, entities)
    installed = Module.objects.get(pk=fields["module_id"])
    response = post(ui, action, fields)

    assert response.status_code == 302
    replacement = Module.objects.get(device=device, module_bay_id=installed.module_bay_id)
    assert replacement.pk != installed.pk
    assert replacement.module_type.model == _nim(entities)[0]
    remote_pks = {holder.pk, child.pk}
    remaining_pks = set(Module.objects.filter(pk__in=remote_pks).values_list("pk", flat=True))
    assert fields.get("conflict_module_id") == (str(holder.pk) if admitted else None)
    assert remaining_pks == (set() if admitted else remote_pks)
    assert replacement.serial == reported_serial


def _bay_name(entities, index):
    return entities[entities[index]["entPhysicalContainedIn"]]["entPhysicalName"]


def _tagged_holder(device, entities, *, owns_destination):
    """Install a card that stores "TAG <NIM serial>" under a rule that strips "TAG "; it can own the NIM bay."""
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG (.+)$", replacement=r"\1", manufacturer=device.device_type.manufacturer
    )
    holder = install_module(
        device,
        _bay_name(entities, SERVICE_MODULE),
        entities[SERVICE_MODULE]["entPhysicalModelName"],
        f"TAG {_nim(entities)[1]}",
    )
    if owns_destination:
        _own_the_nim_bay(device, entities, holder)
    return holder


def _own_the_nim_bay(device, entities, holder):
    from dcim.models import ModuleBay

    ModuleBay.objects.filter(device=device, name=_bay_name(entities, REAL_NIM)).update(module=holder)


def _replace_forms(html):
    return [form for form in module_tab_forms(html).forms if "replace-module" in form["action"]]


@pytest.mark.parametrize("owns_destination", [True, False], ids=["ancestor", "sibling"])
def test_the_preview_offers_replace_only_when_the_serial_holder_does_not_own_the_bay(ui, c8300, owns_destination):
    """Replace deletes the serial holder; a holder above the destination bay would delete that bay too."""
    device, entities = c8300
    holder = _tagged_holder(device, entities, owns_destination=owns_destination)
    install_module(device, _bay_name(entities, REAL_NIM), entities[SERVICE_MODULE]["entPhysicalModelName"], "OLD")

    preview = ui.get(rendered_preview_url(render(ui, device), REAL_NIM), HTTP_HX_REQUEST="true")

    assert preview.status_code == 200, preview.content
    html = preview.content.decode()
    forms = _replace_forms(html)
    if owns_destination:
        assert forms == []
        # Move would put the holder into a bay of its own subtree.
        assert [form for form in module_tab_forms(html).forms if "move-module" in form["action"]] == []
    else:
        assert [form["fields"].get("conflict_module_id") for form in forms] == [str(holder.pk)]


@pytest.mark.parametrize("owns_destination", [True, False], ids=["ancestor", "sibling"])
def test_the_table_offers_move_only_when_the_serial_holder_does_not_own_the_bay(ui, c8300, owns_destination):
    """The table offers the same Move as the preview: never into a bay of the holder's own subtree."""
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    holder = _tagged_holder(device, entities, owns_destination=owns_destination)

    holders = [form["fields"].get("conflict_module_id") for form in _move_forms(render(ui, device))]

    assert holders == ([] if owns_destination else [str(holder.pk)])


def test_the_table_offers_move_only_into_an_empty_bay(ui, c8300):
    """The table Move removes no occupant, so it always refuses for an occupied bay; the dialog covers that case."""
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    _tagged_holder(device, entities, owns_destination=False)
    install_module(device, _bay_name(entities, REAL_NIM), entities[SERVICE_MODULE]["entPhysicalModelName"], "OLD")

    assert _move_forms(render(ui, device)) == []


def test_replace_refuses_a_serial_holder_that_owns_the_bay_since_its_preview(ui, c8300):
    from dcim.models import Module

    from netbox_librenms_plugin.views.sync.modules import HOLDER_OWNS_BAY

    device, entities = c8300
    holder = _tagged_holder(device, entities, owns_destination=False)
    form = _replace_form(ui, device, entities)
    assert form[1]["conflict_module_id"] == str(holder.pk)
    _own_the_nim_bay(device, entities, holder)
    before = written_state(device)

    response = post(ui, *form)

    assert HOLDER_OWNS_BAY.format(serial=_nim(entities)[1]) in messages_of(response)
    assert written_state(device) == before
    assert Module.objects.filter(pk=holder.pk).exists()


@pytest.mark.parametrize("parent", ["holder", "installed"])
def test_replace_refuses_a_module_added_below_a_removal_tree_after_its_preview(ui, c8300, parent):
    """The preview showed the removal trees; a child added since would be deleted without confirmation."""
    from dcim.models import Module, ModuleBay

    from netbox_librenms_plugin.module_grants import GrantRefusal
    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

    device, entities = c8300
    remote = make_device_with_module_bays("replace-tree-holder", ["Slot 1"])
    holder = install_in_bay(remote, "Slot 1", "TREE-CARD", serial=_nim(entities)[1], child_bays=["Child"])
    form = _replace_form(ui, device, entities)
    assert form[1]["conflict_module_id"] == str(holder.pk)
    if parent == "holder":
        child = install_in_bay(remote, "Child", "TREE-CHILD", serial="TREE-CHILD-1", parent_module=holder)
    else:
        installed = Module.objects.get(pk=form[1]["module_id"])
        ModuleBay.objects.create(device=device, module=installed, name="Added child")
        child = install_in_bay(device, "Added child", "TREE-CHILD", serial="TREE-CHILD-1", parent_module=installed)
    before = written_state(device)

    response = post(ui, *form)

    assert GrantRefusal.STALE.message in messages_of(response)
    assert written_state(device) == before
    assert set(Module.objects.filter(pk__in=[holder.pk, child.pk]).values_list("pk", flat=True)) == {
        holder.pk,
        child.pk,
    }


@pytest.mark.parametrize("parent", ["holder", "installed"])
def test_replace_signs_the_removal_trees_that_its_preview_summarized(ui, c8300, parent, monkeypatch):
    """A child added between two preview reads of a removal tree must not enter the grant without the dialog."""
    from dcim.models import Module, ModuleBay

    from netbox_librenms_plugin.module_grants import GrantRefusal
    from netbox_librenms_plugin.serial_identity import module_subtree_serials
    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

    device, entities = c8300
    remote = make_device_with_module_bays("replace-tree-holder", ["Slot 1"])
    holder = install_in_bay(remote, "Slot 1", "TREE-CARD", serial=_nim(entities)[1], child_bays=["Child"])
    installed = install_module(
        device, _bay_name(entities, REAL_NIM), entities[SERVICE_MODULE]["entPhysicalModelName"], "OLD"
    )
    ModuleBay.objects.create(device=device, module=installed, name="Added child")
    root, bay_device, bay_name = (holder, remote, "Child") if parent == "holder" else (installed, device, "Added child")
    added = []

    def read_then_add_a_child(module_pk):
        tree = module_subtree_serials(module_pk)
        if module_pk == root.pk and not added:
            added.append(install_in_bay(bay_device, bay_name, "TREE-CHILD", serial="TREE-CHILD-1", parent_module=root))
        return tree

    preview_url = rendered_preview_url(render(ui, device), REAL_NIM)
    with monkeypatch.context() as reading:
        reading.setattr("netbox_librenms_plugin.views.sync.modules.module_subtree_serials", read_then_add_a_child)
        preview = ui.get(preview_url, HTTP_HX_REQUEST="true")
    assert preview.status_code == 200, preview.content
    form = rendered_module_form(preview.content.decode(), "replace-module", REAL_NIM)
    assert form[1]["conflict_module_id"] == str(holder.pk)
    [child] = added
    # The dialog summarized the same first reads that the grant signed: two roots, without the added child.
    assert re.findall(r'data-replace-removes="(\d+)"', preview.content.decode()) == ["2"]
    assert child.module_type.model not in preview.content.decode()
    before = written_state(device)

    response = post(ui, *form)

    assert GrantRefusal.STALE.message in messages_of(response)
    assert written_state(device) == before
    assert set(Module.objects.filter(pk__in=[holder.pk, installed.pk, child.pk]).values_list("pk", flat=True)) == {
        holder.pk,
        installed.pk,
        child.pk,
    }


def _remote_holder_with_child(device, entities, parent):
    """Hold the NIM serial on another device; install a child below the holder or below the module in the NIM bay."""
    from dcim.models import ModuleBay

    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays

    remote = make_device_with_module_bays("replace-tree-holder", ["Slot 1"])
    holder = install_in_bay(remote, "Slot 1", "TREE-CARD", serial=_nim(entities)[1], child_bays=["Child"])
    installed = install_module(
        device, _bay_name(entities, REAL_NIM), entities[SERVICE_MODULE]["entPhysicalModelName"], "OLD"
    )
    if parent == "holder":
        child = install_in_bay(remote, "Child", "TREE-CHILD", serial="TREE-CHILD-1", parent_module=holder)
    else:
        ModuleBay.objects.create(device=device, module=installed, name="Added child")
        child = install_in_bay(device, "Added child", "TREE-CHILD", serial="TREE-CHILD-1", parent_module=installed)
    return holder, installed, child


def _previewed_replace_form(client, device):
    preview = client.get(rendered_preview_url(render(client, device), REAL_NIM), HTTP_HX_REQUEST="true")
    assert preview.status_code == 200, preview.content
    return rendered_module_form(preview.content.decode(), "replace-module", REAL_NIM)


def _preview_html(client, device):
    preview = client.get(rendered_preview_url(render(client, device), REAL_NIM), HTTP_HX_REQUEST="true")
    assert preview.status_code == 200, preview.content
    return preview.content.decode()


def _move_forms(html):
    return [form for form in module_tab_forms(html).forms if "move-module" in form["action"]]


def _module_writer(name, *, device_scope=None, delete_scope=None, change_scope=None, view_scope=None):
    """Create a user with the Replace and Move permissions; a scope constrains Device views or Module grants."""
    from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    user = make_user_with_perms(name, [("view", Device)], constraints=device_scope)
    user = grant(user, "delete", Module, constraints=delete_scope, name=f"{name}-module-delete")
    user = grant(user, "view", Module, constraints=view_scope)
    for action, model in [("view", ModuleBay), ("view", ModuleType), ("add", Module)]:
        user = grant(user, action, model)
    user = grant(user, "change", Module, constraints=change_scope)
    for action, model in [("add", Interface), ("change", Interface), ("delete", Interface)]:
        user = grant(user, action, model)
    return user


def _module_places(*modules):
    from dcim.models import Module

    pks = [module.pk for module in modules]
    return dict(
        (pk, (device, bay))
        for pk, device, bay in Module.objects.filter(pk__in=pks).values_list("pk", "device_id", "module_bay_id")
    )


def test_the_replace_offer_counts_a_holder_inside_the_installed_module_once(ui, c8300):
    """The holder sits below the installed module, so both removal trees hold it; it is one module."""
    from dcim.models import ModuleBay

    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay

    device, entities = c8300
    installed = install_module(
        device, _bay_name(entities, REAL_NIM), entities[SERVICE_MODULE]["entPhysicalModelName"], "OLD"
    )
    ModuleBay.objects.create(device=device, module=installed, name="Holder bay")
    install_in_bay(device, "Holder bay", "INNER-CARD", serial=_nim(entities)[1], parent_module=installed)

    html = _preview_html(ui, device)

    assert len(_replace_forms(html)) == 1
    assert re.findall(r'data-replace-removes="(\d+)"', html) == ["2"]


@pytest.mark.parametrize("viewer", ["all", "roots-only"])
def test_the_replace_offer_states_how_many_modules_it_deletes(client, c8300, viewer):
    """Replace deletes the installed tree and the holder's tree; the dialog counts both, naming only visible ones."""
    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, "holder")
    view_scope = None if viewer == "all" else {"pk__in": [installed.pk, holder.pk]}
    client.force_login(_module_writer(f"replace-removes-{viewer}", view_scope=view_scope))

    html = _preview_html(client, device)

    assert len(_replace_forms(html)) == 1
    assert re.findall(r'data-replace-removes="(\d+)"', html) == ["3"]
    assert (child.module_type.model in html) is (viewer == "all")


@pytest.mark.parametrize("viewer", ["all", "occupant-only"])
def test_the_move_offer_states_how_many_modules_it_deletes(client, c8300, viewer):
    """Move deletes the occupant and its children; the dialog counts them and names only the visible ones."""
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, "installed")
    view_scope = None if viewer == "all" else {"pk__in": [installed.pk, holder.pk]}
    client.force_login(_module_writer(f"move-removes-{viewer}", view_scope=view_scope))

    html = _preview_html(client, device)

    assert len(_move_forms(html)) == 1
    assert re.findall(r'data-move-removes="(\d+)"', html) == ["2"]
    assert (child.module_type.model in html) is (viewer == "all")


def test_move_refuses_a_source_module_whose_serial_changed_after_its_preview(ui, c8300):
    """The dialog offered to move the module that held the serial; a module that stores another serial since is not it."""
    from dcim.models import Module

    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import MOVE_STALE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, "holder")
    Module.objects.filter(pk=child.pk).delete()
    form = _previewed_move_form(ui, device)
    Module.objects.filter(pk=holder.pk).update(serial="OTHER456")
    before, places = written_state(device), _module_places(holder)

    response = post(ui, *form)

    assert MOVE_STALE in messages_of(response)
    assert written_state(device) == before
    assert _module_places(holder) == places
    assert Module.objects.get(pk=holder.pk).serial == "OTHER456"


def test_the_table_move_refuses_a_source_module_whose_serial_changed_after_render(ui, c8300):
    """The table offered to move the module that held the serial into an empty bay; a changed source is not it."""
    from dcim.models import Module

    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import MOVE_STALE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    remote = make_device_with_module_bays("table-source-holder", ["Slot 1"])
    holder = install_in_bay(remote, "Slot 1", "TABLE-SOURCE-CARD", serial=_nim(entities)[1])
    (form,) = _move_forms(render(ui, device))
    Module.objects.filter(pk=holder.pk).update(serial="OTHER456")
    before, places = written_state(device), _module_places(holder)

    response = post(ui, form["action"], dict(form["fields"]))

    assert MOVE_STALE in messages_of(response)
    assert written_state(device) == before
    assert _module_places(holder) == places


def test_move_refuses_a_source_and_an_occupant_taken_from_two_offers(ui, c8300):
    """Each dialog signs one Move; the source of one offer and the occupant of another are no offer at all."""
    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import MOVE_STALE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    service_model = entities[SERVICE_MODULE]["entPhysicalModelName"]
    offers = {}
    for index in (REAL_NIM, SERVICE_MODULE):
        remote = make_device_with_module_bays(f"paired-offer-holder-{index}", ["Slot 1"])
        holder = install_in_bay(
            remote, "Slot 1", f"PAIRED-CARD-{index}", serial=entities[index]["entPhysicalSerialNum"]
        )
        occupant = install_module(device, _bay_name(entities, index), service_model, f"OLD-{index}")
        offers[index] = holder, occupant
    table = render(ui, device)
    forms = {}
    for index in offers:
        preview = ui.get(rendered_preview_url(table, index), HTTP_HX_REQUEST="true")
        (form,) = _move_forms(preview.content.decode())
        forms[index] = form
    # The occupant and bay of the second offer, with the source and every source-side field of the first.
    crafted = dict(forms[SERVICE_MODULE]["fields"])
    crafted["conflict_module_id"] = str(offers[REAL_NIM][0].pk)
    for name, value in forms[REAL_NIM]["fields"].items():
        if name.endswith("token") and name != "occupant_tree_token":
            crafted[name] = value
    modules = [module for pair in offers.values() for module in pair]
    before, places = written_state(device), _module_places(*modules)

    response = post(ui, forms[SERVICE_MODULE]["action"], crafted)

    assert MOVE_STALE in messages_of(response)
    assert written_state(device) == before
    assert _module_places(*modules) == places


@pytest.mark.parametrize("change", ["deleted", "moved"])
def test_move_refuses_an_occupant_that_left_the_bay_after_its_preview(ui, c8300, change):
    """The dialog signed an occupant; a bay that is empty since is not the state the operator confirmed."""
    from dcim.models import Module, ModuleBay

    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import MOVE_STALE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, "holder")
    Module.objects.filter(pk=child.pk).delete()
    form = _previewed_move_form(ui, device)
    if change == "deleted":
        Module.objects.filter(pk=installed.pk).delete()
    else:
        free = ModuleBay.objects.get(device=device, name=_bay_name(entities, SERVICE_MODULE))
        Module.objects.filter(pk=installed.pk).update(module_bay=free)
    # The helper leaves out a deleted row, so the deleted case still compares the holder alone.
    places = _module_places(holder, installed)

    response = post(ui, *form)

    assert MOVE_STALE in messages_of(response)
    assert _module_places(holder, installed) == places


def test_move_refuses_an_occupant_that_contains_the_holder(ui, c8300):
    """Move deletes the occupant first; an occupant above the holder would delete the module that Move keeps."""
    from dcim.models import ModuleBay
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.tests.view_test_helpers import move_offer_fields
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import MOVE_OCCUPANT_HOLDS_MODULE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    installed = install_module(
        device, _bay_name(entities, REAL_NIM), entities[SERVICE_MODULE]["entPhysicalModelName"], "OLD"
    )
    ModuleBay.objects.create(device=device, module=installed, name="Holder bay")
    holder = install_in_bay(device, "Holder bay", "INNER-CARD", serial=_nim(entities)[1], parent_module=installed)

    assert _move_forms(_preview_html(ui, device)) == []
    before = written_state(device)

    response = post(
        ui,
        reverse("plugins:netbox_librenms_plugin:move_module", args=[device.pk]),
        {
            "server_key": SERVER_KEY,
            "selected_device_id": str(device.pk),
            "module_id": str(installed.pk),
            "conflict_module_id": str(holder.pk),
            "target_bay_id": str(installed.module_bay_id),
            **move_offer_fields(installed, holder),
        },
    )

    assert MOVE_OCCUPANT_HOLDS_MODULE in messages_of(response)
    assert written_state(device) == before


def test_move_refuses_a_moved_subtree_with_a_module_outside_the_change_scope(client, c8300):
    """A cross-device Move changes the Device of every module below the holder, so the scope must cover them."""
    from users.models import ObjectPermission

    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import MOVE_OUTSIDE_CHANGE_SCOPE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, "holder")
    covering = {"pk__in": [installed.pk, holder.pk, child.pk]}
    client.force_login(_module_writer("move-change-subtree", change_scope=covering))
    form = _previewed_move_form(client, device)
    # The scope now misses the child below the holder.
    ObjectPermission.objects.filter(name__startswith="move-change-subtree-change-module").update(
        constraints={"pk__in": [installed.pk, holder.pk]}
    )
    assert _move_forms(_preview_html(client, device)) == []
    before, places = written_state(device), _module_places(holder, child, installed)

    response = post(client, *form)

    assert MOVE_OUTSIDE_CHANGE_SCOPE in messages_of(response)
    assert written_state(device) == before
    assert _module_places(holder, child, installed) == places


def test_move_refuses_a_holder_that_the_save_takes_outside_the_change_scope(client, c8300):
    """The change scope covers the holder on its source Device only; the saved row must still be in scope."""
    from dcim.models import Module

    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import MOVE_OUTSIDE_CHANGE_SCOPE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, "holder")
    Module.objects.filter(pk=child.pk).delete()
    client.force_login(_module_writer("move-change-place", change_scope={"device_id": holder.device_id}))
    form = _previewed_move_form(client, device)
    before, places = written_state(device), _module_places(holder, installed)

    response = post(client, *form)

    assert MOVE_OUTSIDE_CHANGE_SCOPE in messages_of(response)
    assert written_state(device) == before
    assert _module_places(holder, installed) == places


@pytest.mark.parametrize("scope", ["covers", "misses"])
@pytest.mark.parametrize("where", ["table", "dialog"])
def test_move_is_offered_only_when_the_change_scope_covers_the_holder(client, c8300, where, scope):
    """Move changes the holder, so the offer reads the change scope with the same queryset as the Move POST."""
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    if where == "table":
        holder = _tagged_holder(device, entities, owns_destination=False)
    else:
        holder, _installed, _child = _remote_holder_with_child(device, entities, "holder")
    change_scope = None if scope == "covers" else {"serial__in": ["OLD"]}
    client.force_login(_module_writer(f"move-change-{where}-{scope}", change_scope=change_scope))

    html = render(client, device) if where == "table" else _preview_html(client, device)

    holders = [form["fields"].get("conflict_module_id") for form in _move_forms(html)]
    assert holders == ([str(holder.pk)] if scope == "covers" else [])


def test_the_preview_names_no_holder_on_a_device_the_user_cannot_view(client, c8300):
    """The table names a holder only with view on its Device; the preview must not show its place either."""
    device, entities = c8300
    holder, _installed, _child = _remote_holder_with_child(device, entities, "holder")
    client.force_login(_module_writer("preview-hidden-device", device_scope={"pk": device.pk}))

    html = _preview_html(client, device)

    assert holder.device.name not in html
    assert _replace_forms(html) == []
    assert _move_forms(html) == []


def test_replace_refuses_a_holder_whose_device_the_user_can_no_longer_view(client, c8300):
    from dcim.models import Module
    from users.models import ObjectPermission

    from netbox_librenms_plugin.views.sync.modules import _SerialConflictUnavailable

    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, "holder")
    user = _module_writer("replace-hidden-device")
    client.force_login(user)
    form = _previewed_replace_form(client, device)
    assert form[1]["conflict_module_id"] == str(holder.pk)
    ObjectPermission.objects.filter(name__startswith="replace-hidden-device-view-device").update(
        constraints={"pk": device.pk}
    )
    before = written_state(device)

    response = post(client, *form)

    texts = messages_of(response)
    assert _SerialConflictUnavailable.MESSAGE.format(serial=_nim(entities)[1]) in texts
    assert holder.device.name not in " ".join(texts)
    assert written_state(device) == before
    kept = {holder.pk, installed.pk, child.pk}
    assert set(Module.objects.filter(pk__in=kept).values_list("pk", flat=True)) == kept


def _previewed_move_form(client, device):
    forms = _move_forms(_preview_html(client, device))
    assert len(forms) == 1, forms
    return forms[0]["action"], dict(forms[0]["fields"])


def test_move_refuses_an_occupant_tree_with_a_module_outside_the_delete_scope(client, c8300):
    """Move deletes the occupant of the destination bay and the modules below it, so the scope must cover them."""
    from dcim.models import Module
    from users.models import ObjectPermission

    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import MOVE_REMOVAL_OUTSIDE_DELETE_SCOPE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, "installed")
    roots = ["OLD", _nim(entities)[1]]
    client.force_login(_module_writer("move-delete-scope", delete_scope={"serial__in": [*roots, child.serial]}))
    form = _previewed_move_form(client, device)
    assert (form[1]["conflict_module_id"], form[1]["module_id"]) == (str(holder.pk), str(installed.pk))
    # The scope now misses the child below the occupant.
    ObjectPermission.objects.filter(name="move-delete-scope-module-delete").update(constraints={"serial__in": roots})
    assert _move_forms(_preview_html(client, device)) == []
    before = written_state(device)

    response = post(client, *form)

    assert MOVE_REMOVAL_OUTSIDE_DELETE_SCOPE in messages_of(response)
    assert written_state(device) == before
    kept = {holder.pk: holder.module_bay_id, installed.pk: installed.module_bay_id, child.pk: child.module_bay_id}
    assert dict(Module.objects.filter(pk__in=kept).values_list("pk", "module_bay_id")) == kept


def test_move_refuses_an_occupant_tree_changed_after_its_preview(ui, c8300):
    """The preview showed the occupant that Move deletes; a child added below it since must not be deleted."""
    from dcim.models import Module, ModuleBay

    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import MOVE_STALE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, "holder")
    Module.objects.filter(pk=child.pk).delete()
    form = _previewed_move_form(ui, device)
    assert (form[1]["conflict_module_id"], form[1]["module_id"]) == (str(holder.pk), str(installed.pk))
    ModuleBay.objects.create(device=device, module=installed, name="Added child")
    added = install_in_bay(device, "Added child", "TREE-CHILD", serial="TREE-CHILD-2", parent_module=installed)
    before = written_state(device)

    response = post(ui, *form)

    assert MOVE_STALE in messages_of(response)
    assert written_state(device) == before
    kept = {holder.pk: holder.module_bay_id, installed.pk: installed.module_bay_id, added.pk: added.module_bay_id}
    assert dict(Module.objects.filter(pk__in=kept).values_list("pk", "module_bay_id")) == kept


@pytest.mark.parametrize("parent", ["holder", "installed"])
def test_replace_refuses_a_removal_tree_with_a_module_outside_the_delete_scope(client, c8300, parent):
    """A scope that covers both trees offers Replace; one that misses a child offers none and refuses the POST."""
    from dcim.models import Device, Interface, Module, ModuleBay, ModuleType
    from users.models import ObjectPermission

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
    from netbox_librenms_plugin.views.sync.modules import REMOVAL_OUTSIDE_DELETE_SCOPE

    device, entities = c8300
    holder, installed, child = _remote_holder_with_child(device, entities, parent)
    perms = [("view", Device), ("view", ModuleBay), ("view", ModuleType), ("view", Module)]
    perms += [("add", Module), ("change", Module)]
    perms += [("add", Interface), ("change", Interface), ("delete", Interface)]
    user = make_user_with_perms("replace-delete-scope", perms)
    roots = ["OLD", _nim(entities)[1]]
    user = grant(user, "delete", Module, constraints={"serial__in": [*roots, child.serial]}, name="scoped-delete")
    client.force_login(user)
    html = _preview_html(client, device)
    form = rendered_module_form(html, "replace-module", REAL_NIM)
    assert form[1]["conflict_module_id"] == str(holder.pk)
    # The conflict alert says that Replace also removes the holder only while the dialog offers Replace.
    assert "data-replace-also-removes" in html
    # The scope now misses the child below one of the removal roots.
    ObjectPermission.objects.filter(name="scoped-delete").update(constraints={"serial__in": roots})
    html = _preview_html(client, device)
    assert _replace_forms(html) == []
    assert "data-replace-also-removes" not in html
    before = written_state(device)

    response = post(client, *form)

    assert REMOVAL_OUTSIDE_DELETE_SCOPE in messages_of(response)
    assert written_state(device) == before
    kept = {holder.pk, installed.pk, child.pk}
    assert set(Module.objects.filter(pk__in=kept).values_list("pk", flat=True)) == kept


def test_the_replace_grant_tokens_are_keyed_by_the_server_secret(client, settings, c8300):
    """The client can read a signed grant, so a token of module pks must not be computable from pks alone."""
    from django.core import signing

    from netbox_librenms_plugin.module_grants import _SALT
    from netbox_librenms_plugin.tests.conftest import make_superuser

    device, entities = c8300
    _remote_holder_with_child(device, entities, "holder")
    user = make_superuser("module-grants-keyed")
    targets = []
    for secret in ("first-test-secret-" * 4, "second-test-secret-" * 4):
        settings.SECRET_KEY = secret
        client.force_login(user)
        _action, fields = _previewed_replace_form(client, device)
        targets.append(signing.loads(fields["inventory_binding"], salt=_SALT)["action_target"])

    for name in ("serial_holder_token", "removal_trees_token"):
        assert targets[0][name] != targets[1][name], name


@pytest.mark.parametrize("depth", ["direct", "deep"])
def test_move_refuses_a_module_into_a_bay_of_its_own_subtree(ui, c8300, depth):
    """The holder owns the destination bay directly or through a child; the move refuses before any write."""
    from dcim.models import Module, ModuleBay
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.tests.view_test_helpers import move_offer_fields
    from netbox_librenms_plugin.views.sync.modules import MOVE_INTO_OWN_SUBTREE

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    holder = _tagged_holder(device, entities, owns_destination=False)
    owner = holder
    if depth == "deep":
        ModuleBay.objects.create(device=device, module=holder, name="Mid bay")
        owner = install_in_bay(device, "Mid bay", "MID-CARD", serial="MID-1", parent_module=holder)
    _own_the_nim_bay(device, entities, owner)
    installed = install_module(
        device, _bay_name(entities, REAL_NIM), entities[SERVICE_MODULE]["entPhysicalModelName"], "OLD"
    )
    before = written_state(device)

    response = post(
        ui,
        reverse("plugins:netbox_librenms_plugin:move_module", args=[device.pk]),
        {
            "server_key": SERVER_KEY,
            "selected_device_id": str(device.pk),
            "module_id": str(installed.pk),
            "conflict_module_id": str(holder.pk),
            "target_bay_id": str(installed.module_bay_id),
            **move_offer_fields(installed, holder),
        },
    )

    assert MOVE_INTO_OWN_SUBTREE in messages_of(response)
    assert written_state(device) == before
    assert Module.objects.filter(pk=installed.pk).exists()


def test_the_table_offers_move_only_for_a_tree_without_duplicate_destination_identities(ui, c8300):
    """One holder's tree holds one identity twice, so its Move refuses; the other row keeps its Move offer."""
    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device, entities = c8300
    first = make_device_with_module_bays("duplicate-tree-holder", ["Slot 1"])
    duplicate = install_in_bay(first, "Slot 1", "DUP-CARD", serial=_nim(entities)[1], child_bays=["C1", "C2"])
    for bay in ("C1", "C2"):
        install_in_bay(first, bay, "DUP-CHILD", serial="DUP-CHILD-1", parent_module=duplicate)
    second = make_device_with_module_bays("valid-tree-holder", ["Slot 1"])
    valid = install_in_bay(second, "Slot 1", "VALID-CARD", serial=entities[SERVICE_MODULE]["entPhysicalSerialNum"])

    holders = [form["fields"].get("conflict_module_id") for form in _move_forms(render(ui, device))]

    assert holders == [str(valid.pk)]


@pytest.mark.parametrize(("sibling_serial", "offered"), [("S/N ABC123", False), ("OTHER123", True)])
@pytest.mark.parametrize("where", ["dialog", "table"])
def test_move_is_not_offered_beside_a_legacy_holder_on_the_destination(
    ui, settings, librenms_server, where, sibling_serial, offered
):
    """An earlier release stored "S/N S/N ABC123" as "S/N ABC123"; the Move POST refuses beside such a row."""
    from dcim.models import ModuleBay

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers
    from netbox_librenms_plugin.tests.conftest import install_module as install_in_bay
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    NormalizationRule.objects.get_or_create(
        scope="serial", match_pattern=r"^S/N\s+(.+)$", manufacturer=None, defaults={"replacement": r"\1"}
    )
    recording = _recording()
    entities = _entities(recording)
    entities[REAL_NIM]["entPhysicalSerialNum"] = "S/N S/N ABC123"
    librenms_server.load_recording(recording)
    configure_librenms_servers(
        settings, {SERVER_KEY: {"librenms_url": librenms_server.url, "api_token": "grants-token", "verify_ssl": False}}
    )
    device = c8300_device("c8300-legacy-sibling", recording, SERVER_KEY)
    contrib_rule(MODEL_RULE)
    remote = make_device_with_module_bays("legacy-sibling-holder", ["Slot 1"])
    holder = install_in_bay(remote, "Slot 1", "LEGACY-CARD", serial="S/N S/N ABC123")
    service_model = entities[SERVICE_MODULE]["entPhysicalModelName"]
    install_module(device, _bay_name(entities, SERVICE_MODULE), service_model, sibling_serial)
    if where == "dialog":
        installed = install_module(device, _bay_name(entities, REAL_NIM), service_model, "OLD")
        html = _preview_html(ui, device)
    else:
        installed = None
        html = render(ui, device)

    holders = [form["fields"].get("conflict_module_id") for form in _move_forms(html)]

    assert holders == ([str(holder.pk)] if offered else [])
    if not offered:
        # The offer and the Move POST run one check, so a scripted Move refuses too.
        from django.urls import reverse

        from netbox_librenms_plugin.serial_identity import RELOCATION_HELD
        from netbox_librenms_plugin.tests.view_test_helpers import move_offer_fields

        fields = {"server_key": SERVER_KEY, "selected_device_id": str(device.pk), "conflict_module_id": str(holder.pk)}
        if installed is None:
            fields["target_bay_id"] = str(ModuleBay.objects.get(device=device, name=_bay_name(entities, REAL_NIM)).pk)
        else:
            fields.update(
                target_bay_id=str(installed.module_bay_id),
                module_id=str(installed.pk),
                **move_offer_fields(installed, holder),
            )
        before = written_state(device)
        response = post(ui, reverse("plugins:netbox_librenms_plugin:move_module", args=[device.pk]), fields)
        assert messages_of(response) == [f"Move failed: {RELOCATION_HELD}"]
        assert written_state(device) == before


@pytest.mark.parametrize(("reported_serial", "admitted"), [("TAG 12", False), ("TAG CARD123", True)])
def test_move_requires_a_serial_the_destination_admits(ui, settings, librenms_server, reported_serial, admitted):
    from dcim.models import ModuleBay
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import (
        configure_librenms_servers,
        install_module as install_in_bay,
        make_device_with_module_bays,
    )
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    recording = _recording()
    entities = _entities(recording)
    entities[REAL_NIM]["entPhysicalSerialNum"] = reported_serial
    librenms_server.load_recording(recording)
    configure_librenms_servers(
        settings, {SERVER_KEY: {"librenms_url": librenms_server.url, "api_token": "grants-token", "verify_ssl": False}}
    )
    device = c8300_device("c8300-move-serial", recording, SERVER_KEY)
    contrib_rule(MODEL_RULE)
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG (.+)$", replacement=r"\1", manufacturer=device.device_type.manufacturer
    )
    source = make_device_with_module_bays("move-serial-holder", ["Slot 1"])
    holder = install_in_bay(source, "Slot 1", "MOVE-CARD", serial=reported_serial, child_bays=["Child"])
    child = install_in_bay(source, "Child", "MOVE-CHILD", serial="CHILD123", parent_module=holder)
    source_bay_id = holder.module_bay_id
    bay_name = entities[entities[REAL_NIM]["entPhysicalContainedIn"]]["entPhysicalName"]
    target_bay = ModuleBay.objects.get(device=device, name=bay_name)
    url = reverse("plugins:netbox_librenms_plugin:move_module", args=[device.pk])
    forms = [form["fields"] for form in module_tab_forms(render(ui, device)).forms if form["action"] == url]
    fields = {
        "server_key": SERVER_KEY,
        "selected_device_id": str(device.pk),
        "conflict_module_id": str(holder.pk),
        "target_bay_id": str(target_bay.pk),
    }

    response = post(ui, url, forms[0] if admitted else fields)

    assert response.status_code == 302
    if not admitted:
        assert "The module serial has no identity at the destination." in messages_of(response)
    holder.refresh_from_db()
    child.refresh_from_db()
    expected = (True, device.pk, target_bay.pk, device.pk) if admitted else (False, source.pk, source_bay_id, source.pk)
    assert (bool(forms), holder.device_id, holder.module_bay_id, child.device_id) == expected


@pytest.mark.parametrize(("stored_serial", "offered"), [("TAG CARD123", False), ("CARD123", True)])
def test_the_table_offers_move_only_when_the_destination_rules_keep_the_identity(
    ui, settings, librenms_server, stored_serial, offered
):
    """The holder's own rules key "TAG CARD123" to the reported "CARD123"; the destination has no such rule."""
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import (
        configure_librenms_servers,
        install_module as install_in_bay,
        make_device_with_module_bays,
    )
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    recording = _recording()
    entities = _entities(recording)
    entities[REAL_NIM]["entPhysicalSerialNum"] = "CARD123"
    librenms_server.load_recording(recording)
    configure_librenms_servers(
        settings, {SERVER_KEY: {"librenms_url": librenms_server.url, "api_token": "grants-token", "verify_ssl": False}}
    )
    device = c8300_device("c8300-move-identity", recording, SERVER_KEY)
    contrib_rule(MODEL_RULE)
    source = make_device_with_module_bays("move-identity-holder", ["Slot 1"])
    holder = install_in_bay(source, "Slot 1", "MOVE-IDENTITY-CARD", serial=stored_serial)
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG (.+)$", replacement=r"\1", manufacturer=source.device_type.manufacturer
    )
    url = reverse("plugins:netbox_librenms_plugin:move_module", args=[device.pk])

    response = ui.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )
    rows = list(response.context["module_sync"]["table"].data)
    forms = [form["fields"] for form in module_tab_forms(response.content.decode()).forms if form["action"] == url]

    assert [row["serial_conflict_module"] for row in rows if row.get("serial_conflict_module")] == [holder]
    assert [fields["conflict_module_id"] for fields in forms] == ([str(holder.pk)] if offered else [])


@pytest.mark.parametrize(
    ("stored_serial", "destination_rule", "admitted"),
    [("TAG CARD12", True, False), ("CARD12", True, True), ("TAG CARD12", False, False)],
    ids=["no-destination-identity", "same-identity", "other-destination-identity"],
)
@pytest.mark.parametrize("netbox", ["installed", "below-4.7"])
def test_mismatch_preview_offers_move_only_when_the_destination_admits_the_holder(
    ui, settings, librenms_server, monkeypatch, stored_serial, destination_rule, admitted, netbox
):
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import (
        configure_librenms_servers,
        install_module as install_in_bay,
        make_device_with_module_bays,
    )
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    recording = _recording()
    entities = _entities(recording)
    entities[REAL_NIM]["entPhysicalSerialNum"] = "CARD12"
    librenms_server.load_recording(recording)
    configure_librenms_servers(
        settings, {SERVER_KEY: {"librenms_url": librenms_server.url, "api_token": "grants-token", "verify_ssl": False}}
    )
    device = c8300_device("c8300-preview-move", recording, SERVER_KEY)
    contrib_rule(MODEL_RULE)
    source = make_device_with_module_bays("preview-move-holder", ["Slot 1"])
    holder = install_in_bay(source, "Slot 1", "PREVIEW-MOVE-CARD", serial=stored_serial)
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG (.+)$", replacement=r"\1", manufacturer=source.device_type.manufacturer
    )
    if destination_rule:
        NormalizationRule.objects.create(
            scope="serial",
            match_pattern=r"^TAG CARD(\d+)$",
            replacement=r"\1",
            manufacturer=device.device_type.manufacturer,
        )
    bay_name = entities[entities[REAL_NIM]["entPhysicalContainedIn"]]["entPhysicalName"]
    install_module(device, bay_name, *_nim(entities, "OLD"))
    if netbox == "below-4.7":
        # The Move POST refuses every move below NetBox 4.7, so the preview must not offer one.
        monkeypatch.setattr("netbox_librenms_plugin.views.sync.modules.netbox_relocates_module_subtree", lambda: False)
    relocates = netbox == "installed" and netbox_relocates_module_subtree()

    preview = ui.get(rendered_preview_url(render(ui, device), REAL_NIM), HTTP_HX_REQUEST="true")

    assert preview.status_code == 200
    html = preview.content.decode()
    _, replace_fields = rendered_module_form(html, "replace-module", REAL_NIM)
    assert replace_fields["conflict_module_id"] == str(holder.pk)
    move_url = reverse("plugins:netbox_librenms_plugin:move_module", args=[device.pk])
    move_holders = [
        form["fields"]["conflict_module_id"] for form in module_tab_forms(html).forms if form["action"] == move_url
    ]
    assert move_holders == ([str(holder.pk)] if admitted and relocates else [])


def _preview_serial_form(client, device, entities):
    install_module(
        device, entities[entities[REAL_NIM]["entPhysicalContainedIn"]]["entPhysicalName"], *_nim(entities, "OLD")
    )
    preview = client.get(rendered_preview_url(render(client, device), REAL_NIM), HTTP_HX_REQUEST="true")
    assert preview.status_code == 200, preview.content
    return rendered_module_form(preview.content.decode(), "update-module-serial", REAL_NIM)


MODULE_WRITERS = {"serial": _serial_form, "interface": _interface_form, "preview-serial": _preview_serial_form}


@pytest.mark.parametrize("writer", list(MODULE_WRITERS))
def test_a_module_write_refuses_a_module_moved_to_another_bay_after_render(ui, c8300, writer):
    """The row matched the module in its bay, so the module in another bay is not the row's module."""
    from dcim.models import Module, ModuleBay

    from netbox_librenms_plugin.module_grants import GrantRefusal

    device, entities = c8300
    form = MODULE_WRITERS[writer](ui, device, entities)
    module = Module.objects.get(device=device, pk=form[1]["module_id"])
    free_bay = (
        ModuleBay.objects.filter(device=device, installed_module__isnull=True).exclude(pk=module.module_bay_id).first()
    )
    Module.objects.filter(pk=module.pk).update(module_bay=free_bay)
    before = written_state(device)

    response = post(ui, *form)

    assert GrantRefusal.STALE.message in messages_of(response)
    assert written_state(device) == before


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
    from netbox_librenms_plugin.module_grants import GrantRefusal

    device, entities = c8300
    form = WRITERS[writer](ui, device, entities)
    BREAKAGES[breakage]()
    before = written_state(device)

    response = post(ui, *form)

    assert GrantRefusal.RULE_ERROR.message in messages_of(response)
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


def _vc_rows(page_serial, member_serial, *, optic_1_serial="SN-O1X1", card=None):
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
        module(OPTIC_2, 22, "Optic 2", "GR-OPTIC", "SN-O2X2"),
        {**module(CARD, 1, "Card Slot", "GR-CARD", "SN-CARD"), **(card or {})},
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

    def build(*, optic_1_on_member=False, card=None, extra_rows=(), rows=None):
        member_vendor = Manufacturer.objects.create(name="Grants Member Vendor", slug="grants-member-vendor")
        bays = ["Carrier Slot", "Cage 1", "Cage 2", "Card Slot"]
        page = make_device_with_module_bays("vc-grants-page", bays, serial="VCA-0001")
        member = make_device_with_module_bays("vc-grants-member", bays, manufacturer=member_vendor, serial="VCB-0001")
        make_virtual_chassis("vc-grants", page, member)
        make_module_type_with_bays("GR-CARRIER", bay_names=["Cage 1", "Cage 2"])
        make_module_type_with_bays("GR-OPTIC")
        make_module_type_with_bays("GR-CARD")
        rows = rows or _vc_rows(
            page.serial, member.serial, optic_1_serial=member.serial if optic_1_on_member else "SN-O1X1", card=card
        ) + list(extra_rows)
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


def _row_forms(parsed, index):
    """Return the rendered forms that act on inventory row *index*, read from their own row index field."""
    return [
        form
        for form in parsed.forms
        if str(index) in (form["fields"].get("ent_index"), form["fields"].get("parent_index"))
    ]


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


def test_a_branch_names_a_member_row_bay_from_a_container_on_the_page(ui, vc_grants):
    """The page owns the Cage 1 container, but the member optic still takes its bay name from it, as the table does."""
    page, member = vc_grants(optic_1_on_member=True)
    action, fields = rendered_module_form(render(ui, page), "install-branch", CARRIER)

    post(ui, action, fields)

    assert ("Cage 1", "GR-OPTIC") in _installed(member)


def test_a_member_serial_change_after_render_refuses_the_member_row(ui, vc_grants):
    """The row belongs to the member by its serial, so a member serial edit changes the policy it was rendered under."""
    from dcim.models import Device

    page, member = vc_grants(optic_1_on_member=True)
    action, fields = rendered_module_form(render(ui, page), "install-selected")
    Device.objects.filter(pk=member.pk).update(serial="VC-B-REPLACED")

    response = post(ui, action, {**fields, "select": [str(OPTIC_1)], f"device_selection_{OPTIC_1}": str(member.pk)})

    assert "Inventory rules or device identity changed. Refresh Modules and try again." in messages_of(response)
    assert _installed(member) == set()


def _stack_rows(positions, serials=("AB-12", "CD-34")):
    """Return a stack root with chassis members 2, 3, ... that report *serials*; card CARD + n sits in member n + 2."""

    def row(index, parent, entity_class, name, serial, **fields):
        return {
            "entPhysicalIndex": index,
            "entPhysicalContainedIn": parent,
            "entPhysicalClass": entity_class,
            "entPhysicalName": name,
            "entPhysicalDescr": name,
            "entPhysicalSerialNum": serial,
            **fields,
        }

    pairs = list(enumerate(zip(serials, positions, strict=True)))
    members = [
        row(2 + n, 1, "chassis", f"Switch {2 + n}", serial, entPhysicalParentRelPos=position)
        for n, (serial, position) in pairs
    ]
    cards = [
        row(CARD + n, 2 + n, "module", "Card Slot", f"SN-CARD-{n + 1}", entPhysicalModelName="GR-CARD")
        for n, _member in pairs
    ]
    return [row(1, 0, "stack", "Stack", ""), *members, *cards]


@pytest.mark.parametrize(
    ("positions", "withheld"),
    [((-1, -1), True), ((1, 2), False), ((2, 1), False)],
    ids=["row-order", "reported", "reported-reversed"],
)
@pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
def test_a_row_order_position_attributes_no_stack_member_with_a_short_serial(
    ui, vc_grants, positions, withheld, reverse
):
    """Row order is no identity of "AB-12" or "CD-34", so neither card is offered on any member in either row order."""
    rows = _stack_rows(positions)
    if reverse:
        rows[1:3] = rows[2:0:-1]
    page, member = vc_grants(rows=rows)
    content = render(ui, page)
    parsed = module_tab_forms(content)

    for index in (CARD, CARD + 1):
        (row,) = re.findall(rf'<tr[^>]*data-ent-index="{index}".*?</tr>', content, flags=re.S)
        assert ("data-member-unattributed" in row) is withheld
        assert bool(_row_forms(parsed, index)) is (not withheld)
    if not withheld:
        on_page = CARD if positions[0] == 1 else CARD + 1
        _action, fields = rendered_module_form(content, "install-module", on_page)
        assert fields["selected_device_id"] == str(page.pk)


@pytest.mark.parametrize(
    ("second_serial", "withheld"), [("NEW123", True), ("", False)], ids=["unowned-serial", "blank"]
)
@pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
def test_a_row_order_position_attributes_no_stack_member_with_a_serial_no_member_owns(
    ui, vc_grants, second_serial, withheld, reverse
):
    """Switch 2 reports the member's serial; row order names no member for Switch 3's unowned "NEW123".

    A blank Switch 3 takes position 1 in reversed order, the page device, which Switch 2 does not own.
    """
    rows = _stack_rows((-1, -1), serials=("VCB-0001", second_serial))
    if reverse:
        rows[1:3] = rows[2:0:-1]
    page, member = vc_grants(rows=rows)
    content = render(ui, page)
    parsed = module_tab_forms(content)
    # In order, a blank Switch 3 takes position 2, the member that Switch 2 owns by serial.
    withheld = withheld or not reverse

    (owned,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)
    (row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD + 1}".*?</tr>', content, flags=re.S)
    assert "data-member-unattributed" not in owned
    assert f'<option value="{member.pk}" selected>' in owned
    assert ("data-member-unattributed" in row) is withheld
    assert bool(_row_forms(parsed, CARD + 1)) is (not withheld)


@pytest.mark.parametrize(("fpc_serial", "withheld"), [("12", True), ("VCB-0001", False)], ids=["short", "identity"])
def test_a_card_below_a_junos_fpc_with_a_short_serial_is_offered_on_no_member(ui, vc_grants, fpc_serial, withheld):
    """FPC 1 reports "12", so the card below it does not follow the root's master serial onto the page device."""

    def row(index, parent, entity_class, descr, serial, **fields):
        return {
            "entPhysicalIndex": index,
            "entPhysicalContainedIn": parent,
            "entPhysicalClass": entity_class,
            "entPhysicalName": descr,
            "entPhysicalDescr": descr,
            "entPhysicalSerialNum": serial,
            **fields,
        }

    rows = [
        row(1, 0, "chassis", "Juniper Virtual Chassis Switch", "VCA-0001"),
        row(120, 1, "container", "FPC: EX4400-24X @ 0/*/*", "VCA-0001", entPhysicalParentRelPos=0),
        row(121, 1, "container", "FPC: EX4400-24X @ 1/*/*", fpc_serial, entPhysicalParentRelPos=1),
        row(CARD, 121, "module", "Card Slot", "SN-CARD-1", entPhysicalModelName="GR-CARD"),
    ]
    page, member = vc_grants(rows=rows)
    content = render(ui, page)
    parsed = module_tab_forms(content)

    (card,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)
    assert ("data-member-unattributed" in card) is withheld
    assert (f'<option value="{member.pk}" selected>' in card) is (not withheld)
    assert f'<option value="{page.pk}" selected>' not in card
    assert bool(_row_forms(parsed, CARD)) is (not withheld)


@pytest.mark.parametrize(("optic_serials", "branch"), [(("12", "34"), False), (("SN-O1X1", "SN-O2X2"), True)])
def test_a_carrier_whose_typed_children_no_member_reported_offers_no_install_branch(
    ui, vc_grants, optic_serials, branch
):
    """The optics report "12" and "34", so no member owns them; the carrier offers its own Install only."""
    rows = _vc_rows("VCA-0001", "VCB-0001", optic_1_serial=optic_serials[0])
    rows = [
        {**row, "entPhysicalSerialNum": optic_serials[1]} if row["entPhysicalIndex"] == OPTIC_2 else row for row in rows
    ]
    page, _member = vc_grants(rows=rows)
    content = render(ui, page)
    parsed = module_tab_forms(content)

    (carrier,) = re.findall(rf'<tr[^>]*data-ent-index="{CARRIER}".*?</tr>', content, flags=re.S)
    for optic in (OPTIC_1, OPTIC_2):
        (row,) = re.findall(rf'<tr[^>]*data-ent-index="{optic}".*?</tr>', content, flags=re.S)
        assert ("data-member-unattributed" in row) is (not branch)
    assert 'data-action="install"' in carrier
    assert ('data-action="install-branch"' in carrier) is branch
    assert bool([form for form in _row_forms(parsed, CARRIER) if "install-branch" in form["action"]]) is branch


@pytest.mark.parametrize("third_fpc", ["12", None], ids=["short-fpc-3", "no-fpc-3"])
def test_a_junos_row_follows_its_named_fpc_when_another_fpc_has_a_short_serial(ui, vc_grants, third_fpc):
    """FPC 3 reports "12", which turns off the stack shape; the card that names FPC 2 still follows FPC 2's member."""

    def row(index, entity_class, descr, serial, **fields):
        return {
            "entPhysicalIndex": index,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": entity_class,
            "entPhysicalName": descr,
            "entPhysicalDescr": descr,
            "entPhysicalSerialNum": serial,
            **fields,
        }

    rows = [
        {**row(1, "chassis", "Juniper Virtual Chassis Switch", "VCA-0001"), "entPhysicalContainedIn": 0},
        row(120, "container", "FPC: EX4400-24X @ 1/*/*", "VCA-0001", entPhysicalParentRelPos=1),
        row(121, "container", "FPC: EX4400-24X @ 2/*/*", "VCB-0001", entPhysicalParentRelPos=2),
        {
            **row(CARD, "module", "FPC 2 Card 0", "SN-CARD-1", entPhysicalModelName="GR-CARD"),
            "entPhysicalName": "Card Slot",
        },
    ]
    if third_fpc is not None:
        rows.append(row(122, "container", "FPC: EX4400-24X @ 3/*/*", third_fpc, entPhysicalParentRelPos=3))
    page, member = vc_grants(rows=rows)
    content = render(ui, page)
    parsed = module_tab_forms(content)

    (card,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)
    assert "data-member-unattributed" not in card
    assert f'<option value="{member.pk}" selected>' in card
    assert f'<option value="{page.pk}" selected>' not in card
    assert _row_forms(parsed, CARD)


@pytest.mark.parametrize(("fpc_serial", "withheld"), [("12", True), ("VCB-0001", False)], ids=["short", "identity"])
def test_a_junos_row_that_names_an_fpc_with_a_short_serial_is_offered_on_no_member(ui, vc_grants, fpc_serial, withheld):
    """The card names FPC 2, which reports "12", so it does not fall through to the root's serial of the page device."""

    def row(index, entity_class, descr, serial, **fields):
        return {
            "entPhysicalIndex": index,
            "entPhysicalContainedIn": 1,
            "entPhysicalClass": entity_class,
            "entPhysicalName": descr,
            "entPhysicalDescr": descr,
            "entPhysicalSerialNum": serial,
            **fields,
        }

    rows = [
        {**row(1, "chassis", "Juniper Virtual Chassis Switch", "VCA-0001"), "entPhysicalContainedIn": 0},
        row(120, "container", "FPC: EX4400-24X @ 1/*/*", "VCA-0001", entPhysicalParentRelPos=1),
        row(121, "container", "FPC: EX4400-24X @ 2/*/*", fpc_serial, entPhysicalParentRelPos=2),
        {
            **row(CARD, "module", "FPC 2 Card 0", "SN-CARD-1", entPhysicalModelName="GR-CARD"),
            "entPhysicalName": "Card Slot",
        },
    ]
    page, member = vc_grants(rows=rows)
    content = render(ui, page)
    parsed = module_tab_forms(content)

    (card,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)
    assert ("data-member-unattributed" in card) is withheld
    assert (f'<option value="{member.pk}" selected>' in card) is (not withheld)
    assert f'<option value="{page.pk}" selected>' not in card
    assert bool(_row_forms(parsed, CARD)) is (not withheld)


@pytest.mark.parametrize(("first_serial", "withheld"), [("VCB-0001", True), ("VCA-0001", False)], ids=["taken", "free"])
def test_a_reported_position_gives_no_member_that_another_switch_owns_by_serial(ui, vc_grants, first_serial, withheld):
    """Switch 2 reports the member's serial at position 1, so position 2 can not give Switch 3 that member too."""
    page, member = vc_grants(rows=_stack_rows((1, 2), serials=(first_serial, "12")))
    content = render(ui, page)
    parsed = module_tab_forms(content)

    (row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD + 1}".*?</tr>', content, flags=re.S)
    assert ("data-member-unattributed" in row) is withheld
    assert (f'<option value="{member.pk}" selected>' in row) is (not withheld)
    assert bool(_row_forms(parsed, CARD + 1)) is (not withheld)


@pytest.mark.parametrize(("third_serial", "withheld"), [("NEW123", True), ("VCB-0001", False)], ids=["new", "owned"])
def test_a_stack_member_that_netbox_does_not_hold_takes_its_cards_off_every_member(
    ui, vc_grants, third_serial, withheld
):
    """Switch 4 reports position 3, which no NetBox member holds, so its card is never the page device's."""
    rows = _stack_rows((1, 2, 3), serials=("VCA-0001", "CD-34", third_serial))
    page, member = vc_grants(rows=rows)
    content = render(ui, page)
    parsed = module_tab_forms(content)

    (on_page,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)
    (row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD + 2}".*?</tr>', content, flags=re.S)
    assert f'<option value="{page.pk}" selected>' in on_page
    assert ("data-member-unattributed" in row) is withheld
    assert bool(_row_forms(parsed, CARD + 2)) is (not withheld)


@pytest.mark.parametrize("occupant", [None, "GR-OTHER", "GR-CARD"], ids=["empty-bay", "other-type", "same-type"])
@pytest.mark.parametrize(
    ("card", "withheld"),
    [
        ({"entPhysicalContainedIn": 0, "entPhysicalSerialNum": "12"}, True),
        ({"entPhysicalContainedIn": 0, "entPhysicalSerialNum": ""}, False),
        ({"entPhysicalSerialNum": ""}, False),
    ],
    ids=["unattributed-short-serial", "unattributed-blank-serial", "attributed-by-the-page-serial"],
)
def test_a_stack_row_that_no_member_reported_offers_no_write(ui, vc_grants, card, withheld, occupant):
    """The card sits at the root and reports "12", so no rule names its member, and the page device must not take it.

    With a module in the bay, the row keeps the read-only Report VC issue control and loses Replace and the
    interface type review.
    """
    from dcim.models import Interface, InterfaceTemplate, ModuleType

    from netbox_librenms_plugin.tests.conftest import make_module_type_with_bays

    page, member = vc_grants(card=card)
    if occupant:
        occupant_type = (
            make_module_type_with_bays("GR-OTHER") if occupant == "GR-OTHER" else ModuleType.objects.get(model=occupant)
        )
        # A template name without a member position is what the VC naming report is for.
        InterfaceTemplate.objects.create(module_type=occupant_type, name="mgmt", type="1000base-t")
        module = install_module(page, "Card Slot", occupant, "OLD")
        # The interface differs from its template, so the row offers the interface type review.
        Interface.objects.filter(module=module, name="mgmt").update(type="10gbase-x-sfpp")
    before = _installed(page)
    content = render(ui, page)
    parsed = module_tab_forms(content)
    (row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)
    (checkbox,) = re.findall(rf'<input type="checkbox" name="select" value="{CARD}"[^>]*>', row)
    action, fields = rendered_module_form(content, "install-selected")

    assert bool(_row_forms(parsed, CARD)) is (not withheld and not occupant)
    assert ("data-member-unattributed" in row) is withheld
    assert (" disabled" in checkbox) is withheld
    if occupant:
        assert 'data-action="report-vc-issue"' in row
    if occupant == "GR-OTHER":
        assert ('data-action="replace"' in row) is (not withheld)
    if occupant == "GR-CARD":
        assert "Installed" in row
        assert ('data-action="review-interface-types"' in row) is (not withheld)
    post(ui, action, {**fields, "select": [str(CARD)]})
    assert _installed(page) == (before if withheld or occupant else {("Card Slot", "GR-CARD")})
    assert _installed(member) == set()


@pytest.mark.parametrize(
    ("card", "withheld"),
    [({"entPhysicalContainedIn": 0, "entPhysicalSerialNum": "12"}, True), ({}, False)],
    ids=["unattributed-short-serial", "attributed-by-the-page-serial"],
)
def test_a_stack_row_that_no_member_reported_offers_no_module_type_fix(ui, vc_grants, card, withheld):
    """The Fix Model modal adds a bay template to the occupant's type, which can hold another member's part."""
    child = {
        "entPhysicalIndex": CARD + 1,
        "entPhysicalContainedIn": CARD,
        "entPhysicalClass": "module",
        "entPhysicalName": "Daughter Card",
        "entPhysicalDescr": "Daughter Card",
        "entPhysicalModelName": "GR-DAUGHTER",
        "entPhysicalSerialNum": "SN-DAUGHTER",
    }
    page, _member = vc_grants(card=card, extra_rows=[child])
    install_module(page, "Card Slot", "GR-CARD", "SN-CARD")
    content = render(ui, page)
    (row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)

    assert "Installed" in row
    assert ("data-member-unattributed" in row) is withheld
    assert ("Fix Model" in row) is (not withheld)
    # The daughter reports its own serial, but its parent's "12" names no member, so it is unattributed too.
    (child_row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD + 1}".*?</tr>', content, flags=re.S)
    (child_checkbox,) = re.findall(rf'<input type="checkbox" name="select" value="{CARD + 1}"[^>]*>', child_row)
    assert ("data-member-unattributed" in child_row) is withheld
    assert (" disabled" in child_checkbox) is withheld
    assert (f'<option value="{page.pk}" selected>' in child_row) is (not withheld)


@pytest.mark.parametrize(("daughter_serial", "fix"), [("12", False), ("SN-DAUGHTER", True)], ids=["short", "identity"])
def test_an_attributed_card_takes_no_bay_suggestion_from_a_daughter_no_member_reported(
    ui, vc_grants, daughter_serial, fix
):
    """The card belongs to the page device, but its only daughter reports "12", so Fix Model has no bay to suggest."""
    child = {
        "entPhysicalIndex": CARD + 1,
        "entPhysicalContainedIn": CARD,
        "entPhysicalClass": "module",
        "entPhysicalName": "Daughter Card",
        "entPhysicalDescr": "Daughter Card",
        "entPhysicalModelName": "GR-DAUGHTER",
        "entPhysicalSerialNum": daughter_serial,
    }
    page, _member = vc_grants(extra_rows=[child])
    install_module(page, "Card Slot", "GR-CARD", "SN-CARD")
    content = render(ui, page)
    (row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)
    (child_row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD + 1}".*?</tr>', content, flags=re.S)

    assert "data-member-unattributed" not in row
    assert ("data-member-unattributed" in child_row) is (not fix)
    assert ("Fix Model" in row) is fix


@pytest.mark.parametrize(
    ("card", "withheld"),
    [({"entPhysicalContainedIn": 0}, True), ({"entPhysicalSerialNum": ""}, False)],
    ids=["unattributed-short-serial", "attributed-by-the-page-serial"],
)
def test_a_stack_row_that_no_member_reported_offers_no_device_type_fix_or_bay_mapping(ui, vc_grants, card, withheld):
    """The Fix Device Type and Map Existing Bay dialogs read the page device, which can be the wrong member."""
    page, _member = vc_grants(
        card={"entPhysicalName": "Fan Tray 9", "entPhysicalDescr": "Fan Tray 9", "entPhysicalSerialNum": "12", **card}
    )
    content = render(ui, page)
    (row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)

    assert "No Bay" in row
    assert ("data-member-unattributed" in row) is withheld
    assert ("Fix Device Type" in row) is (not withheld)
    # The Map Existing Bay dialog lists the page device's bays, which the reporting member can lack.
    assert ('data-action="map-existing-bay"' in row) is (not withheld)


@pytest.mark.parametrize(
    ("card", "withheld"),
    [({"entPhysicalContainedIn": 0}, True), ({"entPhysicalSerialNum": ""}, False)],
    ids=["unattributed-short-serial", "attributed-by-the-page-serial"],
)
def test_a_stack_row_that_no_member_reported_offers_no_bay_mapping_suggestion(ui, vc_grants, card, withheld):
    """The suggestion maps "0/1" to the page device's "Cage 1", a bay that the reporting member can lack."""
    page, _member = vc_grants(
        card={"entPhysicalName": "0/1", "entPhysicalDescr": "0/1", "entPhysicalSerialNum": "12", **card}
    )
    content = render(ui, page)
    (row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)

    assert "No Bay" in row
    assert ("data-member-unattributed" in row) is withheld
    assert ('data-action="add-bay-mapping"' in row) is (not withheld)


@pytest.mark.parametrize(
    ("card", "withheld"),
    [({"entPhysicalContainedIn": 0}, True), ({"entPhysicalSerialNum": ""}, False)],
    ids=["unattributed-short-serial", "attributed-by-the-page-serial"],
)
def test_a_stack_row_that_no_member_reported_names_no_bay_in_its_type_mapping(ui, vc_grants, card, withheld):
    """The card matches the page device's "Card Slot", which the reporting member can lack, so no bay is named."""
    page, _member = vc_grants(
        card={
            "entPhysicalModelName": "GR-UNKNOWN",
            "entPhysicalDescr": "Line card",
            "entPhysicalSerialNum": "12",
            **card,
        }
    )
    content = render(ui, page)
    (row,) = re.findall(rf'<tr[^>]*data-ent-index="{CARD}".*?</tr>', content, flags=re.S)
    (href,) = re.findall(r'href="([^"]*module-type-mappings[^"]*)"', row)
    description = parse_qs(urlsplit(href.replace("&amp;", "&")).query)["description"][0]

    assert "No Type" in row
    assert ("data-member-unattributed" in row) is withheld
    assert "GR-UNKNOWN" in description
    assert ("Card Slot" in description) is (not withheld)


@pytest.mark.parametrize("pick_page", [False, True], ids=["other-member", "page-device"])
def test_a_member_pick_offers_the_install_of_a_stack_row_that_no_member_reported(ui, vc_grants, pick_page):
    """The user names the member, so the table pinned to it offers the install there.

    The select shows no member, so a pick of the page device is a change that runs the verify too.
    """
    page, member = vc_grants(card={"entPhysicalContainedIn": 0, "entPhysicalSerialNum": "12"})
    picked, other = (page, member) if pick_page else (member, page)
    content = render(ui, page)
    (select,) = re.findall(rf'<select[^>]*data-module="{CARD}".*?</select>', content, flags=re.S)

    assert '<option value="" selected>' in select
    assert f'<option value="{page.pk}">' in select
    response = verify(ui, content, page, picked, CARD)

    assert response.status_code == 200, response.content
    action, fields = rendered_module_form(response.json()["formatted_row"]["actions"], "install-module", CARD)
    post(ui, action, fields)
    assert _installed(picked) == {("Card Slot", "GR-CARD")}
    assert _installed(other) == set()


def test_the_member_select_names_only_the_members_the_user_may_view(client, vc_grants):
    """Optic 1 belongs to the hidden member by its serial, so its select names no member."""
    from dcim.models import Device, ModuleBay, ModuleType

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    page, member = vc_grants(optic_1_on_member=True)
    user = make_user_with_perms("module-grants-page-only", [("view", Device)], constraints={"pk": page.pk})
    for model in (ModuleBay, ModuleType):
        user = grant(user, "view", model)
    client.force_login(user)

    content = render(client, page)

    def options(index):
        (select,) = re.findall(rf'<select name="device_selection_{index}".*?</select>', content, flags=re.S)
        return re.findall(r'<option value="([^"]*)"( selected)?', select)

    assert member.name not in content
    assert options(OPTIC_1) == [("", " selected"), (str(page.pk), "")]
    assert options(CARD) == [(str(page.pk), " selected")]


@pytest.mark.parametrize("module_serial", ["other", "row"])
def test_a_row_on_a_hidden_member_shows_no_bay_module_or_action_of_that_member(client, vc_grants, module_serial):
    """Optic 1 belongs to the hidden member by its serial: the table withholds that member's bays and module.

    With the row's serial, the member's module is also a serial conflict of the row, which must not name it either.
    """
    from dcim.models import Device, Module, ModuleBay, ModuleType

    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    page, member = vc_grants(optic_1_on_member=True)
    member_bay = ModuleBay.objects.get(device=member, name="Cage 1")
    member_module = Module.objects.create(
        device=member,
        module_bay=member_bay,
        module_type=ModuleType.objects.get(model="GR-OPTIC"),
        serial=member.serial if module_serial == "row" else "OLD",
    )
    user = make_user_with_perms("module-grants-hidden-bays", [("view", Device)], constraints={"pk": page.pk})
    for action, model in (("view", ModuleBay), ("view", ModuleType), ("view", Module), ("add", Module)):
        user = grant(user, action, model)
    for action in ("change", "delete"):
        user = grant(user, action, Module)
    client.force_login(user)

    content = render(client, page)
    parsed = module_tab_forms(content)

    assert member.name not in content
    assert member_bay.get_absolute_url() not in content
    assert member_module.get_absolute_url() not in content
    assert not _row_forms(parsed, OPTIC_1)
    assert not [form for form in parsed.forms if form["fields"].get("conflict_module_id") == str(member_module.pk)]
    assert not any(f"ent_index={OPTIC_1}" in link.get("hx-get", "") for link in parsed.links)
    (checkbox,) = re.findall(rf'<input type="checkbox" name="select" value="{OPTIC_1}"[^>]*>', content)
    assert " disabled" in checkbox
    # The page member keeps its rows: Card Slot still offers its install.
    assert rendered_module_form(content, "install-module", CARD)


def test_a_serial_conflict_on_a_device_the_user_cannot_view_names_nothing_and_offers_no_move(client, vc_grants):
    """The card serial sits on a module of a device outside the user's view: the row neither names nor moves it."""
    from dcim.models import Device, Module, ModuleBay, ModuleType
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays
    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    page, _member = vc_grants()
    other = make_device_with_module_bays("module-grants-hidden-other", ["Card Slot"])
    other_module = Module.objects.create(
        device=other,
        module_bay=ModuleBay.objects.get(device=other, name="Card Slot"),
        module_type=ModuleType.objects.get(model="GR-CARD"),
        serial="SN-CARD",
    )
    user = make_user_with_perms("module-grants-hidden-other", [("view", Device)], constraints={"pk": page.pk})
    for action, model in (("view", ModuleBay), ("view", ModuleType), ("view", Module)):
        user = grant(user, action, model)
    for action in ("add", "change", "delete"):
        user = grant(user, action, Module)
    client.force_login(user)

    content = render(client, page)

    assert other.name not in content
    assert other_module.get_absolute_url() not in content
    assert not [
        form
        for form in module_tab_forms(content).forms
        if form["fields"].get("conflict_module_id") == str(other_module.pk)
    ]

    # A scripted Move of that module is refused too, and the module stays where it is.
    response = post(
        client,
        reverse("plugins:netbox_librenms_plugin:move_module", args=[page.pk]),
        {
            "server_key": SERVER_KEY,
            "selected_device_id": str(page.pk),
            "conflict_module_id": str(other_module.pk),
            "target_bay_id": str(ModuleBay.objects.get(device=page, name="Card Slot").pk),
        },
    )
    assert other.name not in " ".join(messages_of(response))
    assert Module.objects.filter(pk=other_module.pk, device=other).exists()


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


@pytest.mark.parametrize(
    "fields",
    [
        {"page_device_id": None},
        {"device_id": None},
        {"device_id": "not-a-pk"},
        {"action_target": {"module_id": None}},
        {"action": "install_branch", "ent_index": 30, "rows": [[30, None]]},
    ],
    ids=["page-device", "device", "device-text", "action-target", "row-target"],
)
def test_a_grant_never_signs_a_missing_netbox_id(fields):
    from netbox_librenms_plugin.module_grants import sign_module_grant

    grant = {
        "page_device_id": 1,
        "device_id": 2,
        "server_key": SERVER_KEY,
        "action": "replace_module",
        "inventory_digest": "digest",
        "policy_fingerprint": "fingerprint",
        "render_mode": "attributed",
        "ent_index": 30,
        "action_target": {"module_id": 5},
    }

    with pytest.raises(ValueError, match="invalid module grant payload"):
        sign_module_grant(**{**grant, **fields})


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


# --- One rule withholds the write controls of a flagged row ----------------------------------------

# The controls that write the row's modules or interfaces, or an inventory rule for the row.
WRITE_ACTIONS = frozenset(
    {
        "install",
        "install-branch",
        "install-carrier",
        "update-serial",
        "update-interface",
        "review-interface-types",
        "replace",
        "move",
        "mark-built-in",
    }
)
# The controls that write a bay template or a bay mapping from the page device's bays or types.
PAGE_DEVICE_ACTIONS = frozenset(
    {"add-bay-mapping", "map-existing-bay", "add-carrier-rule", "fix-model", "fix-device-type"}
)
# The controls that keep a flagged row's actions: rules and types keyed by LibreNMS text, and the VC report.
UNGATED_ACTIONS = frozenset({"add-type-mapping", "add-module-type", "report-vc-issue"})
FLAG_WITHHOLDS = {"member_unattributed": WRITE_ACTIONS | PAGE_DEVICE_ACTIONS, "unlinked_optic": WRITE_ACTIONS}


def _flag_guard_records():
    """Return rows that together offer every write control of the table."""
    from netbox_librenms_plugin.tests.conftest import install_module, make_device_with_module_bays

    source = make_device_with_module_bays("flag-guard-move-source", ["Slot 1"])
    conflict = install_module(source, "Slot 1", "FLAG-GUARD-TYPE", serial="FLAG-GUARD-SERIAL")
    return [
        {
            "can_install": True,
            "module_bay_id": 5,
            "module_type_id": 10,
            "has_installable_children": True,
            "ent_physical_index": 42,
            "branch_rows": [[42, 1]],
        },
        {
            "status": "Installed",
            "can_update_serial": True,
            "can_update_interface_binding": True,
            "interface_type_mismatch_count": 2,
            "can_replace": True,
            "installed_module_id": 42,
            "module_bay_id": 7,
            "ent_physical_index": 77,
            "model_incomplete": True,
            "model_incomplete_target_pk": 3,
        },
        {
            "can_move_from": True,
            "serial_conflict_module": conflict,
            "serial_conflict_visible": True,
            "module_bay_id": 7,
        },
        {
            "status": "No Bay",
            "carrier_install_options": [
                {"bay_id": 12, "module_type_id": 34, "module_type_name": "CARRIER-A", "bay_name": "Slot 0"}
            ],
            "model_suggestion": {"librenms_name": "RE0", "netbox_bay_name": "RE0"},
        },
        {
            "status": "No Bay",
            "holder_hint_present": True,
            "device_empty_bay_names": ["RE0"],
            "mapping_source_name": "Routing Engine 0",
            "device_type_incomplete": True,
            "device_type_incomplete_target_pk": 4,
        },
        {
            "status": "No Type",
            "type_suggestion": {"librenms_model": "MODEL-A"},
            "module_type_create": {"model": "MODEL-A"},
        },
        {
            "ent_physical_index": 50,
            "built_in": {"records_part_number": False, "part_number": "", "rule_id": None, "device_type_id": 1},
        },
    ]


def _rendered_actions(records, **flags):
    """Return each data-action that the actions and status cells of *records* render with *flags* set."""
    from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable
    from netbox_librenms_plugin.tests.conftest import make_device

    table = LibreNMSModuleTable(
        [],
        device=make_device(f"flag-guard-page-{'-'.join(flags) or 'none'}"),
        server_key=SERVER_KEY,
        **{
            name: True
            for name in (
                "has_write_permission",
                "can_add_module",
                "can_change_module",
                "can_change_interface",
                "can_review_interface_types",
                "can_delete_module",
                "can_add_module_bay_template",
                "can_add_module_type",
                "can_add_carrier_rule",
                "can_add_module_bay_mapping",
                "can_map_existing_bay",
                "can_add_module_type_mapping",
                "can_change_device_type",
                "can_add_inventory_rule",
                "can_change_inventory_rule",
            )
        },
    )
    table.csrf_token = "flag-guard-csrf"
    table.inventory_snapshot_digest = "flag-guard-digest"
    table.policy_fingerprint = "flag-guard-fingerprint"
    rendered = ""
    for record in records:
        flagged = {**record, **flags}
        rendered += str(table.render_actions(None, flagged)) + str(table.render_status(flagged.get("status"), flagged))
    return set(re.findall(r'data-action="([^"]+)"', rendered))


def test_every_control_of_the_module_table_has_a_write_class():
    """A new control fails here until it names its class; a data-action built at run time is out of reach."""
    import inspect

    from netbox_librenms_plugin.tables import modules

    source = inspect.getsource(modules)
    names = set(re.findall(r'module_action_control\(\s*"\w+",\s*"([a-z-]+)"', source))
    names |= set(re.findall(r'\baction="([a-z-]+)"', source))

    assert names == WRITE_ACTIONS | PAGE_DEVICE_ACTIONS | UNGATED_ACTIONS
    assert not (WRITE_ACTIONS & PAGE_DEVICE_ACTIONS or (WRITE_ACTIONS | PAGE_DEVICE_ACTIONS) & UNGATED_ACTIONS)


@pytest.mark.parametrize("flag", sorted(FLAG_WITHHOLDS))
def test_a_flagged_row_renders_none_of_the_write_controls_its_flag_withholds(flag):
    """The rows offer every write control without a flag; the flag withholds its class and keeps the others."""
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    records = _flag_guard_records()
    offered = _rendered_actions(records)
    flagged = _rendered_actions(records, **{flag: True})

    expected = WRITE_ACTIONS | PAGE_DEVICE_ACTIONS
    if not netbox_relocates_module_subtree():
        # Move is offered only where NetBox relocates the module subtree.
        expected -= {"move"}
    assert expected <= offered
    assert not flagged & FLAG_WITHHOLDS[flag]
    assert flagged == offered - FLAG_WITHHOLDS[flag]
