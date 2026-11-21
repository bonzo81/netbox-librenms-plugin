"""Two real database sessions that write rule-equivalent serials, and the events of a failed inner install."""

import time
from functools import partial
from threading import Barrier

import pytest
from django.db import IntegrityError, close_old_connections, connection

from netbox_librenms_plugin.module_grants import GrantRefusal

from netbox_librenms_plugin.tests.conftest import (
    bounded_executor,
    configure_librenms_servers,
    install_module,
    make_device,
    make_device_with_module_bays,
    make_module_bay,
    make_module_type,
    make_superuser,
    run_in_threads,
    transactional_db_with_all_apps,
)
from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.view_test_helpers import (
    messages_on,
    rendered_module_form,
    render_module_tab,
    trusted_module_inventory_payload,
)

SERVER_KEY = "default"


@transactional_db_with_all_apps()
def test_a_locked_serial_lookup_does_not_wait_for_an_unrelated_candidate():
    from dcim.models import Module
    from django.db import transaction

    from netbox_librenms_plugin.serial_identity import resolve_module
    from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_row, lock_timeout, second_connection

    _seeded_rule()
    device = make_device_with_module_bays("serial-unrelated-candidate", ["Slot 1", "Slot 2"])
    match = install_module(device, "Slot 1", "UNRELATED-CARD-1", serial="ABC")
    unrelated = install_module(device, "Slot 2", "UNRELATED-CARD-2", serial="XABC")

    with second_connection() as other:
        lock_row(other, Module, unrelated.pk)
        with transaction.atomic(), lock_timeout(100):
            result = resolve_module("ABC", device_ids=[device.pk], lock=True)

    assert result.object.pk == match.pk


@transactional_db_with_all_apps()
@pytest.mark.parametrize(
    ("stored", "incoming", "first_refused"),
    [(("S/N ABC", "XABC"), ("ABC", "S/N XABC"), False), (("AAA", "BBB"), ("BBB", "CCC"), True)],
    ids=["substring-candidate", "confirmed-conflict"],
)
def test_module_serial_updates_resolve_conflicts_without_a_lock_cycle(stored, incoming, first_refused):
    from threading import Event

    from dcim.models import Module
    from django.db import transaction

    from netbox_librenms_plugin.serial_identity import (
        SerialRules,
        assignment_lock,
        locked_type_context,
        module_assignment,
    )

    _seeded_rule()
    device = make_device_with_module_bays("serial-candidate-locks", ["Slot 1", "Slot 2"])
    first = install_module(device, "Slot 1", "CANDIDATE-CARD-1", serial=stored[0])
    second = install_module(device, "Slot 2", "CANDIDATE-CARD-2", serial=stored[1])
    device_locked, second_locked = Event(), Event()

    def assign(module_pk, incoming, first_writer):
        close_old_connections()
        try:
            with transaction.atomic():
                rules = SerialRules.load()
                assignment_lock([incoming], model=Module, rules=rules)
                module = Module.objects.select_for_update().get(pk=module_pk)
                if first_writer:
                    locked_type_context(module.device, rules=rules)
                    device_locked.set()
                    assert second_locked.wait(timeout=10)
                else:
                    assert device_locked.wait(timeout=10)
                    second_locked.set()
                assignment = module_assignment(module, incoming, rules=rules)
                if first_writer and first_refused:
                    assert assignment.conflict is not None
                    return "refused"
                assert assignment.conflict is None
                module.serial = assignment.value
                module.save(update_fields=["serial"])
            return incoming
        finally:
            connection.close()

    assert run_in_threads(
        partial(assign, first.pk, incoming[0], True),
        partial(assign, second.pk, incoming[1], False),
        barriers=(),
        timeout=30,
    ) == ["refused" if first_refused else incoming[0], incoming[1]]


@transactional_db_with_all_apps()
def test_opposite_module_relocations_lock_the_device_pair_in_one_order():
    from threading import Event

    from dcim.models import Device, Module
    from django.db import transaction

    from netbox_librenms_plugin.serial_identity import (
        SerialRules,
        module_subtree_serials,
        relocation_conflict,
    )

    from netbox_librenms_plugin.module_writes import lock_module_write, observe_module_write

    first = make_device_with_module_bays("opposite-move-first", ["Slot 1"])
    second = make_device_with_module_bays("opposite-move-second", ["Slot 1"])
    modules = [
        install_module(first, "Slot 1", "OPPOSITE-CARD-1", serial="MOVE-ABC"),
        install_module(second, "Slot 1", "OPPOSITE-CARD-2", serial="MOVE-XYZ"),
    ]
    start = Barrier(2, timeout=10)
    acquired = [Event(), Event()]

    def relocate(index):
        close_old_connections()
        waited = False

        def observe_device_lock(execute, sql, params, many, context):
            nonlocal waited
            result = execute(sql, params, many, context)
            if '"dcim_device"' in sql and "FOR NO KEY UPDATE" in sql and not waited:
                waited = True
                acquired[index].set()
                acquired[1 - index].wait(timeout=2)
            return result

        try:
            rules = SerialRules.load()
            moving = module_subtree_serials(modules[index].pk)
            with transaction.atomic(), connection.execute_wrapper(observe_device_lock):
                target = Device.objects.get(pk=(second if index == 0 else first).pk)
                observed = observe_module_write(device_ids=[target.pk], subtree_roots=[modules[index].pk])
                start.wait()
                scope = lock_module_write(observed, rules=rules)
                module = Module.objects.select_for_update().get(pk=modules[index].pk)
                return relocation_conflict(module, target, moving, rules=rules, scope=scope)
        finally:
            connection.close()

    assert run_in_threads(partial(relocate, 0), partial(relocate, 1), barriers=(start,), timeout=30) == [False, False]


@pytest.fixture
def librenms_server(settings, monkeypatch):
    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        configure_librenms_servers(
            settings, {SERVER_KEY: {"librenms_url": server.url, "api_token": "race-token", "cache_timeout": 0}}
        )
        yield server


def _seeded_rule():
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
    NormalizationRule.objects.create(scope="serial", match_pattern=r"^S/N\s+(.+)$", replacement=r"\1")


def _post_serial_update(device_pk, user_pk, barrier):
    """POST Update Serial for one device in this thread's own database session; wait at the first identity lock."""
    from django.contrib.auth import get_user_model
    from django.test import Client
    from django.urls import reverse

    waited = False

    def meet_at_the_lock(execute, sql, params, many, context):
        nonlocal waited
        if "pg_advisory_xact_lock" in sql and not waited:
            waited = True
            barrier.wait(timeout=10)
        return execute(sql, params, many, context)

    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '10s'")
        client = Client()
        client.force_login(get_user_model().objects.get(pk=user_pk))
        with connection.execute_wrapper(meet_at_the_lock):
            response = client.post(
                reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": device_pk}),
                {"server_key": SERVER_KEY},
            )
        return response.status_code
    finally:
        connection.close()


@transactional_db_with_all_apps()
def test_two_sessions_that_assign_rule_equivalent_serials_store_one_of_them(librenms_server):
    """
    Both sessions reach the identity lock together. Their serials share a key, so they share the lock.

    The second session resolves after the first commits, sees the equivalent serial, and refuses.
    """
    from dcim.models import Device

    _seeded_rule()
    first = seed_mapping(make_device("race-serial-first"), SERVER_KEY, own=9801)
    second = seed_mapping(make_device("race-serial-second"), SERVER_KEY, own=9802)
    librenms_server.device_info_response(device_id=9801, hostname=first.name, serial="S/N RACE-SERIAL")
    librenms_server.device_info_response(device_id=9802, hostname=second.name, serial="RACE-SERIAL")
    user = make_superuser("race-serial-user")
    barrier = Barrier(2, timeout=10)

    statuses = run_in_threads(
        partial(_post_serial_update, first.pk, user.pk, barrier),
        partial(_post_serial_update, second.pk, user.pk, barrier),
        barriers=(barrier,),
        timeout=60,
    )

    assert statuses == [302, 302]
    stored = sorted(Device.objects.filter(pk__in=[first.pk, second.pk]).values_list("serial", flat=True))
    assert stored in (["", "S/N RACE-SERIAL"], ["", "RACE-SERIAL"]), stored


