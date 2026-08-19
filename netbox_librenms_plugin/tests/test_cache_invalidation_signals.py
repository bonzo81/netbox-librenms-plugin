"""A NetBox write invalidates the snapshots of whatever object it changed.

Views used to decide which devices to invalidate, and kept getting it wrong: an action on
one device could delete a module from another and leave that device's tabs claiming their
cached data was current. The decision now follows the ORM writes themselves, so a caller
cannot forget an object it changed.

The page the user acted on is excluded: only the request knows which tab is the source and
only the response can report the cleanup, so that half stays with the view.
"""

import pytest


SERVER_KEY = "default"


def _snapshot_state(obj, server_key=SERVER_KEY):
    """Return which tabs still hold a snapshot for *obj*."""
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency

    coordinator = SyncCacheConsistency(obj)
    return {
        tab: cache.get(coordinator.snapshot_key(tab, server_key)) is not None for tab in coordinator.applicable_tabs()
    }


def _seed_every_tab(obj, server_key=SERVER_KEY):
    """Give *obj* a snapshot on every applicable tab so invalidation is observable."""
    from django.core.cache import cache

    from netbox_librenms_plugin.sync_cache import SyncCacheConsistency

    _drain_pending_commit_callbacks()
    coordinator = SyncCacheConsistency(obj)
    keys = []
    for tab in coordinator.applicable_tabs():
        key = coordinator.snapshot_key(tab, server_key)
        cache.set(key, [{"seeded": tab.value}], timeout=300)
        keys.append(key)
    assert all(cache.get(key) is not None for key in keys), "the seed never landed"
    return keys


def _drain_pending_commit_callbacks():
    """Discard cleanups queued by the fixture writes, so a test isolates its own change.

    Building a device and its bays is itself a tracked write, so a cleanup for that device is
    already queued before the test seeds anything. Dropping it here is what makes the capture
    below observe only the write under test.
    """
    from django.db import transaction

    transaction.get_connection().run_on_commit.clear()


def _clear(keys):
    from django.core.cache import cache

    for key in keys:
        cache.delete(key)


@pytest.mark.django_db
class TestOrmWritesInvalidateTheirOwner:
    """Every tracked write reaches the owning object's snapshots, whoever made it."""

    def test_creating_a_module_invalidates_its_device(self, django_capture_on_commit_callbacks):
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        device = make_device("signal-module-create", librenms_cf={SERVER_KEY: 7})
        make_module_bay(device, "Bay 1")
        keys = _seed_every_tab(device)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    install_module(device, "Bay 1", "Signal Model", serial="SIG-1")
            remaining = _snapshot_state(device)
        finally:
            _clear(keys)

        assert not any(remaining.values()), f"a new module left stale snapshots: {remaining}"

    def test_deleting_a_module_invalidates_its_device(self, django_capture_on_commit_callbacks):
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        device = make_device("signal-module-delete", librenms_cf={SERVER_KEY: 7})
        make_module_bay(device, "Bay 1")
        module = install_module(device, "Bay 1", "Signal Model", serial="SIG-2")
        keys = _seed_every_tab(device)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    module.delete()
            remaining = _snapshot_state(device)
        finally:
            _clear(keys)

        assert not any(remaining.values()), f"a deleted module left stale snapshots: {remaining}"

    def test_a_queryset_delete_still_invalidates(self, django_capture_on_commit_callbacks):
        """A queryset delete reports each row, not just an instance delete."""
        from dcim.models import Module
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        device = make_device("signal-module-qs-delete", librenms_cf={SERVER_KEY: 7})
        make_module_bay(device, "Bay 1")
        module = install_module(device, "Bay 1", "Signal Model", serial="SIG-3")
        keys = _seed_every_tab(device)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    Module.objects.filter(pk=module.pk).delete()
            remaining = _snapshot_state(device)
        finally:
            _clear(keys)

        assert not any(remaining.values()), f"a queryset delete left stale snapshots: {remaining}"

    def test_creating_an_interface_invalidates_its_device(self, django_capture_on_commit_callbacks):
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("signal-interface-create", librenms_cf={SERVER_KEY: 7})
        keys = _seed_every_tab(device)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    make_interface(device, "Ethernet1")
            remaining = _snapshot_state(device)
        finally:
            _clear(keys)

        assert not any(remaining.values()), f"a new interface left stale snapshots: {remaining}"

    def test_a_tagged_vlan_change_invalidates_its_device(self, django_capture_on_commit_callbacks):
        """post_save never fires for an m2m write, so the m2m signal has to carry it."""
        from django.db import transaction
        from ipam.models import VLAN

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("signal-vlan-m2m", librenms_cf={SERVER_KEY: 7})
        interface = make_interface(device, "Ethernet1")
        vlan = VLAN.objects.create(vid=101, name="signal-vlan")
        keys = _seed_every_tab(device)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    interface.tagged_vlans.set([vlan])
            remaining = _snapshot_state(device)
        finally:
            _clear(keys)

        assert not any(remaining.values()), f"a tagged-VLAN change left stale snapshots: {remaining}"

    def test_an_unrelated_device_keeps_its_snapshots(self, django_capture_on_commit_callbacks):
        """Positive control: invalidation follows the write, it is not a blanket flush."""
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        changed = make_device("signal-bystander-changed", librenms_cf={SERVER_KEY: 7})
        bystander = make_device("signal-bystander", librenms_cf={SERVER_KEY: 8})
        make_module_bay(changed, "Bay 1")
        keys = _seed_every_tab(bystander)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    install_module(changed, "Bay 1", "Signal Model", serial="SIG-4")
            remaining = _snapshot_state(bystander)
        finally:
            _clear(keys)

        assert all(remaining.values()), f"an untouched device lost its snapshots: {remaining}"

    def test_a_rolled_back_write_invalidates_nothing(self, django_capture_on_commit_callbacks):
        """Nothing changed in NetBox, so nothing may be dropped."""
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        device = make_device("signal-rollback", librenms_cf={SERVER_KEY: 7})
        make_module_bay(device, "Bay 1")
        keys = _seed_every_tab(device)

        class _Rollback(Exception):
            pass

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with pytest.raises(_Rollback):
                    with transaction.atomic():
                        install_module(device, "Bay 1", "Signal Model", serial="SIG-5")
                        raise _Rollback
            remaining = _snapshot_state(device)
        finally:
            _clear(keys)

        assert all(remaining.values()), f"a rolled-back write dropped snapshots: {remaining}"

    def test_an_unmapped_device_is_skipped(self, django_capture_on_commit_callbacks):
        """A device with no LibreNMS mapping owns no snapshots, so it costs no work."""
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        device = make_device("signal-unmapped")
        make_module_bay(device, "Bay 1")
        _drain_pending_commit_callbacks()

        with django_capture_on_commit_callbacks(execute=True):
            with transaction.atomic():
                install_module(device, "Bay 1", "Signal Model", serial="SIG-6")

        assert not any(_snapshot_state(device).values())


