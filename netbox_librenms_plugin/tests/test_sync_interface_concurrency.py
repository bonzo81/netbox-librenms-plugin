"""Concurrency coverage for interface target validation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import os

import pytest
from django.apps import apps

from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members

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


def test_selected_vc_target_is_locked_through_interface_sync():
    """A membership update must wait until target validation and sync commit."""
    from dcim.models import Device, Interface
    from django.contrib.auth import get_user_model
    from django.db import OperationalError, close_old_connections, connection

    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, make_view
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    _vc, (page_device, target_device) = make_virtual_chassis_members("sync-target-lock")
    user = make_superuser("sync-target-lock-user")
    validation_done = Event()
    release_sync = Event()

    port = {
        "ifName": "Gi0/1",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifAlias": "uplink",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "port_id": 101,
    }

    def sync_interface():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            page = Device.objects.get(pk=page_device.pk)
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {"device_selection_101": str(target_device.pk)},
                user=thread_user,
            )
            view = make_view(SyncInterfacesView, request)
            view._post_server_key = "default"
            view._selected_port_ids = {101}
            view._auto_selected_port_ids = set()
            real_resolve = view._resolve_device_interface

            def pause_after_validation(*args, **kwargs):
                validation_done.set()
                assert release_sync.wait(5), "test did not release the sync transaction"
                return real_resolve(*args, **kwargs)

            view._resolve_device_interface = pause_after_validation
            view.sync_selected_interfaces(page, [port], ["vlans"], "ifName")
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

    with ThreadPoolExecutor(max_workers=2) as executor:
        sync_future = executor.submit(sync_interface)
        assert validation_done.wait(5), "interface sync did not reach target validation"
        move_future = executor.submit(move_target_out_of_chassis)
        try:
            with pytest.raises(OperationalError, match="lock timeout"):
                move_future.result(timeout=5)
        finally:
            release_sync.set()
        sync_future.result(timeout=10)

    assert Interface.objects.filter(device=target_device, name="Gi0/1").exists()


def _run_vlan_scope_sync(*, move_target, suffix):
    """Run the VLAN-scope sync once and return the synced interface.

    ``move_target`` commits the target's site change inside the lock window. The caller with
    ``move_target=False`` is the positive control: it proves the POST keys and the row itself
    reach VLAN assignment, so an empty result in the racing run means the scope was rejected
    rather than never attempted.
    """
    from types import SimpleNamespace

    from dcim.models import Device, Site
    from django.contrib.auth import get_user_model
    from django.contrib.contenttypes.models import ContentType
    from django.core.cache import cache
    from django.db import close_old_connections, connection
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.librenms_api import LibreNMSAPI
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, post
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    _vc, (page_device, target_device) = make_virtual_chassis_members(f"sync-vlan-scope-{suffix}")
    # Use the real configured key. The devcontainer and CI use different names, and the request
    # must pass the configured-server guard before this test can reach the lock boundary.
    server_key = next(iter(LibreNMSAPI.get_available_servers()))
    set_librenms_device_id(page_device, 1, server_key)
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
        with ThreadPoolExecutor(max_workers=2) as executor:
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


def test_vlan_scope_assigns_the_selected_group_without_a_race():
    """Positive control: the posted vlan_group key really does assign the VLAN."""
    interface = _run_vlan_scope_sync(move_target=False, suffix="no-race")

    assert [vlan.vid for vlan in interface.tagged_vlans.all()] == [100]


def test_vlan_scope_is_built_after_the_selected_vc_target_is_locked():
    """A site change must not commit between VLAN scope resolution and interface sync."""
    interface = _run_vlan_scope_sync(move_target=True, suffix="race")

    # Meaningful only because the control above assigns VLAN 100 through the same POST key.
    assert list(interface.tagged_vlans.all()) == []


def test_auto_selected_owner_is_revalidated_after_vc_position_changes():
    """An owner guessed before locking must not survive a later chassis-position change."""
    from dcim.models import Device, Interface

    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, make_view
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    _vc, (page_device, old_position_two, new_position_two) = make_virtual_chassis_members(
        "sync-auto-position",
        count=3,
    )
    port = {
        "port_id": 10,
        "ifName": "Ethernet2/1",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifAlias": "",
        "ifMtu": 1500,
        "ifAdminStatus": "up",
    }
    request = make_request("post", user=make_superuser("sync-auto-position-user"))
    view = make_view(SyncInterfacesView, request)
    view._post_server_key = "default"
    view._selected_port_ids = {10}
    view._auto_selected_port_ids = {10}
    view._auto_selected_target_ids = {10: old_position_two.pk}

    Device.objects.filter(pk=old_position_two.pk).update(vc_position=None)
    Device.objects.filter(pk=new_position_two.pk).update(vc_position=2)
    Device.objects.filter(pk=old_position_two.pk).update(vc_position=3)

    view.sync_selected_interfaces(page_device, [port], [], "ifName")

    assert Interface.objects.filter(device=new_position_two, name="Ethernet2/1").exists()
    assert not Interface.objects.filter(device=old_position_two, name="Ethernet2/1").exists()


def test_inaccessible_selected_target_is_not_locked():
    """A forged inaccessible target must not block work on that Device."""
    from dcim.models import Device
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms, make_view
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    page_device = make_device("restricted-target-page")
    inaccessible_target = make_device("restricted-target-hidden")
    user = make_user_with_perms("restricted-target-user", [])
    user = grant(user, "view", Device, constraints={"id": page_device.pk})
    sync_finished = Event()

    def sync_forged_target():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                # Below the caller's future.result(timeout=5): if this test regresses and the row
                # IS locked, the lock timeout must fire first so the failure names the real cause.
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_page = Device.objects.get(pk=page_device.pk)
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {"device_selection_10": str(inaccessible_target.pk)},
                user=thread_user,
            )
            view = make_view(SyncInterfacesView, request)
            view._post_server_key = "default"
            view._selected_port_ids = {10}
            view._auto_selected_port_ids = set()
            view._auto_selected_target_ids = {}
            view._skipped_conflicts = []
            view._synced_count = 0
            view.sync_selected_interfaces(
                thread_page,
                [{"port_id": 10, "ifName": "Ethernet1", "ifAdminStatus": "up"}],
                ["vlans", "mac_address", "description", "mtu", "speed", "type"],
                "ifName",
            )
            sync_finished.set()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            Device.objects.select_for_update().get(pk=inaccessible_target.pk)
            future = executor.submit(sync_forged_target)
            future.result(timeout=5)

    assert sync_finished.is_set()


def test_viewable_outside_selected_target_is_not_locked():
    """A target outside the page Device scope must not be locked."""
    from dcim.models import Device
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, make_view
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    page_device = make_device("outside-target-page")
    outside_target = make_device("outside-target-device")
    user = make_superuser("outside-target-user")
    sync_finished = Event()

    def sync_forged_target():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_page = Device.objects.get(pk=page_device.pk)
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request(
                "post",
                {"device_selection_10": str(outside_target.pk)},
                user=thread_user,
            )
            view = make_view(SyncInterfacesView, request)
            view._post_server_key = "default"
            view._selected_port_ids = {10}
            view._auto_selected_port_ids = set()
            view._auto_selected_target_ids = {}
            view._skipped_conflicts = []
            view._synced_count = 0
            view.sync_selected_interfaces(
                thread_page,
                [{"port_id": 10, "ifName": "Ethernet1", "ifAdminStatus": "up"}],
                ["vlans", "mac_address", "description", "mtu", "speed", "type"],
                "ifName",
            )
            sync_finished.set()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            Device.objects.select_for_update().get(pk=outside_target.pk)
            future = executor.submit(sync_forged_target)
            future.result(timeout=5)

    assert sync_finished.is_set()


def test_selected_vc_targets_lock_chassis_before_devices():
    """Bulk sync must use the same chassis-first lock order as relationship sync."""
    from django.db import connection, transaction

    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, make_view
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    _virtual_chassis, (page_device, target_device) = make_virtual_chassis_members("bulk-lock-order")
    request = make_request(
        "post",
        {"device_selection_10": str(target_device.pk)},
        user=make_superuser("bulk-lock-order-user"),
    )
    view = make_view(SyncInterfacesView, request)
    locked_selects = []

    def record_locked_select(execute, sql, params, many, context):
        if "FOR UPDATE" in sql.upper():
            locked_selects.append(sql.lower())
        return execute(sql, params, many, context)

    with transaction.atomic(), connection.execute_wrapper(record_locked_select):
        view._lock_selected_device_targets(page_device)

    chassis_lock = next(index for index, sql in enumerate(locked_selects) if "dcim_virtualchassis" in sql)
    device_lock = next(index for index, sql in enumerate(locked_selects) if '"dcim_device"' in sql)
    assert chassis_lock < device_lock


def test_vm_sync_serializes_duplicate_display_name_resolution():
    """A second VM sync must not resolve the same unbound natural-key row concurrently."""
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection
    from virtualization.models import VMInterface, VirtualMachine

    from netbox_librenms_plugin.tests.conftest import make_vm
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, make_view
    from netbox_librenms_plugin.utils import get_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    vm = make_vm("vm-duplicate-name-lock")
    VMInterface.objects.create(virtual_machine=vm, name="Ethernet")
    user = make_superuser("vm-duplicate-name-lock-user")
    first_resolved = Event()
    second_resolved = Event()
    release_first = Event()

    def sync_port(port_id, resolved_event, wait_for_release):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_vm = VirtualMachine.objects.get(pk=vm.pk)
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request("post", {}, user=thread_user)
            view = make_view(SyncInterfacesView, request)
            view._post_server_key = "default"
            view._selected_port_ids = {port_id}
            view._skipped_conflicts = []
            real_resolve = view._resolve_vm_interface

            def pause_after_resolution(*args, **kwargs):
                interface = real_resolve(*args, **kwargs)
                resolved_event.set()
                if wait_for_release:
                    assert release_first.wait(5), "test did not release the first VM sync"
                return interface

            view._resolve_vm_interface = pause_after_resolution
            port = {
                "port_id": port_id,
                "ifName": f"Ethernet{port_id}",
                "ifDescr": "Ethernet",
                "ifAdminStatus": "up",
            }
            view.sync_selected_interfaces(
                thread_vm,
                [port],
                ["vlans", "mac_address", "description", "mtu", "speed", "type"],
                "ifDescr",
            )
            return list(view._skipped_conflicts)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(sync_port, 10, first_resolved, True)
        assert first_resolved.wait(5), "first VM sync did not resolve the interface"
        second_future = executor.submit(sync_port, 11, second_resolved, False)
        resolved_during_first = second_resolved.wait(BLOCKED_WAIT_SECONDS)
        release_first.set()
        first_skips = first_future.result(timeout=10)
        second_skips = second_future.result(timeout=10)

    interface = VMInterface.objects.get(virtual_machine=vm, name="Ethernet")
    assert not resolved_during_first
    assert get_librenms_device_id(interface, "default") == 10, (
        interface.custom_field_data,
        first_skips,
        second_skips,
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
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

    _vc, (aggregate_device, member_device) = make_virtual_chassis_members("relationship-scope-lock")
    aggregate = make_interface(aggregate_device, "Port-Channel1", iface_type="lag")
    member = make_interface(member_device, "Ethernet2")
    set_librenms_device_id(aggregate, 20, "default")
    set_librenms_device_id(member, 10, "default")
    aggregate.save()
    member.save()
    user = make_superuser("relationship-scope-lock-user")
    cache_key = SyncInterfaceLagView().get_cache_key(aggregate_device, "ports", "default")
    cache.set(
        cache_key,
        {
            "ports": [
                {"port_id": 10, "ifName": member.name},
                {"port_id": 20, "ifName": aggregate.name},
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
            view._librenms_api = SimpleNamespace(server_key="default")
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

    with ThreadPoolExecutor(max_workers=2) as executor:
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


def test_inline_relationship_rechecks_migrated_donor_after_lock():
    """A donor migrated while the request waits for its lock must stay read-only."""
    from types import SimpleNamespace

    from dcim.models import Device
    from django.contrib.auth import get_user_model
    from django.core.cache import cache
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, post
    from netbox_librenms_plugin.utils import mark_librenms_migrated, set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

    donor = make_device("relationship-migrated-donor")
    winner = make_device("relationship-migrated-winner")
    child = make_interface(donor, "Ethernet1.100", iface_type="virtual")
    parent = make_interface(donor, "Ethernet1")
    set_librenms_device_id(child, 10, "default")
    set_librenms_device_id(parent, 20, "default")
    child.save()
    parent.save()
    user = make_superuser("relationship-migrated-user")
    view_template = SyncInterfaceParentView()
    cache_key = view_template.get_cache_key(donor, "ports", "default")
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
                mark_librenms_migrated(locked_donor, winner.pk, "default")
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
            view._librenms_api = SimpleNamespace(server_key="default")
            return post(view, request, object_type="device", object_id=donor.pk)
        finally:
            close_old_connections()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
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
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

    device = make_device("targeted-inline-lock")
    child = make_interface(device, "Ethernet1.100", iface_type="virtual")
    parent = make_interface(device, "Ethernet1")
    unrelated = make_interface(device, "Ethernet99")
    set_librenms_device_id(child, 10, "default")
    set_librenms_device_id(parent, 20, "default")
    child.save()
    parent.save()
    user = make_superuser("targeted-inline-lock-user")
    view_template = SyncInterfaceParentView()
    cache_key = view_template.get_cache_key(device, "ports", "default")
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
            view._librenms_api = SimpleNamespace(server_key="default")

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
        with ThreadPoolExecutor(max_workers=2) as executor:
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


def test_bulk_relationship_pass_skips_scope_locks_without_selected_edges():
    """An unrelated cached edge must not serialize a selected access port."""
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, make_view
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    device = make_device("bulk-unrelated-edge")
    selected = make_interface(device, "Ethernet1")
    child = make_interface(device, "Ethernet2.100", iface_type="virtual")
    parent = make_interface(device, "Ethernet2")
    for interface, port_id in ((selected, 10), (child, 20), (parent, 30)):
        set_librenms_device_id(interface, port_id, "default")
        interface.save()
    user = make_superuser("bulk-unrelated-edge-user")
    relationship_finished = Event()
    ports = [
        {"port_id": 10, "ifName": selected.name},
        {"port_id": 20, "ifName": child.name},
        {"port_id": 30, "ifName": parent.name},
    ]

    def apply_relationships():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                # Below the caller's future.result(timeout=5), as in sync_forged_target above.
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_device = type(device).objects.get(pk=device.pk)
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request("post", {}, user=thread_user)
            view = make_view(SyncInterfacesView, request)
            view.interface_name_field = "ifName"
            view._selected_port_ids = {10}
            view._sync_lag_and_parent_relationships(
                thread_device,
                ports,
                {"lag_members": {}, "sub_interfaces": {20: 30}},
                "default",
            )
            relationship_finished.set()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            type(device).objects.select_for_update().get(pk=device.pk)
            future = executor.submit(apply_relationships)
            future.result(timeout=5)

    assert relationship_finished.is_set()


def test_bulk_relationship_pass_does_not_lock_unrelated_interfaces():
    """A selected parent edge must lock only its source and related candidates."""
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections, connection, transaction

    from netbox_librenms_plugin.tests.conftest import make_device, make_interface
    from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_superuser, make_view
    from netbox_librenms_plugin.utils import set_librenms_device_id
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    device = make_device("bulk-targeted-edge")
    child = make_interface(device, "Ethernet1.100", iface_type="virtual")
    parent = make_interface(device, "Ethernet1")
    unrelated = make_interface(device, "Ethernet99")
    for interface, port_id in ((child, 10), (parent, 20), (unrelated, 30)):
        set_librenms_device_id(interface, port_id, "default")
        interface.save()
    user = make_superuser("bulk-targeted-edge-user")
    relationship_finished = Event()
    ports = [
        {"port_id": 10, "ifName": child.name},
        {"port_id": 20, "ifName": parent.name},
        {"port_id": 30, "ifName": unrelated.name},
    ]

    def apply_relationships():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '500ms'")
                cursor.execute("SET statement_timeout = '5s'")
            thread_device = type(device).objects.get(pk=device.pk)
            thread_user = get_user_model().objects.get(pk=user.pk)
            request = make_request("post", {}, user=thread_user)
            view = make_view(SyncInterfacesView, request)
            view.interface_name_field = "ifName"
            view._selected_port_ids = {10}
            view._auto_selected_port_ids = set()
            view._auto_selected_target_ids = {}
            view._sync_lag_and_parent_relationships(
                thread_device,
                ports,
                {"lag_members": {}, "sub_interfaces": {10: 20}},
                "default",
            )
            relationship_finished.set()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            type(unrelated).objects.select_for_update().get(pk=unrelated.pk)
            future = executor.submit(apply_relationships)
            future.result(timeout=5)

    assert relationship_finished.is_set()
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

    with ThreadPoolExecutor(max_workers=2) as executor:
        lock_future = executor.submit(lock_scope)
        assert scope_locked.wait(5), "relationship scope was not locked"
        join_future = executor.submit(join_scope)
        joined_while_scope_locked = member_joined.wait(BLOCKED_WAIT_SECONDS)
        release_scope.set()
        lock_future.result(timeout=10)
        join_future.result(timeout=10)

    assert not joined_while_scope_locked
