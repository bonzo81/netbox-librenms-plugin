"""
The single-row LAG, parent and bridge endpoints save only when every row that they wrote is in the user's change scope.

The tests post the inline endpoints with real constrained object permissions. The endpoint reads
the rows that it may change and the rows that it may name before its first write. After the last
write, each written row must be in the user's change scope and in that selection. A row outside
refuses the whole link: nothing is saved, NetBox sends no event, the server logs no write, and the
refusal names only a row that the user may view. Every other text of the endpoints obeys the same display rule.
"""

import logging

import pytest
from core.models import ObjectChange
from dcim.models import Device, Interface, Platform
from django.db import IntegrityError
from django.urls import reverse
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import SERVER_KEY, seed_ports, sync_port
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
from netbox_librenms_plugin.transactions import row_changed
from netbox_librenms_plugin.utils import set_librenms_device_id
from netbox_librenms_plugin.views.sync import interfaces as interfaces_view

CACHE_TRANSITION_HEADER = "X-LibreNMS-Cache-Transition"


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


@pytest.fixture
def write_logs(caplog):
    """Return a function that lists the write lines that the sync views log, as the server log records them."""
    caplog.set_level(logging.INFO, logger=interfaces_view.__name__)
    return lambda: [
        record.getMessage()
        for record in caplog.records
        if record.name == interfaces_view.__name__ and record.getMessage().startswith("Set ")
    ]


def _refused(rows):
    """Return the JSON error of a refused link that names *rows*."""
    return {
        "error": f"Nothing was saved. These interfaces are outside the scope of your permissions after the sync: {rows}."
    }


def _bound(owner, name, port_id, **fields):
    """Create an interface of *owner* that is bound to LibreNMS port *port_id*."""
    if isinstance(owner, Device):
        interface = Interface(device=owner, name=name, type=fields.pop("type", "other"), **fields)
    else:
        interface = VMInterface(virtual_machine=owner, name=name, **fields)
    set_librenms_device_id(interface, port_id, SERVER_KEY)
    interface.save()
    return interface


def _user(username, owner_model, interface_model, *, view=None, change=None):
    """Return a user who may view the owner, with the given view and change scopes of the interfaces."""
    user = make_user_with_perms(username, [("view", owner_model)])
    user = grant(user, "view", interface_model, constraints=view)
    return grant(user, "change", interface_model, constraints=change)


def _link(client, owner, relation, port_id, related_port_id):
    """POST one single-row relationship sync, the way the inline button of the row does."""
    object_type = "device" if isinstance(owner, Device) else "virtualmachine"
    return client.post(
        reverse(
            f"plugins:netbox_librenms_plugin:sync_interface_{relation}",
            kwargs={"object_type": object_type, "object_id": owner.pk},
        ),
        {"port_id": str(port_id), f"{relation}_port_id": str(related_port_id), "server_key": SERVER_KEY},
    )


def _lag_scenario(tag):
    """A member ``eth1`` (port 1) and an aggregate ``Po1`` (port 100) of type ``other``, which the link promotes."""
    device = make_device(tag)
    member = _bound(device, "eth1", 1)
    aggregate = _bound(device, "Po1", 100)
    seed_ports(device, [sync_port(1, "eth1"), sync_port(100, "Po1", if_type="ieee8023adLag")], lag_members={1: 100})
    return device, member, aggregate


def _interface_change_records():
    return ObjectChange.objects.filter(changed_object_type__model__in=("interface", "vminterface"))


def _assert_nothing_saved(response, flushed_events, write_logs, error, *rows):
    """Assert the 403 refusal *error*, and that no write of the link stayed: no row, change record, event or log."""
    assert response.status_code == 403
    assert response.json() == error
    for row, before in rows:
        row.refresh_from_db()
        assert {field: getattr(row, field) for field in before} == before
    assert not _interface_change_records().exists()
    assert flushed_events == []
    assert write_logs() == []
    assert CACHE_TRANSITION_HEADER not in response


@transactional_db_with_all_apps()
def test_a_lag_link_that_moves_the_member_out_of_the_change_scope_saves_nothing(client, flushed_events, write_logs):
    """The aggregate stays in the scope; the member leaves it with its LAG, so the aggregate promotion rolls back too."""
    device, member, aggregate = _lag_scenario("relationship-scope-lag-member")
    client.force_login(_user("relationship-scope-lag-member-user", Device, Interface, change={"lag__isnull": True}))

    response = _link(client, device, "lag", 1, 100)

    _assert_nothing_saved(
        response,
        flushed_events,
        write_logs,
        _refused("eth1 (change)"),
        (member, {"lag_id": None}),
        (aggregate, {"type": "other"}),
    )


@transactional_db_with_all_apps()
def test_a_lag_link_whose_promotion_moves_the_aggregate_out_of_the_change_scope_saves_nothing(
    client, flushed_events, write_logs
):
    device, member, aggregate = _lag_scenario("relationship-scope-lag-aggregate")
    client.force_login(_user("relationship-scope-lag-aggregate-user", Device, Interface, change={"type": "other"}))

    response = _link(client, device, "lag", 1, 100)

    _assert_nothing_saved(
        response,
        flushed_events,
        write_logs,
        _refused("Po1 (change)"),
        (member, {"lag_id": None}),
        (aggregate, {"type": "other"}),
    )