@pytest.mark.django_db
def test_a_failed_install_after_its_save_leaves_events_only_for_the_committed_sibling(
    client, monkeypatch, flushed_events
):
    """The second install saves its module, then fails; its savepoint drops the module and the module's events."""
    from dcim.models import InterfaceTemplate, Module
    from django.core.cache import cache
    from django.urls import reverse

    from netbox_librenms_plugin.views.mixins import CacheMixin
    from netbox_librenms_plugin.views.sync import modules as module_views

    device = make_device("event-safe-branch")
    for number in (1, 2):
        make_module_bay(device, f"Slot {number}", position=str(number))
    module_type = make_module_type("EVENT-SAFE-CARD", manufacturer=device.device_type.manufacturer)
    InterfaceTemplate.objects.create(module_type=module_type, name="Ethernet{module}/1", type="10gbase-x-sfpp")
    rows = [
        {
            "entPhysicalIndex": 1,
            "entPhysicalClass": "module",
            "entPhysicalName": "Carrier",
            "entPhysicalModelName": "EVENT-SAFE-CARRIER",
            "entPhysicalContainedIn": 0,
        },
        *(
            {
                "entPhysicalIndex": number + 1,
                "entPhysicalContainedIn": 1,
                "entPhysicalClass": "module",
                "entPhysicalName": f"Slot {number}",
                "entPhysicalModelName": module_type.model,
                "entPhysicalSerialNum": f"EVENT-SN{number}",
            }
            for number in (1, 2)
        ),
    ]
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", SERVER_KEY), trusted_module_inventory_payload(device, rows)
    )
    user = make_superuser("event-safe-branch-user")
    _url, data = rendered_module_form(render_module_tab(device, user=user), "install-branch", 1)
    normalize = module_views._normalize_module_interface_names_for_vc_member
    saved = []

    def fail_after_the_second_save(target, module, *args, **kwargs):
        saved.append(module.pk)
        if module.serial == "EVENT-SN2":
            # The module row and its interface are written; the failure comes after them.
            raise IntegrityError("injected failure after the second module save")
        return normalize(target, module, *args, **kwargs)

    monkeypatch.setattr(module_views, "_normalize_module_interface_names_for_vc_member", fail_after_the_second_save)
    client.force_login(user)

    response = client.post(reverse("plugins:netbox_librenms_plugin:install_branch", args=[device.pk]), data)

    assert response.status_code == 302
    (kept,) = Module.objects.filter(device=device)
    assert kept.serial == "EVENT-SN1"
    rolled_back = [pk for pk in saved if pk != kept.pk]
    assert len(rolled_back) == 1 and not Module.objects.filter(pk__in=rolled_back).exists()
    assert ("module", kept.pk) in flushed_events
    assert ("module", rolled_back[0]) not in flushed_events
    kept_interfaces = set(kept.interfaces.values_list("pk", flat=True))
    assert {pk for model, pk in flushed_events if model == "interface"} <= kept_interfaces


@pytest.mark.django_db
def test_a_legacy_module_serial_heals_to_the_librenms_text(client):
    """An update of a module whose stored rule output names the LibreNMS hardware writes the LibreNMS text."""
    from dcim.models import Module
    from django.core.cache import cache

    from netbox_librenms_plugin.views.mixins import CacheMixin

    _seeded_rule()
    device = make_device_with_module_bays("module-heal", ["Slot 1"])
    installed = install_module(device, "Slot 1", "HEAL-CARD", serial="HEAL-SN1")
    rows = [
        {
            "entPhysicalIndex": 7,
            "entPhysicalClass": "module",
            "entPhysicalName": "Slot 1",
            "entPhysicalModelName": installed.module_type.model,
            "entPhysicalSerialNum": "S/N HEAL-SN1",
            "entPhysicalContainedIn": 0,
        }
    ]
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", SERVER_KEY), trusted_module_inventory_payload(device, rows)
    )
    user = make_superuser("module-heal-user")
    # The row matches, and the page offers the update because the stored form differs.
    url, data = rendered_module_form(render_module_tab(device, user=user), "update-module-serial", 7)
    client.force_login(user)

    response = client.post(url, data)

    assert response.status_code == 302
    assert Module.objects.get(pk=installed.pk).serial == "S/N HEAL-SN1"


