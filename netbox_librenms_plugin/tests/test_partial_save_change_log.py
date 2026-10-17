"""
A partial save of a row moves ``last_updated``, keeps ``_name`` with ``name``, and records the before-state.

A partial save runs ``pre_save()`` only for the fields in ``update_fields``. The module tests post
a real view through the test client, so NetBox's change logging runs as in production.
"""

import uuid
from contextlib import contextmanager

import pytest
from core.models import ObjectChange
from dcim.models import Interface, InterfaceTemplate, Module
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.urls import reverse
from utilities.ordering import naturalize_interface

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_interface,
    make_module_bay,
    make_module_type,
    make_superuser,
    make_virtual_chassis,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request, trusted_module_inventory_payload
from netbox_librenms_plugin.utils import module_inventory_binding_token, module_inventory_row_digest
from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

pytestmark = pytest.mark.django_db


def test_partial_device_save_validates_the_current_manufacturer():
    from dcim.models import Device, DeviceType, Manufacturer, Platform

    from netbox_librenms_plugin.views.imports.actions import _save_device

    stale = make_device("partial-save-manufacturer")
    platform = Platform.objects.create(
        name="Restricted platform", slug="restricted-platform", manufacturer=stale.device_type.manufacturer
    )
    other_type = DeviceType.objects.create(
        manufacturer=Manufacturer.objects.create(name="Other manufacturer", slug="other-manufacturer"),
        model="Other type",
        slug="other-type",
    )
    Device.objects.filter(pk=stale.pk).update(device_type=other_type)
    stale.platform = platform

    response = _save_device(stale, ["platform"])

    assert response is not None
    stale.refresh_from_db()
    assert stale.device_type_id == other_type.pk
    assert stale.platform_id is None


def test_partial_device_save_validates_the_current_rack_position():
    from dcim.models import Device, DeviceType, Rack

    from netbox_librenms_plugin.views.imports.actions import _save_device

    stale = make_device("partial-save-rack-position")
    rack = Rack.objects.create(name="Partial save rack", site=stale.site, u_height=42)
    taller = DeviceType.objects.create(
        manufacturer=stale.device_type.manufacturer, model="Two units", slug="two-units", u_height=2
    )
    Device.objects.filter(pk=stale.pk).update(rack=rack, position=1, face="front")
    stale.refresh_from_db()
    Device.objects.filter(pk=stale.pk).update(position=42)
    stale.device_type = taller

    response = _save_device(stale, ["device_type"])

    assert response is not None
    stale.refresh_from_db()
    assert stale.position == 42
    assert stale.device_type_id != taller.pk


SERVER_KEY = "default"
ENT_INDEX = 710


@pytest.fixture(autouse=True)
def _server(settings):
    configure_default_librenms_server(settings)


def _changes(obj, action):
    """Return the change records of *obj* with *action*, oldest first."""
    return list(
        ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(obj), changed_object_id=obj.pk, action=action
        ).order_by("time", "pk")
    )


def _update(obj):
    """Return the one update record of *obj*."""
    [change] = _changes(obj, "update")
    return change


def _stored_last_updated(obj):
    return type(obj).objects.filter(pk=obj.pk).values_list("last_updated", flat=True).get()


def _inventory_row(module_type, bay_name, **extra):
    return {
        "entPhysicalIndex": ENT_INDEX,
        "entPhysicalModelName": module_type.model,
        "entPhysicalName": bay_name,
        "entPhysicalDescr": bay_name,
        "entPhysicalClass": "module",
        "entPhysicalContainedIn": 0,
        "entPhysicalSerialNum": "",
        **extra,
    }


def _seed_inventory(device, row, librenms_id):
    """Put *row* in the module tab snapshot of *device*, as a refresh of the tab does."""
    payload = trusted_module_inventory_payload(device, [row], server_key=SERVER_KEY, librenms_id=librenms_id)
    cache.set(DeviceModuleTableView().get_cache_key(device, "inventory", server_key=SERVER_KEY), payload, 300)


def _post_module_action(client, url_name, device, row, action_target, **data):
    """Post a module action for the cached *row*, with the binding that the rendered button carries."""
    binding = module_inventory_binding_token(
        device.pk, SERVER_KEY, url_name, action_target, ENT_INDEX, module_inventory_row_digest(row)
    )
    url = reverse(f"plugins:netbox_librenms_plugin:{url_name}", kwargs={"pk": device.pk})
    return client.post(
        url, {"server_key": SERVER_KEY, "ent_index": str(ENT_INDEX), "inventory_binding": binding, **data}
    )


def _installed_module(device, model):
    bay = make_module_bay(device, "Slot 1")
    return Module.objects.create(device=device, module_bay=bay, module_type=make_module_type(model), status="active")


