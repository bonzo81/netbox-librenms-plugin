"""Shared helpers for observing sync-cache invalidation in ORM tests."""


def snapshot_state(obj, server_key="default"):
    """Return which applicable tabs still hold a snapshot for *obj*."""
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency

    coordinator = SyncCacheConsistency(obj)
    return {
        tab: cache.get(coordinator.snapshot_key(tab, server_key)) is not None for tab in coordinator.applicable_tabs()
    }


def drain_pending_commit_callbacks():
    """Discard cleanups queued by fixture writes before a test seeds snapshots."""
    from django.db import transaction

    transaction.get_connection().run_on_commit.clear()


def seed_every_tab(obj, server_key="default"):
    """Give *obj* a snapshot on every applicable tab so invalidation is observable."""
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency

    drain_pending_commit_callbacks()
    coordinator = SyncCacheConsistency(obj)
    keys = []
    for tab in coordinator.applicable_tabs():
        key = coordinator.snapshot_key(tab, server_key)
        cache.set(key, [{"seeded": tab.value}], timeout=300)
        keys.append(key)
    assert all(cache.get(key) is not None for key in keys), "the seed never landed"
    return keys


def clear_snapshots(keys):
    """Delete snapshot keys seeded by a test."""
    from django.core.cache import cache

    for key in keys:
        cache.delete(key)