@transactional_db_with_all_apps()
def test_an_import_type_change_reads_the_device_serial_again_under_its_locks(librenms_server):
    """Another session stores "XABC" after the type change read "DEF456"; under the new rule it is the holder's "ABC"."""
    from dcim.models import Device, DeviceType, Manufacturer

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
    from netbox_librenms_plugin.tests.view_test_helpers import post as post_view
    from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

    target = make_device("retype-race-target", serial="DEF456", librenms_cf={SERVER_KEY: 9811})
    make_device("retype-race-holder", serial="ABC")
    new_maker = Manufacturer.objects.create(name="Retype race maker", slug="retype-race-maker")
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=new_maker
    )
    new_type = DeviceType.objects.create(
        manufacturer=new_maker, model="Retype race model", slug="retype-race-model", part_number="RETYPE-RACE-HW"
    )
    librenms_server.device_info_response(
        device_id=9811, hostname=target.name, serial="DEF456", hardware=new_type.part_number
    )
    librenms_server.vc_inventory_callable(9811, [], {})
    view = DeviceConflictActionView()
    view._librenms_api = LibreNMSAPI(server_key=SERVER_KEY)
    request = make_request(
        "post",
        {"server_key": SERVER_KEY, "action": "update_type", "existing_device_id": str(target.pk), "force": "on"},
        user=make_user_with_perms("retype-race-user", [("change", Device)]),
        HTTP_HX_REQUEST="true",
    )
    changed = []

    def change_before_the_first_lock(execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql and not changed:
            changed.append(True)
            with second_connection() as other:
                commit_row_change(other, Device, target.pk, {"serial": "XABC"})
        return execute(sql, params, many, context)

    with connection.execute_wrapper(change_before_the_first_lock):
        post_view(view, request, device_id=9811)

    assert changed
    assert Device.objects.get(pk=target.pk).device_type_id != new_type.pk


@transactional_db_with_all_apps()
@pytest.mark.parametrize("surface", ["import", "sync"])
def test_a_type_change_reads_the_new_type_again_under_its_lock(librenms_server, surface):
    """Another session moves the new type to a maker whose rule keys "XABC" to the holder's "ABC" after the match."""
    from dcim.models import Device, DeviceType, Manufacturer
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.tests.view_test_helpers import post as post_view
    from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

    target = make_device(f"type-lock-{surface}-target", serial="XABC", librenms_cf={SERVER_KEY: 9815})
    make_device(f"type-lock-{surface}-holder", serial="ABC")
    plain_maker = Manufacturer.objects.create(name=f"Type lock plain {surface}", slug=f"type-lock-plain-{surface}")
    strip_maker = Manufacturer.objects.create(name=f"Type lock strip {surface}", slug=f"type-lock-strip-{surface}")
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=strip_maker
    )
    new_type = DeviceType.objects.create(
        manufacturer=plain_maker, model=f"Type lock {surface}", slug=f"type-lock-{surface}", part_number="TYPE-LOCK-HW"
    )
    librenms_server.device_info_response(
        device_id=9815, hostname=target.name, serial="XABC", hardware=new_type.part_number
    )
    librenms_server.vc_inventory_callable(9815, [], {})
    user = make_superuser(f"type-lock-{surface}-user")
    changed = []

    def change_before_the_first_lock(execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql and not changed:
            changed.append(True)
            with second_connection() as other:
                commit_row_change(other, DeviceType, new_type.pk, {"manufacturer_id": strip_maker.pk})
        return execute(sql, params, many, context)

    with connection.execute_wrapper(change_before_the_first_lock):
        if surface == "import":
            view = DeviceConflictActionView()
            view._librenms_api = LibreNMSAPI(server_key=SERVER_KEY)
            data = {"server_key": SERVER_KEY, "action": "update_type", "existing_device_id": str(target.pk)}
            post_view(
                view, make_request("post", {**data, "force": "on"}, user=user, HTTP_HX_REQUEST="true"), device_id=9815
            )
        else:
            client = Client()
            client.force_login(user)
            client.post(
                reverse("plugins:netbox_librenms_plugin:update_device_type", kwargs={"pk": target.pk}),
                {"server_key": SERVER_KEY},
            )

    assert changed
    assert Device.objects.get(pk=target.pk).device_type_id != new_type.pk


@transactional_db_with_all_apps()
def test_a_type_and_serial_update_checks_the_serial_under_the_locked_new_type(librenms_server):
    """
    The old master rule strips "PREFIX-", so the member "PREFIX-ABC123" keys to "ABC123". The new type
    moves to a maker with the same rule after the match: the master's new "ABC123" then names the
    member's hardware, although the stale plain type kept the two apart.
    """
    from dcim.models import Device, DeviceType, Manufacturer, VirtualChassis

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
    from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.tests.view_test_helpers import post as post_view
    from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

    master = make_device("stale-type-master", serial="BEFORE123", librenms_cf={SERVER_KEY: 9816})
    member = make_device("stale-type-member", serial="PREFIX-ABC123")
    chassis = make_virtual_chassis("stale-type-chassis", master, member)
    VirtualChassis.objects.filter(pk=chassis.pk).update(master=master)
    old_type_id = master.device_type_id
    strip_maker = Manufacturer.objects.create(name="Stale type strip", slug="stale-type-strip")
    for maker in (master.device_type.manufacturer, strip_maker):
        NormalizationRule.objects.create(
            scope="serial", match_pattern=r"^PREFIX-(.+)$", replacement=r"\1", manufacturer=maker
        )
    new_type = DeviceType.objects.create(
        manufacturer=Manufacturer.objects.create(name="Stale type plain", slug="stale-type-plain"),
        model="Stale type model",
        slug="stale-type-model",
        part_number="STALE-TYPE-HW",
    )
    librenms_server.device_info_response(
        device_id=9816, hostname=master.name, serial="ABC123", hardware=new_type.part_number
    )
    librenms_server.vc_inventory_callable(9816, [], {})
    view = DeviceConflictActionView()
    view._librenms_api = LibreNMSAPI(server_key=SERVER_KEY)
    request = make_request(
        "post",
        {"server_key": SERVER_KEY, "action": "update", "existing_device_id": str(master.pk), "force": "on"},
        user=make_superuser("stale-type-user"),
        HTTP_HX_REQUEST="true",
    )
    changed = []

    def change_before_the_first_lock(execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql and not changed:
            changed.append(True)
            with second_connection() as other:
                commit_row_change(other, DeviceType, new_type.pk, {"manufacturer_id": strip_maker.pk})
        return execute(sql, params, many, context)

    with connection.execute_wrapper(change_before_the_first_lock):
        post_view(view, request, device_id=9816)

    assert changed
    assert Device.objects.values_list("device_type_id", "serial").get(pk=master.pk) == (old_type_id, "BEFORE123")


@transactional_db_with_all_apps()
@pytest.mark.parametrize("action", ["update", "update_serial"])
def test_a_type_and_serial_update_admits_the_serial_under_the_locked_new_type(librenms_server, action):
    """The matched type's maker gives "SN-ABC123" no identity; the maker that the type moves to does."""
    from dcim.models import Device, DeviceType, Manufacturer

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
    from netbox_librenms_plugin.tests.view_test_helpers import make_request
    from netbox_librenms_plugin.tests.view_test_helpers import post as post_view
    from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

    tag = f"stale-admission-{action}".replace("_", "-")
    target = make_device(tag, serial="OLD12345", librenms_cf={SERVER_KEY: 9817})
    empty_maker = Manufacturer.objects.create(name=f"{tag} empty", slug=f"{tag}-empty")
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^SN-ABC123()$", replacement=r"\1", manufacturer=empty_maker
    )
    plain_maker = Manufacturer.objects.create(name=f"{tag} plain", slug=f"{tag}-plain")
    new_type = DeviceType.objects.create(
        manufacturer=empty_maker, model=f"{tag} model", slug=f"{tag}-model", part_number=f"{tag}-hw".upper()
    )
    librenms_server.device_info_response(
        device_id=9817, hostname=target.name, serial="SN-ABC123", hardware=new_type.part_number
    )
    librenms_server.vc_inventory_callable(9817, [], {})
    view = DeviceConflictActionView()
    view._librenms_api = LibreNMSAPI(server_key=SERVER_KEY)
    request = make_request(
        "post",
        {"server_key": SERVER_KEY, "action": action, "existing_device_id": str(target.pk), "force": "on"},
        user=make_superuser(f"{tag}-user"),
        HTTP_HX_REQUEST="true",
    )
    changed = []

    def change_before_the_first_lock(execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql and not changed:
            changed.append(True)
            with second_connection() as other:
                commit_row_change(other, DeviceType, new_type.pk, {"manufacturer_id": plain_maker.pk})
        return execute(sql, params, many, context)

    with connection.execute_wrapper(change_before_the_first_lock):
        post_view(view, request, device_id=9817)

    assert changed
    # The saved type admits the LibreNMS serial, so the write stores it with the type.
    assert Device.objects.values_list("device_type_id", "serial").get(pk=target.pk) == (new_type.pk, "SN-ABC123")


@transactional_db_with_all_apps()
def test_a_member_serial_write_waits_for_a_chassis_master_type_change(librenms_server, monkeypatch):
    """
    The master's new type keys the member's new "XABC" to "ABC", which a third device stores.

    The member write runs after the type change passed its check and before it commits; it must
    wait for the commit and then read the new type.
    """
    from threading import Event

    from dcim.models import Device, DeviceType, Manufacturer, VirtualChassis
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
    from netbox_librenms_plugin.tests.lock_conflict_helpers import wait_for_lock_wait
    from netbox_librenms_plugin.views.sync import device_fields

    master = seed_mapping(make_device("ctx-race-master", serial="MASTER1"), SERVER_KEY, own=9831)
    member = make_device("ctx-race-member", serial="OLD123")
    chassis = make_virtual_chassis("ctx-race-chassis", master, member)
    VirtualChassis.objects.filter(pk=chassis.pk).update(master=master)
    make_device("ctx-race-holder", serial="ABC")
    new_maker = Manufacturer.objects.create(name="Context race maker", slug="ctx-race-maker")
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=new_maker
    )
    new_type = DeviceType.objects.create(
        manufacturer=new_maker, model="Context race model", slug="ctx-race-model", part_number="CTX-RACE-HW"
    )
    librenms_server.device_info_response(device_id=9831, hostname=master.name, hardware=new_type.part_number)
    user = make_superuser("ctx-race-user")
    checked, release, arrived = Event(), Event(), Event()
    writer_pids = []
    original = device_fields.retype_conflict

    def check_then_wait(*args, **kwargs):
        refused = original(*args, **kwargs)
        checked.set()
        release.wait(timeout=20)
        return refused

    monkeypatch.setattr(device_fields, "retype_conflict", check_then_wait)

    def post(name, data):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '20s'")
            client = Client()
            client.force_login(user)

            def observe_lock(execute, sql, params, many, context):
                if name != "update_device_type" and (
                    "pg_advisory_xact_lock" in sql or ('"dcim_device"' in sql and "FOR NO KEY UPDATE" in sql)
                ):
                    writer_pids.append(connection.connection.info.backend_pid)
                    arrived.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe_lock):
                return client.post(reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"pk": master.pk}), data)
        finally:
            connection.close()

    with bounded_executor(max_workers=2) as executor:
        retype = executor.submit(post, "update_device_type", {"server_key": SERVER_KEY})
        assert checked.wait(timeout=20)
        assign = executor.submit(
            post,
            "assign_vc_serial",
            {"server_key": SERVER_KEY, "member_id_1": str(member.pk), "librenms_serial_1": "XABC"},
        )
        try:
            assert arrived.wait(timeout=20)
            wait_for_lock_wait(connection, writer_pids[0])
            assert not assign.done()
        finally:
            release.set()
        assert retype.result(timeout=30).status_code == 302
        assert assign.result(timeout=30).status_code == 302

    retyped = Device.objects.get(pk=master.pk).device_type_id == new_type.pk
    member_serial = Device.objects.get(pk=member.pk).serial
    assert retyped, "the type change itself must commit"
    assert member_serial == "OLD123", member_serial


