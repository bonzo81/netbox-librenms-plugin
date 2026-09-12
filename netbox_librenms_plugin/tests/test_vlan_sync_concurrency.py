"""
Concurrency tests for VLAN synchronization.

Global VIDs are serialized by an advisory lock. Confirmed grouped VLAN changes lock their exact
row before applying the disclosed rename.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, Event

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import close_old_connections, connection

from netbox_librenms_plugin.tests.conftest import make_device
from netbox_librenms_plugin.tests.view_test_helpers import (
    assert_locked_before_update,
    make_request,
    make_user_with_perms,
    messages_on,
    post,
)


class _CapturedQueries:
    """Expose synthetic SQL through the CaptureQueriesContext contract."""

    def __init__(self, *statements):
        self.captured_queries = [{"sql": statement} for statement in statements]


def test_lock_order_assertion_ignores_sibling_table_names():
    """A lock on ipam_vlangroup must not satisfy the ipam_vlan assertion."""
    captured = _CapturedQueries(
        'SELECT "ipam_vlangroup"."id" FROM "ipam_vlangroup" FOR UPDATE',
        'UPDATE "ipam_vlan" SET "name" = \'new\' WHERE "ipam_vlan"."id" = 1',
    )

    with pytest.raises(AssertionError, match="ipam_vlan was never locked"):
        assert_locked_before_update(captured, "ipam_vlan")


def test_lock_order_assertion_requires_one_exact_update_pair():
    """One early lock must not hide a later unpaired update in the same capture."""
    captured = _CapturedQueries(
        'SELECT "ipam_vlan"."id" FROM "ipam_vlan" FOR UPDATE',
        'UPDATE "ipam_vlan" SET "name" = \'one\' WHERE "ipam_vlan"."id" = 1',
        'UPDATE "ipam_vlan" SET "name" = \'two\' WHERE "ipam_vlan"."id" = 2',
    )

    with pytest.raises(AssertionError, match="exactly one lock/update pair"):
        assert_locked_before_update(captured, "ipam_vlan")


class _GlobalVLANLookupBarrier:
    """Pause both workers after their first global VLAN lookup completes."""

    def __init__(self, barrier):
        self.barrier = barrier
        self.lookup_seen = False

    def __call__(self, execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if (
            not self.lookup_seen
            and sql.lstrip().upper().startswith("SELECT")
            and 'FROM "ipam_vlan"' in sql
            and '"ipam_vlan"."group_id" IS NULL' in sql
            and '"ipam_vlan"."vid"' in sql
        ):
            self.lookup_seen = True
            try:
                self.barrier.wait(timeout=1)
            except BrokenBarrierError:
                # With serialization, the first worker times out while the second
                # waits for its transaction lock. It can then create and commit.
                pass
        return result


def _sync_global_vlan(device, user, vid, lookup_wrapper):
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '5s'")
            cursor.execute("SET statement_timeout = '5s'")

        request = make_request(
            data={"action": "create_vlans", "select": [str(vid)], "server_key": "default"},
            user=user,
            path="/sync/vlans/",
        )

        view = SyncVLANsView()
        view.request = request
        cache_key = view.get_cache_key(device, "vlans", "default")
        cache.set(cache_key, [{"vlan_vlan": vid, "vlan_name": "Concurrent global VLAN"}], timeout=60)

        with connection.execute_wrapper(lookup_wrapper):
            view.post(request, object_type="device", object_id=device.pk)
    finally:
        connection.close()


@pytest.mark.django_db(
    transaction=True,
    # Explicit available apps make Django use cascade-aware transaction cleanup.
    # This is required when another installed plugin has M2M tables outside the default flush list.
    available_apps=[app.name for app in apps.get_app_configs()],
)
def test_concurrent_global_vlan_sync_creates_one_vlan():
    """Two requests that sync the same missing global VID must create one row."""
    from ipam.models import VLAN

    user = get_user_model().objects.create_user(
        username="global-vlan-concurrency-user",
        password="test-password",
        is_superuser=True,
    )
    devices = [make_device("global-vlan-device-a"), make_device("global-vlan-device-b")]
    lookup_barrier = Barrier(2)
    lookup_wrappers = [_GlobalVLANLookupBarrier(lookup_barrier) for _device in devices]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_sync_global_vlan, device, user, 321, lookup_wrapper)
            for device, lookup_wrapper in zip(devices, lookup_wrappers, strict=True)
        ]
        for future in futures:
            future.result(timeout=10)

    assert all(wrapper.lookup_seen for wrapper in lookup_wrappers)
    assert VLAN.objects.filter(vid=321, group__isnull=True).count() == 1


def _drive_grouped_sync(device, user, group, vid, librenms_name, *, intent=None):
    """POST one grouped VID into the real view and return the recorded messages."""
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    data = {
        "action": "create_vlans",
        "select": [str(vid)],
        f"vlan_group_{vid}": str(group.pk),
        "server_key": "default",
    }
    if intent is not None:
        data.update(
            {
                "confirm_conflicts": "1",
                "force_conflict": str(vid),
                "conflict_intent": intent,
            }
        )
    request = make_request(
        data=data,
        user=user,
        path="/sync/vlans/",
    )
    view = SyncVLANsView()
    cache.set(
        view.get_cache_key(device, "vlans", "default"),
        [{"vlan_vlan": vid, "vlan_name": librenms_name}],
        timeout=60,
    )
    post(view, request, object_type="device", object_id=device.pk)
    return messages_on(request)


def _rename_intent(vlan, device, proposed_name):
    """Return a signed intent for the current test VLAN state."""
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    conflict = SyncVLANsView()._build_conflict(
        vlan=vlan,
        proposed_name=proposed_name,
        obj=device,
        object_type="device",
        server_key="default",
    )
    return conflict["intent"]


@pytest.mark.django_db
def test_grouped_vlan_row_is_locked_before_the_rename():
    """Verify SQL locks a grouped VLAN before its confirmed rename."""
    from django.db import transaction
    from django.test.utils import CaptureQueriesContext
    from ipam.models import VLAN, VLANGroup

    device = make_device("vlan-lock-sql")
    group = VLANGroup.objects.create(name="Lock-SQL", slug="lock-sql")
    vlan = VLAN.objects.create(vid=41, group=group, name="old-name", status="active")
    user = make_user_with_perms(
        "vlan-lock-sql-user",
        [("view", type(device)), ("view", VLANGroup), ("add", VLAN), ("change", VLAN)],
    )
    intent = _rename_intent(vlan, device, "librenms-name")

    with transaction.atomic(), CaptureQueriesContext(connection) as captured:
        _drive_grouped_sync(
            device,
            user,
            group,
            vid=41,
            librenms_name="librenms-name",
            intent=intent,
        )

    assert_locked_before_update(captured, "ipam_vlan")
    vlan.refresh_from_db()
    assert vlan.name == "librenms-name"


@pytest.mark.django_db
def test_grouped_vlan_name_collision_is_rejected_without_a_database_error():
    """A confirmed rename must not violate the group's unique VLAN-name constraint."""
    from ipam.models import VLAN, VLANGroup

    device = make_device("vlan-rename-collision")
    group = VLANGroup.objects.create(name="Rename-Collision", slug="rename-collision")
    vlan = VLAN.objects.create(vid=51, group=group, name="old-name", status="active")
    VLAN.objects.create(vid=52, group=group, name="claimed-name", status="active")
    user = make_user_with_perms(
        "vlan-rename-collision-user",
        [("view", type(device)), ("view", VLANGroup), ("add", VLAN), ("change", VLAN)],
    )
    intent = _rename_intent(vlan, device, "claimed-name")

    recorded_messages = _drive_grouped_sync(
        device,
        user,
        group,
        vid=51,
        librenms_name="claimed-name",
        intent=intent,
    )

    vlan.refresh_from_db()
    assert vlan.name == "old-name"
    assert any(level == "error" and "name already exists" in message for level, message in recorded_messages)


