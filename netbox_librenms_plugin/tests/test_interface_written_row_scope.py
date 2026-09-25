"""
The interface sync saves only when every row that it wrote is in the user's scope, as NetBox's edit views do.

The tests post the interface sync with real constrained object permissions. Each row that the sync
created or changed must be in the user's change scope, and each row that it created also in the add
scope. The check reads the rows after the last write of the sync, the relationship pass too. A row
outside the scope refuses the whole sync: nothing is saved, and NetBox sends no event. Every pass
reads the rows that it may change from one selection, read before the first write, and every text
names only a row that the user may view. The IP tab creates a missing interface with the same rule,
and refuses only that address.
"""

import ast
import inspect

import pytest
from core.models import ObjectChange
from dcim.models import Device, Interface, MACAddress
from django.urls import reverse
from ipam.models import VLAN, IPAddress, VLANGroup
from netbox import context_managers
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_vm,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import (
    SERVER_KEY,
    SYNCED,
    bound_interface,
    seed_ports,
    sync_port,
    synced_interface,
)
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms, messages_on
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

IN_SCOPE = {"name__startswith": "eth"}
HIDDEN = "an interface you cannot view"
NO_LAG = {"lag__isnull": True}
CACHE_TRANSITION_HEADER = "X-LibreNMS-Cache-Transition"
OWNERS = pytest.mark.parametrize(
    "object_type, owner_model, interface_model",
    [("device", Device, Interface), ("virtualmachine", VirtualMachine, VMInterface)],
    ids=["device", "vm"],
)


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


@pytest.fixture
def attempts(monkeypatch):
    """Count the attempts of each interface sync; each attempt still runs."""
    real_attempt = SyncInterfacesView._sync_attempt
    calls = []

    def counting_attempt(self, *args, **kwargs):
        calls.append(self.object.pk)
        return real_attempt(self, *args, **kwargs)

    monkeypatch.setattr(SyncInterfacesView, "_sync_attempt", counting_attempt)
    return calls


@pytest.fixture
def flushed_events(monkeypatch):
    """Record the object of each event that NetBox sends at the end of a request; NetBox still sends it."""
    real_flush = context_managers.flush_events
    flushed = []

    def recording_flush(events):
        flushed.extend((event["object_type"].model, event["object_id"]) for event in events)
        return real_flush(events)

    monkeypatch.setattr(context_managers, "flush_events", recording_flush)
    return flushed


def _refused(rows):
    """Return the one message of a refused sync that names *rows*."""
    return (
        "error",
        f"Nothing was saved. These interfaces are outside the scope of your permissions after the sync: {rows}.",
    )


def _user(username, owner_model, interface_model, *, view=None, add=None, change=None):
    """Return a user who may view the owner and the VLANs, with the given view, add and change scopes of the interfaces."""
    user = make_user_with_perms(username, [("view", owner_model), ("view", VLAN), ("view", VLANGroup)])
    for action, constraints in (("view", view), ("add", add), ("change", change)):
        user = grant(user, action, interface_model, constraints=constraints)
    return user


def _owner(object_type, name):
    return make_device(name) if object_type == "device" else make_vm(name)


def _interfaces(owner):
    """Return the interfaces of *owner* by name."""
    if isinstance(owner, Device):
        return {interface.name: interface for interface in Interface.objects.filter(device=owner)}
    return {interface.name: interface for interface in VMInterface.objects.filter(virtual_machine=owner)}


def _post_sync(client, owner, object_type, port_ids, *, exclude_columns=(), htmx=False):
    """Post the interface sync of *owner* for *port_ids*; every column is synced unless excluded."""
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_selected_interfaces",
        kwargs={"object_type": object_type, "object_id": owner.pk},
    )
    data = {
        "server_key": SERVER_KEY,
        "interface_name_field": "ifName",
        "select": [str(port_id) for port_id in port_ids],
        "exclude_columns": list(exclude_columns),
    }
    headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
    return client.post(url, data, **headers)


def _written_change_records():
    """Return the change records of interfaces and MAC addresses."""
    return ObjectChange.objects.filter(changed_object_type__model__in=("interface", "vminterface", "macaddress"))


