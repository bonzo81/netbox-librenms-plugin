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
def test_a_child_committed_before_the_topology_read_refuses_the_observation(monkeypatch):
    """A child that the subtree read missed but the topology holds would move without its serial claim."""
    from dcim.models import Module

    from netbox_librenms_plugin import module_writes

    source = make_device_with_module_bays("owner-late-child", ["Slot 1"])
    root = install_module(source, "Slot 1", "LATE-ROOT", serial="ROOT123", child_bays=("Sub 1",))
    original = module_writes._topology

    def commit_child_first(device_ids):
        # The interleaving point: another writer commits a child after the subtree read.
        if not Module.objects.filter(module_bay__module=root).exists():
            install_module(source, "Sub 1", "LATE-CHILD", serial="LATE123", parent_module=root)
        return original(device_ids)

    monkeypatch.setattr(module_writes, "_topology", commit_child_first)

    with pytest.raises(ValidationError, match="changed"):
        module_writes.observe_module_write(subtree_roots=[root.pk])


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


@transactional_db_with_all_apps()
@pytest.mark.parametrize(
    "target_kind", ["module_type", "device_type"], ids=["module-type-membership", "device-type-shared-device"]
)
@pytest.mark.parametrize("first_writer", ["template", "creation"])
def test_bay_templates_and_module_creation_serialize_type_membership(target_kind, first_writer, monkeypatch, settings):
    """
    Neither contention order may leave a committed type instance without its new bay.

    A module creation adds a Module to the ModuleType, so the module_type case changes membership.
    It adds no Device to the DeviceType: the device_type case covers the lock on the shared Device.
    """
    import time
    from threading import Event

    from dcim.models import Device, Module, ModuleBay, ModuleBayTemplate
    from django.db import DatabaseError, close_old_connections
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import (
        bounded_executor,
        configure_default_librenms_server,
        make_superuser,
    )
    from netbox_librenms_plugin.views.sync import modules as module_views

    configure_default_librenms_server(settings)
    first = make_device_with_module_bays("type-membership-existing", ["Slot 1"])
    second = Device.objects.create(
        name="type-membership-new", device_type=first.device_type, role=first.role, site=first.site, status="active"
    )
    installed = install_module(first, "Slot 1", "TYPE-MEMBERSHIP-CARD")
    user = make_superuser("type-membership-user")
    bay_name = "Additional Type Bay"
    target_pk = installed.module_type_id if target_kind == "module_type" else first.device_type_id
    requests = {
        "template": (
            reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": first.pk}),
            {"server_key": "default", "target_kind": target_kind, "target_pk": target_pk, "name": bay_name},
        ),
        "creation": (
            reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": second.pk}),
            {
                "server_key": "default",
                "module_bay_id": second.modulebays.get(name="Slot 1").pk,
                "module_type_id": installed.module_type_id,
            },
        ),
    }
    paused, release = Event(), Event()
    pids, errors = {}, []
    clean_template = ModuleBayTemplate.full_clean
    save_module = module_views._save_module_with_scoped_component_adoption

    def pause_template(template, *args, **kwargs):
        result = clean_template(template, *args, **kwargs)
        if first_writer == "template" and template.name == bay_name:
            paused.set()
            assert release.wait(timeout=20)
        return result

    def pause_creation(module, *args, **kwargs):
        result = save_module(module, *args, **kwargs)
        if first_writer == "creation" and module.device_id == second.pk:
            paused.set()
            assert release.wait(timeout=20)
        return result

    monkeypatch.setattr(ModuleBayTemplate, "full_clean", pause_template)
    monkeypatch.setattr(module_views, "_save_module_with_scoped_component_adoption", pause_creation)

    def post(actor):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '20s'")
                cursor.execute("SET statement_timeout = '30s'")
            client = Client()
            client.force_login(user)
            pids[actor] = connection.connection.info.backend_pid

            def observe(execute, sql, params, many, context):
                try:
                    return execute(sql, params, many, context)
                except DatabaseError as exc:
                    errors.append(getattr(exc.__cause__, "sqlstate", None))
                    raise

            action, data = requests[actor]
            with connection.execute_wrapper(observe):
                return client.post(action, data).status_code
        finally:
            connection.close()

    other_writer = "creation" if first_writer == "template" else "template"
    waited = False
    with bounded_executor(max_workers=2) as executor:
        first_result = executor.submit(post, first_writer)
        assert paused.wait(timeout=10), "the first writer did not reach its native save boundary"
        other_result = executor.submit(post, other_writer)
        try:
            deadline = time.monotonic() + 10
            while not other_result.done():
                assert time.monotonic() < deadline, "the second writer neither completed nor reached a database wait"
                if other_writer in pids:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_blocking_pids(%s)", [pids[other_writer]])
                        if pids[first_writer] in cursor.fetchone()[0]:
                            waited = True
                            break
                time.sleep(0.02)
        finally:
            release.set()
        assert first_result.result(timeout=30) == 302
        assert other_result.result(timeout=30) == 302
    assert "40P01" not in errors
    assert waited, "the second writer did not wait for the first writer's lock"
    template_exists = ModuleBayTemplate.objects.filter(name=bay_name).exists()
    if target_kind == "module_type":
        expected = set(Module.objects.filter(module_type_id=installed.module_type_id).values_list("pk", flat=True))
        populated = set(
            ModuleBay.objects.filter(module__module_type_id=installed.module_type_id, name=bay_name).values_list(
                "module_id", flat=True
            )
        )
        assert Module.objects.filter(device=second, module_type_id=installed.module_type_id).exists()
    else:
        expected = set(Device.objects.filter(device_type_id=first.device_type_id).values_list("pk", flat=True))
        populated = set(
            ModuleBay.objects.filter(
                device__device_type_id=first.device_type_id, module__isnull=True, name=bay_name
            ).values_list("device_id", flat=True)
        )
    assert not template_exists or populated == expected, "a committed template omitted a concurrent type instance"
    if not template_exists:
        assert not populated, "a refused template write must leave no partial bays"
    if first_writer == "template":
        assert template_exists
    for device in (first, second):
        device.refresh_from_db()
        assert device.module_bay_count == ModuleBay.objects.filter(device=device).count()


