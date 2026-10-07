"""Concurrency coverage for interface target validation."""

import os
from functools import partial
from threading import Event

import pytest
from django.apps import apps

from netbox_librenms_plugin.server_mappings import read_mapping
from netbox_librenms_plugin.tests.conftest import (
    _PORT_KEYS_UNSET,
    bounded_executor,
    configure_default_librenms_server,
    configured_server_key,
    make_virtual_chassis_members,
    run_in_threads,
)
from netbox_librenms_plugin.tests.interface_sync_post_helpers import SYNCED, post_interface_sync, seed_ports
from netbox_librenms_plugin.tests.view_test_helpers import messages_on
from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE


# Window a competing thread must NOT get through while the row lock is held. A negative wait
# proves only that nothing happened inside it, so keep the four sites on one name and raise it
# here (or via the environment) when a loaded runner needs more headroom.
BLOCKED_WAIT_SECONDS = float(os.environ.get("NBLP_BLOCKED_WAIT_SECONDS", "0.75"))
# Raising BLOCKED_WAIT_SECONDS makes a negative assertion stricter but would make a positive one
# weaker, so the "this must happen" wait gets its own, generous budget.
ALLOWED_WAIT_SECONDS = float(os.environ.get("NBLP_ALLOWED_WAIT_SECONDS", "5"))

pytestmark = pytest.mark.django_db(
    transaction=True,
    # Include every installed app so transaction cleanup cascades through other
    # plugins whose M2M tables are outside Django's default flush list.
    available_apps=[app.name for app in apps.get_app_configs()],
)


@pytest.fixture(autouse=True)
def restore_librenms_id_custom_field():
    """Recreate migration-seeded custom-field state after each TransactionTestCase flush."""
    from netbox_librenms_plugin import _ensure_librenms_id_custom_field

    executed_aliases = getattr(_ensure_librenms_id_custom_field, "_executed_aliases", set())
    executed_aliases.discard("default")
    _ensure_librenms_id_custom_field(sender=None, using="default")


def _post_from_this_thread(user_pk, owner, port_ids, **post_kwargs):
    """Post the interface sync with a client of this thread's connection; return the message texts."""
    from django.contrib.auth import get_user_model
    from django.test import Client

    from netbox_librenms_plugin.tests.view_test_helpers import message_texts

    client = Client()
    client.force_login(get_user_model().objects.get(pk=user_pk))
    response = post_interface_sync(client, owner, port_ids, htmx=False, **post_kwargs)
    return message_texts(response.wsgi_request)


def test_selected_vc_target_is_locked_through_interface_sync(settings, monkeypatch):
    """A membership update must wait until target validation and sync commit."""
    from dcim.models import Device, Interface
    from django.db import OperationalError, close_old_connections, connection

    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    configure_default_librenms_server(settings)
    _vc, (page_device, target_device) = make_virtual_chassis_members("sync-target-lock")
    user = make_superuser("sync-target-lock-user")
    validation_done = Event()
    release_sync = Event()

    port = {
        **_PORT_KEYS_UNSET,
        "ifName": "Gi0/1",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifAlias": "uplink",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "port_id": 101,
    }
    seed_ports(page_device, [port])
    real_resolve = SyncInterfacesView._resolve_device_interface

    def pause_after_validation(self, *args, **kwargs):
        validation_done.set()
        assert release_sync.wait(5), "test did not release the sync transaction"
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(SyncInterfacesView, "_resolve_device_interface", pause_after_validation)

    def sync_interface():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            return _post_from_this_thread(
                user.pk, page_device, [101], extra={"device_selection_101": str(target_device.pk)}
            )
        finally:
            close_old_connections()

    def move_target_out_of_chassis():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            Device.objects.filter(pk=target_device.pk).update(virtual_chassis=None, vc_position=None)
        finally:
            close_old_connections()

    with bounded_executor(max_workers=2) as executor:
        sync_future = executor.submit(sync_interface)
        assert validation_done.wait(5), "interface sync did not reach target validation"
        move_future = executor.submit(move_target_out_of_chassis)
        try:
            with pytest.raises(OperationalError, match="lock timeout"):
                move_future.result(timeout=5)
        finally:
            release_sync.set()
        texts = sync_future.result(timeout=10)

    assert texts == [SYNCED]
    assert Interface.objects.filter(device=target_device, name="Gi0/1").exists()