@transactional_db_with_all_apps()
@OWNERS
@pytest.mark.parametrize("refused_action", ["add", "change"])
def test_a_new_row_outside_a_constrained_scope_refuses_the_whole_sync(
    client, attempts, flushed_events, object_type, owner_model, interface_model, refused_action
):
    owner = _owner(object_type, f"written-scope-{object_type}-{refused_action}")
    seed_ports(
        owner,
        [
            sync_port(10, "eth0", alias="uplink", mac="00:11:22:33:44:0a"),
            sync_port(11, "private0", alias="secret uplink", mac="00:11:22:33:44:0b"),
        ],
    )
    client.force_login(
        _user(
            f"written-scope-{object_type}-{refused_action}-user",
            owner_model,
            interface_model,
            **{refused_action: IN_SCOPE},
        )
    )

    response = _post_sync(client, owner, object_type, [10, 11])

    assert messages_on(response.wsgi_request) == [_refused(f"private0 ({refused_action})")]
    assert len(attempts) == 1, "a refusal is not a lock conflict, so the sync does not run it again"
    assert _interfaces(owner) == {}
    assert not MACAddress.objects.exists()
    assert not _written_change_records().exists()
    assert flushed_events == []
    assert CACHE_TRANSITION_HEADER not in response


@transactional_db_with_all_apps()
@pytest.mark.parametrize("member_exists", [False, True], ids=["new", "existing"])
def test_the_relationship_pass_cannot_move_a_row_out_of_the_scope(client, attempts, flushed_events, member_exists):
    """The relationship pass sets the LAG after the attribute pass wrote the member, so the check reads the row after it."""
    device = make_device(f"written-scope-lag-{member_exists}")
    bound_interface(device, "Po1", 100, iface_type="lag")
    if member_exists:
        synced_interface(device, "eth1", 1)
    seed_ports(
        device,
        [sync_port(1, "eth1", mac="00:11:22:33:44:0c"), sync_port(100, "Po1", if_type="ieee8023adLag")],
        lag_members={1: 100},
    )
    client.force_login(_user(f"written-scope-lag-{member_exists}-user", Device, Interface, add=NO_LAG, change=NO_LAG))

    # The existing member is in sync already, so only the relationship pass writes it.
    response = _post_sync(
        client, device, "device", [1], exclude_columns=("vlans", "mac_address") if member_exists else ("vlans",)
    )

    assert messages_on(response.wsgi_request) == [_refused("eth1 (change)" if member_exists else "eth1 (add, change)")]
    assert len(attempts) == 1
    assert set(_interfaces(device)) == ({"Po1", "eth1"} if member_exists else {"Po1"})
    assert not Interface.objects.filter(lag__isnull=False).exists()
    assert not MACAddress.objects.exists()
    assert not _written_change_records().exists()
    assert flushed_events == []
    assert CACHE_TRANSITION_HEADER not in response


@pytest.mark.django_db
def test_a_row_that_only_the_relationship_pass_wrote_is_checked_and_named(client):
    """The pass promotes the aggregate ``Po1``, which the user did not select, to ``type=lag``."""
    device = make_device("written-scope-aggregate")
    bound_interface(device, "Po1", 100)
    seed_ports(
        device,
        [sync_port(1, "eth1"), sync_port(100, "Po1", if_type="ieee8023adLag")],
        lag_members={1: 100},
    )
    change = [IN_SCOPE, {"name": "Po1", "type": "other"}]
    client.force_login(_user("written-scope-aggregate-user", Device, Interface, change=change))

    response = _post_sync(client, device, "device", [1], exclude_columns=("vlans",))

    assert messages_on(response.wsgi_request) == [_refused("Po1 (change)")]
    assert Interface.objects.get(device=device, name="Po1").type == "other"


@pytest.mark.django_db
def test_the_relationship_pass_uses_the_selection_read_at_the_start_of_the_attempt(client):
    """After the attribute write, ``eth1`` is outside the scope until the relationship pass sets its LAG."""
    device = make_device("written-scope-lag-final-state")
    member = synced_interface(device, "eth1", 1, description="old")
    aggregate = bound_interface(device, "Po1", 100, iface_type="lag")
    seed_ports(
        device,
        [sync_port(1, "eth1", alias="new"), sync_port(100, "Po1", if_type="ieee8023adLag")],
        lag_members={1: 100},
    )
    change = [
        {"description": "old", "lag__isnull": True},
        {"description": "new", "lag_id": aggregate.pk},
        {"pk": aggregate.pk},
    ]
    client.force_login(_user("written-scope-lag-final-state-user", Device, Interface, change=change))

    response = _post_sync(client, device, "device", [1], exclude_columns=("vlans",))

    assert messages_on(response.wsgi_request) == [("success", SYNCED)]
    member.refresh_from_db()
    assert (member.description, member.lag_id) == ("new", aggregate.pk)


