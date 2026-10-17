"""Who holds a host row's reported name, how the tab reports it, and the stale-binding Rebind action."""

import re

import pytest
from dcim.models import Cable, Device, Interface
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from virtualization.models import VMInterface

from netbox_librenms_plugin.constants import (
    NAME_OWNER_LIVE,
    NAME_OWNER_STALE,
    NAME_OWNER_UNKNOWN,
    REPORTED_NAME_PORT_COLLISION_REASON,
)
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_cluster,
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.utils import (
    get_librenms_device_id,
    reported_name_owners,
    set_librenms_device_id,
    synced_interface_names,
)
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView


SERVER_KEY = "default"
HOST_PORT = 9001
STALE_PORT = 8679


def _port(port_id, name, *, source=None):
    port = {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifType": "ethernetCsmacd",
        "ifAdminStatus": "up",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifPhysAddress": "",
        "ifAlias": "",
    }
    if source is not None:
        port["_source"] = source
    return port


def _seed(owner, ports, *, oob_incomplete=False):
    payload = {"ports": ports, "port_stack_relationships": {}}
    if oob_incomplete:
        payload["oob_incomplete"] = True
    cache.set(SyncInterfacesView().get_cache_key(owner, "ports", SERVER_KEY), payload, timeout=300)


def _bound_interface(owner, name, port_id):
    if isinstance(owner, Device):
        interface = make_interface(owner, name)
    else:
        interface = VMInterface.objects.create(virtual_machine=owner, name=name)
    set_librenms_device_id(interface, port_id, SERVER_KEY)
    interface.save()
    return interface


def _binding(interface):
    interface.refresh_from_db()
    return get_librenms_device_id(interface, SERVER_KEY, auto_save=False)


def _device(name):
    return make_device(name, librenms_cf={SERVER_KEY: {"id": 71}})


def _rebind(client, owner, port_id, *, expected=STALE_PORT, object_type="device", extra=None):
    data = {"server_key": SERVER_KEY, "rebind_one": str(port_id), **(extra or {})}
    if expected is not None:
        data[f"rebind_expected_port_{port_id}"] = str(expected)
    return client.post(
        reverse(
            "plugins:netbox_librenms_plugin:rebind_interface_port",
            kwargs={"object_type": object_type, "object_id": owner.pk},
        )
        + "?interface_name_field=ifName",
        data,
    )


def _sync(client, device, port_id):
    return client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_selected_interfaces",
            kwargs={"object_type": "device", "object_id": device.pk},
        ),
        {
            "server_key": SERVER_KEY,
            "interface_name_field": "ifName",
            "select": [str(port_id)],
            "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
        },
    )


def _messages(response, level_tag=None):
    return [
        str(message)
        for message in get_messages(response.wsgi_request)
        if level_tag is None or message.level_tag == level_tag
    ]


def _row_html(device, client, port_id):
    url = reverse(
        "plugins:netbox_librenms_plugin:sync_cache_fragment",
        kwargs={"object_type": "device", "pk": device.pk, "tab": "interfaces"},
    )
    response = client.get(url, {"server_key": SERVER_KEY, "interface_name_field": "ifName"})
    assert response.status_code == 200
    rows = re.findall(r"<tr[^>]*>.*?</tr>", response.content.decode(), flags=re.S)
    matches = [row for row in rows if f'data-port-id="{port_id}"' in row]
    assert len(matches) == 1
    return matches[0]


def _owners(ports, reserved, *, snapshot_complete=True):
    target_device_ids = {int(port["port_id"]): 1 for port in ports}
    names, rejected = synced_interface_names(
        ports,
        "ifName",
        target_device_ids=target_device_ids,
        reserved_name_port_ids_by_device={1: reserved},
    )
    return reported_name_owners(
        ports,
        "ifName",
        names,
        rejected,
        target_device_ids=target_device_ids,
        reserved_name_port_ids_by_device={1: reserved},
        snapshot_complete=snapshot_complete,
    ), rejected