@transactional_db_with_all_apps()
@pytest.mark.parametrize("relation", ["parent", "bridge"])
@pytest.mark.parametrize(
    "owner_model, interface_model", [(Device, Interface), (VirtualMachine, VMInterface)], ids=["device", "vm"]
)
def test_a_parent_or_bridge_link_that_moves_the_source_out_of_the_change_scope_saves_nothing(
    client, flushed_events, write_logs, relation, owner_model, interface_model
):
    tag = f"relationship-scope-{relation}-{owner_model._meta.model_name}"
    owner = make_device(tag) if owner_model is Device else make_vm(tag)
    related_fields = {"type": "bridge"} if relation == "bridge" and owner_model is Device else {}
    source = _bound(owner, "eth1.100", 11)
    related = _bound(owner, "eth1", 10, **related_fields)
    edges = {"sub_interfaces": {11: 10}} if relation == "parent" else {"bridge_members": {11: 10}}
    seed_ports(owner, [sync_port(11, "eth1.100"), sync_port(10, "eth1")], **edges)
    client.force_login(_user(f"{tag}-user", owner_model, interface_model, change={f"{relation}__isnull": True}))

    response = _link(client, owner, relation, 11, 10)

    source_before = {f"{relation}_id": None}
    if owner_model is Device and relation == "parent":
        source_before["type"] = "other"
    _assert_nothing_saved(response, flushed_events, write_logs, _refused("eth1.100 (change)"), (source, source_before))
    assert related.pk not in {pk for _model, pk in flushed_events}


@transactional_db_with_all_apps()
def test_a_refusal_names_only_the_rows_that_the_user_may_view(client, flushed_events, write_logs):
    """The user may view only the aggregate, so the refusal counts the member and never names it."""
    device, member, aggregate = _lag_scenario("relationship-scope-hidden")
    client.force_login(
        _user(
            "relationship-scope-hidden-user",
            Device,
            Interface,
            view={"name__startswith": "Po"},
            change={"lag__isnull": True, "type": "other"},
        )
    )

    response = _link(client, device, "lag", 1, 100)

    _assert_nothing_saved(
        response,
        flushed_events,
        write_logs,
        _refused("Po1 (change) and 1 interface you cannot view"),
        (member, {"lag_id": None}),
        (aggregate, {"type": "other"}),
    )
    assert "eth1" not in response.json()["error"]


@transactional_db_with_all_apps()
def test_a_link_that_keeps_every_written_row_in_the_change_scope_is_saved(client, flushed_events, write_logs):
    device, member, aggregate = _lag_scenario("relationship-scope-kept")
    client.force_login(_user("relationship-scope-kept-user", Device, Interface, change={"enabled": True}))

    response = _link(client, device, "lag", 1, 100)

    assert response.status_code == 200
    assert response.json() == {"status": "success", "message": "Linked eth1 to LAG Po1"}
    member.refresh_from_db()
    aggregate.refresh_from_db()
    assert (member.lag_id, aggregate.type) == (aggregate.pk, "lag")
    assert sorted(flushed_events) == sorted([("interface", member.pk), ("interface", aggregate.pk)])
    assert write_logs() == ["Set interface Po1 type=lag", "Set eth1.lag = Po1"]


@transactional_db_with_all_apps()
def test_a_link_that_runs_again_after_a_lock_conflict_logs_each_write_once(client, monkeypatch, write_logs):
    """Error injection: the first attempt records a stale row after its writes, so the runner rolls it back and runs again."""
    device, member, aggregate = _lag_scenario("relationship-scope-retry")
    client.force_login(_user("relationship-scope-retry-user", Device, Interface))
    real_check = interfaces_view._RowSelection.check_writes
    attempts = []

    def check_then_conflict_once(selection, writes):
        real_check(selection, writes)
        attempts.append(selection)
        if len(attempts) == 1:
            row_changed("eth1")

    monkeypatch.setattr(interfaces_view._RowSelection, "check_writes", check_then_conflict_once)

    response = _link(client, device, "lag", 1, 100)

    assert response.status_code == 200
    assert len(attempts) == 2
    member.refresh_from_db()
    aggregate.refresh_from_db()
    assert (member.lag_id, aggregate.type) == (aggregate.pk, "lag")
    assert write_logs() == ["Set interface Po1 type=lag", "Set eth1.lag = Po1"]


# Each end of the LAG link that the user may change but not view, as a view scope that hides it.
HIDDEN_END_SCOPES = {"member": {"name__startswith": "Po"}, "aggregate": {"name__startswith": "eth"}}