def _run_vlan_scope_sync(*, move_target, suffix, settings):
    """Run one VLAN-scope sync, optionally commit a target site change inside the lock window, and return its interface."""
    from types import SimpleNamespace

    from dcim.models import Device, Site
    from django.contrib.auth import get_user_model
    from django.contrib.contenttypes.models import ContentType
    from django.core.cache import cache
    from django.db import close_old_connections, connection
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, post
    from netbox_librenms_plugin.tests.conftest import seed_own_mapping
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    _vc, (page_device, target_device) = make_virtual_chassis_members(f"sync-vlan-scope-{suffix}")
    server_key = configure_default_librenms_server(settings)
    seed_own_mapping(page_device, 1, server_key)
    page_device.save()
    new_site = Site.objects.create(
        name=f"Sync VLAN New Site {suffix}", slug=f"sync-vlan-new-site-{suffix}", status="active"
    )
    site_type = ContentType.objects.get_for_model(Site)
    vlan_group = VLANGroup.objects.create(
        name=f"Sync VLAN Original Site {suffix}",
        slug=f"sync-vlan-original-site-{suffix}",
        scope_type=site_type,
        scope_id=target_device.site_id,
    )
    VLAN.objects.create(vid=100, name="Sync VLAN 100", group=vlan_group, status="active")
    user = make_superuser(f"sync-vlan-scope-{suffix}-user")
    view_template = SyncInterfacesView()
    cache_key = view_template.get_cache_key(page_device, "ports", server_key)
    cache.set(
        cache_key,
        {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": "Ethernet2",
                    "ifDescr": "Ethernet2",
                    "ifAlias": "",
                    "ifType": "ethernetCsmacd",
                    "ifSpeed": 1_000_000_000,
                    "ifPhysAddress": "",
                    "ifMtu": 1500,
                    "ifAdminStatus": "up",
                    "untagged_vlan": None,
                    "tagged_vlans": [100],
                }
            ],
            "port_stack_relationships": {},
        },
    )
    target_lock_reached = Event()
    release_sync = Event()

    class PausingSyncInterfacesView(SyncInterfacesView):
        def _lock_selected_device_targets(self, obj):
            target_lock_reached.set()
            assert release_sync.wait(5), "test did not release the interface sync"
            return super()._lock_selected_device_targets(obj)

    def sync_interface():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {
                    "select": ["10"],
                    "server_key": server_key,
                    "device_selection_10": str(target_device.pk),
                    "vlan_group_10_100": str(vlan_group.pk),
                    "exclude_columns": ["mac_address", "mtu", "speed", "type"],
                },
                user=thread_user,
            )
            request.GET = request.GET.copy()
            request.GET["interface_name_field"] = "ifName"
            view = PausingSyncInterfacesView()
            view._librenms_api = SimpleNamespace(server_key=server_key)
            response = post(view, request, object_type="device", object_id=page_device.pk)
            assert response.status_code == 302
        finally:
            close_old_connections()

    def move_target_to_new_site():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            Device.objects.filter(pk=target_device.pk).update(site=new_site)
        finally:
            close_old_connections()

    try:
        with bounded_executor(max_workers=2) as executor:
            sync_future = executor.submit(sync_interface)
            assert target_lock_reached.wait(20), "interface sync did not reach target locking"
            if move_target:
                move_future = executor.submit(move_target_to_new_site)
                try:
                    move_future.result(timeout=5)
                finally:
                    release_sync.set()
            else:
                release_sync.set()
            sync_future.result(timeout=20)
    finally:
        cache.delete(cache_key)

    return target_device.interfaces.get(name="Ethernet2")


def test_vlan_scope_assigns_the_selected_group_without_a_race(settings):
    """Positive control: the posted vlan_group key really does assign the VLAN."""
    interface = _run_vlan_scope_sync(move_target=False, suffix="no-race", settings=settings)

    assert [vlan.vid for vlan in interface.tagged_vlans.all()] == [100]


def test_vlan_scope_is_built_after_the_selected_vc_target_is_locked(settings):
    """A site change must not commit between VLAN scope resolution and interface sync."""
    interface = _run_vlan_scope_sync(move_target=True, suffix="race", settings=settings)

    # Meaningful only because the control above assigns VLAN 100 through the same POST key.
    assert list(interface.tagged_vlans.all()) == []


def test_auto_selected_owner_is_revalidated_after_vc_position_changes(client, settings):
    """The owner of a row with no posted member comes from the chassis positions read under the lock."""
    from dcim.models import Device, Interface

    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser

    configure_default_librenms_server(settings)
    _vc, (page_device, old_position_two, new_position_two) = make_virtual_chassis_members(
        "sync-auto-position",
        count=3,
    )
    port = {
        **_PORT_KEYS_UNSET,
        "port_id": 10,
        "ifName": "Ethernet2/1",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifAlias": "",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
    }
    # The page was rendered while old_position_two held position 2; the chassis changed since.
    seed_ports(page_device, [port])
    Device.objects.filter(pk=old_position_two.pk).update(vc_position=None)
    Device.objects.filter(pk=new_position_two.pk).update(vc_position=2)
    Device.objects.filter(pk=old_position_two.pk).update(vc_position=3)
    client.force_login(make_superuser("sync-auto-position-user"))

    post_interface_sync(client, page_device, [10], htmx=False, exclude_columns=())

    assert Interface.objects.filter(device=new_position_two, name="Ethernet2/1").exists()
    assert not Interface.objects.filter(device=old_position_two, name="Ethernet2/1").exists()


