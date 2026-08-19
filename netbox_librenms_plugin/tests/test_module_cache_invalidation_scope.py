"""A module action must drop the snapshots of every device it changes, and only those."""

from types import SimpleNamespace

import pytest

from netbox_librenms_plugin.sync_cache import schedule_collateral_cache_invalidation
from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view
from netbox_librenms_plugin.tests.view_test_helpers import post as _post


def _seed_inventory(view, device, inventory, *, librenms_id=None, server_key="default"):
    """Write one inventory payload the way BaseModuleTableView.post writes it."""
    from django.core.cache import cache

    key = view.get_cache_key(device, "inventory", server_key=server_key)
    payload = {"inventory": inventory, "librenms_id": librenms_id, "oob_librenms_id": None}
    cache.set(key, payload, timeout=300)
    return key


@pytest.mark.django_db
class TestModuleActionsInvalidateEveryChangedDevice:
    """A module action that changes another device must drop that device's snapshots too."""

    @staticmethod
    def _snapshot_state(device, server_key="default"):
        """Return the per-tab snapshot presence for *device* on *server_key*."""
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import TAB_SPECS, SyncCacheConsistency

        coordinator = SyncCacheConsistency(device)
        return {
            tab: cache.get(coordinator.snapshot_key(tab, server_key)) is not None
            for tab in coordinator.applicable_tabs()
            if tab in TAB_SPECS
        }

    @classmethod
    def _seed_every_tab(cls, device, server_key="default"):
        """Give *device* a snapshot on every applicable tab so invalidation is observable."""
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency

        coordinator = SyncCacheConsistency(device)
        keys = []
        for tab in coordinator.applicable_tabs():
            key = coordinator.snapshot_key(tab, server_key)
            cache.set(key, [{"seeded": tab.value}], timeout=300)
            keys.append(key)
        return keys

    def test_replace_invalidates_the_device_that_lost_the_serial_conflicting_module(
        self, django_capture_on_commit_callbacks
    ):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        page_device = make_device("replace-page-device", librenms_cf={"default": 7})
        other_device = make_device("replace-conflict-device", librenms_cf={"default": 8})
        make_module_bay(page_device, "Bay 1")
        make_module_bay(other_device, "Bay 1")
        installed = install_module(page_device, "Bay 1", "Installed Model", serial="OLD")
        conflict = install_module(other_device, "Bay 1", "Conflicting Model", serial="SHARED-SERIAL")
        module_type = conflict.module_type

        user = make_user_with_perms(
            "replace-conflict-invalidate",
            [
                ("view", Device),
                ("view", ModuleType),
                ("view", ModuleBay),
                ("add", Module),
                ("change", Module),
                ("delete", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ],
        )
        request = make_request(
            "post",
            {"module_id": str(installed.pk), "ent_index": "100", "server_key": "default"},
            user=user,
        )
        view = make_view(ReplaceModuleView, request, librenms_api=SimpleNamespace(server_key="default"))
        inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": module_type.model,
                "entPhysicalSerialNum": "SHARED-SERIAL",
                "entPhysicalContainedIn": 0,
            }
        ]
        key = _seed_inventory(view, page_device, inventory, librenms_id=7)
        seeded = self._seed_every_tab(other_device)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                _post(view, request, pk=page_device.pk)
            assert not Module.objects.filter(pk=conflict.pk).exists(), (
                "the serial-conflicting module on the other device was not removed, "
                "so this test never reached the invalidation it asserts"
            )
            remaining = self._snapshot_state(other_device)
        finally:
            cache.delete(key)
            for seeded_key in seeded:
                cache.delete(seeded_key)

        assert not any(remaining.values()), f"{other_device.name} lost a module but kept cached snapshots: {remaining}"

    def test_move_invalidates_the_device_the_module_left(self, django_capture_on_commit_callbacks):
        from dcim.models import Device, Module, ModuleBay
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import MoveModuleView

        page_device = make_device("move-page-device", librenms_cf={"default": 7})
        source_device = make_device("move-source-device", librenms_cf={"default": 8})
        make_module_bay(source_device, "Bay 1")
        moving = install_module(source_device, "Bay 1", "Moving Model", serial="MOVED")
        target_bay = make_module_bay(page_device, "Target Bay")

        user = make_user_with_perms(
            "move-source-invalidate",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("change", Module),
                ("delete", Module),
            ],
        )
        request = make_request(
            "post",
            {
                "conflict_module_id": str(moving.pk),
                "target_bay_id": str(target_bay.pk),
                "server_key": "default",
            },
            user=user,
        )
        view = make_view(MoveModuleView, request, librenms_api=SimpleNamespace(server_key="default"))
        seeded = self._seed_every_tab(source_device)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                _post(view, request, pk=page_device.pk)
            moving.refresh_from_db()
            assert moving.device_id == page_device.pk, (
                "the module never moved, so this test never reached the invalidation it asserts"
            )
            remaining = self._snapshot_state(source_device)
        finally:
            for seeded_key in seeded:
                cache.delete(seeded_key)

        assert not any(remaining.values()), f"{source_device.name} lost a module but kept cached snapshots: {remaining}"

    def test_an_untouched_device_keeps_its_snapshots(self, django_capture_on_commit_callbacks):
        """Positive control: invalidation is scoped to devices the action actually changed."""
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_module_bay
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        page_device = make_device("replace-bystander-page", librenms_cf={"default": 7})
        other_device = make_device("replace-conflict-owner", librenms_cf={"default": 8})
        bystander = make_device("replace-bystander", librenms_cf={"default": 9})
        make_module_bay(page_device, "Bay 1")
        make_module_bay(other_device, "Bay 1")
        installed = install_module(page_device, "Bay 1", "Installed Model", serial="OLD")
        conflict = install_module(other_device, "Bay 1", "Conflicting Model", serial="SHARED-SERIAL")

        user = make_user_with_perms(
            "replace-bystander",
            [
                ("view", Device),
                ("view", ModuleType),
                ("view", ModuleBay),
                ("add", Module),
                ("change", Module),
                ("delete", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ],
        )
        request = make_request(
            "post",
            {"module_id": str(installed.pk), "ent_index": "100", "server_key": "default"},
            user=user,
        )
        view = make_view(ReplaceModuleView, request, librenms_api=SimpleNamespace(server_key="default"))
        inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalClass": "module",
                "entPhysicalModelName": conflict.module_type.model,
                "entPhysicalSerialNum": "SHARED-SERIAL",
                "entPhysicalContainedIn": 0,
            }
        ]
        key = _seed_inventory(view, page_device, inventory, librenms_id=7)
        seeded = self._seed_every_tab(bystander)

        try:
            with django_capture_on_commit_callbacks(execute=True):
                _post(view, request, pk=page_device.pk)
            remaining = self._snapshot_state(bystander)
        finally:
            cache.delete(key)
            for seeded_key in seeded:
                cache.delete(seeded_key)

        assert all(remaining.values()), f"an untouched device lost its snapshots: {remaining}"

    def test_a_vc_sibling_keeps_the_shared_snapshot_the_page_transition_preserved(
        self, django_capture_on_commit_callbacks
    ):
        """The acting page's preserved source snapshot survives a sibling's invalidation.

        ``target_device`` is always the page device or a VC member of it, and the modules tab is
        VC-shared. Invalidating the sibling without honouring the preserved key would delete the
        very snapshot ``schedule_mutation`` just kept for the page being rendered.
        """
        from django.core.cache import cache

        from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.utils import set_librenms_device_id

        _vc, (page_device, sibling) = make_virtual_chassis_members("collateral-vc")
        for member in (page_device, sibling):
            set_librenms_device_id(member, 11, "default")
            member.save()

        page_coordinator = SyncCacheConsistency(page_device)
        preserved_key = page_coordinator.snapshot_key(SyncTab.MODULES, "default")
        sibling_only_key = SyncCacheConsistency(sibling).snapshot_key(SyncTab.IP_ADDRESSES, "default")
        cache.set(preserved_key, [{"seeded": "modules"}], timeout=300)
        cache.set(sibling_only_key, [{"seeded": "ipaddresses"}], timeout=300)

        request = make_request("post", {}, user=None)
        try:
            with django_capture_on_commit_callbacks(execute=True):
                schedule_collateral_cache_invalidation(request, page_device, [sibling], SyncTab.MODULES, "default")
            survived = cache.get(preserved_key) is not None
            sibling_cleared = cache.get(sibling_only_key) is None
        finally:
            cache.delete(preserved_key)
            cache.delete(sibling_only_key)

        assert survived, "collateral invalidation deleted the snapshot the page transition preserved"
        assert sibling_cleared, "the sibling's own non-shared snapshot was left stale"