@transactional_db_with_all_apps()
def test_a_sync_whose_rows_are_all_in_scope_syncs_every_row(client, attempts, flushed_events):
    """The check refuses no row that is in the scope, also a row that the relationship pass wrote."""
    device = make_device("written-scope-inside")
    aggregate = bound_interface(device, "Po1", 100, iface_type="lag")
    existing = synced_interface(device, "eth1", 11)
    VLAN.objects.create(vid=100, name="written-scope-inside")
    seed_ports(
        device,
        [
            sync_port(10, "eth0", alias="uplink", mac="00:11:22:33:44:0d", untagged_vlan=100, tagged_vlans=[]),
            sync_port(11, "eth1"),
            sync_port(100, "Po1", if_type="ieee8023adLag"),
        ],
        lag_members={11: 100},
    )
    client.force_login(_user("written-scope-inside-user", Device, Interface, add=IN_SCOPE, change=IN_SCOPE))

    response = _post_sync(client, device, "device", [10, 11])

    assert messages_on(response.wsgi_request) == [("success", SYNCED)]
    assert len(attempts) == 1
    interfaces = _interfaces(device)
    assert set(interfaces) == {"eth0", "eth1", "Po1"}
    created = interfaces["eth0"]
    assert (created.description, created.untagged_vlan.vid) == ("uplink", 100)
    assert str(created.primary_mac_address.mac_address).lower() == "00:11:22:33:44:0d"
    assert interfaces["eth1"].lag == aggregate
    assert {("interface", created.pk), ("interface", existing.pk)} <= set(flushed_events)
    assert CACHE_TRANSITION_HEADER in response


@pytest.mark.django_db
@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_the_refusal_reaches_the_user_on_a_plain_and_an_htmx_submit(client, htmx):
    """The refusal publishes one error, and no success banner or skipped-row counter."""
    device = make_device(f"written-scope-answer-{htmx}")
    seed_ports(device, [sync_port(10, "eth0"), sync_port(11, "private0")])
    client.force_login(_user(f"written-scope-answer-{htmx}-user", Device, Interface, add=IN_SCOPE))

    response = _post_sync(client, device, "device", [10, 11], htmx=htmx)

    refusal = _refused("private0 (add)")
    assert messages_on(response.wsgi_request) == [refusal]
    if htmx:
        assert response.status_code == 200
        assert refusal[1] in response.content.decode()
    else:
        assert response.status_code == 302
        tab = reverse("dcim:device_librenms_sync", kwargs={"pk": device.pk}) + "?tab=interfaces"
        assert response["Location"].startswith(tab)
    assert _interfaces(device) == {}


@pytest.mark.django_db
def test_the_add_scope_is_checked_on_the_row_as_the_sync_wrote_it(client):
    """The new rows are empty before the write and match no constraint, so a check of the empty rows would name ``eth0`` too."""
    device = make_device("written-scope-written-row")
    seed_ports(device, [sync_port(10, "eth0", alias="uplink"), sync_port(11, "eth1", alias="other")])
    client.force_login(
        _user("written-scope-written-row-user", Device, Interface, add={"description": "uplink", "mtu": 1500})
    )

    response = _post_sync(client, device, "device", [10, 11])

    assert messages_on(response.wsgi_request) == [_refused("eth1 (add)")]
    assert _interfaces(device) == {}


@pytest.mark.django_db
def test_the_add_scope_is_checked_after_the_vlan_write(client):
    """Only the VLAN write sets the mode, so a check before it would name ``eth0`` too."""
    device = make_device("written-scope-vlan")
    VLAN.objects.create(vid=100, name="written-scope-vlan")
    seed_ports(device, [sync_port(10, "eth0", untagged_vlan=100, tagged_vlans=[]), sync_port(11, "eth1")])
    client.force_login(_user("written-scope-vlan-user", Device, Interface, add={"mode": "access"}))

    response = _post_sync(client, device, "device", [10, 11])

    assert messages_on(response.wsgi_request) == [_refused("eth1 (add)")]
    assert _interfaces(device) == {}


