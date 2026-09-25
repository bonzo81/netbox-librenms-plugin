"""Integration tests for shared interface attribute and MAC synchronization."""

import pytest
from dcim.models import Interface

from netbox_librenms_plugin.interface_sync import assign_interface_mac
from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import bound_interface, post_interface_sync, seed_ports


def _post_sync(client, settings, device, port, *, exclude_columns):
    """Seed *port* for *device* and post the sync of that one row through the real URL, as a superuser."""
    configure_default_librenms_server(settings)
    client.force_login(make_superuser(f"{device.name}-user"))
    seed_ports(device, [port])
    return post_interface_sync(client, device, [port["port_id"]], htmx=False, exclude_columns=exclude_columns)


@pytest.mark.django_db
class TestUpdateInterfaceAttributes:
    """The interface writer must persist the real NetBox model state."""

    def test_updates_fields_and_stable_port_identity(self, client, settings):
        from netbox_librenms_plugin.models import InterfaceTypeMapping
        from netbox_librenms_plugin.utils import get_librenms_device_id

        interface = bound_interface(make_device("interface-fields"), "old-name", 77)
        InterfaceTypeMapping.objects.create(librenms_type="ethernetCsmacd", netbox_type="1000base-t")

        _post_sync(
            client,
            settings,
            interface.device,
            {
                "ifName": "eth0",
                "ifDescr": "eth0",
                "ifType": "ethernetCsmacd",
                "ifSpeed": 1_000_000_000,
                "ifAlias": "uplink",
                "ifMtu": 1500,
                "ifAdminStatus": "down",
                "port_id": 77,
            },
            exclude_columns=("vlans",),
        )

        interface.refresh_from_db()
        assert interface.name == "eth0"
        assert interface.type == "1000base-t"
        assert interface.speed == 1_000_000
        assert interface.description == "uplink"
        assert interface.mtu == 1500
        assert interface.enabled is False
        assert get_librenms_device_id(interface, "default", auto_save=False) == 77

    def test_excluded_fields_and_mac_remain_unchanged(self, client, settings):
        from dcim.models import MACAddress

        interface = bound_interface(make_device("interface-exclusions"), "keep-name", 1, iface_type="1000base-t")
        interface.speed = 1000
        interface.description = "keep-description"
        interface.mtu = 9000
        interface.enabled = True
        interface.save()

        _post_sync(
            client,
            settings,
            interface.device,
            {
                "ifName": "new-name",
                "ifDescr": "new-name",
                "ifType": "ethernetCsmacd",
                "ifSpeed": 1_000_000_000,
                "ifAlias": "new-description",
                "ifMtu": 1500,
                "ifAdminStatus": "down",
                "ifPhysAddress": "aa:bb:cc:dd:ee:ff",
                "port_id": 1,
            },
            exclude_columns=("name", "type", "speed", "description", "mtu", "enabled", "mac_address", "vlans"),
        )

        interface.refresh_from_db()
        assert (interface.name, interface.type, interface.speed) == ("keep-name", "1000base-t", 1000)
        assert (interface.description, interface.mtu, interface.enabled) == ("keep-description", 9000, True)
        assert not MACAddress.objects.exists()


@pytest.mark.django_db
class TestAssignInterfaceMac:
    """
    assign_interface_mac() must work for both Interface and VMInterface. Both carry
    primary_mac_address in the NetBox versions this plugin supports."""

    def test_creates_new_mac_and_adds_to_interface(self):
        from dcim.models import MACAddress

        iface = make_interface(make_device("mac-create"), "Gi0/1")

        assign_interface_mac(iface, "aa:bb:cc:dd:ee:ff")

        mac = MACAddress.objects.get(mac_address="aa:bb:cc:dd:ee:ff")
        assert list(iface.mac_addresses.all()) == [mac]

    def test_reuses_existing_mac(self):
        """The already-attached MAC is reused AND promoted to primary, not re-created."""
        from dcim.models import Interface, MACAddress

        iface = make_interface(make_device("mac-reuse"), "Gi0/1")
        existing = MACAddress.objects.create(mac_address="aa:bb:cc:dd:ee:ff")
        iface.mac_addresses.add(existing)
        assert iface.primary_mac_address is None  # the branch has done nothing yet

        assign_interface_mac(iface, "aa:bb:cc:dd:ee:ff")
        iface.save()

        assert MACAddress.objects.filter(mac_address="aa:bb:cc:dd:ee:ff").count() == 1
        assert list(iface.mac_addresses.all()) == [existing]
        # Without this the test would pass on an early return: the m2m link predates the call.
        assert Interface.objects.get(pk=iface.pk).primary_mac_address == existing

    def test_sets_primary_mac_when_attribute_present(self):
        from dcim.models import Interface, MACAddress

        iface = make_interface(make_device("mac-primary"), "Gi0/1")

        assign_interface_mac(iface, "aa:bb:cc:dd:ee:ff")
        iface.save()

        mac = MACAddress.objects.get(mac_address="aa:bb:cc:dd:ee:ff")
        assert Interface.objects.get(pk=iface.pk).primary_mac_address == mac

    def test_vm_interface_also_gets_its_primary_mac_set(self):
        """VMInterface carries primary_mac_address in this NetBox version, same as Interface.

        The old mock built the VM interface with ``spec=["mac_addresses"]``, fabricating an
        absence NetBox no longer has, so it pinned a fact that had stopped being true. The
        writer no longer checks for the attribute.
        """
        from dcim.models import MACAddress
        from virtualization.models import VMInterface

        vm = make_vm("mac-vm")
        vmiface = VMInterface.objects.create(virtual_machine=vm, name="eth0")

        assign_interface_mac(vmiface, "aa:bb:cc:dd:ee:ff")
        vmiface.save()

        mac = MACAddress.objects.get(mac_address="aa:bb:cc:dd:ee:ff")
        assert list(vmiface.mac_addresses.all()) == [mac]
        assert VMInterface.objects.get(pk=vmiface.pk).primary_mac_address == mac

    def test_noop_when_mac_address_is_falsy(self):
        from dcim.models import MACAddress

        iface = make_interface(make_device("mac-falsy"), "Gi0/1")

        assign_interface_mac(iface, "")
        assign_interface_mac(iface, None)

        assert not MACAddress.objects.exists()
        assert not iface.mac_addresses.exists()


