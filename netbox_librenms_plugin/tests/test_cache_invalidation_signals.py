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


def test_connect_rejects_a_missing_owner_column(monkeypatch):
    """Fail startup when NetBox no longer exposes a declared owner column."""
    from django.core.exceptions import ImproperlyConfigured

    from netbox_librenms_plugin import cache_signals

    monkeypatch.setitem(cache_signals.OWNER_COLUMNS, "dcim.interface", ("missing_owner_id",))

    with pytest.raises(ImproperlyConfigured, match=r"dcim\.interface.*missing_owner_id"):
        cache_signals.connect()


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
class TestARolledBackInnerWriteIsNotFlushed:
    """An uncommitted write must not clear anybody's cache."""

    def test_an_inner_rollback_does_not_invalidate_its_own_device(self, django_capture_on_commit_callbacks):
        """A batch belongs to the savepoint scope that opened it.

        Django removes only the callbacks registered inside a rolled-back savepoint. An owner
        recorded there has to ride that block's own hook, or an enclosing batch would carry it
        to commit and clear the caches of a device whose write never landed.
        """
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        outer = make_device("savepoint-outer", librenms_cf={SERVER_KEY: 7})
        inner = make_device("savepoint-inner", librenms_cf={SERVER_KEY: 8})
        make_module_bay(outer, "Bay 1")
        make_module_bay(inner, "Bay 1")
        keys = _seed_every_tab(inner) + _seed_every_tab(outer)

        class _Rollback(Exception):
            pass

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    # Opens the outer batch.
                    install_module(outer, "Bay 1", "Signal Model", serial="SPI-1")
                    with pytest.raises(_Rollback):
                        with transaction.atomic():
                            install_module(inner, "Bay 1", "Signal Model", serial="SPI-2")
                            raise _Rollback
            inner_state = _snapshot_state(inner)
            outer_state = _snapshot_state(outer)
        finally:
            _clear(keys)

        assert all(inner_state.values()), f"a rolled-back write cleared its device: {inner_state}"
        assert not any(outer_state.values()), f"the committed write was not cleaned up: {outer_state}"


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

    def test_moving_an_ip_between_devices_invalidates_both_devices(self, django_capture_on_commit_callbacks):
        from django.db import transaction

        from netbox_librenms_plugin.tests.conftest import ip_on, make_device, make_interface

        donor = make_device("ip-move-donor", librenms_cf={SERVER_KEY: 7})
        winner = make_device("ip-move-winner", librenms_cf={SERVER_KEY: 8})
        bystander = make_device("ip-move-bystander", librenms_cf={SERVER_KEY: 9})
        ip = ip_on(donor, "198.18.30.10/24", "Ethernet1")
        winner_interface = make_interface(winner, "Ethernet1")
        keys = _seed_every_tab(winner) + _seed_every_tab(bystander) + _seed_every_tab(donor)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                with transaction.atomic():
                    ip.assigned_object = winner_interface
                    ip.save()
            ip.refresh_from_db()
            assert ip.assigned_object_id == winner_interface.pk, "the IP never moved"
            donor_state = _snapshot_state(donor)
            winner_state = _snapshot_state(winner)
            bystander_state = _snapshot_state(bystander)
        finally:
            _clear(keys)

        assert not any(donor_state.values()), f"the device the IP left kept stale snapshots: {donor_state}"
        assert not any(winner_state.values()), f"the device the IP joined kept stale snapshots: {winner_state}"
        assert all(bystander_state.values()), f"an untouched device lost its snapshots: {bystander_state}"