@transactional_db_with_all_apps()
def test_a_member_serial_write_waits_for_a_chassis_master_change(librenms_server):
    """
    The new master's manufacturer keys the member's new "XABC" to "ABC", which a third device stores.

    Another session changes the chassis master and has not committed; the member write must wait
    for the commit and then read the context of the new master.
    """
    from dcim.models import Device, DeviceType, Manufacturer, VirtualChassis
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
    from netbox_librenms_plugin.tests.lock_conflict_helpers import second_connection, wait_for_lock_wait

    old_master = make_device("master-race-old", serial="MASTER1")
    member = make_device("master-race-member", serial="OLD123")
    new_master = make_device("master-race-new", serial="MASTER2")
    new_maker = Manufacturer.objects.create(name="Master race maker", slug="master-race-maker")
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=new_maker
    )
    new_master.device_type = DeviceType.objects.create(
        manufacturer=new_maker, model="Master race model", slug="master-race-model"
    )
    new_master.save()
    chassis = make_virtual_chassis("master-race-chassis", old_master, member, new_master)
    VirtualChassis.objects.filter(pk=chassis.pk).update(master=old_master)
    make_device("master-race-holder", serial="ABC")
    user = make_superuser("master-race-user")
    writer_pids = []

    def assign():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '20s'")
            client = Client()
            client.force_login(user)

            def observe_lock(execute, sql, params, many, context):
                if "pg_advisory_xact_lock" in sql and not writer_pids:
                    writer_pids.append(connection.connection.info.backend_pid)
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe_lock):
                return client.post(
                    reverse("plugins:netbox_librenms_plugin:assign_vc_serial", kwargs={"pk": member.pk}),
                    {"server_key": SERVER_KEY, "member_id_1": str(member.pk), "librenms_serial_1": "XABC"},
                ).status_code
        finally:
            connection.close()

    with second_connection() as other, bounded_executor(max_workers=1) as executor:
        with other.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{VirtualChassis._meta.db_table}" SET master_id = %s WHERE id = %s',
                [new_master.pk, chassis.pk],
            )
        writer = executor.submit(assign)
        try:
            deadline = time.monotonic() + 20
            while not writer_pids and time.monotonic() < deadline:
                time.sleep(0.02)
            assert writer_pids, "the member write never reached its identity lock"
            wait_for_lock_wait(connection, writer_pids[0])
            assert not writer.done()
        finally:
            other.commit()
        assert writer.result(timeout=30) == 302

    assert VirtualChassis.objects.get(pk=chassis.pk).master_id == new_master.pk
    assert Device.objects.get(pk=member.pk).serial == "OLD123"


@transactional_db_with_all_apps()
def test_a_type_change_of_a_chassis_master_waits_for_a_master_change_and_reads_the_new_scope(librenms_server):
    """
    Another session moves the master to a second member and has not committed.

    The retype of the old master must wait for the commit, then read its scope again: the old
    master no longer decides the member serials, so the scope it read first is stale and it refuses.
    """
    from dcim.models import Device, DeviceType, Manufacturer, VirtualChassis
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
    from netbox_librenms_plugin.tests.lock_conflict_helpers import second_connection, wait_for_lock_wait

    old_master = seed_mapping(make_device("rescope-old-master", serial="MASTER1"), SERVER_KEY, own=9851)
    new_master = make_device("rescope-new-master", serial="MASTER2")
    member = make_device("rescope-member", serial="MEMBER3")
    chassis = make_virtual_chassis("rescope-chassis", old_master, new_master, member)
    VirtualChassis.objects.filter(pk=chassis.pk).update(master=old_master)
    new_maker = Manufacturer.objects.create(name="Rescope maker", slug="rescope-maker")
    new_type = DeviceType.objects.create(
        manufacturer=new_maker, model="Rescope model", slug="rescope-model", part_number="RESCOPE-HW"
    )
    librenms_server.device_info_response(device_id=9851, hostname=old_master.name, hardware=new_type.part_number)
    user = make_superuser("rescope-user")
    writer_pids = []

    def retype():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '20s'")
            client = Client()
            client.force_login(user)

            def observe(execute, sql, params, many, context):
                if not writer_pids:
                    writer_pids.append(connection.connection.info.backend_pid)
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                return client.post(
                    reverse("plugins:netbox_librenms_plugin:update_device_type", kwargs={"pk": old_master.pk}),
                    {"server_key": SERVER_KEY},
                ).status_code
        finally:
            connection.close()

    with second_connection() as other, bounded_executor(max_workers=1) as executor:
        with other.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{VirtualChassis._meta.db_table}" SET master_id = %s WHERE id = %s',
                [new_master.pk, chassis.pk],
            )
        writer = executor.submit(retype)
        try:
            deadline = time.monotonic() + 20
            while not writer_pids and time.monotonic() < deadline:
                time.sleep(0.02)
            assert writer_pids, "the type change never reached the database"
            wait_for_lock_wait(connection, writer_pids[0], timeout=10)
            assert not writer.done()
        finally:
            other.commit()
        assert writer.result(timeout=30) == 302

    assert VirtualChassis.objects.get(pk=chassis.pk).master_id == new_master.pk
    assert Device.objects.get(pk=old_master.pk).device_type_id != new_type.pk


def _write_during_a_chassis_change(user, url, data, before, after=()):
    """
    POST *data* to *url* in its own session while another session holds a chassis change.

    The holder runs the *before* statements, the POST starts and must wait for a lock, then the
    holder runs the *after* statements and commits. Return the status and the SQLSTATEs the POST raised.
    """
    from django.test import Client

    from netbox_librenms_plugin.tests.lock_conflict_helpers import (
        raised_sqlstates,
        second_connection,
        wait_for_lock_wait,
    )

    writer_pids, writer_sqlstates = [], []

    def write():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '20s'")
            writer_pids.append(connection.connection.info.backend_pid)
            client = Client()
            client.force_login(user)
            with raised_sqlstates() as raised:
                status = client.post(url, data).status_code
            writer_sqlstates.extend(raised)
            return status
        finally:
            connection.close()

    with second_connection() as other, bounded_executor(max_workers=1) as executor:
        with other.cursor() as cursor:
            for sql, params in before:
                cursor.execute(sql, params)
        writer = executor.submit(write)
        deadline = time.monotonic() + 20
        while not writer_pids and time.monotonic() < deadline:
            time.sleep(0.02)
        assert writer_pids, "the write never opened its session"
        wait_for_lock_wait(connection, writer_pids[0], timeout=10)
        with other.cursor() as cursor:
            for sql, params in after:
                cursor.execute(sql, params)
        other.commit()
        status = writer.result(timeout=30)
    return status, writer_sqlstates