@pytest.mark.django_db
class TestTheActingPageKeepsItsOwnTransition:
    """The page the user acted on is excluded, so its source tab survives."""

    def test_a_claimed_page_is_left_to_its_own_transition(self, django_capture_on_commit_callbacks):
        from django.core.cache import cache
        from django.db import transaction

        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, claim_sync_page, sync_page_key
        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        device = make_device("signal-claimed-page", librenms_cf={SERVER_KEY: 7})
        make_module_bay(device, "Bay 1")
        keys = _seed_every_tab(device)
        modules_key = SyncCacheConsistency(device).snapshot_key(SyncTab.MODULES, SERVER_KEY)

        try:
            with claim_sync_page(sync_page_key(device)):
                with django_capture_on_commit_callbacks(execute=True):
                    with transaction.atomic():
                        install_module(device, "Bay 1", "Signal Model", serial="SIG-7")
            survived = cache.get(modules_key) is not None
        finally:
            _clear(keys)

        assert survived, "the acting page's snapshot was dropped by the signal flush"

    def test_an_unclaimed_page_is_invalidated(self, django_capture_on_commit_callbacks):
        """Positive control: the claim is what protects it, not the seeding."""
        from django.core.cache import cache
        from django.db import transaction

        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab
        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        device = make_device("signal-unclaimed-page", librenms_cf={SERVER_KEY: 7})
        make_module_bay(device, "Bay 1")
        keys = _seed_every_tab(device)
        modules_key = SyncCacheConsistency(device).snapshot_key(SyncTab.MODULES, SERVER_KEY)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    install_module(device, "Bay 1", "Signal Model", serial="SIG-8")
            survived = cache.get(modules_key) is not None
        finally:
            _clear(keys)

        assert not survived

    def test_the_claim_is_released_even_when_the_body_raises(self):
        from netbox_librenms_plugin.sync_cache import active_sync_page_keys, claim_sync_page, sync_page_key
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("signal-claim-release")

        class _Boom(Exception):
            pass

        with pytest.raises(_Boom):
            with claim_sync_page(sync_page_key(device)):
                assert active_sync_page_keys()
                raise _Boom

        assert not active_sync_page_keys(), "a raising view would leave the next request unprotected"


@pytest.mark.django_db
class TestOneFlushPerObject:
    """A bulk write must not schedule one cleanup per row."""

    def test_many_writes_to_one_device_flush_once(self, django_capture_on_commit_callbacks):
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("signal-dedup", librenms_cf={SERVER_KEY: 7})
        _drain_pending_commit_callbacks()

        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with transaction.atomic():
                for index in range(25):
                    make_interface(device, f"Ethernet{index}")

        assert len(callbacks) == 1, f"expected one flush for 25 writes, got {len(callbacks)}"