class _ScopedVLANReadGate:
    """Pause sync after the scoped VLAN read so locked and unlocked paths race from the same point."""

    def __init__(self, read_done, resume):
        self.read_done = read_done
        self.resume = resume
        self.seen = False

    def __call__(self, execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if (
            not self.seen
            and sql.lstrip().upper().startswith("SELECT")
            and 'FROM "ipam_vlan"' in sql
            and '"ipam_vlan"."id" =' in sql
        ):
            self.seen = True
            self.read_done.set()
            assert self.resume.wait(10), "test never released the VLAN sync"
        return result


@pytest.mark.django_db(
    transaction=True,
    # Explicit available apps make Django use cascade-aware transaction cleanup.
    # This is required when another installed plugin has M2M tables outside the default flush list.
    available_apps=[app.name for app in apps.get_app_configs()],
)
def test_grouped_vlan_deleted_after_the_scope_check_is_skipped_not_crashed(request):
    """Verify deleting a grouped VLAN after the scope check skips it instead of raising a failed-update error."""
    from ipam.models import VLAN, VLANGroup

    device = make_device("vlan-relock-delete")
    group = VLANGroup.objects.create(name="Relock-Delete", slug="relock-delete")
    vlan = VLAN.objects.create(vid=42, group=group, name="old-name", status="active")
    user = make_user_with_perms(
        "vlan-relock-delete-user",
        [("view", type(device)), ("view", VLANGroup), ("add", VLAN), ("change", VLAN)],
    )

    with connection.cursor() as cursor:
        cursor.execute("SHOW lock_timeout")
        original_lock_timeout = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('lock_timeout', %s, false)", ["137ms"])
        cursor.execute("SHOW lock_timeout")
        initial_lock_timeout = cursor.fetchone()[0]

    def restore_original_lock_timeout():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('lock_timeout', %s, false)", [original_lock_timeout])

    request.addfinalizer(restore_original_lock_timeout)

    read_done, resume = Event(), Event()

    def sync_as_caller():
        close_old_connections()
        try:
            # Bound both racing connections, as the sibling concurrency tests do. The test assumes
            # the gate fires on the scope-check read, before the re-lock; if that order changes the
            # main-thread delete blocks on the row lock and resume.set() can never run.
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '10s'")
            thread_user = get_user_model().objects.get(pk=user.pk)
            with connection.execute_wrapper(_ScopedVLANReadGate(read_done, resume)):
                return _drive_grouped_sync(device, thread_user, group, vid=42, librenms_name="librenms-name")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        syncing = executor.submit(sync_as_caller)
        try:
            assert read_done.wait(10), "the VLAN sync never reached its scoped read"
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
            VLAN.objects.filter(pk=vlan.pk).delete()
        finally:
            resume.set()
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('lock_timeout', %s, false)", [initial_lock_timeout])
        recorded = syncing.result(timeout=20)

    with connection.cursor() as cursor:
        cursor.execute("SHOW lock_timeout")
        final_lock_timeout = cursor.fetchone()[0]

    assert final_lock_timeout == initial_lock_timeout
    joined = " || ".join(text for _level, text in recorded)
    assert "concurrent VLAN change" in joined, joined
    assert not any(level == "success" for level, _text in recorded), joined
    assert not VLAN.objects.filter(pk=vlan.pk).exists()