def _promoted_member(tag, own):
    """Return a chassis whose master is a plain device, its mapped member D, and a third device that stores "ABC"."""
    from dcim.models import DeviceType, Manufacturer, VirtualChassis

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

    old_master = make_device(f"{tag}-old-master", serial="MASTER1")
    promoted = seed_mapping(make_device(f"{tag}-promoted", serial="OLD-D1"), SERVER_KEY, own=own)
    chassis = make_virtual_chassis(f"{tag}-chassis", old_master, promoted)
    VirtualChassis.objects.filter(pk=chassis.pk).update(master=old_master)
    make_device(f"{tag}-holder", serial="ABC")
    maker = Manufacturer.objects.create(name=f"{tag} maker", slug=f"{tag}-maker")
    NormalizationRule.objects.create(scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=maker)
    rule_type = DeviceType.objects.create(
        manufacturer=maker, model=f"{tag} model", slug=f"{tag}-model", part_number=f"{tag}-HW"
    )
    return chassis, promoted, rule_type


def _set_master(chassis, device):
    from dcim.models import VirtualChassis

    return (f'UPDATE "{VirtualChassis._meta.db_table}" SET master_id = %s WHERE id = %s', [device.pk, chassis.pk])


@transactional_db_with_all_apps()
def test_a_serial_write_on_a_member_that_becomes_master_waits_and_uses_its_new_role(librenms_server):
    """As master, D's own rule keys "XABC" to the held "ABC", so the write refuses; as a member it would not."""
    from dcim.models import Device, VirtualChassis
    from django.urls import reverse

    chassis, promoted, rule_type = _promoted_member("promote-serial", 9861)
    promoted.device_type = rule_type
    promoted.save()
    librenms_server.device_info_response(device_id=9861, hostname=promoted.name, serial="XABC")
    url = reverse("plugins:netbox_librenms_plugin:update_device_serial", kwargs={"pk": promoted.pk})

    status, sqlstates = _write_during_a_chassis_change(
        make_superuser("promote-serial-user"), url, {"server_key": SERVER_KEY}, [_set_master(chassis, promoted)]
    )

    assert "40P01" not in sqlstates
    assert status == 302
    assert VirtualChassis.objects.get(pk=chassis.pk).master_id == promoted.pk
    assert Device.objects.get(pk=promoted.pk).serial == "OLD-D1"


@transactional_db_with_all_apps()
def test_a_type_change_of_a_member_that_becomes_master_waits_and_reads_its_new_scope(librenms_server):
    """As master, D decides the member serials too, so the scope that the type change read first is stale."""
    from dcim.models import Device, VirtualChassis
    from django.urls import reverse

    chassis, promoted, rule_type = _promoted_member("promote-type", 9862)
    librenms_server.device_info_response(device_id=9862, hostname=promoted.name, hardware=rule_type.part_number)
    url = reverse("plugins:netbox_librenms_plugin:update_device_type", kwargs={"pk": promoted.pk})

    status, sqlstates = _write_during_a_chassis_change(
        make_superuser("promote-type-user"), url, {"server_key": SERVER_KEY}, [_set_master(chassis, promoted)]
    )

    assert "40P01" not in sqlstates
    assert status == 302
    assert VirtualChassis.objects.get(pk=chassis.pk).master_id == promoted.pk
    assert Device.objects.get(pk=promoted.pk).device_type_id != rule_type.pk


@transactional_db_with_all_apps()
def test_a_vc_serial_assignment_and_a_chassis_edit_in_netbox_order_both_commit(librenms_server):
    """NetBox's chassis edit updates the chassis row, then the member rows, in one transaction."""
    from dcim.models import Device, VirtualChassis
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

    master = make_device("core-order-master", serial="MASTER1")
    member = make_device("core-order-member", serial="OLD-M1")
    chassis = make_virtual_chassis("core-order-chassis", master, member)
    VirtualChassis.objects.filter(pk=chassis.pk).update(master=master)
    url = reverse("plugins:netbox_librenms_plugin:assign_vc_serial", kwargs={"pk": member.pk})
    data = {"server_key": SERVER_KEY, "member_id_1": str(member.pk), "librenms_serial_1": "NEWSER1"}

    status, sqlstates = _write_during_a_chassis_change(
        make_superuser("core-order-user"),
        url,
        data,
        [(f'UPDATE "{VirtualChassis._meta.db_table}" SET name = %s WHERE id = %s', ["core-order-renamed", chassis.pk])],
        after=[(f'UPDATE "{Device._meta.db_table}" SET vc_priority = %s WHERE id = %s', [7, member.pk])],
    )

    assert "40P01" not in sqlstates
    assert status == 302
    assert VirtualChassis.objects.get(pk=chassis.pk).name == "core-order-renamed"
    assert Device.objects.filter(pk=member.pk).values_list("serial", "vc_priority").get() == ("NEWSER1", 7)


@transactional_db_with_all_apps()
@pytest.mark.parametrize("write", ["serial", "type"])
def test_a_writer_under_its_device_row_lock_never_waits_for_a_chassis_it_did_not_lock_first(write):
    """A device that joined a chassis after the writer's chassis pre-lock: the write fails at once, it does not wait."""
    from dcim.models import Device, VirtualChassis
    from django.db import OperationalError, transaction

    from netbox_librenms_plugin.serial_identity import SerialRules, device_assignment, retype_conflict, retype_scope
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
    from netbox_librenms_plugin.tests.lock_conflict_helpers import lock_timeout, second_connection

    master = make_device(f"late-chassis-{write}-master", serial="MASTER1")
    device = make_device(f"late-chassis-{write}-member", serial="OLD-L1")
    chassis = make_virtual_chassis(f"late-chassis-{write}", master, device)
    VirtualChassis.objects.filter(pk=chassis.pk).update(master=master)
    rules = SerialRules.load()

    with second_connection() as other:
        with other.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{VirtualChassis._meta.db_table}" SET name = %s WHERE id = %s', ["late-renamed", chassis.pk]
            )
        started = time.monotonic()
        with pytest.raises(OperationalError) as raised, lock_timeout(10000), transaction.atomic():
            # The row lock without lock_chassis_of() first: the state of a device that joined the chassis late.
            locked = Device.objects.select_for_update().get(pk=device.pk)
            if write == "serial":
                device_assignment(locked, "NEW-L1", rules=rules)
            else:
                retype_conflict(locked, locked.device_type, rules=rules, scope=retype_scope(locked))
        elapsed = time.monotonic() - started

    assert raised.value.__cause__.sqlstate == "55P03"
    assert elapsed < 5, f"the writer waited {elapsed:.1f}s for the chassis under its device row lock"