@pytest.mark.django_db
class TestABatchSurvivesASavepointRollback:
    """A rolled-back inner block must not suppress a later, real write."""

    def test_a_write_after_a_rolled_back_savepoint_still_invalidates(self, django_capture_on_commit_callbacks):
        """The end of a batch cannot be inferred from the commit queue being empty.

        A savepoint rollback removes only the callbacks registered inside it, so an unrelated
        callback queued further out keeps the queue non-empty. Treating that as "the batch is
        still live" would reuse a batch whose own hook has been discarded, and the device
        written to after the rollback would never be cleaned up.
        """
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        device = make_device("savepoint-reuse", librenms_cf={SERVER_KEY: 7})
        make_module_bay(device, "Bay 1")
        make_module_bay(device, "Bay 2")
        keys = _seed_every_tab(device)

        class _Rollback(Exception):
            pass

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    # Something else queues a callback at the outer level, so the rollback
                    # below cannot leave the queue empty.
                    transaction.on_commit(lambda: None)
                    with pytest.raises(_Rollback):
                        with transaction.atomic():
                            # First tracked write of the batch, so its hook carries this
                            # savepoint and is discarded with it.
                            install_module(device, "Bay 1", "Signal Model", serial="SP-1")
                            raise _Rollback
                    install_module(device, "Bay 2", "Signal Model", serial="SP-2")
            remaining = _snapshot_state(device)
        finally:
            _clear(keys)

        assert not any(remaining.values()), f"the write after the rollback was dropped: {remaining}"


@pytest.mark.django_db
class TestSharedSnapshotsFollowTheWholeChassis:
    """A claim covers the shared snapshot whichever member the write came through."""

    def test_a_claim_by_a_non_sync_member_still_protects_the_shared_snapshot(self, django_capture_on_commit_callbacks):
        """The page holding the claim need not be the member the snapshot is keyed on."""
        from django.core.cache import cache
        from django.db import transaction

        from netbox_librenms_plugin.sync_cache import (
            SyncCacheConsistency,
            SyncTab,
            claim_sync_page,
            sync_page_key,
        )
        from netbox_librenms_plugin.tests.conftest import install_module, make_module_bay, make_virtual_chassis_members
        from netbox_librenms_plugin.utils import get_librenms_sync_device, set_librenms_device_id

        _vc, (member_one, member_two) = make_virtual_chassis_members("shared-claim-vc")
        for member in (member_one, member_two):
            set_librenms_device_id(member, 11, SERVER_KEY)
            member.save()
        make_module_bay(member_one, "Bay 1")

        sync_device = get_librenms_sync_device(member_one, server_key=SERVER_KEY) or member_one
        # The page is the member that does NOT own the shared snapshot, and the write lands on
        # the member that does; the old identity comparison missed exactly this direction.
        page_device = member_two if sync_device.pk == member_one.pk else member_one
        writing_device = member_one if page_device.pk == member_two.pk else member_two
        make_module_bay(writing_device, "Bay 9")

        shared_key = SyncCacheConsistency(page_device).snapshot_key(SyncTab.MODULES, SERVER_KEY)
        cache.set(shared_key, [{"seeded": "modules"}], timeout=300)
        _drain_pending_commit_callbacks()

        try:
            with claim_sync_page(sync_page_key(page_device)):
                with django_capture_on_commit_callbacks(execute=True):
                    with transaction.atomic():
                        install_module(writing_device, "Bay 9", "Signal Model", serial="SHARED-1")
            survived = cache.get(shared_key) is not None
        finally:
            cache.delete(shared_key)

        assert survived, "a write through a sibling deleted the snapshot the claimed page renders"


@pytest.mark.django_db
class TestAReassignedRowInvalidatesBothSides:
    """Moving a row between owners leaves neither side serving a stale snapshot."""

    def test_moving_an_ip_between_devices_invalidates_the_donor(self, django_capture_on_commit_callbacks):
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import ip_on, make_device, make_interface

        donor = make_device("ip-move-donor", librenms_cf={SERVER_KEY: 7})
        winner = make_device("ip-move-winner", librenms_cf={SERVER_KEY: 8})
        ip = ip_on(donor, "198.18.30.10/24", "Ethernet1")
        winner_interface = make_interface(winner, "Ethernet1")
        keys = _seed_every_tab(donor)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    ip.assigned_object = winner_interface
                    ip.save()
            remaining = _snapshot_state(donor)
        finally:
            _clear(keys)

        assert not any(remaining.values()), f"the device the IP left kept stale snapshots: {remaining}"