@pytest.mark.django_db
def test_a_change_of_only_the_tagged_vlans_is_checked(client):
    """The sync saves no column of ``eth0``; only its new tagged VLAN takes it out of the change scope."""
    device = make_device("written-scope-tagged")
    interface = synced_interface(device, "eth0", 10, mode="tagged")
    VLAN.objects.create(vid=100, name="written-scope-tagged")
    seed_ports(device, [sync_port(10, "eth0", tagged_vlans=[100])])
    client.force_login(_user("written-scope-tagged-user", Device, Interface, change={"tagged_vlans__isnull": True}))

    response = _post_sync(client, device, "device", [10], exclude_columns=("mac_address",))

    assert messages_on(response.wsgi_request) == [_refused("eth0 (change)")]
    assert not interface.tagged_vlans.exists()


@pytest.mark.django_db
def test_a_write_that_takes_a_row_out_of_the_change_scope_gets_the_refusal_not_a_retry(client, attempts):
    """The VLAN write reads the row after the attribute write took it out of the scope; only the final check decides."""
    device = make_device("written-scope-description")
    interface = synced_interface(device, "eth0", 1, description="old")
    seed_ports(device, [sync_port(1, "eth0", alias="new")])
    client.force_login(_user("written-scope-description-user", Device, Interface, change={"description": "old"}))

    response = _post_sync(client, device, "device", [1])

    assert messages_on(response.wsgi_request) == [_refused("eth0 (change)")]
    assert len(attempts) == 1
    interface.refresh_from_db()
    assert interface.description == "old"


@pytest.mark.django_db
def test_a_final_state_in_the_change_scope_is_saved_through_an_intermediate_state_outside_it(client):
    """The attribute write sets the description first, so the row is outside the scope until the VLAN write sets the mode."""
    device = make_device("written-scope-final-state")
    interface = synced_interface(device, "eth0", 1, description="old", mode="access")
    vlan = VLAN.objects.create(vid=100, name="written-scope-final-state")
    seed_ports(device, [sync_port(1, "eth0", alias="new", tagged_vlans=[100])])
    change = [{"description": "old", "mode": "access"}, {"description": "new", "mode": "tagged"}]
    client.force_login(_user("written-scope-final-state-user", Device, Interface, change=change))

    response = _post_sync(client, device, "device", [1])

    assert messages_on(response.wsgi_request) == [("success", SYNCED)]
    interface.refresh_from_db()
    assert (interface.description, interface.mode, list(interface.tagged_vlans.all())) == ("new", "tagged", [vlan])


# ---------------------------------------------------------------------------
# A refusal names only a row that the user may view
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_attribute_pass_names_a_refused_row_only_when_the_user_may_view_it(client):
    """The user may change ``private0`` but not view it, so the refusal counts it and does not name it."""
    device = make_device("written-scope-hidden-row")
    synced_interface(device, "eth0", 10, description="old")
    synced_interface(device, "private0", 11, description="old")
    seed_ports(device, [sync_port(10, "eth0", alias="new"), sync_port(11, "private0", alias="new")])
    client.force_login(
        _user("written-scope-hidden-row-user", Device, Interface, view=IN_SCOPE, change={"description": "old"})
    )

    response = _post_sync(client, device, "device", [10, 11], exclude_columns=("vlans",))

    assert messages_on(response.wsgi_request) == [_refused("eth0 (change) and 1 interface you cannot view")]


@pytest.mark.django_db
def test_the_relationship_pass_names_a_refused_row_only_when_the_user_may_view_it(client):
    """The pass finds the aggregate through its port and may change it, but the user may not view it."""
    device = make_device("written-scope-hidden-aggregate")
    aggregate = bound_interface(device, "private-current", 100)
    seed_ports(
        device,
        [sync_port(1, "eth1"), sync_port(100, "Po1", if_type="ieee8023adLag")],
        lag_members={1: 100},
    )
    change = [IN_SCOPE, {"type": "other"}]
    client.force_login(_user("written-scope-hidden-aggregate-user", Device, Interface, view=IN_SCOPE, change=change))

    response = _post_sync(client, device, "device", [1], exclude_columns=("vlans",))

    assert messages_on(response.wsgi_request) == [_refused("1 interface you cannot view")]
    assert Interface.objects.get(pk=aggregate.pk).type == "other"