@transactional_db_with_all_apps()
def test_module_serial_writes_wait_for_a_type_change_of_their_device(librenms_server, monkeypatch):
    """
    Under the new type "XABC" keys to "ABC". Two module serial updates run after the type change
    passed its check and before it commits; they must not leave two modules with one identity.
    """
    from threading import Event

    from dcim.models import Device, DeviceType, Manufacturer, Module
    from django.core.cache import cache
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.lock_conflict_helpers import wait_for_lock_wait
    from netbox_librenms_plugin.views.mixins import CacheMixin
    from netbox_librenms_plugin.views.sync import device_fields

    device = make_device_with_module_bays("ctx-module-race", ["Slot 1", "Slot 2"])
    first = install_module(device, "Slot 1", "CTX-MODULE-CARD-1", serial="OLD111")
    second = install_module(device, "Slot 2", "CTX-MODULE-CARD-2", serial="OLD222")
    new_maker = Manufacturer.objects.create(name="Context module maker", slug="ctx-module-maker")
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=new_maker
    )
    new_type = DeviceType.objects.create(
        manufacturer=new_maker, model="Context module model", slug="ctx-module-model", part_number="CTX-MODULE-HW"
    )
    rows = [
        {
            "entPhysicalIndex": index,
            "entPhysicalClass": "module",
            "entPhysicalName": bay,
            "entPhysicalModelName": module.module_type.model,
            "entPhysicalSerialNum": serial,
            "entPhysicalContainedIn": 0,
        }
        for index, bay, module, serial in ((7, "Slot 1", first, "ABC"), (8, "Slot 2", second, "XABC"))
    ]
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", SERVER_KEY),
        trusted_module_inventory_payload(device, rows, librenms_id=9841),
    )
    librenms_server.device_info_response(device_id=9841, hostname=device.name, hardware=new_type.part_number)
    user = make_superuser("ctx-module-race-user")
    rendered = render_module_tab(device, user=user)
    updates = [rendered_module_form(rendered, "update-module-serial", index) for index in (7, 8)]
    checked, release, arrived = Event(), Event(), Event()
    writer_pids = []
    original = device_fields.retype_conflict

    def check_then_wait(*args, **kwargs):
        refused = original(*args, **kwargs)
        checked.set()
        release.wait(timeout=20)
        return refused

    monkeypatch.setattr(device_fields, "retype_conflict", check_then_wait)

    def session(posts, observe=False):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '20s'")

            def observe_lock(execute, sql, params, many, context):
                if observe and (
                    "pg_advisory_xact_lock" in sql or ('"dcim_device"' in sql and "FOR NO KEY UPDATE" in sql)
                ):
                    writer_pids.append(connection.connection.info.backend_pid)
                    arrived.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe_lock):
                responses = []
                for url, data in posts:
                    client = Client()
                    client.force_login(user)
                    responses.append(messages_on(client.post(url, data).wsgi_request))
                return responses
        finally:
            connection.close()

    with bounded_executor(max_workers=2) as executor:
        retype = executor.submit(
            session,
            [
                (
                    reverse("plugins:netbox_librenms_plugin:update_device_type", kwargs={"pk": device.pk}),
                    {"server_key": SERVER_KEY},
                )
            ],
        )
        assert checked.wait(timeout=20)
        modules = executor.submit(session, updates, True)
        try:
            assert arrived.wait(timeout=20)
            wait_for_lock_wait(connection, writer_pids[0])
            assert not modules.done()
        finally:
            release.set()
        retype.result(timeout=30)
        stale = modules.result(timeout=30)

    assert Device.objects.get(pk=device.pk).device_type_id == new_type.pk, "the type change itself must commit"
    assert stale[0] == [("error", GrantRefusal.POLICY.message)]
    assert stale[1] == [("error", GrantRefusal.POLICY.message)]
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.serial, second.serial) == ("OLD111", "OLD222")
    # The snapshot was merged under the old type's serial context, so a refresh stores it again under the new one.
    device.refresh_from_db()
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", SERVER_KEY),
        trusted_module_inventory_payload(device, rows, librenms_id=9841),
    )
    fresh = render_module_tab(device, user=user)
    levels = []
    for index in (7, 8):
        url, data = rendered_module_form(fresh, "update-module-serial", index)
        client = Client()
        client.force_login(user)
        levels.append([level for level, _text in messages_on(client.post(url, data).wsgi_request)])
    stored = (Module.objects.get(pk=first.pk).serial, Module.objects.get(pk=second.pk).serial)
    # Under the new type "XABC" names the hardware of "ABC", so the second update refuses.
    assert stored == ("ABC", "OLD222"), stored
    assert "error" not in levels[0] and levels[1] == ["error"], levels


@transactional_db_with_all_apps()
def test_a_module_move_reads_its_subtree_again_after_its_device_locks(client, monkeypatch):
    """Another session stores "XABC" on the moved child after the subtree read; the destination keys it to "ABC"."""
    from dcim.models import Device, DeviceType, Manufacturer, Module, ModuleBay
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.serial_identity import RELOCATION_HELD
    from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync import modules as module_views

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    source, target = make_device("move-race-source"), make_device("move-race-target")
    target_maker = Manufacturer.objects.create(name="Move race maker", slug="move-race-maker")
    NormalizationRule.objects.create(
        scope="serial", match_pattern=r"^X(.+)$", replacement=r"\1", manufacturer=target_maker
    )
    target_type = DeviceType.objects.create(manufacturer=target_maker, model="Move race model", slug="move-race-model")
    Device.objects.filter(pk=target.pk).update(device_type=target_type)
    card = make_module_type("MOVE-RACE-CARD")
    parent = Module.objects.create(
        device=source, module_bay=make_module_bay(source, "Source Bay"), module_type=card, serial="ROOT123"
    )
    child = Module.objects.create(
        device=source,
        module_bay=ModuleBay.objects.create(device=source, module=parent, name="Sub 1"),
        module_type=card,
        serial="OLD111",
    )
    Module.objects.create(device=target, module_bay=make_module_bay(target, "Held Bay"), module_type=card, serial="ABC")
    target_bay = make_module_bay(target, "Target Bay")
    original = module_views.relocation_conflict
    changed = []

    def commit_the_child_serial_first(*args, **kwargs):
        if not changed:
            changed.append(True)
            with second_connection() as other:
                commit_row_change(other, Module, child.pk, {"serial": "XABC"})
        return original(*args, **kwargs)

    monkeypatch.setattr(module_views, "relocation_conflict", commit_the_child_serial_first)
    client.force_login(make_superuser("move-race-user"))

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:move_module", kwargs={"pk": target.pk}),
        {
            "conflict_module_id": str(parent.pk),
            "target_bay_id": str(target_bay.pk),
            "selected_device_id": str(target.pk),
        },
    )

    assert changed
    assert response.status_code == 302
    # The refusal names the identity check, not another failure that also leaves the child in place.
    assert any(RELOCATION_HELD in text for level, text in messages_on(response.wsgi_request) if level == "error")
    child.refresh_from_db()
    assert (child.device_id, child.serial) != (target.pk, "XABC")


def _wait_for_held_serial_claim(writer_pid):
    """Require the writer to wait on this session's advisory claim before its Module row lock."""
    from netbox_librenms_plugin.tests.lock_conflict_helpers import wait_for_lock_wait

    wait_for_lock_wait(connection, writer_pid)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s "
            "AND locktype = 'advisory' AND NOT granted), "
            "pg_backend_pid() = ANY(pg_blocking_pids(%s))",
            [writer_pid, writer_pid],
        )
        assert cursor.fetchone() == (True, True)


