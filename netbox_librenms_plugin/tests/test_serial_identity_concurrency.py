"""Two real database sessions that write rule-equivalent serials, and the events of a failed inner install."""

from functools import partial
from threading import Barrier

import pytest
from django.db import IntegrityError, close_old_connections, connection

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
    rendered_module_form,
    render_module_tab,
    trusted_module_inventory_payload,
)

SERVER_KEY = "default"


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
def test_a_member_serial_write_waits_for_a_chassis_master_type_change(librenms_server, monkeypatch):
    """
    The master's new type keys the member's new "XABC" to "ABC", which a third device stores.

    The member write runs after the type change passed its check and before it commits; it must
    wait for the commit and then read the new type.
    """
    from concurrent.futures import TimeoutError as FutureTimeout
    from threading import Event

    from dcim.models import Device, DeviceType, Manufacturer, VirtualChassis
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
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
    checked, release = Event(), Event()
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
            assign.result(timeout=3)
        except FutureTimeout:
            pass
        release.set()
        assert retype.result(timeout=30).status_code == 302
        assert assign.result(timeout=30).status_code == 302

    retyped = Device.objects.get(pk=master.pk).device_type_id == new_type.pk
    member_serial = Device.objects.get(pk=member.pk).serial
    assert retyped, "the type change itself must commit"
    assert member_serial == "OLD123", member_serial


@transactional_db_with_all_apps()
def test_module_serial_writes_wait_for_a_type_change_of_their_device(librenms_server, monkeypatch):
    """
    Under the new type "XABC" keys to "ABC". Two module serial updates run after the type change
    passed its check and before it commits; they must not leave two modules with one identity.
    """
    from concurrent.futures import TimeoutError as FutureTimeout
    from threading import Event

    from dcim.models import Device, DeviceType, Manufacturer, Module
    from django.core.cache import cache
    from django.test import Client
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
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
    checked, release = Event(), Event()
    original = device_fields.retype_conflict

    def check_then_wait(*args, **kwargs):
        refused = original(*args, **kwargs)
        checked.set()
        release.wait(timeout=20)
        return refused

    monkeypatch.setattr(device_fields, "retype_conflict", check_then_wait)

    def session(posts):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '20s'")
            client = Client()
            client.force_login(user)
            return [client.post(url, data).status_code for url, data in posts]
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
        modules = executor.submit(session, updates)
        try:
            modules.result(timeout=3)
        except FutureTimeout:
            pass
        release.set()
        assert retype.result(timeout=30) == [302]
        assert modules.result(timeout=30) == [302, 302]

    assert Device.objects.get(pk=device.pk).device_type_id == new_type.pk, "the type change itself must commit"
    stored = (Module.objects.get(pk=first.pk).serial, Module.objects.get(pk=second.pk).serial)
    assert stored != ("ABC", "XABC"), stored


@transactional_db_with_all_apps()
def test_a_module_move_reads_its_subtree_again_after_its_device_locks(client, monkeypatch):
    """Another session stores "XABC" on the moved child after the subtree read; the destination keys it to "ABC"."""
    from dcim.models import Device, DeviceType, Manufacturer, Module, ModuleBay
    from django.urls import reverse

    from netbox_librenms_plugin.models import NormalizationRule
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
    child.refresh_from_db()
    assert (child.device_id, child.serial) != (target.pk, "XABC")