class TestReportedNameOwners:
    """The one classification the table and the Rebind endpoint both read."""

    def test_a_port_the_complete_snapshot_no_longer_reports_is_stale(self):
        owners, rejected = _owners([_port(HOST_PORT, "eth0")], {"eth0": {STALE_PORT}})

        assert rejected[HOST_PORT] == REPORTED_NAME_PORT_COLLISION_REASON
        assert owners[HOST_PORT].port_id == STALE_PORT
        assert owners[HOST_PORT].status == NAME_OWNER_STALE

    def test_a_port_the_snapshot_reports_is_live_and_names_its_row(self):
        ports = [_port(HOST_PORT, "eth0"), _port(STALE_PORT, "eth0", source="oob")]

        owners, _rejected = _owners(ports, {"eth0": {STALE_PORT}})

        owner = owners[HOST_PORT]
        assert owner.status == NAME_OWNER_LIVE
        assert (owner.port_id, owner.owner_row_name, owner.owner_row_is_oob) == (STALE_PORT, "eth0", True)
        assert owner.owner_synced_name == "eth0-oob"

    def test_absence_from_an_incomplete_snapshot_proves_nothing(self):
        owners, _rejected = _owners([_port(HOST_PORT, "eth0")], {"eth0": {STALE_PORT}}, snapshot_complete=False)

        assert owners[HOST_PORT].status == NAME_OWNER_UNKNOWN

    def test_a_row_whose_name_is_free_has_no_owner(self):
        owners, rejected = _owners([_port(HOST_PORT, "eth0")], {"eth1": {STALE_PORT}})

        assert owners == {}
        assert rejected == {}


@pytest.mark.django_db
class TestTheTabReportsTheHolder:
    """The real interfaces tab, rendered from a seeded snapshot."""

    def test_a_stale_holder_is_named_and_offers_rebind_instead_of_sync(self, client, settings):
        configure_default_librenms_server(settings)
        device = _device("name-owner-stale-tab")
        _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, [_port(HOST_PORT, "eth0")])
        client.force_login(make_superuser("name-owner-stale-tab-user"))

        row = _row_html(device, client, HOST_PORT)

        assert f"Name held by port {STALE_PORT}" in row
        assert f'name="rebind_one" value="{HOST_PORT}"' in row
        assert f'name="rebind_expected_port_{HOST_PORT}" value="{STALE_PORT}"' in row
        rebind_url = reverse(
            "plugins:netbox_librenms_plugin:rebind_interface_port",
            kwargs={"object_type": "device", "object_id": device.pk},
        )
        assert f'formaction="{rebind_url}?interface_name_field=ifName"' in row
        assert "data-confirm=" in row
        assert 'name="sync_one"' not in row

    def test_a_live_holder_is_named_and_offers_no_rebind(self, client, settings):
        configure_default_librenms_server(settings)
        device = _device("name-owner-live-tab")
        _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, [_port(HOST_PORT, "eth0"), _port(STALE_PORT, "eth0", source="oob")])
        client.force_login(make_superuser("name-owner-live-tab-user"))

        row = _row_html(device, client, HOST_PORT)

        assert f"Name held by port {STALE_PORT}" in row
        assert "eth0-oob" in row
        assert 'name="rebind_one"' not in row

    def test_an_incomplete_oob_inventory_offers_no_rebind(self, client, settings):
        configure_default_librenms_server(settings)
        device = _device("name-owner-unknown-tab")
        _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, [_port(HOST_PORT, "eth0")], oob_incomplete=True)
        client.force_login(make_superuser("name-owner-unknown-tab-user"))

        row = _row_html(device, client, HOST_PORT)

        assert f"Name held by port {STALE_PORT}" in row
        assert 'name="rebind_one"' not in row

    def test_a_holder_outside_the_view_scope_is_not_named(self, client, settings):
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        configure_default_librenms_server(settings)
        device = _device("name-owner-hidden-tab")
        _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, [_port(HOST_PORT, "eth0")])
        user = make_user_with_perms("name-owner-hidden-tab-user", [("view", Device)])
        user = grant(user, "view", Interface, constraints={"name": "not-eth0"})
        client.force_login(user)

        row = _row_html(device, client, HOST_PORT)

        assert str(STALE_PORT) not in row
        assert 'name="rebind_one"' not in row