@transactional_db_with_all_apps()
@pytest.mark.parametrize("guard", ["install", "replace"])
@pytest.mark.parametrize("actor", ["update", "replace"])
def test_a_module_writer_claims_its_old_identity_before_its_row_lock(client, librenms_server, guard, actor):
    from threading import Event

    from dcim.models import Module
    from django.core.cache import cache
    from django.db import DatabaseError, transaction
    from django.test import Client

    from netbox_librenms_plugin.serial_identity import SerialRules, assignment_lock, locked_type_context, resolve_module
    from netbox_librenms_plugin.views.mixins import CacheMixin
    from netbox_librenms_plugin.views.sync.modules import _module_already_on_device

    device = seed_mapping(make_device_with_module_bays("old-identity-writer", ["Slot 1"]), SERVER_KEY, own=9871)
    module = install_module(device, "Slot 1", "OLD-IDENTITY-CARD", serial="BBB")
    row = {
        "entPhysicalIndex": 7,
        "entPhysicalClass": "module",
        "entPhysicalName": "Slot 1",
        "entPhysicalModelName": module.module_type.model,
        "entPhysicalSerialNum": "CCC",
        "entPhysicalContainedIn": 0,
    }
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", SERVER_KEY),
        trusted_module_inventory_payload(device, [row], librenms_id=9871),
    )
    user = make_superuser("old-identity-writer-user")
    rendered = render_module_tab(device, user=user)
    if actor == "replace":
        from netbox_librenms_plugin.tests.view_test_helpers import rendered_preview_url

        client.force_login(user)
        preview = client.get(rendered_preview_url(rendered, 7))
        assert preview.status_code == 200
        action, fields = rendered_module_form(preview.content.decode(), "replace-module", 7)
    else:
        action, fields = rendered_module_form(rendered, "update-module-serial", 7)
    device_locked, module_locked, writer_ready = Event(), Event(), Event()
    writer_pids = []
    errors = []

    def check_conflict():
        close_old_connections()
        try:
            rules = SerialRules.load()
            with transaction.atomic():
                assignment_lock(["BBB"], model=Module, rules=rules)
                locked_type_context(device, rules=rules)
                device_locked.set()
                assert writer_ready.wait(timeout=10)
                _wait_for_held_serial_claim(writer_pids[0])
                assert not module_locked.is_set()
                if guard == "install":
                    return _module_already_on_device(device, "BBB", rules)
                return resolve_module("BBB", rules=rules, device_ids=[device.pk], lock=True).object is not None
        finally:
            connection.close()

    def update():
        close_old_connections()

        def observe(execute, sql, params, many, context):
            try:
                result = execute(sql, params, many, context)
            except DatabaseError as exc:
                errors.append(getattr(exc.__cause__, "sqlstate", None))
                raise
            if '"dcim_module"' in sql and "FOR UPDATE" in sql:
                module_locked.set()
            return result

        try:
            client = Client()
            client.force_login(user)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pids.append(cursor.fetchone()[0])
            writer_ready.set()
            assert device_locked.wait(timeout=10)
            with connection.execute_wrapper(observe):
                return client.post(action, fields).status_code
        finally:
            connection.close()

    assert run_in_threads(check_conflict, update, barriers=(), timeout=30) == [True, 302]
    assert errors == []
    assert module_locked.is_set()
    assert Module.objects.get(module_bay_id=module.module_bay_id).serial == "CCC"


@transactional_db_with_all_apps()
def test_a_move_claims_the_removed_occupants_identity_before_locking_it(librenms_server):
    from threading import Event

    from dcim.models import Module
    from django.db import DatabaseError, transaction
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.serial_identity import SerialRules, assignment_lock, locked_type_context
    from netbox_librenms_plugin.utils import netbox_relocates_module_subtree
    from netbox_librenms_plugin.views.sync.modules import _module_already_on_device

    if not netbox_relocates_module_subtree():
        pytest.skip("NetBox moves a module from 4.7.")
    source = make_device_with_module_bays("occupant-lock-source", ["Source Bay"])
    target = make_device_with_module_bays("occupant-lock-target", ["Target Bay"])
    moving = install_module(source, "Source Bay", "OCCUPANT-MOVE-CARD", serial="AAA")
    occupant = install_module(target, "Target Bay", "OCCUPANT-OLD-CARD", serial="BBB")
    user = make_superuser("occupant-lock-writer")
    mover = Client()
    mover.force_login(user)
    device_locked, occupant_locked, writer_ready = Event(), Event(), Event()
    writer_pids = []
    errors = []

    def install_check():
        close_old_connections()
        try:
            rules = SerialRules.load()
            with transaction.atomic():
                assignment_lock(["BBB"], model=Module, rules=rules)
                locked_type_context(target, rules=rules)
                device_locked.set()
                assert writer_ready.wait(timeout=10)
                _wait_for_held_serial_claim(writer_pids[0])
                assert not occupant_locked.is_set()
                return _module_already_on_device(target, "BBB", rules)
        finally:
            connection.close()

    def move():
        close_old_connections()

        def observe(execute, sql, params, many, context):
            try:
                result = execute(sql, params, many, context)
            except DatabaseError as exc:
                errors.append(getattr(exc.__cause__, "sqlstate", None))
                raise
            if '"dcim_module"' in sql and "FOR UPDATE" in sql and occupant.pk in (params or ()):
                occupant_locked.set()
            return result

        try:
            client = mover
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pids.append(cursor.fetchone()[0])
            writer_ready.set()
            assert device_locked.wait(timeout=10)
            with connection.execute_wrapper(observe):
                return client.post(
                    reverse("plugins:netbox_librenms_plugin:move_module", kwargs={"pk": target.pk}),
                    {
                        "conflict_module_id": moving.pk,
                        "target_bay_id": occupant.module_bay_id,
                        "module_id": occupant.pk,
                        "selected_device_id": target.pk,
                        "server_key": SERVER_KEY,
                    },
                ).status_code
        finally:
            connection.close()

    assert run_in_threads(install_check, move, barriers=(), timeout=30) == [True, 302]
    assert errors == []
    assert occupant_locked.is_set()
    assert Module.objects.get(pk=moving.pk).device_id == target.pk
    assert not Module.objects.filter(pk=occupant.pk).exists()


