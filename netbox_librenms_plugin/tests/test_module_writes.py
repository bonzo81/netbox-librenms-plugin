"""Real owner locks and stale topology checks for participating module writers."""

import pytest
from django.core.exceptions import ValidationError
from django.db import connection, transaction

from netbox_librenms_plugin.tests.conftest import (
    install_module,
    make_device_with_module_bays,
    make_module_type,
    transactional_db_with_all_apps,
)


@transactional_db_with_all_apps()
@pytest.mark.parametrize("change", ["child-created", "child-removed", "bay", "parent", "device", "serial"])
def test_owner_refuses_changed_membership_or_topology(change):
    from dcim.models import Module, ModuleBay

    from netbox_librenms_plugin.module_writes import lock_module_write, observe_module_write
    from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection

    source = make_device_with_module_bays("owner-topology-source", ["Slot 1", "Slot 2"])
    other = make_device_with_module_bays("owner-topology-other", ["Slot 1"])
    root = install_module(source, "Slot 1", "OWNER-ROOT", serial="ROOT123", child_bays=("Sub 1", "Sub 2"))
    child = install_module(source, "Sub 1", "OWNER-CHILD", parent_module=root)
    second_bay = ModuleBay.objects.get(module=root, name="Sub 2")
    observed = observe_module_write(subtree_roots=[root.pk])

    # Native saves/deletes include the real counter and membership side effects.
    if change == "child-created":
        install_module(source, "Sub 2", "OWNER-NEW-CHILD", parent_module=root)
    elif change == "child-removed":
        child.delete()
    else:
        model, row, fields = {
            "bay": (Module, child, {"module_bay_id": second_bay.pk}),
            "parent": (ModuleBay, child.module_bay, {"module_id": None}),
            "device": (Module, child, {"device_id": other.pk}),
            "serial": (Module, child, {"serial": "CHANGED123"}),
        }[change]
        with second_connection() as peer:
            commit_row_change(peer, model, row.pk, fields)

    with transaction.atomic(), pytest.raises(ValidationError, match="changed"):
        lock_module_write(observed, incoming=["NEW123"])
    assert Module.objects.filter(pk=root.pk).exists()


@transactional_db_with_all_apps()
def test_owner_locks_complete_destinations_in_pk_order_before_downstream_rows():
    from netbox_librenms_plugin.module_writes import lock_module_write, observe_module_write

    first = make_device_with_module_bays("owner-order-first", ["Slot 1"])
    second = make_device_with_module_bays("owner-order-second", ["Slot 1"])
    module = install_module(first, "Slot 1", "OWNER-ORDER", serial="")
    observed = observe_module_write(device_ids=[second.pk, first.pk], module_ids=[module.pk])
    locks = []

    def record(execute, sql, params, many, context):
        if "FOR NO KEY UPDATE" in sql:
            locks.append((sql, params))
        return execute(sql, params, many, context)

    with transaction.atomic(), connection.execute_wrapper(record):
        scope = lock_module_write(observed)
        assert sorted(scope.devices) == sorted([first.pk, second.pk])
        assert len(locks) == 1
        assert "ORDER BY 1 ASC" in locks[0][0]
        with pytest.raises(ValidationError):
            scope.require_devices([max(scope.devices) + 1000])


@transactional_db_with_all_apps()
def test_owner_locks_allow_a_writer_on_an_unrelated_device_to_finish():
    from dcim.models import Device

    from netbox_librenms_plugin.module_writes import lock_module_write, observe_module_write
    from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, lock_timeout, second_connection

    busy = make_device_with_module_bays("owner-unrelated-busy", ["Slot 1"])
    available = make_device_with_module_bays("owner-unrelated-free", ["Slot 1"])
    observed = observe_module_write(device_ids=[available.pk])
    with second_connection() as peer:
        lock_row(peer, Device, busy.pk)
        with transaction.atomic(), lock_timeout(100):
            scope = lock_module_write(observed, incoming=["UNRELATED123"])
            installed = install_module(scope.devices[available.pk], "Slot 1", "OWNER-FREE")
    assert installed.device_id == available.pk


@transactional_db_with_all_apps()
def test_even_a_serialless_owner_requires_a_transaction():
    from netbox_librenms_plugin.module_writes import lock_module_write, observe_module_write

    observed = observe_module_write()
    with pytest.raises(RuntimeError, match="transaction"):
        lock_module_write(observed)


