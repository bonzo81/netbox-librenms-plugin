"""Contract tests for the module inventory cache: an empty snapshot is data, not a miss."""

from types import SimpleNamespace

import pytest

from netbox_librenms_plugin.tests.cache_test_helpers import seed_inventory
from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view, message_texts
from netbox_librenms_plugin.tests.view_test_helpers import post as _post

CACHE_MISS_TEXT = "No cached inventory data. Please refresh modules first."


@pytest.mark.django_db
class TestEmptyInventoryIsNotACacheMiss:
    """A refreshed device whose LibreNMS inventory is empty is data, not a missing snapshot."""

    def test_install_selected_reports_no_match_rather_than_a_missing_snapshot(self):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        device = make_device("empty-inventory-install", librenms_cf={"default": 7})
        user = make_user_with_perms(
            "empty-inventory-install",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ],
        )
        request = make_request("post", {"select": ["100"], "server_key": "default"}, user=user)
        view = make_view(InstallSelectedView, request, librenms_api=SimpleNamespace(server_key="default"))
        key = seed_inventory(view, device, [], librenms_id=7)

        try:
            _post(view, request, pk=device.pk)
        finally:
            cache.delete(key)

        texts = message_texts(request)
        assert CACHE_MISS_TEXT not in texts, (
            f"a valid empty inventory was reported as a cache miss; messages were {texts}"
        )
        assert "None of the selected indices matched cached inventory." in texts, texts

    def test_absent_snapshot_still_reports_a_cache_miss(self):
        """Positive control: with no cache entry at all the miss message must still fire."""
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallSelectedView

        device = make_device("absent-inventory-install", librenms_cf={"default": 7})
        user = make_user_with_perms(
            "absent-inventory-install",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ],
        )
        request = make_request("post", {"select": ["100"], "server_key": "default"}, user=user)
        view = make_view(InstallSelectedView, request, librenms_api=SimpleNamespace(server_key="default"))

        _post(view, request, pk=device.pk)

        assert CACHE_MISS_TEXT in message_texts(request)

    def test_install_branch_reports_an_empty_branch_rather_than_a_missing_snapshot(self):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import InstallBranchView

        device = make_device("empty-inventory-branch", librenms_cf={"default": 7})
        user = make_user_with_perms(
            "empty-inventory-branch",
            [
                ("view", Device),
                ("view", ModuleBay),
                ("view", ModuleType),
                ("add", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
            ],
        )
        request = make_request("post", {"parent_index": "100", "server_key": "default"}, user=user)
        view = make_view(InstallBranchView, request, librenms_api=SimpleNamespace(server_key="default"))
        key = seed_inventory(view, device, [], librenms_id=7)

        try:
            _post(view, request, pk=device.pk)
        finally:
            cache.delete(key)

        texts = message_texts(request)
        assert CACHE_MISS_TEXT not in texts, texts
        assert "No installable items found in this branch." in texts, texts


@pytest.mark.django_db
class TestReplaceReadsAreStalenessChecked:
    """The replace/preview readers must honour the same librenms_id fingerprint as the table."""

    @staticmethod
    def _inventory(ent_index=100, model="Foreign Model", serial="FOREIGN-SERIAL"):
        return [
            {
                "entPhysicalIndex": ent_index,
                "entPhysicalClass": "module",
                "entPhysicalModelName": model,
                "entPhysicalSerialNum": serial,
                "entPhysicalContainedIn": 0,
            }
        ]

    def test_replace_rejects_inventory_cached_for_a_different_librenms_device(self):
        from dcim.models import Device, Interface, Module, ModuleBay, ModuleType
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import (
            install_module,
            make_device,
            make_module_bay,
            make_module_type,
        )
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("replace-stale-fingerprint", librenms_cf={"default": 7})
        make_module_bay(device, "Bay 1")
        module = install_module(device, "Bay 1", "Installed Model", serial="INSTALLED")
        # A resolvable type, so the staleness guard is the only thing that can stop the write.
        make_module_type("Foreign Model", manufacturer=device.device_type.manufacturer)
        user = make_user_with_perms(
            "replace-stale-fingerprint",
            [
                ("view", Device),
                ("view", ModuleType),
                ("add", Module),
                ("change", Module),
                ("delete", Module),
                ("add", Interface),
                ("change", Interface),
                ("delete", Interface),
                ("view", ModuleBay),
            ],
        )
        request = make_request(
            "post",
            {"module_id": str(module.pk), "ent_index": "100", "server_key": "default"},
            user=user,
        )
        view = make_view(ReplaceModuleView, request, librenms_api=SimpleNamespace(server_key="default"))
        # The device is linked to LibreNMS id 7; the snapshot was built for id 999.
        key = seed_inventory(view, device, self._inventory(), librenms_id=999)

        try:
            _post(view, request, pk=device.pk)
        finally:
            cache.delete(key)

        assert Module.objects.filter(pk=module.pk, serial="INSTALLED").exists(), (
            "inventory cached for another LibreNMS device was applied to this device"
        )
        assert CACHE_MISS_TEXT in message_texts(request)


@pytest.mark.django_db
class TestTrustedPayloadMatchesTheDeviceMapping:
    """The payload fingerprint must equal the mapping the helper actually wrote."""

    @pytest.mark.parametrize(
        ("case", "librenms_cf", "librenms_id"),
        [
            ("legacy-bare-integer", 42, 7),
            ("non-positive-id", {"default": 3}, 0),
        ],
    )
    def test_a_declined_mapping_write_fails_the_helper(self, case, librenms_cf, librenms_id):
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import trusted_module_inventory_payload

        device = make_device(f"trusted-payload-{case}", librenms_cf=librenms_cf)

        with pytest.raises(AssertionError, match="declined the write"):
            trusted_module_inventory_payload(device, [], librenms_id=librenms_id)

    def test_a_written_mapping_still_returns_the_payload(self):
        """Positive control: the guard must not reject the ordinary path."""
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import trusted_module_inventory_payload

        device = make_device("trusted-payload-ok", librenms_cf={"default": 3})

        payload = trusted_module_inventory_payload(device, [{"index": 1}], librenms_id=9)

        assert payload == {"inventory": [{"index": 1}], "librenms_id": 9, "oob_librenms_id": None}