@pytest.mark.django_db(transaction=True)
class TestAWriteOutsideATransactionStillInvalidates:
    """Autocommit runs the commit hook immediately, so the batch must be filled first."""

    def test_an_autocommit_write_invalidates_its_owner(self):
        """Outside an atomic block Django runs on_commit inline, not at some later commit.

        Registering the hook before recording the owner therefore flushes an empty batch and
        the write is never cleaned up. Every other test here wraps its writes in
        transaction.atomic(), which is exactly why none of them can see this.
        """
        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay

        device = make_device("autocommit-write", librenms_cf={SERVER_KEY: 7})
        make_module_bay(device, "Bay 1")
        keys = _seed_every_tab(device)

        try:
            # No atomic block: this is how a management command, a script or an rq job writes.
            install_module(device, "Bay 1", "Signal Model", serial="AC-1")
            remaining = _snapshot_state(device)
        finally:
            _clear(keys)
            device.delete()

        assert not any(remaining.values()), f"an autocommit write left stale snapshots: {remaining}"


class TestEverySchedulingViewTakesTheClaim:
    """A view that schedules a transition must also claim its page.

    Without the claim the write signals invalidate the page object for the view's own write,
    deleting the source snapshot the transition preserves and the response is about to render.
    Missing the mixin is silent, so it is checked rather than remembered.

    The claim is read from the imported class, because issubclass answers it the way Python
    does: an alias, a dotted base, a shadowed import or a repeated class name cannot hide it.
    Only the call is read from the source, because a call site is a source-level fact. The
    names that count as a call are derived from the one real scheduler rather than listed.
    """

    SCHEDULER = "schedule_request_cache_mutation"

    def _view_modules(self):
        """Return every module in the views package, imported."""
        import importlib
        import pkgutil

        from netbox_librenms_plugin import views

        return [views] + [
            importlib.import_module(info.name)
            for info in pkgutil.walk_packages(views.__path__, prefix=f"{views.__name__}.")
        ]

    def _module_source(self, module):
        """Return a module's source, read from its file.

        Not inspect.getsource: an empty file has no lines, and on Python 3.12 that is an
        OSError rather than an empty string. views/sync/__init__.py is empty.
        """
        from pathlib import Path

        return Path(module.__file__).read_text()

    def _view_classes(self):
        """Return every class the views package defines, nested ones included.

        A class is taken from the module that defines it, so a re-export is not a second entry.
        """
        import inspect

        found = set()
        for module in self._view_modules():
            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if obj.__module__ == module.__name__:
                    found.add(obj)
                    found.update(self._nested_classes(obj))
        return found

    def _nested_classes(self, cls):
        """Yield the classes *cls* nests, at any depth, but not the ones merely assigned to it."""
        import inspect

        for obj in vars(cls).values():
            if inspect.isclass(obj) and obj.__qualname__.startswith(f"{cls.__qualname__}."):
                yield obj
                yield from self._nested_classes(obj)

    def _scheduler_names(self, trees=None):
        """Return the scheduler and every name in the views package that reaches it.

        Wrapping the scheduler in a helper is an established pattern here, so the set is derived
        from the one real scheduler instead of listed: a new wrapper, an import alias or an
        assignment cannot fall outside a set it builds itself.
        """
        import ast

        if trees is None:
            from netbox_librenms_plugin import sync_cache

            # The scheduler's own module counts: it is the natural home for a shared wrapper.
            trees = [ast.parse(self._module_source(module)) for module in [*self._view_modules(), sync_cache]]
        refers = {}
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if alias.asname:
                            refers.setdefault(alias.asname, set()).add(alias.name.rsplit(".", 1)[-1])
                elif isinstance(node, ast.Assign):
                    # Covers a plain alias and a wrapped one such as functools.partial(...).
                    mentioned = {name.id for name in ast.walk(node.value) if isinstance(name, ast.Name)}
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            refers.setdefault(target.id, set()).update(mentioned)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    refers.setdefault(node.name, set()).update(self._called_names(node))

        names = {self.SCHEDULER}
        while True:
            grown = names | {name for name, refs in refers.items() if refs & names}
            if grown == names:
                return names
            names = grown

    def _called_names(self, node):
        """Return the names *node* calls in its own body, ignoring classes nested inside it."""
        import ast

        return {
            call.func.id if isinstance(call.func, ast.Name) else call.func.attr
            for call in self._own_nodes(node)
            if isinstance(call, ast.Call) and isinstance(call.func, (ast.Name, ast.Attribute))
        }

    def _schedules(self, cls, names=None):
        """Return whether *cls* calls a scheduler, read from its own source."""
        import ast
        import inspect
        import textwrap

        definition = ast.parse(textwrap.dedent(inspect.getsource(cls))).body[0]
        names = names if names is not None else self._scheduler_names()
        return bool(self._called_names(definition) & names)

    def _server_key_guarded_scheduler_calls(self, cls, names=None):
        """Return scheduler calls nested in a truthy server-key branch."""
        import ast
        import inspect
        import textwrap

        definition = ast.parse(textwrap.dedent(inspect.getsource(cls))).body[0]
        names = names if names is not None else self._scheduler_names()

        def requires_server_key(test):
            if isinstance(test, ast.Name):
                return test.id == "server_key"
            return (
                isinstance(test, ast.BoolOp)
                and isinstance(test.op, ast.And)
                and any(requires_server_key(value) for value in test.values)
            )

        def find(node, server_key_guarded=False):
            found = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    continue
                guarded = server_key_guarded
                if isinstance(node, ast.If) and child in node.body:
                    guarded |= requires_server_key(node.test)
                if isinstance(child, ast.Call) and guarded:
                    name = child.func.id if isinstance(child.func, ast.Name) else getattr(child.func, "attr", None)
                    if name in names:
                        found.append(name)
                found.extend(find(child, guarded))
            return found

        return find(definition)

    def _own_nodes(self, node):
        """Yield every node under *node* except the body of a class nested inside it."""
        import ast

        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                continue
            yield child
            yield from self._own_nodes(child)

    def _claims(self, cls):
        """Return whether *cls* takes the claim, as Python resolves it."""
        from netbox_librenms_plugin.views.mixins import CacheMixin, SyncPageClaimMixin

        return issubclass(cls, (CacheMixin, SyncPageClaimMixin))

    def _unclaimed_scheduling_views(self):
        names = self._scheduler_names()
        return sorted(
            f"{cls.__module__}.{cls.__qualname__}"
            for cls in self._view_classes()
            if self._schedules(cls, names) and not self._claims(cls)
        )

    def test_no_scheduling_view_is_missing_the_claim(self):
        unclaimed = self._unclaimed_scheduling_views()
        assert not unclaimed, "these views schedule a transition without claiming their page: " + ", ".join(unclaimed)

    def test_server_guarded_scheduling_views_drop_the_claim_without_a_server(self):
        """A guarded scheduler must not suppress the write signal when its guard is false.

        This is a drift alarm over the shapes present today, not a proof. It cannot see a view
        that returns early on a missing server key. The behaviour is proven end to end in
        test_sync_cache_consistency.py.
        """
        names = self._scheduler_names()
        guarded = {
            cls
            for cls in self._view_classes()
            if self._claims(cls) and self._server_key_guarded_scheduler_calls(cls, names)
        }
        expected = {
            "AddBayTemplateView",
            "DeleteNetBoxInterfacesView",
            "MoveModuleView",
            "UpdateModuleSerialView",
        }
        guarded_names = {cls.__qualname__ for cls in guarded}
        assert expected <= guarded_names, f"the check no longer finds {sorted(expected - guarded_names)}"

        missing = sorted(
            f"{cls.__module__}.{cls.__qualname__}"
            for cls in guarded
            if not getattr(cls, "DROP_SYNC_PAGE_CLAIM_WITHOUT_SERVER", False)
        )
        assert not missing, "these guarded scheduling views retain the claim without a server: " + ", ".join(missing)

    def test_the_scan_reaches_every_class_the_views_package_defines(self):
        """Cross-check against the source: a class the scan never reaches is never checked."""
        import ast
        from collections import Counter

        # Counted, not just present: a second class of the same name would replace the first.
        reached = Counter((cls.__module__, cls.__qualname__.rsplit(".", 1)[-1]) for cls in self._view_classes())
        for module in self._view_modules():
            defined = Counter(
                (module.__name__, node.name)
                for node in ast.walk(ast.parse(self._module_source(module)))
                if isinstance(node, ast.ClassDef)
            )
            mine = Counter({key: reached[key] for key in defined})
            assert defined == mine, (
                f"the scan never sees {sorted(name for _module, name in (defined - mine).elements())}"
            )

    def test_every_module_source_is_readable(self):
        """An empty file is valid source, and the views package has one."""
        import ast

        for module in self._view_modules():
            ast.parse(self._module_source(module))
        assert any(not self._module_source(module) for module in self._view_modules()), (
            "expected the views package to still contain an empty module"
        )

    def test_the_check_can_actually_find_a_scheduling_view(self):
        """Positive control, so an import or parse change cannot make the check vacuous.

        These six name the views themselves rather than a count, and between them they reach the
        scheduler directly, through the module wrapper and through the winner wrapper.
        """
        names = self._scheduler_names()
        scheduling = {cls.__qualname__ for cls in self._view_classes() if self._schedules(cls, names)}
        expected = {
            "SyncCablesView",
            "SyncIPAddressesView",
            "SyncInterfacesView",
            "SyncVLANsView",
            "InstallModuleView",
            "MoveInterfaceToWinnerView",
        }
        assert expected <= scheduling, f"the check no longer finds {sorted(expected - scheduling)}"

    def test_a_view_without_the_claim_is_reported(self):
        """Positive control on both predicates, against real classes rather than parsed names."""
        from django.views import View

        from netbox_librenms_plugin.sync_cache import schedule_request_cache_mutation
        from netbox_librenms_plugin.views.mixins import CacheMixin

        class Bare(View):
            def post(self, request):
                schedule_request_cache_mutation(request, None, None, None)

        class Claiming(CacheMixin, View):
            def post(self, request):
                schedule_request_cache_mutation(request, None, None, None)

        assert self._schedules(Bare) and not self._claims(Bare)
        assert self._schedules(Claiming) and self._claims(Claiming)

    def test_a_nested_class_is_checked_on_its_own(self):
        """A class nested in a view is a separate class, so its call is not the view's call."""
        from django.views import View

        from netbox_librenms_plugin.sync_cache import schedule_request_cache_mutation
        from netbox_librenms_plugin.views.mixins import CacheMixin

        class Outer(CacheMixin, View):
            class Inner(View):
                def post(self, request):
                    schedule_request_cache_mutation(request, None, None, None)

        assert not self._schedules(Outer), "the enclosing class schedules nothing itself"
        assert self._schedules(Outer.Inner) and not self._claims(Outer.Inner)

    def test_the_scheduler_names_are_derived_from_the_one_real_scheduler(self):
        """A helper that calls the scheduler schedules too, so the wrappers must be found."""
        assert self._scheduler_names() >= {
            "schedule_request_cache_mutation",
            "_schedule_module_cache_mutation",
            "_schedule_winner_cache_mutation",
        }

    def test_a_wrapper_an_import_alias_and_an_assignment_all_reach_the_scheduler(self):
        """Positive control on each shape the derivation has to close."""
        import ast

        tree = ast.parse(
            "from netbox_librenms_plugin.sync_cache import schedule_request_cache_mutation as run\n"
            "\n"
            "def helper(request):\n"
            "    run(request)\n"
            "\n"
            "shortcut = helper\n"
        )

        assert self._scheduler_names([tree]) == {"schedule_request_cache_mutation", "run", "helper", "shortcut"}

    def test_a_scheduler_wrapped_in_a_partial_still_counts(self):
        """An alias is what the value refers to, not how it is written, so a partial is one too."""
        import ast

        tree = ast.parse(
            "import functools\n"
            "\n"
            "from netbox_librenms_plugin.sync_cache import schedule_request_cache_mutation\n"
            "\n"
            "run = functools.partial(schedule_request_cache_mutation)\n"
        )

        assert "run" in self._scheduler_names([tree])
