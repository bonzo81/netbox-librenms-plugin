"""Tests for ModuleMismatchPreviewView, ReplaceModuleView, and MoveModuleView."""

from unittest.mock import MagicMock, patch

import pytest


def _bind_and_call(view, request, method, **kwargs):
    """Call *view*.<method>, binding the request the way ``View.setup()`` does under dispatch().

    A direct ``view.post(request, ...)`` leaves ``self.request`` unset, which the object-scoped
    lookups read — production always goes through dispatch(), so bind it here too.
    """
    view.setup(request)
    return getattr(view, method)(request, **kwargs)


def _post(view, request, **kwargs):
    """POST into *view* with the request bound (see :func:`_bind_and_call`)."""
    return _bind_and_call(view, request, "post", **kwargs)


def _get(view, request, **kwargs):
    """GET into *view* with the request bound (see :func:`_bind_and_call`)."""
    return _bind_and_call(view, request, "get", **kwargs)


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
            mock_module_cls.objects.filter.return_value.exclude.return_value.select_related.return_value.count.return_value = 0
            mock_module_cls.objects.filter.return_value.exclude.return_value.select_related.return_value.first.return_value = None
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
            mock_module_cls.objects.filter.return_value.exclude.return_value.select_related.return_value.count.return_value = 1
            mock_module_cls.objects.filter.return_value.exclude.return_value.select_related.return_value.first.return_value = conflict_module
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

    def test_replace_deletes_old_and_creates_new(self):
        """POST with valid data deletes old module and creates new one."""
        view = self._view()
        device = _make_device()
        sync_device = _make_device(pk=999, name="vc-sync-device")
        installed = _make_module(serial="OLD", type_id=5)
        request = _make_request("POST", data={"module_id": "42", "ent_index": "100", "server_key": "prod"})
        cached = [{"entPhysicalIndex": 100, "entPhysicalModelName": "XCM-7s", "entPhysicalSerialNum": "NEW"}]
        matched_type = MagicMock()
        matched_type.model = "XCM-7s"

        new_module = MagicMock()

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, installed],
            ),
            patch.object(view, "require_all_permissions", return_value=None),
            # "prod" must be a configured server for resolve_posted_server_key to honour the posted key
            # (else it degrades to the active "default"); mirrors a real multi-server deployment.
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"prod": "Prod", "default": "Default"},
            ),
            patch.object(view, "get_cache_key", return_value="ck") as mock_get_cache_key,
            patch(
                "netbox_librenms_plugin.views.sync.modules._get_sync_device_for_inventory",
                return_value=sync_device,
            ) as mock_get_sync_device,
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_module_types_indexed",
                return_value={"XCM-7s": matched_type},
            ),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
            patch("dcim.models.Module") as mock_module_cls,
        ):
            mock_cache.get.return_value = {"inventory": cached, "librenms_id": "test"}
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            mock_module_cls.return_value = new_module
            # New code does TWO select_for_update() calls inside the atomic
            # block — one for the installed-module re-fetch, one for the
            # serial-conflict re-derivation (returns empty list here).
            installed_chain = MagicMock()
            installed_chain.filter.return_value.select_related.return_value.first.return_value = installed
            conflict_chain = MagicMock()
            conflict_chain.filter.return_value.exclude.return_value.select_related.return_value.__iter__ = lambda s: (
                iter([])
            )
            mock_module_cls.objects.select_for_update.side_effect = [installed_chain, conflict_chain]

            _post(view, request, pk=24)

        installed.delete.assert_called_once()
        new_module.full_clean.assert_called_once()
        new_module.save.assert_called_once()
        mock_msg.success.assert_called_once()
        # server_key from POST must be forwarded to the cache key lookup
        mock_get_sync_device.assert_called_with(device, "prod")
        mock_get_cache_key.assert_called_with(sync_device, "inventory", server_key="prod")

    def test_replace_removes_serial_conflict_from_db(self):
        """POST re-derives the conflicting module from serial, not from conflict_module_id."""
        view = self._view()
        device = _make_device()
        installed = _make_module(serial="OLD", type_id=5)
        # No conflict_module_id in POST — conflict must be derived from serial
        request = _make_request("POST", data={"module_id": "42", "ent_index": "100", "server_key": "prod"})
        cached = [
            {"entPhysicalIndex": 100, "entPhysicalModelName": "XCM-7s", "entPhysicalSerialNum": "CONFLICT_SERIAL"}
        ]
        matched_type = MagicMock()
        matched_type.model = "XCM-7s"

        conflict = _make_module(pk=55, serial="CONFLICT_SERIAL", bay_name="Slot 3")
        new_module = MagicMock()

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                side_effect=[device, installed],
            ),
            patch.object(view, "require_all_permissions", return_value=None),
            patch.object(view, "get_cache_key", return_value="ck"),
            patch("netbox_librenms_plugin.views.sync.modules.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.modules.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.views.sync.modules.get_module_types_indexed",
                return_value={"XCM-7s": matched_type},
            ),
            patch("netbox_librenms_plugin.utils.apply_normalization_rules", return_value="XCM-7s"),
            patch("netbox_librenms_plugin.views.sync.modules.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.sync.modules.messages") as mock_msg,
            patch("netbox_librenms_plugin.views.sync.modules.redirect"),
            patch("dcim.models.Module") as mock_module_cls,
        ):
            mock_cache.get.return_value = {"inventory": cached, "librenms_id": "test"}
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            mock_module_cls.return_value = new_module
            # New code: both the installed re-fetch and the serial-conflict
            # re-derivation use select_for_update() to lock both rows inside
            # the atomic block.  Configure the chained mocks accordingly:
            #   1. installed = Module.objects.select_for_update()
            #                    .filter(pk=, device=).select_related().first()
            #   2. conflicts = list(Module.objects.select_for_update()
            #                    .filter(serial=).exclude(pk=).select_related(...))
            installed_chain = MagicMock()
            installed_chain.filter.return_value.select_related.return_value.first.return_value = installed
            conflict_chain = MagicMock()
            conflict_chain.filter.return_value.exclude.return_value.select_related.return_value.__iter__ = lambda s: (
                iter([conflict])
            )
            mock_module_cls.objects.select_for_update.side_effect = [installed_chain, conflict_chain]

            _post(view, request, pk=24)

        # Conflict module must be deleted, then the installed module, then new one saved
        conflict.delete.assert_called_once()
        installed.delete.assert_called_once()
        new_module.save.assert_called_once()
        mock_msg.info.assert_called_once()
        mock_msg.success.assert_called_once()

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
            resp = view.post(request, pk=24)

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# MoveModuleView
# ---------------------------------------------------------------------------


class TestMoveModuleView:
    def _view(self):
        from netbox_librenms_plugin.views.sync.modules import MoveModuleView

        v = object.__new__(MoveModuleView)
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
            patch("dcim.models.Module") as mock_module_cls,
            patch("dcim.models.ModuleBay") as mock_bay_cls,
        ):
            mock_tx.atomic.return_value.__enter__ = lambda s: s
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
            # select_for_update on ModuleBay returns locked target_bay
            mock_bay_cls.objects.select_for_update.return_value.get.return_value = target_bay
            # select_for_update chain returns conflict_module
            sfu_qs = MagicMock()
            sfu_qs.filter.return_value.select_related.return_value.first.return_value = conflict_module
            mock_module_cls.objects.select_for_update.return_value = sfu_qs
            # No occupant in target bay
            mock_module_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = None

            view.post(request, pk=24)

        assert conflict_module.module_bay is target_bay
        assert conflict_module.device is device
        conflict_module.full_clean.assert_called_once()
        conflict_module.save.assert_called_once()
        mock_msg.success.assert_called_once()

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
            resp = view.post(request, pk=24)

        assert resp.status_code == 403
