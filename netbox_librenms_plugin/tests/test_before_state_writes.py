"""
The interface sync records the before-state of every update of a row it created, and creates a MAC already assigned.

NetBox takes the before-state of an update from ``snapshot()``. A save without it gives a change
record with no before-state, so the change log shows no diff and a branch merge sees no conflict.
The tests post the interface sync end to end for a port that has no NetBox interface yet.
"""

import pytest
from core.models import ObjectChange
from dcim.models import Interface, MACAddress
from django.contrib.contenttypes.models import ContentType
from virtualization.models import VMInterface

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_superuser,
    make_vm,
    seed_own_mapping,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import (
    SERVER_KEY,
    SYNCED,
    post_interface_sync,
    seed_ports,
    sync_port,
)
from netbox_librenms_plugin.tests.view_test_helpers import messages_on

MAC = "00:11:22:33:44:55"


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


def _device_owner(tag):
    return make_device(f"before-state-{tag}", librenms_cf={SERVER_KEY: {"id": 40}}), Interface


def _vm_owner(tag):
    vm = make_vm(f"before-state-{tag}")
    seed_own_mapping(vm, 40, SERVER_KEY)
    vm.save()
    return vm, VMInterface


def _changes(model, pk):
    return list(
        ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(model), changed_object_id=pk
        ).order_by("pk")
    )


def _sync_new_port(client, owner, tag, **port):
    seed_ports(owner, [sync_port(10, "eth10", **port)])
    client.force_login(make_superuser(f"before-state-{tag}-user"))
    response = post_interface_sync(client, owner, [10], htmx=False, exclude_columns=("vlans",))
    assert [text for level, text in messages_on(response.wsgi_request) if level == "success"] == [SYNCED]


OWNERS = pytest.mark.parametrize("make_owner", [_device_owner, _vm_owner], ids=["device", "vm"])


@pytest.mark.django_db
@OWNERS
def test_the_update_of_a_created_interface_records_the_created_row_as_its_before_state(client, make_owner):
    owner, model = make_owner(f"created-{make_owner.__name__}")

    _sync_new_port(client, owner, f"created-{make_owner.__name__}")

    interface = model.objects.get(name="eth10")
    create, update = _changes(model, interface.pk)
    assert (create.action, update.action) == ("create", "update")
    assert update.prechange_data == create.postchange_data
    assert (update.prechange_data["mtu"], update.postchange_data["mtu"]) == (None, 1500)


@pytest.mark.django_db
@OWNERS
def test_a_new_mac_is_created_assigned_to_its_interface(client, make_owner):
    owner, model = make_owner(f"mac-{make_owner.__name__}")

    _sync_new_port(client, owner, f"mac-{make_owner.__name__}", mac=MAC)

    interface = model.objects.get(name="eth10")
    mac = MACAddress.objects.get(mac_address=MAC)
    assert (mac.assigned_object, interface.primary_mac_address) == (interface, mac)
    [create] = _changes(MACAddress, mac.pk)
    assert create.action == "create"
    assert (create.postchange_data["assigned_object_type"], create.postchange_data["assigned_object_id"]) == (
        ContentType.objects.get_for_model(model).pk,
        interface.pk,
    )