def test_inaccessible_selected_target_is_not_locked(settings):
    """A forged inaccessible target must not block work on that Device."""
    from dcim.models import Device, Interface
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

    configure_default_librenms_server(settings)
    page_device = make_device("restricted-target-page")
    inaccessible_target = make_device("restricted-target-hidden")
    user = make_user_with_perms("restricted-target-user", [("add", Interface), ("change", Interface)])
    user = grant(user, "view", Device, constraints={"id": page_device.pk})
    seed_ports(page_device, [{**_PORT_KEYS_UNSET, "port_id": 10, "ifName": "Ethernet1", "ifAdminStatus": "up"}])

    def sync_forged_target():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                # Below the caller's future.result(timeout=5): if this test regresses and the row
                # IS locked, the lock timeout must fire first so the failure names the real cause.
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            return _post_from_this_thread(
                user.pk,
                page_device,
                [10],
                extra={"device_selection_10": str(inaccessible_target.pk)},
                exclude_columns=("vlans", "mac_address", "description", "mtu", "speed", "type"),
            )
        finally:
            close_old_connections()

    with bounded_executor(max_workers=1) as executor:
        with transaction.atomic():
            Device.objects.select_for_update().get(pk=inaccessible_target.pk)
            future = executor.submit(sync_forged_target)
            texts = future.result(timeout=5)

    # The row is refused without a wait on the target: the sync ran, not the lock-conflict answer.
    assert texts == ["1 interface(s) skipped: Ethernet1 (selected target unavailable)."]


def test_viewable_outside_selected_target_is_not_locked(settings):
    """A target outside the page Device scope must not be locked."""
    from dcim.models import Device
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser

    configure_default_librenms_server(settings)
    page_device = make_device("outside-target-page")
    outside_target = make_device("outside-target-device")
    user = make_superuser("outside-target-user")
    seed_ports(page_device, [{**_PORT_KEYS_UNSET, "port_id": 10, "ifName": "Ethernet1", "ifAdminStatus": "up"}])

    def sync_forged_target():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                # Below the caller's future.result(timeout=5): if this test regresses and the row
                # IS locked, the lock timeout must fire first so the failure names the real cause.
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            return _post_from_this_thread(
                user.pk,
                page_device,
                [10],
                extra={"device_selection_10": str(outside_target.pk)},
                exclude_columns=("vlans", "mac_address", "description", "mtu", "speed", "type"),
            )
        finally:
            close_old_connections()

    with bounded_executor(max_workers=1) as executor:
        with transaction.atomic():
            Device.objects.select_for_update().get(pk=outside_target.pk)
            future = executor.submit(sync_forged_target)
            texts = future.result(timeout=5)

    # The row is refused without a wait on the target: the sync ran, not the lock-conflict answer.
    assert texts == ["1 interface(s) skipped: Ethernet1 (selected target unavailable)."]


def test_selected_vc_targets_lock_chassis_before_devices(client, settings):
    """Bulk sync must use the same chassis-first lock order as relationship sync."""
    from django.db import connection

    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser

    configure_default_librenms_server(settings)
    _virtual_chassis, (page_device, target_device) = make_virtual_chassis_members("bulk-lock-order")
    seed_ports(page_device, [{**_PORT_KEYS_UNSET, "port_id": 10, "ifName": "Ethernet1"}])
    client.force_login(make_superuser("bulk-lock-order-user"))
    locked_selects = []

    def record_locked_select(execute, sql, params, many, context):
        if "FOR UPDATE" in sql.upper():
            locked_selects.append(sql.lower())
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record_locked_select):
        post_interface_sync(client, page_device, [10], htmx=False, extra={"device_selection_10": str(target_device.pk)})

    chassis_lock = next(index for index, sql in enumerate(locked_selects) if "dcim_virtualchassis" in sql)
    device_lock = next(index for index, sql in enumerate(locked_selects) if '"dcim_device"' in sql)
    assert chassis_lock < device_lock


def test_vm_sync_serializes_duplicate_display_name_resolution(settings, monkeypatch):
    """A second VM sync must not resolve the same unbound natural-key row concurrently."""
    from django.db import close_old_connections, connection
    from virtualization.models import VMInterface

    from netbox_librenms_plugin.tests.conftest import make_vm
    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    server_key = configure_default_librenms_server(settings)
    vm = make_vm("vm-duplicate-name-lock")
    VMInterface.objects.create(virtual_machine=vm, name="Ethernet")
    user = make_superuser("vm-duplicate-name-lock-user")
    resolved = {10: Event(), 11: Event()}
    second_attempt_started = Event()
    release_first = Event()
    real_resolve = SyncInterfacesView._resolve_vm_interface
    real_attempt = SyncInterfacesView._sync_attempt

    def pause_after_resolution(self, vm, interface_name, port_id, *args, **kwargs):
        interface = real_resolve(self, vm, interface_name, port_id, *args, **kwargs)
        resolved[port_id].set()
        if port_id == 10:
            assert release_first.wait(5), "test did not release the first VM sync"
        return interface

    def observed_attempt(self, visible_port_ids, *args, **kwargs):
        # The second post read its snapshot before it starts the attempt.
        if 11 in visible_port_ids:
            second_attempt_started.set()
        return real_attempt(self, visible_port_ids, *args, **kwargs)

    monkeypatch.setattr(SyncInterfacesView, "_resolve_vm_interface", pause_after_resolution)
    monkeypatch.setattr(SyncInterfacesView, "_sync_attempt", observed_attempt)

    def sync_port(port_id):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            port = {
                **_PORT_KEYS_UNSET,
                "port_id": port_id,
                "ifName": f"Ethernet{port_id}",
                "ifDescr": "Ethernet",
                "ifAdminStatus": "up",
            }
            # Each sync reads its own snapshot, as two refreshes of the page would give.
            seed_ports(vm, [port])
            return _post_from_this_thread(
                user.pk,
                vm,
                [port_id],
                interface_name_field="ifDescr",
                exclude_columns=("vlans", "mac_address", "description", "mtu", "speed", "type"),
            )
        finally:
            close_old_connections()

    with bounded_executor(max_workers=2) as executor:
        first_future = executor.submit(sync_port, 10)
        assert resolved[10].wait(5), "first VM sync did not resolve the interface"
        second_future = executor.submit(sync_port, 11)
        assert second_attempt_started.wait(5), "second VM sync did not start its attempt"
        resolved_during_first = resolved[11].wait(BLOCKED_WAIT_SECONDS)
        release_first.set()
        first_texts = first_future.result(timeout=10)
        second_texts = second_future.result(timeout=10)

    interface = VMInterface.objects.get(virtual_machine=vm, name="Ethernet")
    assert not resolved_during_first
    assert read_mapping(interface).own_id(server_key) == 10, (
        interface.custom_field_data,
        first_texts,
        second_texts,
    )