def _kept_name_of_a_hidden_row(device):
    """The hidden row holds port 1, whose reported name ``eth-new`` another interface holds."""
    bound_interface(device, "private-current", 1)
    bound_interface(device, "eth-new", 2)
    seed_ports(device, [sync_port(1, "eth-new")])
    return [1], ("vlans",)


def _ignored_hidden_aggregate(device):
    """An ignore rule blocks the LibreNMS aggregate ``Po1``, which the hidden row holds."""
    bound_interface(device, "eth1", 1)
    bound_interface(device, "private-current", 100)
    seed_ports(device, [sync_port(1, "eth1"), sync_port(100, "Po1", if_type="ieee8023adLag")], lag_members={1: 100})
    InterfaceTypeMapping.objects.create(action=InterfaceTypeMapping.ACTION_IGNORE, name_pattern="^Po1$")
    return [1], ("vlans",)


def _hidden_aggregate_that_a_rule_keeps_off_lag(device):
    """A Set type rule gives the hidden aggregate a type that is not ``lag``, so the pass cannot promote it."""
    bound_interface(device, "eth1", 1)
    bound_interface(device, "private-current", 100)
    seed_ports(device, [sync_port(1, "eth1"), sync_port(100, "Po1", if_type="ieee8023adLag")], lag_members={1: 100})
    InterfaceTypeMapping.objects.create(name_pattern="^Po1$", netbox_type="1000base-t")
    return [1], ("vlans",)


def _hidden_member_of_an_aggregate_whose_type_is_excluded(device):
    """The hidden member keeps its name (the name is excluded), and the aggregate needs a type the sync may not write."""
    bound_interface(device, "private-member", 1)
    bound_interface(device, "eth-agg", 100)
    seed_ports(device, [sync_port(1, "eth1"), sync_port(100, "Po1", if_type="ieee8023adLag")], lag_members={1: 100})
    return [1], ("vlans", "name", "type")


def _hidden_child_that_a_rule_keeps_physical(device):
    """A Set type rule keeps the hidden child physical, so the pass cannot promote it under its hidden parent."""
    bound_interface(device, "private-child", 1, iface_type="1000base-t")
    bound_interface(device, "private-parent", 2, iface_type="1000base-t")
    seed_ports(device, [sync_port(1, "eth1.100"), sync_port(2, "eth1")], sub_interfaces={1: 2})
    InterfaceTypeMapping.objects.create(name_pattern="^eth1\\.100$", netbox_type="1000base-t")
    return [1], ("vlans", "name")


def _hidden_child_whose_type_is_excluded(device):
    """The hidden child is physical, and the sync may not write the type that a parent link needs."""
    bound_interface(device, "private-child", 1, iface_type="1000base-t")
    bound_interface(device, "eth-parent", 2, iface_type="1000base-t")
    seed_ports(device, [sync_port(1, "eth1.100"), sync_port(2, "eth1")], sub_interfaces={1: 2})
    return [1], ("vlans", "name", "type")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "scenario",
    [
        _kept_name_of_a_hidden_row,
        _ignored_hidden_aggregate,
        _hidden_aggregate_that_a_rule_keeps_off_lag,
        _hidden_member_of_an_aggregate_whose_type_is_excluded,
        _hidden_child_that_a_rule_keeps_physical,
        _hidden_child_whose_type_is_excluded,
    ],
    ids=lambda scenario: scenario.__name__.strip("_"),
)
def test_no_text_of_the_sync_names_a_row_that_the_user_may_change_but_not_view(client, scenario):
    """Each scenario drives one warning path of the sync to a row whose name starts with ``private``."""
    name = scenario.__name__.strip("_")[:60]
    device = make_device(name)
    port_ids, exclude_columns = scenario(device)
    user = _user(f"{name}-user", Device, Interface, view=IN_SCOPE)
    private = Interface.objects.filter(device=device, name__startswith="private")
    assert private.exists() and not private.restrict(user, "view").exists(), "precondition: the private rows are hidden"
    client.force_login(user)

    response = _post_sync(client, device, "device", port_ids, exclude_columns=exclude_columns, htmx=True)

    texts = [text for _level, text in messages_on(response.wsgi_request)]
    assert any(HIDDEN in text.lower() for text in texts), texts
    assert all("private" not in text for text in texts), texts
    assert "private" not in response.content.decode()