@pytest.mark.django_db
def test_interface_delete_does_not_expose_database_error_details(client):
    """A failed delete does not roll back an earlier success or expose details."""
    from django.db import DatabaseError, connection
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_superuser

    device = make_device("interface-delete-error")
    deleted_interface = make_interface(device, "Ethernet1")
    failed_interface = make_interface(device, "Ethernet2")
    client.force_login(make_superuser("interface-delete-error-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:delete_netbox_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    sensitive_detail = "private database constraint detail"

    def fail_interface_delete(execute, sql, params, many, context):
        if (
            sql.lstrip().upper().startswith("DELETE")
            and '"dcim_interface"' in sql
            and failed_interface.pk in (params or ())
        ):
            raise DatabaseError(sensitive_detail)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_interface_delete):
        response = client.post(
            url,
            {"interface_ids": [str(deleted_interface.pk), str(failed_interface.pk)]},
        )

    assert response.status_code == 200
    assert sensitive_detail.encode() not in response.content
    assert response.json()["deleted_count"] == 1
    assert response.json()["errors"] == ["Error deleting interface Ethernet2. Check server logs."]
    assert not type(deleted_interface).objects.filter(pk=deleted_interface.pk).exists()
    assert type(failed_interface).objects.filter(pk=failed_interface.pk).exists()


@pytest.mark.django_db
def test_interface_delete_counts_only_committed_savepoints(client):
    """A savepoint release failure does not count or persist the deletion."""
    from django.db import DatabaseError, connection
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_superuser

    device = make_device("interface-delete-savepoint-error")
    interface = make_interface(device, "Ethernet1")
    client.force_login(make_superuser("interface-delete-savepoint-error-user"))
    url = reverse(
        "plugins:netbox_librenms_plugin:delete_netbox_interfaces",
        kwargs={"object_type": "device", "object_id": device.pk},
    )
    delete_executed = False
    release_failed = False

    def fail_savepoint_release(execute, sql, params, many, context):
        nonlocal delete_executed, release_failed
        normalized_sql = sql.lstrip().upper()
        if normalized_sql.startswith("DELETE") and '"DCIM_INTERFACE"' in normalized_sql:
            delete_executed = True
        if delete_executed and not release_failed and normalized_sql.startswith("RELEASE SAVEPOINT"):
            release_failed = True
            raise DatabaseError("private savepoint failure detail")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_savepoint_release):
        response = client.post(url, {"interface_ids": [str(interface.pk)]})

    assert response.status_code == 200
    assert response.json()["deleted_count"] == 0
    assert response.json()["errors"] == ["Error deleting interface Ethernet1. Check server logs."]
    assert type(interface).objects.filter(pk=interface.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("mac", [True, 123, ["aa:bb:cc:dd:ee:ff"], {"value": "aa:bb:cc:dd:ee:ff"}])
def test_interface_update_ignores_non_string_mac(mac):
    """Malformed MAC data must not prevent the remaining interface update."""
    from dcim.models import MACAddress
    from netbox_librenms_plugin.interface_rules import InterfaceRuleMatcher
    from netbox_librenms_plugin.interface_sync import update_interface_from_port

    interface = make_interface(make_device("malformed-mac"), "eth0")
    update_interface_from_port(
        interface,
        {
            "ifName": "eth0",
            "ifDescr": "eth0",
            "ifType": "ethernetCsmacd",
            "ifSpeed": None,
            "ifAlias": "updated description",
            "ifPhysAddress": mac,
        },
        rules=InterfaceRuleMatcher.load(),
        synced_name="eth0",
        server_key="default",
        interface_name_field="ifName",
        created=False,
        fresh_read_queryset=Interface.objects.all(),
    )
    interface.refresh_from_db()
    assert interface.description == "updated description"
    assert interface.primary_mac_address_id is None
    assert not MACAddress.objects.exists()