def test_relationship_write_locks_virtual_chassis_members_through_validation():
    """A membership update must wait until relationship validation and persistence commit."""
    from types import SimpleNamespace

    from dcim.models import Device
    from django.contrib.auth import get_user_model
    from django.core.cache import cache
    from django.db import close_old_connections, connection

    from netbox_librenms_plugin.tests.conftest import make_interface
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, post
    from netbox_librenms_plugin.tests.conftest import seed_own_mapping
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

    server_key = configured_server_key()

    _vc, (aggregate_device, member_device) = make_virtual_chassis_members("relationship-scope-lock")
    aggregate = make_interface(aggregate_device, "Port-Channel1", iface_type="lag")
    member = make_interface(member_device, "Ethernet2")
    seed_own_mapping(aggregate, 20, server_key)
    seed_own_mapping(member, 10, server_key)
    aggregate.save()
    member.save()
    user = make_superuser("relationship-scope-lock-user")
    cache_key = SyncInterfaceLagView().get_cache_key(aggregate_device, "ports", server_key)
    cache.set(
        cache_key,
        {
            "ports": [
                {**_PORT_KEYS_UNSET, "port_id": 10, "ifName": member.name},
                {**_PORT_KEYS_UNSET, "port_id": 20, "ifName": aggregate.name},
            ],
            "port_stack_relationships": {"lag_members": {10: 20}, "sub_interfaces": {}},
        },
    )
    validation_reached = Event()
    release_relationship = Event()
    membership_changed = Event()

    def write_relationship():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {"port_id": "10", "lag_port_id": "20", "lag_name": "Port-Channel1"},
                user=thread_user,
            )
            view = SyncInterfaceLagView()
            view._librenms_api = SimpleNamespace(server_key=server_key)
            prepare_related = view._prepare_related

            def pause_before_validation(related_interface):
                validation_reached.set()
                assert release_relationship.wait(5), "test did not release the relationship transaction"
                return prepare_related(related_interface)

            view._prepare_related = pause_before_validation
            response = post(
                view,
                request,
                object_type="device",
                object_id=member_device.pk,
            )
            assert response.status_code == 200, response.content
        finally:
            close_old_connections()

    def move_member_out_of_chassis():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            Device.objects.filter(pk=aggregate_device.pk).update(virtual_chassis=None, vc_position=None)
            membership_changed.set()
        finally:
            close_old_connections()

    try:
        with bounded_executor(max_workers=2) as executor:
            relationship_future = executor.submit(write_relationship)
            if not validation_reached.wait(5):
                release_relationship.set()
                relationship_future.result(timeout=10)
                pytest.fail("relationship sync did not reach validation")
            membership_future = executor.submit(move_member_out_of_chassis)
            changed_during_relationship = membership_changed.wait(BLOCKED_WAIT_SECONDS)
            release_relationship.set()
            relationship_future.result(timeout=10)
            membership_future.result(timeout=10)

        member.refresh_from_db()
        assert not changed_during_relationship
        assert member.lag_id == aggregate.pk
    finally:
        cache.delete(cache_key)