def test_adopting_a_standalone_template_interface_records_its_module(client):
    device = make_device("partial-save-adopt")
    module = _installed_module(device, "PARTIAL-SAVE-ADOPT-CARD")
    InterfaceTemplate.objects.create(module_type=module.module_type, name="Ethernet1/1", type="other")
    interface = make_interface(device, "Ethernet1/1")
    before = _stored_last_updated(interface)
    row = _inventory_row(module.module_type, "Slot 1")
    _seed_inventory(device, row, librenms_id=71)
    client.force_login(make_superuser("partial-save-adopt-user"))

    _post_module_action(client, "update_module_interface", device, row, {"module_id": module.pk}, module_id=module.pk)

    interface.refresh_from_db()
    assert interface.module_id == module.pk
    assert interface.last_updated > before
    change = _update(interface)
    assert (change.prechange_data["module"], change.postchange_data["module"]) == (None, module.pk)


def test_binding_a_port_to_a_module_interface_records_the_module_and_the_port(client):
    device = make_device("partial-save-bind")
    module = _installed_module(device, "PARTIAL-SAVE-BIND-CARD")
    interface = make_interface(device, "Ethernet1/1")
    before = _stored_last_updated(interface)
    row = _inventory_row(module.module_type, "Slot 1", _librenms_port_id=8801, _librenms_ifname="Ethernet1/1")
    _seed_inventory(device, row, librenms_id=72)
    client.force_login(make_superuser("partial-save-bind-user"))

    _post_module_action(client, "update_module_interface", device, row, {"module_id": module.pk}, module_id=module.pk)

    interface.refresh_from_db()
    assert interface.module_id == module.pk
    assert interface.last_updated > before
    change = _update(interface)
    assert (change.prechange_data["module"], change.prechange_data["custom_fields"].get("librenms_id")) == (None, None)
    assert (change.postchange_data["module"], change.postchange_data["custom_fields"].get("librenms_id")) == (
        module.pk,
        {SERVER_KEY: 8801},
    )


def test_a_module_serial_update_records_the_serial(client):
    device = make_device("partial-save-serial")
    module = _installed_module(device, "PARTIAL-SAVE-SERIAL-CARD")
    before = _stored_last_updated(module)
    row = _inventory_row(module.module_type, "Slot 1", entPhysicalSerialNum="SER-NEW-1")
    _seed_inventory(device, row, librenms_id=73)
    client.force_login(make_superuser("partial-save-serial-user"))

    _post_module_action(client, "update_module_serial", device, row, {"module_id": module.pk}, module_id=module.pk)

    module.refresh_from_db()
    assert module.serial == "SER-NEW-1"
    assert module.last_updated > before
    change = _update(module)
    assert (change.prechange_data["serial"], change.postchange_data["serial"]) == ("", "SER-NEW-1")


def test_a_template_type_update_records_the_type(client):
    page_device = make_device("partial-save-type-page")
    member = make_device("partial-save-type-member")
    make_virtual_chassis("partial-save-type-vc", page_device, member)
    module = _installed_module(member, "PARTIAL-SAVE-TYPE-CARD")
    InterfaceTemplate.objects.create(module_type=module.module_type, name="Te1/1/1", type="10gbase-x-sfpp")
    interface = Interface.objects.create(device=member, module=module, name="Te2/1/1", type="1000base-t")
    before = _stored_last_updated(interface)
    client.force_login(make_superuser("partial-save-type-user"))

    client.post(
        reverse("plugins:netbox_librenms_plugin:apply_module_interface_types", kwargs={"pk": page_device.pk}),
        {
            "server_key": SERVER_KEY,
            "selected_device_id": str(member.pk),
            "module_id": str(module.pk),
            "interface_id": [str(interface.pk)],
            f"current_type_{interface.pk}": "1000base-t",
            f"template_type_{interface.pk}": "10gbase-x-sfpp",
        },
    )

    interface.refresh_from_db()
    assert interface.type == "10gbase-x-sfpp"
    assert interface.last_updated > before
    change = _update(interface)
    assert (change.prechange_data["type"], change.postchange_data["type"]) == ("1000base-t", "10gbase-x-sfpp")


def test_installing_a_module_on_a_second_member_records_the_rename_and_the_adoption(client):
    """The install creates ``Te1/1/1`` and ``Te1/1/2``: it renames the first, and adopts ``Te2/1/2`` for the second."""
    first = make_device("partial-save-vc-first")
    member = make_device("partial-save-vc-member")
    make_virtual_chassis("partial-save-vc", first, member)
    bay = make_module_bay(member, "Slot 1")
    module_type = make_module_type("PARTIAL-SAVE-VC-CARD")
    for name in ("Te1/1/1", "Te1/1/2"):
        InterfaceTemplate.objects.create(module_type=module_type, name=name, type="10gbase-x-sfpp")
    standalone = make_interface(member, "Te2/1/2")
    standalone_before = _stored_last_updated(standalone)
    row = _inventory_row(module_type, "Slot 1", entPhysicalSerialNum="SER-VC-1")
    _seed_inventory(member, row, librenms_id=74)
    client.force_login(make_superuser("partial-save-vc-user"))

    _post_module_action(
        client,
        "install_module",
        member,
        row,
        {"module_bay_id": bay.pk, "module_type_id": module_type.pk},
        module_bay_id=bay.pk,
        module_type_id=module_type.pk,
        serial="SER-VC-1",
    )

    module = Module.objects.get(module_bay=bay)
    renamed = Interface.objects.filter(device=member, module=module).exclude(pk=standalone.pk).get()
    assert renamed.name == "Te2/1/1"
    assert renamed._name == naturalize_interface("Te2/1/1", max_length=100)
    [created] = _changes(renamed, "create")
    assert renamed.last_updated > created.time
    change = _update(renamed)
    assert (change.prechange_data["name"], change.postchange_data["name"]) == ("Te1/1/1", "Te2/1/1")

    standalone.refresh_from_db()
    assert standalone.module_id == module.pk
    assert standalone.last_updated > standalone_before
    change = _update(standalone)
    assert (change.prechange_data["module"], change.postchange_data["module"]) == (None, module.pk)


