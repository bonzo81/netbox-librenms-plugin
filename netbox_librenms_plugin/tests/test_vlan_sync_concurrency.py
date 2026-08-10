"""Concurrency tests for global VLAN synchronization."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import close_old_connections, connection

from netbox_librenms_plugin.tests.conftest import make_device
from netbox_librenms_plugin.tests.view_test_helpers import make_request


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