@transactional_db_with_all_apps()
@pytest.mark.parametrize(
    "old_serial", ["BBB", "", "12", "REJECTED:"], ids=["admitted", "blank", "short", "rule-rejected"]
)
@pytest.mark.parametrize("external_conflict", [False, True], ids=["target-tree", "external-conflict-tree"])
def test_a_replacement_claims_the_serials_of_children_it_deletes(
    client, librenms_server, old_serial, external_conflict
):
    from threading import Event

    from dcim.models import Module
    from django.core.cache import cache
    from django.db import DatabaseError
    from django.test import Client

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.view_test_helpers import rendered_preview_url
    from netbox_librenms_plugin.views.mixins import CacheMixin

    NormalizationRule.objects.create(scope="serial", match_pattern=r"^REJECTED:(.*)$", replacement=r"\1")
    device = seed_mapping(make_device_with_module_bays("remove-child-identities", ["Slot 1"]), SERVER_KEY, own=9872)
    parent = install_module(device, "Slot 1", "REMOVAL-PARENT-CARD", serial="AAA", child_bays=("Sub 1",))
    child_device, child_parent = device, parent
    if external_conflict:
        child_device = make_device_with_module_bays("external-removal-owner", ["Slot 1"])
        child_parent = install_module(
            child_device, "Slot 1", "REMOVAL-PARENT-CARD", serial="DDD", child_bays=("Sub 1",)
        )
    child = install_module(child_device, "Sub 1", "REMOVAL-CHILD-CARD", serial=old_serial, parent_module=child_parent)
    rows = [
        {
            "entPhysicalIndex": 7,
            "entPhysicalClass": "module",
            "entPhysicalName": "Slot 1",
            "entPhysicalModelName": parent.module_type.model,
            "entPhysicalSerialNum": "DDD",
            "entPhysicalContainedIn": 0,
        },
        {
            "entPhysicalIndex": 8,
            "entPhysicalClass": "module",
            "entPhysicalName": "Sub 1",
            "entPhysicalModelName": child.module_type.model,
            "entPhysicalSerialNum": "CCC",
            "entPhysicalContainedIn": 7,
        },
    ]
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", SERVER_KEY),
        trusted_module_inventory_payload(device, rows, librenms_id=9872),
    )
    user = make_superuser("remove-child-identities-user")
    client.force_login(user)
    rendered = render_module_tab(device, user=user)
    preview = client.get(rendered_preview_url(rendered, 7))
    assert preview.status_code == 200
    replace_action, replace_fields = rendered_module_form(preview.content.decode(), "replace-module", 7)
    if external_conflict:
        external_rows = [{**rows[0], "entPhysicalSerialNum": "DDD"}, rows[1]]
        cache.set(
            CacheMixin().get_cache_key(child_device, "inventory", SERVER_KEY),
            trusted_module_inventory_payload(child_device, external_rows),
        )
        rendered = render_module_tab(child_device, user=user)
    update_action, update_fields = rendered_module_form(rendered, "update-module-serial", 8)
    replacing_client, updating_client = Client(), Client()
    replacing_client.force_login(user)
    updating_client.force_login(user)
    device_locked, child_locked = Event(), Event()
    errors = []
    updater_pids = []
    waits = []

    from netbox_librenms_plugin.serial_identity import _LOCK_PREFIX
    from netbox_librenms_plugin.tests.lock_conflict_helpers import wait_for_lock_wait
    from netbox_librenms_plugin.utils import advisory_lock_key

    claim = advisory_lock_key(f"{_LOCK_PREFIX}:{Module._meta.label_lower}:{old_serial}")

    def updater_wait(holder_pid, waiter_pid):
        """Return (the holder has the child's claim and the waiter waits on it, the waiter waits on a row)."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks AS held JOIN pg_locks AS waiting USING "
                "(locktype, classid, objid, objsubid) WHERE locktype = 'advisory' AND held.pid = %s "
                "AND held.granted AND waiting.pid = %s AND NOT waiting.granted AND held.classid = %s "
                "AND held.objid = %s AND held.objsubid = 1), "
                "EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'transactionid' AND NOT granted)",
                [holder_pid, waiter_pid, (claim >> 32) & 0xFFFFFFFF, claim & 0xFFFFFFFF, waiter_pid],
            )
            return cursor.fetchone()

    def post(actor):
        close_old_connections()
        observed = False

        def observe(execute, sql, params, many, context):
            nonlocal observed
            if actor == "update" and (
                "pg_advisory_xact_lock" in sql or ('"dcim_device"' in sql and "FOR NO KEY UPDATE" in sql)
            ):
                updater_pids.append(connection.connection.info.backend_pid)
                child_locked.set()
            try:
                result = execute(sql, params, many, context)
            except DatabaseError as exc:
                errors.append(getattr(exc.__cause__, "sqlstate", None))
                raise
            if actor == "replace" and not observed and '"dcim_device"' in sql and "FOR NO KEY UPDATE" in sql:
                observed = True
                device_locked.set()
                assert child_locked.wait(timeout=10)
                wait_for_lock_wait(connection, updater_pids[0])
                waits.append(updater_wait(connection.connection.info.backend_pid, updater_pids[0]))
            return result

        try:
            if actor == "update":
                assert device_locked.wait(timeout=10)
            with connection.execute_wrapper(observe):
                if actor == "replace":
                    return replacing_client.post(replace_action, replace_fields).status_code
                return updating_client.post(update_action, update_fields).status_code
        finally:
            connection.close()

    assert run_in_threads(partial(post, "replace"), partial(post, "update"), barriers=(), timeout=40) == [302, 302]
    assert "40P01" not in errors
    # Only an admitted serial is an identity: the updater waits on its claim, else on the Device row.
    assert waits == [(True, False) if old_serial == "BBB" else (False, True)], waits
    assert Module.objects.get(module_bay_id=parent.module_bay_id).serial == "DDD"
    assert not Module.objects.filter(pk=child.pk).exists()


@transactional_db_with_all_apps()
def test_replacement_refuses_a_child_reparented_within_the_observed_tree(client):
    """The subtree keeps its identities, but a child changes bay before the owner lock."""
    from dcim.models import Module, ModuleBay
    from django.core.cache import cache

    from netbox_librenms_plugin.tests.lock_conflict_helpers import commit_row_change, second_connection
    from netbox_librenms_plugin.tests.view_test_helpers import rendered_preview_url
    from netbox_librenms_plugin.views.mixins import CacheMixin

    device = make_device_with_module_bays("replacement-topology", ["Slot 1"])
    parent = install_module(device, "Slot 1", "TOPOLOGY-PARENT", serial="TOPOLOGY-OLD", child_bays=("Sub 1", "Sub 2"))
    child = install_module(device, "Sub 1", "TOPOLOGY-CHILD", serial="CHILD123", parent_module=parent)
    destination = ModuleBay.objects.get(module=parent, name="Sub 2")
    row = {
        "entPhysicalIndex": 7,
        "entPhysicalClass": "module",
        "entPhysicalName": "Slot 1",
        "entPhysicalModelName": parent.module_type.model,
        "entPhysicalSerialNum": "TOPOLOGY-NEW",
        "entPhysicalContainedIn": 0,
    }
    cache.set(
        CacheMixin().get_cache_key(device, "inventory", SERVER_KEY), trusted_module_inventory_payload(device, [row])
    )
    user = make_superuser("replacement-topology-user")
    client.force_login(user)
    preview = client.get(rendered_preview_url(render_module_tab(device, user=user), 7))
    action, fields = rendered_module_form(preview.content.decode(), "replace-module", 7)
    changed = []

    def change_before_first_claim(execute, sql, params, many, context):
        if "pg_advisory_xact_lock" in sql and not changed:
            changed.append(True)
            with second_connection() as other:
                commit_row_change(other, Module, child.pk, {"module_bay_id": destination.pk})
        return execute(sql, params, many, context)

    with connection.execute_wrapper(change_before_first_claim):
        response = client.post(action, fields)

    assert changed
    assert response.status_code == 302
    assert Module.objects.filter(pk=parent.pk).exists(), "stale topology must refuse before deletion"
    child.refresh_from_db()
    assert child.module_bay_id == destination.pk


@transactional_db_with_all_apps()
def test_replacement_refuses_a_new_conflict_owner_without_locking_its_module(client):
    """A conflict found after observation must not extend the locked Device set."""
    from dcim.models import Module
    from django.core.cache import cache

    from netbox_librenms_plugin.module_writes import _CHANGED
    from netbox_librenms_plugin.tests.lock_conflict_helpers import (
        commit_row_change,
        lock_row,
        lock_timeout,
        raised_sqlstates,
        second_connection,
    )
    from netbox_librenms_plugin.tests.view_test_helpers import rendered_preview_url
    from netbox_librenms_plugin.views.mixins import CacheMixin

    target = make_device_with_module_bays("unplanned-conflict-target", ["Slot 1"])
    other = make_device_with_module_bays("unplanned-conflict-other", ["Slot 1"])
    parent = install_module(target, "Slot 1", "UNPLANNED-TARGET", serial="TARGET123")
    # The preview shows this holder, so the grant signs it.
    conflict = install_module(other, "Slot 1", "UNPLANNED-CONFLICT", serial="NEW123")
    row = {
        "entPhysicalIndex": 7,
        "entPhysicalClass": "module",
        "entPhysicalName": "Slot 1",
        "entPhysicalModelName": parent.module_type.model,
        "entPhysicalSerialNum": "NEW123",
        "entPhysicalContainedIn": 0,
    }
    cache.set(
        CacheMixin().get_cache_key(target, "inventory", SERVER_KEY), trusted_module_inventory_payload(target, [row])
    )
    user = make_superuser("unplanned-conflict-user")
    client.force_login(user)
    preview = client.get(rendered_preview_url(render_module_tab(target, user=user), 7))
    action, fields = rendered_module_form(preview.content.decode(), "replace-module", 7)
    # The holder lets go of the serial before the POST observes its owners, and takes it back after.
    Module.objects.filter(pk=conflict.pk).update(serial="OTHER123")
    changed = []
    with second_connection() as peer:

        def introduce_conflict(execute, sql, params, many, context):
            if "pg_advisory_xact_lock" in sql and not changed:
                changed.append(True)
                commit_row_change(peer, Module, conflict.pk, {"serial": "NEW123"})
                lock_row(peer, Module, conflict.pk)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(introduce_conflict), lock_timeout(200), raised_sqlstates() as sqlstates:
            response = client.post(action, fields)
    assert changed and response.status_code == 302
    # A lock timeout on the peer's Module would also leave both rows as they were.
    assert "55P03" not in sqlstates, sqlstates
    assert any(_CHANGED in text for _level, text in messages_on(response.wsgi_request))
    assert Module.objects.filter(pk=parent.pk, serial="TARGET123").exists()
    assert Module.objects.filter(pk=conflict.pk, serial="NEW123").exists()