@transactional_db_with_all_apps()
@pytest.mark.parametrize("target_kind", ["module_type", "device_type"])
def test_template_rechecks_instances_after_its_save_before_materialization(
    target_kind, monkeypatch, settings, flushed_events
):
    """An external instance committed before template save must cause a safe rollback."""
    from threading import Event

    from dcim.models import Device, Module, ModuleBay, ModuleBayTemplate
    from django.db import close_old_connections
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import (
        bounded_executor,
        configure_default_librenms_server,
        make_superuser,
    )

    configure_default_librenms_server(settings)
    device = make_device_with_module_bays("template-postsave-existing", ["Slot 1"])
    installed = install_module(device, "Slot 1", "TEMPLATE-POSTSAVE-CARD")
    user = make_superuser("template-postsave-user")
    target_pk = installed.module_type_id if target_kind == "module_type" else device.device_type_id
    before_save, release = Event(), Event()
    save_template = ModuleBayTemplate.save
    bay_name = "Postsave Membership Bay"

    def pause_before_save(template, *args, **kwargs):
        # Inside save(), before the INSERT: a membership check that runs before save() misses the newcomer.
        if template.name == bay_name:
            before_save.set()
            assert release.wait(timeout=15)
        return save_template(template, *args, **kwargs)

    monkeypatch.setattr(ModuleBayTemplate, "save", pause_before_save)

    def post():
        close_old_connections()
        try:
            client = Client()
            client.force_login(user)
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:add_bay_template", kwargs={"pk": device.pk}),
                {"server_key": "default", "target_kind": target_kind, "target_pk": target_pk, "name": bay_name},
            )
            return response.status_code
        finally:
            connection.close()

    with bounded_executor(max_workers=1) as executor:
        pending = executor.submit(post)
        try:
            assert before_save.wait(timeout=10)
            newcomer = Device.objects.create(
                name="template-postsave-external",
                device_type=device.device_type,
                role=device.role,
                site=device.site,
                status="active",
            )
            if target_kind == "module_type":
                Module.objects.create(
                    device=newcomer,
                    module_bay=newcomer.modulebays.get(name="Slot 1"),
                    module_type=installed.module_type,
                )
        finally:
            release.set()
        assert pending.result(timeout=20) == 302
    assert not ModuleBayTemplate.objects.filter(name=bay_name).exists(), (
        "membership expansion must roll back the template"
    )
    assert not ModuleBay.objects.filter(name=bay_name).exists(), "a refusal must leave no partial instance bays"
    assert not any(model == "modulebaytemplate" for model, _pk in flushed_events), (
        "a rolled-back template must emit no native event"
    )