def test_inline_relationship_rechecks_migrated_donor_after_lock():
    """A donor migrated while the request waits for its lock must stay read-only."""
    from types import SimpleNamespace

    from dcim.models import Device
    from django.contrib.auth import get_user_model
    from django.core.cache import cache
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, post
    from netbox_librenms_plugin.server_mappings import mark_migrated
    from netbox_librenms_plugin.tests.conftest import seed_own_mapping
    from netbox_librenms_plugin.tests.mapping_fixtures import apply_mapping_change
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

    server_key = configured_server_key()

    donor = make_device("relationship-migrated-donor")
    winner = make_device("relationship-migrated-winner")
    child = make_interface(donor, "Ethernet1.100", iface_type="virtual")
    parent = make_interface(donor, "Ethernet1")
    seed_own_mapping(child, 10, server_key)
    seed_own_mapping(parent, 20, server_key)
    child.save()
    parent.save()
    user = make_superuser("relationship-migrated-user")
    view_template = SyncInterfaceParentView()
    cache_key = view_template.get_cache_key(donor, "ports", server_key)
    cache.set(
        cache_key,
        {
            "ports": [
                {"port_id": 10, "ifName": child.name},
                {"port_id": 20, "ifName": parent.name},
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        },
    )
    donor_locked = Event()
    request_checked_cache = Event()

    class PausingSyncInterfaceParentView(SyncInterfaceParentView):
        def _get_current_edge(self, *args, **kwargs):
            edge = super()._get_current_edge(*args, **kwargs)
            request_checked_cache.set()
            return edge

    def migrate_donor():
        close_old_connections()
        try:
            with transaction.atomic():
                locked_donor = Device.objects.select_for_update().get(pk=donor.pk)
                donor_locked.set()
                assert request_checked_cache.wait(5), "relationship request did not reach cache validation"
                apply_mapping_change(locked_donor, mark_migrated(locked_donor, winner.pk, server_key))
                locked_donor.save()
        finally:
            close_old_connections()

    def write_relationship():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {"port_id": "10", "parent_port_id": "20"},
                user=thread_user,
            )
            view = PausingSyncInterfaceParentView()
            view._librenms_api = SimpleNamespace(server_key=server_key)
            return post(view, request, object_type="device", object_id=donor.pk)
        finally:
            close_old_connections()

    try:
        with bounded_executor(max_workers=2) as executor:
            migration_future = executor.submit(migrate_donor)
            assert donor_locked.wait(5), "migration did not lock the donor"
            relationship_future = executor.submit(write_relationship)
            response = relationship_future.result(timeout=10)
            migration_future.result(timeout=10)
    finally:
        cache.delete(cache_key)

    assert response.status_code == 409
    child.refresh_from_db()
    assert child.parent_id is None


def test_inline_relationship_does_not_lock_unrelated_interfaces():
    """A single parent update must not lock every interface on the Device."""
    from types import SimpleNamespace

    from django.contrib.auth import get_user_model
    from django.core.cache import cache
    from django.db import close_old_connections, connection

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, post
    from netbox_librenms_plugin.tests.conftest import seed_own_mapping
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

    server_key = configured_server_key()

    device = make_device("targeted-inline-lock")
    child = make_interface(device, "Ethernet1.100", iface_type="virtual")
    parent = make_interface(device, "Ethernet1")
    unrelated = make_interface(device, "Ethernet99")
    seed_own_mapping(child, 10, server_key)
    seed_own_mapping(parent, 20, server_key)
    child.save()
    parent.save()
    user = make_superuser("targeted-inline-lock-user")
    view_template = SyncInterfaceParentView()
    cache_key = view_template.get_cache_key(device, "ports", server_key)
    cache.set(
        cache_key,
        {
            "ports": [
                {**_PORT_KEYS_UNSET, "port_id": 10, "ifName": child.name},
                {**_PORT_KEYS_UNSET, "port_id": 20, "ifName": parent.name},
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        },
    )
    validation_reached = Event()
    release_relationship = Event()
    unrelated_updated = Event()

    def write_relationship():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {"port_id": "10", "parent_port_id": "20"},
                user=thread_user,
            )
            view = SyncInterfaceParentView()
            view._librenms_api = SimpleNamespace(server_key=server_key)

            def pause_before_validation(_related_interface):
                validation_reached.set()
                assert release_relationship.wait(5), "test did not release the relationship transaction"

            view._prepare_related = pause_before_validation
            response = post(view, request, object_type="device", object_id=device.pk)
            assert response.status_code == 200, response.content
        finally:
            close_old_connections()

    def update_unrelated():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            type(unrelated).objects.filter(pk=unrelated.pk).update(description="updated concurrently")
            unrelated_updated.set()
        finally:
            close_old_connections()

    try:
        with bounded_executor(max_workers=2) as executor:
            relationship_future = executor.submit(write_relationship)
            assert validation_reached.wait(5), "relationship sync did not reach validation"
            update_future = executor.submit(update_unrelated)
            # Bounded, so a lock that wrongly blocks this update fails the test instead of hanging it.
            update_future.result(timeout=10)
            assert unrelated_updated.is_set()
            assert not relationship_future.done()
            release_relationship.set()
            relationship_future.result(timeout=10)
    finally:
        cache.delete(cache_key)

    child.refresh_from_db()
    unrelated.refresh_from_db()
    assert child.parent_id == parent.pk
    assert unrelated.description == "updated concurrently"


