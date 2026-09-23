"""A LibreNMS port bound on one interface model is never bound a second time on the other model."""

import pytest
from dcim.models import Interface
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from virtualization.models import VMInterface

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_cluster,
    make_device,
    make_interface,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.utils import (
    AmbiguousLibreNMSIdError,
    find_interface_by_librenms_port_id,
    get_librenms_device_id,
    set_librenms_device_id,
)
from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

SERVER_KEY = "default"
PORT = 9301

pytestmark = pytest.mark.django_db


def _port(port_id, name):
    return {
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


def _bind(interface, port_id):
    set_librenms_device_id(interface, port_id, SERVER_KEY)
    interface.save()
    return interface


def _binding(interface):
    interface.refresh_from_db()
    return get_librenms_device_id(interface, SERVER_KEY, auto_save=False)


def _vm_interface(name, port_id):
    vm = make_vm(f"{name}-vm", make_cluster(f"{name}-cluster"))
    return _bind(VMInterface.objects.create(virtual_machine=vm, name="eth0"), port_id)


def _sync(client, owner, object_type, port_id):
    cache.set(
        SyncInterfacesView().get_cache_key(owner, "ports", SERVER_KEY),
        {"ports": [_port(port_id, "eth0")], "port_stack_relationships": {}},
        timeout=300,
    )
    return client.post(
        reverse(
            "plugins:netbox_librenms_plugin:sync_selected_interfaces",
            kwargs={"object_type": object_type, "object_id": owner.pk},
        ),
        {
            "server_key": SERVER_KEY,
            "interface_name_field": "ifName",
            "select": [str(port_id)],
            "exclude_columns": ["vlans", "mac_address"],
        },
    )


def _warnings(response):
    return [str(message) for message in get_messages(response.wsgi_request) if message.level_tag == "warning"]


class TestTheSharedLookup:
    """One helper answers "who holds this port" across both interface models."""

    def test_a_vm_interface_is_found_for_a_port_no_device_interface_holds(self):
        vm_interface = _vm_interface("lookup-vm", PORT)

        assert find_interface_by_librenms_port_id(PORT, SERVER_KEY) == vm_interface

    def test_a_holder_on_each_model_is_ambiguous(self):
        _vm_interface("lookup-both", PORT)
        _bind(make_interface(make_device("lookup-both-device"), "eth0"), PORT)

        with pytest.raises(AmbiguousLibreNMSIdError):
            find_interface_by_librenms_port_id(PORT, SERVER_KEY)


class TestTheSyncWriter:
    """The interfaces tab sync refuses a row whose port the other model holds, and says so."""

    @pytest.mark.parametrize("same_name_exists", [False, True], ids=["create", "adopt"])
    def test_a_device_sync_does_not_bind_a_port_a_vm_interface_holds(self, client, settings, same_name_exists):
        configure_default_librenms_server(settings)
        vm_interface = _vm_interface(f"sync-device-{same_name_exists}", PORT)
        device = make_device(f"sync-device-{same_name_exists}", librenms_cf={SERVER_KEY: {"id": 71}})
        existing = make_interface(device, "eth0") if same_name_exists else None
        client.force_login(make_superuser(f"sync-device-{same_name_exists}-user"))

        response = _sync(client, device, "device", PORT)

        assert _binding(vm_interface) == PORT
        assert not Interface.objects.filter(device=device).exclude(pk=getattr(existing, "pk", None)).exists()
        if existing is not None:
            assert _binding(existing) is None
        assert any("eth0 (port already mapped elsewhere or ambiguous)" in text for text in _warnings(response))

    def test_a_vm_sync_does_not_bind_a_port_a_device_interface_holds(self, client, settings):
        configure_default_librenms_server(settings)
        device_interface = _bind(make_interface(make_device("sync-vm-holder"), "eth0"), PORT)
        vm = make_vm("sync-vm", make_cluster("sync-vm-cluster"))
        client.force_login(make_superuser("sync-vm-user"))

        response = _sync(client, vm, "virtualmachine", PORT)

        assert _binding(device_interface) == PORT
        assert not VMInterface.objects.filter(virtual_machine=vm).exists()
        assert any("eth0 (port already mapped elsewhere or ambiguous)" in text for text in _warnings(response))

    def test_the_field_writer_does_not_bind_a_port_the_other_model_holds(self):
        from netbox_librenms_plugin.interface_sync import update_interface_from_port

        vm_interface = _vm_interface("writer", PORT)
        interface = make_interface(make_device("writer-device"), "eth0")

        update_interface_from_port(
            interface,
            _port(PORT, "eth0"),
            synced_name="eth0",
            server_key=SERVER_KEY,
            interface_name_field="ifName",
        )

        assert _binding(interface) is None
        assert _binding(vm_interface) == PORT


class TestTheIPPathResolver:
    """The IP tab's resolve-or-create neither creates nor adopts an interface for a held port."""

    @pytest.mark.parametrize("same_name_exists", [False, True], ids=["create", "adopt"])
    def test_a_port_a_vm_interface_holds_is_refused(self, same_name_exists):
        from netbox_librenms_plugin.interface_sync import resolve_or_create_interface_from_port

        vm_interface = _vm_interface(f"resolve-{same_name_exists}", PORT)
        device = make_device(f"resolve-{same_name_exists}")
        existing = make_interface(device, "eth0") if same_name_exists else None

        with pytest.raises(ValueError, match="already assigned to another NetBox interface owner"):
            resolve_or_create_interface_from_port(
                device,
                _port(PORT, "eth0"),
                server_key=SERVER_KEY,
                interface_name_field="ifName",
                changeable_queryset=Interface.objects.all(),
                viewable_queryset=Interface.objects.all(),
            )

        assert list(Interface.objects.filter(device=device)) == ([existing] if existing else [])
        if existing is not None:
            assert _binding(existing) is None
        assert _binding(vm_interface) == PORT


def test_the_module_binder_does_not_bind_a_port_a_vm_interface_holds():
    from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

    vm_interface = _vm_interface("module-bind", PORT)
    device = make_device("module-bind-device")
    interface = make_interface(device, "Ethernet1/1")

    result = _bind_interface_librenms_id(
        device,
        {"_librenms_port_id": PORT, "_librenms_ifname": "Ethernet1/1"},
        None,
        SERVER_KEY,
        Interface.objects.all(),
    )

    assert result["status"] == "conflict"
    assert _binding(interface) is None
    assert _binding(vm_interface) == PORT


def _cable_scenario(librenms_server, settings, name, *, holder=None):
    """A page device, a modelled neighbour without the port, one seeded row, and an optional port holder."""
    from netbox_librenms_plugin.tests.conftest import (
        bind_librenms_server,
        configured_server_key,
        map_device_to_librenms,
        persist_test_server_mapping,
    )
    from netbox_librenms_plugin.tests.test_cable_remote_matching import _row, _seed_cable_row

    server_key = configured_server_key()
    bind_librenms_server(settings, librenms_server, server_key=server_key)
    local_device = make_device(f"{name}-local")
    local_interface = make_interface(local_device, "eth0")
    remote_device = make_device(f"{name}-remote")
    persist_test_server_mapping(local_device, server_key)
    map_device_to_librenms(remote_device, 9, server_key=server_key)
    librenms_server.register(
        "/api/v0/ports/500", {"status": "ok", "port": [{"port_id": 500, "ifName": "Gi0/1", "ifType": "ethernetCsmacd"}]}
    )
    if holder is not None:
        _hold_cable_port(name, holder, server_key)
    row_id = _seed_cable_row(
        local_device,
        _row(local_port="eth0", remote_device=remote_device.name, remote_port="Gi0/1", remote_port_key=500),
        server_key,
    )
    return server_key, local_device, local_interface, remote_device, row_id


def _hold_cable_port(name, holder, server_key):
    if holder == "vm-interface":
        vm = make_vm(f"{name}-vm", make_cluster(f"{name}-cluster"))
        held_by = VMInterface.objects.create(virtual_machine=vm, name="Gi0/1")
    else:
        held_by = make_interface(make_device(f"{name}-third"), "Gi0/1")
    set_librenms_device_id(held_by, 500, server_key)
    held_by.save()


def _post_cable_create(local_device, row_id, server_key, user_name):
    from netbox_librenms_plugin.tests.test_cable_remote_matching import _logged_in, _remote_create_url

    return _logged_in(make_superuser(user_name)).post(
        _remote_create_url(local_device), {"row_id": row_id, "server_key": server_key}, follow=True
    )


class TestTheCableFarEndCreate:
    """The offer and the endpoint share one rule: a port another interface holds gets neither."""

    @pytest.mark.parametrize("holder", ["vm-interface", "other-device"])
    def test_no_create_is_offered_for_a_port_another_interface_holds(self, holder):
        from netbox_librenms_plugin.tests.conftest import configured_server_key
        from netbox_librenms_plugin.tests.test_cable_remote_matching import _create_setup
        from netbox_librenms_plugin.tests.test_serial_cables_view import _make_view

        server_key, local_device, _, _, row = _create_setup(f"cable-offer-held-{holder}")
        view = _make_view()
        view._set_remote_create_affordance(row, local_device, configured_server_key())
        assert row.get("remote_create_url")
        _hold_cable_port(f"cable-offer-held-{holder}", holder, server_key)
        del row["remote_create_url"]

        view._set_remote_create_affordance(row, local_device, configured_server_key())

        assert row.get("remote_create_url") is None

    @pytest.mark.parametrize("holder", ["vm-interface", "other-device"])
    def test_the_endpoint_refuses_a_port_another_interface_holds(self, librenms_server, settings, holder):
        server_key, local_device, local_interface, remote_device, row_id = _cable_scenario(
            librenms_server, settings, f"cable-held-{holder}", holder=holder
        )

        _post_cable_create(local_device, row_id, server_key, f"cable-held-{holder}-user")

        assert not Interface.objects.filter(device=remote_device).exists()
        local_interface.refresh_from_db()
        assert local_interface.cable is None

    def test_a_holder_bound_after_the_offer_is_refused_at_post_time(self, librenms_server, settings, monkeypatch):
        from netbox_librenms_plugin.tests.test_cable_remote_matching import _messages
        from netbox_librenms_plugin.views.sync.cables import CableRemoteCreateView

        server_key, local_device, local_interface, remote_device, row_id = _cable_scenario(
            librenms_server, settings, "cable-held-late"
        )
        original = CableRemoteCreateView._remote_port_record

        def bind_the_port_after_the_offer(view, row):
            _hold_cable_port("cable-held-late", "vm-interface", server_key)
            return original(view, row)

        monkeypatch.setattr(CableRemoteCreateView, "_remote_port_record", bind_the_port_after_the_offer)

        response = _post_cable_create(local_device, row_id, server_key, "cable-held-late-user")

        assert not Interface.objects.filter(device=remote_device).exists()
        assert any("LibreNMS port 500 is already bound" in text for text in _messages(response))
