"""Two writers on one device that bind the same LibreNMS port at the same time leave one holder."""

import time
from concurrent.futures import ThreadPoolExecutor, wait
from threading import Event

import pytest
from django.apps import apps

from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_device, make_superuser
from netbox_librenms_plugin.tests.interface_sync_post_helpers import (
    SERVER_KEY,
    post_interface_sync,
    seed_ports,
    sync_port,
)

PORT = 9301
LIBRENMS_DEVICE_ID = 71
ENT_INDEX = 4077
SHORT_NAME, LONG_NAME = "Gi0/1", "GigabitEthernet0/1"
PAUSE_TIMEOUT_SECONDS = 10
# The paused writer outwaits a full observation window, so a failed observation reports itself.
RELEASE_TIMEOUT_SECONDS = 2 * PAUSE_TIMEOUT_SECONDS
# A blocked query fails after this time instead of hanging the suite; it is longer than the pause.
DB_TIMEOUT = "30s"
_INTERFACE_UPDATE = 'UPDATE "dcim_interface"'
_INVENTORY_ITEM = {"entPhysicalIndex": ENT_INDEX, "_librenms_port_id": PORT, "_librenms_ifname": SHORT_NAME}
_SNAPSHOT_FIELDS = ("name", "mtu", "description", "enabled", "custom_field_data", "last_updated", "module_id")

pytestmark = pytest.mark.django_db(
    transaction=True,
    available_apps=[app.name for app in apps.get_app_configs()],
)


@pytest.fixture(autouse=True)
def restore_librenms_id_custom_field():
    """Recreate migration-seeded custom-field state after each TransactionTestCase flush."""
    from netbox_librenms_plugin import _ensure_librenms_id_custom_field

    executed_aliases = getattr(_ensure_librenms_id_custom_field, "_executed_aliases", set())
    executed_aliases.discard("default")
    _ensure_librenms_id_custom_field(sender=None, using="default")


def _seed():
    """Return a device with a module interface ``Gi0/1`` and a plain ``GigabitEthernet0/1``, both unbound."""
    from dcim.models import Interface, Module, ModuleBay, ModuleType

    device = make_device("port-claim", librenms_cf={SERVER_KEY: {"id": LIBRENMS_DEVICE_ID}})
    module_type = ModuleType.objects.create(manufacturer=device.device_type.manufacturer, model="port-claim-mt")
    bay = ModuleBay.objects.create(device=device, name="port-claim-bay")
    module = Module.objects.create(device=device, module_bay=bay, module_type=module_type)
    short = Interface.objects.create(device=device, name=SHORT_NAME, type="1000base-t", module=module)
    long = Interface.objects.create(device=device, name=LONG_NAME, type="1000base-t")
    seed_ports(device, [sync_port(PORT, SHORT_NAME) | {"ifDescr": LONG_NAME}])
    _seed_inventory(device)
    return device, module, short, long


def _seed_inventory(device):
    """Cache the module inventory row that names ``PORT`` by ifName; the helper saves the device row."""
    from django.core.cache import cache

    from netbox_librenms_plugin.tests.view_test_helpers import trusted_module_inventory_payload
    from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

    cache.set(
        UpdateModuleInterfaceView().get_cache_key(device, "inventory", server_key=SERVER_KEY),
        trusted_module_inventory_payload(
            device, [_INVENTORY_ITEM], server_key=SERVER_KEY, librenms_id=LIBRENMS_DEVICE_ID
        ),
        timeout=300,
    )


def _port_holders():
    """Return the pk of every Interface and VMInterface that holds ``PORT`` on the server."""
    from dcim.models import Interface
    from virtualization.models import VMInterface

    from netbox_librenms_plugin.utils import build_librenms_id_qs

    host_q, oob_q = build_librenms_id_qs(SERVER_KEY, PORT)
    holders = (model.objects.filter(host_q | oob_q).values_list("pk", flat=True) for model in (Interface, VMInterface))
    return sorted(pk for pks in holders for pk in pks)


def _persisted(interface):
    """Return the persisted state of *interface* that a refused writer must not change."""
    from core.models import ObjectChange
    from dcim.models import Interface

    row = Interface.objects.values(*_SNAPSHOT_FIELDS).get(pk=interface.pk)
    changes = ObjectChange.objects.filter(changed_object_type__model="interface", changed_object_id=interface.pk)
    return row, changes.count()


def _interface_sync(user_pk, device, name_field):
    """Post the interface sync of *device* with *name_field* naming the port."""
    from django.contrib.auth import get_user_model
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import messages_on

    client = Client()
    client.force_login(get_user_model().objects.get(pk=user_pk))
    response = post_interface_sync(client, device, [PORT], htmx=False, interface_name_field=name_field)
    return messages_on(response.wsgi_request)


def _module_bind(user_pk, device, module):
    """Post the module "Update Interface" action for the cached inventory row."""
    from django.contrib.auth import get_user_model
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.test import RequestFactory

    from netbox_librenms_plugin.tests.view_test_helpers import change_logging, messages_on, post
    from netbox_librenms_plugin.utils import module_inventory_binding_token, module_inventory_row_digest
    from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

    token = module_inventory_binding_token(
        device.pk,
        SERVER_KEY,
        "update_module_interface",
        {"module_id": module.pk},
        ENT_INDEX,
        module_inventory_row_digest(_INVENTORY_ITEM),
    )
    request = RequestFactory().post(
        f"/modules/{device.pk}/interface/",
        data={
            "module_id": str(module.pk),
            "server_key": SERVER_KEY,
            "ent_index": str(ENT_INDEX),
            "inventory_binding": token,
        },
    )
    request.user = get_user_model().objects.get(pk=user_pk)
    request.session = {}
    request._messages = FallbackStorage(request)
    with change_logging(request):
        post(UpdateModuleInterfaceView(), request, pk=device.pk)
    return messages_on(request)