def test_bulk_relationship_pass_skips_scope_locks_without_selected_edges(client, settings, monkeypatch):
    """An unrelated cached edge must not make the relationship pass take the scope locks for a selected access port."""
    from django.db import connection

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
    from netbox_librenms_plugin.tests.conftest import seed_own_mapping
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    server_key = configure_default_librenms_server(settings)
    device = make_device("bulk-unrelated-edge")
    selected = make_interface(device, "Ethernet1")
    child = make_interface(device, "Ethernet2.100", iface_type="virtual")
    parent = make_interface(device, "Ethernet2")
    for interface, port_id in ((selected, 10), (child, 20), (parent, 30)):
        seed_own_mapping(interface, port_id, server_key)
        interface.save()
    ports = [
        {**_PORT_KEYS_UNSET, "port_id": 10, "ifName": selected.name},
        {**_PORT_KEYS_UNSET, "port_id": 20, "ifName": child.name},
        {**_PORT_KEYS_UNSET, "port_id": 30, "ifName": parent.name},
    ]
    seed_ports(device, ports, sub_interfaces={20: 30})
    client.force_login(make_superuser("bulk-unrelated-edge-user"))
    pass_locks = []
    real_pass = SyncInterfacesView._sync_interface_relationships

    def record_locked_select(execute, sql, params, many, context):
        if "FOR UPDATE" in sql.upper():
            pass_locks.append(sql)
        return execute(sql, params, many, context)

    def observed_pass(self, *args, **kwargs):
        # The real attempt calls the pass with its own selection; this only records the locks of the pass.
        with connection.execute_wrapper(record_locked_select):
            return real_pass(self, *args, **kwargs)

    monkeypatch.setattr(SyncInterfacesView, "_sync_interface_relationships", observed_pass)

    post_interface_sync(client, device, [10], htmx=False)

    assert pass_locks == []
    child.refresh_from_db()
    assert child.parent_id is None


def test_bulk_relationship_pass_does_not_lock_unrelated_interfaces(settings):
    """A selected parent edge must lock only its source and related candidates."""
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
    from netbox_librenms_plugin.tests.conftest import seed_own_mapping

    server_key = configure_default_librenms_server(settings)

    device = make_device("bulk-targeted-edge")
    child = make_interface(device, "Ethernet1.100", iface_type="virtual")
    parent = make_interface(device, "Ethernet1")
    unrelated = make_interface(device, "Ethernet99")
    for interface, port_id in ((child, 10), (parent, 20), (unrelated, 30)):
        seed_own_mapping(interface, port_id, server_key)
        interface.save()
    user = make_superuser("bulk-targeted-edge-user")
    ports = [
        {**_PORT_KEYS_UNSET, "port_id": 10, "ifName": child.name},
        {**_PORT_KEYS_UNSET, "port_id": 20, "ifName": parent.name},
        {**_PORT_KEYS_UNSET, "port_id": 30, "ifName": unrelated.name},
    ]
    seed_ports(device, ports, sub_interfaces={10: 20})

    def post_the_sync():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            return _post_from_this_thread(user.pk, device, [10])
        finally:
            close_old_connections()

    with bounded_executor(max_workers=1) as executor:
        with transaction.atomic():
            type(unrelated).objects.select_for_update().get(pk=unrelated.pk)
            future = executor.submit(post_the_sync)
            texts = future.result(timeout=5)

    assert texts == [SYNCED]
    child.refresh_from_db()
    assert child.parent_id == parent.pk


def test_relationship_scope_lock_blocks_new_virtual_chassis_members():
    """The relationship scope must not gain an unlocked member after enumeration."""
    from dcim.models import Device
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.views.sync.interfaces import _lock_relationship_scope

    virtual_chassis, (page_device, _member) = make_virtual_chassis_members("relationship-phantom-member")
    joining_device = make_device("relationship-phantom-joining")
    scope_locked = Event()
    release_scope = Event()
    member_joined = Event()

    def lock_scope():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            with transaction.atomic():
                page = Device.objects.get(pk=page_device.pk)
                locked_page, _locked_ids = _lock_relationship_scope(page)
                assert locked_page is not None
                scope_locked.set()
                assert release_scope.wait(5), "test did not release the relationship scope"
        finally:
            close_old_connections()

    def join_scope():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            Device.objects.filter(pk=joining_device.pk).update(
                virtual_chassis=virtual_chassis,
                vc_position=3,
            )
            member_joined.set()
        finally:
            close_old_connections()

    with bounded_executor(max_workers=2) as executor:
        lock_future = executor.submit(lock_scope)
        assert scope_locked.wait(5), "relationship scope was not locked"
        join_future = executor.submit(join_scope)
        joined_while_scope_locked = member_joined.wait(BLOCKED_WAIT_SECONDS)
        release_scope.set()
        lock_future.result(timeout=10)
        join_future.result(timeout=10)

    assert not joined_while_scope_locked