def _reads_a_name(expression):
    """Return whether *expression* reads the ``name`` attribute of an object."""
    return any(isinstance(node, ast.Attribute) and node.attr == "name" for node in ast.walk(expression))


def test_no_text_of_the_sync_view_takes_an_interface_name_past_the_display_rule():
    """A cheap first check; the scenarios above prove the rule for each warning path."""
    from netbox_librenms_plugin.views.sync import interfaces

    module = ast.parse(inspect.getsource(interfaces))
    view = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "SyncInterfacesView")
    direct = [
        ast.unparse(node)
        for node in ast.walk(view)
        if (isinstance(node, ast.FormattedValue) and _reads_a_name(node.value))
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_record_skipped_conflict"
            and _reads_a_name(node.args[0])
        )
    ]

    assert direct == [], "an interface name reaches a text without _shown()"


# ---------------------------------------------------------------------------
# NetBox renames the channel children of a renamed parent after the commit
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(Interface, "channels"), reason="NetBox before 4.7 has no channelized interfaces")
@transactional_db_with_all_apps()
@pytest.mark.parametrize(
    "scope, refused",
    [("inside", None), ("outside", "eth-old:1 (change)"), ("outside-hidden", "1 interface you cannot view")],
)
def test_the_channel_children_that_netbox_renames_must_be_in_the_change_scope(client, flushed_events, scope, refused):
    """The sync renames ``eth-old``; NetBox renames ``eth-old:1`` after the commit, but not ``breakout-2``."""
    device = make_device(f"written-scope-channels-{scope}")
    parent = bound_interface(device, "eth-old", 1, iface_type="1000base-t")
    parent.channels = 4
    parent.save()
    child = Interface.objects.create(device=device, name="eth-old:1", parent=parent, channel_id=1, type="1000base-t")
    # Outside every change scope here, but its name does not follow ``<parent name>:<channel ID>``.
    Interface.objects.create(device=device, name="breakout-2", parent=parent, channel_id=2, type="1000base-t")
    seed_ports(device, [sync_port(1, "eth-new")])
    view = {"channel_id__isnull": True} if scope == "outside-hidden" else None
    change = IN_SCOPE if scope == "inside" else {"pk": parent.pk}
    client.force_login(_user(f"written-scope-channels-{scope}-user", Device, Interface, view=view, change=change))

    response = _post_sync(client, device, "device", [1], exclude_columns=("vlans", "type"))

    parent.refresh_from_db()
    child.refresh_from_db()
    assert Interface.objects.filter(device=device, name="breakout-2").exists()
    if refused is None:
        assert messages_on(response.wsgi_request) == [("success", SYNCED)]
        assert (parent.name, child.name) == ("eth-new", "eth-new:1")
    else:
        assert messages_on(response.wsgi_request) == [_refused(refused)]
        assert (parent.name, child.name) == ("eth-old", "eth-old:1")
        assert flushed_events == []


@pytest.mark.skipif(not hasattr(Interface, "channels"), reason="NetBox before 4.7 has no channelized interfaces")
@pytest.mark.django_db
def test_a_model_with_channels_but_without_the_channel_rename_of_netbox_fails_the_sync(client, monkeypatch):
    """The check mirrors NetBox's rename rule, so a NetBox that renames the channel children in another way stops it."""
    from dcim.models import mixins

    device = make_device("written-scope-channel-rule")
    parent = bound_interface(device, "eth-old", 1, iface_type="1000base-t")
    parent.channels = 4
    parent.save()
    Interface.objects.create(device=device, name="eth-old:1", parent=parent, channel_id=1, type="1000base-t")
    seed_ports(device, [sync_port(1, "eth-new")])
    client.force_login(_user("written-scope-channel-rule-user", Device, Interface))
    monkeypatch.setattr(mixins, "InterfaceChannelRenameMixin", type("OtherChannelRename", (), {}))

    with pytest.raises(RuntimeError, match="channel"):
        _post_sync(client, device, "device", [1], exclude_columns=("vlans", "type"))

    assert Interface.objects.get(pk=parent.pk).name == "eth-old"


