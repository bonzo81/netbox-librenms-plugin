"""Real database and cache tests for module preview, replacement, and movement."""

from types import SimpleNamespace

from unittest.mock import patch

import pytest
from django.core.cache import cache

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_module_bay,
    make_module_type,
)
from netbox_librenms_plugin.tests.view_test_helpers import (
    get as view_get,
    make_request,
    make_user_with_perms,
    make_view,
    message_texts,
    module_row_binding,
    post as view_post,
    trusted_module_inventory_payload,
)


pytestmark = pytest.mark.django_db


def _module(device, bay, module_type, serial):
    from dcim.models import Module

    return Module.objects.create(
        device=device,
        module_bay=bay,
        module_type=module_type,
        serial=serial,
        status="active",
    )


def _view(view_class, request):
    return make_view(
        view_class,
        request,
        librenms_api=SimpleNamespace(server_key="default"),
    )


def _cache_inventory(view, device, inventory):
    cache_key = view.get_cache_key(device, "inventory", server_key="default")
    cache.set(
        cache_key,
        trusted_module_inventory_payload(device, inventory),
        timeout=300,
    )
    return cache_key


class TestModuleMismatchPreviewView:
    def _setup(self, tag, *, installed_model="INSTALLED", librenms_model=None, installed_serial="OLD", serial="NEW"):
        from netbox_librenms_plugin.views.sync.modules import ModuleMismatchPreviewView

        device = make_device(f"preview-{tag}")
        module_type = make_module_type(f"{installed_model}-{tag}")
        bay = make_module_bay(device, f"Preview Bay {tag}")
        installed = _module(device, bay, module_type, installed_serial)
        request = make_request(
            "get",
            {"module_id": str(installed.pk), "ent_index": "100", "server_key": "default"},
        )
        view = _view(ModuleMismatchPreviewView, request)
        inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalModelName": librenms_model or module_type.model,
                "entPhysicalSerialNum": serial,
            }
        ]
        return device, installed, request, view, inventory

    def test_the_preview_shows_the_serial_without_the_vendor_marker(self):
        """
        Juniper reports "S/N NS123"; the install path stores "NS123" through the serial rules.

        Rendering the raw value beside the stored one reads as a mismatch against a module that
        already agrees with its own inventory row.
        """
        device, _installed, request, view, inventory = self._setup(
            "marker", installed_serial="NS123", serial="S/N NS123"
        )
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_get(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        html = response.content.decode()
        assert response.status_code == 200
        assert "S/N NS123" not in html
        assert "NS123" in html
        # The reported symptom: the row read as a serial mismatch against itself.
        assert "Only the serial differs" not in html

    def test_the_preview_refuses_an_oob_sourced_row(self):
        """OOB-controller inventory is read-only, so the dialog that leads to Replace must refuse it."""
        device, _installed, request, view, inventory = self._setup("oob")
        inventory[0]["_source"] = "oob"
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_get(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 400
        assert b"read-only" in response.content

    @pytest.mark.parametrize(
        "query",
        [
            {},
            {"module_id": "1", "ent_index": "not-an-integer"},
        ],
    )
    def test_invalid_parameters_return_400(self, query):
        from netbox_librenms_plugin.views.sync.modules import ModuleMismatchPreviewView

        device = make_device(f"preview-invalid-{len(query)}")
        request = make_request("get", query)

        response = view_get(_view(ModuleMismatchPreviewView, request), request, pk=device.pk)

        assert response.status_code == 400

    def test_missing_or_unmatched_cache_entry_returns_400(self):
        device, _installed, request, view, inventory = self._setup("cache")

        assert view_get(view, request, pk=device.pk).status_code == 400
        cache_key = _cache_inventory(view, device, inventory)
        request.GET = request.GET.copy()
        request.GET["ent_index"] = "999"
        try:
            assert view_get(view, request, pk=device.pk).status_code == 400
        finally:
            cache.delete(cache_key)

    def test_exact_real_module_type_renders_the_match_badge(self):
        device, _installed, request, view, inventory = self._setup("matched")
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_get(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        html = response.content.decode()
        assert response.status_code == 200
        assert "mdi-check-decagram" in html
        assert "NEW" in html

    def test_real_mapping_can_match_a_different_librenms_model_string(self):
        from netbox_librenms_plugin.models import ModuleTypeMapping

        device, installed, request, view, inventory = self._setup(
            "mapped",
            installed_model="MAPPED-TYPE",
            librenms_model="LIBRENMS-PART-NUMBER",
            installed_serial="",
            serial="MAPPED-SERIAL",
        )
        ModuleTypeMapping.objects.create(
            librenms_model="LIBRENMS-PART-NUMBER",
            netbox_module_type=installed.module_type,
            manufacturer=device.device_type.manufacturer,
        )
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_get(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        html = response.content.decode()
        assert "mdi-check-decagram" in html
        assert "Recognised as NetBox module type MAPPED-TYPE-mapped" in html
        assert "LIBRENMS-PART-NUMBER" in html

    def test_different_mapped_type_renders_the_mismatch_path(self):
        from netbox_librenms_plugin.models import ModuleTypeMapping

        device, installed, request, view, inventory = self._setup(
            "different",
            installed_model="INSTALLED-TYPE",
            librenms_model="OTHER-PART",
        )
        other_type = make_module_type("OTHER-TYPE-different")
        ModuleTypeMapping.objects.create(
            librenms_model="OTHER-PART",
            netbox_module_type=other_type,
            manufacturer=device.device_type.manufacturer,
        )
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_get(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        html = response.content.decode()
        assert installed.module_type != other_type
        assert "mdi-check-decagram" not in html
        assert "Different module type" in html

    def test_serial_conflict_is_derived_from_the_real_module_table(self):
        device, _installed, request, view, inventory = self._setup("conflict", serial="CONFLICT-SERIAL")
        other_device = make_device("preview-conflict-owner")
        other_type = make_module_type("PREVIEW-CONFLICT-TYPE")
        other_bay = make_module_bay(other_device, "Conflict Bay")
        conflict = _module(other_device, other_bay, other_type, "CONFLICT-SERIAL")
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_get(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        html = response.content.decode()
        assert str(conflict.device) in html
        assert "CONFLICT-SERIAL" in html


class TestReplaceModuleView:
    def _setup(self, tag, *, new_serial="NEW-SERIAL"):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device(f"replace-{tag}")
        old_type = make_module_type(f"REPLACE-OLD-{tag}")
        new_type = make_module_type(f"REPLACE-NEW-{tag}")
        bay = make_module_bay(device, f"Replace Bay {tag}")
        installed = _module(device, bay, old_type, f"OLD-SERIAL-{tag}")
        inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalModelName": new_type.model,
                "entPhysicalSerialNum": new_serial,
            }
        ]
        request = make_request(
            "post",
            {
                "module_id": str(installed.pk),
                "ent_index": "100",
                "server_key": "default",
                "inventory_binding": module_row_binding(
                    device,
                    "replace_module",
                    inventory[0],
                    action_target={"module_id": installed.pk},
                ),
            },
        )
        view = _view(ReplaceModuleView, request)
        return Module, device, old_type, new_type, bay, installed, request, view, inventory

    def test_the_replacement_stores_the_serial_without_the_vendor_marker(self):
        """The stored serial must match what the install path writes for the same inventory row."""
        Module, device, _old_type, _new_type, bay, _installed, request, view, inventory = self._setup(
            "marker", new_serial="S/N NS123"
        )
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert Module.objects.get(device=device, module_bay=bay).serial == "NS123"

    def test_a_rule_that_leaves_padding_still_stores_a_clean_serial(self):
        """A serial rule is operator-written, so it can drop a prefix and leave the space behind."""
        from netbox_librenms_plugin.models import NormalizationRule

        NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
        NormalizationRule.objects.create(
            scope=NormalizationRule.SCOPE_SERIAL,
            match_pattern=r"^S/N(.+)$",
            replacement=r"\1",
            priority=100,
        )
        Module, device, _old_type, _new_type, bay, _installed, request, view, inventory = self._setup(
            "padded", new_serial="S/N NS123"
        )
        cache_key = _cache_inventory(view, device, inventory)
        try:
            view_post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert Module.objects.get(device=device, module_bay=bay).serial == "NS123"

    def test_an_oob_sourced_row_cannot_replace_a_host_module(self):
        """A crafted POST naming an OOB row's index deleted the real module and installed the OOB row."""
        Module, device, old_type, _new_type, bay, installed, request, view, inventory = self._setup("oob")
        inventory[0]["_source"] = "oob"
        cache_key = _cache_inventory(view, device, inventory)
        try:
            view_post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        installed.refresh_from_db()
        assert installed.module_type == old_type
        assert Module.objects.filter(device=device, module_bay=bay).count() == 1
        assert any("read-only" in text for text in message_texts(request))

    def test_invalid_parameters_and_missing_cache_redirect_with_errors(self):
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("replace-invalid")
        invalid_request = make_request("post", {})
        invalid_response = view_post(_view(ReplaceModuleView, invalid_request), invalid_request, pk=device.pk)
        assert invalid_response.status_code == 302
        assert any("Missing or invalid" in text for text in message_texts(invalid_request))

        _Module, device, _old, _new, _bay, _installed, request, view, _inventory = self._setup("no-cache")
        response = view_post(view, request, pk=device.pk)
        assert response.status_code == 302
        assert any("cache" in text.lower() for text in message_texts(request))

    def test_replacement_deletes_the_old_row_and_creates_the_real_new_module(self):
        Module, device, old_type, new_type, bay, installed, request, view, inventory = self._setup("success")
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert not Module.objects.filter(pk=installed.pk).exists()
        replacement = Module.objects.get(device=device, module_bay=bay)
        assert replacement.module_type == new_type
        assert replacement.serial == "NEW-SERIAL"
        assert any(f"Replaced {old_type.model} with {new_type.model}" in text for text in message_texts(request))

    def test_replacement_removes_the_single_real_serial_conflict(self):
        Module, device, _old_type, new_type, bay, installed, request, view, inventory = self._setup(
            "conflict",
            new_serial="SHARED-NEW-SERIAL",
        )
        conflict_device = make_device("replace-conflict-owner")
        conflict_type = make_module_type("REPLACE-CONFLICT-OWNER-TYPE")
        conflict_bay = make_module_bay(conflict_device, "Replace Conflict Owner Bay")
        conflict = _module(conflict_device, conflict_bay, conflict_type, "SHARED-NEW-SERIAL")
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert not Module.objects.filter(pk__in=[installed.pk, conflict.pk]).exists()
        replacement = Module.objects.get(device=device, module_bay=bay)
        assert replacement.module_type == new_type
        assert replacement.serial == "SHARED-NEW-SERIAL"
        assert any("Removed REPLACE-CONFLICT-OWNER-TYPE" in text for text in message_texts(request, "info"))

    def test_interface_scope_is_checked_before_the_old_module_is_deleted(self):
        from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleType

        from netbox_librenms_plugin.tests.view_test_helpers import grant
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("replace-adoption-scope")
        old_type = make_module_type("REPLACE-ADOPTION-OLD")
        new_type = make_module_type("REPLACE-ADOPTION-NEW")
        InterfaceTemplate.objects.create(module_type=new_type, name="Ethernet1", type="10gbase-x-sfpp")
        bay = make_module_bay(device, "Replace Adoption Bay")
        installed = _module(device, bay, old_type, "ADOPTION-OLD-SERIAL")
        hidden = make_interface(device, "Ethernet1", iface_type="10gbase-x-sfpp")
        user = make_user_with_perms(
            "replace-adoption-scope",
            [
                ("view", Device),
                ("view", ModuleType),
                ("add", Module),
                ("change", Module),
                ("delete", Module),
                ("add", Interface),
                ("delete", Interface),
            ],
        )
        user = grant(user, "change", Interface, constraints={"device__modules__isnull": True})
        inventory = [
            {
                "entPhysicalIndex": 100,
                "entPhysicalModelName": new_type.model,
                "entPhysicalSerialNum": "ADOPTION-NEW-SERIAL",
            }
        ]
        request = make_request(
            "post",
            {
                "module_id": str(installed.pk),
                "ent_index": "100",
                "server_key": "default",
                "inventory_binding": module_row_binding(
                    device,
                    "replace_module",
                    inventory[0],
                    action_target={"module_id": installed.pk},
                ),
            },
            user=user,
        )
        view = _view(ReplaceModuleView, request)
        cache_key = _cache_inventory(view, device, inventory)
        try:
            response = view_post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert Module.objects.filter(pk=installed.pk, module_type=old_type, module_bay=bay).exists()
        assert not Module.objects.filter(device=device, module_type=new_type).exists()
        hidden.refresh_from_db()
        assert hidden.module_id is None
        assert any("not available for module adoption" in text for text in message_texts(request))

    def test_real_permission_gate_rejects_a_user_without_module_permissions(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("replace-permission")
        user = make_user_with_perms("replace-permission", [("view", Device)])
        request = make_request("post", {"module_id": "1", "ent_index": "100"}, user=user)

        response = view_post(_view(ReplaceModuleView, request), request, pk=device.pk)

        assert response.status_code == 302
        assert response.url == "/"
        assert any("Missing permissions" in text for text in message_texts(request))


class TestMoveModuleView:
    def test_invalid_parameters_redirect_with_a_real_error_message(self):
        from netbox_librenms_plugin.views.sync.modules import MoveModuleView

        device = make_device("move-invalid")
        request = make_request("post", {})

        # Relocation exists from NetBox 4.7; CI also gates 4.4/4.6, where the view refuses before
        # it reads these parameters, so the assertion below would pass for the wrong reason.
        with patch("netbox_librenms_plugin.views.sync.modules.netbox_relocates_module_subtree", return_value=True):
            response = view_post(_view(MoveModuleView, request), request, pk=device.pk)

        assert response.status_code == 302
        assert any("Missing or invalid conflict_module_id/target_bay_id" in text for text in message_texts(request))

    def test_moves_a_real_module_and_removes_the_real_target_occupant(self):
        from dcim.models import Module

        from netbox_librenms_plugin.views.sync.modules import MoveModuleView

        source_device = make_device("move-source")
        target_device = make_device("move-target")
        module_type = make_module_type("MOVE-TYPE")
        occupant_type = make_module_type("MOVE-OCCUPANT-TYPE")
        source_bay = make_module_bay(source_device, "Source Bay")
        target_bay = make_module_bay(target_device, "Target Bay")
        moving = _module(source_device, source_bay, module_type, "MOVE-SERIAL")
        occupant = _module(target_device, target_bay, occupant_type, "OCCUPANT-SERIAL")
        request = make_request(
            "post",
            {
                "conflict_module_id": str(moving.pk),
                "target_bay_id": str(target_bay.pk),
                "module_id": str(occupant.pk),
                "selected_device_id": str(target_device.pk),
                "server_key": "default",
            },
        )

        # Relocation exists from NetBox 4.7; CI also gates 4.4/4.6, where the view refuses.
        with patch("netbox_librenms_plugin.views.sync.modules.netbox_relocates_module_subtree", return_value=True):
            response = view_post(_view(MoveModuleView, request), request, pk=target_device.pk)

        moving.refresh_from_db()
        assert response.status_code == 302
        assert moving.device == target_device
        assert moving.module_bay == target_bay
        assert not Module.objects.filter(pk=occupant.pk).exists()
        assert any(
            "Moved MOVE-TYPE from move-source/Source Bay to Target Bay" in text for text in message_texts(request)
        )

    def test_real_permission_gate_rejects_a_user_without_change_scope(self):
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.modules import MoveModuleView

        device = make_device("move-permission")
        user = make_user_with_perms("move-permission", [("view", Device)])
        request = make_request(
            "post",
            {"conflict_module_id": "1", "target_bay_id": "1"},
            user=user,
        )

        response = view_post(_view(MoveModuleView, request), request, pk=device.pk)

        assert response.status_code == 302
        assert response.url == "/"
        assert any("Missing permissions" in text for text in message_texts(request))