@pytest.mark.parametrize(
    "winner_kind,loser_path", [("device", "sync"), ("virtualmachine", "sync"), ("virtualmachine", "module")]
)
def test_concurrent_cross_model_port_claim_refuses_without_partial_writes(settings, winner_kind, loser_path):
    """An uncommitted binding excludes another owner before it can create or update a row."""
    from dcim.models import Interface
    from django.db import close_old_connections, connections, transaction
    from django.test import Client
    from virtualization.models import VMInterface

    from netbox_librenms_plugin.interface_rules import InterfaceRuleMatcher
    from netbox_librenms_plugin.interface_sync import resolve_or_create_interface_from_port
    from netbox_librenms_plugin.tests.conftest import make_cluster, make_device, make_interface, make_superuser, make_vm
    from netbox_librenms_plugin.tests.test_interface_port_binding_cross_model import _port, _sync
    from netbox_librenms_plugin.server_mappings import LibreNMSPortBindingBusy
    from netbox_librenms_plugin.views.sync.modules import _bind_interface_librenms_id

    server_key = configure_default_librenms_server(settings)
    device = make_device("claim-device", librenms_cf={server_key: {"id": 71}})
    vm = make_vm("claim-vm", make_cluster("claim-cluster"))
    winner, loser = (device, vm) if winner_kind == "device" else (vm, device)
    winner_model = Interface if winner_kind == "device" else VMInterface
    loser_model = VMInterface if winner_kind == "device" else Interface
    user = make_superuser("claim-user")
    loser_interface = make_interface(device, "eth0") if loser_path == "module" else None
    client = Client()
    client.force_login(user)

    def competing_write():
        close_old_connections()
        try:
            if loser_path == "module":
                return _bind_interface_librenms_id(
                    device,
                    {"_librenms_port_id": "009301", "_librenms_ifname": "eth0"},
                    None,
                    server_key,
                    Interface.objects.all(),
                )
            return _sync(client, loser, "virtualmachine" if winner_kind == "device" else "device", "009301")
        finally:
            connections.close_all()

    with bounded_executor(max_workers=1) as executor:
        with transaction.atomic():
            held = resolve_or_create_interface_from_port(
                winner,
                _port(9301, "eth0"),
                rules=InterfaceRuleMatcher(()),
                server_key=server_key,
                interface_name_field="ifName",
                changeable_queryset=winner_model.objects.all(),
                viewable_queryset=winner_model.objects.all(),
                addable_queryset=winner_model.objects.all(),
            )
            if loser_path == "module":
                with pytest.raises(LibreNMSPortBindingBusy):
                    executor.submit(competing_write).result(timeout=10)
                loser_interface.refresh_from_db()
                assert read_mapping(loser_interface).own_id(server_key) is None
            else:
                from django.contrib.messages import get_messages

                response = executor.submit(competing_write).result(timeout=10)
                assert [str(message) for message in get_messages(response.wsgi_request)] == [TRY_AGAIN_MESSAGE]
                owner_filter = {"virtual_machine": loser} if winner_kind == "device" else {"device": loser}
                assert not loser_model.objects.filter(**owner_filter).exists()
        held.refresh_from_db()
        assert read_mapping(held).own_id(server_key) == 9301


def test_port_claims_are_reentrant_isolated_by_server_and_released_on_rollback():
    """Real PostgreSQL claims canonicalize IDs and do not block opposite-order batches."""
    from django.db import close_old_connections, connections, transaction
    from netbox_librenms_plugin.server_mappings import LibreNMSPortBindingBusy, claim_librenms_port_binding

    def compete():
        close_old_connections()
        try:
            with transaction.atomic():
                claim_librenms_port_binding(9301, "other")
                claim_librenms_port_binding(9302, "default")
                with pytest.raises(LibreNMSPortBindingBusy):
                    claim_librenms_port_binding("009301", "default")
                return True
        finally:
            connections.close_all()

    with pytest.raises(RuntimeError, match="open transaction"):
        claim_librenms_port_binding(9301, "default")
    with bounded_executor(max_workers=1) as executor:
        with transaction.atomic():
            claim_librenms_port_binding(9301, "default")
            claim_librenms_port_binding("009301", "default")
            assert executor.submit(compete).result(timeout=5)
            transaction.set_rollback(True)

        # A different connection can now claim the released identity.
        def after_rollback():
            close_old_connections()
            try:
                with transaction.atomic():
                    claim_librenms_port_binding(9301, "default")
            finally:
                connections.close_all()

        executor.submit(after_rollback).result(timeout=5)