def _shown_ends(hidden_end):
    """Return the texts that name the member and the aggregate when the user may not view *hidden_end*."""
    hidden = interfaces_view.HIDDEN_INTERFACE
    return (hidden, "Po1") if hidden_end == "member" else ("eth1", hidden)


def _hidden_end_user(tag, hidden_end):
    return _user(f"{tag}-{hidden_end}-user", Device, Interface, view=HIDDEN_END_SCOPES[hidden_end])


def _assert_link_refused(response, error, member, aggregate, hidden_end):
    assert response.status_code == 409
    assert response.json() == {"error": error}
    assert ("eth1" if hidden_end == "member" else "Po1") not in error
    member.refresh_from_db()
    aggregate.refresh_from_db()
    assert (member.lag_id, aggregate.type) == (None, "other")


@transactional_db_with_all_apps()
@pytest.mark.parametrize("hidden_end", HIDDEN_END_SCOPES)
def test_a_saved_link_names_only_the_interfaces_that_the_user_may_view(client, hidden_end):
    device, member, aggregate = _lag_scenario(f"relationship-text-saved-{hidden_end}")
    client.force_login(_hidden_end_user("relationship-text-saved", hidden_end))

    response = _link(client, device, "lag", 1, 100)

    member_text, aggregate_text = _shown_ends(hidden_end)
    assert response.status_code == 200
    assert response.json() == {"status": "success", "message": f"Linked {member_text} to LAG {aggregate_text}"}
    member.refresh_from_db()
    assert member.lag_id == aggregate.pk


@transactional_db_with_all_apps()
@pytest.mark.parametrize("hidden_end", HIDDEN_END_SCOPES)
def test_an_interface_rule_refusal_names_only_the_interfaces_that_the_user_may_view(client, hidden_end):
    """The rule ignores the hidden end, so the refusal also says which end blocks the link without naming it."""
    tag = f"relationship-text-rule-{hidden_end}"
    device, member, aggregate = _lag_scenario(tag)
    device.platform = Platform.objects.create(name=tag, slug=tag)
    device.save()
    rule = InterfaceTypeMapping.objects.create(
        action=InterfaceTypeMapping.ACTION_IGNORE,
        platform=device.platform,
        name_pattern="^eth" if hidden_end == "member" else "^Po",
    )
    client.force_login(_hidden_end_user("relationship-text-rule", hidden_end))

    response = _link(client, device, "lag", 1, 100)

    member_text, aggregate_text = _shown_ends(hidden_end)
    reason = f"ignored by interface rule {rule.pk} ({rule})"
    error = f"Cannot link {member_text} to LAG {aggregate_text}; {interfaces_view.HIDDEN_INTERFACE}: {reason}."
    _assert_link_refused(response, error, member, aggregate, hidden_end)


@transactional_db_with_all_apps()
@pytest.mark.parametrize("hidden_end", HIDDEN_END_SCOPES)
def test_a_netbox_validation_refusal_names_only_the_interfaces_that_the_user_may_view(client, hidden_end):
    """NetBox refuses a LAG for a virtual member."""
    device, member, aggregate = _lag_scenario(f"relationship-text-invalid-{hidden_end}")
    Interface.objects.filter(pk=member.pk).update(type="virtual")
    client.force_login(_hidden_end_user("relationship-text-invalid", hidden_end))

    response = _link(client, device, "lag", 1, 100)

    member_text, aggregate_text = _shown_ends(hidden_end)
    error = (
        f"Cannot link {member_text} to LAG {aggregate_text}: NetBox rejected the LAG relationship. Check the "
        "interface types, chassis membership, and that the two interfaces are not the same interface."
    )
    _assert_link_refused(response, error, member, aggregate, hidden_end)


@transactional_db_with_all_apps()
@pytest.mark.parametrize("hidden_end", HIDDEN_END_SCOPES)
def test_a_database_conflict_names_only_the_interfaces_that_the_user_may_view(client, monkeypatch, hidden_end):
    """Error injection: the write of the link raises the IntegrityError of a concurrent change."""
    device, member, aggregate = _lag_scenario(f"relationship-text-conflict-{hidden_end}")
    client.force_login(_hidden_end_user("relationship-text-conflict", hidden_end))

    def conflict(*_args, **_kwargs):
        raise IntegrityError("the aggregate was deleted concurrently")

    monkeypatch.setattr(interfaces_view, "_apply_interface_relationship", conflict)

    response = _link(client, device, "lag", 1, 100)

    member_text, aggregate_text = _shown_ends(hidden_end)
    error = (
        f"Cannot link {member_text} to LAG {aggregate_text}: "
        "a concurrent change interrupted the update. Refresh and retry."
    )
    _assert_link_refused(response, error, member, aggregate, hidden_end)


def test_scope_refusal_separates_the_user_message_from_exception_details():
    """Backend diagnostic changes must not replace the permission-scoped response text."""
    refusal = interfaces_view._RowsOutsideScopeError([("eth1", ("change",))], 1)
    refusal.args = ("internal diagnostic detail",)

    assert refusal.user_message == _refused("eth1 (change) and 1 interface you cannot view")["error"]