# ---------------------------------------------------------------------------
# The collection of the written rows
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_collection_records_a_change_of_the_tagged_vlans_from_the_vlan_side():
    from netbox_librenms_plugin.interface_sync import collect_interface_writes

    device = make_device("written-scope-reverse")
    added, cleared = make_interface(device, "eth0"), make_interface(device, "eth1")
    vlan = VLAN.objects.create(vid=100, name="written-scope-reverse")
    vlan.interfaces_as_tagged.add(cleared)

    user = make_user_with_perms("written-scope-reverse-user", [])
    with collect_interface_writes(user) as writes:
        vlan.interfaces_as_tagged.add(added)
    with collect_interface_writes(user) as clear_writes:
        vlan.interfaces_as_tagged.clear()

    assert (writes.written, writes.created) == ({Interface: {added.pk}}, {})
    assert clear_writes.written == {Interface: {added.pk, cleared.pk}}


# ---------------------------------------------------------------------------
# The IP tab creates a missing interface with the same rule
# ---------------------------------------------------------------------------


def _change_records(interface_model, name):
    return ObjectChange.objects.filter(changed_object_type__model=interface_model._meta.model_name, object_repr=name)


def _post_ip_sync_creating_the_interface(client, device, address, port_id, name):
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncTab, sync_snapshot_key

    cache.set(
        sync_snapshot_key(device, TAB_SPECS[SyncTab.IP_ADDRESSES].data_type, SERVER_KEY),
        {
            "ip_addresses": [
                {"ip_address": address, "prefix_length": 24, "ip_with_mask": f"{address}/24", "port_id": port_id}
            ],
            "mgmt_ip": "",
            "ports_by_id": {port_id: sync_port(port_id, name, alias="secret uplink")},
            "interface_name_field": "ifName",
        },
        timeout=300,
    )
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_device_ip_addresses", kwargs={"object_type": "device", "pk": device.pk}
    )
    data = {
        "server_key": SERVER_KEY,
        "create-missing-interfaces-toggle": "on",
        "select": [f"{address}/24"],
        f"vrf_{address}/24": "",
    }
    return client.post(url, data)


@pytest.mark.django_db
@pytest.mark.parametrize("refused_action", ["add", "change"])
def test_the_ip_tab_refuses_a_new_interface_outside_a_constrained_scope(client, refused_action):
    device = make_device(f"created-scope-ip-{refused_action}", librenms_cf={SERVER_KEY: {"id": 44}})
    user = _user(f"created-scope-ip-{refused_action}-user", Device, Interface, **{refused_action: IN_SCOPE})
    user = grant(user, "add", IPAddress)
    client.force_login(grant(user, "change", IPAddress))

    response = _post_ip_sync_creating_the_interface(client, device, "198.18.30.10", 7030, "private0")

    assert messages_on(response.wsgi_request) == [
        (
            "error",
            "Failed to sync IP addresses: 198.18.30.10/24 "
            f"(The new NetBox interface is outside your {refused_action} scope.)",
        )
    ]
    assert not Interface.objects.filter(device=device).exists()
    assert not IPAddress.objects.filter(address="198.18.30.10/24").exists()
    assert not _change_records(Interface, "private0").exists()


@pytest.mark.django_db
def test_the_ip_tab_creates_a_new_interface_inside_the_constrained_scopes(client):
    device = make_device("created-scope-ip-inside", librenms_cf={SERVER_KEY: {"id": 45}})
    user = _user("created-scope-ip-inside-user", Device, Interface, add=IN_SCOPE, change=IN_SCOPE)
    user = grant(user, "add", IPAddress)
    client.force_login(grant(user, "change", IPAddress))

    response = _post_ip_sync_creating_the_interface(client, device, "198.18.31.10", 7031, "eth0")

    assert [level for level, _ in messages_on(response.wsgi_request)] == ["success"]
    interface = Interface.objects.get(device=device)
    assert (interface.name, interface.description) == ("eth0", "secret uplink")
    assert IPAddress.objects.get(address="198.18.31.10/24").assigned_object == interface