@transactional_db_with_all_apps()
@pytest.mark.parametrize("writer", ["carrier", "interface", "interface-types", "device-bays", "module-bays"])
def test_component_and_serialless_writers_wait_for_the_owner_before_downstream_locks(writer, settings):
    """A real request waits on Device before it locks or writes a component or bay."""
    from threading import Event

    from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay
    from django.core.cache import cache
    from django.db import close_old_connections
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.server_mappings import read_mapping
    from netbox_librenms_plugin.tests.conftest import (
        bounded_executor,
        configure_default_librenms_server,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, second_connection, wait_for_lock_wait
    from netbox_librenms_plugin.tests.view_test_helpers import (
        rendered_module_form,
        render_module_tab,
        trusted_module_inventory_payload,
    )
    from netbox_librenms_plugin.views.mixins import CacheMixin

    configure_default_librenms_server(settings)
    device = make_device_with_module_bays("component-owner-wait", ["Slot 1", "Slot 2"])
    module = install_module(device, "Slot 1", "COMPONENT-OWNER-CARD", child_bays=("Sub 1",))
    InterfaceTemplate.objects.create(module_type=module.module_type, name="Ethernet1", type="10gbase-x-sfpp")
    interface = Interface.objects.create(device=device, module=module, name="Ethernet1", type="1000base-t")
    user = make_superuser("component-owner-user")
    data = {"server_key": "default", "selected_device_id": device.pk}
    if writer == "carrier":
        action = "install_module"
        data.update(
            module_type_id=make_module_type("OWNER-CARRIER").pk,
            module_bay_id=ModuleBay.objects.get(module=module, name="Sub 1").pk,
        )
    elif writer == "interface-types":
        action = "apply_module_interface_types"
        data.update(module_id=module.pk, interface_id=[interface.pk])
        data.update({f"current_type_{interface.pk}": "1000base-t", f"template_type_{interface.pk}": "10gbase-x-sfpp"})
    elif writer in {"device-bays", "module-bays"}:
        action = "add_bay_template"
        data.update(
            target_kind="device_type" if writer == "device-bays" else "module_type",
            target_pk=device.device_type_id if writer == "device-bays" else module.module_type_id,
            name="Additional Bay",
        )
    else:
        row = {
            "entPhysicalIndex": 7,
            "entPhysicalClass": "module",
            "entPhysicalName": "Slot 1",
            "entPhysicalModelName": module.module_type.model,
            "entPhysicalSerialNum": "",
            "entPhysicalContainedIn": 0,
            "_librenms_port_id": 4321,
            "_librenms_ifname": "Ethernet1",
        }
        cache.set(
            CacheMixin().get_cache_key(device, "inventory", "default"),
            trusted_module_inventory_payload(device, [row]),
        )
        url, data = rendered_module_form(render_module_tab(device, user=user), "update-module-interface", 7)
        action = None
    if action:
        url = reverse(f"plugins:netbox_librenms_plugin:{action}", kwargs={"pk": device.pk})
    reached = Event()
    lock_queries, writer_pids = [], []

    def post():
        close_old_connections()
        try:
            client = Client()
            client.force_login(user)

            def record(execute, sql, params, many, context):
                if "FOR UPDATE" in sql or "FOR NO KEY UPDATE" in sql:
                    lock_queries.append(sql)
                    writer_pids.append(connection.connection.info.backend_pid)
                    reached.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(record):
                return client.post(url, data).status_code
        finally:
            connection.close()

    with bounded_executor(max_workers=1) as executor:
        with second_connection() as peer:
            lock_row(peer, Device, device.pk)
            pending = executor.submit(post)
            assert reached.wait(timeout=10)
            assert '"dcim_device"' in lock_queries[0] and "FOR NO KEY UPDATE" in lock_queries[0], lock_queries
            wait_for_lock_wait(connection, writer_pids[0])
            assert not pending.done()
        assert pending.result(timeout=20) == 302
    if writer == "carrier":
        assert Module.objects.filter(device=device, module_bay__module=module, module_bay__name="Sub 1").exists()
    elif writer == "interface":
        interface.refresh_from_db()
        assert read_mapping(interface).own_id("default") == 4321
    elif writer == "interface-types":
        interface.refresh_from_db()
        assert interface.type == "10gbase-x-sfpp"
    else:
        assert ModuleBay.objects.filter(device=device, name="Additional Bay").exists()


@transactional_db_with_all_apps()
def test_opposite_bulk_destinations_commit_one_complete_batch_without_a_lock_cycle():
    from functools import partial
    from threading import Barrier

    from dcim.models import Module
    from django.db import close_old_connections

    from netbox_librenms_plugin.module_writes import lock_module_write, observe_module_write
    from netbox_librenms_plugin.tests.conftest import run_in_threads

    devices = [
        make_device_with_module_bays("bulk-owner-first", ["Slot 1"]),
        make_device_with_module_bays("bulk-owner-second", ["Slot 1"]),
    ]
    modules = [install_module(device, "Slot 1", f"BULK-OWNER-{index}") for index, device in enumerate(devices)]
    start = Barrier(2, timeout=10)

    def batch(index):
        close_old_connections()
        try:
            order = modules if index == 0 else list(reversed(modules))
            observed = observe_module_write(
                device_ids=[module.device_id for module in order], module_ids=[module.pk for module in order]
            )
            incoming = [f"BULK-{index}-{module.pk}" for module in order]
            start.wait()
            try:
                with transaction.atomic():
                    lock_module_write(observed, incoming=incoming)
                    for module, serial in zip(order, incoming, strict=True):
                        locked = Module.objects.select_for_update().get(pk=module.pk)
                        locked.serial = serial
                        locked.save(update_fields=["serial"])
                return "committed"
            except ValidationError:
                return "stale"
        finally:
            connection.close()

    results = run_in_threads(partial(batch, 0), partial(batch, 1), barriers=(start,), timeout=30)
    assert sorted(results) == ["committed", "stale"]
    prefixes = {
        serial.rsplit("-", 1)[0]
        for serial in Module.objects.filter(pk__in=[module.pk for module in modules]).values_list("serial", flat=True)
    }
    assert len(prefixes) == 1, "a stale multi-Device batch must leave no partial writes"


@transactional_db_with_all_apps()
@pytest.mark.parametrize("selection", ["module_ids", "subtree_roots"])
def test_observation_refuses_a_requested_module_deleted_before_the_observation(selection):
    """A view can retain an instance that a concurrent native delete has removed."""
    from netbox_librenms_plugin.module_writes import observe_module_write

    device = make_device_with_module_bays("owner-already-removed", ["Slot 1"])
    module = install_module(device, "Slot 1", "OWNER-REMOVED")
    module_pk = module.pk
    module.delete()
    with pytest.raises(ValidationError, match="changed"):
        observe_module_write(device_ids=[device.pk], **{selection: [module_pk]})


@transactional_db_with_all_apps()
@pytest.mark.parametrize("writer", ["serial", "replace", "interface"])
@pytest.mark.parametrize("change", ["policy", "source"])
def test_existing_module_actions_recheck_their_grant_after_the_owner_wait(writer, change, settings):
    """A grant checked for planning must not authorize a stale policy or source after a wait."""
    from threading import Event

    from dcim.models import Device, Interface
    from django.contrib.messages import get_messages
    from django.core.cache import cache
    from django.db import close_old_connections
    from django.test import Client
    from psycopg.types.json import Jsonb

    from netbox_librenms_plugin.models import InventoryIgnoreRule
    from netbox_librenms_plugin.module_grants import GrantRefusal
    from netbox_librenms_plugin.server_mappings import read_mapping
    from netbox_librenms_plugin.tests.conftest import (
        bounded_executor,
        configure_default_librenms_server,
        make_superuser,
    )
    from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, second_connection, wait_for_lock_wait
    from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
    from netbox_librenms_plugin.tests.view_test_helpers import (
        rendered_module_form,
        rendered_preview_url,
        render_module_tab,
        trusted_module_inventory_payload,
    )
    from netbox_librenms_plugin.views.mixins import CacheMixin

    configure_default_librenms_server(settings)
    device = make_device_with_module_bays("module-grant-owner-wait", ["Slot 1"])
    module = install_module(device, "Slot 1", "GRANT-OWNER-CARD", serial="OLD123")
    interface = Interface.objects.create(device=device, module=module, name="Ethernet1", type="1000base-t")
    row = {
        "entPhysicalIndex": 7,
        "entPhysicalClass": "module",
        "entPhysicalName": "Slot 1",
        "entPhysicalModelName": module.module_type.model,
        "entPhysicalSerialNum": "OLD123" if writer == "interface" else "NEW123",
        "entPhysicalContainedIn": 0,
        "_librenms_port_id": 4322,
        "_librenms_ifname": "Ethernet1",
    }
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", "default"), trusted_module_inventory_payload(device, [row])
    )
    user = make_superuser("module-grant-owner-user")
    client = Client()
    client.force_login(user)
    content = render_module_tab(device, user=user)
    if writer == "replace":
        preview = client.get(rendered_preview_url(content, 7))
        action, data = rendered_module_form(preview.content.decode(), "replace-module", 7)
    else:
        action, data = rendered_module_form(
            content, "update-module-interface" if writer == "interface" else "update-module-serial", 7
        )
    waiting, pids = Event(), []

    def post():
        close_old_connections()
        try:
            request_client = Client()
            request_client.force_login(user)

            def observe(execute, sql, params, many, context):
                if '"dcim_device"' in sql and "FOR NO KEY UPDATE" in sql:
                    pids.append(connection.connection.info.backend_pid)
                    waiting.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                response = request_client.post(action, data)
            return response.status_code, [str(message) for message in get_messages(response.wsgi_request)]
        finally:
            connection.close()

    with bounded_executor(max_workers=1) as executor:
        with second_connection() as holder:
            lock_row(holder, Device, device.pk)
            pending = executor.submit(post)
            assert waiting.wait(timeout=10)
            wait_for_lock_wait(connection, pids[0])
            if change == "policy":
                InventoryIgnoreRule.objects.create(
                    name="Changed during owner wait", match_type="contains", pattern="absent-entity", action="skip"
                )
            else:
                seed_mapping(device, own=9432, save=False)
                with holder.cursor() as cursor:
                    cursor.execute(
                        'UPDATE "dcim_device" SET custom_field_data = %s WHERE id = %s',
                        [Jsonb(device.custom_field_data), device.pk],
                    )
            holder.commit()
        status, messages = pending.result(timeout=20)
    assert status == 302
    expected = GrantRefusal.POLICY if change == "policy" else GrantRefusal.CACHE_MISSING
    assert messages == [expected.message]
    module.refresh_from_db()
    interface.refresh_from_db()
    assert module.serial == "OLD123"
    assert read_mapping(interface).own_id("default") is None