# ---------------------------------------------------------------------------
# The device helpers that save only some columns, with NetBox's change logging of a request
# ---------------------------------------------------------------------------


@contextmanager
def _change_logging(username):
    """Run the block as NetBox runs a request: the saves record change records for *username*."""
    from netbox.context_managers import event_tracking

    request = make_request("post", user=make_superuser(username))
    request.id = uuid.uuid4()
    with event_tracking(request):
        yield


def test_a_device_save_of_some_columns_records_the_stored_before_state():
    """The import actions change the device before they call the save, so the before-state is the stored row."""
    from netbox_librenms_plugin.views.imports.actions import _save_device

    device = make_device("partial-save-device")
    before = _stored_last_updated(device)
    device.name = "partial-save-device-renamed"

    with _change_logging("partial-save-device-user"):
        response = _save_device(device, update_fields=["name"])

    assert response is None
    assert _stored_last_updated(device) > before
    change = _update(device)
    assert (change.prechange_data["name"], change.postchange_data["name"]) == (
        "partial-save-device",
        "partial-save-device-renamed",
    )


def test_a_normalized_string_librenms_id_records_the_stored_string():
    from netbox_librenms_plugin.utils import get_librenms_device_id

    device = make_device("partial-save-string-id", librenms_cf={SERVER_KEY: "42"})
    before = _stored_last_updated(device)

    with _change_logging("partial-save-string-id-user"):
        assert get_librenms_device_id(device, SERVER_KEY) == 42

    assert _stored_last_updated(device) > before
    change = _update(device)
    assert change.prechange_data["custom_fields"]["librenms_id"] == {SERVER_KEY: "42"}
    assert change.postchange_data["custom_fields"]["librenms_id"] == {SERVER_KEY: 42}


def test_a_primary_ip_set_on_its_own_records_the_previous_address():
    from ipam.models import IPAddress

    from netbox_librenms_plugin.utils import set_device_ip_fk

    device = make_device("partial-save-primary-ip")
    interface = make_interface(device, "eth0")
    address = IPAddress.objects.create(address="198.18.40.10/24", assigned_object=interface, status="active")
    before = _stored_last_updated(device)

    with _change_logging("partial-save-primary-ip-user"):
        set_device_ip_fk(device, "primary_ip4", address)

    assert _stored_last_updated(device) > before
    change = _update(device)
    assert (change.prechange_data["primary_ip4"], change.postchange_data["primary_ip4"]) == (None, address.pk)


@pytest.mark.django_db(transaction=True)
def test_partial_device_save_protects_the_audit_snapshot_from_a_concurrent_edit():
    from concurrent.futures import ThreadPoolExecutor

    from dcim.models import Device
    from django.db import OperationalError, connection, connections, transaction

    from netbox_librenms_plugin.views.imports.actions import _save_device

    device = make_device("audit-lock-device")
    device.name = "audit-lock-renamed"
    concurrent_edits = []

    def concurrent_edit():
        try:
            with transaction.atomic():
                other = Device.objects.select_for_update(nowait=True).get(pk=device.pk)
                other.name = "intervening-edit"
                other.save(update_fields=["name"])
            return True
        except OperationalError as error:
            assert getattr(error.__cause__, "sqlstate", None) == "55P03"
            return False
        finally:
            connections.close_all()

    def edit_after_snapshot_read(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if not concurrent_edits and sql.startswith("SELECT") and 'FROM "dcim_device"' in sql:
            with ThreadPoolExecutor(max_workers=1) as executor:
                concurrent_edits.append(executor.submit(concurrent_edit).result(timeout=10))
        return result

    with _change_logging("audit-lock-user"), connection.execute_wrapper(edit_after_snapshot_read):
        response = _save_device(device, update_fields=["name"])

    assert response is None
    assert concurrent_edits == [False]
    change = _update(device)
    assert (change.prechange_data["name"], change.postchange_data["name"]) == (
        "audit-lock-device",
        "audit-lock-renamed",
    )