@transactional_db_with_all_apps()
def test_a_creation_scope_refuses_unplanned_or_incremental_type_claims():
    from netbox_librenms_plugin.module_writes import lock_module_write, observe_module_write

    device = make_device_with_module_bays("type-scope-complete", ["Slot 1"])
    planned = make_module_type("TYPE-SCOPE-PLANNED")
    unplanned = make_module_type("TYPE-SCOPE-UNPLANNED")
    with transaction.atomic():
        scope = lock_module_write(observe_module_write(device_ids=[device.pk])).lock_module_types([planned.pk])
        scope.require_module_types([planned.pk])
        with pytest.raises(ValidationError, match="changed"):
            scope.require_module_types([unplanned.pk])
        with pytest.raises(RuntimeError, match="complete type set once"):
            scope.lock_module_types([unplanned.pk])
        with pytest.raises(RuntimeError, match="complete type set once"):
            scope.lock_template_type("module_type", planned.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("writer", ["interface-types", "module-bays"])
def test_a_writer_that_keeps_module_serials_does_not_read_a_broken_serial_rule(writer, settings):
    """A legacy serial rule that does not work blocks serial decisions, not a write that changes no serial."""
    from dcim.models import Interface, InterfaceTemplate, ModuleBay
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import configure_default_librenms_server, make_superuser

    configure_default_librenms_server(settings)
    device = make_device_with_module_bays("keeps-serial-owner", ["Slot 1"])
    module = install_module(device, "Slot 1", "KEEPS-SERIAL-CARD", serial="CARD12345")
    InterfaceTemplate.objects.create(module_type=module.module_type, name="Ethernet1", type="10gbase-x-sfpp")
    interface = Interface.objects.create(device=device, module=module, name="Ethernet1", type="1000base-t")
    rule = NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N (.+)$", replacement=r"\1")
    NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"X\1")
    data = {"server_key": "default", "selected_device_id": device.pk}
    if writer == "interface-types":
        action = "apply_module_interface_types"
        data.update(module_id=module.pk, interface_id=[interface.pk])
        data.update({f"current_type_{interface.pk}": "1000base-t", f"template_type_{interface.pk}": "10gbase-x-sfpp"})
    else:
        action = "add_bay_template"
        data.update(target_kind="module_type", target_pk=module.module_type_id, name="Additional Bay")
    client = Client()
    client.force_login(make_superuser("keeps-serial-user"))

    response = client.post(reverse(f"plugins:netbox_librenms_plugin:{action}", kwargs={"pk": device.pk}), data)

    assert response.status_code == 302
    if writer == "interface-types":
        interface.refresh_from_db()
        assert interface.type == "10gbase-x-sfpp"
    else:
        assert ModuleBay.objects.filter(module=module, name="Additional Bay").exists()


