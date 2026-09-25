"""
The interface sync saves only when every row that it wrote is in the user's scope, as NetBox's edit views do.

The tests post the interface sync with real constrained object permissions. Each row that the sync
created or changed must be in the user's change scope, and each row that it created also in the add
scope. The check reads the rows after the last write of the sync, the relationship pass too. A row
outside the scope refuses the whole sync: nothing is saved, and NetBox sends no event. The IP tab
creates a missing interface with the same rule, and refuses only that address.
"""

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
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms, messages_on
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

IN_SCOPE = {"name__startswith": "eth"}
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


def _user(username, owner_model, interface_model, *, add=None, change=None):
    """Return a user who may view the owner, the interfaces and the VLANs, with the given add and change scopes."""
    user = make_user_with_perms(
        username, [("view", owner_model), ("view", interface_model), ("view", VLAN), ("view", VLANGroup)]
    )
    user = grant(user, "add", interface_model, constraints=add)
    return grant(user, "change", interface_model, constraints=change)


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

    with collect_interface_writes() as writes:
        vlan.interfaces_as_tagged.add(added)
    with collect_interface_writes() as clear_writes:
        vlan.interfaces_as_tagged.clear()

    assert (writes.written, writes.created) == ({Interface: {added.pk}}, {})
    assert clear_writes.written == {Interface: {added.pk, cleared.pk}}


@pytest.mark.django_db
def test_a_refused_row_that_no_write_path_named_is_a_defect():
    """Each write path of the sync names the rows that it writes, so the message can name a refused row."""
    from netbox_librenms_plugin.interface_sync import collect_interface_writes

    device = make_device("written-scope-unnamed")
    with collect_interface_writes() as writes:
        make_interface(device, "eth0")

    with pytest.raises(RuntimeError, match="no name"):
        writes.outside_scope(make_user_with_perms("written-scope-unnamed-user", []))


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
