"""
A row that a sync creates must be in the user's add scope and change scope, as the sync wrote it.

The tests post the interface sync and the IP sync with real constrained object permissions. A
refused row is rolled back with its change records, and the other rows still sync.
"""

import pytest
from core.models import ObjectChange
from dcim.models import Device, Interface
from django.urls import reverse
from ipam.models import VLAN, IPAddress, VLANGroup
from virtualization.models import VirtualMachine, VMInterface

from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_vm
from netbox_librenms_plugin.tests.interface_sync_post_helpers import SERVER_KEY, SYNCED, seed_ports, sync_port
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms, messages_on
from netbox_librenms_plugin.utils import get_librenms_device_id

pytestmark = pytest.mark.django_db

IN_SCOPE = {"name__startswith": "eth"}
OWNERS = pytest.mark.parametrize(
    "object_type, owner_model, interface_model",
    [("device", Device, Interface), ("virtualmachine", VirtualMachine, VMInterface)],
    ids=["device", "vm"],
)


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


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


def _post_sync(client, owner, object_type, port_ids, *, exclude_columns=("mac_address",)):
    """Post the interface sync of *owner* for *port_ids*; the VLANs are synced unless excluded."""
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
    return client.post(url, data)


def _change_records(interface_model, name):
    return ObjectChange.objects.filter(changed_object_type__model=interface_model._meta.model_name, object_repr=name)


@OWNERS
@pytest.mark.parametrize("refused_action", ["add", "change"])
def test_a_new_row_outside_a_constrained_scope_is_rolled_back_and_the_other_rows_sync(
    client, object_type, owner_model, interface_model, refused_action
):
    owner = _owner(object_type, f"created-scope-{object_type}-{refused_action}")
    seed_ports(owner, [sync_port(10, "eth0", alias="uplink"), sync_port(11, "private0", alias="secret uplink")])
    client.force_login(
        _user(
            f"created-scope-{object_type}-{refused_action}-user",
            owner_model,
            interface_model,
            **{refused_action: IN_SCOPE},
        )
    )

    response = _post_sync(client, owner, object_type, [10, 11])

    assert messages_on(response.wsgi_request) == [
        ("warning", f"1 interface(s) skipped: private0 (new interface outside your {refused_action} scope)."),
        ("success", SYNCED),
    ]
    interfaces = _interfaces(owner)
    assert set(interfaces) == {"eth0"}
    assert interfaces["eth0"].description == "uplink"
    assert get_librenms_device_id(interfaces["eth0"], SERVER_KEY, auto_save=False) == 10
    assert not _change_records(interface_model, "private0").exists()
    assert _change_records(interface_model, "eth0").exists()


def test_the_add_scope_is_checked_on_the_row_as_the_sync_wrote_it(client):
    """A new row is empty before the write, so a check of the empty row would refuse ``eth0`` and allow ``eth1``."""
    device = _owner("device", "created-scope-written-row")
    seed_ports(device, [sync_port(10, "eth0", alias="uplink"), sync_port(11, "eth1", alias="other")])
    client.force_login(
        _user("created-scope-written-row-user", Device, Interface, add={"description": "uplink", "mtu": 1500})
    )

    response = _post_sync(client, device, "device", [10, 11])

    assert messages_on(response.wsgi_request) == [
        ("warning", "1 interface(s) skipped: eth1 (new interface outside your add scope)."),
        ("success", SYNCED),
    ]
    assert set(_interfaces(device)) == {"eth0"}


def test_the_add_scope_is_checked_after_the_vlan_write(client):
    """Only the VLAN write sets the mode, so a check before it would refuse ``eth0``."""
    device = _owner("device", "created-scope-vlan")
    VLAN.objects.create(vid=100, name="created-scope-vlan")
    seed_ports(device, [sync_port(10, "eth0", untagged_vlan=100, tagged_vlans=[]), sync_port(11, "eth1")])
    client.force_login(_user("created-scope-vlan-user", Device, Interface, add={"mode": "access"}))

    response = _post_sync(client, device, "device", [10, 11])

    assert messages_on(response.wsgi_request) == [
        ("warning", "1 interface(s) skipped: eth1 (new interface outside your add scope)."),
        ("success", SYNCED),
    ]
    interfaces = _interfaces(device)
    assert set(interfaces) == {"eth0"}
    assert interfaces["eth0"].untagged_vlan.vid == 100


# ---------------------------------------------------------------------------
# The IP tab creates a missing interface with the same check
# ---------------------------------------------------------------------------


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