@transactional_db_with_all_apps()
def test_a_module_move_whose_commit_meets_a_lock_conflict_sends_no_event(client, flushed_events):
    """The moved row's savepoint releases, then the commit meets a 55P03: the move rolls back with its events."""
    from dcim.models import Module
    from django.contrib.auth import get_user_model
    from django.contrib.messages import get_messages
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_module_bay, make_superuser
    from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, lock_timeout, second_connection
    from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    device = make_device_with_module_bays("move-events-device", ["Slot 1"])
    module = install_module(device, "Slot 1", "MOVE-EVENTS-CARD", serial="MOVE12345")
    target_bay = make_module_bay(device, "Target Bay")
    user = make_superuser("move-events-user")
    client.force_login(user)
    flushed_events.clear()

    # The commit checks the change record's user foreign key; the held user row gives a real 55P03.
    with second_connection() as other:
        lock_row(other, get_user_model(), user.pk)
        with lock_timeout(200):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:move_module", kwargs={"pk": device.pk}),
                {
                    "conflict_module_id": str(module.pk),
                    "target_bay_id": str(target_bay.pk),
                    "selected_device_id": str(device.pk),
                },
            )

    assert TRY_AGAIN_MESSAGE in [str(message) for message in get_messages(response.wsgi_request)]
    assert Module.objects.get(pk=module.pk).module_bay_id != target_bay.pk
    assert flushed_events == []


@pytest.mark.django_db
@pytest.mark.parametrize("destination", ["same-device", "cross-device", "occupied", "occupied-without-serial"])
def test_move_claims_serials_only_when_ownership_changes_or_a_serial_is_removed(client, destination):
    from dcim.models import Manufacturer, Module, ModuleBay
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_superuser
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    source = make_device_with_module_bays("move-policy-source", ["Source Bay", "Target Bay"])
    target = (
        make_device_with_module_bays("move-policy-target", ["Target Bay"]) if destination == "cross-device" else source
    )
    serial = "" if destination == "occupied" else "ABC123"
    moving = install_module(source, "Source Bay", "MOVE-POLICY-CARD", serial=serial)
    source_bay_id = moving.module_bay_id
    target_bay = ModuleBay.objects.get(device=target, name="Target Bay")
    occupant = None
    if destination in {"occupied", "occupied-without-serial"}:
        occupant_serial = "DEF456" if destination == "occupied" else ""
        occupant = install_module(target, target_bay.name, "MOVE-POLICY-OCCUPANT", serial=occupant_serial)
    other_maker = Manufacturer.objects.create(name="Move Policy Other Maker", slug="move-policy-other-maker")
    rule = NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^TAG (.+)$", replacement=r"\1", manufacturer=other_maker
    )
    NormalizationRule.objects.filter(pk=rule.pk).update(match_pattern="(")
    client.force_login(make_superuser("move-policy-user"))

    data = {"conflict_module_id": moving.pk, "target_bay_id": target_bay.pk}
    if occupant:
        data["module_id"] = occupant.pk
    response = client.post(reverse("plugins:netbox_librenms_plugin:move_module", args=[target.pk]), data)

    assert response.status_code == 302
    moving.refresh_from_db()
    assert moving.serial == serial
    if destination in {"same-device", "occupied-without-serial"}:
        assert moving.module_bay_id == target_bay.pk
        if occupant:
            assert not Module.objects.filter(pk=occupant.pk).exists()
    else:
        assert moving.device_id == source.pk
        assert moving.module_bay_id == source_bay_id
        if occupant:
            assert Module.objects.filter(pk=occupant.pk, module_bay=target_bay, serial="DEF456").exists()


@transactional_db_with_all_apps()
def test_a_batch_claims_each_json_number_serial_by_its_text():
    """JSON 123 and 123.0 are one Python key but two stored serials, so a writer of either one waits for the batch."""
    from dcim.models import Module

    from netbox_librenms_plugin.serial_identity import _LOCK_PREFIX, SerialRules, assignment_lock
    from netbox_librenms_plugin.tests.lock_conflict_helpers import second_connection
    from netbox_librenms_plugin.utils import advisory_lock_key

    free = []
    with transaction.atomic():
        assignment_lock([123, 123.0], model=Module, rules=SerialRules.load())
        with second_connection() as other, other.cursor() as cursor:
            for text in ("123", "123.0"):
                identity = f"{_LOCK_PREFIX}:{Module._meta.label_lower}:{text}"
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [advisory_lock_key(identity)])
                free.append(cursor.fetchone()[0])

    assert free == [False, False]