def _on_own_connection(writer, *args, pids, role, paused=None, resume=None):
    """Run *writer* on a bounded connection of this thread; pause at the first interface write if *paused* is set."""
    from django.db import close_old_connections, connection

    first_write = True

    def pause_at_first_interface_write(execute, sql, params, many, context):
        nonlocal first_write
        if paused is not None and first_write and sql.startswith(_INTERFACE_UPDATE):
            first_write = False
            paused.set()
            assert resume.wait(timeout=RELEASE_TIMEOUT_SECONDS), "the race did not release the first writer"
        return execute(sql, params, many, context)

    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SET lock_timeout = '{DB_TIMEOUT}'")
            cursor.execute(f"SET statement_timeout = '{DB_TIMEOUT}'")
            cursor.execute("SELECT pg_backend_pid()")
            pids[role] = cursor.fetchone()[0]
        with connection.execute_wrapper(pause_at_first_interface_write):
            return writer(*args)
    finally:
        connection.close()


def _race(first, second, while_first_paused):
    """Pause *first* at its first interface write, run *second*, observe, then release *first*."""
    first_paused, resume, pids = Event(), Event(), {}
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        first_future = executor.submit(
            _on_own_connection, *first, pids=pids, role="first", paused=first_paused, resume=resume
        )
        assert first_paused.wait(timeout=PAUSE_TIMEOUT_SECONDS), "the first writer never reached its write"
        second_future = executor.submit(_on_own_connection, *second, pids=pids, role="second")
        observed = while_first_paused(second_future, pids)
        resume.set()
        first_result = first_future.result(timeout=PAUSE_TIMEOUT_SECONDS)
        second_result = second_future.result(timeout=PAUSE_TIMEOUT_SECONDS)
    finally:
        resume.set()
        executor.shutdown(wait=False, cancel_futures=True)
    return first_result, second_result, observed


def _second_waits_on_first(_second_future, pids):
    """Return True when PostgreSQL reports that the second writer's backend waits on the first writer's backend."""
    from django.db import connection

    deadline = time.monotonic() + PAUSE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if "second" in pids:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_blocking_pids(%s)", [pids["second"]])
                if pids["first"] in cursor.fetchone()[0]:
                    return True
        time.sleep(0.05)
    return False


def _second_finishes_alone(second_future, _pids):
    """Return True when the second writer finishes while the first writer is still paused."""
    done, _pending = wait([second_future], timeout=PAUSE_TIMEOUT_SECONDS)
    return second_future in done


def test_two_interface_syncs_of_one_device_wait_on_the_device_row_lock(settings):
    """The ifName sync holds the device row; the ifDescr sync waits for it and then finds the holder."""
    configure_default_librenms_server(settings)
    device, _module, short, long = _seed()
    user_pk = make_superuser("port-claim-user").pk
    before = _persisted(long)

    _first, second_messages, second_waited = _race(
        (_interface_sync, user_pk, device, "ifName"),
        (_interface_sync, user_pk, device, "ifDescr"),
        _second_waits_on_first,
    )

    assert second_waited, "the second sync did not wait on the first sync's backend"
    assert _port_holders() == [short.pk]
    assert second_messages == [
        (
            "warning",
            f"Interface '{SHORT_NAME}' kept its current name because the reported name is in use "
            f"on the same interface owner: '{LONG_NAME}'.",
        ),
        ("success", "Selected interfaces synced successfully."),
    ]
    assert _persisted(long) == before


@pytest.mark.parametrize("first_writer", ["interface_sync", "module_bind"])
def test_module_bind_and_interface_sync_of_one_device_leave_one_holder(settings, first_writer):
    """The module action binds Gi0/1 by ifName while the ifDescr sync binds GigabitEthernet0/1."""
    # The module action takes no device row lock, so only the port claim can refuse the second writer.
    configure_default_librenms_server(settings)
    device, module, short, long = _seed()
    user_pk = make_superuser("port-claim-user").pk
    sync = (_interface_sync, user_pk, device, "ifDescr")
    bind = (_module_bind, user_pk, device, module)
    first, second = (sync, bind) if first_writer == "interface_sync" else (bind, sync)
    winner, loser = (long, short) if first_writer == "interface_sync" else (short, long)
    refusal = "Another operation is binding this LibreNMS port. Refresh and retry."
    # The module action wraps the refusal in its association message; the interface sync shows it as is.
    expected = f"Could not update interface association: {refusal}" if first_writer == "interface_sync" else refusal
    before = _persisted(loser)
    _winner_row, winner_changes = _persisted(winner)

    _first, second_messages, second_finished_alone = _race(first, second, _second_finishes_alone)

    assert second_finished_alone, "the second writer did not finish while the first writer held its claim"
    assert _port_holders() == [winner.pk]
    assert second_messages == [("warning", expected)]
    assert _persisted(loser) == before
    # A change record for the winner shows that both writers run with change logging on.
    assert _persisted(winner)[1] > winner_changes