@pytest.mark.django_db
class TestRebind:
    """The Rebind POST re-derives every precondition and moves only the LibreNMS binding."""

    def test_rebind_moves_only_the_binding(self, client, settings):
        from core.models import ObjectChange

        configure_default_librenms_server(settings)
        device = _device("name-owner-rebind")
        interface = _bound_interface(device, "eth0", STALE_PORT)
        ip = make_ip("192.0.2.10/24", assigned_object=interface)
        peer = make_interface(make_device("name-owner-rebind-peer"), "eth9")
        cable = Cable(a_terminations=[interface], b_terminations=[peer])
        cable.save()
        _seed(device, [_port(HOST_PORT, "eth0")])
        client.force_login(make_superuser("name-owner-rebind-user"))

        response = _rebind(client, device, HOST_PORT)

        assert response.status_code == 302
        assert _binding(interface) == HOST_PORT
        assert Interface.objects.filter(device=device).count() == 1
        assert Interface.objects.get(device=device, name="eth0").pk == interface.pk
        ip.refresh_from_db()
        assert ip.assigned_object == interface
        assert interface.cable_id == cable.pk
        successes = _messages(response, "success")
        assert len(successes) == 1
        assert str(HOST_PORT) in successes[0] and str(STALE_PORT) in successes[0]
        assert ObjectChange.objects.filter(
            changed_object_id=interface.pk,
            changed_object_type__model="interface",
            action="update",
        ).exists()

        row = _row_html(device, client, HOST_PORT)
        assert "Name held by port" not in row
        assert 'name="rebind_one"' not in row

    def test_rebind_locks_the_holding_interface(self, client, settings):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        configure_default_librenms_server(settings)
        device = _device("name-owner-rebind-lock")
        interface = _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, [_port(HOST_PORT, "eth0")])
        client.force_login(make_superuser("name-owner-rebind-lock-user"))

        with CaptureQueriesContext(connection) as queries:
            _rebind(client, device, HOST_PORT)

        assert _binding(interface) == HOST_PORT
        assert any(
            '"dcim_interface"' in query["sql"] and "FOR UPDATE" in query["sql"].upper()
            for query in queries.captured_queries
        )

    @pytest.mark.parametrize(
        ("ports", "oob_incomplete"),
        [
            pytest.param([_port(HOST_PORT, "eth0"), _port(STALE_PORT, "eth0", source="oob")], False, id="live"),
            pytest.param([_port(HOST_PORT, "eth0")], True, id="unknown"),
            pytest.param([_port(HOST_PORT + 1, "eth1")], False, id="row-not-in-snapshot"),
        ],
    )
    def test_rebind_is_refused_unless_the_holder_is_stale(self, client, settings, ports, oob_incomplete):
        configure_default_librenms_server(settings)
        device = _device("name-owner-rebind-refused")
        interface = _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, ports, oob_incomplete=oob_incomplete)
        client.force_login(make_superuser("name-owner-rebind-refused-user"))

        response = _rebind(client, device, HOST_PORT)

        assert response.status_code == 302
        assert _binding(interface) == STALE_PORT
        assert len(_messages(response, "danger")) == 1
        assert _messages(response, "success") == []

    def test_rebind_is_refused_when_the_row_port_is_already_bound(self, client, settings):
        configure_default_librenms_server(settings)
        device = _device("name-owner-rebind-row-bound")
        interface = _bound_interface(device, "eth0", STALE_PORT)
        other = _bound_interface(device, "eth9", HOST_PORT)
        _seed(device, [_port(HOST_PORT, "eth0")])
        client.force_login(make_superuser("name-owner-rebind-row-bound-user"))

        response = _rebind(client, device, HOST_PORT)

        assert _binding(interface) == STALE_PORT
        assert _binding(other) == HOST_PORT
        assert len(_messages(response, "danger")) == 1

    @pytest.mark.parametrize(
        "new_binding",
        [STALE_PORT + 1, None, HOST_PORT + 1],
        ids=["another-stale-port", "unbound", "live"],
    )
    def test_rebind_is_refused_when_the_binding_changed_after_render(self, client, settings, new_binding):
        configure_default_librenms_server(settings)
        device = _device("name-owner-rebind-changed")
        interface = _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, [_port(HOST_PORT, "eth0"), _port(HOST_PORT + 1, "eth1")])
        client.force_login(make_superuser("name-owner-rebind-changed-user"))
        row = _row_html(device, client, HOST_PORT)
        assert f'name="rebind_expected_port_{HOST_PORT}" value="{STALE_PORT}"' in row
        interface.custom_field_data["librenms_id"] = {} if new_binding is None else {SERVER_KEY: new_binding}
        interface.save()

        response = _rebind(client, device, HOST_PORT, expected=STALE_PORT)

        assert _binding(interface) == new_binding
        assert len(_messages(response, "danger")) == 1
        assert _messages(response, "success") == []

    @pytest.mark.parametrize("expected", [None, "not-a-port"], ids=["missing", "invalid"])
    def test_rebind_is_refused_without_the_rendered_holder_port(self, client, settings, expected):
        configure_default_librenms_server(settings)
        device = _device("name-owner-rebind-no-expected")
        interface = _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, [_port(HOST_PORT, "eth0")])
        client.force_login(make_superuser("name-owner-rebind-no-expected-user"))

        response = _rebind(client, device, HOST_PORT, expected=expected)

        assert _binding(interface) == STALE_PORT
        assert len(_messages(response, "danger")) == 1

    def test_rebind_is_refused_when_a_vm_interface_holds_the_row_port(self, client, settings):
        configure_default_librenms_server(settings)
        device = _device("name-owner-rebind-cross-model")
        interface = _bound_interface(device, "eth0", STALE_PORT)
        vm_interface = _bound_interface(make_vm("name-owner-rebind-cross-model-vm"), "eth0", HOST_PORT)
        _seed(device, [_port(HOST_PORT, "eth0")])
        client.force_login(make_superuser("name-owner-rebind-cross-model-user"))

        response = _rebind(client, device, HOST_PORT)

        assert _binding(interface) == STALE_PORT
        assert _binding(vm_interface) == HOST_PORT
        assert len(_messages(response, "danger")) == 1

    @pytest.mark.parametrize("holder_row", [None, _port(STALE_PORT, "eth0", source="oob")], ids=["stale", "live"])
    def test_a_holder_outside_the_view_scope_is_refused_without_naming_its_port(self, client, settings, holder_row):
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        configure_default_librenms_server(settings)
        device = _device("name-owner-rebind-hidden")
        interface = _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, [_port(HOST_PORT, "eth0"), *([holder_row] if holder_row else [])])
        user = make_user_with_perms("name-owner-rebind-hidden-user", [("view", Device), ("change", Interface)])
        user = grant(user, "view", Interface, constraints={"name": "not-eth0"})
        client.force_login(user)

        response = _rebind(client, device, HOST_PORT)

        assert _binding(interface) == STALE_PORT
        refusals = _messages(response, "danger")
        assert len(refusals) == 1
        assert str(STALE_PORT) not in refusals[0]
        assert str(STALE_PORT).encode() not in response.content

    def test_rebind_is_refused_for_a_migrated_chassis_member(self, client, settings):
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        configure_default_librenms_server(settings)
        _chassis, (viewed_member, target_member) = make_virtual_chassis_members("name-owner-rebind-migrated")
        set_librenms_device_id(viewed_member, 86, SERVER_KEY)
        viewed_member.save()
        winner = make_device("name-owner-rebind-migrated-winner")
        mark_librenms_migrated(target_member, winner.pk, SERVER_KEY)
        target_member.save()
        interface = _bound_interface(target_member, "eth0", STALE_PORT)
        _seed(viewed_member, [_port(HOST_PORT, "eth0")])
        client.force_login(make_superuser("name-owner-rebind-migrated-user"))

        response = _rebind(
            client,
            viewed_member,
            HOST_PORT,
            extra={f"device_selection_{HOST_PORT}": str(target_member.pk)},
        )

        assert _binding(interface) == STALE_PORT
        assert len(_messages(response, "danger")) == 1
        assert _messages(response, "success") == []

    def test_rebind_needs_change_permission_on_the_holding_interface(self, client, settings):
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        configure_default_librenms_server(settings)
        device = _device("name-owner-rebind-denied")
        interface = _bound_interface(device, "eth0", STALE_PORT)
        _seed(device, [_port(HOST_PORT, "eth0")])
        user = make_user_with_perms("name-owner-rebind-denied-user", [("view", Device), ("view", Interface)])
        user = grant(user, "change", Interface, constraints={"name": "not-eth0"})
        client.force_login(user)

        response = _rebind(client, device, HOST_PORT)

        assert _binding(interface) == STALE_PORT
        refusals = _messages(response, "danger")
        assert len(refusals) == 1
        assert str(STALE_PORT) not in refusals[0]

    def test_rebind_on_a_virtual_machine(self, client, settings):
        configure_default_librenms_server(settings)
        vm = make_vm("name-owner-rebind-vm", make_cluster("name-owner-rebind-cluster"))
        interface = _bound_interface(vm, "eth0", STALE_PORT)
        _seed(vm, [_port(HOST_PORT, "eth0")])
        client.force_login(make_superuser("name-owner-rebind-vm-user"))

        response = _rebind(client, vm, HOST_PORT, object_type="virtualmachine")

        assert response.status_code == 302
        assert _binding(interface) == HOST_PORT
        assert VMInterface.objects.get(virtual_machine=vm, name="eth0").pk == interface.pk


