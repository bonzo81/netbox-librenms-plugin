"""Tests for ModuleMismatchPreviewView, ReplaceModuleView, and MoveModuleView."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


from netbox_librenms_plugin.tests.view_test_helpers import get as _get, post as _post


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_device(pk=24, name="test-device"):
    d = MagicMock()
    d.pk = pk
    d.name = name
    d.device_type = MagicMock()
    d.device_type.manufacturer = None
    return d


def _make_module(pk=42, serial="OLD_SERIAL", bay_name="Slot 1", bay_id=10, type_id=5, type_model="XCM-7s"):
    module = MagicMock()
    module.pk = pk
    module.serial = serial
    module.module_bay = MagicMock()
    module.module_bay.pk = bay_id
    module.module_bay.name = bay_name
    module.module_bay_id = bay_id
    module.module_type = MagicMock()
    module.module_type.pk = type_id
    module.module_type.model = type_model
    module.module_type_id = type_id
    module.device = _make_device()
    module.get_absolute_url.return_value = f"/dcim/modules/{pk}/"
    return module


def _make_request(method="GET", data=None):
    req = MagicMock()
    req.method = method
    if method == "GET":
        req.GET = data or {}
    else:
        req.POST = data or {}
    return req


# ---------------------------------------------------------------------------
# ModuleMismatchPreviewView
# ---------------------------------------------------------------------------


class TestModuleMismatchPreviewView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.modules import ModuleMismatchPreviewView

        v = object.__new__(ModuleMismatchPreviewView)
        v._librenms_api = MagicMock()
        v._librenms_api.server_key = "default"
        return v

    def test_missing_params_returns_400(self):
        """GET without module_id or ent_index returns 400."""
        view = self._view()
        device = _make_device()
        request = _make_request(data={})
        view.request = request

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=device,
        ):
            resp = view.get(request, pk=24)

        assert resp.status_code == 400

    def test_invalid_ent_index_returns_400(self):
        """GET with non-integer ent_index returns 400."""
        view = self._view()
        device = _make_device()
        request = _make_request(data={"module_id": "42", "ent_index": "notanint"})
        view.request = request

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=device,
        ):
            resp = view.get(request, pk=24)

        assert resp.status_code == 400

    def test_no_cache_returns_400(self):
        """GET with valid params but no cached inventory returns 400."""
        view = self._view()
        device = _make_device()
        installed = _make_module()
        request = _make_request(data={"module_id": "42", "ent_index": "100"})
        view.request = request

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, installed],
            ),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            resp = _get(view, request, pk=24)

        assert resp.status_code == 400

    def test_item_not_in_cache_returns_400(self):
        """GET returns 400 when ent_index not found in cached data."""
        view = self._view()
        device = _make_device()
        installed = _make_module()
        request = _make_request(data={"module_id": "42", "ent_index": "999"})
        view.request = request
        cached = [{"entPhysicalIndex": 100, "entPhysicalModelName": "XCM-7s", "entPhysicalSerialNum": "S1"}]

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, installed],
            ),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
        ):
            mock_cache.get.return_value = {"inventory": cached, "librenms_id": "test"}
            resp = _get(view, request, pk=24)

        assert resp.status_code == 400

    def test_renders_template_on_success(self):
        """GET with valid data returns 200 with rendered template."""
        from django.http import HttpResponse

        view = self._view()
        device = _make_device()
        installed = _make_module(type_id=5, type_model="XCM-7s")
        request = _make_request(data={"module_id": "42", "ent_index": "100"})
        view.request = request
        cached = [{"entPhysicalIndex": 100, "entPhysicalModelName": "XCM-7s", "entPhysicalSerialNum": "NS123"}]

        matched_type = MagicMock()
        matched_type.pk = 5

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, installed],
            ),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_module_types_indexed",
                return_value={"XCM-7s": matched_type},
            ),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s"),
            patch("dcim.models.Module") as mock_module_cls,
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value=HttpResponse("OK")) as mock_render,
        ):
            mock_cache.get.return_value = {"inventory": cached, "librenms_id": "test"}
            mock_module_cls.objects.restrict.return_value = mock_module_cls.objects
            mock_module_cls.objects.filter.return_value.exclude.return_value.count.return_value = 0
            resp = view.get(request, pk=24)

        assert resp.status_code == 200
        mock_render.assert_called_once()
        ctx = mock_render.call_args[0][2]
        assert ctx["device_pk"] == 24
        assert ctx["librenms_serial"] == "NS123"

    def test_serial_conflict_passed_to_template(self):
        """When serial exists elsewhere, serial_conflict is set in template context."""
        from django.http import HttpResponse

        view = self._view()
        device = _make_device()
        installed = _make_module(serial="OLD", type_id=5)
        request = _make_request(data={"module_id": "42", "ent_index": "100"})
        view.request = request
        cached = [{"entPhysicalIndex": 100, "entPhysicalModelName": "XCM-7s", "entPhysicalSerialNum": "NEW_SERIAL"}]

        matched_type = MagicMock()
        matched_type.pk = 5
        conflict_module = MagicMock()

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, installed],
            ),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_module_types_indexed",
                return_value={"XCM-7s": matched_type},
            ),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s"),
            patch("dcim.models.Module") as mock_module_cls,
            patch("netbox_librenms_plugin.views.sync.modules.render", return_value=HttpResponse("OK")) as mock_render,
        ):
            mock_cache.get.return_value = {"inventory": cached, "librenms_id": "test"}
            mock_module_cls.objects.restrict.return_value = mock_module_cls.objects
            mock_module_cls.objects.filter.return_value.exclude.return_value.count.return_value = 1
            mock_module_cls.objects.filter.return_value.select_related.return_value.first.return_value = conflict_module
            view.get(request, pk=24)

        ctx = mock_render.call_args[0][2]
        assert ctx["serial_conflict"] is conflict_module


@pytest.mark.django_db
class TestModuleMismatchTypeMatchedBadge:
    """The mismatch modal badges the LibreNMS model as matched when it resolves to the installed type.

    DB-backed and rendering the REAL template (the sibling tests above patch render/resolve): builds a
    real ModuleTypeMapping so a LibreNMS model string that DIFFERS from the NetBox type still resolves
    to it — the user's serial-mismatch case where only text said "same type".
    """

    def _build(self, *, installed_type_model, mapped_type_model, librenms_model, installed_serial, librenms_serial):
        from dcim.models import (
            Device,
            DeviceRole,
            DeviceType,
            Manufacturer,
            Module,
            ModuleBay,
            ModuleType,
            Site,
        )

        from netbox_librenms_plugin.models import ModuleTypeMapping

        tag = mapped_type_model.lower().replace(" ", "-")
        mfr, _ = Manufacturer.objects.get_or_create(name=f"Mfr-mmb-{tag}", slug=f"mfr-mmb-{tag}")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"DT-mmb-{tag}", slug=f"dt-mmb-{tag}")
        role, _ = DeviceRole.objects.get_or_create(name=f"Role-mmb-{tag}", slug=f"role-mmb-{tag}")
        site, _ = Site.objects.get_or_create(name=f"Site-mmb-{tag}", slug=f"site-mmb-{tag}")
        device = Device.objects.create(name=f"host-mmb-{tag}", device_type=dt, role=role, site=site, status="active")

        installed_type = ModuleType.objects.create(manufacturer=mfr, model=installed_type_model)
        mapped_type = (
            installed_type
            if mapped_type_model == installed_type_model
            else ModuleType.objects.create(manufacturer=mfr, model=mapped_type_model)
        )
        # The LibreNMS model string DIFFERS from the NetBox type model but maps to it (the user's case).
        ModuleTypeMapping.objects.create(
            librenms_model=librenms_model, netbox_module_type=mapped_type, manufacturer=mfr
        )

        bay = ModuleBay.objects.create(device=device, name="Slot 1")
        module = Module.objects.create(
            device=device, module_bay=bay, module_type=installed_type, serial=installed_serial
        )
        return device, module

    def _render_modal(self, device, module, librenms_model, librenms_serial):
        from unittest.mock import MagicMock

        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_superuser

        from netbox_librenms_plugin.views.sync.modules import (
            ModuleMismatchPreviewView,
            _get_sync_device_for_inventory,
        )

        view = object.__new__(ModuleMismatchPreviewView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        request = RequestFactory().get("/", {"module_id": str(module.pk), "ent_index": "100", "server_key": "default"})
        # A real user: the view resolves the device through a restricted queryset, and an
        # anonymous caller legitimately sees nothing.
        request.user = make_superuser("mmb-su")
        view.setup(request)

        sync_device = _get_sync_device_for_inventory(device, "default")
        ckey = view.get_cache_key(sync_device, "inventory", server_key="default")
        cache.set(
            ckey,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 100,
                        "entPhysicalModelName": librenms_model,
                        "entPhysicalSerialNum": librenms_serial,
                    }
                ],
                "librenms_id": 1,
            },
        )
        try:
            with patch.object(view, "require_object_permissions", return_value=None):
                resp = view.get(request, pk=device.pk)
        finally:
            cache.delete(ckey)
        return resp

    def test_matched_model_shows_check_badge(self):
        """A mapped LibreNMS model (different string, same type) renders the matched check badge."""
        device, module = self._build(
            installed_type_model="MDA-s36-400gb-qsfpdd",
            mapped_type_model="MDA-s36-400gb-qsfpdd",
            librenms_model="3HE12391AARK01",
            installed_serial="",  # NetBox has no serial (the user's exact case)
            librenms_serial="NS2217F6334",
        )
        resp = self._render_modal(device, module, "3HE12391AARK01", "NS2217F6334")

        assert resp.status_code == 200
        html = resp.content.decode()
        assert "mdi-check-decagram" in html  # the matched badge is present
        assert "Recognised as NetBox module type MDA-s36-400gb-qsfpdd" in html
        assert "3HE12391AARK01" in html  # the differing LibreNMS string is still shown

    def test_mismatched_type_has_no_check_badge(self):
        """When the LibreNMS model maps to a DIFFERENT type, no matched badge — the danger path shows."""
        device, module = self._build(
            installed_type_model="MDA-s36-400gb-qsfpdd",
            mapped_type_model="XMA-other-type",
            librenms_model="3HE-OTHER-PN",
            installed_serial="EXISTING",
            librenms_serial="NS-NEW",
        )
        resp = self._render_modal(device, module, "3HE-OTHER-PN", "NS-NEW")

        assert resp.status_code == 200
        html = resp.content.decode()
        assert "mdi-check-decagram" not in html  # type mismatch → no matched badge
        assert "Different module type" in html


# ---------------------------------------------------------------------------
# ReplaceModuleView
# ---------------------------------------------------------------------------


class TestReplaceModuleView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        v = object.__new__(ReplaceModuleView)
        # Bypass permission mixin
        v.required_object_permissions = {}
        v._librenms_api = MagicMock()
        v._librenms_api.server_key = "default"
        return v

    def test_missing_params_redirects_with_error(self):
        """POST without module_id or ent_index adds error and redirects."""
        view = self._view()
        device = _make_device()
        request = _make_request("POST", data={})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.request = request
            view.post(request, pk=24)

        mock_msg.error.assert_called_once()
        mock_redirect.assert_called_once()

    def test_no_cache_redirects_with_error(self):
        """POST with valid params but no cache adds error and redirects."""
        view = self._view()
        device = _make_device()
        installed = _make_module()
        request = _make_request("POST", data={"module_id": "42", "ent_index": "100"})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, installed],
            ),
            patch.object(view, "require_all_permissions", return_value=None),
            patch.object(view, "get_cache_key", return_value="ck"),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            mock_cache.get.return_value = None
            _post(view, request, pk=24)

        mock_msg.error.assert_called_once()
        mock_redirect.assert_called_once()

    @pytest.mark.django_db
    def test_replace_deletes_old_and_creates_new(self):
        """POST with valid data deletes old module and creates new one."""
        from types import SimpleNamespace

        from dcim.models import Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view, message_texts
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("replace-real-device")
        old_type = make_module_type("REPLACE-REAL-OLD")
        new_type = make_module_type("REPLACE-REAL-NEW")
        bay = make_module_bay(device, "Replace Real Bay")
        installed = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=old_type,
            serial="REPLACE-REAL-OLD-SERIAL",
        )
        request = make_request(
            "post",
            {"module_id": str(installed.pk), "ent_index": "100", "server_key": "default"},
        )
        view = make_view(
            ReplaceModuleView,
            request,
            librenms_api=SimpleNamespace(server_key="default"),
        )
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(
            cache_key,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 100,
                        "entPhysicalModelName": new_type.model,
                        "entPhysicalSerialNum": "REPLACE-REAL-NEW-SERIAL",
                    }
                ],
                "librenms_id": 1,
            },
            timeout=300,
        )
        try:
            response = _post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert not Module.objects.filter(pk=installed.pk).exists()
        replacement = Module.objects.get(device=device, module_bay=bay)
        assert replacement.module_type == new_type
        assert replacement.serial == "REPLACE-REAL-NEW-SERIAL"
        assert any("Replaced REPLACE-REAL-OLD with REPLACE-REAL-NEW" in text for text in message_texts(request))

    @pytest.mark.django_db
    def test_replace_removes_serial_conflict_from_db(self):
        """POST re-derives the conflicting module from serial, not from conflict_module_id."""
        from types import SimpleNamespace

        from dcim.models import Module
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view, message_texts
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("replace-conflict-target")
        conflict_device = make_device("replace-conflict-source")
        old_type = make_module_type("REPLACE-CONFLICT-OLD")
        new_type = make_module_type("REPLACE-CONFLICT-NEW")
        conflict_type = make_module_type("REPLACE-CONFLICT-SOURCE")
        target_bay = make_module_bay(device, "Replace Conflict Target Bay")
        conflict_bay = make_module_bay(conflict_device, "Replace Conflict Source Bay")
        installed = Module.objects.create(
            device=device,
            module_bay=target_bay,
            module_type=old_type,
            serial="REPLACE-CONFLICT-OLD-SERIAL",
        )
        conflict = Module.objects.create(
            device=conflict_device,
            module_bay=conflict_bay,
            module_type=conflict_type,
            serial="REPLACE-CONFLICT-NEW-SERIAL",
        )
        request = make_request(
            "post",
            {"module_id": str(installed.pk), "ent_index": "100", "server_key": "default"},
        )
        view = make_view(
            ReplaceModuleView,
            request,
            librenms_api=SimpleNamespace(server_key="default"),
        )
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(
            cache_key,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 100,
                        "entPhysicalModelName": new_type.model,
                        "entPhysicalSerialNum": conflict.serial,
                    }
                ],
                "librenms_id": 1,
            },
            timeout=300,
        )
        try:
            response = _post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert not Module.objects.filter(pk__in=[installed.pk, conflict.pk]).exists()
        replacement = Module.objects.get(device=device, module_bay=target_bay)
        assert replacement.module_type == new_type
        assert replacement.serial == "REPLACE-CONFLICT-NEW-SERIAL"
        assert any("Removed REPLACE-CONFLICT-SOURCE" in text for text in message_texts(request, "info"))

    @pytest.mark.django_db
    def test_replacement_checks_interface_scope_before_deleting_the_old_module(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleType
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import (
            make_device,
            make_interface,
            make_module_bay,
            make_module_type,
        )
        from netbox_librenms_plugin.tests.view_test_helpers import (
            grant,
            make_request,
            make_user_with_perms,
            make_view,
            message_texts,
        )
        from netbox_librenms_plugin.views.sync.modules import ReplaceModuleView

        device = make_device("replace-adoption-scope")
        old_type = make_module_type("REPLACE-ADOPTION-OLD")
        new_type = make_module_type("REPLACE-ADOPTION-NEW")
        InterfaceTemplate.objects.create(
            module_type=new_type,
            name="Te1/1/1",
            type="10gbase-x-sfpp",
        )
        bay = make_module_bay(device, "Replace Adoption Scope Bay")
        installed = Module.objects.create(
            device=device,
            module_bay=bay,
            module_type=old_type,
            serial="REPLACE-ADOPTION-OLD-SERIAL",
        )
        hidden = make_interface(device, "Te1/1/1", iface_type="10gbase-x-sfpp")
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
        request = make_request(
            "post",
            {"module_id": str(installed.pk), "ent_index": "100", "server_key": "default"},
            user=user,
        )
        view = make_view(
            ReplaceModuleView,
            request,
            librenms_api=SimpleNamespace(server_key="default"),
        )
        cache_key = view.get_cache_key(device, "inventory", server_key="default")
        cache.set(
            cache_key,
            {
                "inventory": [
                    {
                        "entPhysicalIndex": 100,
                        "entPhysicalModelName": new_type.model,
                        "entPhysicalSerialNum": "REPLACE-ADOPTION-NEW-SERIAL",
                    }
                ],
                "librenms_id": 1,
            },
            timeout=300,
        )
        try:
            response = _post(view, request, pk=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert Module.objects.filter(pk=installed.pk, module_type=old_type, module_bay=bay).exists()
        assert not Module.objects.filter(device=device, module_type=new_type).exists()
        hidden.refresh_from_db()
        assert hidden.module_id is None
        recorded_messages = message_texts(request)
        assert any("not available for module adoption" in text for text in recorded_messages), recorded_messages

    def test_requires_all_permissions(self):
        """POST returns early when require_all_permissions returns a response."""
        from django.http import HttpResponse

        view = self._view()
        device = _make_device()
        request = _make_request("POST", data={"module_id": "42", "ent_index": "100"})

        deny = HttpResponse(status=403)

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch.object(view, "require_all_permissions", return_value=deny),
        ):
            view.request = request
            resp = view.post(request, pk=24)

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# MoveModuleView
# ---------------------------------------------------------------------------


class TestMoveModuleView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.modules import MoveModuleView

        v = MoveModuleView()
        v._librenms_api = SimpleNamespace(server_key="production")
        v.required_object_permissions = {}
        return v

    def test_missing_params_redirects_with_error(self):
        """POST without conflict_module_id or target_bay_id redirects with error."""
        view = self._view()
        device = _make_device()
        request = _make_request("POST", data={})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
        ):
            view.request = request
            view.post(request, pk=24)

        mock_msg.error.assert_called_once()
        mock_redirect.assert_called_once()

    def test_move_updates_module_bay(self):
        """POST moves conflict_module to target_bay."""
        view = self._view()
        device = _make_device(pk=24)
        conflict_module = _make_module(pk=99, serial="SN1", bay_name="Slot 3", bay_id=30)
        target_bay = MagicMock()
        target_bay.name = "Slot 1"
        target_bay.pk = 10
        request = _make_request("POST", data={"conflict_module_id": "99", "target_bay_id": "10"})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, target_bay],
            ),
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
            patch("dcim.models.Module"),
            patch("dcim.models.ModuleBay") as mock_bay_cls,
            # The conflict module is resolved through the SCOPED queryset (it is mutated below),
            # so that is the seam this test stubs.
            patch.object(view, "restricted_queryset") as mock_scoped,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            # Both the locked target bay and the conflict module are resolved through
            # restricted_queryset now, so dispatch on the model the view asks for.
            bay_qs = MagicMock()
            bay_qs.select_for_update.return_value.filter.return_value.first.return_value = target_bay
            module_qs = MagicMock()
            module_qs.select_for_update.return_value.filter.return_value.select_related.return_value.first.return_value = conflict_module
            mock_scoped.side_effect = lambda model, *a, **kw: bay_qs if model is mock_bay_cls else module_qs

            _post(view, request, pk=24)

        assert conflict_module.module_bay is target_bay
        assert conflict_module.device is device
        conflict_module.full_clean.assert_called_once()
        conflict_module.save.assert_called_once()
        mock_msg.success.assert_called_once()

    def test_target_bay_deleted_before_lock_reports_error(self):
        """A target bay removed after validation must not cause an uncaught exception."""
        from dcim.models import ModuleBay

        view = self._view()
        device = _make_device(pk=24)
        target_bay = MagicMock()
        request = _make_request("POST", data={"conflict_module_id": "99", "target_bay_id": "10"})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, target_bay],
            ),
            patch.object(view, "require_all_permissions", return_value=None),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect") as mock_redirect,
            patch.object(view, "restricted_queryset") as mock_scoped,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda context: context
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            bay_qs = MagicMock()
            bay_qs.select_for_update.return_value.filter.return_value.first.return_value = None
            module_qs = MagicMock()
            mock_scoped.side_effect = lambda model, *args, **kwargs: bay_qs if model is ModuleBay else module_qs

            _post(view, request, pk=24)

        mock_msg.error.assert_called_once_with(request, "Module bay no longer exists.")
        mock_redirect.assert_called_once()

    def test_requires_all_permissions(self):
        """POST returns early when require_all_permissions returns a response."""
        from django.http import HttpResponse

        view = self._view()
        device = _make_device()
        request = _make_request("POST", data={"conflict_module_id": "99", "target_bay_id": "10"})

        deny = HttpResponse(status=403)
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=device,
            ),
            patch.object(view, "require_all_permissions", return_value=deny),
        ):
            view.request = request
            resp = view.post(request, pk=24)

        assert resp.status_code == 403