@pytest.mark.parametrize("action", ["rebind", "cable"])
@pytest.mark.parametrize("htmx", [False, True])
def test_direct_actions_refuse_a_concurrent_port_claim_without_leftovers(settings, librenms_server, action, htmx):
    """Both browser response paths deliver the retry message without partial writes."""
    from dcim.models import Interface
    from django.db import close_old_connections, connections, transaction
    from django.test import Client
    from django.urls import reverse
    from netbox_librenms_plugin.tests.conftest import bind_librenms_server, make_superuser
    from netbox_librenms_plugin.tests.test_interface_name_owner_rebind import (
        HOST_PORT,
        STALE_PORT,
        _binding,
        _bound_interface,
        _device,
        _port,
        _seed,
    )
    from netbox_librenms_plugin.tests.test_interface_port_binding_cross_model import _cable_scenario
    from netbox_librenms_plugin.server_mappings import claim_librenms_port_binding

    server_key = configure_default_librenms_server(settings)
    bind_librenms_server(settings, librenms_server, server_key=server_key)
    client = Client()
    client.force_login(make_superuser("claim-response-user"))
    if action == "rebind":
        owner = _device("claim-rebind")
        existing = _bound_interface(owner, "eth0", STALE_PORT)
        _seed(owner, [_port(HOST_PORT, "eth0")])
        port_id = HOST_PORT
        url = reverse(
            "plugins:netbox_librenms_plugin:rebind_interface_port",
            kwargs={"object_type": "device", "object_id": owner.pk},
        )
        data = {
            "server_key": server_key,
            "interface_name_field": "ifName",
            "rebind_one": str(port_id),
            f"rebind_expected_port_{port_id}": str(STALE_PORT),
        }
    else:
        server_key, owner, existing, remote, row_id = _cable_scenario(librenms_server, settings, "claim-cable")
        port_id = 500
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_create", args=[owner.pk])
        data = {"expected_local_id": existing.pk, "row_id": row_id, "server_key": server_key}

    page = reverse("dcim:device_librenms_sync", kwargs={"pk": owner.pk}) + (
        f"?tab={'interfaces' if action == 'rebind' else 'cables'}"
    )

    def compete():
        close_old_connections()
        try:
            headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
            return client.post(url, data, HTTP_REFERER=f"http://testserver{page}", **headers)
        finally:
            connections.close_all()

    with bounded_executor(max_workers=1) as executor:
        with transaction.atomic():
            claim_librenms_port_binding(port_id, server_key)
            response = executor.submit(compete).result(timeout=10)
            # A busy claim is a lock conflict: the middleware gives its one "try again" answer.
            if htmx:
                assert response.status_code == 200
                assert TRY_AGAIN_MESSAGE in response.content.decode()
            else:
                assert (response.status_code, response["Location"]) == (302, f"http://testserver{page}")
                assert messages_on(response.wsgi_request) == [("error", TRY_AGAIN_MESSAGE)]
    if action == "rebind":
        assert _binding(existing) == STALE_PORT
    else:
        assert not Interface.objects.filter(device=remote).exists()
        existing.refresh_from_db()
        assert existing.cable is None


def test_run_in_threads_raises_the_failure_that_broke_the_barrier_at_once():
    """A peer at the barrier fails at once, and the error of the failed call is raised, not BrokenBarrierError."""
    from threading import Barrier
    from time import monotonic

    barrier = Barrier(2)

    def fail():
        raise ValueError("the real failure")

    started = monotonic()
    with pytest.raises(ValueError, match="the real failure"):
        run_in_threads(partial(barrier.wait, timeout=5), fail, barriers=(barrier,), timeout=10)
    assert monotonic() - started < 2


def test_run_in_threads_fails_a_hung_call_within_its_timeout():
    """A hung call fails the run at the deadline, and a peer at the barrier is released at once."""
    from threading import Barrier, BrokenBarrierError, Timer
    from time import monotonic

    barrier = Barrier(2)
    hung = Event()
    # Release the hung thread later, so a helper that waits for it ends instead of hanging the session.
    release = Timer(3, hung.set)
    release.start()
    peer_released = Event()

    def peer():
        try:
            barrier.wait(timeout=30)
        except BrokenBarrierError:
            peer_released.set()

    started = monotonic()
    try:
        with pytest.raises(TimeoutError):
            run_in_threads(hung.wait, peer, barriers=(barrier,), timeout=0.5)
        assert monotonic() - started < 2
    finally:
        hung.set()
        release.cancel()
    assert peer_released.wait(timeout=2)


def test_bounded_executor_fails_a_hung_worker_within_its_timeout():
    """A result timeout inside the block raises at once and does not wait for the hung worker."""
    from threading import Timer
    from time import monotonic

    hung = Event()
    # Release the hung worker later, so a helper that waits for it fails on time instead of hanging the session.
    release = Timer(3, hung.set)
    release.start()
    started = monotonic()
    try:
        with pytest.raises(TimeoutError), bounded_executor(max_workers=1) as executor:
            executor.submit(hung.wait).result(timeout=0.5)
        assert monotonic() - started < 2
    finally:
        hung.set()
        release.cancel()


def test_opposite_order_port_claims_refuse_without_deadlock():
    from threading import Barrier
    from django.db import close_old_connections, connections, transaction
    from netbox_librenms_plugin.server_mappings import LibreNMSPortBindingBusy, claim_librenms_port_binding

    first_claims_ready = Barrier(2)
    second_claims_done = Barrier(2)

    def claim_in_order(first, second):
        close_old_connections()
        try:
            with transaction.atomic():
                claim_librenms_port_binding(first, "default")
                first_claims_ready.wait(timeout=5)
                with pytest.raises(LibreNMSPortBindingBusy):
                    claim_librenms_port_binding(second, "default")
                # Both transactions retain their first claim until both try the second.
                second_claims_done.wait(timeout=5)
        finally:
            connections.close_all()

    run_in_threads(
        partial(claim_in_order, 9301, 9302),
        partial(claim_in_order, 9302, 9301),
        barriers=(first_claims_ready, second_claims_done),
        timeout=10,
    )