@pytest.mark.django_db
def test_a_live_holder_releases_the_name_after_its_own_row_syncs(client, settings):
    """An OOB port synced before the -oob suffix holds the bare name until its own row syncs."""
    configure_default_librenms_server(settings)
    device = _device("name-owner-live-two-step")
    legacy = _bound_interface(device, "eth0", STALE_PORT)
    _seed(device, [_port(HOST_PORT, "eth0"), _port(STALE_PORT, "eth0", source="oob")])
    client.force_login(make_superuser("name-owner-live-two-step-user"))

    blocked = _sync(client, device, HOST_PORT)

    assert any(f"{REPORTED_NAME_PORT_COLLISION_REASON} {STALE_PORT}" in text for text in _messages(blocked))
    assert not Interface.objects.filter(device=device, name="eth0").exclude(pk=legacy.pk).exists()

    _sync(client, device, STALE_PORT)
    legacy.refresh_from_db()
    assert (legacy.name, _binding(legacy)) == ("eth0-oob", STALE_PORT)

    _sync(client, device, HOST_PORT)
    host = Interface.objects.get(device=device, name="eth0")
    assert host.pk != legacy.pk
    assert _binding(host) == HOST_PORT


@pytest.mark.django_db
@pytest.mark.parametrize("viewable", [True, False], ids=["viewable", "hidden"])
def test_the_ip_path_names_the_holding_port_only_inside_the_view_scope(viewable):
    from netbox_librenms_plugin.interface_rules import InterfaceRuleMatcher
    from netbox_librenms_plugin.interface_sync import resolve_or_create_interface_from_port

    device = _device(f"name-owner-ip-path-{viewable}")
    holder = _bound_interface(device, "eth0", STALE_PORT)
    viewable_queryset = Interface.objects.all() if viewable else Interface.objects.exclude(pk=holder.pk)

    with pytest.raises(ValueError, match="already bound") as refusal:
        resolve_or_create_interface_from_port(
            device,
            _port(HOST_PORT, "eth0"),
            rules=InterfaceRuleMatcher.load(),
            server_key=SERVER_KEY,
            interface_name_field="ifName",
            changeable_queryset=Interface.objects.all(),
            viewable_queryset=viewable_queryset,
        )

    assert (str(STALE_PORT) in str(refusal.value)) is viewable
