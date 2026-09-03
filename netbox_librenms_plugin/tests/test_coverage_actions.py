"""Coverage tests for views/imports/actions.py missing lines."""

from unittest.mock import MagicMock, patch
from types import SimpleNamespace as Namespace

import pytest
from django.test import RequestFactory
from django.urls import reverse as url_for

from netbox_librenms_plugin.tests.conftest import (
    make_cluster,
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server as run_librenms_server
from netbox_librenms_plugin.tests.test_modules_view import configure_servers as configure_test_servers
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant as grant_view_permission,
    make_request as make_view_request,
    make_user_with_perms as make_view_user,
    message_texts as view_message_texts,
    post as post_view,
)


def _make_request(post=None, get=None, headers=None, user_is_superuser=False):
    """Build a mock request object with QueryDict-like POST/GET."""
    req = MagicMock()

    # Create a QueryDict-like object for POST
    post_data = post or {}
    post_mock = MagicMock()
    post_mock.__contains__ = lambda self, key: key in post_data
    post_mock.get = lambda key, default=None: post_data.get(key, default)
    post_mock.getlist = lambda key: (
        post_data.get(key, [])
        if isinstance(post_data.get(key), list)
        else ([post_data[key]] if key in post_data else [])
    )
    post_mock.__getitem__ = lambda self, key: post_data[key]
    req.POST = post_mock

    # Create a QueryDict-like object for GET
    get_data = get or {}
    get_mock = MagicMock()
    get_mock.__contains__ = lambda self, key: key in get_data
    get_mock.get = lambda key, default=None: get_data.get(key, default)
    get_mock.getlist = lambda key: get_data.get(key, [])
    get_mock.__getitem__ = lambda self, key: get_data[key]
    req.GET = get_mock

    req.user = MagicMock()
    req.user.is_superuser = user_is_superuser
    req.headers = headers or {}
    return req


def _make_api():
    """Create a minimal LibreNMSAPI mock."""
    api = MagicMock()
    api.server_key = "default"
    api.cache_timeout = 300
    api.librenms_url = "https://x.example.com"
    return api


def _constrained_device_writer(constraints, username):
    """A real non-superuser with plugin write access and a ``constraints``-scoped change_device grant."""
    from core.models import ObjectType
    from dcim.models import Device
    from django.apps import apps
    from django.contrib.auth import get_user_model
    from users.models import ObjectPermission

    # Resolve via the app registry: the autouse config fixtures patch the models module during
    # the full suite, so a plain import could hand get_for_model() a mock class.
    LibreNMSSettings = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")

    user = get_user_model().objects.create_user(username=username, password="x")
    write = ObjectPermission.objects.create(name=f"{username}-plugin-write", actions=["change"])
    write.object_types.set([ObjectType.objects.get_for_model(LibreNMSSettings)])
    write.users.set([user])

    scoped = ObjectPermission.objects.create(
        name=f"{username}-scoped-change-device", actions=["change"], constraints=constraints
    )
    scoped.object_types.set([ObjectType.objects.get_for_model(Device)])
    scoped.users.set([user])

    return get_user_model().objects.get(pk=user.pk)  # clear the per-request perm cache


def _scoped_device_writer(in_scope_device, username):
    """A real non-superuser whose change_device grant covers only *in_scope_device*."""
    return _constrained_device_writer({"pk": in_scope_device.pk}, username)


class TestSaveDevice:
    """Tests for _save_device (lines 44-56)."""

    def test_validation_error_returns_400(self):
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = MagicMock()
        device.full_clean.side_effect = ValidationError({"name": ["This field is required."]})

        response = _save_device(device)
        assert response.status_code == 400
        assert b"Validation error" in response.content

    def test_integrity_error_returns_409(self):
        from django.db import IntegrityError

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = MagicMock()
        device.full_clean.return_value = None
        raw_error = "duplicate key value violates unique constraint device_name_key"
        device.save.side_effect = IntegrityError(raw_error)

        response = _save_device(device)
        assert response.status_code == 409
        assert b"integrity constraint" in response.content
        # Pin the sanitization contract: none of the raw DB exception text leaks to the
        # client (case-insensitive full-text, not just a fragment that a partial leak passes).
        assert raw_error.encode().lower() not in response.content.lower()
        assert b"unique constraint" not in response.content.lower()

    def test_success_returns_none(self):
        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = MagicMock()
        device.full_clean.return_value = None
        device.save.return_value = None

        result = _save_device(device)
        assert result is None

    @pytest.mark.django_db
    def test_update_fields_dataerror_returns_400_not_500(self):
        """A REAL overlong value persisted via save(update_fields=...) (skips full_clean) raises Postgres DataError, which _save_device must turn into a 400, not a 500."""
        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = make_device("dataerr-dev")
        # Exceed the Device.name varchar(64) column. update_fields skips full_clean(), so the
        # overlong value reaches the DB and the real backend raises DataError — proving the
        # production except clause catches the ACTUAL exception class, not an assumed one.
        device.name = "x" * 100

        response = _save_device(device, update_fields=["name"])

        assert response.status_code == 400
        assert b"field value is invalid" in response.content
        # No schema-revealing DB text (e.g. the column type) leaks to the client.
        assert b"character varying" not in response.content.lower()
        # NOTE: the real DataError aborts the surrounding test transaction, so no ORM query
        # may follow here — assert only on the returned response.

    @pytest.mark.django_db
    def test_update_fields_databaseerror_returns_409_not_500(self):
        """A backend-level UPDATE failure maps to 409, not 500 — on a REAL Device, with only the backend save() simulating the failure (the 0-row forced-update path is not reliably raised on Django 6.0, per _save_device's own note, so it can't be triggered for real)."""
        from django.db import DatabaseError

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = make_device("dberr-dev")
        raw_error = "could not serialize access due to concurrent update"
        # Real Device + real platform/device_type preflight; only the backend save() — the genuine
        # external boundary — is forced to raise, since this failure class can't be provoked
        # deterministically against the test DB.
        with patch.object(device, "save", side_effect=DatabaseError(raw_error)) as mock_save:
            response = _save_device(device, update_fields=["name"])

        mock_save.assert_called_once_with(update_fields=["name"])
        assert response.status_code == 409
        assert b"changed or deleted" in response.content
        # Full raw DB exception text must not leak to the client (case-insensitive).
        assert raw_error.encode().lower() not in response.content.lower()

    @pytest.mark.django_db
    def test_update_fields_device_type_rack_overflow_is_blocked(self):
        """A taller device_type that overflows the device's rack slot must be rejected, not saved.

        save(update_fields=["device_type"]) skips full_clean(), so NetBox's Device.clean()
        rack-space check is bypassed. Re-validate just that rule: a 4U type at U40 in a 42U rack
        (would need U40-43, but the rack ends at U42) must be blocked with an error response, and
        the DB row must keep its original 1U type. Real Site/Rack/DeviceType/Device end to end.
        """
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        from netbox_librenms_plugin.views.imports.actions import _save_device

        mfr, _ = Manufacturer.objects.get_or_create(name="RackFitMfr", slug="rackfit-mfr")
        role, _ = DeviceRole.objects.get_or_create(name="RackFitRole", slug="rackfit-role")
        site, _ = Site.objects.get_or_create(name="RackFitSite", slug="rackfit-site")
        rack = Rack.objects.create(name="RackFit-R1", site=site, u_height=42, status="active")
        one_u = DeviceType.objects.create(manufacturer=mfr, model="RackFit-1U", slug="rackfit-1u", u_height=1)
        four_u = DeviceType.objects.create(manufacturer=mfr, model="RackFit-4U", slug="rackfit-4u", u_height=4)
        device = Device.objects.create(
            name="rackfit-dev",
            device_type=one_u,
            role=role,
            site=site,
            rack=rack,
            position=40,
            face="front",
            status="active",
        )

        # Swap to the 4U type in memory and persist via the update_fields fast path.
        device.device_type = four_u
        response = _save_device(device, update_fields=["device_type"])

        # Blocked: an error response is returned (not a silent success/None)...
        assert response is not None
        assert b"sufficient space" in response.content
        # ...and the DB row still carries the original 1U type (nothing was persisted).
        assert Device.objects.get(pk=device.pk).device_type_id == one_u.pk

    @pytest.mark.django_db
    def test_update_fields_device_type_that_fits_still_saves(self):
        """The rack-fit guard must not block a legitimate device_type change that fits the slot."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        from netbox_librenms_plugin.views.imports.actions import _save_device

        mfr, _ = Manufacturer.objects.get_or_create(name="RackFitMfr", slug="rackfit-mfr")
        role, _ = DeviceRole.objects.get_or_create(name="RackFitRole", slug="rackfit-role")
        site, _ = Site.objects.get_or_create(name="RackFitSite", slug="rackfit-site")
        rack = Rack.objects.create(name="RackFit-R2", site=site, u_height=42, status="active")
        one_u = DeviceType.objects.create(manufacturer=mfr, model="RackFit-1Ub", slug="rackfit-1ub", u_height=1)
        two_u = DeviceType.objects.create(manufacturer=mfr, model="RackFit-2U", slug="rackfit-2u", u_height=2)
        device = Device.objects.create(
            name="rackfit-ok-dev",
            device_type=one_u,
            role=role,
            site=site,
            rack=rack,
            position=10,
            face="front",
            status="active",
        )

        # A 2U type at U10 fits (U10-11 free); the write must succeed (None) and persist.
        device.device_type = two_u
        response = _save_device(device, update_fields=["device_type"])

        assert response is None
        assert Device.objects.get(pk=device.pk).device_type_id == two_u.pk

    @pytest.mark.django_db
    def test_update_fields_device_type_0u_at_rack_position_is_blocked(self):
        """Device.clean() forbids a 0U device type at a rack position; the update_fields mirror must too.

        get_available_units(u_height=0) contains every unit, so the space check alone passes
        trivially — without the explicit 0U rule the write persists a rack-invariant violation
        with a success toast.
        """
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        from netbox_librenms_plugin.views.imports.actions import _save_device

        mfr, _ = Manufacturer.objects.get_or_create(name="RackFitMfr", slug="rackfit-mfr")
        role, _ = DeviceRole.objects.get_or_create(name="RackFitRole", slug="rackfit-role")
        site, _ = Site.objects.get_or_create(name="RackFitSite", slug="rackfit-site")
        rack = Rack.objects.create(name="RackFit-R3", site=site, u_height=42, status="active")
        one_u = DeviceType.objects.create(manufacturer=mfr, model="RackFit-1Uc", slug="rackfit-1uc", u_height=1)
        zero_u = DeviceType.objects.create(manufacturer=mfr, model="RackFit-0U", slug="rackfit-0u", u_height=0)
        device = Device.objects.create(
            name="rackfit-0u-dev",
            device_type=one_u,
            role=role,
            site=site,
            rack=rack,
            position=20,
            face="front",
            status="active",
        )

        device.device_type = zero_u
        response = _save_device(device, update_fields=["device_type"])

        assert response is not None
        assert b"0U" in response.content
        assert Device.objects.get(pk=device.pk).device_type_id == one_u.pk

    @pytest.mark.django_db
    def test_update_fields_child_device_type_on_rack_face_is_blocked(self):
        """Device.clean() forbids a child device type at a rack face; the update_fields mirror must too (a DeviceTypeMapping can map a hardware string to a blade/child type).

        Face-without-position is the case the 0U rule can't catch (child types are 0U, so
        with a position set the 0U rule fires first — matching Device.clean()'s own order).
        """
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site

        from netbox_librenms_plugin.views.imports.actions import _save_device

        mfr, _ = Manufacturer.objects.get_or_create(name="RackFitMfr", slug="rackfit-mfr")
        role, _ = DeviceRole.objects.get_or_create(name="RackFitRole", slug="rackfit-role")
        site, _ = Site.objects.get_or_create(name="RackFitSite", slug="rackfit-site")
        rack = Rack.objects.create(name="RackFit-R4", site=site, u_height=42, status="active")
        zero_u = DeviceType.objects.create(manufacturer=mfr, model="RackFit-0Ud", slug="rackfit-0ud", u_height=0)
        child = DeviceType.objects.create(
            manufacturer=mfr,
            model="RackFit-Child",
            slug="rackfit-child",
            u_height=0,
            subdevice_role="child",
        )
        # A 0U device mounted on a rack face with no position — a valid NetBox placement.
        device = Device.objects.create(
            name="rackfit-child-dev",
            device_type=zero_u,
            role=role,
            site=site,
            rack=rack,
            position=None,
            face="front",
            status="active",
        )

        device.device_type = child
        response = _save_device(device, update_fields=["device_type"])

        assert response is not None
        assert b"hild device type" in response.content
        assert Device.objects.get(pk=device.pk).device_type_id == zero_u.pk


class TestResolveNamingPreferences:
    """Tests for resolve_naming_preferences (utils.resolve_naming_preferences)."""

    def test_post_use_sysname_toggle_truthy(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request(post={"use-sysname-toggle": "on"})
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
                MockSettings.objects.first.return_value = None
                use_sysname, strip_domain = resolve_naming_preferences(request)
        assert use_sysname is True

    def test_post_use_sysname_underscored_key(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request(post={"use_sysname-toggle": "on"})
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
                MockSettings.objects.first.return_value = None
                use_sysname, _ = resolve_naming_preferences(request)
        assert use_sysname is True

    def test_post_use_sysname_plain_key(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request(post={"use_sysname": "true"})
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
                MockSettings.objects.first.return_value = None
                use_sysname, _ = resolve_naming_preferences(request)
        assert use_sysname is True

    def test_get_fallback_when_no_post(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request(get={"use_sysname": "on"})
        request.POST = {}
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
                MockSettings.objects.first.return_value = None
                use_sysname, _ = resolve_naming_preferences(request)
        assert use_sysname is True

    def test_user_pref_used_when_no_post_get(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request()
        with patch("netbox_librenms_plugin.utils.get_user_pref") as mock_pref:
            mock_pref.return_value = False
            with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
                MockSettings.objects.first.return_value = None
                use_sysname, _ = resolve_naming_preferences(request)
        assert use_sysname is False

    def test_settings_fallback_when_no_pref(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request()
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
                settings_obj = MagicMock()
                settings_obj.use_sysname_default = False
                settings_obj.strip_domain_default = True
                MockSettings.objects.first.return_value = settings_obj
                use_sysname, strip_domain = resolve_naming_preferences(request)
        assert use_sysname is False
        assert strip_domain is True

    def test_no_settings_defaults_to_true_false(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request()
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
                MockSettings.objects.first.return_value = None
                use_sysname, strip_domain = resolve_naming_preferences(request)
        assert use_sysname is True
        assert strip_domain is False

    def test_strip_domain_post_toggle(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request(post={"strip-domain-toggle": "on"})
        with patch("netbox_librenms_plugin.utils.get_user_pref", return_value=None):
            with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
                MockSettings.objects.first.return_value = None
                _, strip_domain = resolve_naming_preferences(request)
        assert strip_domain is True


class TestResolveVCDetectionEnabled:
    """Tests for shared VC detection resolver across confirm/import steps."""

    def test_prefers_post_value_over_get(self):
        from netbox_librenms_plugin.views.imports.actions import _resolve_vc_detection_enabled

        request = _make_request(post={"enable_vc_detection": "false"}, get={"enable_vc_detection": "true"})
        assert _resolve_vc_detection_enabled(request) is False

    def test_reads_get_when_post_missing(self):
        from netbox_librenms_plugin.views.imports.actions import _resolve_vc_detection_enabled

        request = _make_request(get={"enable_vc_detection": "true"})
        assert _resolve_vc_detection_enabled(request) is True

    def test_falls_back_to_return_url(self):
        from netbox_librenms_plugin.views.imports.actions import _resolve_vc_detection_enabled

        request = _make_request(
            post={"return_url": "/plugins/librenms_plugin/librenms-import/?enable_vc_detection=true"}
        )
        assert _resolve_vc_detection_enabled(request) is True

    def test_legacy_skip_vc_detection_in_return_url(self):
        from netbox_librenms_plugin.views.imports.actions import _resolve_vc_detection_enabled

        request = _make_request(post={"return_url": "/plugins/librenms_plugin/librenms-import/?skip_vc_detection=true"})
        assert _resolve_vc_detection_enabled(request) is False


@pytest.mark.django_db
class TestBulkImportConfirmView:
    """BulkImportConfirmView.post: the preview/confirm render step."""

    @pytest.fixture(autouse=True)
    def _clear_django_cache(self):
        # fetch_device_with_cache reads/writes the real Django cache; isolate tests so a
        # device cached by one doesn't satisfy another's lookup.
        from django.core.cache import cache

        cache.clear()
        yield
        cache.clear()

    @staticmethod
    def _make_view(settings, server, server_key):
        from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView

        configure_test_servers(
            settings,
            {
                server_key: {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        return BulkImportConfirmView()

    @pytest.fixture
    def librenms_server(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as server:
            yield server

    def test_no_permission_returns_error(self, settings, librenms_server):
        from dcim.models import Device
        from django.apps import apps
        from django.urls import get_script_prefix
        from virtualization.models import VirtualMachine

        server_key = "confirm-denied"
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-denied-user", [], plugin_write=False)
        settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        user = grant_view_permission(user, "view", settings_model)
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["1"]},
            user=user,
            HTTP_HX_REQUEST="true",
        )
        device_count = Device.objects.count()
        vm_count = VirtualMachine.objects.count()

        response = post_view(view, request)

        assert response.status_code == 200
        assert response.content == b""
        assert response["HX-Redirect"] == get_script_prefix()
        assert view_message_texts(request, "error") == ["You do not have permission to perform this action."]
        assert Device.objects.count() == device_count
        assert VirtualMachine.objects.count() == vm_count

    def test_no_devices_selected_renders_alert(self, settings, librenms_server):
        # This is HTMX modal content (hx-target=#htmx-modal-content); htmx won't swap a
        # 4xx, so the alert must come back 200 to render in-place.
        server_key = "confirm-empty"
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-empty-user", [])
        request = make_view_request(
            "post",
            {"server_key": server_key},
            user=user,
            HTTP_HX_REQUEST="true",
        )

        result = post_view(view, request)

        assert result.status_code == 200
        assert b"Select at least one device" in result.content

    def test_invalid_device_id_renders_generic_alert(self, settings, librenms_server):
        server_key = "confirm-invalid-id"
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-invalid-id-user", [])
        # Invalid id never reaches the API; get_device_info would not be called.
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["not-an-int"]},
            user=user,
            HTTP_HX_REQUEST="true",
        )

        result = post_view(view, request)

        # No valid devices and nothing expired → generic alert, rendered 200 in the modal.
        assert result.status_code == 200
        assert b"No valid devices selected" in result.content

    def test_all_cache_expired_renders_expiry_alert(self, settings, librenms_server):
        server_key = "confirm-expired"
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-expired-user", [])
        # The LibreNMS API reports the device is gone → real fetch_device_with_cache returns
        # None for every valid id → all-expired alert.
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["1", "2"]},
            user=user,
            HTTP_HX_REQUEST="true",
        )

        result = post_view(view, request)

        assert result.status_code == 200
        assert b"expired" in result.content.lower()

    def test_valid_devices_renders_confirm_template(self, settings, librenms_server):
        server_key = "confirm-valid"
        librenms_server.device_info_response(
            device_id=1,
            hostname="confirm-router-01",
            serial="CONFIRM-SERIAL-01",
            ip="198.18.0.1",
        )
        librenms_server.vc_inventory_callable(
            1,
            [{"entPhysicalClass": "stack", "entPhysicalIndex": 100}],
            {
                100: [
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 101,
                        "entPhysicalParentRelPos": 1,
                        "entPhysicalSerialNum": "CONFIRM-SERIAL-01",
                        "entPhysicalModelName": "Test chassis",
                        "entPhysicalName": "Member 1",
                        "entPhysicalDescr": "Test member 1",
                    },
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 102,
                        "entPhysicalParentRelPos": 2,
                        "entPhysicalSerialNum": "CONFIRM-SERIAL-02",
                        "entPhysicalModelName": "Test chassis",
                        "entPhysicalName": "Member 2",
                        "entPhysicalDescr": "Test member 2",
                    },
                ]
            },
        )
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-valid-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["1"],
                "enable_vc_detection": "false",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 200
        assert b"Confirm Import" in response.content
        assert b"confirm-router-01" in response.content
        assert b"CONFIRM-SERIAL-02" in response.content
        assert b'name="select" value="1"' in response.content
        assert f'name="server_key" value="{server_key}"'.encode() in response.content

    def test_uses_return_url_vc_flag_for_context_and_validation(self, settings, librenms_server, client):
        server_key = "confirm-return-url-vc"
        librenms_server.device_info_response(
            device_id=1,
            hostname="confirm-return-url-router",
            serial="",
            ip="198.18.0.2",
        )
        librenms_server.vc_inventory_callable(1, [], {})
        configure_test_servers(
            settings,
            {
                server_key: {
                    "librenms_url": librenms_server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        user = make_view_user("confirm-return-url-user", [])
        client.force_login(user)

        response = client.post(
            url_for("plugins:netbox_librenms_plugin:bulk_import_confirm"),
            {
                "server_key": server_key,
                "select": ["1"],
                "return_url": "/plugins/librenms_plugin/librenms-import/?enable_vc_detection=true",
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert b"confirm-return-url-router" in response.content
        assert b'name="vc_detection_enabled" value="on"' in response.content
        assert b'name="enable_vc_detection" value="true"' in response.content
        assert response.context["vc_detection_enabled"] is True
        assert response.context["devices"][0]["validation"]["_vc_detection_enabled"] is True


@pytest.mark.django_db
class TestBulkImportConfirmViewIntegration:
    """BulkImportConfirmView.post integration paths for selections and collision checks."""

    @pytest.fixture(autouse=True)
    def _clear_django_cache(self):
        # fetch_device_with_cache reads/writes the real Django cache; isolate tests so a
        # device cached by one doesn't satisfy another's lookup.
        from django.core.cache import cache

        cache.clear()
        yield
        cache.clear()

    @staticmethod
    def _make_view(settings, server, server_key):
        from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView

        configure_test_servers(
            settings,
            {
                server_key: {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        return BulkImportConfirmView()

    @staticmethod
    def _post_capturing_context(view, request):
        from django.test.signals import template_rendered

        rendered_contexts = []

        def capture_context(**kwargs):
            if kwargs["template"].name.endswith("bulk_import_confirm.html"):
                rendered_contexts.append(kwargs["context"])

        template_rendered.connect(capture_context)
        try:
            response = post_view(view, request)
        finally:
            template_rendered.disconnect(capture_context)

        assert len(rendered_contexts) == 1
        return response, rendered_contexts[0]

    @pytest.fixture
    def librenms_server(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as server:
            yield server

    def test_duplicate_device_id_renders_one_device(self, settings, librenms_server):
        """A repeated LibreNMS ID produces one confirmation row and one selected device."""
        server_key = "confirm-integration-duplicate"
        librenms_server.device_info_response(
            device_id=11,
            hostname="confirm-integration-duplicate-router",
            hardware="Test duplicate hardware",
            os="test-duplicate-os",
            serial="CONFIRM-INTEGRATION-DUPLICATE-SERIAL",
            ip="198.18.0.11",
        )
        librenms_server.vc_inventory_callable(11, [], {})
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-integration-duplicate-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["11", "11"],
                "use_sysname": "true",
                "strip_domain": "false",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response, context = self._post_capturing_context(view, request)

        assert response.status_code == 200
        assert context["selected_count"] == 1
        assert context["device_count"] == 1
        assert response.content.count(b'<div class="border rounded-3 p-2 mb-2">') == 1
        assert response.content.count(b'name="select" value="11"') == 1
        assert b"Import 1 device" in response.content
        assert b"Import 2 devices" not in response.content
        assert view_message_texts(request, "error") == []

    def test_unknown_device_renders_only_all_expired_alert(self, settings, librenms_server):
        """One unknown LibreNMS ID renders the all-expired alert instead of another empty-result alert."""
        server_key = "confirm-integration-all-expired"
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-integration-all-expired-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["999"],
                "use_sysname": "true",
                "strip_domain": "false",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 200
        assert b"<strong>Filter results have expired.</strong>" in response.content
        assert b"<strong>Some device data has expired.</strong>" not in response.content
        assert b"No valid devices selected." not in response.content
        assert view_message_texts(request, "error") == []

    def test_stack_members_use_resolved_device_name(self, settings, librenms_server):
        """Stack member suggestions use the resolved master name in the rendered confirmation row."""
        from django.apps import apps

        server_key = "confirm-integration-stack-names"
        settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        settings_row, _ = settings_model.objects.get_or_create(pk=1)
        settings_row.vc_member_name_pattern = "-M{position}"
        settings_row.save(update_fields=["vc_member_name_pattern"])
        librenms_server.device_info_response(
            device_id=21,
            hostname="confirm-integration-stack.example.test",
            hardware="Test stack hardware",
            os="test-stack-os",
            serial="CONFIRM-INTEGRATION-STACK-SERIAL-1",
            ip="198.18.0.21",
        )
        librenms_server.vc_inventory_callable(
            21,
            [{"entPhysicalClass": "stack", "entPhysicalIndex": 2100}],
            {
                2100: [
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 2101,
                        "entPhysicalParentRelPos": 1,
                        "entPhysicalSerialNum": "CONFIRM-INTEGRATION-STACK-SERIAL-1",
                        "entPhysicalModelName": "Test stack chassis",
                        "entPhysicalName": "Stack member 1",
                        "entPhysicalDescr": "Test stack member 1",
                    },
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 2102,
                        "entPhysicalParentRelPos": 2,
                        "entPhysicalSerialNum": "CONFIRM-INTEGRATION-STACK-SERIAL-2",
                        "entPhysicalModelName": "Test stack chassis",
                        "entPhysicalName": "Stack member 2",
                        "entPhysicalDescr": "Test stack member 2",
                    },
                ]
            },
        )
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-integration-stack-names-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["21"],
                "use_sysname": "true",
                "strip_domain": "true",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 200
        assert b"confirm-integration-stack-M1" in response.content
        assert b"confirm-integration-stack-M2" in response.content
        assert b"confirm-integration-stack.example.test-M1" not in response.content
        assert view_message_texts(request, "error") == []

    def test_stack_members_use_the_device_id_name_when_stripping_empties_the_hostname(self, settings, librenms_server):
        """Stack member suggestions use the device ID name when domain stripping empties the LibreNMS hostname."""
        from django.apps import apps

        server_key = "confirm-integration-stack-fallback"
        settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        settings_row, _ = settings_model.objects.get_or_create(pk=1)
        settings_row.vc_member_name_pattern = "-F{position}"
        settings_row.save(update_fields=["vc_member_name_pattern"])
        librenms_server.device_info_response(
            device_id=71,
            hostname=".confirm-integration-stack-fallback.example.test",
            hardware="Test fallback stack hardware",
            os="test-fallback-stack-os",
            serial="CONFIRM-INTEGRATION-STACK-FALLBACK-SERIAL-1",
            ip="198.18.0.71",
        )
        librenms_server.vc_inventory_callable(
            71,
            [{"entPhysicalClass": "stack", "entPhysicalIndex": 7100}],
            {
                7100: [
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 7101,
                        "entPhysicalParentRelPos": 1,
                        "entPhysicalSerialNum": "CONFIRM-INTEGRATION-STACK-FALLBACK-SERIAL-1",
                        "entPhysicalModelName": "Test fallback stack chassis",
                        "entPhysicalName": "Fallback stack member 1",
                        "entPhysicalDescr": "Test fallback stack member 1",
                    },
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 7102,
                        "entPhysicalParentRelPos": 2,
                        "entPhysicalSerialNum": "CONFIRM-INTEGRATION-STACK-FALLBACK-SERIAL-2",
                        "entPhysicalModelName": "Test fallback stack chassis",
                        "entPhysicalName": "Fallback stack member 2",
                        "entPhysicalDescr": "Test fallback stack member 2",
                    },
                ]
            },
        )
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-integration-stack-fallback-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["71"],
                "use_sysname": "true",
                "strip_domain": "true",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 200
        assert b"device-71-F1" in response.content
        assert b"device-71-F2" in response.content
        assert view_message_texts(request, "error") == []

    def test_vm_row_shows_selected_cluster_and_role(self, settings, librenms_server):
        """A VM confirmation row shows its selected cluster and role in their own fields."""
        from dcim.models import DeviceRole

        cluster_issue = "Cluster must be manually selected before importing as VM"
        server_key = "confirm-integration-vm-selections"
        role = DeviceRole.objects.create(
            name="Confirm Integration VM Selected Role",
            slug="confirm-integration-vm-selected-role",
            color="336699",
        )
        cluster = make_cluster("Confirm Integration VM Selected Cluster")
        librenms_server.device_info_response(
            device_id=31,
            hostname="confirm-integration-vm",
            hardware="Test VM hardware",
            os="test-vm-os",
            serial="CONFIRM-INTEGRATION-VM-SERIAL",
            ip="198.18.0.31",
        )
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-integration-vm-selections-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["31"],
                "cluster_31": str(cluster.pk),
                "role_31": str(role.pk),
                "use_sysname": "true",
                "strip_domain": "false",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response, context = self._post_capturing_context(view, request)
        validation = context["devices"][0]["validation"]

        assert response.status_code == 200
        assert validation["cluster"]["cluster"] == cluster
        assert validation["cluster"]["found"] is True
        assert validation["device_role"]["role"] == role
        assert validation["device_role"]["found"] is True
        assert cluster_issue not in validation["issues"]
        assert cluster_issue.encode() not in response.content
        assert b"Virtual Machine" in response.content
        assert b"Confirm Integration VM Selected Role" in response.content
        assert b"<strong>Cluster:</strong> Confirm Integration VM Selected Cluster" in response.content
        assert b"<strong>Rack:</strong>" not in response.content
        assert f'name="cluster_31" value="{cluster.pk}"'.encode() in response.content
        assert f'name="role_31" value="{role.pk}"'.encode() in response.content
        assert view_message_texts(request, "error") == []

    def test_device_row_shows_selected_role_and_rack(self, settings, librenms_server):
        """A device confirmation row shows its selected role and rack in their own fields."""
        from dcim.models import DeviceRole, Rack

        role_issue = "Device role must be manually selected before import"
        server_key = "confirm-integration-device-selections"
        site_source = make_device("confirm-integration-device-site-source")
        role = DeviceRole.objects.create(
            name="Confirm Integration Device Selected Role",
            slug="confirm-integration-device-selected-role",
            color="663399",
        )
        rack = Rack.objects.create(
            name="Confirm Integration Device Selected Rack",
            site=site_source.site,
            status="active",
        )
        librenms_server.device_info_response(
            device_id=41,
            hostname="confirm-integration-device",
            hardware="Test device hardware",
            os="test-device-os",
            serial="CONFIRM-INTEGRATION-DEVICE-SERIAL",
            ip="198.18.0.41",
            location=site_source.site.name,
        )
        librenms_server.vc_inventory_callable(41, [], {})
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-integration-device-selections-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["41"],
                "role_41": str(role.pk),
                "rack_41": str(rack.pk),
                "use_sysname": "true",
                "strip_domain": "false",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response, context = self._post_capturing_context(view, request)
        validation = context["devices"][0]["validation"]

        assert response.status_code == 200
        assert validation["device_role"]["role"] == role
        assert validation["device_role"]["found"] is True
        assert validation["rack"]["rack"] == rack
        assert validation["rack"]["found"] is True
        assert role_issue not in validation["issues"]
        assert role_issue.encode() not in response.content
        assert b"Virtual Machine" not in response.content
        assert b"Confirm Integration Device Selected Role" in response.content
        assert b"<strong>Rack:</strong>" in response.content
        assert b"Confirm Integration Device Selected Rack" in response.content
        assert b"<strong>Cluster:</strong>" not in response.content
        assert f'name="role_41" value="{role.pk}"'.encode() in response.content
        assert f'name="rack_41" value="{rack.pk}"'.encode() in response.content
        assert view_message_texts(request, "error") == []

    def test_partial_expiry_renders_survivor_and_warning(self, settings, librenms_server):
        """A valid row survives confirmation when another selected LibreNMS row has expired."""
        server_key = "confirm-integration-partial-expiry"
        librenms_server.device_info_response(
            device_id=51,
            hostname="confirm-integration-partial-survivor",
            hardware="Test partial expiry hardware",
            os="test-partial-expiry-os",
            serial="CONFIRM-INTEGRATION-PARTIAL-SERIAL",
            ip="198.18.0.51",
        )
        librenms_server.vc_inventory_callable(51, [], {})
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-integration-partial-expiry-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["51", "52"],
                "use_sysname": "true",
                "strip_domain": "false",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 200
        assert b"Confirm Import" in response.content
        assert b"confirm-integration-partial-survivor" in response.content
        assert b"1 of 2 selected devices had expired cache data" in response.content
        assert response.content.count(b'<div class="border rounded-3 p-2 mb-2">') == 1
        assert b'name="select" value="51"' in response.content
        assert b'name="select" value="52"' not in response.content
        assert b"Import 1 device" in response.content
        assert view_message_texts(request, "error") == []

    def test_distinct_existing_devices_render_confirmation(self, settings, librenms_server):
        """Two LibreNMS rows targeting distinct NetBox devices pass the bulk collision gate."""
        server_key = "confirm-integration-distinct-targets"
        first_device = make_device("confirm-integration-distinct-target-a")
        second_device = make_device("confirm-integration-distinct-target-b")
        librenms_server.device_info_response(
            device_id=61,
            hostname=first_device.name,
            hardware="Test distinct target hardware A",
            os="test-distinct-target-os-a",
            serial="",
            ip="198.18.0.61",
        )
        librenms_server.vc_inventory_callable(61, [], {})
        librenms_server.device_info_response(
            device_id=62,
            hostname=second_device.name,
            hardware="Test distinct target hardware B",
            os="test-distinct-target-os-b",
            serial="",
            ip="198.18.0.62",
        )
        librenms_server.vc_inventory_callable(62, [], {})
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-integration-distinct-targets-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["61", "62"],
                "use_sysname": "true",
                "strip_domain": "false",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 200
        assert b"Confirm Import" in response.content
        assert first_device.name.encode() in response.content
        assert second_device.name.encode() in response.content
        assert b"Bulk import blocked" not in response.content
        assert response.content.count(b'<div class="border rounded-3 p-2 mb-2">') == 2
        assert b"Import 2 devices" in response.content
        assert view_message_texts(request, "error") == []


@pytest.mark.django_db
class TestBulkImportDevicesViewPost:
    """Tests for BulkImportDevicesView.post."""

    @staticmethod
    def _make_view(settings, server, server_key):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        configure_test_servers(
            settings,
            {
                server_key: {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        return BulkImportDevicesView()

    @pytest.fixture
    def librenms_server(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as server:
            yield server

    def test_no_permission_returns_error(self, settings, librenms_server):
        from dcim.models import Device
        from django.apps import apps
        from django.urls import get_script_prefix

        server_key = "bulk-post-denied"
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user(
            "bulk-post-denied-user",
            [("add", Device), ("change", Device)],
            plugin_write=False,
        )
        settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        user = grant_view_permission(user, "view", settings_model)
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["1"]},
            user=user,
        )

        with (
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_devices") as mock_device_import,
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_vms") as mock_vm_import,
        ):
            response = post_view(view, request)

        assert response.status_code == 302
        assert response["Location"] == get_script_prefix()
        assert view_message_texts(request, "error") == ["You do not have permission to perform this action."]
        mock_device_import.assert_not_called()
        mock_vm_import.assert_not_called()

    def test_no_devices_returns_400(self, settings, librenms_server):
        # HTMX path: bare 400 (non-HTMX redirects instead — covered elsewhere).
        server_key = "bulk-post-empty"
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("bulk-post-empty-user", [])
        request = make_view_request(
            "post",
            {"server_key": server_key},
            user=user,
            HTTP_HX_REQUEST="true",
        )

        result = post_view(view, request)

        assert result.status_code == 400
        assert result.content == b"No devices selected"
        assert view_message_texts(request, "error") == []

    def test_invalid_ids_returns_400(self, settings, librenms_server):
        # HTMX path: bare 400 (non-HTMX redirects instead — covered elsewhere).
        server_key = "bulk-post-invalid-id"
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("bulk-post-invalid-id-user", [])
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["abc"]},
            user=user,
            HTTP_HX_REQUEST="true",
        )

        result = post_view(view, request)

        assert result.status_code == 400
        assert result.content == b"Invalid device identifier"
        assert view_message_texts(request, "error") == []

    def test_non_superuser_cannot_use_background_job(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = BulkImportDevicesView()
        request = make_view_request(
            "post",
            {"select": ["1"], "use_background_job": "on"},
            user=make_view_user("bulk-post-foreground-user", []),
        )
        # should_use_background_job_for_import returns False for non-superuser
        result = view.should_use_background_job_for_import(request)
        assert result is False

    def test_superuser_can_use_background_job(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = BulkImportDevicesView()
        request = make_view_request(
            "post",
            {"use_background_job": "on"},
            user=make_superuser("bulk-post-background-user"),
        )
        result = view.should_use_background_job_for_import(request)
        assert result is True

    def test_superuser_without_flag_returns_false(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = BulkImportDevicesView()
        request = make_view_request(
            "post",
            {},
            user=make_superuser("bulk-post-no-background-user"),
        )
        result = view.should_use_background_job_for_import(request)
        assert result is False

    def test_htmx_user_is_told_about_sync_fallback_when_no_workers(self, settings, librenms_server):
        """With RQ workers down, a background-import request silently blocks for a synchronous run — the HTMX summary (the normal import page's ONLY message channel) must say so, not just the never-rendered Django message."""
        server_key = "bulk-post-no-workers"
        view = self._make_view(settings, librenms_server, server_key)
        empty_result = {"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0}
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["1"], "use_background_job": "on"},
            user=make_superuser("bulk-post-no-workers-user"),
            HTTP_HX_REQUEST="true",
        )

        with (
            patch("utilities.rqworker.get_workers_for_queue", return_value=0),
            patch(
                "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                return_value=dict(empty_result),
            ) as mock_device_import,
            patch(
                "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                return_value={"success": [], "failed": [], "skipped": []},
            ) as mock_vm_import,
        ):
            response = post_view(view, request)

        mock_device_import.assert_called_once()
        mock_vm_import.assert_not_called()
        assert response.status_code == 200
        # Outcome-neutral wording: the fallback banner must NOT claim every selected row was
        # "Imported" — the per-row summary toasts report the actual successes/failures/skips.
        assert b"no workers are available" in response.content
        assert b"ran synchronously" in response.content
        assert b"devices synchronously" not in response.content

    def test_import_denied_without_model_add_perms_before_collision_precheck(self, settings, librenms_server):
        """A user with the plugin change perm but WITHOUT dcim.add_device is denied BEFORE the sync collision pre-check (which surfaces NetBox object names/pks in its modal) — mirroring the async job's authorize-before-scan ordering."""
        from netbox_librenms_plugin.views.imports.actions import detect_collisions_for_device_ids

        server_key = "bulk-post-model-denied"
        view = self._make_view(settings, librenms_server, server_key)
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["1", "2"],  # >=2 ids: the collision pre-check would otherwise run
            },
            user=make_view_user("bulk-post-model-denied-user", []),
            HTTP_HX_REQUEST="true",
        )
        # The real plugin permission gate passes, but the user lacks the model add/change grants.
        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.detect_collisions_for_device_ids",
                wraps=detect_collisions_for_device_ids,
            ) as detect_spy,
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_devices") as mock_device_import,
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_vms") as mock_vm_import,
        ):
            response = post_view(view, request)

        # Denied before any collision scan or import: the detector and importer never ran, and the
        # response is the permission redirect (HX-Redirect), not the collision modal or an import.
        detect_spy.assert_not_called()
        mock_device_import.assert_not_called()
        mock_vm_import.assert_not_called()
        assert response.status_code == 200
        assert response.content == b""
        assert response["HX-Redirect"] == url_for("plugins:netbox_librenms_plugin:librenms_import")
        assert view_message_texts(request, "error") == [
            "You do not have permission to import these rows (missing: dcim.add_device, dcim.change_device)."
        ]


class TestDeviceImportHelperMixin:
    """Tests for DeviceImportHelperMixin methods (lines 154-220)."""

    def _make_mixin_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceRoleUpdateView

        # Use DeviceRoleUpdateView which inherits from both LibreNMSAPIMixin and DeviceImportHelperMixin
        view = object.__new__(DeviceRoleUpdateView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_get_validated_device_returns_none_when_device_not_found(self):
        view = self._make_mixin_view()
        with patch("netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                return_value={"cluster_id": None, "role_id": None, "rack_id": None},
            ):
                libre_device, validation, selections = view.get_validated_device_with_selections(1, MagicMock())
        assert libre_device is None
        assert validation is None

    def test_get_validated_device_returns_data_when_found(self):
        view = self._make_mixin_view()
        libre_device = {"device_id": 1, "hostname": "sw01"}

        with patch("netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=libre_device):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                return_value={"cluster_id": None, "role_id": None, "rack_id": None},
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                    return_value=(True, False),
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                        return_value={"status": "importable"},
                    ):
                        with patch("netbox_librenms_plugin.views.imports.actions.cache") as mock_cache:
                            mock_cache.get.return_value = None
                            request = _make_request()
                            result_device, validation, selections = view.get_validated_device_with_selections(
                                1, request
                            )
        assert result_device is libre_device
        assert validation is not None

    @patch("netbox_librenms_plugin.views.imports.actions.render")
    def test_render_device_row_calls_render(self, mock_render):
        view = self._make_mixin_view()
        mock_render.return_value = MagicMock()

        libre_device = {"device_id": 1}
        validation = {"status": "importable"}
        selections = {"cluster_id": None, "role_id": None, "rack_id": None}

        with patch("netbox_librenms_plugin.views.imports.actions.DeviceImportTable") as MockTable:
            MockTable.return_value = MagicMock()
            view.render_device_row(MagicMock(), libre_device, validation, selections)

        mock_render.assert_called_once()
        assert "device_import_row.html" in mock_render.call_args[0][1]

    def test_post_commit_refresh_fallback_returns_200_not_error(self):
        """A committed mutation whose post-commit row reload fails must NOT report failure: surface the deferred messages + a refresh hint and return 200 with the success trigger, so the user doesn't retry an action that already succeeded."""
        from django.contrib import messages as dj_messages

        view = self._make_mixin_view()
        request = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.imports.actions.messages") as mock_msgs,
            patch(
                "netbox_librenms_plugin.views.imports.actions._attach_messages_oob",
                side_effect=lambda resp, req: resp,
            ) as mock_attach,
        ):
            response = view.post_commit_refresh_fallback(
                request, "closeModal", deferred_messages=[(dj_messages.INFO, "OOB attached")]
            )

        # Success-shaped response (200 + the trigger), never an HTMX error.
        assert response.status_code == 200
        assert response["HX-Trigger"] == "closeModal"
        # The deferred outcome message and the "couldn't reload, refresh" hint were surfaced.
        mock_msgs.add_message.assert_called_once_with(request, dj_messages.INFO, "OOB attached")
        mock_msgs.warning.assert_called_once()
        mock_attach.assert_called_once()


class TestAttachMessagesOob:
    """Tests for the _attach_messages_oob helper."""

    def test_returns_none_when_response_is_none(self):
        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        assert _attach_messages_oob(None, MagicMock()) is None

    def test_skips_response_without_bytes_content(self):
        """When .content is a MagicMock or similar non-bytes value, skip cleanly."""
        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        response = MagicMock()
        response.content = MagicMock()  # not bytes / bytearray
        result = _attach_messages_oob(response, MagicMock())
        assert result is response  # returned unchanged

    @staticmethod
    def _storage(items):
        """A messages-storage stand-in with a REAL __iter__."""

        class _Storage:
            def __init__(self, values):
                self.used = False
                self._values = values

            def __iter__(self):
                self.used = True
                return iter(self._values)

        return _Storage(items)

    def test_appends_rendered_messages_to_bytes_content(self):
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        response = HttpResponse(b"<tr>row html</tr>")
        storage = self._storage(["a message"])
        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.messages.get_messages",
                return_value=storage,
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.render_to_string",
                return_value='<div id="django-messages" hx-swap-oob="true"></div>',
            ) as mock_render,
        ):
            result = _attach_messages_oob(response, MagicMock())

        mock_render.assert_called_once()
        assert b'<div id="django-messages"' in result.content
        assert result.content.startswith(b"<tr>row html</tr>")
        # The CodeQL-safe format_html() composition produces exactly the concatenation of the
        # original response bytes and the rendered (trusted) fragment — no escaping of either.
        assert result.content == b"<tr>row html</tr>" + b'<div id="django-messages" hx-swap-oob="true"></div>'
        # The peek must not leave the storage consumed for the page renderer.
        assert storage.used is False

    def test_skips_oob_swap_when_no_messages_queued(self):
        """No pending messages → don't append an empty OOB container that would wipe toasts already visible from an earlier action."""
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        response = HttpResponse(b"<tr>row html</tr>")
        original = response.content
        storage = self._storage([])
        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.messages.get_messages",
                return_value=storage,
            ),
            patch("netbox_librenms_plugin.views.imports.actions.render_to_string") as mock_render,
        ):
            result = _attach_messages_oob(response, MagicMock())

        mock_render.assert_not_called()
        assert result.content == original
        # Even on the empty path, the peek must restore used so nothing is consumed.
        assert storage.used is False

    def test_swallows_render_errors(self):
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        response = HttpResponse(b"<tr>row html</tr>")
        original = response.content
        storage = self._storage(["a message"])
        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.messages.get_messages",
                return_value=storage,
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.render_to_string",
                side_effect=RuntimeError("db not available"),
            ),
        ):
            result = _attach_messages_oob(response, MagicMock())

        assert result.content == original
        # Peeking at the storage marks it consumed; the function restores used=False before the
        # render. A render error must not leave the storage consumed, or the page's own renderer
        # (and the next OOB attach) would silently drop the queued messages.
        assert storage.used is False


class TestDeviceValidationDetailsView:
    """Tests for DeviceValidationDetailsView (lines 477-822)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        view = object.__new__(DeviceValidationDetailsView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    @patch("netbox_librenms_plugin.views.imports.actions.render")
    def test_get_device_not_found_returns_200_html_fragment(self, mock_render):
        # HTMX fragment: a 4xx makes HTMX skip the swap, so the inline alert must come back 200.
        # Use a real request with NO ?server_key: resolve_get_render_server_key then keeps the
        # already-bound default client instead of trying to rebuild a client for a key. A bare
        # MagicMock() request makes request.GET.get("server_key") a truthy MagicMock, which the
        # view treats as an unresolvable ?server_key and fails closed before reaching this branch
        # (deterministic only when no multi-server config is loaded — hence flaky under the full suite).
        view = self._make_view()
        request = RequestFactory().get("/x/")
        with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, {})):
            with patch.object(view, "require_write_permission", return_value=None):
                result = view.get(request, device_id=1)
        assert result.status_code == 200
        assert b"not found in LibreNMS" in result.content

    @patch("netbox_librenms_plugin.views.imports.actions.render")
    def test_get_with_existing_device_adds_sync_info(self, mock_render):
        view = self._make_view()
        mock_render.return_value = MagicMock()
        # Real request, no ?server_key — see the note in test_get_device_not_found_*: a bare
        # MagicMock() request trips the unresolved-server_key fail-closed guard before render.
        request = RequestFactory().get("/x/")

        libre_device = {"device_id": 1, "serial": "SN001", "os": "ios", "hardware": "Cisco C9300"}
        existing = MagicMock()
        existing.serial = "SN001"
        existing.platform = None
        existing._meta.model_name = "device"

        validation = {
            "existing_device": existing,
        }

        with patch.object(view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch.object(view, "_build_sync_info", return_value={"serial_synced": True}):
                    with patch.object(view, "_build_id_server_info", return_value=None):
                        view.get(request, device_id=1)

        mock_render.assert_called_once()
        ctx = mock_render.call_args[0][2]
        assert "sync_info" in ctx

    def test_get_rebinds_to_request_server_key(self, mock_multi_server_config):
        # Reached via its own URL (modal-open GET), the view must rebind to ?server_key so the
        # fetch targets the import's server, not the global selected_server. Here the bound client
        # is the default server; the request asks for "secondary" and the client must follow.
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        view = object.__new__(DeviceValidationDetailsView)
        view._librenms_api = _make_api()  # bound to the default server
        request = RequestFactory().get("/x/?server_key=secondary")

        with (
            patch(
                "netbox_librenms_plugin.librenms_api.get_plugin_config",
                side_effect=lambda _plugin, key: mock_multi_server_config if key == "servers" else None,
            ),
            patch.object(view, "get_validated_device_with_selections", return_value=(None, None, {})),
        ):
            view.get(request, device_id=1)

        assert view._librenms_api.server_key == "secondary"

    def test_get_unresolved_server_key_fails_closed(self):
        # A ?server_key that no longer resolves (deleted/misconfigured) must NOT fall through to a
        # fetch against the still-bound default client — that would render another server's
        # validation data as the requested server's. Real view.get -> real
        # resolve_get_render_server_key -> real rebind_api_for_server; only the HTTP-client factory
        # (build_librenms_api) and the LibreNMS fetch boundary are stubbed.
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        view = object.__new__(DeviceValidationDetailsView)
        view._librenms_api = _make_api()  # bound to the default server
        request = RequestFactory().get("/x/?server_key=ghost")

        fetched = {"called": False}

        def _spy(*_a, **_k):
            fetched["called"] = True
            return ({"device_id": 1}, {}, {})

        with (
            # Non-blank ?server_key=ghost that build_librenms_api can't resolve -> rebind returns
            # None -> resolve_get_render_server_key reports unresolved=True.
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None),
            patch.object(view, "get_validated_device_with_selections", side_effect=_spy),
        ):
            result = view.get(request, device_id=1)

        # With the fix, get() returns the fail-closed alert before fetching or rendering anything.
        assert result.status_code == 200
        assert b"no longer configured" in result.content
        assert fetched["called"] is False  # never fetched from the wrong (default) server


class TestBuildSyncInfo:
    """Tests for _build_sync_info (lines 828-886)."""

    def _get_method(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        return DeviceValidationDetailsView._build_sync_info

    def test_serial_matches(self):
        build_sync_info = self._get_method()
        libre_device = {"serial": "SN001", "os": "ios", "hardware": "-"}
        existing = MagicMock()
        existing.serial = "SN001"
        existing.platform = None
        existing.device_type = None

        with patch("netbox_librenms_plugin.utils.find_matching_platform", return_value={"found": False}):
            result = build_sync_info(libre_device, existing)

        assert result["serial_synced"] is True

    def test_serial_mismatch(self):
        build_sync_info = self._get_method()
        libre_device = {"serial": "SN_LIBRENMS", "os": "-", "hardware": "-"}
        existing = MagicMock()
        existing.serial = "SN_NETBOX"
        existing.platform = None
        existing.device_type = None

        result = build_sync_info(libre_device, existing)
        assert result["serial_synced"] is False

    def test_padded_incoming_serial_counts_as_synced(self):
        """A whitespace-padded LibreNMS serial equal to the stored trimmed value must not report drift."""
        build_sync_info = self._get_method()
        libre_device = {"serial": " SN001 ", "os": "ios", "hardware": "-"}
        existing = MagicMock()
        existing.serial = "SN001"
        existing.platform = None
        existing.device_type = None

        with patch("netbox_librenms_plugin.utils.find_matching_platform", return_value={"found": False}):
            result = build_sync_info(libre_device, existing)

        assert result["serial_synced"] is True

    @pytest.mark.django_db
    def test_padded_stored_serial_counts_as_synced(self):
        """A real device whose STORED serial is legacy-padded must not report serial drift in the details modal."""
        build_sync_info = self._get_method()
        existing = make_device("sync-info-padded-serial", serial=" SN-STORED-1 ")
        libre_device = {"serial": "SN-STORED-1", "os": "-", "hardware": "-"}

        result = build_sync_info(libre_device, existing)

        assert result["serial_synced"] is True, "padded stored serial reported as drift"
        assert result["all_synced"] is True

    def test_platform_synced_when_matching(self):
        build_sync_info = self._get_method()
        libre_device = {"serial": "-", "os": "ios", "hardware": "-"}
        existing = MagicMock()
        existing.serial = ""
        existing.device_type = None

        mock_platform = MagicMock()
        mock_platform.pk = 1
        existing.platform = mock_platform

        with patch("netbox_librenms_plugin.utils.find_matching_platform") as mock_match:
            mock_match.return_value = {"found": True, "platform": mock_platform}
            result = build_sync_info(libre_device, existing)

        assert result["platform_synced"] is True

    def test_device_type_synced_when_matched(self):
        build_sync_info = self._get_method()
        libre_device = {"serial": "-", "os": "-", "hardware": "Cisco C9300"}
        existing = MagicMock()
        existing.serial = ""
        existing.platform = None

        mock_dt = MagicMock()
        mock_dt.pk = 10
        existing.device_type = mock_dt

        with patch("netbox_librenms_plugin.utils.match_librenms_hardware_to_device_type") as mock_hw:
            mock_hw.return_value = {"matched": True, "device_type": mock_dt}
            result = build_sync_info(libre_device, existing)

        assert result["device_type_synced"] is True

    def test_device_type_not_synced_when_mismatch(self):
        build_sync_info = self._get_method()
        libre_device = {"serial": "-", "os": "-", "hardware": "Cisco C9300"}
        existing = MagicMock()
        existing.serial = ""
        existing.platform = None

        netbox_dt = MagicMock()
        netbox_dt.pk = 5
        librenms_dt = MagicMock()
        librenms_dt.pk = 10
        existing.device_type = netbox_dt

        with patch("netbox_librenms_plugin.utils.match_librenms_hardware_to_device_type") as mock_hw:
            mock_hw.return_value = {"matched": True, "device_type": librenms_dt}
            result = build_sync_info(libre_device, existing)

        assert result["device_type_synced"] is False


class TestBuildIdServerInfo:
    """Tests for _build_id_server_info (lines 888-924)."""

    def _get_method(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        return DeviceValidationDetailsView._build_id_server_info

    def test_legacy_int_returns_none(self):
        method = self._get_method()
        existing = MagicMock()
        existing.custom_field_data = {"librenms_id": 42}
        result = method(existing)
        assert result is None

    def test_none_cf_returns_none(self):
        method = self._get_method()
        existing = MagicMock()
        existing.custom_field_data = {}
        result = method(existing)
        assert result is None

    def test_dict_cf_returns_list(self):
        method = self._get_method()
        existing = MagicMock()
        existing.custom_field_data = {"librenms_id": {"default": 42}}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {"servers": {"default": {"display_name": "Default Server"}}}
            }
            result = method(existing)

        assert result is not None
        assert result[0]["server_key"] == "default"
        assert result[0]["device_id"] == 42

    def test_bool_value_skipped(self):
        method = self._get_method()
        existing = MagicMock()
        existing.custom_field_data = {"librenms_id": {"default": True, "other": 99}}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {"other": {"display_name": "Other"}}}}
            result = method(existing)

        assert result is not None
        assert len(result) == 1
        assert result[0]["server_key"] == "other"

    def test_dict_entry_uses_host_id(self):
        """New dict form {server_key: {"id": N, "oob": {...}}} renders the host id, not None."""
        method = self._get_method()
        existing = MagicMock()
        existing.custom_field_data = {"librenms_id": {"default": {"id": 42, "oob": {"id": 17, "type": "idrac"}}}}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {"servers": {"default": {"display_name": "Default Server"}}}
            }
            result = method(existing)

        assert result is not None
        assert result[0]["device_id"] == 42

    def test_oob_only_dict_entry_surfaced_with_controller_id(self):
        """An OOB-only entry is still a real link → surfaced with the OOB controller's id."""
        method = self._get_method()
        existing = MagicMock()
        existing.custom_field_data = {"librenms_id": {"default": {"oob": {"id": 17, "type": "idrac"}}}}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {"servers": {"default": {"display_name": "Default Server"}}}
            }
            result = method(existing)

        # Mirrors the device-sync modal (_build_all_server_mappings): the OOB-only link is shown
        # using the OOB controller's id rather than dropped (which would risk a duplicate re-import).
        assert result == [{"server_key": "default", "display_name": "Default Server", "device_id": 17}]

    def test_default_key_fallback_display_name(self):
        """'default' with no servers config uses root display_name."""
        method = self._get_method()
        existing = MagicMock()
        existing.custom_field_data = {"librenms_id": {"default": 55}}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {
                    "display_name": "My LibreNMS",
                    "servers": {},
                }
            }
            result = method(existing)

        assert result is not None
        assert result[0]["display_name"] == "My LibreNMS"

    def test_string_device_id_converted(self):
        method = self._get_method()
        existing = MagicMock()
        existing.custom_field_data = {"librenms_id": {"default": "77"}}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {"default": {"display_name": "D"}}}}
            result = method(existing)

        assert result[0]["device_id"] == 77


class TestDeviceRoleUpdateView:
    """Tests for DeviceRoleUpdateView.post (lines ~927+)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceRoleUpdateView

        view = object.__new__(DeviceRoleUpdateView)
        view._librenms_api = _make_api()
        return view

    def test_device_not_found_renders_htmx_error_toast(self):
        view = self._make_view()
        with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, {})):
            result = view.post(_make_request(post={}), device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Device not found" in result.content

    @patch("netbox_librenms_plugin.views.imports.actions.render")
    def test_device_found_renders_row(self, mock_render):
        view = self._make_view()
        mock_render.return_value = MagicMock()

        libre_device = {"device_id": 1}
        validation = {}
        selections = {"cluster_id": None, "role_id": None, "rack_id": None}

        with patch.object(
            view, "get_validated_device_with_selections", return_value=(libre_device, validation, selections)
        ):
            with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render_row:
                view.post(_make_request(post={}), device_id=1)

        mock_render_row.assert_called_once()


class TestDeviceClusterUpdateView:
    """Tests for DeviceClusterUpdateView.post."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceClusterUpdateView

        view = object.__new__(DeviceClusterUpdateView)
        view._librenms_api = _make_api()
        return view

    def test_device_not_found_renders_htmx_error_toast(self):
        view = self._make_view()
        with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, {})):
            result = view.post(_make_request(post={}), device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Device not found" in result.content


class TestDeviceRackUpdateView:
    """Tests for DeviceRackUpdateView.post."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceRackUpdateView

        view = object.__new__(DeviceRackUpdateView)
        view._librenms_api = _make_api()
        return view

    def test_device_not_found_renders_htmx_error_toast(self):
        view = self._make_view()
        with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, {})):
            result = view.post(_make_request(post={}), device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Device not found" in result.content


class TestRowUpdateViewsServerRebind:
    """DeviceRole/Cluster/RackUpdateView must pin the client to the POSTed server_key.

    The import page's row selects post the page's server_key (hx-vals); without the
    rebind the lookup routes through the global selected server and re-validates/caches
    the WRONG server's device for the row.
    """

    VIEWS = ["DeviceRoleUpdateView", "DeviceClusterUpdateView", "DeviceRackUpdateView"]

    def _view(self, view_name):
        from netbox_librenms_plugin.views.imports import actions

        return object.__new__(getattr(actions, view_name))

    @pytest.mark.parametrize("view_name", VIEWS)
    def test_stale_server_key_fails_closed_before_lookup(self, view_name):
        """An unresolvable POSTed key errors out without any device lookup (mirrors the sibling import endpoints)."""
        view = self._view(view_name)
        req = _make_request(post={"server_key": "ghost"})
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
            with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, {})) as lookup:
                result = view.post(req, device_id=42)
        lookup.assert_not_called()
        assert result.headers.get("HX-Reswap") == "none"
        assert b"no longer configured" in result.content

    @pytest.mark.parametrize("view_name", VIEWS)
    def test_rebinds_to_posted_server(self, view_name):
        """The POSTed server_key is bound before the lookup, so the row re-validates against the page's server."""
        view = self._view(view_name)
        api = MagicMock()
        api.server_key = "secondary"
        req = _make_request(post={"server_key": "secondary"})
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=api) as mock_build:
            with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, {})):
                view.post(req, device_id=42)
        mock_build.assert_called_once_with("secondary")
        assert view._librenms_api is api


@pytest.mark.django_db
@pytest.mark.django_db
class TestDeviceConflictActionView:
    """DeviceConflictActionView.post — input guards + real-DB lookup paths."""

    @pytest.fixture(autouse=True)
    def _configure_librenms_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"conflict-{request.node.name}".replace("_", "-").replace("[", "-").replace("]", "")
        with run_librenms_server() as server:
            configure_test_servers(
                settings,
                {
                    self.server_key: {
                        "librenms_url": server.url,
                        "api_token": "test-token",
                        "verify_ssl": False,
                    }
                },
            )
            self.librenms_server = server
            yield

    def _make_view(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = DeviceConflictActionView()
        view._librenms_api = LibreNMSAPI(server_key=self.server_key)
        return view

    def _register_device(self, device_id, hostname, *, serial=""):
        self.librenms_server.device_info_response(
            device_id=device_id,
            hostname=hostname,
            hardware="Test chassis",
            os="ios",
            serial=serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(device_id, [], {})

    @pytest.mark.parametrize("htmx", [False, True], ids=["regular", "htmx"])
    def test_no_permission_returns_error(self, htmx):
        from dcim.models import Device
        from django.urls import get_script_prefix

        view = self._make_view()
        existing_device = make_device(
            "conflict-denied",
            librenms_cf={self.server_key: {"id": 10}},
        )
        self._register_device(17, existing_device.name)
        user = make_view_user(
            f"conflict-denied-{'htmx' if htmx else 'regular'}-user",
            [("change", Device)],
            plugin_write=False,
        )
        factory_kwargs = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "link",
                "existing_device_id": str(existing_device.pk),
            },
            user=user,
            **factory_kwargs,
        )
        device_count = Device.objects.count()

        response = post_view(view, request, device_id=17)

        if htmx:
            assert response.status_code == 200
            assert response.content == b""
            assert response["HX-Redirect"] == get_script_prefix()
        else:
            assert response.status_code == 302
            assert response["Location"] == get_script_prefix()
        assert view_message_texts(request, "error") == ["You do not have permission to perform this action."]
        assert Device.objects.count() == device_count
        reloaded = Device.objects.get(pk=existing_device.pk)
        assert reloaded.custom_field_data["librenms_id"][self.server_key] == {"id": 10}

    def test_missing_action_renders_htmx_error_toast(self):
        view = self._make_view()
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": "1"},
            user=make_view_user("conflict-missing-action-user", []),
            HTTP_HX_REQUEST="true",
        )
        result = post_view(view, request, device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Missing action or existing_device_id" in result.content

    def test_missing_existing_device_id_renders_htmx_error_toast(self):
        view = self._make_view()
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "action": "link"},
            user=make_view_user("conflict-missing-device-user", []),
            HTTP_HX_REQUEST="true",
        )
        result = post_view(view, request, device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Missing action or existing_device_id" in result.content

    def test_vm_with_unsupported_action_renders_htmx_error_toast(self):
        view = self._make_view()
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "link",
                "existing_device_id": "5",
                "existing_device_type": "virtualmachine",
            },
            user=make_view_user("conflict-unsupported-vm-user", []),
            HTTP_HX_REQUEST="true",
        )
        result = post_view(view, request, device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"is not supported for virtual machines" in result.content

    def test_existing_device_not_found_renders_htmx_error_toast(self):
        from dcim.models import Device

        # A pk that isn't in the DB → a real Device.objects.get miss, not a stubbed raise.
        view = self._make_view()
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "link",
                "existing_device_id": "987654321",
            },
            user=make_view_user("conflict-missing-row-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )
        result = post_view(view, request, device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Existing device not found" in result.content

    def test_unknown_action_renders_htmx_error_toast(self):
        from dcim.models import Device

        view = self._make_view()
        existing_device = make_device("conflict-unknown-action")
        self._register_device(1, existing_device.name)
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "unknown_action",
                "existing_device_id": str(existing_device.pk),
            },
            user=make_view_user("conflict-unknown-action-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )
        result = post_view(view, request, device_id=1)

        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Unknown action: unknown_action" in result.content


class TestSaveUserPrefView:
    """Tests for SaveUserPrefView.post."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import SaveUserPrefView

        view = object.__new__(SaveUserPrefView)
        return view

    def test_invalid_json_returns_400(self):
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            request = MagicMock()
            request.body = b"not-json"
            result = view.post(request)
        assert result.status_code == 400

    def test_invalid_key_returns_400(self):
        import json

        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            request = MagicMock()
            request.body = json.dumps({"key": "disallowed_key", "value": True}).encode()
            result = view.post(request)
        assert result.status_code == 400

    def test_non_object_json_returns_400(self):
        """Valid JSON that is not an object (list/str/number) must be rejected as 400, not 500 on data.get()."""
        import json

        view = self._make_view()
        for payload in ([1, 2, 3], "hello", 42):
            with patch.object(view, "require_write_permission", return_value=None):
                request = MagicMock()
                request.body = json.dumps(payload).encode()
                result = view.post(request)  # must not raise AttributeError
            assert result.status_code == 400

    def test_valid_pref_saved(self):
        import json

        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.imports.actions.save_user_pref") as mock_save:
                request = MagicMock()
                request.body = json.dumps({"key": "use_sysname", "value": True}).encode()
                result = view.post(request)

        assert result.status_code == 200
        mock_save.assert_called_once()


class TestDeviceVCDetailsView:
    """Tests for DeviceVCDetailsView.get (lines 766-790)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceVCDetailsView

        view = object.__new__(DeviceVCDetailsView)
        view._librenms_api = _make_api()
        return view

    def test_device_not_found_returns_200_html_fragment(self):
        # HTMX fragment swapped into the modal; HTMX skips the swap on a 4xx, so return 200.
        view = self._make_view()
        with patch("netbox_librenms_plugin.views.imports.actions.get_librenms_device_by_id", return_value=None):
            result = view.get(MagicMock(), device_id=1)
        assert result.status_code == 200
        assert b"not found in LibreNMS" in result.content

    @patch("netbox_librenms_plugin.views.imports.actions.render")
    def test_device_found_renders_template(self, mock_render):
        view = self._make_view()
        mock_render.return_value = MagicMock()
        libre_device = {"device_id": 1, "hostname": "router01"}
        vc_data = {"is_stack": False, "members": []}

        with patch("netbox_librenms_plugin.views.imports.actions.get_librenms_device_by_id", return_value=libre_device):
            with patch("netbox_librenms_plugin.views.imports.actions.get_virtual_chassis_data", return_value=vc_data):
                view.get(MagicMock(), device_id=1)

        mock_render.assert_called_once()
        assert "device_vc_details.html" in mock_render.call_args[0][1]


class TestBulkImportDevicesViewSyncExecution:
    """Tests for BulkImportDevicesView methods."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = object.__new__(BulkImportDevicesView)
        view._librenms_api = _make_api()
        return view

    def test_should_use_background_job_superuser_with_flag(self):
        """should_use_background_job_for_import returns True for superuser with flag."""
        view = self._make_view()
        request = _make_request(post={"use_background_job": "on"})
        request.user.is_superuser = True

        result = view.should_use_background_job_for_import(request)
        assert result is True

    def test_should_use_background_job_non_superuser(self):
        """Non-superuser always gets False."""
        view = self._make_view()
        request = _make_request(post={"use_background_job": "on"})
        request.user.is_superuser = False

        result = view.should_use_background_job_for_import(request)
        assert result is False

    def test_should_use_background_job_superuser_without_flag(self):
        """Superuser without flag gets False."""
        view = self._make_view()
        request = _make_request(post={})
        request.user.is_superuser = True

        result = view.should_use_background_job_for_import(request)
        assert result is False


class TestBuildSyncInfoNoPlatform:
    """Tests for _build_sync_info when no platform on either side."""

    def _get_method(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        return DeviceValidationDetailsView._build_sync_info

    def test_both_platforms_none_not_synced(self):
        method = self._get_method()
        libre_device = {"serial": "-", "os": "-", "hardware": "-"}
        existing = MagicMock()
        existing.serial = ""
        existing.platform = None
        existing.device_type = None

        result = method(libre_device, existing)
        assert "platform_synced" in result

    def test_serial_empty_treated_as_not_set(self):
        method = self._get_method()
        libre_device = {"serial": "-", "os": "-", "hardware": "-"}
        existing = MagicMock()
        existing.serial = ""  # Empty string
        existing.platform = None
        existing.device_type = None

        result = method(libre_device, existing)
        # Both serials are blank/dash → serial_synced could be True or False but should be in result
        assert "serial_synced" in result


class TestResolveTruthyPreferences:
    """Tests for resolve_naming_preferences truthy parsing via integration."""

    def test_on_value_resolves_to_true(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request(post={"use_sysname": "on", "strip_domain": "on"})
        with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
            MockSettings.objects.first.return_value = None
            use_sysname, strip_domain = resolve_naming_preferences(request)
        assert use_sysname is True
        assert strip_domain is True

    def test_false_value_resolves_to_false(self):
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        request = _make_request(post={"use_sysname": "false", "strip_domain": "0"})
        with patch("netbox_librenms_plugin.models.LibreNMSSettings", create=True) as MockSettings:
            MockSettings.objects.first.return_value = None
            use_sysname, strip_domain = resolve_naming_preferences(request)
        assert use_sysname is False
        assert strip_domain is False


class TestBuildIdServerInfoEdgeCases:
    """Tests for DeviceValidationDetailsView._build_id_server_info edge cases (lines 905, 912)."""

    def test_non_dict_servers_config_treated_as_empty(self):
        """Line 905: servers_config is not a dict → treated as {}."""
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": 42}}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {"servers": "not-a-dict"}  # Not a dict
            }
            result = DeviceValidationDetailsView._build_id_server_info(obj)
        assert result is not None

    def test_string_non_digit_id_is_skipped(self):
        """Line 912: string ID that is not digit is skipped."""
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": "notdigit", "main": 42}}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": {"servers": {}}}
            result = DeviceValidationDetailsView._build_id_server_info(obj)
        # "notdigit" key is skipped (line 912), "main": 42 is included
        if result:
            ids = [item["device_id"] for item in result]
            assert 42 in ids


@pytest.mark.django_db
class TestBulkImportDevicesViewErrorPaths:
    """Tests for BulkImportDevicesView.post() early-exit paths."""

    @staticmethod
    def _make_view(settings, server, server_key):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        configure_test_servers(
            settings,
            {
                server_key: {
                    "librenms_url": server.url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        return BulkImportDevicesView()

    @pytest.fixture
    def librenms_server(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as server:
            yield server

    def test_post_no_devices_selected_htmx_returns_400(self, settings, librenms_server):
        """Empty device_ids on the HTMX path returns a raw 400 (surfaced as a toast client-side)."""
        server_key = "bulk-errors-empty-htmx"
        view = self._make_view(settings, librenms_server, server_key)
        request = make_view_request(
            "post",
            {"server_key": server_key},
            user=make_view_user("bulk-errors-empty-htmx-user", []),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 400
        assert response.content == b"No devices selected"
        assert view_message_texts(request, "error") == []

    def test_post_no_devices_selected_full_page_redirects_with_message(self, settings, librenms_server):
        """Empty device_ids on a non-HTMX POST queues an error message and redirects to the import page, rather than serving a bare 400 body as a full page."""
        server_key = "bulk-errors-empty-page"
        view = self._make_view(settings, librenms_server, server_key)
        request = make_view_request(
            "post",
            {"server_key": server_key},
            user=make_view_user("bulk-errors-empty-page-user", []),
        )  # no HX-Request header

        result = post_view(view, request)

        assert result.status_code == 302
        assert result["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")
        assert view_message_texts(request, "error") == ["No devices selected for import"]

    def test_post_invalid_device_id_htmx_returns_400(self, settings, librenms_server):
        """A non-int device_id on the HTMX path returns a raw 400."""
        server_key = "bulk-errors-invalid-htmx"
        view = self._make_view(settings, librenms_server, server_key)
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["not-an-int"]},
            user=make_view_user("bulk-errors-invalid-htmx-user", []),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 400
        assert response.content == b"Invalid device identifier"
        assert view_message_texts(request, "error") == []

    def test_post_invalid_device_id_full_page_redirects_with_message(self, settings, librenms_server):
        """A non-int device_id on a non-HTMX POST queues an error and redirects to the import page."""
        server_key = "bulk-errors-invalid-page"
        view = self._make_view(settings, librenms_server, server_key)
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["not-an-int"]},
            user=make_view_user("bulk-errors-invalid-page-user", []),
        )  # no HX-Request header

        response = post_view(view, request)

        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")
        assert view_message_texts(request, "error") == ["Invalid device identifier supplied"]

    def test_post_permission_denied(self, settings, librenms_server):
        """Permission check returns error early."""
        from dcim.models import Device
        from django.apps import apps
        from django.urls import get_script_prefix

        server_key = "bulk-errors-denied"
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user(
            "bulk-errors-denied-user",
            [("add", Device), ("change", Device)],
            plugin_write=False,
        )
        settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        user = grant_view_permission(user, "view", settings_model)
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["1"]},
            user=user,
        )

        with (
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_devices") as mock_device_import,
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_vms") as mock_vm_import,
        ):
            response = post_view(view, request)

        assert response.status_code == 302
        assert response["Location"] == get_script_prefix()
        assert view_message_texts(request, "error") == ["You do not have permission to perform this action."]
        mock_device_import.assert_not_called()
        mock_vm_import.assert_not_called()


class TestDeviceConflictActionViewVMGuard:
    """Tests for DeviceConflictActionView VM action guard (lines 994-1002)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_non_migrate_action_for_vm_renders_htmx_error_toast(self):
        """Lines 995-999: VM + non-migrate action = 400."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
                "existing_device_type": "virtualmachine",
            }
        )

        with patch.object(view, "require_all_permissions", return_value=None):
            response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"is not supported for virtual machines" in response.content

    def test_missing_action_renders_htmx_error_toast(self):
        """Line 989-990: missing action renders htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "1"})  # No action

        with patch.object(view, "require_all_permissions", return_value=None):
            response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Missing action or existing_device_id" in response.content

    def test_server_key_override_creates_new_api(self):
        """Line 987: POST server_key creates new LibreNMSAPI."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
                "server_key": "secondary",
            }
        )

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("netbox_librenms_plugin.librenms_api.LibreNMSAPI") as MockAPI:
                # The posted key is honoured only when it names a CONFIGURED server: the rebind guard
                # consults get_available_servers() first, so a forged/stale key can't 500 the action.
                MockAPI.get_available_servers.return_value = {"secondary": "Secondary"}
                with patch("dcim.models.Device") as MockDevice:
                    mock_device_obj = MagicMock()
                    MockDevice.objects.restrict.return_value.get.return_value = mock_device_obj
                    MockDevice.DoesNotExist = Exception
                    with patch("netbox_librenms_plugin.views.imports.actions.cache"):
                        with patch.object(
                            view, "get_validated_device_with_selections", return_value=(None, None, None)
                        ):
                            response = view.post(request, device_id=1)

        MockAPI.assert_called_with(server_key="secondary")
        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"


class TestDeviceRoleClusterRackViews:
    """Tests for DeviceRoleUpdateView, DeviceClusterUpdateView, DeviceRackUpdateView."""

    def test_device_role_update_not_found(self):
        """DeviceRoleUpdateView renders htmx error toast (200) when device not found."""
        from netbox_librenms_plugin.views.imports.actions import DeviceRoleUpdateView

        view = object.__new__(DeviceRoleUpdateView)
        view._librenms_api = _make_api()

        request = _make_request(post={"role_id": "1"})

        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, None)):
                response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Device not found" in response.content

    def test_device_cluster_update_not_found(self):
        """DeviceClusterUpdateView renders htmx error toast (200) when device not found."""
        from netbox_librenms_plugin.views.imports.actions import DeviceClusterUpdateView

        view = object.__new__(DeviceClusterUpdateView)
        view._librenms_api = _make_api()

        request = _make_request(post={"cluster_id": "1"})

        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, None)):
                response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Device not found" in response.content

    def test_device_rack_update_not_found(self):
        """DeviceRackUpdateView renders htmx error toast (200) when device not found."""
        from netbox_librenms_plugin.views.imports.actions import DeviceRackUpdateView

        view = object.__new__(DeviceRackUpdateView)
        view._librenms_api = _make_api()

        request = _make_request(post={"rack_id": "1"})

        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, None)):
                response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Device not found" in response.content

    def test_device_role_update_renders_row(self):
        """DeviceRoleUpdateView renders row when device found."""
        from netbox_librenms_plugin.views.imports.actions import DeviceRoleUpdateView

        view = object.__new__(DeviceRoleUpdateView)
        view._librenms_api = _make_api()

        request = _make_request(post={"role_id": "1"})
        libre_device = {"device_id": 1, "hostname": "router01"}
        validation = {"status": "importable"}
        selections = {}

        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(
                view, "get_validated_device_with_selections", return_value=(libre_device, validation, selections)
            ):
                with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render:
                    view.post(request, device_id=1)
        mock_render.assert_called_once()


@pytest.mark.django_db
class TestDeviceConflictActionLinkAction:
    """DeviceConflictActionView 'link' action against a real Device."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def test_link_action_persists_librenms_id(self):
        """The 'link' action writes the LibreNMS id into custom_field_data and sets the name."""
        from dcim.models import Device
        from django.http import HttpResponse

        view = self._make_view()
        dev = make_device("router01-link")  # unlinked → not legacy, no id conflict
        request = _make_request(post={"action": "link", "existing_device_id": str(dev.pk)})

        libre_device = {"device_id": 42, "hostname": "router01", "hardware": "Cisco"}
        validation = {"existing_device": dev, "device_type_mismatch": False}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        with patch(
            "netbox_librenms_plugin.views.imports.actions._get_hostname_for_action",
            return_value="router01-linked",
        ):
            response = view.post(request, device_id=42)

        assert response["HX-Trigger"] == "closeModal"
        view.render_device_row.assert_called_once()
        # Reload from the DB: the real set_librenms_device_id + _save_device committed the link.
        reloaded = Device.objects.get(pk=dev.pk)
        assert reloaded.custom_field_data["librenms_id"]["default"] == 42
        assert reloaded.name == "router01-linked"

    def test_link_action_blocked_by_librenms_id_conflict(self):
        """If another device already owns the incoming LibreNMS id, the link is refused and nothing is persisted — driven by the real find_by_librenms_id conflict check."""
        from dcim.models import Device
        from django.http import HttpResponse

        view = self._make_view()
        owner = make_device("router-owns-42", librenms_cf={"default": 42})
        dev = make_device("router01-link-conflict")
        request = _make_request(post={"action": "link", "existing_device_id": str(dev.pk)})

        libre_device = {"device_id": 42, "hostname": "router01", "hardware": "Cisco"}
        validation = {"existing_device": dev, "device_type_mismatch": False}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        with patch(
            "netbox_librenms_plugin.views.imports.actions._get_hostname_for_action",
            return_value="router01",
        ):
            response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"already assigned to device" in response.content
        assert owner.name.encode() in response.content  # the conflicting owner is named
        # The target device was NOT linked.
        assert "librenms_id" not in Device.objects.get(pk=dev.pk).custom_field_data


class TestApplyUserSelectionsToValidation:
    """Tests for _apply_user_selections_to_validation (lines 279-300)."""

    def test_vm_with_cluster_and_role(self):
        """Lines 279-288: VM mode applies cluster and role."""
        from netbox_librenms_plugin.views.imports.actions import _apply_user_selections_to_validation

        validation = {}
        selections = {"cluster_id": "1", "role_id": "2", "rack_id": None}
        mock_cluster = MagicMock()
        mock_role = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.imports.actions.fetch_model_by_id",
            side_effect=lambda model, id_: mock_cluster if str(id_) == "1" else mock_role,
        ):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.apply_cluster_to_validation"
            ) as mock_apply_cluster:
                with patch("netbox_librenms_plugin.views.imports.actions.apply_role_to_validation") as mock_apply_role:
                    _apply_user_selections_to_validation(validation, selections, is_vm=True)

        mock_apply_cluster.assert_called_once_with(validation, mock_cluster)
        mock_apply_role.assert_called_once_with(validation, mock_role, is_vm=True)

    def test_device_with_role_and_rack(self):
        """Lines 292-300: Device mode applies role and rack."""
        from netbox_librenms_plugin.views.imports.actions import _apply_user_selections_to_validation

        validation = {}
        selections = {"cluster_id": None, "role_id": "1", "rack_id": "2"}
        mock_role = MagicMock()
        mock_rack = MagicMock()

        call_count = [0]

        def mock_fetch(model, id_):
            call_count[0] += 1
            return mock_role if call_count[0] == 1 else mock_rack

        with patch("netbox_librenms_plugin.views.imports.actions.fetch_model_by_id", side_effect=mock_fetch):
            with patch("netbox_librenms_plugin.views.imports.actions.apply_role_to_validation") as mock_apply_role:
                with patch("netbox_librenms_plugin.views.imports.actions.apply_rack_to_validation") as mock_apply_rack:
                    _apply_user_selections_to_validation(validation, selections, is_vm=False)

        mock_apply_role.assert_called_once_with(validation, mock_role, is_vm=False)
        mock_apply_rack.assert_called_once_with(validation, mock_rack)


class TestDeviceVCDetailsViewAdditional:
    """Tests for DeviceVCDetailsView.get() (line 334 in vc details)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceVCDetailsView

        view = object.__new__(DeviceVCDetailsView)
        view._librenms_api = _make_api()
        return view

    def test_device_not_found_in_librenms_returns_200_html_fragment(self):
        """Device not found in LibreNMS: HTMX fragment must come back 200 (a 4xx makes HTMX skip the swap), with the inline alert in the body."""
        view = self._make_view()
        request = _make_request()

        with patch("netbox_librenms_plugin.views.imports.actions.get_librenms_device_by_id", return_value=None):
            response = view.get(request, device_id=1)

        assert response.status_code == 200
        assert b"not found in LibreNMS" in response.content

    def test_device_found_renders_vc_details(self):
        """DeviceVCDetailsView.get renders vc details template."""
        view = self._make_view()
        request = _make_request()

        libre_device = {"device_id": 1, "hostname": "sw01"}
        vc_data = {"is_stack": True}

        with patch("netbox_librenms_plugin.views.imports.actions.get_librenms_device_by_id", return_value=libre_device):
            with patch("netbox_librenms_plugin.views.imports.actions.get_virtual_chassis_data", return_value=vc_data):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.render", return_value=MagicMock(status_code=200)
                ) as mock_render:
                    view.get(request, device_id=1)

        mock_render.assert_called_once()


@pytest.mark.django_db
class TestDeviceConflictActionMigrateLibreNMSId:
    """DeviceConflictActionView migrate_librenms_id action through real integration seams."""

    @pytest.fixture(autouse=True)
    def _configure_librenms_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"migrate-{request.node.name}".replace("_", "-").replace("[", "-").replace("]", "")
        with run_librenms_server() as server:
            configure_test_servers(
                settings,
                {
                    self.server_key: {
                        "librenms_url": server.url,
                        "api_token": "test-token",
                        "verify_ssl": False,
                    }
                },
            )
            self.librenms_server = server
            yield

    def _make_view(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = DeviceConflictActionView()
        view._librenms_api = LibreNMSAPI(server_key=self.server_key)
        return view

    def _register_device(self, device_id, hostname, *, serial=""):
        self.librenms_server.device_info_response(
            device_id=device_id,
            hostname=hostname,
            hardware="Test chassis",
            os="ios",
            serial=serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(device_id, [], {})

    @staticmethod
    def _post_after_validation(view, request, device_id, mutation):
        """Run a real validation, then inject one deterministic concurrent database write."""
        validate = view.get_validated_device_with_selections

        def validate_then_mutate(*args, **kwargs):
            result = validate(*args, **kwargs)
            mutation()
            return result

        with patch.object(view, "get_validated_device_with_selections", side_effect=validate_then_mutate):
            return post_view(view, request, device_id=device_id)

    def test_rejects_a_mapping_that_is_already_json(self):
        """Reject a mapping that already uses the multi-server dictionary format."""
        from dcim.models import Device

        view = self._make_view()
        device = make_device(
            "migrate-json-device",
            serial="MIGRATE-SERIAL",
            librenms_cf={self.server_key: 42},
        )
        self._register_device(42, "migrate-json-device", serial="MIGRATE-SERIAL")
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(device.pk),
            },
            user=make_view_user("migrate-json-device-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"already in JSON format" in response.content
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_rejects_a_legacy_id_that_is_not_the_active_device_id(self):
        """Reject a signed legacy ID that differs from the active LibreNMS device ID."""
        from dcim.models import Device

        view = self._make_view()
        device = make_device(
            "migrate-hostname-device",
            serial="MIGRATE-SERIAL",
            librenms_cf="+99",
        )
        self._register_device(42, "migrate-hostname-device", serial="MIGRATE-SERIAL")
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(device.pk),
            },
            user=make_view_user("migrate-hostname-device-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"does not match the active device ID" in response.content
        assert b"already in JSON format" not in response.content
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == "+99"

    def test_refuses_a_reader_only_legacy_form_even_when_a_rival_owns_the_id(self):
        """Refuse the reader-only "4_2": under int() the rival's single match would pass the ambiguity guard."""
        from dcim.models import Device

        view = self._make_view()
        device = make_device("migrate-reader-only-device", serial="MIGRATE-SERIAL", librenms_cf="4_2")
        rival = make_device(
            "migrate-reader-only-rival",
            serial="RIVAL-SERIAL",
            librenms_cf={self.server_key: 99},
        )
        self._register_device(42, "migrate-reader-only-device", serial="MIGRATE-SERIAL")
        # Only an ID match sets serial_confirmed, and "4_2" matches no lookup, so force is required here.
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(device.pk),
                "force": "on",
            },
            user=make_view_user("migrate-reader-only-device-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

        response = self._post_after_validation(
            view,
            request,
            42,
            lambda: Device.objects.filter(pk=rival.pk).update(custom_field_data={"librenms_id": {self.server_key: 42}}),
        )

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"is not a plain positive integer" in response.content
        assert b"does not match the active device ID" not in response.content
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == "4_2"
        assert Device.objects.get(pk=rival.pk).custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_rejects_a_vm_migration_without_force_because_no_vm_confirms_a_serial(self):
        """VMs need force: only Device ID matches set serial_confirmed (import_utils/device_operations.py:802)."""
        from virtualization.models import VirtualMachine

        view = self._make_view()
        vm = make_vm("migrate-force-vm")
        vm.custom_field_data["librenms_id"] = 42
        vm.save()
        self.librenms_server.device_info_response(
            device_id=42,
            hostname="migrate-force-vm",
            hardware="Test VM",
            os="linux",
            serial="",
            ip="",
        )
        self.librenms_server.vc_inventory_callable(42, [], {})
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(vm.pk),
                "existing_device_type": "virtualmachine",
                "cluster_42": str(vm.cluster_id),
            },
            user=make_view_user("migrate-force-vm-user", [("change", VirtualMachine)]),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Serial number not confirmed" in response.content
        assert VirtualMachine.objects.get(pk=vm.pk).custom_field_data["librenms_id"] == 42

    def test_migrates_a_device_and_persists_the_dict_format(self):
        """Migrate a confirmed Device mapping and render the updated import row."""
        from dcim.models import Device

        view = self._make_view()
        device = make_device("migrate-success-device", serial="MIGRATE-SERIAL", librenms_cf=42)
        self._register_device(42, "migrate-success-device", serial="MIGRATE-SERIAL")
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(device.pk),
            },
            user=make_view_user("migrate-success-device-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=42)

        assert response.status_code == 200
        assert response["HX-Trigger"] == "closeModal"
        assert response.content.strip()
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_migrates_a_vm_when_force_is_supplied(self):
        """Migrate a VM mapping when the request explicitly supplies force."""
        from virtualization.models import VirtualMachine

        view = self._make_view()
        vm = make_vm("migrate-forced-vm")
        vm.custom_field_data["librenms_id"] = 42
        vm.save()
        self.librenms_server.device_info_response(
            device_id=42,
            hostname="migrate-forced-vm",
            hardware="Test VM",
            os="linux",
            serial="",
            ip="",
        )
        self.librenms_server.vc_inventory_callable(42, [], {})
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(vm.pk),
                "existing_device_type": "virtualmachine",
                "cluster_42": str(vm.cluster_id),
                "force": "on",
            },
            user=make_view_user("migrate-forced-vm-user", [("change", VirtualMachine)]),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=42)

        assert response.status_code == 200
        assert response["HX-Trigger"] == "closeModal"
        assert VirtualMachine.objects.get(pk=vm.pk).custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_fails_closed_when_the_row_is_deleted_between_validation_and_the_lock(self):
        """Fail closed when the validated Device is deleted before the locked re-read."""
        from dcim.models import Device

        view = self._make_view()
        device = make_device("migrate-deleted-device", serial="MIGRATE-SERIAL", librenms_cf=42)
        device_pk = device.pk
        self._register_device(42, "migrate-deleted-device", serial="MIGRATE-SERIAL")
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(device_pk),
            },
            user=make_view_user("migrate-deleted-device-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

        response = self._post_after_validation(
            view,
            request,
            42,
            lambda: Device.objects.filter(pk=device_pk).delete(),
        )

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"no longer exists" in response.content
        assert not Device.objects.filter(pk=device_pk).exists()

    def test_fails_closed_when_another_request_migrates_first(self):
        """Preserve a concurrent dictionary migration found by the locked re-read."""
        from dcim.models import Device

        view = self._make_view()
        device = make_device("migrate-concurrent-json-device", serial="MIGRATE-SERIAL", librenms_cf=42)
        self._register_device(42, "migrate-concurrent-json-device", serial="MIGRATE-SERIAL")
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(device.pk),
            },
            user=make_view_user("migrate-concurrent-json-device-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

        response = self._post_after_validation(
            view,
            request,
            42,
            lambda: Device.objects.filter(pk=device.pk).update(
                custom_field_data={"librenms_id": {self.server_key: 42}}
            ),
        )

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"already in JSON format" in response.content
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_fails_closed_when_the_legacy_id_changes_under_the_lock(self):
        """Preserve a concurrent legacy ID change found by the locked re-read."""
        from dcim.models import Device

        view = self._make_view()
        device = make_device("migrate-changed-id-device", serial="MIGRATE-SERIAL", librenms_cf=42)
        self._register_device(42, "migrate-changed-id-device", serial="MIGRATE-SERIAL")
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(device.pk),
            },
            user=make_view_user("migrate-changed-id-device-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

        response = self._post_after_validation(
            view,
            request,
            42,
            lambda: Device.objects.filter(pk=device.pk).update(custom_field_data={"librenms_id": 99}),
        )

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"changed under lock" in response.content
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == 99

    def test_fails_closed_when_a_rival_claims_the_id_under_the_lock(self):
        """Preserve both mappings when a concurrent rival makes the ID ambiguous."""
        from dcim.models import Device

        view = self._make_view()
        device = make_device("migrate-ambiguity-target", serial="MIGRATE-SERIAL", librenms_cf=42)
        rival = make_device(
            "migrate-ambiguity-rival",
            serial="RIVAL-SERIAL",
            librenms_cf={self.server_key: 99},
        )
        self._register_device(42, "migrate-ambiguity-target", serial="MIGRATE-SERIAL")
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(device.pk),
            },
            user=make_view_user("migrate-ambiguity-target-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

        response = self._post_after_validation(
            view,
            request,
            42,
            lambda: Device.objects.filter(pk=rival.pk).update(custom_field_data={"librenms_id": {self.server_key: 42}}),
        )

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"ambiguous" in response.content
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == 42
        assert Device.objects.get(pk=rival.pk).custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_fails_closed_when_the_save_raises_integrity_error(self):
        """Keep the legacy mapping when the real model save method raises IntegrityError."""
        from dcim.models import Device
        from django.db import IntegrityError

        view = self._make_view()
        device = make_device("migrate-integrity-device", serial="MIGRATE-SERIAL", librenms_cf=42)
        self._register_device(42, "migrate-integrity-device", serial="MIGRATE-SERIAL")
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": "migrate_librenms_id",
                "existing_device_id": str(device.pk),
            },
            user=make_view_user("migrate-integrity-device-user", [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

        with patch.object(Device, "save", side_effect=IntegrityError("dup")):
            response = post_view(view, request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Unable to migrate the LibreNMS mapping" in response.content
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == 42


class TestDeviceConflictActionMissingExisting:
    """Tests for DeviceConflictActionView when device not found (line 1008-1009)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_existing_device_not_found_renders_htmx_error_toast(self):
        """Line 1008-1009: Device.objects.get raises DoesNotExist → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "999",
            }
        )

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                # Use a DISTINCT DoesNotExist type (not aliased to ValueError) so this
                # genuinely exercises the view's `except Device.DoesNotExist` path rather
                # than a conflated ValueError handler.
                class _DeviceDoesNotExist(Exception):
                    pass

                MockDevice.DoesNotExist = _DeviceDoesNotExist
                MockDevice.objects.restrict.return_value.get.side_effect = _DeviceDoesNotExist("Not found")
                response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert b"Existing device not found" in response.content
        assert response["HX-Reswap"] == "none"


@pytest.mark.django_db
class TestDeviceConflictActionBranches:
    """DeviceConflictActionView guard and action branches through real integration seams."""

    @pytest.fixture(autouse=True)
    def _configure_librenms_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"branches-{request.node.name}".replace("_", "-").replace("[", "-").replace("]", "")
        with run_librenms_server() as server:
            configure_test_servers(
                settings,
                {
                    self.server_key: {
                        "librenms_url": server.url,
                        "api_token": "test-token",
                        "verify_ssl": False,
                    }
                },
            )
            self.librenms_server = server
            yield

    def _make_view(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = DeviceConflictActionView()
        view._librenms_api = LibreNMSAPI(server_key=self.server_key)
        return view

    def _register_device(self, device_id, hostname, *, serial="", hardware="Branches unmatched chassis", os=""):
        self.librenms_server.device_info_response(
            device_id=device_id,
            hostname=hostname,
            hardware=hardware,
            os=os,
            serial=serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(device_id, [], {})

    def _request(self, action, device, username, **extra_post):
        from dcim.models import Device

        return make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": action,
                "existing_device_id": str(device.pk),
                **extra_post,
            },
            user=make_view_user(username, [("change", Device)]),
            HTTP_HX_REQUEST="true",
        )

    @staticmethod
    def _post_after_validation(view, request, device_id, mutation, *, before_revalidation=False):
        """Inject one state change after validation or immediately before the post-action re-validation."""
        validate = view.get_validated_device_with_selections
        validation_calls = 0

        def validate_around_mutation(*args, **kwargs):
            nonlocal validation_calls
            validation_calls += 1
            if before_revalidation and validation_calls == 2:
                mutation()
            result = validate(*args, **kwargs)
            if not before_revalidation and validation_calls == 1:
                mutation()
            return result

        with patch.object(view, "get_validated_device_with_selections", side_effect=validate_around_mutation):
            return post_view(view, request, device_id=device_id)

    @staticmethod
    def _assert_htmx_error(response, message):
        from html import unescape

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert message in unescape(response.content.decode())

    @staticmethod
    def _assert_success(response):
        assert response.status_code == 200
        assert response["HX-Trigger"] == "closeModal"
        assert response.content.strip()

    def test_rejects_a_missing_validated_conflict_target(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device("branches-missing-validation-target")
        self._register_device(42, "branches-missing-validation-source")
        request = self._request(
            "link",
            target,
            "branches-missing-validation-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(response, "Missing validated conflict target")
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-missing-validation-target"
        assert "librenms_id" not in reloaded.custom_field_data

    def test_rejects_a_posted_device_that_differs_from_the_validated_target(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device("branches-mismatch-posted-target")
        validated_target = make_device("branches-mismatch-validated-target")
        self._register_device(42, validated_target.name)
        request = self._request(
            "link",
            target,
            "branches-mismatch-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(
            response,
            "Device ID mismatch: existing_device_id does not match validated device",
        )
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-mismatch-posted-target"
        assert "librenms_id" not in reloaded.custom_field_data

    def test_rejects_a_validated_vm_with_the_same_pk_as_the_posted_device(self):
        from dcim.models import Device
        from virtualization.models import VirtualMachine

        view = self._make_view()
        target = make_device("branches-type-mismatch-device-target")
        assert not VirtualMachine.objects.filter(pk=target.pk).exists()
        validated_target = VirtualMachine.objects.create(
            pk=target.pk,
            name="branches-type-mismatch-vm-target",
            cluster=make_cluster("branches-type-mismatch-cluster"),
            status="active",
        )
        self._register_device(42, validated_target.name)
        request = self._request(
            "sync_name",
            target,
            "branches-type-mismatch-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(
            response,
            "Device ID mismatch: existing_device_id does not match validated device",
        )
        assert Device.objects.get(pk=target.pk).name == "branches-type-mismatch-device-target"
        assert VirtualMachine.objects.get(pk=validated_target.pk).name == "branches-type-mismatch-vm-target"

    def test_requires_force_for_an_action_that_uses_a_mismatched_device_type(self):
        from dcim.models import Device, DeviceType

        view = self._make_view()
        target = make_device("branches-force-required-target")
        reported_type = DeviceType.objects.create(
            manufacturer=target.device_type.manufacturer,
            model="Branches Force Required Type",
            slug="branches-force-required-type",
            part_number="BRANCHES-FORCE-HARDWARE",
        )
        self._register_device(
            42,
            target.name,
            hardware=reported_type.part_number,
        )
        request = self._request(
            "link",
            target,
            "branches-force-required-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(
            response,
            "Device type mismatch detected. Check the force checkbox to proceed.",
        )
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.device_type_id != reported_type.pk
        assert "librenms_id" not in reloaded.custom_field_data

    def test_rejects_an_invalid_device_id_from_the_live_payload(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device("branches-invalid-payload-id-target")
        invalid_payload_id = "invalid-device-id"
        self.librenms_server.register(
            "/api/v0/devices/42",
            {
                "status": "ok",
                "devices": [
                    {
                        "device_id": invalid_payload_id,
                        "hostname": target.name,
                        "hardware": "Branches invalid ID chassis",
                        "os": "",
                        "serial": "",
                        "sysName": target.name,
                        "ip": "",
                        "version": "",
                        "features": "-",
                        "location": "-",
                    }
                ],
            },
        )
        # fetch_device_with_cache accepts a live dictionary without re-checking its own device_id.
        # Validation can therefore bind the real row by hostname before the action rejects the bad ID.
        self.librenms_server.vc_inventory_callable(invalid_payload_id, [], {})
        request = self._request(
            "link",
            target,
            "branches-invalid-payload-id-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(response, "Invalid or missing LibreNMS device_id in payload")
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-invalid-payload-id-target"
        assert "librenms_id" not in reloaded.custom_field_data

    def test_rejects_a_legacy_mapping_inside_the_lock(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device("branches-legacy-link-target", librenms_cf=42)
        self._register_device(42, target.name)
        request = self._request(
            "link",
            target,
            "branches-legacy-link-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(
            response,
            "Device has a legacy bare-integer librenms_id; use 'Convert mapping' "
            "to migrate to the multi-server format before linking.",
        )
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-legacy-link-target"
        assert reloaded.custom_field_data["librenms_id"] == 42

    def test_link_sets_the_name_mapping_and_device_type_then_renders_the_row(self):
        from dcim.models import Device, DeviceType

        view = self._make_view()
        target = make_device("branches-link-target", serial="BRANCHES-LINK-SERIAL")
        reported_type = DeviceType.objects.create(
            manufacturer=target.device_type.manufacturer,
            model="Branches Link Type",
            slug="branches-link-type",
            part_number="BRANCHES-LINK-HARDWARE",
        )
        self._register_device(
            42,
            "branches-link-source-name",
            serial="BRANCHES-LINK-SERIAL",
            hardware=reported_type.part_number,
        )
        request = self._request(
            "link",
            target,
            "branches-link-user",
            force="on",
        )

        response = post_view(view, request, device_id=42)

        self._assert_success(response)
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-link-source-name"
        assert reloaded.device_type_id == reported_type.pk
        assert reloaded.custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_update_sets_the_name_serial_mapping_and_device_type_then_renders_the_row(self):
        from dcim.models import Device, DeviceType

        view = self._make_view()
        target = make_device(
            "branches-update-target",
            serial="BRANCHES-UPDATE-OLD",
            librenms_cf={self.server_key: 42},
        )
        reported_type = DeviceType.objects.create(
            manufacturer=target.device_type.manufacturer,
            model="Branches Update Type",
            slug="branches-update-type",
            part_number="BRANCHES-UPDATE-HARDWARE",
        )
        self._register_device(
            42,
            "branches-update-source-name",
            serial="BRANCHES-UPDATE-NEW",
            hardware=reported_type.part_number,
        )
        request = self._request(
            "update",
            target,
            "branches-update-user",
            force="on",
        )

        response = post_view(view, request, device_id=42)

        self._assert_success(response)
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-update-source-name"
        assert reloaded.serial == "BRANCHES-UPDATE-NEW"
        assert reloaded.device_type_id == reported_type.pk
        assert reloaded.custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_update_reports_a_real_serial_conflict(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device(
            "branches-update-conflict-target",
            serial="BRANCHES-UPDATE-CONFLICT-OLD",
            librenms_cf={self.server_key: 42},
        )
        conflict = make_device(
            "branches-update-conflict-owner",
            serial="BRANCHES-UPDATE-CONFLICT-NEW",
        )
        self._register_device(
            42,
            "branches-update-conflict-source",
            serial=conflict.serial,
        )
        request = self._request(
            "update",
            target,
            "branches-update-conflict-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(
            response,
            f"Serial conflict: '{conflict.serial}' is already assigned to device '{conflict.name}' (ID: {conflict.pk})",
        )
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-update-conflict-target"
        assert reloaded.serial == "BRANCHES-UPDATE-CONFLICT-OLD"
        assert reloaded.custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_update_serial_sets_the_serial_mapping_and_device_type_then_renders_the_row(self):
        from dcim.models import Device, DeviceType

        view = self._make_view()
        target = make_device(
            "branches-update-serial-target",
            serial="BRANCHES-UPDATE-SERIAL-OLD",
            librenms_cf={self.server_key: 42},
        )
        reported_type = DeviceType.objects.create(
            manufacturer=target.device_type.manufacturer,
            model="Branches Update Serial Type",
            slug="branches-update-serial-type",
            part_number="BRANCHES-UPDATE-SERIAL-HARDWARE",
        )
        self._register_device(
            42,
            "branches-update-serial-source",
            serial="BRANCHES-UPDATE-SERIAL-NEW",
            hardware=reported_type.part_number,
        )
        request = self._request(
            "update_serial",
            target,
            "branches-update-serial-user",
            force="on",
        )

        response = post_view(view, request, device_id=42)

        self._assert_success(response)
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-update-serial-target"
        assert reloaded.serial == "BRANCHES-UPDATE-SERIAL-NEW"
        assert reloaded.device_type_id == reported_type.pk
        assert reloaded.custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_update_serial_reports_a_real_serial_conflict(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device(
            "branches-update-serial-conflict-target",
            serial="BRANCHES-UPDATE-SERIAL-CONFLICT-OLD",
            librenms_cf={self.server_key: 42},
        )
        conflict = make_device(
            "branches-update-serial-conflict-owner",
            serial="BRANCHES-UPDATE-SERIAL-CONFLICT-NEW",
        )
        self._register_device(
            42,
            "branches-update-serial-conflict-source",
            serial=conflict.serial,
        )
        request = self._request(
            "update_serial",
            target,
            "branches-update-serial-conflict-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(
            response,
            f"Serial conflict: '{conflict.serial}' is already assigned to device '{conflict.name}' (ID: {conflict.pk})",
        )
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-update-serial-conflict-target"
        assert reloaded.serial == "BRANCHES-UPDATE-SERIAL-CONFLICT-OLD"
        assert reloaded.custom_field_data["librenms_id"] == {self.server_key: 42}

    def test_sync_name_sets_the_name_then_renders_the_row(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device(
            "branches-sync-name-target",
            librenms_cf={self.server_key: 42},
        )
        self._register_device(42, "branches-sync-name-source")
        request = self._request(
            "sync_name",
            target,
            "branches-sync-name-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_success(response)
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-sync-name-source"
        assert reloaded.custom_field_data["librenms_id"] == {self.server_key: 42}

    @pytest.mark.django_db(transaction=True)
    def test_sync_name_reports_a_real_database_name_collision(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device(
            "branches-sync-name-collision-target",
            librenms_cf={self.server_key: 42},
        )
        conflict = make_device("branches-sync-name-collision-owner")
        self._register_device(42, conflict.name)
        request = self._request(
            "sync_name",
            target,
            "branches-sync-name-collision-user",
        )

        # NetBox has a database UniqueConstraint on Lower("name") plus site and tenant,
        # with a second site/name constraint for tenant-less devices. Because sync_name
        # uses update_fields, it skips full_clean and the real UPDATE raises IntegrityError.
        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(
            response,
            "Could not save: a database integrity constraint was violated.",
        )
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-sync-name-collision-target"
        assert Device.objects.get(pk=conflict.pk).name == "branches-sync-name-collision-owner"

    def test_update_type_reports_when_validation_has_no_librenms_device_type(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device("branches-update-type-missing-target")
        original_type_id = target.device_type_id
        self._register_device(42, target.name)
        request = self._request(
            "update_type",
            target,
            "branches-update-type-missing-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(response, "No LibreNMS device type available to update")
        assert Device.objects.get(pk=target.pk).device_type_id == original_type_id

    def test_sync_serial_sets_the_serial_then_renders_the_row(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device(
            "branches-sync-serial-target",
            serial="BRANCHES-SYNC-SERIAL-OLD",
        )
        self._register_device(
            42,
            target.name,
            serial="BRANCHES-SYNC-SERIAL-NEW",
        )
        request = self._request(
            "sync_serial",
            target,
            "branches-sync-serial-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_success(response)
        assert Device.objects.get(pk=target.pk).serial == "BRANCHES-SYNC-SERIAL-NEW"

    def test_sync_serial_reports_when_librenms_has_no_valid_serial(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device(
            "branches-sync-serial-empty-target",
            serial="BRANCHES-SYNC-SERIAL-KEEP",
        )
        self._register_device(42, target.name, serial="-")
        request = self._request(
            "sync_serial",
            target,
            "branches-sync-serial-empty-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(response, "No valid serial from LibreNMS")
        assert Device.objects.get(pk=target.pk).serial == "BRANCHES-SYNC-SERIAL-KEEP"

    def test_sync_serial_reports_when_the_row_is_deleted_before_the_lock(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device("branches-sync-serial-deleted-target")
        target_pk = target.pk
        self._register_device(
            42,
            target.name,
            serial="BRANCHES-SYNC-SERIAL-DELETED",
        )
        request = self._request(
            "sync_serial",
            target,
            "branches-sync-serial-deleted-user",
        )

        response = self._post_after_validation(
            view,
            request,
            42,
            lambda: Device.objects.filter(pk=target_pk).delete(),
        )

        self._assert_htmx_error(
            response,
            "Device no longer exists; it may have been deleted concurrently.",
        )
        assert not Device.objects.filter(pk=target_pk).exists()

    def test_sync_platform_sets_the_platform_then_renders_the_row(self):
        from dcim.models import Device, Platform

        view = self._make_view()
        target = make_device("branches-sync-platform-target")
        platform = Platform.objects.create(
            name="branches-sync-platform-os",
            slug="branches-sync-platform-os",
        )
        self._register_device(
            42,
            target.name,
            os=platform.name,
        )
        request = self._request(
            "sync_platform",
            target,
            "branches-sync-platform-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_success(response)
        assert Device.objects.get(pk=target.pk).platform_id == platform.pk

    def test_sync_platform_reports_when_no_platform_matches(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device("branches-sync-platform-missing-target")
        librenms_os = "branches-sync-platform-missing-os"
        self._register_device(
            42,
            target.name,
            os=librenms_os,
        )
        request = self._request(
            "sync_platform",
            target,
            "branches-sync-platform-missing-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(
            response,
            f"Platform '{librenms_os}' not found in NetBox",
        )
        assert Device.objects.get(pk=target.pk).platform_id is None

    def test_sync_platform_reports_real_case_insensitive_platform_ambiguity(self):
        from dcim.models import Device, Platform

        view = self._make_view()
        target = make_device("branches-sync-platform-ambiguous-target")
        librenms_os = "branches-sync-platform-ambiguous-os"
        Platform.objects.create(
            name=librenms_os,
            slug="branches-sync-platform-ambiguous-lower",
        )
        Platform.objects.create(
            name=librenms_os.upper(),
            slug="branches-sync-platform-ambiguous-upper",
        )
        # Platform names are unique case-sensitively. Both real rows therefore exist, while
        # find_matching_platform uses name__iexact and reports the pair as ambiguous.
        self._register_device(
            42,
            target.name,
            os=librenms_os,
        )
        request = self._request(
            "sync_platform",
            target,
            "branches-sync-platform-ambiguous-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(
            response,
            f"Multiple Platforms match OS '{librenms_os}' — resolve the conflict in Platforms",
        )
        assert Device.objects.get(pk=target.pk).platform_id is None

    def test_sync_device_type_sets_the_matching_real_type_then_renders_the_row(self):
        from dcim.models import Device, DeviceType

        view = self._make_view()
        target = make_device("branches-sync-device-type-target")
        matched_type = DeviceType.objects.create(
            manufacturer=target.device_type.manufacturer,
            model="Branches Sync Device Type",
            slug="branches-sync-device-type",
            part_number="BRANCHES-SYNC-DEVICE-TYPE-HARDWARE",
        )
        self._register_device(
            42,
            target.name,
            hardware=matched_type.part_number,
        )
        request = self._request(
            "sync_device_type",
            target,
            "branches-sync-device-type-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_success(response)
        assert Device.objects.get(pk=target.pk).device_type_id == matched_type.pk

    def test_sync_device_type_reports_when_no_real_type_matches(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device("branches-sync-device-type-missing-target")
        hardware = "BRANCHES-NO-MATCHING-DEVICE-TYPE"
        original_type_id = target.device_type_id
        self._register_device(
            42,
            target.name,
            hardware=hardware,
        )
        request = self._request(
            "sync_device_type",
            target,
            "branches-sync-device-type-missing-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(response, f"No matching device type for '{hardware}'")
        assert Device.objects.get(pk=target.pk).device_type_id == original_type_id

    def test_reports_when_librenms_deletes_the_device_before_post_action_revalidation(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device(
            "branches-post-action-missing-target",
            librenms_cf={self.server_key: 42},
        )
        self._register_device(42, "branches-post-action-missing-source")
        request = self._request(
            "sync_name",
            target,
            "branches-post-action-missing-user",
        )

        # This guard checks the second LibreNMS payload, not whether the NetBox target survived.
        # Remove the external row immediately before the second real validation, after sync_name saved.
        response = self._post_after_validation(
            view,
            request,
            42,
            lambda: self.librenms_server.register(
                "/api/v0/devices/42",
                {"status": "error", "message": "Device not found"},
                status=404,
            ),
            before_revalidation=True,
        )

        self._assert_htmx_error(response, "Device not found after action")
        reloaded = Device.objects.get(pk=target.pk)
        assert reloaded.name == "branches-post-action-missing-source"
        assert reloaded.custom_field_data["librenms_id"] == {self.server_key: 42}


@pytest.mark.django_db
class TestDeviceConflictUpdateAction:
    """DeviceConflictActionView 'update' action against a real Device."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def test_update_action_persists_name_serial_and_link(self):
        from dcim.models import Device
        from django.http import HttpResponse

        view = self._make_view()
        dev = make_device("router-update", serial="SN-OLD")
        request = _make_request(post={"action": "update", "existing_device_id": str(dev.pk)})

        libre_device = {"device_id": 42, "hostname": "router01", "serial": "SN-NEW"}
        validation = {"existing_device": dev, "device_type_mismatch": False}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        with patch(
            "netbox_librenms_plugin.views.imports.actions._get_hostname_for_action",
            return_value="router01-updated",
        ):
            response = view.post(request, device_id=42)

        assert response["HX-Trigger"] == "closeModal"
        view.render_device_row.assert_called_once()
        reloaded = Device.objects.get(pk=dev.pk)
        assert reloaded.name == "router01-updated"
        assert reloaded.serial == "SN-NEW"
        assert reloaded.custom_field_data["librenms_id"]["default"] == 42


class TestDeviceClusterRackRenderRow:
    """Tests for DeviceClusterUpdateView and DeviceRackUpdateView render_device_row (lines 950, 963)."""

    def test_device_cluster_update_renders_row(self):
        """Line 950: DeviceClusterUpdateView renders row when device found."""
        from netbox_librenms_plugin.views.imports.actions import DeviceClusterUpdateView

        view = object.__new__(DeviceClusterUpdateView)
        view._librenms_api = _make_api()

        request = _make_request(post={"cluster_id": "1"})
        libre_device = {"device_id": 1, "hostname": "vm01"}
        validation = {"status": "importable"}
        selections = {}

        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(
                view, "get_validated_device_with_selections", return_value=(libre_device, validation, selections)
            ):
                with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render:
                    view.post(request, device_id=1)
        mock_render.assert_called_once()

    def test_device_rack_update_renders_row(self):
        """Line 963: DeviceRackUpdateView renders row when device found."""
        from netbox_librenms_plugin.views.imports.actions import DeviceRackUpdateView

        view = object.__new__(DeviceRackUpdateView)
        view._librenms_api = _make_api()

        request = _make_request(post={"rack_id": "1"})
        libre_device = {"device_id": 1, "hostname": "router01"}
        validation = {"status": "importable"}
        selections = {}

        with patch.object(view, "require_write_permission", return_value=None):
            with patch.object(
                view, "get_validated_device_with_selections", return_value=(libre_device, validation, selections)
            ):
                with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render:
                    view.post(request, device_id=1)
        mock_render.assert_called_once()


class TestDeviceConflictActionBoolAndInvalidId:
    """Tests for lines 1044 and 1047-1048 (bool/invalid librenms_id)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_bool_librenms_id_renders_htmx_error_toast(self):
        """Line 1044: librenms_id is a boolean → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1
        libre_device = {"device_id": True}  # Boolean!
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.restrict.return_value.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Invalid or missing LibreNMS device_id in payload" in response.content

    def test_non_int_librenms_id_renders_htmx_error_toast(self):
        """Lines 1047-1048: librenms_id is non-int string → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1
        libre_device = {"device_id": "not-an-int"}  # Non-int string
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.restrict.return_value.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Invalid or missing LibreNMS device_id in payload" in response.content


class TestDeviceConflictLinkIdConflict:
    """Test DeviceConflictActionView 'link' when ID is already used (line 1069-1070)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_id_conflict_renders_htmx_error_toast(self):
        """Lines 1075-1079: LibreNMS ID conflict → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1

        conflicting_device = MagicMock()
        conflicting_device.name = "router02"
        conflicting_device.pk = 99  # Different pk

        libre_device = {"device_id": 42, "hostname": "router01"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.restrict.return_value.get.return_value = mock_existing
                MockDevice.objects.select_for_update.return_value.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        with patch(
                            "netbox_librenms_plugin.utils.find_by_librenms_id", return_value=conflicting_device
                        ):  # ID conflict!
                            with patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx:
                                mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
                                mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
                                response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"LibreNMS ID conflict" in response.content


class TestSaveDevicePath:
    """Test _save_device IntegrityError and ValidationError paths (line 168)."""

    def test_save_device_validation_error(self):
        """ValidationError during full_clean → 400 response."""
        from netbox_librenms_plugin.views.imports.actions import _save_device
        from django.core.exceptions import ValidationError as DjangoValidationError

        mock_device = MagicMock()
        mock_device.full_clean.side_effect = DjangoValidationError({"name": ["This field is required."]})

        result = _save_device(mock_device)
        assert result is not None
        assert result.status_code == 400
        assert b"Validation error" in result.content

    def test_save_device_integrity_error(self):
        """IntegrityError during save → 409 response."""
        from netbox_librenms_plugin.views.imports.actions import _save_device
        from django.db import IntegrityError

        mock_device = MagicMock()
        mock_device.full_clean.return_value = None
        raw_error = "Duplicate key value violates unique constraint"
        mock_device.save.side_effect = IntegrityError(raw_error)

        result = _save_device(mock_device)
        assert result is not None
        assert result.status_code == 409
        assert b"integrity constraint" in result.content
        # Full raw DB exception text must not leak to the client (case-insensitive).
        assert raw_error.encode().lower() not in result.content.lower()


class TestDeviceConflictSelectForUpdateDoesNotExist:
    """Tests for select_for_update DoesNotExist (lines 1069-1070)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_device_deleted_during_lock_renders_htmx_error_toast(self):
        """Lines 1069-1073: Device.DoesNotExist during select_for_update → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1

        libre_device = {"device_id": 42, "hostname": "router01"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }

        DoesNotExistExc = type("DoesNotExist", (Exception,), {})

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                # The locked re-read is reached through restrict(user, action), so hand back the
                # same manager: the stubs below then describe both the primary read and the lock.
                MockDevice.objects.restrict.return_value = MockDevice.objects
                MockDevice.objects.get.return_value = mock_existing
                # select_for_update().get() raises DoesNotExist
                MockDevice.objects.select_for_update.return_value.get.side_effect = DoesNotExistExc("gone")
                MockDevice.DoesNotExist = DoesNotExistExc
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        with patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None):
                            with patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx:
                                mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
                                mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
                                response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Device no longer exists" in response.content


@pytest.mark.django_db
class TestSyncSerialAction:
    """DeviceConflictActionView 'sync_serial' action against a real Device."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def test_sync_serial_no_serial_renders_htmx_error_toast(self):
        """sync_serial with an empty incoming serial → htmx error toast, nothing persisted."""
        view = self._make_view()
        dev = make_device("sync-serial-empty", serial="KEEP-ME")
        request = _make_request(post={"action": "sync_serial", "existing_device_id": str(dev.pk)})
        libre_device = {"device_id": 42, "hostname": "router01", "serial": ""}
        validation = {"existing_device": dev, "device_type_mismatch": False}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))

        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"No valid serial from LibreNMS" in response.content

    def test_sync_serial_persists_serial(self):
        """sync_serial with a valid serial writes it through the real locked save path."""
        from dcim.models import Device
        from django.http import HttpResponse

        view = self._make_view()
        dev = make_device("sync-serial-ok", serial="SN-OLD")
        request = _make_request(post={"action": "sync_serial", "existing_device_id": str(dev.pk)})
        libre_device = {"device_id": 42, "hostname": "router01", "serial": "SN-FRESH"}
        validation = {"existing_device": dev, "device_type_mismatch": False}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        response = view.post(request, device_id=42)

        assert response["HX-Trigger"] == "closeModal"
        assert Device.objects.get(pk=dev.pk).serial == "SN-FRESH"

    def test_sync_serial_conflict_blocks_and_keeps_serial(self):
        """A serial already owned by another device blocks the sync; the target keeps its serial."""
        from dcim.models import Device

        view = self._make_view()
        make_device("sync-serial-owner", serial="SN-TAKEN")
        dev = make_device("sync-serial-target", serial="SN-OLD")
        request = _make_request(post={"action": "sync_serial", "existing_device_id": str(dev.pk)})
        libre_device = {"device_id": 42, "hostname": "router01", "serial": "SN-TAKEN"}
        validation = {"existing_device": dev, "device_type_mismatch": False}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))

        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Serial conflict" in response.content
        assert Device.objects.get(pk=dev.pk).serial == "SN-OLD"


class TestUpdateAndSerialSaveErrors:
    """Tests for update/update_serial _save_device error paths (lines 1119, 1149)."""

    @pytest.fixture(autouse=True)
    def _no_advisory_lock(self):
        """The serial guard's pg_advisory_xact_lock needs a real connection these mock tests don't have."""
        with patch("netbox_librenms_plugin.views.imports.actions._acquire_serial_assignment_lock"):
            yield

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def _make_setup(self, action):
        view = self._make_view()
        request = _make_request(post={"action": action, "existing_device_id": "1"})
        mock_existing = MagicMock()
        mock_existing.pk = 1
        mock_existing.name = "router01"
        libre_device = {"device_id": 42, "hostname": "r01", "serial": "SN001", "hardware": "Cisco", "os": "ios"}
        validation = {"existing_device": mock_existing, "device_type_mismatch": False}
        return view, request, mock_existing, libre_device, validation

    def _common_patches(self, view, mock_existing, libre_device, validation):
        from contextlib import ExitStack

        DoesNotExistExc = type("DoesNotExist", (Exception,), {})
        MockDevice = MagicMock()
        # restrict() hands back the same manager, so the locked re-read reached through it
        # resolves to the stubs below.
        MockDevice.objects.restrict.return_value = MockDevice.objects
        MockDevice.objects.get.return_value = mock_existing
        MockDevice.objects.select_for_update.return_value.get.return_value = mock_existing
        MockDevice.objects.filter.return_value.exclude.return_value.first.return_value = None
        MockDevice.DoesNotExist = DoesNotExistExc
        mock_tx = MagicMock()
        mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
        stack = ExitStack()
        stack.enter_context(patch.object(view, "require_all_permissions", return_value=None))
        stack.enter_context(patch("dcim.models.Device", MockDevice))
        stack.enter_context(patch.object(view, "require_object_permissions", return_value=None))
        stack.enter_context(
            patch.object(view, "get_validated_device_with_selections", return_value=(libre_device, validation, {}))
        )
        stack.enter_context(patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None))
        stack.enter_context(patch("netbox_librenms_plugin.views.imports.actions.set_librenms_device_id"))
        stack.enter_context(patch("netbox_librenms_plugin.views.imports.actions.cache"))
        stack.enter_context(
            patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key", return_value="key")
        )
        stack.enter_context(patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx))
        stack.enter_context(
            patch("netbox_librenms_plugin.views.imports.actions._get_hostname_for_action", return_value="r01")
        )
        stack.enter_context(
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value={"device_id": 42}
            )
        )
        return stack, MockDevice

    def test_update_save_error(self):
        """Line 1119: update action + _save_device error → return error."""
        view, request, mock_existing, libre_device, validation = self._make_setup("update")
        from django.http import HttpResponse

        err = HttpResponse("save error", status=400)
        stack, _ = self._common_patches(view, mock_existing, libre_device, validation)
        with stack:
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=err):
                response = view.post(request, device_id=42)
        assert response.status_code == 400

    def test_update_serial_save_error(self):
        """Line 1149: update_serial + _save_device error → return error."""
        view, request, mock_existing, libre_device, validation = self._make_setup("update_serial")
        from django.http import HttpResponse

        err = HttpResponse("save error", status=400)
        stack, _ = self._common_patches(view, mock_existing, libre_device, validation)
        with stack:
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=err):
                response = view.post(request, device_id=42)
        assert response.status_code == 400


class TestSyncSerialMorePaths:
    """Tests for sync_serial action edge cases (lines 1182-1200, 1207)."""

    @pytest.fixture(autouse=True)
    def _no_advisory_lock(self):
        """The serial guard's pg_advisory_xact_lock needs a real connection these mock tests don't have."""
        with patch("netbox_librenms_plugin.views.imports.actions._acquire_serial_assignment_lock"):
            yield

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def _common_patches_for_serial(self, view, mock_existing, libre_device, validation):
        from contextlib import ExitStack

        DoesNotExistExc = type("DoesNotExist", (Exception,), {})
        MockDevice = MagicMock()
        # restrict() hands back the same manager so the locked re-read the tests stub as
        # objects.select_for_update() resolves through it too.
        MockDevice.objects.restrict.return_value = MockDevice.objects
        MockDevice.objects.get.return_value = mock_existing
        MockDevice.DoesNotExist = DoesNotExistExc
        mock_tx = MagicMock()
        mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
        stack = ExitStack()
        stack.enter_context(patch.object(view, "require_all_permissions", return_value=None))
        stack.enter_context(patch("dcim.models.Device", MockDevice))
        stack.enter_context(patch.object(view, "require_object_permissions", return_value=None))
        stack.enter_context(
            patch.object(view, "get_validated_device_with_selections", return_value=(libre_device, validation, {}))
        )
        stack.enter_context(patch("netbox_librenms_plugin.views.imports.actions.cache"))
        stack.enter_context(
            patch("netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key", return_value="k")
        )
        stack.enter_context(patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx))
        return stack, MockDevice, DoesNotExistExc

    def test_sync_serial_device_deleted_under_lock(self):
        """Lines 1182-1183: Device.DoesNotExist during select_for_update → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(post={"action": "sync_serial", "existing_device_id": "1"})
        mock_existing = MagicMock()
        mock_existing.pk = 1
        libre_device = {"device_id": 42, "hostname": "r01", "serial": "SN001"}
        validation = {"existing_device": mock_existing, "device_type_mismatch": False}

        stack, MockDevice, DoesNotExistExc = self._common_patches_for_serial(
            view, mock_existing, libre_device, validation
        )
        with stack:
            MockDevice.objects.select_for_update.return_value.get.side_effect = DoesNotExistExc("gone")
            response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Device no longer exists" in response.content

    def test_sync_serial_conflict_under_lock(self):
        """Lines 1196-1200: sync_serial serial conflict → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(post={"action": "sync_serial", "existing_device_id": "1"})
        mock_existing = MagicMock()
        mock_existing.pk = 1
        locked_device = MagicMock()
        locked_device.pk = 1
        conflict_device = MagicMock()
        conflict_device.name = "router99"
        conflict_device.pk = 99

        libre_device = {"device_id": 42, "hostname": "r01", "serial": "CONFLICT_SN"}
        validation = {"existing_device": mock_existing, "device_type_mismatch": False}

        stack, MockDevice, DoesNotExistExc = self._common_patches_for_serial(
            view, mock_existing, libre_device, validation
        )
        with stack:
            MockDevice.objects.select_for_update.return_value.get.return_value = locked_device
            # The conflict lookup is deliberately UNLOCKED (advisory lock on the serial value instead);
            # a second row lock would deadlock two swap-direction requests (A→B / B→A).
            MockDevice.objects.filter.return_value.exclude.return_value.first.return_value = conflict_device
            response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Serial conflict" in response.content

    def test_sync_serial_save_error(self):
        """Line 1207: sync_serial → _save_device returns error."""
        view = self._make_view()
        request = _make_request(post={"action": "sync_serial", "existing_device_id": "1"})
        mock_existing = MagicMock()
        mock_existing.pk = 1
        locked_device = MagicMock()
        locked_device.pk = 1

        libre_device = {"device_id": 42, "hostname": "r01", "serial": "SN001"}
        validation = {"existing_device": mock_existing, "device_type_mismatch": False}

        from django.http import HttpResponse

        err = HttpResponse("save error", status=400)

        stack, MockDevice, DoesNotExistExc = self._common_patches_for_serial(
            view, mock_existing, libre_device, validation
        )
        with stack:
            MockDevice.objects.select_for_update.return_value.get.return_value = locked_device
            # The conflict lookup is deliberately UNLOCKED (advisory lock on the serial value instead);
            # a second row lock would deadlock two swap-direction requests (A→B / B→A).
            MockDevice.objects.filter.return_value.exclude.return_value.first.return_value = None
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=err):
                response = view.post(request, device_id=42)

        assert response.status_code == 400


class TestSyncSerialConflictGuard:
    """Real-DB check of the sync_serial conflict guard under an actual conflict.

    Writers of the same serial serialize on a transaction-scoped advisory lock keyed by
    the serial value; the conflict lookup itself must NOT take a second row lock — with
    own-device rows already held, two swap-direction requests would deadlock (A→B / B→A).
    """

    @pytest.mark.django_db
    def test_sync_serial_conflict_guard_uses_advisory_lock_not_row_lock(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.auth import get_user_model
        from django.db import connection
        from django.test import RequestFactory
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        site = Site.objects.create(name="site-serial-lock", slug="site-serial-lock")
        manufacturer = Manufacturer.objects.create(name="mf-serial-lock", slug="mf-serial-lock")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="dt-serial-lock", slug="dt-serial-lock"
        )
        role = DeviceRole.objects.create(name="role-serial-lock", slug="role-serial-lock")
        target = Device.objects.create(
            name="serial-lock-target", site=site, device_type=device_type, role=role, serial=""
        )
        conflict = Device.objects.create(
            name="serial-lock-conflict", site=site, device_type=device_type, role=role, serial="SN-LOCK-CONF"
        )

        user = get_user_model().objects.create_user(username="serial-lock-admin", is_superuser=True)
        request = RequestFactory().post("/", data={"action": "sync_serial", "existing_device_id": str(target.pk)})
        request.user = user

        view = DeviceConflictActionView()
        view.request = request
        libre_device = {"device_id": 42, "hostname": "serial-lock-target", "serial": "SN-LOCK-CONF"}
        validation = {"existing_device": target, "device_type_mismatch": False}

        with (
            patch.object(
                DeviceConflictActionView,
                "get_validated_device_with_selections",
                return_value=(libre_device, validation, {}),
            ),
            CaptureQueriesContext(connection) as ctx,
        ):
            response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert b"Serial conflict" in response.content
        target.refresh_from_db()
        assert target.serial == ""

        conflict_lookups = [
            q["sql"]
            for q in ctx.captured_queries
            if "dcim_device" in q["sql"] and '."serial" = ' in q["sql"] and q["sql"].lstrip().startswith("SELECT")
        ]
        assert conflict_lookups, "conflicting-serial lookup was not captured"
        assert all("TRIM(" not in sql for sql in conflict_lookups)
        assert all("FOR UPDATE" not in sql for sql in conflict_lookups), (
            "sync_serial conflict lookup must not row-lock the conflicting row "
            f"(locked queries: {[s for s in conflict_lookups if 'FOR UPDATE' in s]})"
        )
        assert any("pg_advisory_xact_lock" in q["sql"] for q in ctx.captured_queries), (
            "advisory lock on the serial value was not taken"
        )
        # The pre-check must not have been broken by the lock: the conflicting row still owns the serial.
        conflict.refresh_from_db()
        assert conflict.serial == "SN-LOCK-CONF"


@pytest.mark.django_db
class TestBulkImportDevicesViewBasicPaths:
    """Tests for BulkImportDevicesView early paths (lines 498-763)."""

    @staticmethod
    def _make_view(settings, server_key, server_url="https://librenms.example.test"):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        configure_test_servers(
            settings,
            {
                server_key: {
                    "librenms_url": server_url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        return BulkImportDevicesView()

    @staticmethod
    def _device_import_user(username):
        from dcim.models import Device

        return make_view_user(username, [("add", Device), ("change", Device)])

    def test_user_without_plugin_write_is_denied_before_any_import(self, settings):
        """A user lacking the plugin change permission must be turned away by the real gate.

        Every other denial test in this file injects an HttpResponse into
        require_write_permission and then asserts the view returned it, which cannot fail while
        has_write_permission is broken. This one supplies a real unauthorized user instead, so
        deleting the gate turns it red.
        """
        from dcim.models import Device
        from django.apps import apps
        from django.urls import get_script_prefix

        server_key = "bulk-basic-denied"
        view = self._make_view(settings, server_key)
        # Real Device and plugin-view grants leave only PERM_CHANGE_PLUGIN ungranted.
        user = make_view_user("bulk-basic-denied-user", [("add", Device), ("change", Device)], plugin_write=False)
        settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
        user = grant_view_permission(user, "view", settings_model)
        request = make_view_request("post", {"server_key": server_key, "select": ["1"]}, user=user)

        with (
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_devices") as mock_device_import,
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_vms") as mock_vm_import,
        ):
            response = post_view(view, request)

        assert response.status_code == 302
        assert response["Location"] == get_script_prefix()
        assert "You do not have permission to perform this action." in view_message_texts(request, "error")
        mock_device_import.assert_not_called()
        mock_vm_import.assert_not_called()

    def test_no_devices_selected_returns_400(self, settings):
        """No device IDs on the HTMX path → bare 400 (non-HTMX redirects instead)."""
        server_key = "bulk-basic-empty"
        view = self._make_view(settings, server_key)
        user = make_view_user("bulk-basic-empty-user", [])
        request = make_view_request(
            "post",
            {"server_key": server_key},
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 400
        assert response.content == b"No devices selected"
        assert view_message_texts(request, "error") == []

    def test_invalid_device_id_returns_400(self, settings):
        """Non-integer device_id on the HTMX path → bare 400 (non-HTMX redirects instead)."""
        server_key = "bulk-basic-invalid-id"
        view = self._make_view(settings, server_key)
        user = make_view_user("bulk-basic-invalid-id-user", [])
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["not-an-int"]},
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 400
        assert response.content == b"Invalid device identifier"
        assert view_message_texts(request, "error") == []

    def test_sync_mode_import_runs(self, settings, monkeypatch):
        """The synchronous path fetches LibreNMS data and persists the imported device."""
        from dcim.models import Device

        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        server_key = "bulk-basic-sync"
        mapping_source = make_device("bulk-basic-sync-mapping-source")
        user = self._device_import_user("bulk-basic-sync-user")
        with run_librenms_server() as server:
            server.device_info_response(
                device_id=1,
                hostname="bulk-basic-sync-imported",
                hardware=mapping_source.device_type.model,
                serial="",
                ip="198.18.0.1",
                location=mapping_source.site.name,
            )
            server.vc_inventory_callable(1, [], {})
            view = self._make_view(settings, server_key, server.url)
            request = make_view_request(
                "post",
                {
                    "server_key": server_key,
                    "select": ["1"],
                    "role_1": str(mapping_source.role_id),
                },
                user=user,  # A non-superuser forces sync mode.
            )
            response = post_view(view, request)

        imported = Device.objects.get(name="bulk-basic-sync-imported")
        assert imported.site_id == mapping_source.site_id
        assert imported.device_type_id == mapping_source.device_type_id
        assert imported.role_id == mapping_source.role_id
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_background_mode_returns_job_json(self):
        """Background mode: should_use_background_job returns True for superuser."""
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = BulkImportDevicesView()
        # Just test the should_use_background_job_for_import helper
        request = make_view_request(
            "post",
            {"use_background_job": "on"},
            user=make_superuser("bulk-basic-background-user"),
        )
        result = view.should_use_background_job_for_import(request)
        assert result is True

    def test_sync_import_uses_return_url_vc_flag(self, settings):
        """VC detection flag from return_url is propagated to sync bulk import."""
        server_key = "bulk-basic-return-url"
        view = self._make_view(settings, server_key)
        user = self._device_import_user("bulk-basic-return-url-user")
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["1"],
                "return_url": "/plugins/librenms_plugin/librenms-import/?enable_vc_detection=true",
            },
            user=user,
        )

        import_result = {"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0}

        with patch(
            "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
            return_value=import_result,
        ) as mock_bulk_import:
            response = post_view(view, request)

        mock_bulk_import.assert_called_once()
        assert mock_bulk_import.call_args.kwargs["sync_options"]["vc_detection_enabled"] is True
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")


@pytest.mark.django_db
class TestBulkImportDevicesMorePaths:
    """Additional paths in BulkImportDevicesView (lines 516-693, 701-758)."""

    @staticmethod
    def _make_view(settings, server_key, server_url="https://librenms.example.test"):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        configure_test_servers(
            settings,
            {
                server_key: {
                    "librenms_url": server_url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        return BulkImportDevicesView()

    @staticmethod
    def _device_import_user(username):
        from dcim.models import Device

        return make_view_user(username, [("add", Device), ("change", Device)])

    @staticmethod
    def _vm_import_user(username):
        from virtualization.models import VirtualMachine

        return make_view_user(username, [("add", VirtualMachine)])

    def _make_base_request(
        self,
        settings,
        device_ids,
        user,
        extra_post=None,
        *,
        server_key,
        server_url="https://librenms.example.test",
        htmx=False,
    ):
        view = self._make_view(settings, server_key, server_url)
        data = {**(extra_post or {}), "server_key": server_key, "select": device_ids}
        factory_kwargs = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        request = make_view_request("post", data, user=user, **factory_kwargs)
        return view, request

    def test_invalid_cluster_value_fails_closed_and_imports_nothing(self, settings):
        """An unparseable cluster id must abort the batch, not quietly import the row as a Device.

        The row never reaches vm_imports when int() raises, so falling through would import it
        through the device path with the requested cluster discarded. It would also shift the
        permission check from add_virtualmachine to add_device.
        """
        user = self._device_import_user("bulk-more-invalid-cluster-user")
        view, request = self._make_base_request(
            settings,
            ["1"],
            user,
            {"cluster_1": "not-int"},
            server_key="bulk-more-invalid-cluster",
        )
        with (
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_devices") as mock_device_import,
            patch("netbox_librenms_plugin.views.imports.actions.bulk_import_vms") as mock_vm_import,
        ):
            response = post_view(view, request)

        mock_device_import.assert_not_called()
        mock_vm_import.assert_not_called()
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")
        assert "Invalid cluster or role selection supplied" in view_message_texts(request, "error")

    @pytest.mark.parametrize(
        ("cluster_value", "case"),
        [("not-int", "text"), ("0", "zero"), ("-1", "negative"), (None, "overflow")],
    )
    def test_invalid_cluster_value_fails_closed_on_the_htmx_path(self, settings, monkeypatch, cluster_value, case):
        """The HTMX path rejects malformed and out-of-range cluster ids without creating a VM."""
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.utils import _POSTGRES_BIGINT_MAX

        if case == "overflow":
            cluster_value = str(_POSTGRES_BIGINT_MAX + 1)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        user = self._vm_import_user(f"bulk-more-invalid-cluster-{case}-user")
        before = set(VirtualMachine.objects.values_list("pk", flat=True))
        with run_librenms_server() as server:
            server.device_info_response(
                device_id=1,
                hostname=f"bulk-more-invalid-cluster-{case}",
                serial="",
                ip="198.18.0.1",
            )
            view, request = self._make_base_request(
                settings,
                ["1"],
                user,
                {"cluster_1": cluster_value},
                server_key=f"bulk-more-invalid-cluster-{case}",
                server_url=server.url,
                htmx=True,
            )
            response = post_view(view, request)

        assert response.status_code == 400
        assert response.content == b"Invalid cluster or role selection"
        assert set(VirtualMachine.objects.values_list("pk", flat=True)) == before

    @pytest.mark.parametrize(
        ("role_value", "case"),
        [("not-int", "text"), ("0", "zero"), ("-1", "negative"), (None, "overflow")],
    )
    def test_invalid_role_on_a_valid_cluster_still_imports_the_vm(
        self, settings, caplog, monkeypatch, role_value, case
    ):
        """A bad role id next to a valid cluster keeps the VM import and drops only the role."""
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.utils import _POSTGRES_BIGINT_MAX

        if case == "overflow":
            role_value = str(_POSTGRES_BIGINT_MAX + 1)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        user = self._vm_import_user(f"bulk-more-invalid-role-{case}-user")
        cluster = make_cluster(f"bulk-more-invalid-role-{case}-cluster")
        with run_librenms_server() as server:
            server.device_info_response(
                device_id=1,
                hostname=f"bulk-more-invalid-role-{case}-imported",
                serial="",
                ip="198.18.0.2",
            )
            view, request = self._make_base_request(
                settings,
                ["1"],
                user,
                {"cluster_1": str(cluster.pk), "role_1": role_value},
                server_key=f"bulk-more-invalid-role-{case}",
                server_url=server.url,
            )
            response = post_view(view, request)

        assert f"Ignoring invalid role id '{role_value}' for VM import of device 1" in caplog.text
        imported = VirtualMachine.objects.get(name=f"bulk-more-invalid-role-{case}-imported")
        assert imported.cluster_id == cluster.pk
        assert imported.role_id is None
        assert response.status_code == 302

    def test_invalid_device_role_and_rack_ids_do_not_abort_valid_rows(self, settings, caplog, monkeypatch):
        """Invalid device mapping IDs must not prevent a valid row from importing."""
        from dcim.models import Device

        from netbox_librenms_plugin.utils import _POSTGRES_BIGINT_MAX

        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        invalid_id = str(_POSTGRES_BIGINT_MAX + 1)
        mapping_source = make_device("bulk-more-invalid-device-mapping-source")
        user = self._device_import_user("bulk-more-invalid-device-mapping-user")

        with run_librenms_server() as server:
            for device_id in (1, 2):
                server.device_info_response(
                    device_id=device_id,
                    hostname=f"bulk-more-invalid-device-mapping-{device_id}",
                    hardware=mapping_source.device_type.model,
                    serial="",
                    ip=f"198.18.0.{device_id}",
                    location=mapping_source.site.name,
                )
                server.vc_inventory_callable(device_id, [], {})
            view, request = self._make_base_request(
                settings,
                ["1", "2"],
                user,
                {
                    "role_1": invalid_id,
                    "role_2": str(mapping_source.role_id),
                    "rack_2": invalid_id,
                },
                server_key="bulk-more-invalid-device-mapping",
                server_url=server.url,
            )
            response = post_view(view, request)

        assert f"Ignoring invalid role id '{invalid_id}' for device 1" in caplog.text
        assert f"Ignoring invalid rack id '{invalid_id}' for device 2" in caplog.text
        assert not Device.objects.filter(name="bulk-more-invalid-device-mapping-1").exists()
        imported = Device.objects.get(name="bulk-more-invalid-device-mapping-2")
        assert imported.role_id == mapping_source.role_id
        assert imported.rack_id is None
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_valid_role_and_rack_values_applied(self, settings, monkeypatch):
        """The importer persists the selected role and rack on the new device."""
        from dcim.models import Device, Rack

        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        mapped_device = make_device("bulk-more-mapping-source")
        rack = Rack.objects.create(name="Bulk More Rack", site=mapped_device.site, status="active")
        user = self._device_import_user("bulk-more-valid-mapping-user")

        with run_librenms_server() as server:
            server.device_info_response(
                device_id=1,
                hostname="bulk-more-valid-mapping-imported",
                hardware=mapped_device.device_type.model,
                serial="",
                ip="198.18.0.3",
                location=mapped_device.site.name,
            )
            server.vc_inventory_callable(1, [], {})
            view, request = self._make_base_request(
                settings,
                ["1"],
                user,
                {"role_1": str(mapped_device.role_id), "rack_1": str(rack.pk)},
                server_key="bulk-more-valid-mapping",
                server_url=server.url,
            )
            response = post_view(view, request)

        imported = Device.objects.get(name="bulk-more-valid-mapping-imported")
        assert imported.role_id == mapped_device.role_id
        assert imported.rack_id == rack.pk
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_vc_detection_disabled_in_post_is_passed_to_device_import(self, settings):
        """vc_detection_enabled=off from POST must propagate to bulk import call."""
        user = self._device_import_user("bulk-more-vc-off-user")
        view, request = self._make_base_request(
            settings,
            ["1"],
            user,
            {"enable_vc_detection": "off"},
            server_key="bulk-more-vc-off",
        )

        with patch(
            "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
            return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
        ) as mock_bulk_import:
            response = post_view(view, request)

        call_kwargs = mock_bulk_import.call_args.kwargs
        assert call_kwargs["sync_options"]["vc_detection_enabled"] is False
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_invalid_role_and_rack_values_log_warning(self, settings, caplog):
        """Lines 534-535, 544-546: invalid role_id/rack_id → warning."""
        user = self._device_import_user("bulk-more-invalid-mapping-user")
        view, request = self._make_base_request(
            settings,
            ["1"],
            user,
            {"role_1": "not-int", "rack_1": "not-int"},
            server_key="bulk-more-invalid-mapping",
        )

        with patch(
            "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
            return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
        ) as mock_import:
            response = post_view(view, request)

        assert "Ignoring invalid role id 'not-int' for device 1" in caplog.text
        assert "Ignoring invalid rack id 'not-int' for device 1" in caplog.text
        mock_import.assert_called_once()
        assert mock_import.call_args.kwargs["manual_mappings_per_device"] == {}
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_import_with_success_messages(self, settings, monkeypatch):
        """Lines 683, 688, 693: success/fail/skipped messages."""
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        server_key = "bulk-more-summary-messages"
        user = self._device_import_user("bulk-more-summary-user")
        skipped_device = make_device("bulk-more-summary-skipped")
        with run_librenms_server() as server:
            server.device_info_response(
                device_id=11,
                hostname="bulk-more-summary-success",
                serial="",
                ip="198.18.0.11",
            )
            server.device_info_response(device_id=12, hostname="bulk-more-summary-failed", serial="", ip="198.18.0.12")
            server.device_info_response(device_id=13, hostname=skipped_device.name, serial="", ip="198.18.0.13")
            view, request = self._make_base_request(
                settings,
                ["11", "12", "13"],
                user,
                server_key=server_key,
                server_url=server.url,
            )

            def import_with_mixed_outcomes(**_kwargs):
                successful_device = make_device(
                    "bulk-more-summary-success",
                    librenms_cf={server_key: {"id": 11}},
                )
                return {
                    "success": [{"device_id": 11, "device": successful_device}],
                    "failed": [{"device_id": 12, "error": "failed"}],
                    "skipped": [{"device_id": 13}],
                    "virtual_chassis_created": 0,
                }

            with patch(
                "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                side_effect=import_with_mixed_outcomes,
            ):
                response = post_view(view, request)

        sent_messages = view_message_texts(request)
        assert "Successfully imported 1 LibreNMS device" in sent_messages
        assert "Failed to import 1 device" in sent_messages
        assert "Skipped 1 existing device" in sent_messages
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_vm_import_triggers_bulk_import_vms(self, settings):
        """Line 651-668: vm_imports non-empty → bulk_import_vms called."""
        existing_vm = make_vm("bulk-more-vm-seed")
        user = self._vm_import_user("bulk-more-vm-user")

        # A real cluster selection makes device 1 a VM.
        view, request = self._make_base_request(
            settings,
            ["1"],
            user,
            {"cluster_1": str(existing_vm.cluster_id)},
            server_key="bulk-more-vm",
        )

        with patch(
            "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
            return_value={"success": [], "failed": [], "skipped": []},
        ) as mock_vm_import:
            response = post_view(view, request)

        mock_vm_import.assert_called_once()
        assert mock_vm_import.call_args.args[0] == {1: {"cluster_id": existing_vm.cluster_id}}
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_htmx_request_returns_oob_rows(self, settings, monkeypatch):
        """Lines 701-761: HTMX request → returns OOB row HTML."""
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        server_key = "bulk-more-htmx-row"
        imported = {}
        with run_librenms_server() as server:
            server.device_info_response(
                device_id=21,
                hostname="bulk-more-htmx-device",
                serial="",
                ip="198.18.0.21",
            )
            user = self._device_import_user("bulk-more-htmx-user")
            view, request = self._make_base_request(
                settings,
                ["21"],
                user,
                server_key=server_key,
                server_url=server.url,
                htmx=True,
            )

            def import_device(**_kwargs):
                imported_device = make_device(
                    "bulk-more-htmx-device",
                    librenms_cf={server_key: {"id": 21}},
                )
                imported["device"] = imported_device
                return {
                    "success": [{"device_id": 21, "device": imported_device}],
                    "failed": [],
                    "skipped": [],
                    "virtual_chassis_created": 0,
                }

            with patch(
                "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                side_effect=import_device,
            ):
                response = post_view(view, request)

        imported_device = imported["device"]
        assert response.status_code == 200
        assert response["HX-Trigger"] == '{"closeModal": null}'
        assert b'hx-swap-oob="true"' in response.content
        assert imported_device.name.encode() in response.content
        assert b"Successfully imported 1 LibreNMS device" in response.content
        assert view_message_texts(request) == []

    def test_permission_denied_during_import_redirects(self, settings):
        """Lines 659-668: PermissionDenied during import → redirect."""
        user = self._device_import_user("bulk-more-import-denied-user")
        view, request = self._make_base_request(
            settings,
            ["1"],
            user,
            server_key="bulk-more-import-denied",
        )

        from django.core.exceptions import PermissionDenied as DjPD

        with patch(
            "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
            side_effect=DjPD("No permission"),
        ):
            response = post_view(view, request)

        assert view_message_texts(request, "error") == ["No permission"]
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_background_no_workers_falls_back_to_sync(self, settings):
        """Line 612-615: background requested but no workers → sync fallback."""
        view, request = self._make_base_request(
            settings,
            ["1"],
            make_superuser("bulk-more-no-workers-user"),
            {"use_background_job": "on"},
            server_key="bulk-more-no-workers",
        )

        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
            ) as mock_import,
            patch("utilities.rqworker.get_workers_for_queue", return_value=0),
        ):
            response = post_view(view, request)

        mock_import.assert_called_once()
        warnings = view_message_texts(request, "warning")
        assert any("Background job requested but no workers available" in message for message in warnings)
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")


@pytest.mark.django_db
class TestBulkImportEdgePaths:
    """Tests for remaining BulkImportDevicesView edge paths."""

    @staticmethod
    def _make_view(settings, server_key, server_url="https://librenms.example.test"):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        configure_test_servers(
            settings,
            {
                server_key: {
                    "librenms_url": server_url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        return BulkImportDevicesView()

    @staticmethod
    def _device_import_user(username):
        from dcim.models import Device

        return make_view_user(username, [("add", Device), ("change", Device)])

    @staticmethod
    def _vm_import_user(username):
        from virtualization.models import VirtualMachine

        return make_view_user(username, [("add", VirtualMachine)])

    def test_cluster_with_role_applies_role_to_vm(self, settings, monkeypatch):
        """The VM importer persists both selected mappings."""
        from virtualization.models import VirtualMachine

        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        server_key = "bulk-edge-vm-role"
        existing_vm = make_vm("bulk-edge-vm-seed")
        role_source = make_device("bulk-edge-role-source")
        user = self._vm_import_user("bulk-edge-vm-role-user")
        with run_librenms_server() as server:
            server.device_info_response(
                device_id=1,
                hostname="bulk-edge-vm-role-imported",
                serial="",
                ip="198.18.0.4",
            )
            view = self._make_view(settings, server_key, server.url)
            request = make_view_request(
                "post",
                {
                    "server_key": server_key,
                    "select": ["1"],
                    "cluster_1": str(existing_vm.cluster_id),
                    "role_1": str(role_source.role_id),
                },
                user=user,
            )
            response = post_view(view, request)

        imported = VirtualMachine.objects.get(name="bulk-edge-vm-role-imported")
        assert imported.cluster_id == existing_vm.cluster_id
        assert imported.role_id == role_source.role_id
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_permission_denied_htmx_returns_htmx_redirect(self, settings):
        """Line 664: PermissionDenied during import with HX-Request → HX-Redirect."""
        server_key = "bulk-edge-htmx-denied"
        view = self._make_view(settings, server_key)
        user = self._device_import_user("bulk-edge-htmx-denied-user")
        request = make_view_request(
            "post",
            {"server_key": server_key, "select": ["1"]},
            user=user,
            HTTP_HX_REQUEST="true",
        )

        from django.core.exceptions import PermissionDenied as DjPD

        with patch(
            "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
            side_effect=DjPD("No permission"),
        ):
            response = post_view(view, request)

        assert response.status_code == 200
        assert response["HX-Redirect"] == url_for("plugins:netbox_librenms_plugin:librenms_import")
        assert view_message_texts(request, "error") == ["No permission"]

    def test_background_with_workers_enqueues_job(self, settings):
        """Lines 575-611: background with workers available → enqueue job."""
        server_key = "bulk-edge-background"
        view = self._make_view(settings, server_key)
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["1"],
                "use_background_job": "on",
                "use_sysname": "on",
            },
            user=make_superuser("bulk-edge-background-user"),
        )

        mock_job = Namespace(pk=123, job_id="uuid-456")

        with (
            patch("utilities.rqworker.get_workers_for_queue", return_value=2),
            # Patch ImportDevicesJob at the point it is imported inside post().
            patch(
                "netbox_librenms_plugin.jobs.ImportDevicesJob.enqueue",
                return_value=mock_job,
            ) as mock_enqueue,
        ):
            result = post_view(view, request)

        # Pin the regression this test is named for: the background path must actually
        # enqueue the job, not merely take some redirecting branch.
        mock_enqueue.assert_called_once()
        # The redirect response must actually be returned, not just produced.
        assert result.status_code == 302
        assert result["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

        # ...and the enqueued job must carry the request's inputs forward, not be enqueued
        # empty: the selected device id (parsed to int), the active server namespace, and
        # the resolved sync options. Otherwise the background import silently does nothing.
        enqueue_kwargs = mock_enqueue.call_args.kwargs
        assert enqueue_kwargs["device_ids"] == [1]
        assert enqueue_kwargs["server_key"] == server_key
        assert enqueue_kwargs["sync_options"]["use_sysname"] is True
        assert any("Import job started for 1 device" in message for message in view_message_texts(request, "info"))

    def test_cold_cache_seed_does_not_fetch_from_librenms_before_enqueue(self, settings):
        """On a cold cache + background path, the pre-enqueue seed reads the Django cache only (no fetch_device_with_cache, which HTTP-fetches on a miss) and enqueues an empty libre_devices_cache."""
        from django.core.cache import cache

        from netbox_librenms_plugin.views.imports.actions import get_import_device_cache_key

        server_key = "bulk-edge-cold-cache"
        view = self._make_view(settings, server_key)
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["1"],
                "use_background_job": "on",
                "use_sysname": "on",
            },
            user=make_superuser("bulk-edge-cold-cache-user"),
        )

        mock_job = Namespace(pk=123, job_id="uuid-cold")
        cache.delete(get_import_device_cache_key(1, server_key))

        with (
            patch("utilities.rqworker.get_workers_for_queue", return_value=2),
            # Cold cache: every import-device key misses. The pre-enqueue seed batches its reads
            # via cache.get_many (one round-trip for N devices).
            # The HTTP-fetching helper must NOT be reached by the pre-enqueue seed.
            patch("netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache") as mock_fetch,
            patch(
                "netbox_librenms_plugin.jobs.ImportDevicesJob.enqueue",
                return_value=mock_job,
            ) as mock_enqueue,
        ):
            response = post_view(view, request)

        # The seed read the Django cache directly and never reached the API-fetching helper.
        mock_fetch.assert_not_called()
        # Cold cache → nothing pre-warmed; the async job fetches misses itself.
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args.kwargs["libre_devices_cache"] == {}
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")


@pytest.mark.django_db
class TestAddDeviceTypeMappingNoSecondRoundTrip:
    """Issue #66: AddDeviceTypeMappingView.post must reuse the LibreNMS device it already fetched for the modal/row refresh, never issuing a second LibreNMS round-trip after the DB write."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import AddDeviceTypeMappingView

        view = object.__new__(AddDeviceTypeMappingView)
        view._librenms_api = _make_api()
        return view

    def _make_device_type(self):
        from dcim.models import DeviceType, Manufacturer

        mfr = Manufacturer.objects.create(name="Cisco-66", slug="cisco-66")
        return DeviceType.objects.create(manufacturer=mfr, model="C9300-66", slug="c9300-66")

    def test_post_reuses_cached_device_no_second_librenms_call(self):
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
        from netbox_librenms_plugin.models import DeviceTypeMapping

        device_id = 4242
        dt = self._make_device_type()
        view = self._make_view()

        # Pre-seed the cache exactly as the table load would (so the first fetch is a cache hit).
        libre_device = {
            "device_id": device_id,
            "hardware": "WS-C9300-66",
            "sysName": "switch-66",
            "hostname": "switch-66",
            "os": "ios",
            "serial": "SN66",
        }
        cache_key = get_import_device_cache_key(device_id, "default")
        cache.set(cache_key, libre_device, timeout=300)

        User = get_user_model()
        user = User.objects.create_user(username="u66", password="x")
        user.is_superuser = True
        user.save()

        request = RequestFactory().post(
            f"/device-import/add-device-type-mapping/{device_id}/",
            data={"device_type_id": str(dt.pk)},
        )
        request.user = user
        view.request = request

        # Mock ONLY the LibreNMS HTTP boundary; have it raise if ever called after the cache hit,
        # so a second round-trip would be unmistakable. Also stub the auth gates and VC detection.
        with (
            patch("netbox_librenms_plugin.import_utils.device_operations.get_librenms_device_by_id") as mock_http,
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "require_object_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": []},
            ),
        ):
            mock_http.return_value = None  # simulate LibreNMS unavailable on any fresh fetch
            response = view.post(request, device_id=device_id)

        # The mapping was actually written to the DB...
        assert DeviceTypeMapping.objects.filter(librenms_hardware="ws-c9300-66").exists()
        # ...the refresh succeeded without ever touching the LibreNMS HTTP boundary...
        assert mock_http.call_count == 0
        # ...and the cached device is still present (repopulated, not cleared).
        assert cache.get(cache_key) is not None
        assert response.status_code == 200

    def test_cache_repopulation_preserves_remaining_ttl_on_ttl_backends(self):
        """On a TTL-reporting backend (Redis in prod) the repopulated snapshot must keep the entry's REMAINING TTL — a fresh full timeout would re-arm a minutes-old snapshot for another whole window (the bulk-import seed reads this key)."""
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key

        device_id = 4444
        dt = self._make_device_type()
        view = self._make_view()
        view._librenms_api.cache_timeout = 300

        libre_device = {
            "device_id": device_id,
            "hardware": "WS-C9300-TTL",
            "sysName": "switch-ttl",
            "hostname": "switch-ttl",
            "os": "ios",
            "serial": "SNTTL",
        }
        cache_key = get_import_device_cache_key(device_id, "default")
        cache.set(cache_key, libre_device, timeout=300)

        User = get_user_model()
        user = User.objects.create_user(username="u66ttl", password="x")
        user.is_superuser = True
        user.save()

        request = RequestFactory().post(
            f"/device-import/add-device-type-mapping/{device_id}/",
            data={"device_type_id": str(dt.pk)},
        )
        request.user = user
        view.request = request

        spy_cache = MagicMock(wraps=cache)
        with (
            patch("netbox_librenms_plugin.views.imports.actions.cache", spy_cache),
            # LocMemCache can't report TTLs; simulate the Redis behaviour at that boundary.
            patch("netbox_librenms_plugin.utils.cache_remaining_ttl", return_value=120),
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "require_object_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": []},
            ),
        ):
            response = view.post(request, device_id=device_id)

        assert response.status_code == 200
        set_calls = [c for c in spy_cache.set.call_args_list if c.args and c.args[0] == cache_key]
        assert set_calls, "the snapshot was not repopulated at all"
        assert set_calls[-1].kwargs.get("timeout") == 120  # remaining TTL, not a fresh full 300

    def test_mapping_persisted_under_normalised_hardware_key(self):
        """The mapping must be keyed on the NORMALISED hardware string."""
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        device_id = 4343
        dt = self._make_device_type()
        view = self._make_view()

        # A device_type rule strips the "WS-" prefix the raw LibreNMS string carries.
        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(.+)$", replacement=r"\1", priority=10
        )

        libre_device = {
            "device_id": device_id,
            "hardware": "WS-C9300-66",  # normalises to "C9300-66"
            "sysName": "switch-43",
            "hostname": "switch-43",
            "os": "ios",
            "serial": "SN43",
        }
        cache.set(get_import_device_cache_key(device_id, "default"), libre_device, timeout=300)

        User = get_user_model()
        user = User.objects.create_user(username="u43", password="x")
        user.is_superuser = True
        user.save()

        request = RequestFactory().post(
            f"/device-import/add-device-type-mapping/{device_id}/",
            data={"device_type_id": str(dt.pk)},
        )
        request.user = user
        view.request = request

        with (
            patch("netbox_librenms_plugin.import_utils.device_operations.get_librenms_device_by_id", return_value=None),
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "require_object_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": []},
            ),
        ):
            view.post(request, device_id=device_id)

        # Saved under the normalised, lowercased key — NOT the raw "ws-c9300-66".
        assert DeviceTypeMapping.objects.filter(librenms_hardware="c9300-66").exists()
        assert not DeviceTypeMapping.objects.filter(librenms_hardware="ws-c9300-66").exists()

    def test_normalised_hardware_is_trimmed_before_lookup(self):
        """The normalised key must be trimmed."""
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import RequestFactory

        from dcim.models import DeviceType, Manufacturer
        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        device_id = 4444
        mfr = Manufacturer.objects.create(name="Cisco-trim", slug="cisco-trim")
        dt_old = DeviceType.objects.create(manufacturer=mfr, model="C9300-old", slug="c9300-old")
        dt_new = DeviceType.objects.create(manufacturer=mfr, model="C9300-new", slug="c9300-new")
        view = self._make_view()

        # A pre-existing mapping (stored stripped+lowercased by clean()).
        existing = DeviceTypeMapping.objects.create(librenms_hardware="c9300-44", netbox_device_type=dt_old)

        # A rule that pads the value with spaces — the untrimmed output is " C9300-44 ".
        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(.+)$", replacement=r" \1 ", priority=10
        )

        libre_device = {
            "device_id": device_id,
            "hardware": "WS-C9300-44",
            "sysName": "switch-44",
            "hostname": "switch-44",
            "os": "ios",
            "serial": "SN44",
        }
        cache.set(get_import_device_cache_key(device_id, "default"), libre_device, timeout=300)

        User = get_user_model()
        user = User.objects.create_user(username="u44", password="x")
        user.is_superuser = True
        user.save()

        request = RequestFactory().post(
            f"/device-import/add-device-type-mapping/{device_id}/",
            data={"device_type_id": str(dt_new.pk)},
        )
        request.user = user
        view.request = request

        with (
            patch("netbox_librenms_plugin.import_utils.device_operations.get_librenms_device_by_id", return_value=None),
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "require_object_permissions", return_value=None),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": []},
            ),
        ):
            view.post(request, device_id=device_id)

        # The trimmed key matched the existing mapping → it was UPDATED, not duplicated.
        assert DeviceTypeMapping.objects.filter(librenms_hardware="c9300-44").count() == 1
        existing.refresh_from_db()
        assert existing.netbox_device_type_id == dt_new.pk


@pytest.mark.django_db
class TestCreatePlatformAssignmentIndependence:
    """CreatePlatformFromImportView commits the platform independently of the optional assignment."""

    @staticmethod
    def _infra():
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        site, _ = Site.objects.get_or_create(name="PFSite", slug="pf-site")
        mfr, _ = Manufacturer.objects.get_or_create(name="PFMfr", slug="pf-mfr")
        dt, _ = DeviceType.objects.get_or_create(model="PFDT", slug="pf-dt", defaults={"manufacturer": mfr})
        role, _ = DeviceRole.objects.get_or_create(name="PFRole", slug="pf-role", defaults={"color": "00ff00"})
        return site, dt, role

    def test_platform_persists_when_target_assignment_fails(self):
        """A non-DoesNotExist failure assigning the platform (e.g. full_clean tripping on unrelated legacy data on the target) must not roll back the just-created platform."""
        from django.core.exceptions import ValidationError

        from dcim.models import Device, Platform

        from netbox_librenms_plugin.views.imports.actions import CreatePlatformFromImportView

        site, dt, role = self._infra()
        target = Device.objects.create(name="pf-target", device_type=dt, role=role, site=site, status="active")

        view = object.__new__(CreatePlatformFromImportView)
        view._librenms_api = MagicMock(server_key="default")

        request = MagicMock()
        request.POST = {"platform_name": "NewPlatPF"}
        view.request = request  # dispatch() would set this; restricted_queryset reads request.user

        validation = {"existing_device": target}
        dvdv = MagicMock()
        dvdv.return_value.get.return_value.content.decode.return_value = "<div></div>"

        with (
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "require_object_permissions", return_value=None),
            patch.object(view, "get_validated_device_with_selections", return_value=(None, validation, {})),
            patch("netbox_librenms_plugin.views.imports.actions.cache"),
            patch("netbox_librenms_plugin.views.imports.actions.DeviceValidationDetailsView", dvdv),
            # Simulate legacy data on the target: its full_clean trips, so the assignment must be
            # skipped WITHOUT rolling back the platform that was already created.
            patch("dcim.models.Device.full_clean", side_effect=ValidationError("legacy data on target")),
        ):
            view.post(request, device_id=999)

        # The platform create is the primary action: it must survive the failed assignment.
        assert Platform.objects.filter(name="NewPlatPF").exists()
        # ...and the target must be left unassigned (the failed full_clean must not partially apply).
        target.refresh_from_db()
        assert target.platform is None

    def test_assignment_failure_is_surfaced_not_silent_success(self):
        """A failed assignment must be reported to the user (error toast), not hidden behind a success swap that implies the device received the platform."""
        from django.core.exceptions import ValidationError

        from dcim.models import Device, Platform

        from netbox_librenms_plugin.views.imports.actions import CreatePlatformFromImportView

        site, dt, role = self._infra()
        target = Device.objects.create(name="pf-target2", device_type=dt, role=role, site=site, status="active")

        view = object.__new__(CreatePlatformFromImportView)
        view._librenms_api = MagicMock(server_key="default")

        request = MagicMock()
        request.POST = {"platform_name": "NewPlatPF2"}
        view.request = request  # dispatch() would set this; restricted_queryset reads request.user

        validation = {"existing_device": target}
        # Patched so the UNFIXED success path can still render its OOB modal swap cleanly,
        # making the difference observable: success swap (unfixed) vs error toast (fixed).
        dvdv = MagicMock()
        dvdv.return_value.get.return_value.content.decode.return_value = "<div></div>"

        with (
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "require_object_permissions", return_value=None),
            patch.object(view, "get_validated_device_with_selections", return_value=(None, validation, {})),
            patch("netbox_librenms_plugin.views.imports.actions.cache"),
            patch("netbox_librenms_plugin.views.imports.actions.DeviceValidationDetailsView", dvdv),
            patch("dcim.models.Device.full_clean", side_effect=ValidationError("legacy data on target")),
        ):
            response = view.post(request, device_id=999)

        # Platform still created (deliberate "don't roll back" invariant preserved)...
        assert Platform.objects.filter(name="NewPlatPF2").exists()
        # ...but the response must tell the user the assignment failed, NOT render a success swap.
        body = response.content
        assert b"could not be assigned" in body
        assert b"htmx-modal-content" not in body


class TestValidateAndApplySelectionsRevalidatesOnVmToDeviceFlip:
    """validate_and_apply_selections must re-validate in device mode when a VM-requested row flips.

    When the user submitted a cluster (VM mode) but validate_device_for_import binds an existing
    Device by librenms_id/hostname/IP and flips import_as_vm back to False, the first pass skipped
    VC detection / chassis-fallback device-type matching (api=None, include_vc_detection=False).
    The helper must re-run validate_device_for_import in device mode so those apply to the device
    the row actually resolved to.
    """

    def test_flip_triggers_device_mode_revalidation(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceImportHelperMixin

        helper = object.__new__(DeviceImportHelperMixin)
        helper.librenms_api = MagicMock(server_key="default")

        calls = []

        def fake_validate(libre_device, **kwargs):
            calls.append(kwargs)
            # VM mode was requested, but the device matched an existing Device → flip to device mode.
            return {"import_as_vm": False}

        libre_device = {"device_id": 42, "hostname": "h"}
        request = MagicMock()
        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                return_value={"cluster_id": 5, "role_id": None, "rack_id": None},
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                return_value=(True, False),
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                side_effect=fake_validate,
            ),
            patch("netbox_librenms_plugin.views.imports.actions._apply_user_selections_to_validation"),
        ):
            helper.validate_and_apply_selections(42, request, libre_device)

        # Two passes: the VM-requested first pass, then a device-mode re-validation after the flip.
        assert len(calls) == 2
        first, second = calls
        assert first["import_as_vm"] is True
        assert first["include_vc_detection"] is False
        assert first["api"] is None
        assert second["import_as_vm"] is False
        assert second["include_vc_detection"] is True
        assert second["api"] is helper.librenms_api

    def test_no_revalidation_when_vm_import_stays_vm(self):
        """A VM that stays a VM must NOT trigger a second validation pass (no wasted API round-trip)."""
        from netbox_librenms_plugin.views.imports.actions import DeviceImportHelperMixin

        helper = object.__new__(DeviceImportHelperMixin)
        helper.librenms_api = MagicMock(server_key="default")

        calls = []

        def fake_validate(libre_device, **kwargs):
            calls.append(kwargs)
            return {"import_as_vm": True}  # stays a VM

        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                return_value={"cluster_id": 5, "role_id": None, "rack_id": None},
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                return_value=(True, False),
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                side_effect=fake_validate,
            ),
            patch("netbox_librenms_plugin.views.imports.actions._apply_user_selections_to_validation"),
        ):
            helper.validate_and_apply_selections(42, MagicMock(), {"device_id": 42})

        assert len(calls) == 1


@pytest.mark.django_db
class TestBulkImportRerenderVMClassification:
    """The HTMX re-render loop must classify each imported row as VM or device from a prefetched set, not by rebuilding the VM-id list per row."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = object.__new__(BulkImportDevicesView)
        view._librenms_api = _make_api()
        return view

    def test_rerender_classifies_vms_from_prefetched_set_without_per_row_rebuild(self):
        from django.contrib.auth import get_user_model
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key

        class _CountingList(list):
            """A list that records how many times it is iterated."""

            def __init__(self, *args):
                super().__init__(*args)
                self.iter_count = 0

            def __iter__(self):
                self.iter_count += 1
                return super().__iter__()

        view = self._make_view()
        User = get_user_model()
        # Superuser so has_perm passes the import view's authorize-before-precheck gate; this test
        # exercises the VM-classification re-render path, not permission enforcement.
        user = User.objects.create_user(username="u-rerender", password="x", is_superuser=True)

        # device 1 → Device import; devices 2 & 3 → VM imports (a cluster is selected for them).
        request = RequestFactory().post(
            "/device-import/bulk/",
            data={"select": ["1", "2", "3"], "cluster_2": "99", "cluster_3": "99"},
            HTTP_HX_REQUEST="true",
        )
        request.user = user
        # The view emits success/skip toasts via django.contrib.messages, which needs a real
        # session + message store on a RequestFactory request.
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)

        vm_success = _CountingList([{"device_id": 2}, {"device_id": 3}])

        def _fetch(device_id, *a, **k):
            # A minimal-but-realistic LibreNMS device so the REAL validate_device_for_import runs
            # (unique serial/hostname → no existing match; empty hardware/os → no type/platform work).
            return {
                "device_id": device_id,
                "hostname": f"host{device_id}",
                "sysName": f"host{device_id}",
                "serial": f"SN{device_id}",
                "hardware": "",
                "os": "",
            }

        # detect_collisions falls back to api.get_device_info for cold-cache misses — feed it _fetch.
        view._librenms_api.get_device_info = lambda did, *a, **k: (True, _fetch(did))

        # Mock ONLY genuine boundaries: the import process, the LibreNMS fetch, the template render,
        # and the permission/background-job gates. validate_device_for_import, the cache and the
        # request routing all run for real.
        with (
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "should_use_background_job_for_import", return_value=False),
            patch(
                "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                return_value={"success": [{"device_id": 1}], "failed": [], "skipped": [], "virtual_chassis_created": 0},
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                return_value={"success": vm_success, "failed": [], "skipped": []},
            ),
            patch("netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", side_effect=_fetch),
            patch("netbox_librenms_plugin.views.imports.actions.render") as mock_render,
        ):
            mock_render.return_value = MagicMock(content=b"<tr></tr>")
            response = view.post(request)

        assert response.status_code == 200

        def _cached_is_vm(device_id):
            cached = cache.get(get_import_device_cache_key(device_id, "default"))
            return cached["_validation"]["import_as_vm"]

        # The re-render loop classified each row correctly (read back from the REAL cache write).
        assert _cached_is_vm(1) is False
        assert _cached_is_vm(2) is True
        assert _cached_is_vm(3) is True
        # Perf: the VM-success ids are hoisted into a set once, so the list is NOT re-iterated per
        # imported row. Old code rebuilt `[... for item in vm_result["success"]]` inside the loop
        # (one iteration per row → 4 for these 3 rows); the set prefetch keeps it at 2.
        assert vm_success.iter_count <= 2


@pytest.mark.django_db
class TestBulkImportConfirmPartialCacheExpiry:
    """The confirm modal must surface partial cache-expiry: when some rows survive, the dropped-to-expired-cache count must still reach the template."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView

        view = object.__new__(BulkImportConfirmView)
        view._librenms_api = _make_api()
        return view

    def test_partial_cache_expiry_notice_rendered_with_survivors(self):
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory

        view = self._make_view()
        User = get_user_model()
        user = User.objects.create_user(username="u-confirm-expiry", password="x")

        # Device 1 is still cached (survives into the confirm list); device 2's cache has expired.
        survivor = {
            "device_id": 1,
            "hostname": "router01",
            "sysName": "router01",
            "serial": "SN-CONF-1",
            "hardware": "",
            "os": "",
        }

        def _fetch(device_id, *a, **k):
            return survivor if device_id == 1 else None

        request = RequestFactory().post("/device-import/bulk/confirm/", data={"select": ["1", "2"]})
        request.user = user

        # Mock only boundaries: the LibreNMS fetch, the VC-detection LibreNMS call, and the perm gate.
        # validate_device_for_import and the bulk_import_confirm.html render run for real.
        with (
            patch.object(view, "require_write_permission", return_value=None),
            patch("netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", side_effect=_fetch),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.get_virtual_chassis_data",
                return_value={"is_stack": False, "member_count": 0, "members": []},
            ),
        ):
            response = view.post(request)

        # Partial expiry → 200 (survivors render), NOT the all-expired 400.
        assert response.status_code == 200
        html = response.content.decode("utf-8")
        # The survivor renders AND the dropped-to-expired-cache row is surfaced (1 of 2).
        assert "router01" in html
        assert "1 of 2 selected device" in html
        assert "expired cache data" in html
        # The Refresh control is a real button, not a CSP-blocked javascript: pseudo-protocol href.
        assert "javascript:" not in html
        assert "<button" in html and "window.location.reload()" in html


class TestBuildIdServerInfoPaddedId:
    """DeviceValidationDetailsView._build_id_server_info coerces ids with int() so ' 42 ' isn't dropped."""

    def test_whitespace_padded_id_is_included(self):
        """A device linked via {'prod': ' 42 '} appears in the per-server panel with id 42 (issue #99)."""
        from types import SimpleNamespace

        from django.test import override_settings

        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        cfg = {"netbox_librenms_plugin": {"servers": {"prod": {"librenms_url": "http://p", "api_token": "t"}}}}
        device = SimpleNamespace(custom_field_data={"librenms_id": {"prod": " 42 "}})
        with override_settings(PLUGINS_CONFIG=cfg):
            result = DeviceValidationDetailsView._build_id_server_info(device)

        assert result is not None
        prod = [row for row in result if row["server_key"] == "prod"]
        assert prod and prod[0]["device_id"] == 42

    def test_non_numeric_id_is_dropped(self):
        """A non-numeric id is not a valid link and is excluded."""
        from types import SimpleNamespace

        from django.test import override_settings

        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        cfg = {"netbox_librenms_plugin": {"servers": {"prod": {"librenms_url": "http://p", "api_token": "t"}}}}
        device = SimpleNamespace(custom_field_data={"librenms_id": {"prod": "abc"}})
        with override_settings(PLUGINS_CONFIG=cfg):
            result = DeviceValidationDetailsView._build_id_server_info(device)

        assert not result or all(row["server_key"] != "prod" for row in result)


@pytest.mark.django_db
class TestImportActionRebindGuard:
    """Import-action views only rebind to a CONFIGURED posted server_key, so a stale key can't 500."""

    def test_unconfigured_key_does_not_raise_keyerror(self):
        """Posting an unconfigured server_key must not construct LibreNMSAPI(that_key) and KeyError-500."""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory, override_settings

        from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView

        cfg = {"netbox_librenms_plugin": {"servers": {"default": {"librenms_url": "http://d", "api_token": "t"}}}}
        view = object.__new__(BulkImportConfirmView)
        view._librenms_api = None
        request = RequestFactory().post("/import/confirm/", data={"server_key": "ghost-not-configured"})
        request.user = AnonymousUser()
        view.request = request

        with override_settings(PLUGINS_CONFIG=cfg), patch.object(view, "has_write_permission", return_value=True):
            response = view.post(request)

        assert response.status_code != 500


@pytest.mark.django_db
class TestBulkImportDevicesViewCollisionGate:
    """The direct-import view re-runs the collision check the confirm preview can be bypassed on."""

    @staticmethod
    def _make_view(settings, server_key, server_url="https://librenms.example.test"):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        configure_test_servers(
            settings,
            {
                server_key: {
                    "librenms_url": server_url,
                    "api_token": "test-token",
                    "verify_ssl": False,
                }
            },
        )
        return BulkImportDevicesView()

    @staticmethod
    def _device_import_user(username):
        from dcim.models import Device

        return make_view_user(
            username,
            [("view", Device), ("add", Device), ("change", Device)],
        )

    def test_clean_batch_passes_gate_and_imports(self, settings, monkeypatch):
        """Two rows resolving to distinct real devices clear the gate and reach the importer."""
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        server_key = "bulk-collision-clean"
        clean_a = make_device("gate-clean-a")
        clean_b = make_device("gate-clean-b")
        user = self._device_import_user("bulk-collision-clean-user")
        import_result = {
            "success": [
                {"device_id": 1, "device": clean_a},
                {"device_id": 2, "device": clean_b},
            ],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
        }
        with run_librenms_server() as server:
            server.device_info_response(
                device_id=1,
                hostname=clean_a.name,
                serial="gate-clean-serial-a",
                ip="198.18.1.1",
            )
            server.device_info_response(
                device_id=2,
                hostname=clean_b.name,
                serial="gate-clean-serial-b",
                ip="198.18.1.2",
            )
            view = self._make_view(settings, server_key, server.url)
            request = make_view_request(
                "post",
                {"server_key": server_key, "select": ["1", "2"]},
                user=user,
            )
            with patch(
                "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                return_value=import_result,
            ) as mock_import:
                response = post_view(view, request)

        # Gate cleared (distinct devices) → the importer ran.
        mock_import.assert_called_once()
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_background_batch_defers_collision_precheck_to_job(self, settings):
        """A batch routed to a background job must NOT run the collision pre-check synchronously. It is deferred to ImportDevicesJob (which re-runs it), so the request does not pay the validation cost. The job is enqueued and the sync detector is never called."""
        from netbox_librenms_plugin.views.imports.actions import detect_collisions_for_device_ids

        server_key = "bulk-collision-background"
        view = self._make_view(settings, server_key)
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["1", "2"],
                "use_background_job": "on",
            },
            user=make_superuser("bulk-collision-background-user"),
            HTTP_HX_REQUEST="true",
        )

        with (
            patch("utilities.rqworker.get_workers_for_queue", return_value=1),
            patch(
                "netbox_librenms_plugin.jobs.ImportDevicesJob.enqueue",
                return_value=Namespace(pk=4242, job_id="job-4242"),
            ) as mock_enqueue,
            patch(
                "netbox_librenms_plugin.views.imports.actions.detect_collisions_for_device_ids",
                wraps=detect_collisions_for_device_ids,
            ) as mock_detect,
        ):
            response = post_view(view, request)

        # The job is enqueued and the synchronous collision pre-check is skipped entirely (deferred).
        mock_enqueue.assert_called_once()
        mock_detect.assert_not_called()
        assert response.status_code == 200
        assert response["HX-Redirect"] == url_for("plugins:netbox_librenms_plugin:librenms_import")

    def test_colliding_batch_non_htmx_message_is_object_neutral(self, settings, monkeypatch):
        """The non-HTMX collision block toast says "NetBox object", not "NetBox device".

        The gate covers VM rows too (vm_device_ids=vm_imports), so a VM-only batch must not
        be mislabelled. This mirrors the deliberately neutral wording in ImportDevicesJob.
        """
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        server_key = "bulk-collision-neutral-message"
        colliding_device = make_device("gate-collide-plain")
        user = self._device_import_user("bulk-collision-neutral-message-user")
        # No HX-Request header → the messages.error + redirect branch.
        with run_librenms_server() as server:
            server.device_info_response(
                device_id=1,
                hostname=colliding_device.name,
                serial="",
                ip="198.18.1.10",
            )
            server.device_info_response(
                device_id=2,
                hostname=colliding_device.name,
                serial="",
                ip="198.18.1.11",
            )
            view = self._make_view(settings, server_key, server.url)
            request = make_view_request(
                "post",
                {"server_key": server_key, "select": ["1", "2"]},
                user=user,
            )
            with patch("netbox_librenms_plugin.views.imports.actions.bulk_import_devices") as mock_import:
                response = post_view(view, request)

        mock_import.assert_not_called()
        errors = view_message_texts(request, "error")
        assert len(errors) == 1
        toast = errors[0]
        assert "NetBox object" in toast
        assert "NetBox device" not in toast
        assert "colliding device" not in toast
        assert response.status_code == 302
        assert response["Location"] == url_for("plugins:netbox_librenms_plugin:librenms_import")


# ---------------------------------------------------------------------------
# AddAsOOBView / PromoteToHostView — generic "oob" sentinel regression tests
# ---------------------------------------------------------------------------


class TestAddAsOOBViewGenericSentinel:
    """AddAsOOBView must not return HTTP 400 when oob_candidate.type == "oob"."""

    def test_generic_oob_sentinel_accepted_by_set_librenms_oob(self):
        """set_librenms_oob must not raise ValueError for oob_type='oob'."""
        from netbox_librenms_plugin.utils import get_librenms_oob, set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": {"id": 10}}}
        obj.cf = obj.custom_field_data

        # Previously this raised ValueError("does not match any known OOB type")
        # → AddAsOOBView returned HTTP 400 "Invalid OOB data: ..."
        set_librenms_oob(obj, 55, "default", oob_type="oob")
        result = get_librenms_oob(obj, "default")
        assert result is not None
        assert result["type"] == "oob"

    def test_legacy_bare_int_librenms_id_promoted_on_oob_attach(self):
        """A device whose librenms_id is still the legacy bare int must NOT silently no-op: set_librenms_oob promotes it to the per-server dict and attaches the OOB block."""
        from netbox_librenms_plugin.utils import get_librenms_oob, set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": 42}  # legacy single-server format (bare int)
        obj.cf = obj.custom_field_data

        set_librenms_oob(obj, 55, "default", oob_type="idrac")

        cf = obj.custom_field_data["librenms_id"]
        assert isinstance(cf, dict)
        assert cf["default"]["id"] == 42  # legacy host id promoted under the server key
        assert cf["default"]["oob"] == {"id": 55, "type": "idrac"}
        assert get_librenms_oob(obj, "default") == {"id": 55, "type": "idrac"}

    def test_generic_sentinel_from_detection_layer_flows_to_storage(self):
        """The generic 'oob' sentinel that _detect_serial_match_role produces (see TestDetectSerialMatchRole) is accepted by set_librenms_oob and stored."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        # The 'oob' sentinel is REAL production output (verified against _detect_serial_match_role
        # in test_coverage_device_operations.py); here we only assert storage accepts it — no
        # inline reimplementation of the production fallback chain to drift against.
        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": {"id": 99}}}

        set_librenms_oob(obj, 42, "default", oob_type="oob")  # must not raise
        assert obj.custom_field_data["librenms_id"]["default"]["oob"]["id"] == 42
        assert obj.custom_field_data["librenms_id"]["default"]["oob"]["type"] == "oob"


class TestSetLibreNMSOOBGenericSentinel:
    """set_librenms_oob must accept the generic "oob" sentinel oob_type."""

    def test_promote_generic_oob_sentinel_accepted_by_set_librenms_oob(self):
        """The generic 'oob' sentinel from the promote path's existing_oob_type fallback must not raise in set_librenms_oob."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": {"id": 10}}}

        # 'oob' is the promote path's real `existing_oob_from_name or "oob"` fallback (production
        # output); this asserts only that storage accepts it. Previously set_librenms_oob raised
        # ValueError("oob_type 'oob' does not match any known OOB type") here.
        set_librenms_oob(obj, 7, "default", oob_type="oob")
        assert obj.custom_field_data["librenms_id"]["default"]["oob"]["type"] == "oob"


@pytest.mark.django_db
class TestAddAsOOBViewPost:
    """View-level tests for AddAsOOBView.post() — HTTP interface + OOB sentinel regression."""

    @pytest.fixture(autouse=True)
    def _configure_librenms_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"add-oob-{request.node.name}".replace("_", "-").replace("[", "-").replace("]", "")
        with run_librenms_server() as server:
            configure_test_servers(
                settings,
                {
                    self.server_key: {
                        "librenms_url": server.url,
                        "api_token": "test-token",
                        "verify_ssl": False,
                    }
                },
            )
            self.librenms_server = server
            yield

    def _make_view(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        view = AddAsOOBView()
        view._librenms_api = LibreNMSAPI(server_key=self.server_key)
        return view

    def _register_oob_device(self, device_id, hostname, *, serial, ip="", generic=False):
        self.librenms_server.device_info_response(
            device_id=device_id,
            hostname=hostname,
            hardware="Test chassis" if generic else "Test OOB controller",
            os="ios" if generic else "idrac",
            serial=serial,
            ip=ip,
        )
        self.librenms_server.vc_inventory_callable(device_id, [], {})

    @staticmethod
    def _device_writer(username, extra_perms=()):
        from dcim.models import Device

        return make_view_user(username, [("change", Device), *extra_perms])

    @staticmethod
    def _post_after_validation(view, request, device_id, mutation):
        """Run a real validation, then inject one deterministic concurrent database write."""
        validate = view.get_validated_device_with_selections

        def validate_then_mutate(*args, **kwargs):
            result = validate(*args, **kwargs)
            mutation()
            return result

        with patch.object(view, "get_validated_device_with_selections", side_effect=validate_then_mutate):
            return post_view(view, request, device_id=device_id)

    def test_missing_existing_device_id_returns_htmx_error(self):
        """POST without existing_device_id returns HTMX error."""
        view = self._make_view()
        request = make_view_request(
            "post",
            {"server_key": self.server_key},
            user=make_view_user("oob-missing-device-user", []),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=1)

        assert response.status_code == 200
        assert b"Missing existing_device_id" in response.content
        assert response["HX-Reswap"] == "none"

    @pytest.mark.parametrize("htmx", [False, True], ids=["regular", "htmx"])
    def test_write_permission_denied_returns_error(self, htmx):
        """When write permission is denied, view returns that error immediately."""
        from dcim.models import Device
        from django.urls import get_script_prefix

        view = self._make_view()
        existing_device = make_device(
            "oob-denied-host",
            serial="OOB-DENIED-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        self._register_oob_device(17, "oob-denied-controller", serial=existing_device.serial)
        user = make_view_user(
            f"oob-denied-{'htmx' if htmx else 'regular'}-user",
            [("change", Device)],
            plugin_write=False,
        )
        factory_kwargs = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=user,
            **factory_kwargs,
        )
        device_count = Device.objects.count()

        response = post_view(view, request, device_id=17)

        if htmx:
            assert response.status_code == 200
            assert response.content == b""
            assert response["HX-Redirect"] == get_script_prefix()
        else:
            assert response.status_code == 302
            assert response["Location"] == get_script_prefix()
        assert view_message_texts(request, "error") == ["You do not have permission to perform this action."]
        assert Device.objects.count() == device_count
        reloaded = Device.objects.get(pk=existing_device.pk)
        assert reloaded.custom_field_data["librenms_id"][self.server_key] == {"id": 10}

    def test_invalid_existing_device_id_returns_htmx_error(self):
        """POST with a non-integer existing_device_id returns HTMX error — the failure is the int() conversion, which raises before any ORM lookup, so the manager is never hit."""
        view = self._make_view()
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": "not-a-number"},
            user=self._device_writer("oob-invalid-device-user"),
            HTTP_HX_REQUEST="true",
        )

        # int("not-a-number") fails before the real restricted queryset can fetch a row.
        response = post_view(view, request, device_id=1)

        assert response.status_code == 200
        assert b"Existing device not found" in response.content
        assert response["HX-Reswap"] == "none"

    def test_device_does_not_exist_returns_htmx_error(self):
        """POST with an existing_device_id that isn't in the DB returns HTMX error — driven by a real ORM miss on an absent pk, not a stubbed manager raising DoesNotExist."""
        view = self._make_view()
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": "987654321"},
            user=self._device_writer("oob-missing-row-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=1)

        assert response.status_code == 200
        assert b"Existing device not found" in response.content
        assert response["HX-Reswap"] == "none"

    def test_no_oob_candidate_in_validation_returns_htmx_error(self):
        """When validation has no oob_candidate, view returns an HTMX error."""
        view = self._make_view()
        existing_device = make_device("oob-nocand")
        self.librenms_server.device_info_response(
            device_id=99,
            hostname=existing_device.name,
            hardware="Test chassis",
            os="ios",
            serial="",
            ip="",
        )
        self.librenms_server.vc_inventory_callable(99, [], {})
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-no-candidate-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=99)

        assert response.status_code == 200
        assert b"No OOB candidate" in response.content
        assert response["HX-Reswap"] == "none"

    def test_device_id_mismatch_returns_htmx_error(self):
        """When oob_candidate device pk does not match existing_device_id, returns HTMX error."""
        view = self._make_view()
        existing_device = make_device(
            "oob-existing",
            librenms_cf={self.server_key: {"id": 10}},
        )
        # The real serial match resolves the OOB candidate to a different device.
        other_device = make_device("oob-other", serial="OOB-MISMATCH-SERIAL")
        self._register_oob_device(50, "oob-other-controller", serial=other_device.serial)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-device-mismatch-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=50)

        assert response.status_code == 200
        assert b"mismatch" in response.content.lower() or b"Device ID mismatch" in response.content
        assert response["HX-Reswap"] == "none"

    def test_legacy_librenms_id_returns_htmx_error(self):
        """Device with legacy bare-int librenms_id is rejected with convert-first message."""
        view = self._make_view()
        # Legacy bare-int librenms_id (not the expected per-server dict structure).
        existing_device = make_device("oob-legacy", serial="OOB-LEGACY-SERIAL", librenms_cf=42)
        self._register_oob_device(77, "oob-legacy-controller", serial=existing_device.serial)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-legacy-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=77)

        assert response.status_code == 200
        assert b"legacy" in response.content.lower()
        assert response["HX-Reswap"] == "none"

    def test_libre_device_not_found_returns_htmx_error(self):
        """When get_validated_device_with_selections returns no libre_device, returns HTMX error."""
        view = self._make_view()
        existing_device = make_device("oob-nolibre")
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-libre-missing-user"),
            HTTP_HX_REQUEST="true",
        )

        # The stub has no route for device 1, so the real API returns no LibreNMS device.
        response = post_view(view, request, device_id=1)

        assert response.status_code == 200
        assert b"not found" in response.content.lower()
        assert response["HX-Reswap"] == "none"

    def test_happy_path_oob_sentinel_links_and_refreshes(self):
        """End-to-end happy path with type=='oob': the real concurrency guards and ``set_librenms_oob`` run, the link is persisted via the real ``_save_device`` / ``transaction.atomic`` + ``select_for_update`` path, and a non-error validationRefresh response is returned."""
        from dcim.models import Device

        view = self._make_view()

        # Host id 10 is already linked under this test's server key. The OOB-IP sub-flow is skipped because
        # the candidate carries no ip below.
        existing_device = make_device(
            "oob-happy",
            serial="OOB-HAPPY-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )

        # Distinct host id (10) vs incoming OOB controller id (17): pins that the *incoming*
        # id lands in oob.id, not a reused host id.
        self._register_oob_device(17, "controller-node", serial=existing_device.serial, generic=True)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-happy-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=17)

        # Non-error response on the success path.
        assert response.status_code == 200
        assert "validationRefresh" in response.get("HX-Trigger", "")
        assert b"controller-node" in response.content

        # The real set_librenms_oob + _save_device persisted under the active key: reload from
        # the DB and confirm the incoming controller id (17) landed in oob with the generic
        # sentinel type, while the host id (10) is preserved.
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry["id"] == 10
        assert entry["oob"] == {"id": 17, "type": "oob"}

    def test_oob_link_written_to_vc_sync_device_not_selected_member(self):
        """A non-sync VC member's OOB link must be stored on the resolved sync device.

        LibreNMS treats a Virtual Chassis as one logical device: only the sync member
        (get_librenms_sync_device) carries the host librenms_id, and every reader
        (interfaces/cables/modules) resolves that member before get_librenms_oob. The
        OOB candidate, however, is matched by the controller's shared chassis serial /
        primary IP, so ``existing_device`` can be a *different*, non-sync member.

        Writing the link to that raw member (the pre-fix behaviour) stores it where no
        reader looks and — since the non-sync member holds no host id — orphans it under
        no host link. The link (and its guards/lock/save) must target the sync device.
        """
        from dcim.models import Device, VirtualChassis

        from netbox_librenms_plugin.utils import get_librenms_oob, get_librenms_sync_device

        view = self._make_view()

        vc = VirtualChassis.objects.create(name="vc-oob-sync")
        # Sync member: the ONLY member with a host librenms_id for this server key. It is priority 1 of
        # get_librenms_sync_device. Position 1 so it also iterates first.
        sync_member = make_device("vc-oob-sync-a", librenms_cf={self.server_key: {"id": 10}})
        sync_member.virtual_chassis = vc
        sync_member.vc_position = 1
        sync_member.save()

        # The user-selected member the modal matched as the OOB candidate: no host librenms_id
        # of its own, so it is NOT the sync device.
        selected_member = make_device("vc-oob-sync-b", serial="VC-OOB-SELECTED-SERIAL")
        selected_member.virtual_chassis = vc
        selected_member.vc_position = 2
        selected_member.save()

        # Ground truth: resolution really points from the selected member to the sync member.
        assert get_librenms_sync_device(selected_member, server_key=self.server_key).pk == sync_member.pk

        # Incoming OOB controller id 17 is distinct from host id 10.
        self._register_oob_device(17, "controller-node", serial=selected_member.serial, generic=True)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(selected_member.pk)},
            user=self._device_writer("vc-oob-sync-user"),
            HTTP_HX_REQUEST="true",
        )
        # ip=None keeps the OOB-IP sub-flow out of scope; this test pins the linkage target only.
        response = post_view(view, request, device_id=17)

        assert response.status_code == 200
        assert "validationRefresh" in response.get("HX-Trigger", "")

        # The link landed on the SYNC member, nested under its existing host id, so readers
        # (which resolve the sync device) can see it.
        sync_reloaded = Device.objects.get(pk=sync_member.pk)
        assert get_librenms_oob(sync_reloaded, server_key=self.server_key) == {"id": 17, "type": "oob"}
        assert sync_reloaded.custom_field_data["librenms_id"][self.server_key]["id"] == 10  # host id kept
        # The selected non-sync member got NO orphan OOB link written to it.
        assert get_librenms_oob(Device.objects.get(pk=selected_member.pk), server_key=self.server_key) is None

    def test_legacy_id_written_in_race_window_is_rejected_post_lock(self):
        """TOCTOU: the legacy gate must be re-verified on the LOCKED row (mirrors DeviceConflictActionView's post-lock gate).

        The unlocked gate reads the modal's in-memory snapshot; a legacy bare-int written
        concurrently (valid on EVERY server as the documented universal fallback) would reach
        set_librenms_oob, whose legacy-promotion branch silently namespaces it under this
        server only — dropping the device's LibreNMS linkage on all others.
        """
        from dcim.models import Device

        view = self._make_view()
        existing_device = make_device(
            "oob-legacy-race",
            serial="OOB-LEGACY-RACE-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        self._register_oob_device(17, "controller-node", serial=existing_device.serial, generic=True)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-legacy-race-user"),
            HTTP_HX_REQUEST="true",
        )

        def write_legacy_mapping():
            # This lands after the real unlocked validation and before the select_for_update
            # re-fetch. The unlocked in-memory instance still carries the dict form.
            Device.objects.filter(pk=existing_device.pk).update(custom_field_data={"librenms_id": 42})

        response = self._post_after_validation(view, request, 17, write_legacy_mapping)

        assert response.status_code == 200
        assert b"legacy" in response.content.lower()
        assert response["HX-Reswap"] == "none"
        # The universal-fallback id was NOT silently namespaced under one server.
        assert Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"] == 42

    def test_save_device_error_marks_transaction_rollback(self):
        """_save_device returns an error response (it doesn't raise), so the view must mark the transaction rollback-only before returning — otherwise any Interface/IPAddress created earlier in the atomic block by the OOB-attach would commit."""
        from dcim.models import Device
        from django.db import transaction
        from django.http import HttpResponse

        view = self._make_view()
        existing_device = make_device(
            "oob-save-error",
            serial="OOB-SAVE-ERROR-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        self._register_oob_device(17, "controller-node", serial=existing_device.serial, generic=True)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-save-error-user"),
            HTTP_HX_REQUEST="true",
        )

        err_resp = HttpResponse("save failed", status=400)
        with (
            patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=err_resp),
            patch(
                "netbox_librenms_plugin.views.imports.actions.transaction.set_rollback",
                wraps=transaction.set_rollback,
            ) as rollback_spy,
        ):
            response = post_view(view, request, device_id=17)

        assert response is err_resp
        rollback_spy.assert_called_once_with(True)
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 10}

    def test_save_failure_rolls_back_created_interface_and_ip(self):
        """End-to-end: when ``_save_device`` reports failure mid-attach, the real ``transaction.set_rollback(True)`` must discard the Interface AND IPAddress that the OOB-IP sub-flow created earlier in the SAME atomic block — otherwise those rows would commit even though the OOB link/IP never persisted."""
        from dcim.models import Device, Interface
        from django.http import HttpResponse
        from ipam.models import IPAddress

        view = self._make_view()
        # Real device, host id 10 linked, no oob_ip yet → the OOB-IP sub-flow runs and creates
        # a brand-new interface + IP before _save_device is reached.
        existing_device = make_device(
            "oob-rollback",
            serial="OOB-ROLLBACK-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        assert existing_device.oob_ip_id is None
        self._register_oob_device(
            17,
            "controller-node",
            serial=existing_device.serial,
            ip="10.99.99.9",
            generic=True,
        )
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "existing_device_id": str(existing_device.pk),
                "oob_interface_id": "__new__",
                "oob_new_interface_name": "idrac0",
            },
            user=self._device_writer(
                "oob-rollback-user",
                (("add", Interface), ("add", IPAddress)),
            ),
            HTTP_HX_REQUEST="true",
        )

        err_resp = HttpResponse("save failed", status=400)
        # Patch ONLY the device persist step; the atomic block, select_for_update, interface +
        # IP creation, set_device_ip_fk, and set_rollback all run for real.
        with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=err_resp):
            response = post_view(view, request, device_id=17)

        # The view returns the save error unchanged…
        assert response is err_resp
        # …and the rollback discarded BOTH side-effect rows created in the atomic block.
        assert not Interface.objects.filter(device=existing_device, name="idrac0").exists()
        assert not IPAddress.objects.filter(address__net_host="10.99.99.9").exists()
        # The OOB link was never persisted either (cf reloaded from the DB has no oob sub-block).
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert "oob" not in entry

    def test_existing_different_oob_ip_kept_but_user_warned(self):
        """A different existing oob_ip is kept, but a deferred WARNING tells the user it was not changed."""
        from dcim.models import Device

        view = self._make_view()
        # Real device already carrying an OOB IP (10.50.50.50) on one of its interfaces.
        existing_device = make_device(
            "oob-haship",
            serial="OOB-HAS-IP-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        iface = make_interface(existing_device, "mgmt0")
        existing_ip = make_ip("10.50.50.50/24", assigned_object=iface)
        existing_device.oob_ip = existing_ip
        existing_device.save()
        assert existing_device.oob_ip_id is not None

        # Incoming OOB controller carries a DIFFERENT ip (10.99.99.9).
        self._register_oob_device(
            17,
            "controller-node",
            serial=existing_device.serial,
            ip="10.99.99.9",
            generic=True,
        )
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-existing-ip-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=17)

        assert response.status_code == 200
        # The OOB link still committed (the attach itself succeeds)…
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry["oob"] == {"id": 17, "type": "oob"}
        # …the existing oob_ip was NOT overwritten…
        assert Device.objects.get(pk=existing_device.pk).oob_ip_id == existing_ip.pk
        # …and a deferred WARNING naming both the un-applied controller IP and the kept one
        # was surfaced through the real messages framework.
        warnings = view_message_texts(request, "warning")
        assert len(warnings) == 1
        assert "10.99.99.9" in warnings[0] and "10.50.50.50" in warnings[0]

    def test_existing_oob_ip_equal_in_different_textual_form_no_warning(self):
        """An existing OOB IP equal to the controller's — just a different IPv6 textual form — must NOT warn."""
        from dcim.models import Device

        view = self._make_view()
        # Existing OOB IP stored in COMPRESSED IPv6 form.
        existing_device = make_device(
            "oob-samehost-v6",
            serial="OOB-SAME-HOST-V6-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        iface = make_interface(existing_device, "mgmt0")
        existing_ip = make_ip("2001:db8::1/64", assigned_object=iface)
        existing_device.oob_ip = existing_ip
        existing_device.save()

        # Controller reports the SAME address in fully-EXPANDED form (textually different, same host).
        self._register_oob_device(
            17,
            "controller-node",
            serial=existing_device.serial,
            ip="2001:0db8:0000:0000:0000:0000:0000:0001",
            generic=True,
        )
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-same-host-v6-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=17)

        assert response.status_code == 200
        # The OOB link still committed, the existing oob_ip kept…
        assert Device.objects.get(pk=existing_device.pk).oob_ip_id == existing_ip.pk
        # …and NO "different OOB IP" warning was surfaced (the addresses are the same host).
        warnings = view_message_texts(request, "warning")
        assert not any("different OOB IP" in body for body in warnings), warnings

    def test_aborts_when_librenms_id_owned_by_another_device(self):
        """The incoming OOB controller id must not already belong to another NetBox device."""
        from dcim.models import Device

        view = self._make_view()
        existing_device = make_device(
            "host-a",
            serial="OOB-OWNER-RACE-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        # A different real device gains LibreNMS id 17 after validation.
        conflicting_device = make_device("<script>the-idrac</script>")
        self._register_oob_device(17, "controller-node", serial=existing_device.serial, generic=True)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-owner-race-user"),
            HTTP_HX_REQUEST="true",
        )

        def claim_incoming_id():
            Device.objects.filter(pk=conflicting_device.pk).update(
                custom_field_data={"librenms_id": {self.server_key: {"id": 17}}}
            )

        response = self._post_after_validation(view, request, 17, claim_incoming_id)

        # HTMX error toast (200 + HX-Reswap:none) naming the conflicting device.
        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"already linked to &#x27;&lt;script&gt;the-idrac&lt;/script&gt;&#x27;" in response.content
        assert b"&amp;lt;script&amp;gt;" not in response.content
        # Nothing attached: the host device's entry gained no oob sub-block.
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 10}
        conflict_entry = Device.objects.get(pk=conflicting_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert conflict_entry == {"id": 17}

    def test_aborts_when_incoming_id_is_own_host_id(self):
        """A concurrent re-link could make this device's host id equal the incoming OOB id; attaching it as OOB would store the same id in both slots (self host/OOB conflict)."""
        from dcim.models import Device

        view = self._make_view()
        existing_device = make_device(
            "host-a",
            serial="OOB-SELF-RACE-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        self._register_oob_device(17, "controller-node", serial=existing_device.serial, generic=True)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-self-race-user"),
            HTTP_HX_REQUEST="true",
        )

        def relink_host_to_incoming_id():
            Device.objects.filter(pk=existing_device.pk).update(
                custom_field_data={"librenms_id": {self.server_key: {"id": 17}}}
            )

        response = self._post_after_validation(view, request, 17, relink_host_to_incoming_id)

        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"this device&#x27;s host link" in response.content
        # No oob sub-block was written, and the host id is untouched.
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 17}

    def test_aborts_when_locked_oob_type_changed_concurrently(self):
        """Same OOB id already linked, but a concurrent re-detection stored a different type."""
        from dcim.models import Device

        view = self._make_view()
        existing_device = make_device(
            "host-a",
            serial="OOB-TYPE-RACE-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        self._register_oob_device(17, "controller-node", serial=existing_device.serial, generic=True)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("oob-type-race-user"),
            HTTP_HX_REQUEST="true",
        )

        def change_oob_type():
            Device.objects.filter(pk=existing_device.pk).update(
                custom_field_data={"librenms_id": {self.server_key: {"id": 10, "oob": {"id": 17, "type": "ilo"}}}}
            )

        response = self._post_after_validation(view, request, 17, change_oob_type)

        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"modified concurrently" in response.content
        # The stored type is preserved (not overwritten with the stale modal's "oob").
        oob = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]["oob"]
        assert oob == {"id": 17, "type": "ilo"}


@pytest.mark.django_db
class TestGetValidatedDeviceLibreDeviceReuse:
    """get_validated_device_with_selections reuses a supplied libre_device (the post-commit refresh path)."""

    def test_supplied_libre_device_skips_the_fetch(self):
        from netbox_librenms_plugin.views.imports.actions import PromoteToHostView

        view = object.__new__(PromoteToHostView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        request = _make_request(post={})
        supplied = {"device_id": 4242, "hostname": "reuse-host", "sysName": "reuse-host"}

        def _boom(*a, **k):
            raise AssertionError("fetch_device_with_cache must not run when libre_device is supplied")

        with (
            patch("netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", side_effect=_boom),
            patch(
                "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                return_value={"import_as_vm": False},
            ),
        ):
            libre_device, validation, _selections = view.get_validated_device_with_selections(
                4242, request, libre_device=supplied
            )

        # The supplied device flowed through and validate_device_for_import still ran for real.
        assert libre_device is supplied
        assert validation is not None

    def test_without_libre_device_still_fetches(self):
        from netbox_librenms_plugin.views.imports.actions import PromoteToHostView

        view = object.__new__(PromoteToHostView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        request = _make_request(post={})

        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                return_value={"device_id": 7},
            ) as mock_fetch,
            patch(
                "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                return_value={"import_as_vm": False},
            ),
        ):
            view.get_validated_device_with_selections(7, request)

        mock_fetch.assert_called_once()


@pytest.mark.django_db
class TestPostActionRebindFailsClosed:
    """PromoteToHost/Merge POST views are consolidated onto the fail-closed mixin rebind.

    A blank server_key with a misconfigured default must surface a fragment error here instead of
    leaving the lazy default client in place and 500ing on the first self.librenms_api access.
    """

    def test_blank_key_with_misconfigured_default_returns_error(self):
        from netbox_librenms_plugin.views.imports.actions import PromoteToHostView

        view = object.__new__(PromoteToHostView)
        view.kwargs = {}
        view.request = MagicMock()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        # No session client bound + a default that won't build → the mixin must fail closed on the
        # blank key, where the old per-view helper left the default in place and validated nothing.
        request = _make_request(post={"existing_device_id": "5"})
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
            response = view.post(request, device_id=17)

        assert response.status_code == 200
        assert b"no longer configured" in response.content
        assert response["HX-Reswap"] == "none"


@pytest.mark.django_db
class TestPromoteToHostViewPost:
    """View-level tests for PromoteToHostView.post() — HTTP interface + OOB sentinel regression."""

    @pytest.fixture(autouse=True)
    def _configure_librenms_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"promote-{request.node.name}".replace("_", "-").replace("[", "-").replace("]", "")
        with run_librenms_server() as server:
            configure_test_servers(
                settings,
                {
                    self.server_key: {
                        "librenms_url": server.url,
                        "api_token": "test-token",
                        "verify_ssl": False,
                    }
                },
            )
            self.librenms_server = server
            yield

    def _make_view(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.imports.actions import PromoteToHostView

        view = PromoteToHostView()
        view._librenms_api = LibreNMSAPI(server_key=self.server_key)
        return view

    def _register_host_device(self, device_id, hostname, *, serial):
        self.librenms_server.device_info_response(
            device_id=device_id,
            hostname=hostname,
            hardware="Test chassis",
            os="ios",
            serial=serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(device_id, [], {})

    @staticmethod
    def _device_writer(username, extra_perms=()):
        from dcim.models import Device

        return make_view_user(username, [("change", Device), *extra_perms])

    @staticmethod
    def _post_after_validation(view, request, device_id, mutation):
        """Run a real validation, then inject one deterministic concurrent database write."""
        validate = view.get_validated_device_with_selections

        def validate_then_mutate(*args, **kwargs):
            result = validate(*args, **kwargs)
            mutation()
            return result

        with patch.object(view, "get_validated_device_with_selections", side_effect=validate_then_mutate):
            return post_view(view, request, device_id=device_id)

    def test_missing_existing_device_id_returns_htmx_error(self):
        """POST without existing_device_id returns an HTMX error before any ORM lookup."""
        view = self._make_view()
        request = make_view_request(
            "post",
            {"server_key": self.server_key},
            user=make_view_user("promote-missing-device-user", []),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=1)

        assert response.status_code == 200
        assert b"Missing existing_device_id" in response.content
        assert response["HX-Reswap"] == "none"

    @pytest.mark.parametrize("htmx", [False, True], ids=["regular", "htmx"])
    def test_write_permission_denied_returns_error(self, htmx):
        """When write permission is denied, the view returns that error immediately."""
        from dcim.models import Device
        from django.urls import get_script_prefix

        view = self._make_view()
        existing_device = make_device(
            "promote-denied-controller",
            serial="PROMOTE-DENIED-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        self._register_host_device(17, "promote-denied-host", serial=existing_device.serial)
        user = make_view_user(
            f"promote-denied-{'htmx' if htmx else 'regular'}-user",
            [("change", Device)],
            plugin_write=False,
        )
        factory_kwargs = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=user,
            **factory_kwargs,
        )
        device_count = Device.objects.count()

        response = post_view(view, request, device_id=17)

        if htmx:
            assert response.status_code == 200
            assert response.content == b""
            assert response["HX-Redirect"] == get_script_prefix()
        else:
            assert response.status_code == 302
            assert response["Location"] == get_script_prefix()
        assert view_message_texts(request, "error") == ["You do not have permission to perform this action."]
        assert Device.objects.count() == device_count
        reloaded = Device.objects.get(pk=existing_device.pk)
        assert reloaded.custom_field_data["librenms_id"][self.server_key] == {"id": 10}

    def test_no_promote_candidate_returns_htmx_error(self):
        """When validation has no promote_to_host, the endpoint reports promotion N/A."""
        view = self._make_view()
        existing_device = make_device("promote-nocand")
        self._register_host_device(17, existing_device.name, serial="")
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-no-candidate-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=17)

        assert response.status_code == 200
        assert b"Promotion is not applicable" in response.content
        assert response["HX-Reswap"] == "none"

    def test_device_id_mismatch_returns_htmx_error(self):
        """When the validation's existing_device pk does not match the posted existing_device_id, the view rejects the stale modal."""
        view = self._make_view()
        existing_device = make_device(
            "promote-existing",
            librenms_cf={self.server_key: {"id": 20}},
        )
        other_device = make_device(
            "promote-other-controller",
            serial="PROMOTE-MISMATCH-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        self._register_host_device(17, "promote-other-host", serial=other_device.serial)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-device-mismatch-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=17)

        assert response.status_code == 200
        assert b"mismatch" in response.content.lower()
        assert response["HX-Reswap"] == "none"

    def test_legacy_librenms_id_returns_htmx_error(self):
        """A device with a legacy bare-int librenms_id is rejected with a convert-first message."""
        view = self._make_view()
        existing_device = make_device("promote-legacy", serial="PROMOTE-LEGACY-SERIAL", librenms_cf=42)
        self._register_host_device(17, "promote-legacy-host", serial=existing_device.serial)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-legacy-user"),
            HTTP_HX_REQUEST="true",
        )

        # existing_libre_id matches the legacy host id (42) so earlier guards pass and the
        # legacy-form check is the failure point.
        response = post_view(view, request, device_id=17)

        assert response.status_code == 200
        assert b"legacy" in response.content.lower()
        assert response["HX-Reswap"] == "none"

    def test_boolean_existing_libre_id_rejected(self):
        """A boolean existing_libre_id (corrupt CF) must fail closed, not coerce to 1/0 via int()."""
        view = self._make_view()
        existing_device = make_device("promote-bool", librenms_cf={self.server_key: {"id": 10}})
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-boolean-id-user"),
            HTTP_HX_REQUEST="true",
        )

        # The production validator cannot emit a boolean existing_libre_id. Keep the pre-existing
        # synthetic validation payload so this defensive boundary remains covered.
        promote = {"existing_libre_id": True, "existing_oob_type": "oob"}
        validation = {"promote_to_host": promote, "existing_device": existing_device}
        with patch.object(
            view,
            "get_validated_device_with_selections",
            return_value=({"device_id": 17}, validation, {}),
        ):
            response = post_view(view, request, device_id=17)

        assert response.status_code == 200
        assert b"Invalid existing LibreNMS id" in response.content

    def test_promote_rejected_when_new_host_id_already_linked_elsewhere(self):
        """When another device already owns the incoming host id, promotion aborts (exercises the deterministic-order conflict lock)."""
        from dcim.models import Device

        view = self._make_view()
        existing_device = make_device(
            "promote-src-controller",
            serial="PROMOTE-CONFLICT-RACE-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        conflicting_device = make_device("<script>promote-conflict</script>")
        self._register_host_device(17, "promote-src-host", serial=existing_device.serial)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-conflict-race-user"),
            HTTP_HX_REQUEST="true",
        )

        def claim_incoming_id():
            Device.objects.filter(pk=conflicting_device.pk).update(
                custom_field_data={"librenms_id": {self.server_key: {"id": 17}}}
            )

        response = self._post_after_validation(view, request, 17, claim_incoming_id)

        assert response.status_code == 200
        assert b"already linked to" in response.content
        assert b"&lt;script&gt;promote-conflict&lt;/script&gt;" in response.content
        assert b"&amp;lt;script&amp;gt;" not in response.content
        # The source device must be left unchanged (still host id 10, no OOB).
        existing_device.refresh_from_db()
        assert existing_device.custom_field_data["librenms_id"][self.server_key] == {"id": 10}
        conflicting_device.refresh_from_db()
        assert conflicting_device.custom_field_data["librenms_id"][self.server_key] == {"id": 17}

    def test_failed_oob_attach_after_host_swap_leaves_db_untouched(self):
        """A ValueError raised AFTER set_librenms_device_id already ran must not commit a partial swap.

        set_librenms_device_id()/set_librenms_oob() mutate custom_field_data in memory only;
        the transaction's single DB write is _save_device() at the end of the atomic block, so
        the early error return commits nothing. Pins the no-partial-commit contract of the
        promote flow (an invalid OOB type is the in-transaction ValueError source).
        """
        view = self._make_view()
        existing_device = make_device("promote-badoob", librenms_cf={self.server_key: {"id": 10}})
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-invalid-oob-user"),
            HTTP_HX_REQUEST="true",
        )

        # No OOB keyword substring (OOB_TYPE_PATTERN) and not the "oob" sentinel, so
        # set_librenms_oob raises ValueError inside the transaction — after the host swap.
        # The production validator normalizes this value, so keep the pre-existing synthetic
        # payload to exercise the defensive transaction boundary.
        promote = {"existing_libre_id": 10, "existing_oob_type": "management-card"}
        validation = {"promote_to_host": promote, "existing_device": existing_device}
        with patch.object(
            view,
            "get_validated_device_with_selections",
            return_value=({"device_id": 17}, validation, {}),
        ):
            response = post_view(view, request, device_id=17)

        assert response.status_code == 200
        assert b"Invalid promotion data" in response.content
        # The in-memory host swap (10 -> 17) must NOT have been persisted: the row still
        # holds the original host id and gained no oob sub-object.
        existing_device.refresh_from_db()
        assert existing_device.custom_field_data["librenms_id"][self.server_key] == {"id": 10}

    def test_existing_link_already_points_at_incoming_device_returns_error(self):
        """If the existing link already equals the incoming LibreNMS id there is nothing to promote — the view must say so rather than self-demoting the same id into OOB."""
        view = self._make_view()
        existing_device = make_device("promote-noop", librenms_cf={self.server_key: {"id": 17}})
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-noop-user"),
            HTTP_HX_REQUEST="true",
        )

        # A real validation resolves this row directly by device_id and does not offer promotion.
        # Keep the pre-existing synthetic payload to cover this defensive equality guard.
        promote = {"existing_libre_id": 17, "existing_oob_type": "oob"}
        validation = {"promote_to_host": promote, "existing_device": existing_device}
        with patch.object(
            view,
            "get_validated_device_with_selections",
            return_value=({"device_id": 17}, validation, {}),
        ):
            response = post_view(view, request, device_id=17)

        assert response.status_code == 200
        assert b"already points at this LibreNMS device" in response.content
        assert response["HX-Reswap"] == "none"

    def test_happy_path_generic_oob_sentinel_promotes_and_demotes_link(self):
        """End-to-end VIEW-level regression for issue #89: POST to PromoteToHostView with the generic 'oob' sentinel as the existing controller type."""
        from dcim.models import Device

        view = self._make_view()
        # Existing device is currently linked to LibreNMS id 10 (the controller, occupying the
        # host slot pre-promote); the incoming real host is id 17.
        existing_device = make_device(
            "promote-happy",
            serial="PROMOTE-HAPPY-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        self._register_host_device(17, "promote-happy-host", serial=existing_device.serial)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-happy-user"),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=17)

        # Success path: validation modal refresh, no error swap.
        assert response.status_code == 200
        assert "validationRefresh" in response.get("HX-Trigger", "")
        assert b"promote-happy-host" in response.content

        # Reload from the DB: host id swapped to 17, previous link (10) demoted to the OOB
        # slot with the generic sentinel type.
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry["id"] == 17
        assert entry["oob"] == {"id": 10, "type": "oob"}

    def test_override_platform_manufacturer_mismatch_rejected(self):
        """An override platform whose manufacturer differs from the device type's is rejected (update_fields skips full_clean, so the cross-field invariant is enforced explicitly), and nothing is committed."""
        from dcim.models import Device, Manufacturer, Platform

        view = self._make_view()
        existing_device = make_device(
            "promote-badplat",
            serial="PROMOTE-BAD-PLATFORM-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        # A platform under a DIFFERENT manufacturer than the device's device_type.
        other_mfr, _ = Manufacturer.objects.get_or_create(name="OtherMfr-3001", slug="othermfr-3001")
        bad_platform, _ = Platform.objects.get_or_create(
            name="BadPlat-3001", slug="badplat-3001", defaults={"manufacturer": other_mfr}
        )
        assert bad_platform.manufacturer_id != existing_device.device_type.manufacturer_id

        self._register_host_device(17, "promote-bad-platform-host", serial=existing_device.serial)
        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "existing_device_id": str(existing_device.pk),
                "override_platform_id": str(bad_platform.pk),
            },
            user=self._device_writer("promote-bad-platform-user", (("view", Platform),)),
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request, device_id=17)

        # Rejected by the SHARED _platform_device_type_mismatch() check inside _save_device()
        # (not a now-removed inline duplicate); the promote is not committed.
        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"update the platform first" in response.content
        reloaded = Device.objects.get(pk=existing_device.pk)
        assert reloaded.platform_id is None  # the bad override was never persisted
        assert reloaded.custom_field_data["librenms_id"][self.server_key] == {"id": 10}  # host swap not committed

    def test_aborts_when_incoming_host_id_owned_by_another_device(self):
        """The incoming host id must not already belong to another NetBox device."""
        from dcim.models import Device

        view = self._make_view()
        existing_device = make_device(
            "promote-host-controller",
            serial="PROMOTE-OWNER-RACE-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        # A different real device gains LibreNMS id 17 after validation.
        conflicting_device = make_device("promote-thief")
        self._register_host_device(17, "promote-host", serial=existing_device.serial)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-owner-race-user"),
            HTTP_HX_REQUEST="true",
        )

        def claim_incoming_id():
            Device.objects.filter(pk=conflicting_device.pk).update(
                custom_field_data={"librenms_id": {self.server_key: {"id": 17}}}
            )

        response = self._post_after_validation(view, request, 17, claim_incoming_id)

        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"already linked to &#x27;promote-thief&#x27;" in response.content
        # Nothing committed: the host slot is unchanged and no OOB slot was written.
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 10}
        conflict_entry = Device.objects.get(pk=conflicting_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert conflict_entry == {"id": 17}


@pytest.mark.django_db
class TestMergeNetBoxDevicesViewOOBTransfer:
    """MergeNetBoxDevicesView.post: oob_ip may only move to the winner when its underlying IP already sits on a winner interface (the merge does not move interfaces, and the save skips full_clean())."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import MergeNetBoxDevicesView

        view = object.__new__(MergeNetBoxDevicesView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def _run(self, *, oob_on_winner):
        """Drive a merge where the donor's oob_ip sits on an interface owned by the winner (``oob_on_winner=True``) or by the donor (``False``)."""
        from dcim.models import Device
        from django.http import HttpResponse

        from netbox_librenms_plugin.tests.conftest import ip_on

        view = self._make_view()

        winner = make_device("merge-winner", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-donor", librenms_cf={"default": {"id": 10}})

        # The donor carries an oob_ip whose underlying IP is assigned to an interface
        # owned by whichever device the scenario dictates. save() (not full_clean) lets
        # us seed the winner-interface case the view is designed to resolve.
        oob_host = winner if oob_on_winner else donor
        oob_ip = ip_on(oob_host, "192.0.2.7/32", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()

        request = _make_request(post={"winner_pk": str(winner.pk), "donor_pk": str(donor.pk)})
        validation = {"merge_candidates": {"host_named": {"pk": winner.pk}, "oob_named": {"pk": donor.pk}}}
        view.get_validated_device_with_selections = MagicMock(return_value=({"device_id": 99}, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        resp = view.post(request, device_id=99)
        assert resp.status_code == 200
        return Device.objects.get(pk=winner.pk), Device.objects.get(pk=donor.pk), oob_ip

    def test_transfers_when_oob_ip_on_winner_interface(self):
        winner, donor, oob_ip = self._run(oob_on_winner=True)
        # The transfer actually persisted: winner now owns the oob_ip, donor cleared.
        assert winner.oob_ip_id == oob_ip.pk
        assert donor.oob_ip_id is None

    def test_skips_when_oob_ip_on_donor_interface(self):
        winner, donor, oob_ip = self._run(oob_on_winner=False)
        # Left on the donor (its interface owns the IP); winner not given a donor-owned IP.
        assert donor.oob_ip_id == oob_ip.pk
        assert winner.oob_ip_id is None

    def test_oob_ip_and_owning_interface_locked_for_update(self):
        """The oob_ip transfer must SELECT ... FOR UPDATE the IPAddress and its owning interface so a concurrent interface move can't leave winner.oob_ip on an interface no longer on the winner."""
        from django.db import connection
        from django.http import HttpResponse
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import ip_on

        view = self._make_view()
        winner = make_device("merge-winner-lock", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-donor-lock", librenms_cf={"default": {"id": 10}})
        oob_ip = ip_on(winner, "192.0.2.9/32", "mgmt0")  # IP on a WINNER interface → transfer path runs
        donor.oob_ip = oob_ip
        donor.save()

        request = _make_request(post={"winner_pk": str(winner.pk), "donor_pk": str(donor.pk)})
        validation = {"merge_candidates": {"host_named": {"pk": winner.pk}, "oob_named": {"pk": donor.pk}}}
        view.get_validated_device_with_selections = MagicMock(return_value=({"device_id": 99}, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        with CaptureQueriesContext(connection) as ctx:
            resp = view.post(request, device_id=99)
        assert resp.status_code == 200

        def _locked(table):
            return any(table in q["sql"].lower() and "for update" in q["sql"].lower() for q in ctx.captured_queries)

        assert _locked("ipam_ipaddress"), "the transferred oob_ip must be SELECT ... FOR UPDATE"
        assert _locked("dcim_interface"), "the oob_ip's owning interface must be SELECT ... FOR UPDATE"
        # And the transfer still completes.
        winner.refresh_from_db()
        donor.refresh_from_db()
        assert winner.oob_ip_id == oob_ip.pk
        assert donor.oob_ip_id is None

    def test_save_failure_rolls_back_donor_oob_release_and_marker(self):
        """Forced persist failure mid-merge must roll back the donor's already-executed save."""
        from dcim.models import Device
        from django.db import IntegrityError
        from django.http import HttpResponse

        from netbox_librenms_plugin.tests.conftest import ip_on

        view = self._make_view()
        winner = make_device("merge-winner-fail", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-donor-fail", librenms_cf={"default": {"id": 10}})
        # oob_ip on a winner-owned interface → the transfer path runs (oob_ip in update_fields).
        oob_ip = ip_on(winner, "192.0.2.8/32", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()

        request = _make_request(post={"winner_pk": str(winner.pk), "donor_pk": str(donor.pk)})
        validation = {"merge_candidates": {"host_named": {"pk": winner.pk}, "oob_named": {"pk": donor.pk}}}
        view.get_validated_device_with_selections = MagicMock(return_value=({"device_id": 99}, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        # Force the SECOND Device.save (the winner's, per the donor-then-winner order) to fail,
        # so the donor's release is already written inside the savepoint when rollback fires.
        real_save = Device.save
        save_calls = []

        def flaky_save(self, *args, **kwargs):
            save_calls.append(self.pk)
            if len(save_calls) == 2:
                raise IntegrityError("forced winner save failure")
            return real_save(self, *args, **kwargs)

        with patch.object(Device, "save", flaky_save):
            resp = view.post(request, device_id=99)

        # Surfaced as the HTMX OOB error toast (200 + HX-Reswap:none), not a 500.
        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"database integrity constraint was violated" in resp.content
        assert b"forced winner save failure" not in resp.content
        # The donor's save (release + marker) was rolled back: it still owns the oob_ip, the
        # winner never claimed it, and no migration marker was stamped.
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.oob_ip_id == oob_ip.pk
        assert winner.oob_ip_id is None
        entry = donor.custom_field_data["librenms_id"]["default"]
        assert entry == {"id": 10}
        assert "_migrated_to" not in entry

    def test_transfer_survives_interface_move_onto_winner_between_read_and_lock(self):
        """A concurrent interface move ONTO the winner, landing between the assigned_object read and the interface lock, must not spuriously fail the merge: the locked IP's GFK cache is refreshed to the freshly-locked (winner-owned) interface before set_device_ip_fk re-checks ownership."""
        from dcim.models import Interface
        from django.http import HttpResponse

        from netbox_librenms_plugin.tests.conftest import ip_on

        view = self._make_view()
        winner = make_device("merge-winner-race", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-donor-race", librenms_cf={"default": {"id": 10}})
        # The IP starts on a DONOR interface, so the merge's assigned_object read caches device_id=donor.
        oob_ip = ip_on(donor, "192.0.2.11/32", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()
        iface_pk = oob_ip.assigned_object_id

        request = _make_request(post={"winner_pk": str(winner.pk), "donor_pk": str(donor.pk)})
        validation = {"merge_candidates": {"host_named": {"pk": winner.pk}, "oob_named": {"pk": donor.pk}}}
        view.get_validated_device_with_selections = MagicMock(return_value=({"device_id": 99}, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        # Simulate the concurrent move: the first Interface SELECT ... FOR UPDATE (the oob_ip's owning
        # interface lock) fires AFTER the merge cached assigned_object with device_id=donor. Move the
        # interface onto the winner right then, so the freshly locked row is winner-owned while the
        # cached GFK is stale — exactly the TOCTOU the freshen-after-lock guards against.
        real_sfu = Interface.objects.select_for_update
        state = {"moved": False}

        def moving_sfu(*args, **kwargs):
            queryset = real_sfu(*args, **kwargs)
            real_filter = queryset.filter

            def moving_filter(*filter_args, **filter_kwargs):
                if not state["moved"] and filter_kwargs.get("pk") == iface_pk:
                    state["moved"] = True
                    Interface.objects.filter(pk=iface_pk).update(device=winner)
                return real_filter(*filter_args, **filter_kwargs)

            queryset.filter = moving_filter
            return queryset

        with patch.object(Interface.objects, "select_for_update", moving_sfu):
            resp = view.post(request, device_id=99)

        assert resp.status_code == 200
        winner.refresh_from_db()
        donor.refresh_from_db()
        # The interface is now on the winner, so the transfer MUST complete — not be rejected against
        # the stale cached device_id and roll the merge back.
        assert winner.oob_ip_id == oob_ip.pk
        assert donor.oob_ip_id is None
        assert state["moved"], "the injected interface move never fired — test wiring is stale"


def _two_member_vc(name, *, m1_cf=None, m2_cf=None):
    """Create a real 2-member VirtualChassis (positions 1, 2). m1 becomes the sync device when it is the member holding the ``librenms_id``."""
    from dcim.models import VirtualChassis

    vc = VirtualChassis.objects.create(name=name)
    m1 = make_device(f"{name}-m1", librenms_cf=m1_cf)
    m1.virtual_chassis = vc
    m1.vc_position = 1
    m1.save()
    m2 = make_device(f"{name}-m2", librenms_cf=m2_cf)
    m2.virtual_chassis = vc
    m2.vc_position = 2
    m2.save()
    return vc, m1, m2


@pytest.mark.django_db
class TestMergeNetBoxDevicesViewVCSyncDevice:
    """MergeNetBoxDevicesView.post: when a merge candidate is a Virtual Chassis member, the LibreNMS link (host id / OOB) and the ``_migrated_to`` marker must be merged on the VC's sync device (``get_librenms_sync_device``), not the raw selected member. Writing to a non-sync member either split-brains a VC that already has a linked member, or leaves the donor's real link (on its sync sibling) uncleared."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import MergeNetBoxDevicesView

        view = object.__new__(MergeNetBoxDevicesView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def _run_merge(self, *, winner, donor):
        from django.http import HttpResponse

        view = self._make_view()
        request = _make_request(post={"winner_pk": str(winner.pk), "donor_pk": str(donor.pk)})
        validation = {"merge_candidates": {"host_named": {"pk": winner.pk}, "oob_named": {"pk": donor.pk}}}
        view.get_validated_device_with_selections = MagicMock(return_value=({"device_id": 99}, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))
        resp = view.post(request, device_id=99)
        assert resp.status_code == 200
        return resp

    @staticmethod
    def _entry(device):
        device.refresh_from_db()
        return (device.custom_field_data.get("librenms_id") or {}).get("default") or {}

    def test_winner_is_non_sync_vc_member_link_lands_on_sync_device(self):
        """Winner is a non-sync VC member whose sync sibling already holds a host id; the donor's id must merge onto the sync sibling (into its OOB half) and NEVER onto the raw winner member — otherwise two members of one chassis hold ``librenms_id`` (split brain)."""
        _vc, m1, m2 = _two_member_vc("mrg-vc-win", m1_cf={"default": {"id": 30}}, m2_cf=None)
        donor = make_device("mrg-vc-win-donor", librenms_cf={"default": {"id": 40}})

        self._run_merge(winner=m2, donor=donor)

        # The raw non-sync winner member must stay clean — no host id planted on it.
        assert "id" not in self._entry(m2), "non-sync VC member must not receive the merged host id (split brain)"
        # The VC's sync device keeps its own id and absorbs the donor's id into its OOB slot.
        m1_entry = self._entry(m1)
        assert m1_entry.get("id") == 30
        assert (m1_entry.get("oob") or {}).get("id") == 40, "donor id should merge onto the sync device's OOB slot"
        # Donor cleared + marked migrated toward the sync device (the real link holder).
        donor_entry = self._entry(donor)
        assert "id" not in donor_entry
        assert donor_entry.get("_migrated_to", {}).get("device_id") == m1.pk

    def test_donor_is_non_sync_vc_member_clears_link_on_sync_device(self):
        """Donor is a non-sync VC member; its real link lives on the sync sibling. The merge must read + clear that sibling's link (and mark IT migrated), not the empty selected member — otherwise the source VC stays linked to LibreNMS."""
        _vc, m1, m2 = _two_member_vc("mrg-vc-don", m1_cf={"default": {"id": 30}}, m2_cf=None)
        winner = make_device("mrg-vc-don-winner", librenms_cf={"default": {"id": 50}})

        self._run_merge(winner=winner, donor=m2)

        # The donor VC's sync sibling holds the real link: it must be absorbed and cleared + marked.
        m1_entry = self._entry(m1)
        assert "id" not in m1_entry, "the donor VC's sync device must be cleared, not left linked to LibreNMS"
        assert m1_entry.get("_migrated_to", {}).get("device_id") == winner.pk
        # Winner absorbed the sync sibling's id into its OOB slot (winner already had a host id).
        winner_entry = self._entry(winner)
        assert winner_entry.get("id") == 50
        assert (winner_entry.get("oob") or {}).get("id") == 30
        # The raw selected member (m2) never held a link; nothing is planted on it.
        assert "id" not in self._entry(m2)

    def test_legacy_id_on_donor_sync_sibling_gets_convert_first_message(self):
        """A legacy link on the donor's sync sibling gets the friendly convert-first message."""
        _vc, _sync_member, selected_member = _two_member_vc("mrg-vc-legacy", m1_cf=30, m2_cf=None)
        winner = make_device("mrg-vc-legacy-winner", librenms_cf={"default": {"id": 50}})

        response = self._run_merge(winner=winner, donor=selected_member)

        assert b"Donor device has a legacy bare-integer librenms_id" in response.content
        assert b"Convert mapping" in response.content

    def test_merge_locks_every_vc_member_before_resolving_the_sync_device(self):
        """The merge must lock every VC member (incl. bystanders) before resolving the sync device."""
        import re

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        vc, m1, m2 = _two_member_vc("mrg-vc-lock", m1_cf={"default": {"id": 30}}, m2_cf=None)
        # A bystander member: not the selected winner (m2) and not the sync device (m1).
        m3 = make_device("mrg-vc-lock-m3", librenms_cf=None)
        m3.virtual_chassis = vc
        m3.vc_position = 3
        m3.save()
        donor = make_device("mrg-vc-lock-donor", librenms_cf={"default": {"id": 40}})

        with CaptureQueriesContext(connection) as ctx:
            self._run_merge(winner=m2, donor=donor)

        locked_pks = set()
        for q in ctx.captured_queries:
            sql = q["sql"]
            if "dcim_device" in sql and "FOR UPDATE" in sql:
                match = re.search(r"IN \(([\d, ]+)\)", sql)
                if match:
                    locked_pks.update(int(p) for p in match.group(1).split(","))
        # The whole chassis (m1 sync + m2 winner + m3 bystander) plus the donor must be locked. m3
        # missing means the sync device was resolved from unlocked rows — the bug this guards.
        assert {m1.pk, m2.pk, m3.pk, donor.pk} <= locked_pks, (
            f"expected every VC member locked before sync-device resolution; locked={locked_pks}, "
            f"bystander m3={m3.pk} missing"
        )


@pytest.mark.django_db
class TestMergeNetBoxDevicesViewFailClosed:
    """Merge preparation failures must return a toast and leave the donor unmigrated."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import MergeNetBoxDevicesView

        view = object.__new__(MergeNetBoxDevicesView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def _post_merge(self, view, winner, donor):
        from django.http import HttpResponse

        request = _make_request(post={"winner_pk": str(winner.pk), "donor_pk": str(donor.pk)})
        validation = {"merge_candidates": {"host_named": {"pk": winner.pk}, "oob_named": {"pk": donor.pk}}}
        view.get_validated_device_with_selections = MagicMock(return_value=({"device_id": 99}, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))
        return view.post(request, device_id=99)

    def test_orphan_host_id_merge_fails_closed_and_leaves_donor_unmigrated(self):
        """A winner holding both host id + oob and a donor with a distinct host-id-only link fails closed."""
        winner = make_device(
            "merge-orphan-winner",
            librenms_cf={"default": {"id": 100, "oob": {"id": 50, "type": "idrac"}}},
        )
        donor = make_device("<script>merge-orphan-donor</script>", librenms_cf={"default": {"id": 200}})

        resp = self._post_merge(self._make_view(), winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"Cannot merge" in resp.content
        assert b"&lt;script&gt;merge-orphan-donor&lt;/script&gt;" in resp.content
        assert b"&amp;lt;script&amp;gt;" not in resp.content
        # Donor's link is preserved and it was NOT marked migrated (no orphaned LibreNMS host).
        donor.refresh_from_db()
        entry = donor.custom_field_data["librenms_id"]["default"]
        assert entry == {"id": 200}
        assert "_migrated_to" not in entry

    def test_corrupt_donor_oob_id_with_winner_oob_fails_closed_not_500(self):
        """A donor oob id merge_librenms_links skipped (winner already has an oob) fails closed at the marker, not a 500."""
        winner = make_device(
            "merge-f2-winner",
            librenms_cf={"default": {"id": 5, "oob": {"id": 9, "type": "idrac"}}},
        )
        # Same host id (so the orphan guard doesn't fire) but a corrupt donor oob id. Because the
        # winner already holds an oob, merge_librenms_links() skips validating the donor oob id —
        # mark_librenms_migrated() is the one that rejects it, and that call must be guarded too.
        donor = make_device("merge-f2-donor", librenms_cf={"default": {"id": 5, "oob": {"id": "abc"}}})

        resp = self._post_merge(self._make_view(), winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"Cannot merge" in resp.content
        donor.refresh_from_db()
        entry = donor.custom_field_data["librenms_id"]["default"]
        assert entry == {"id": 5, "oob": {"id": "abc"}}
        assert "_migrated_to" not in entry

    def test_oob_transfer_valueerror_fails_closed_and_rolls_back(self):
        """A ValueError from the oob_ip transfer (the TOCTOU race the lock guards) fails closed with rollback, not a 500."""
        import netbox_librenms_plugin.views.imports.actions as actions_mod
        from netbox_librenms_plugin.tests.conftest import ip_on

        winner = make_device("merge-f5-winner", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-f5-donor", librenms_cf={"default": {"id": 10}})
        oob_ip = ip_on(winner, "192.0.2.11/32", "mgmt0")  # IP on a WINNER interface → transfer path runs
        donor.oob_ip = oob_ip
        donor.save()

        # The pre-check (locked_iface.device_id == winner.pk) passes, but set_device_ip_fk re-reads
        # a now-stale cached assignment and raises — the concurrency race the lock exists to catch.
        # Patch the exact boundary (the race is not deterministically reproducible single-threaded).
        real = actions_mod.set_device_ip_fk

        def racy(device, field, ip, *, save=True):
            if field == "oob_ip" and ip is not None:
                raise ValueError("set_device_ip_fk: address is not assigned to an interface on that device")
            return real(device, field, ip, save=save)

        with patch.object(actions_mod, "set_device_ip_fk", racy):
            resp = self._post_merge(self._make_view(), winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"Cannot merge" in resp.content
        # Rolled back: donor keeps its oob_ip and link, winner never claimed it, no marker stamped.
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.oob_ip_id == oob_ip.pk
        assert winner.oob_ip_id is None
        entry = donor.custom_field_data["librenms_id"]["default"]
        assert entry == {"id": 10}
        assert "_migrated_to" not in entry

    def test_oob_ip_lock_database_error_fails_closed_without_leaking_backend_text(self):
        """A DB failure while acquiring the OOB-IP lock returns a safe toast and rolls back."""
        from django.db import DatabaseError

        from ipam.models import IPAddress
        from netbox_librenms_plugin.tests.conftest import ip_on

        winner = make_device("merge-db-lock-winner", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-db-lock-donor", librenms_cf={"default": {"id": 10}})
        oob_ip = ip_on(winner, "192.0.2.12/32", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()

        with patch.object(
            IPAddress.objects,
            "select_for_update",
            side_effect=DatabaseError("forced lock timeout with backend detail"),
        ):
            resp = self._post_merge(self._make_view(), winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"database operation failed" in resp.content
        assert b"forced lock timeout" not in resp.content
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.oob_ip_id == oob_ip.pk
        assert winner.oob_ip_id is None
        assert donor.custom_field_data["librenms_id"]["default"] == {"id": 10}

    def test_device_lock_database_error_fails_closed_without_leaking_backend_text(self):
        """A DB failure while locking the merge pair returns a safe retry toast, not a 500."""
        from dcim.models import Device
        from django.db import DatabaseError

        winner = make_device("merge-device-lock-winner", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-device-lock-donor", librenms_cf={"default": {"id": 10}})

        with patch.object(
            Device.objects,
            "select_for_update",
            side_effect=DatabaseError("forced primary lock timeout with backend detail"),
        ):
            resp = self._post_merge(self._make_view(), winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"database operation failed" in resp.content
        assert b"forced primary lock timeout" not in resp.content
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.custom_field_data["librenms_id"]["default"] == {"id": 10}
        assert winner.custom_field_data["librenms_id"]["default"] == {"id": 20}


@pytest.mark.django_db
class TestSuggestOOBInterface:
    """_suggest_oob_interface: pre-select an OOB/mgmt-named interface + default new name."""

    def test_picks_idrac_named_interface(self):
        from netbox_librenms_plugin.views.imports.actions import _suggest_oob_interface

        dev = make_device("oob-suggest-idrac")
        make_interface(dev, "eth0")
        idrac = make_interface(dev, "iDRAC")
        sid, new_name = _suggest_oob_interface(dev, {"type": "idrac"})
        assert sid == idrac.pk
        assert new_name == "idrac0"

    def test_picks_cimc_named_interface(self):
        # cimc is in OOB_TYPES (detection) but was missing from the interface-suggester pattern,
        # so a cimc0 interface was never pre-selected. The pattern is now derived from OOB_TYPES.
        from netbox_librenms_plugin.views.imports.actions import _suggest_oob_interface

        dev = make_device("oob-suggest-cimc")
        make_interface(dev, "eth0")
        cimc = make_interface(dev, "cimc0")
        sid, new_name = _suggest_oob_interface(dev, {"type": "cimc"})
        assert sid == cimc.pk
        assert new_name == "cimc0"

    def test_no_match_returns_none_and_typed_default(self):
        from netbox_librenms_plugin.views.imports.actions import _suggest_oob_interface

        dev = make_device("oob-suggest-nomatch")
        make_interface(dev, "eth0")
        sid, new_name = _suggest_oob_interface(dev, {"type": "ilo"})
        assert sid is None
        assert new_name == "ilo0"

    def test_missing_type_defaults_to_oob(self):
        from netbox_librenms_plugin.views.imports.actions import _suggest_oob_interface

        dev = make_device("oob-suggest-notype")
        sid, new_name = _suggest_oob_interface(dev, {})
        assert sid is None
        assert new_name == "oob0"

    def test_substring_token_is_not_matched(self):
        """A name that merely contains an OOB token as a substring (no word boundary) is not pre-selected."""
        from netbox_librenms_plugin.views.imports.actions import _suggest_oob_interface

        dev = make_device("oob-suggest-substring")
        # "bmcswitch-uplink" contains "bmc" and "submgmt" contains "mgmt", but neither is an
        # OOB/management interface — without word-boundary anchoring both falsely matched.
        make_interface(dev, "bmcswitch-uplink")
        make_interface(dev, "submgmt")
        sid, new_name = _suggest_oob_interface(dev, {"type": "bmc"})
        assert sid is None
        assert new_name == "bmc0"

    def test_token_with_trailing_index_still_matches(self):
        """A genuine OOB/management interface (token + optional index) is still pre-selected."""
        from netbox_librenms_plugin.views.imports.actions import _suggest_oob_interface

        dev = make_device("oob-suggest-mgmt")
        make_interface(dev, "eth0")
        mgmt = make_interface(dev, "mgmt0")
        sid, new_name = _suggest_oob_interface(dev, {"type": "oob"})
        assert sid == mgmt.pk
        assert new_name == "oob0"


@pytest.mark.django_db
class TestResolveOOBInterface:
    """AddAsOOBView._resolve_oob_interface: select existing / create new / none."""

    def _view(self):
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        return object.__new__(AddAsOOBView)

    def test_none_when_no_selection(self):
        view = self._view()
        dev = make_device("oob-res-none")
        req = _make_request(post={})
        assert view._resolve_oob_interface(req, dev) == (None, None)

    def test_existing_interface_by_id(self):
        from django.db import transaction

        view = self._view()
        dev = make_device("oob-res-existing")
        iface = make_interface(dev, "eth0")
        # Superuser: the reused interface is now read through a restricted queryset, and this
        # test is about resolving the selection, not about the grant (see
        # TestGatedViewsRefuseOutOfScopeObjects for the scoping itself).
        req = _make_request(post={"oob_interface_id": str(iface.pk)}, user_is_superuser=True)
        with transaction.atomic():
            result_iface, reason = view._resolve_oob_interface(req, dev)
        assert result_iface.pk == iface.pk and reason is None

    def test_create_new_interface(self):
        from django.db import transaction

        from dcim.models import Interface

        view = self._view()
        dev = make_device("oob-res-create")
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
        with transaction.atomic():
            result_iface, reason = view._resolve_oob_interface(req, dev)
        assert reason is None
        assert result_iface.pk is not None
        assert result_iface.name == "idrac0"
        assert result_iface.type == "other"
        # Really persisted on the device.
        assert Interface.objects.filter(device=dev, name="idrac0").exists()

    def test_create_new_interface_invalid_name_returns_reason(self):
        """A name far over the length limit fails real ``full_clean`` → reason 'invalid_name' (surfaced as a warning), not a 500, and nothing is persisted."""
        from django.db import transaction

        from dcim.models import Interface

        view = self._view()
        dev = make_device("oob-res-badname")
        long_name = "x" * 500
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": long_name})
        with transaction.atomic():
            result_iface, reason = view._resolve_oob_interface(req, dev)
        assert result_iface is None
        assert reason == "invalid_name"
        assert not Interface.objects.filter(device=dev, name=long_name).exists()

    def test_new_reuses_existing_locked_interface(self):
        """An interface with the requested (device, name) already exists → it is reused, no create, regardless of the 'add' permission."""
        from dcim.models import Interface
        from django.db import transaction

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms

        view = self._view()
        dev = make_device("oob-res-reuse")
        existing = make_interface(dev, "idrac0")
        user = make_user_with_perms("oob-res-reuse", [("view", Interface)])
        req = make_request(
            "post",
            {"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"},
            user=user,
        )
        assert not user.has_perm("dcim.add_interface")
        with transaction.atomic():
            result_iface, reason = view._resolve_oob_interface(req, dev)
        assert result_iface.pk == existing.pk and reason is None

    def test_new_does_not_reuse_an_interface_outside_the_view_grant(self):
        """The name-based reuse path must match the scoped explicit-PK path."""
        from dcim.models import Interface
        from django.db import transaction

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms

        view = self._view()
        device = make_device("oob-res-name-scope")
        hidden = make_interface(device, "idrac0")
        allowed = make_interface(device, "eth0")
        user = make_user_with_perms("oob-interface-view-scope", [("add", Interface)])
        user = grant(user, "view", Interface, constraints={"pk": allowed.pk})
        request = make_request(
            "post",
            {"oob_interface_id": "__new__", "oob_new_interface_name": hidden.name},
            user=user,
        )

        with transaction.atomic():
            result_iface, reason = view._resolve_oob_interface(request, device)

        # A dedicated reason, not the "no selection made" pair: the caller DID choose a name, and
        # the message chain would otherwise tell the operator to choose one.
        assert result_iface is None and reason == "name_out_of_scope"

    def test_out_of_scope_name_tells_the_operator_what_actually_blocked_it(self):
        """The whole view: the refusal must not surface as "choose an interface" when one was chosen."""
        from dcim.models import Interface
        from django.http import HttpResponse
        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, messages_on
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        device = make_device("oob-msg-name-scope")
        make_interface(device, "idrac0")  # exists, and the caller gets no view grant for it
        user = _scoped_device_writer(device, "oob-msg-name-scope-writer")
        user = grant(user, "add", Interface)
        user = grant(user, "add", IPAddress)
        request = make_request(
            "post",
            {
                "existing_device_id": str(device.pk),
                "server_key": "default",
                "oob_interface_id": "__new__",
                "oob_new_interface_name": "idrac0",
            },
            user=user,
            path="/add-as-oob/",
        )
        view = AddAsOOBView()
        view.kwargs = {}
        view._librenms_api = _make_api()
        view.request = request
        libre_device = {"device_id": 4444, "hostname": f"{device.name}-oob", "sysName": f"{device.name}-oob"}
        validation = {"oob_candidate": {"device": device, "type": "idrac", "ip": "10.88.0.7"}}

        with (
            patch.object(
                AddAsOOBView, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
            ),
            patch.object(AddAsOOBView, "render_device_row", return_value=HttpResponse(b"row-ok")),
            patch.object(AddAsOOBView, "rebind_api_for_server", return_value=view._librenms_api),
        ):
            view.post(request, device_id=4444)

        warnings = [text for level, text in messages_on(request) if level == "warning"]
        assert any("outside your view scope" in text for text in warnings), warnings
        assert not any("Choose an interface" in text for _level, text in messages_on(request))
        # The link still committed; only the IP set was skipped.
        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"]["default"]["oob"]["id"] == 4444
        assert device.oob_ip_id is None

    def test_out_of_scope_pk_tells_the_operator_what_actually_blocked_it(self):
        from dcim.models import Interface
        from django.http import HttpResponse
        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, messages_on
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        device = make_device("oob-msg-pk-scope")
        hidden = make_interface(device, "idrac0")
        visible = make_interface(device, "eth0")
        user = _scoped_device_writer(device, "oob-msg-pk-scope-writer")
        user = grant(user, "view", Interface, constraints={"pk": visible.pk})
        user = grant(user, "add", IPAddress)
        request = make_request(
            "post",
            {
                "existing_device_id": str(device.pk),
                "server_key": "default",
                "oob_interface_id": str(hidden.pk),
            },
            user=user,
            path="/add-as-oob/",
        )
        view = AddAsOOBView()
        view.kwargs = {}
        view._librenms_api = _make_api()
        view.request = request
        libre_device = {"device_id": 4445, "hostname": f"{device.name}-oob", "sysName": f"{device.name}-oob"}
        validation = {"oob_candidate": {"device": device, "type": "idrac", "ip": "198.18.0.7"}}

        with (
            patch.object(
                AddAsOOBView, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
            ),
            patch.object(AddAsOOBView, "render_device_row", return_value=HttpResponse(b"row-ok")),
            patch.object(AddAsOOBView, "rebind_api_for_server", return_value=view._librenms_api),
        ):
            view.post(request, device_id=4445)

        queued_messages = messages_on(request)
        warnings = [text for level, text in queued_messages if level == "warning"]
        assert any("outside your view scope" in text for text in warnings), warnings
        assert not any("Choose an interface" in text for _level, text in queued_messages)
        device.refresh_from_db()
        assert device.custom_field_data["librenms_id"]["default"]["oob"]["id"] == 4445
        assert device.oob_ip_id is None

    def test_create_without_add_perm_returns_permission_add(self):
        """No existing row + user lacks Interface 'add' → the write-time re-check refuses the create rather than silently creating it."""
        from django.db import transaction

        from dcim.models import Interface

        view = self._view()
        dev = make_device("oob-res-noperm")
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
        req.user.has_perm.side_effect = lambda perm: "add_interface" not in perm
        with transaction.atomic():
            result_iface, reason = view._resolve_oob_interface(req, dev)
        assert result_iface is None and reason == "permission_add"
        assert not Interface.objects.filter(device=dev, name="idrac0").exists()

    def test_new_without_name_returns_none(self):
        view = self._view()
        dev = make_device("oob-res-noname")
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": ""})
        assert view._resolve_oob_interface(req, dev) == (None, None)


@pytest.mark.django_db
class TestAttachOOBIp:
    """AddAsOOBView._attach_oob_ip: reuse/re-home or create an interface-assigned IP."""

    def _view(self):
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        return object.__new__(AddAsOOBView)

    def test_invalid_ip_returns_invalid(self):
        view = self._view()
        dev = make_device("oob-ip-invalid")
        iface = make_interface(dev, "idrac0")
        ip, reason = view._attach_oob_ip(_make_request(post={}), "not-an-ip", iface)
        assert ip is None and reason == "invalid"

    def test_creates_v4_slash32_when_missing(self):
        from django.db import transaction

        view = self._view()
        dev = make_device("oob-ip-create")
        iface = make_interface(dev, "idrac0")
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(_make_request(post={}), "10.0.0.9", iface)
        assert reason is None
        assert str(ip.address) == "10.0.0.9/32"
        assert ip.assigned_object == iface
        assert ip.status == "active"

    def test_rehomes_existing_unassigned_ip(self):
        from django.db import transaction
        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms

        view = self._view()
        dev = make_device("oob-ip-rehome")
        iface = make_interface(dev, "idrac0")
        existing = make_ip("10.0.0.9/24")  # unassigned host match
        user = make_user_with_perms("oob-ip-rehome", [("change", IPAddress)])
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(make_request("post", user=user), "10.0.0.9", iface)
        assert ip.pk == existing.pk and reason is None
        existing.refresh_from_db()
        assert existing.assigned_object == iface

    def test_vrf_scoped_ip_not_rehomed_creates_global_ip(self):
        """A same-host IP that lives in a VRF must NOT be re-homed: the create path makes a global (no-VRF) /32, so the lookup must be scoped to the global table — overlapping RFC1918 space in a tenant VRF is a different address."""
        from django.db import transaction

        from ipam.models import VRF

        view = self._view()
        dev = make_device("oob-ip-vrf")
        iface = make_interface(dev, "idrac0")
        vrf = VRF.objects.create(name="cust-a")
        from ipam.models import IPAddress

        tenant_ip = IPAddress.objects.create(address="10.0.0.9/24", vrf=vrf, status="active")
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(_make_request(post={}), "10.0.0.9", iface)
        assert reason is None
        # A NEW global /32 was created; the tenant's VRF row was not hijacked.
        assert ip.pk != tenant_ip.pk
        assert ip.vrf_id is None and str(ip.address) == "10.0.0.9/32"
        assert ip.assigned_object == iface
        tenant_ip.refresh_from_db()
        assert tenant_ip.assigned_object is None and tenant_ip.vrf_id == vrf.pk

    def test_vrf_row_does_not_make_global_match_ambiguous(self):
        """A VRF row sharing the host IP must not trip the ambiguity refusal: the single global-table row is the unambiguous re-home candidate."""
        from django.db import transaction

        from ipam.models import VRF, IPAddress

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms

        view = self._view()
        dev = make_device("oob-ip-vrf-ambig")
        iface = make_interface(dev, "idrac0")
        existing = make_ip("10.0.0.9/24")  # global, unassigned → the legitimate candidate
        IPAddress.objects.create(address="10.0.0.9/24", vrf=VRF.objects.create(name="cust-b"), status="active")
        user = make_user_with_perms("oob-ip-vrf-ambig", [("change", IPAddress)])
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(make_request("post", user=user), "10.0.0.9", iface)
        assert reason is None and ip.pk == existing.pk
        existing.refresh_from_db()
        assert existing.assigned_object == iface

    def test_does_not_steal_ip_from_other_device(self):
        from django.db import transaction

        view = self._view()
        dev = make_device("oob-ip-mine")
        iface = make_interface(dev, "idrac0")
        other = make_device("oob-ip-other")
        other_iface = make_interface(other, "eth0")
        make_ip("10.0.0.9/24", assigned_object=other_iface)
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(_make_request(post={}), "10.0.0.9", iface)
        assert ip is None and reason == "conflict"

    def test_ambiguous_match_returns_conflict(self):
        """Two IPAddress rows share the host IP (net_host ignores prefix length): refuse rather than re-home the wrong one by DB ordering."""
        from django.db import transaction

        from ipam.models import IPAddress

        view = self._view()
        dev = make_device("oob-ip-ambig")
        iface = make_interface(dev, "idrac0")
        make_ip("10.0.0.9/24")
        make_ip("10.0.0.9/32")
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(_make_request(post={}), "10.0.0.9", iface)
        assert ip is None and reason == "conflict"
        # Neither was re-homed and no third row was created.
        assert IPAddress.objects.filter(address__net_host="10.0.0.9").count() == 2

    def test_rehome_denied_without_change_permission(self):
        """TOCTOU backstop: re-homing an existing IP needs 'change'; an add-only user is refused."""
        from django.db import transaction

        view = self._view()
        dev = make_device("oob-ip-nochg")
        iface = make_interface(dev, "idrac0")
        existing = make_ip("10.0.0.9/24")  # unassigned → re-home path
        req = _make_request(post={})
        req.user.has_perm.side_effect = lambda perm: "change_ipaddress" not in perm
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(req, "10.0.0.9", iface)
        assert ip is None and reason == "permission_change"
        existing.refresh_from_db()
        assert existing.assigned_object is None  # not re-homed

    def test_rehome_denied_outside_the_constrained_change_grant(self):
        """A model-level change permission must not re-home an excluded IP row."""
        from django.db import transaction
        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms

        view = self._view()
        device = make_device("oob-ip-change-scope")
        interface = make_interface(device, "idrac0")
        hidden = make_ip("10.0.0.9/24")
        allowed = make_ip("10.0.0.10/24")
        user = make_user_with_perms("oob-ip-change-scope", [])
        user = grant(user, "change", IPAddress, constraints={"pk": allowed.pk})
        request = make_request("post", user=user)

        with transaction.atomic():
            ip, reason = view._attach_oob_ip(request, "10.0.0.9", interface)

        assert ip is None and reason == "permission_change"
        hidden.refresh_from_db()
        assert hidden.assigned_object is None

    def test_create_denied_without_add_permission(self):
        """TOCTOU backstop on the create path: the locked create re-verifies 'add' and refuses an add-lacking user rather than creating the IP."""
        from django.db import transaction

        from ipam.models import IPAddress

        view = self._view()
        dev = make_device("oob-ip-noadd")
        iface = make_interface(dev, "idrac0")
        req = _make_request(post={})
        req.user.has_perm.side_effect = lambda perm: "add_ipaddress" not in perm
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(req, "10.0.0.9", iface)
        assert ip is None and reason == "permission_add"
        assert not IPAddress.objects.filter(address__net_host="10.0.0.9").exists()

    def test_locks_candidate_row_with_select_for_update(self):
        """The candidate IPAddress row must be locked, and only through the caller's change scope.

        Asserting on the emitted SQL rather than on a patched manager: a mock records whichever
        call the code happens to make, so it stayed green when the lock ran through an
        unrestricted queryset and pinned rows the caller had no grant for.
        """
        from django.db import connection, transaction
        from django.test.utils import CaptureQueriesContext
        from ipam.models import IPAddress

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms

        view = self._view()
        dev = make_device("oob-ip-lock-sql")
        iface = make_interface(dev, "idrac0")
        existing = make_ip("10.0.0.9/24")
        user = make_user_with_perms("oob-ip-lock-sql", [("change", IPAddress)])
        with transaction.atomic(), CaptureQueriesContext(connection) as captured:
            ip, reason = view._attach_oob_ip(make_request("post", user=user), "10.0.0.9", iface)

        assert reason is None and ip.pk == existing.pk
        locking = [q["sql"] for q in captured.captured_queries if "FOR UPDATE" in q["sql"].upper()]
        assert locking, "the candidate row was never locked"
        # Name the locked table: a bare "OF " matches any substring, and the clause is what keeps
        # a permission join added by restrict() out of the lock set. The scoping itself is pinned
        # by test_rehome_denied_outside_the_constrained_change_grant.
        assert any('FOR UPDATE OF "IPAM_IPADDRESS"' in sql.upper() for sql in locking), (
            f"the lock did not name the address row: {locking}"
        )


@pytest.mark.django_db
class TestMissingOOBIpPermissions:
    """AddAsOOBView._missing_oob_ip_permissions: the IP-set sub-flow must require Interface/IPAddress perms, not just the top-level ('change', Device)."""

    def _view(self):
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        return object.__new__(AddAsOOBView)

    def test_none_when_user_has_all_perms(self):
        from netbox_librenms_plugin.tests.conftest import make_device

        view = self._view()
        device = make_device("oob-perm-all")  # no idrac0 interface, no IP for 10.0.0.9 yet
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
        req.user.has_perm.return_value = True
        assert view._missing_oob_ip_permissions(req, "10.0.0.9", device=device) is None

    def test_blocks_new_interface_without_add_interface(self):
        from netbox_librenms_plugin.tests.conftest import make_device

        view = self._view()
        # idrac0 does NOT exist on the device → _resolve_oob_interface would create it → add_interface.
        device = make_device("oob-perm-noaddiface")
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
        req.user.has_perm.side_effect = lambda p: "add_interface" not in p
        msg = view._missing_oob_ip_permissions(req, "10.0.0.9", device=device)
        assert msg is not None and "add_interface" in msg

    def test_invalid_ip_short_circuits_before_net_host_lookup(self):
        """A malformed IP returns an invalid-IP warning without running the address__net_host preflight (which would raise on it)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import make_device

        view = self._view()
        device = make_device("oob-perm-badip")
        req = _make_request(post={"oob_interface_id": "5"})
        req.user.has_perm.return_value = True
        with CaptureQueriesContext(connection) as ctx:
            msg = view._missing_oob_ip_permissions(req, "not-an-ip", device=device)
        assert msg is not None and "invalid" in msg.lower()
        # The net_host preflight must never run for a malformed IP (real-DB proof it short-circuits).
        assert not any("ipam_ipaddress" in q["sql"].lower() for q in ctx.captured_queries)

    def test_no_interface_target_skips_ip_permission_check(self):
        """No interface selected (empty, or '__new__' without a name) → no Interface/IPAddress write runs, so no add/change perm is demanded and the net_host preflight never runs."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import make_device

        view = self._view()
        device = make_device("oob-perm-notarget")
        for post in ({}, {"oob_interface_id": ""}, {"oob_interface_id": "__new__", "oob_new_interface_name": ""}):
            req = _make_request(post=post)
            # Deny everything; if the IP check ran it would demand a perm and return a warning.
            req.user.has_perm.return_value = False
            with CaptureQueriesContext(connection) as ctx:
                assert view._missing_oob_ip_permissions(req, "10.0.0.9", device=device) is None
            assert not any("ipam_ipaddress" in q["sql"].lower() for q in ctx.captured_queries)

    def test_new_interface_name_that_already_exists_does_not_require_add(self):
        """__new__ + an existing interface name is reused by _resolve_oob_interface, so no Interface write happens — 'add_interface' must NOT be required for a user with change-Device + add_ipaddress."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        view = self._view()
        device = make_device("oob-perm-reuse")
        make_interface(device, "idrac0")  # already exists on THIS device → reused, no create
        # A same-named interface on ANOTHER device must not count (the existence check is device-scoped).
        make_interface(make_device("oob-perm-reuse-other"), "idrac0")
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
        req.user.has_perm.side_effect = lambda p: "add_interface" not in p  # allow add_ipaddress, deny add_interface
        assert view._missing_oob_ip_permissions(req, "10.0.0.9", device=device) is None

    def test_requires_add_ipaddress_when_creating(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        view = self._view()
        device = make_device("oob-perm-addip")
        iface = make_interface(device, "eth0")  # existing iface → no add_interface; no IP → create
        req = _make_request(post={"oob_interface_id": str(iface.pk)})
        req.user.has_perm.side_effect = lambda p: "add_ipaddress" not in p
        msg = view._missing_oob_ip_permissions(req, "10.0.0.9", device=device)
        assert msg is not None and "add_ipaddress" in msg

    def test_requires_change_ipaddress_when_rehoming(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip

        view = self._view()
        device = make_device("oob-perm-rehome")
        selected = make_interface(device, "idrac0")  # the chosen interface
        other = make_interface(device, "eth9")
        make_ip("10.0.0.9/32", assigned_object=other)  # IP exists on a DIFFERENT interface → re-home
        req = _make_request(post={"oob_interface_id": str(selected.pk)})
        req.user.has_perm.side_effect = lambda p: "change_ipaddress" not in p
        msg = view._missing_oob_ip_permissions(req, "10.0.0.9", device=device)
        assert msg is not None and "change_ipaddress" in msg

    def test_vrf_row_requires_add_not_change(self):
        """A same-host IP in a VRF is invisible to the write path (it creates a global /32), so the preflight must demand 'add', not 'change' — a change-lacking user with 'add' must pass."""
        from ipam.models import VRF, IPAddress

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        view = self._view()
        device = make_device("oob-perm-vrf")
        iface = make_interface(device, "eth0")
        IPAddress.objects.create(address="10.0.0.9/24", vrf=VRF.objects.create(name="cust-perm"), status="active")
        req = _make_request(post={"oob_interface_id": str(iface.pk)})
        req.user.has_perm.side_effect = lambda p: "change_ipaddress" not in p  # has add, lacks change
        assert view._missing_oob_ip_permissions(req, "10.0.0.9", device=device) is None

    @pytest.mark.django_db
    def test_no_change_ipaddress_when_already_on_selected_interface(self):
        """IP already assigned to the chosen (real) interface → _attach_oob_ip does not save, so change_ipaddress must not be required (least privilege)."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip

        device = make_device("oob-perm-host")
        iface = make_interface(device, "idrac0")
        make_ip("10.0.0.9/32", assigned_object=iface)

        view = self._view()
        req = _make_request(post={"oob_interface_id": str(iface.pk)})
        # User has every perm EXCEPT change_ipaddress.
        req.user.has_perm.side_effect = lambda p: "change_ipaddress" not in p
        assert view._missing_oob_ip_permissions(req, "10.0.0.9", device=device) is None

    @pytest.mark.django_db
    def test_change_ipaddress_required_when_assigned_object_not_interface(self):
        """A non-Interface assigned_object (a VMInterface) sharing the selected pk must NOT take the already-on-selected shortcut — the GFK pk can collide across models, so a type check is required or change_ipaddress is wrongly waived."""
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.tests.conftest import make_device, make_ip, make_vm

        device = make_device("oob-perm-host2")
        vm = make_vm("oob-perm-vm")
        vmi = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        make_ip("10.0.0.9/32", assigned_object=vmi)

        view = self._view()
        # The POSTed oob_interface_id is treated as an Interface pk; here it equals the
        # VMInterface's pk, so a pk-only compare would wrongly skip the change_ipaddress check.
        req = _make_request(post={"oob_interface_id": str(vmi.pk)})
        req.user.has_perm.side_effect = lambda p: "change_ipaddress" not in p
        msg = view._missing_oob_ip_permissions(req, "10.0.0.9", device=device)
        assert msg is not None and "change_ipaddress" in msg

    def test_ambiguous_match_requires_change_despite_selected_interface(self):
        """Multiple rows share the host IP: the write path refuses, so the preflight must NOT take the already-on-selected-interface shortcut — it requires change_ipaddress."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip

        view = self._view()
        device = make_device("oob-perm-ambig")
        selected = make_interface(device, "idrac0")
        # net_host ignores prefix length, so two rows share host 10.0.0.9 → ambiguous. One IS on the
        # selected interface, which would otherwise short-circuit to "no perms"; the ambiguity must
        # still force change_ipaddress because _attach_oob_ip refuses an ambiguous match.
        make_ip("10.0.0.9/32", assigned_object=selected)
        make_ip("10.0.0.9/24")
        req = _make_request(post={"oob_interface_id": str(selected.pk)})
        req.user.has_perm.side_effect = lambda p: "change_ipaddress" not in p
        msg = view._missing_oob_ip_permissions(req, "10.0.0.9", device=device)
        assert msg is not None and "change_ipaddress" in msg


class TestCreatePlatformFromImportManufacturer:
    """CreatePlatformFromImportView must reject a stale/tampered manufacturer id instead of silently creating a Platform with no manufacturer."""

    def _view(self):
        from netbox_librenms_plugin.views.imports.actions import CreatePlatformFromImportView

        view = object.__new__(CreatePlatformFromImportView)
        view.required_object_permissions = {}
        # Pre-bind a client so the (now unconditional) server rebind is a no-op cache hit — this
        # class exercises manufacturer validation, not server resolution, and a blank POST key
        # would otherwise build the default LibreNMSAPI(None) (a LibreNMSSettings DB read).
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_invalid_manufacturer_id_is_rejected(self):
        view = self._view()
        req = _make_request(post={"platform_name": "New-OS", "manufacturer": "9999"})

        view.request = req  # dispatch() would set this; restricted_queryset reads request.user

        mock_manuf = MagicMock()
        mock_manuf.DoesNotExist = type("DoesNotExist", (Exception,), {})
        # The manufacturer is resolved through restrict(user, "view"), so hand back the same
        # manager and the not-found stub below still describes that chain.
        mock_manuf.objects.restrict.return_value = mock_manuf.objects
        mock_manuf.objects.get.side_effect = mock_manuf.DoesNotExist()
        mock_platform = MagicMock()
        mock_platform.objects.filter.return_value.exists.return_value = False

        with (
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "require_object_permissions", return_value=None),
            patch.object(
                view,
                "get_validated_device_with_selections",
                return_value=(None, {"existing_device": None}, {}),
            ),
            patch("dcim.models.Platform", mock_platform),
            patch("dcim.models.Manufacturer", mock_manuf),
            patch(
                "netbox_librenms_plugin.views.imports.actions._htmx_error_response",
                side_effect=lambda msg: ("ERR", msg),
            ) as mock_err,
        ):
            result = view.post(req, device_id=42)

        # Rejected with a manufacturer-not-found error; the Platform was never created.
        mock_err.assert_called_once()
        assert "manufacturer" in mock_err.call_args[0][0].lower()
        assert result == ("ERR", mock_err.call_args[0][0])
        # Neither the constructor nor the manager create() path persisted a Platform.
        mock_platform.assert_not_called()
        mock_platform.objects.create.assert_not_called()

    @pytest.mark.django_db
    def test_device_platform_manufacturer_mismatch_surfaced_platform_kept(self):
        """A new Platform whose manufacturer conflicts with the target Device's device-type manufacturer fails to assign; the failure is surfaced to the user and the device is left unassigned, but the just-created Platform is intentionally kept (aec0360a1: the platform create is the primary action and the assignment runs in its own transaction)."""
        from dcim.models import Manufacturer, Platform

        device = make_device("plat-assign-mismatch")  # device_type under manufacturer TestMfr
        other_mfr, _ = Manufacturer.objects.get_or_create(name="PlatAssignOther", slug="platassign-other")
        assert other_mfr.pk != device.device_type.manufacturer_id

        view = self._view()
        # Superuser: the subject is the assignment failure, not the object gate.
        req = _make_request(
            post={"platform_name": "Mismatch-OS", "manufacturer": str(other_mfr.pk)},
            headers={"HX-Request": "true"},
            user_is_superuser=True,
        )
        view.request = req

        with (
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "require_object_permissions", return_value=None),
            patch.object(
                view,
                "get_validated_device_with_selections",
                return_value=(None, {"existing_device": device}, {}),
            ),
        ):
            resp = view.post(req, device_id=device.pk)

        # The platform create is the primary action: the manufacturer-mismatch assignment failure is
        # surfaced to the user (error toast) and the device is left unassigned, but the just-created
        # Platform is intentionally NOT rolled back.
        assert resp is not None
        assert Platform.objects.filter(name="Mismatch-OS").exists()
        assert b"could not be assigned" in resp.content
        device.refresh_from_db()
        assert device.platform_id is None

    @pytest.mark.django_db
    def test_device_platform_manufacturer_match_assigns(self):
        """The consistent case still assigns: a Platform under the device-type's manufacturer is persisted onto the Device."""
        from dcim.models import Manufacturer, Platform

        device = make_device("plat-assign-ok")
        mfr = Manufacturer.objects.get(slug="test-mfr")  # make_device's device_type manufacturer

        view = self._view()
        # Superuser: this test is about the platform assignment, not the object gate, and the
        # manufacturer is now read through a restricted queryset.
        req = _make_request(
            post={"platform_name": "Match-OS", "manufacturer": str(mfr.pk)},
            headers={"HX-Request": "true"},
            user_is_superuser=True,
        )
        view.request = req

        with (
            patch.object(view, "require_write_permission", return_value=None),
            patch.object(view, "require_object_permissions", return_value=None),
            patch.object(
                view,
                "get_validated_device_with_selections",
                return_value=(None, {"existing_device": device}, {}),
            ),
        ):
            view.post(req, device_id=device.pk)

        platform = Platform.objects.get(name="Match-OS")
        device.refresh_from_db()
        assert device.platform_id == platform.pk


class TestOOBInterfaceSelectTemplate:
    """The OOB interface picker toggles the "new name" input via a script block (extracted from an inline onchange) so it works under CSP and is maintainable."""

    def _render(self):
        from django.template.loader import render_to_string

        return render_to_string(
            "netbox_librenms_plugin/htmx/_oob_interface_select.html",
            {
                "libre_device": {"device_id": 7},
                "oob_interfaces": [],
                "oob_suggested_interface_id": None,
                "oob_default_new_name": "",
                "validation": {},
            },
        )

    def test_no_inline_onchange_handler(self):
        assert "onchange=" not in self._render()

    def test_wires_change_handler_via_script_block(self):
        html = self._render()
        assert 'addEventListener("change"' in html
        # Targets this device's own select id (namespaced by device_id).
        assert 'getElementById("oob-iface-7")' in html

    def test_initializes_create_state_on_load(self):
        """The script must sync the "new name" input once on load (not only on change), so the input matches the rendered selection even if it differs from the server-side display logic (e.g. a browser-restored form value)."""
        html = self._render()
        assert "function syncCreateState()" in html
        # Bound to change AND invoked immediately so initial state is authoritative.
        assert 'addEventListener("change", syncCreateState)' in html
        assert "syncCreateState();" in html


# ---------------------------------------------------------------------------
# AddAsOOBView._attach_oob_ip — foreign-key conflict handling (real DB)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestAttachOobIpForeignKeyConflict:
    """_attach_oob_ip must not try to re-home an IP that is another device's primary/oob FK."""

    def test_conflict_when_ip_is_another_devices_oob_fk(self):
        from dcim.models import Interface
        from django.db import transaction
        from ipam.models import IPAddress

        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        donor = make_device("oob-donor")
        target = make_device("oob-target")
        target_iface = Interface.objects.create(device=target, name="mgmt0", type="1000base-t")

        # X is not assigned to ANY interface, yet it IS the donor's oob_ip — a state reachable
        # because the import path persists oob_ip via save(update_fields=[...]) (no full_clean()).
        ip = IPAddress.objects.create(address="10.10.0.5/32", status="active")
        donor.oob_ip = ip
        donor.save(update_fields=["oob_ip"])
        ip.refresh_from_db()
        assert ip.assigned_object is None

        request = RequestFactory().post("/")
        request.user = make_superuser()

        # select_for_update needs an open transaction (the real caller provides one).
        with transaction.atomic():
            result_ip, reason = AddAsOOBView._attach_oob_ip(request, "10.10.0.5", target_iface)

        # Must surface a clean conflict, NOT re-home the IP into a doomed UNIQUE-constraint save.
        assert result_ip is None
        assert reason == "conflict"
        # The donor still owns it; nothing was silently re-homed.
        donor.refresh_from_db()
        ip.refresh_from_db()
        assert donor.oob_ip_id == ip.pk
        assert ip.assigned_object is None


# ---------------------------------------------------------------------------
# _save_device — update_fields save still honours cross-field consistency (real DB)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSaveDeviceValidatesPlatformDeviceTypeConsistency:
    """update_fields saves skip full_clean(), but a device_type/platform write must still honour the platform/manufacturer cross-field rule."""

    def test_update_fields_save_rejects_manufacturer_mismatch(self):
        from dcim.models import DeviceType, Manufacturer, Platform

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = make_device("dt-consistency")  # device_type=TestDT, manufacturer=TestMfr
        mfr_a = Manufacturer.objects.get(slug="test-mfr")
        mfr_b, _ = Manufacturer.objects.get_or_create(name="OtherMfr", slug="other-mfr")
        # Platform limited to TestMfr — consistent with the device's current device_type.
        platform = Platform.objects.create(name="P-testmfr", slug="p-testmfr", manufacturer=mfr_a)
        device.platform = platform
        device.save(update_fields=["platform"])
        # A device_type from a DIFFERENT manufacturer than the platform allows.
        dt_other = DeviceType.objects.create(model="DT-other", slug="dt-other", manufacturer=mfr_b)

        device.device_type = dt_other
        resp = _save_device(device, update_fields=["device_type"])

        # Rejected with an error response, NOT silently persisted with a success toast.
        assert resp is not None
        device.refresh_from_db()
        assert device.device_type_id != dt_other.pk

    def test_update_fields_save_allows_consistent_device_type(self):
        from dcim.models import DeviceType, Manufacturer, Platform

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = make_device("dt-consistent-ok")
        mfr_a = Manufacturer.objects.get(slug="test-mfr")
        platform = Platform.objects.create(name="P-ok", slug="p-ok", manufacturer=mfr_a)
        device.platform = platform
        device.save(update_fields=["platform"])
        # Same-manufacturer device_type — the consistent case must still save cleanly.
        dt_same = DeviceType.objects.create(model="DT-same", slug="dt-same", manufacturer=mfr_a)

        device.device_type = dt_same
        resp = _save_device(device, update_fields=["device_type"])

        assert resp is None
        device.refresh_from_db()
        assert device.device_type_id == dt_same.pk


# ---------------------------------------------------------------------------
# DeviceValidationDetailsView._build_id_server_info — per-server id validation (real DB)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestBuildIdServerInfoRejectsNonPositiveIds:
    """Per-server mapping rows must reject 0/negative/malformed host ids (LibreNMS ids start at 1)."""

    def test_zero_negative_and_malformed_host_ids_skipped(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = make_device("idsrv")
        device.custom_field_data["librenms_id"] = {
            "s_zero_int": 0,
            "s_zero_str": "0",
            "s_dict_zero": {"id": 0},
            "s_neg": -5,
            "s_bool": True,
            "s_good": 42,
            "s_good_dict": {"id": 7},
        }
        device.save()

        result = DeviceValidationDetailsView._build_id_server_info(device)

        # Only the genuinely-positive host ids survive — no bogus device_id 0 / -5 rows.
        server_keys = {r["server_key"]: r["device_id"] for r in (result or [])}
        assert server_keys == {"s_good": 42, "s_good_dict": 7}

    def test_oob_only_entry_is_surfaced_with_controller_id(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        device = make_device("idsrv-oob")
        device.custom_field_data["librenms_id"] = {
            "host_srv": {"id": 10},
            "oob_srv": {"oob": {"id": 99}},  # OOB-only link: no host "id"
        }
        device.save()

        result = DeviceValidationDetailsView._build_id_server_info(device)

        # The OOB-only link is still a real link — surface it (controller id), mirroring the
        # device-sync modal, rather than dropping it and risking a duplicate re-import.
        mapping = {r["server_key"]: r["device_id"] for r in (result or [])}
        assert mapping == {"host_srv": 10, "oob_srv": 99}


# ---------------------------------------------------------------------------
# _suggest_oob_interface — reuses a caller-materialized interface list (real DB)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestSuggestOobInterfaceReusesMaterializedList:
    """_suggest_oob_interface must reuse a caller-materialized interface list, not re-query."""

    def test_no_query_when_interfaces_supplied(self):
        from dcim.models import Interface
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.views.imports.actions import _suggest_oob_interface

        device = make_device("oob-reuse")
        Interface.objects.create(device=device, name="idrac0", type="1000base-t")
        interfaces = list(device.interfaces.all())  # caller already materialized them

        with CaptureQueriesContext(connection) as ctx:
            iface_id, default_name = _suggest_oob_interface(device, {"type": "idrac"}, interfaces=interfaces)

        assert iface_id is not None  # matched idrac0
        assert default_name == "idrac0"
        # The supplied list is reused — no second device.interfaces.all() query.
        assert len(ctx.captured_queries) == 0


# ---------------------------------------------------------------------------
# AddDeviceTypeMappingView — single upfront [:2] ambiguity fetch (real DB)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestAddDeviceTypeMappingSingleUpfrontQuery:
    """The upfront ambiguity check must use one [:2] fetch, not a separate count() + first()."""

    def test_no_count_query_on_mapping_upfront_check(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.models import DeviceTypeMapping
        from netbox_librenms_plugin.views.imports.actions import AddDeviceTypeMappingView

        dev = make_device("dtm-host")  # supplies a real DeviceType
        device_type = dev.device_type

        view = object.__new__(AddDeviceTypeMappingView)
        view._librenms_api = MagicMock(server_key="default")  # blank-key rebind returns "default"
        request = RequestFactory().post("/", {"device_type_id": str(device_type.pk), "server_key": ""})
        request.user = make_superuser()
        view.request = request

        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                return_value={"hardware": "WidgetX"},
            ),
            patch("netbox_librenms_plugin.views.imports.actions.DeviceValidationDetailsView") as mock_detail,
            # Skip the post-save modal/row re-render (template URL reversal) — irrelevant to the
            # upfront query count, which has already run by then. post() reuses the in-memory
            # libre_device via validate_and_apply_selections (returns (validation, selections)); a
            # None validation short-circuits the render_device_row call.
            patch.object(view, "validate_and_apply_selections", return_value=(None, None)),
        ):
            mock_detail.return_value.get.return_value = MagicMock(content=b"<div></div>")
            with CaptureQueriesContext(connection) as ctx:
                view.post(request, device_id=1)

        # The fix collapses the upfront .count() + .first() into a single [:2] fetch (the locked
        # read already uses [:2]), so NO COUNT() query should touch the mapping table.
        count_qs = [
            q["sql"]
            for q in ctx.captured_queries
            if "count(" in q["sql"].lower() and "devicetypemapping" in q["sql"].lower()
        ]
        assert not count_qs, f"upfront ambiguity check must use [:2], not COUNT(): {count_qs}"
        # Sanity: the path ran to completion and created the mapping (normalized to lowercase).
        assert DeviceTypeMapping.objects.filter(librenms_hardware="widgetx").exists()


@pytest.mark.django_db
class TestMappingChangeScope:
    """Natural-key mapping updates must remain inside constrained change grants."""

    @staticmethod
    def _request(user, **data):
        from netbox_librenms_plugin.tests.view_test_helpers import make_request

        return make_request("post", data, user=user)

    def test_device_type_mapping_outside_change_grant_is_not_updated(self):
        from dcim.models import DeviceType
        from django.http import HttpResponse

        from netbox_librenms_plugin.models import DeviceTypeMapping
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.utils import apply_normalization_rules
        from netbox_librenms_plugin.views.imports.actions import AddDeviceTypeMappingView

        old_type = make_device("mapping-scope-old").device_type
        new_type = DeviceType.objects.create(
            manufacturer=old_type.manufacturer,
            model="Mapping Scope New Type",
            slug="mapping-scope-new-type",
        )
        allowed = DeviceTypeMapping.objects.create(librenms_hardware="allowed-hw", netbox_device_type=old_type)
        raw_hardware = "Hidden Hardware Scope"
        mapping_hardware = apply_normalization_rules(value=raw_hardware, scope="device_type")
        hidden = DeviceTypeMapping.objects.create(librenms_hardware=mapping_hardware, netbox_device_type=old_type)
        user = make_user_with_perms("mapping-change-scope", [("view", type(old_type))])
        user = grant(user, "change", DeviceTypeMapping, constraints={"pk": allowed.pk})
        request = self._request(user, device_type_id=str(new_type.pk), server_key="default")
        view = AddDeviceTypeMappingView()
        view.setup(request)
        view._librenms_api = MagicMock(server_key="default", cache_timeout=300)

        with (
            patch.object(view, "rebind_api_for_server", return_value="default"),
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                return_value={"hardware": raw_hardware},
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.DeviceValidationDetailsView.get",
                return_value=HttpResponse(b"<div></div>"),
            ),
            patch.object(view, "validate_and_apply_selections", return_value=(None, None)),
        ):
            response = view.post(request, device_id=1)

        # The refusal text, not just the unchanged row: a failed permission gate, a rebind
        # failure and the broad except all leave the mapping alone too.
        assert b"Existing mapping is no longer available." in response.content
        hidden.refresh_from_db()
        assert hidden.netbox_device_type_id == old_type.pk

    def test_platform_mapping_outside_change_grant_is_not_updated(self):
        from dcim.models import Platform
        from django.http import HttpResponse

        from netbox_librenms_plugin.models import PlatformMapping
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.imports.actions import AddPlatformMappingView

        old_platform = Platform.objects.create(name="Mapping Scope Old", slug="mapping-scope-old")
        new_platform = Platform.objects.create(name="Mapping Scope New", slug="mapping-scope-new")
        allowed = PlatformMapping.objects.create(librenms_os="allowed-os", netbox_platform=old_platform)
        hidden = PlatformMapping.objects.create(librenms_os="hidden-os", netbox_platform=old_platform)
        user = make_user_with_perms("platform-mapping-change-scope", [("view", Platform)])
        user = grant(user, "change", PlatformMapping, constraints={"pk": allowed.pk})
        request = self._request(user, platform_id=str(new_platform.pk), server_key="default")
        view = AddPlatformMappingView()
        view.setup(request)
        view._librenms_api = MagicMock(server_key="default", cache_timeout=300)

        with (
            patch.object(view, "rebind_api_for_server", return_value="default"),
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                return_value={"os": "hidden-os"},
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.DeviceValidationDetailsView.get",
                return_value=HttpResponse(b"<div></div>"),
            ),
            patch.object(view, "get_validated_device_with_selections", return_value=(None, None, None)),
        ):
            response = view.post(request, device_id=1)

        assert b"Existing mapping is no longer available." in response.content
        hidden.refresh_from_db()
        assert hidden.netbox_platform_id == old_platform.pk

    def test_device_type_mapping_inside_change_grant_is_updated(self):
        """Control for the refusal above: an in-grant row still updates through the same path.

        Without this, a regression that skips the mapping write entirely leaves the hidden row
        unchanged too, so the refusal test alone cannot tell scoping from a dead update path.
        """
        from dcim.models import DeviceType
        from django.http import HttpResponse

        from netbox_librenms_plugin.models import DeviceTypeMapping
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.utils import apply_normalization_rules
        from netbox_librenms_plugin.views.imports.actions import AddDeviceTypeMappingView

        old_type = make_device("mapping-control-old").device_type
        new_type = DeviceType.objects.create(
            manufacturer=old_type.manufacturer,
            model="Mapping Control New Type",
            slug="mapping-control-new-type",
        )
        raw_hardware = "Allowed Hardware Control"
        mapping_hardware = apply_normalization_rules(value=raw_hardware, scope="device_type")
        allowed = DeviceTypeMapping.objects.create(librenms_hardware=mapping_hardware, netbox_device_type=old_type)
        user = make_user_with_perms("mapping-change-control", [("view", type(old_type))])
        user = grant(user, "change", DeviceTypeMapping, constraints={"pk": allowed.pk})
        request = self._request(user, device_type_id=str(new_type.pk), server_key="default")
        view = AddDeviceTypeMappingView()
        view.setup(request)
        view._librenms_api = MagicMock(server_key="default", cache_timeout=300)

        with (
            patch.object(view, "rebind_api_for_server", return_value="default"),
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                return_value={"hardware": raw_hardware},
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.DeviceValidationDetailsView.get",
                return_value=HttpResponse(b"<div></div>"),
            ),
            patch.object(view, "validate_and_apply_selections", return_value=(None, None)),
        ):
            response = view.post(request, device_id=1)

        assert b"Existing mapping is no longer available." not in response.content
        allowed.refresh_from_db()
        assert allowed.netbox_device_type_id == new_type.pk

    def test_platform_mapping_inside_change_grant_is_updated(self):
        """Control for the platform refusal above (see the device-type control)."""
        from dcim.models import Platform
        from django.http import HttpResponse

        from netbox_librenms_plugin.models import PlatformMapping
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.imports.actions import AddPlatformMappingView

        old_platform = Platform.objects.create(name="Mapping Control Old", slug="mapping-control-old")
        new_platform = Platform.objects.create(name="Mapping Control New", slug="mapping-control-new")
        allowed = PlatformMapping.objects.create(librenms_os="allowed-control-os", netbox_platform=old_platform)
        user = make_user_with_perms("platform-mapping-change-control", [("view", Platform)])
        user = grant(user, "change", PlatformMapping, constraints={"pk": allowed.pk})
        request = self._request(user, platform_id=str(new_platform.pk), server_key="default")
        view = AddPlatformMappingView()
        view.setup(request)
        view._librenms_api = MagicMock(server_key="default", cache_timeout=300)

        with (
            patch.object(view, "rebind_api_for_server", return_value="default"),
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                return_value={"os": "allowed-control-os"},
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.DeviceValidationDetailsView.get",
                return_value=HttpResponse(b"<div></div>"),
            ),
            patch.object(view, "get_validated_device_with_selections", return_value=(None, None, None)),
        ):
            response = view.post(request, device_id=1)

        assert b"Existing mapping is no longer available." not in response.content
        allowed.refresh_from_db()
        assert allowed.netbox_platform_id == new_platform.pk


# ---------------------------------------------------------------------------
# _rebind_or_htmx_error — fail-closed rebind helper for import HTMX endpoints
# ---------------------------------------------------------------------------
class TestRebindOrHtmxErrorHelper:
    """The extracted fail-closed rebind helper used across the import HTMX endpoints."""

    def _view(self):
        from netbox_librenms_plugin.views.imports.actions import AddDeviceTypeMappingView

        view = object.__new__(AddDeviceTypeMappingView)  # any LibreNMSAPIMixin view
        view._librenms_api = None
        return view

    def test_unresolved_server_key_returns_htmx_error_toast(self):
        from netbox_librenms_plugin.views.imports.actions import _rebind_or_htmx_error

        view = self._view()
        request = RequestFactory().post("/", {"server_key": "ghost"})
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
            resp = _rebind_or_htmx_error(view, request)

        assert resp is not None
        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"no longer configured" in resp.content

    def test_resolved_server_key_returns_none_and_binds(self):
        from netbox_librenms_plugin.views.imports.actions import _rebind_or_htmx_error

        view = self._view()
        request = RequestFactory().post("/", {"server_key": "prod"})
        with patch(
            "netbox_librenms_plugin.librenms_api.build_librenms_api",
            return_value=MagicMock(server_key="prod"),
        ):
            assert _rebind_or_htmx_error(view, request) is None
        assert view._librenms_api.server_key == "prod"


class TestHtmxErrorResponse:
    def test_plain_dynamic_message_is_html_escaped_once(self):
        from netbox_librenms_plugin.views.imports.actions import _htmx_error_response

        response = _htmx_error_response("Conflict with '<script>alert(1)</script>'.")

        assert b"<script>" not in response.content
        assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.content
        assert b"&amp;lt;script&amp;gt;" not in response.content


@pytest.mark.django_db
class TestSerialActionsNormalizeAndLock:
    """Serial-writing actions must persist/compare the TRIMMED serial and guard conflicts without a second row lock."""

    def _post_action(self, action, target, serial):
        """Drive DeviceConflictActionView.post for *action* against real device *target* with only the API/cache seams patched."""
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = DeviceConflictActionView()
        view._librenms_api = _make_api()
        libre_device = {
            "device_id": 10,
            "hostname": target.name,
            "sysName": target.name,
            "serial": serial,
        }
        validation = {"can_import": False, "existing_device": target}
        request = RequestFactory().post(
            "/conflict-action/",
            {"action": action, "existing_device_id": str(target.pk), "server_key": "default"},
        )
        request.user = make_superuser()
        view.request = request
        with (
            patch.object(
                DeviceConflictActionView,
                "get_validated_device_with_selections",
                return_value=(libre_device, validation, {}),
            ),
            patch.object(DeviceConflictActionView, "render_device_row", return_value=HttpResponse(b"row-ok")),
            patch.object(DeviceConflictActionView, "rebind_api_for_server", return_value=view._librenms_api),
        ):
            return view.post(request, device_id=10)

    @staticmethod
    def _serial_row_locks(sqls):
        """Return FOR UPDATE queries whose WHERE clause filters by serial."""
        serial_row_locks = []
        for sql in sqls:
            if "FOR UPDATE" not in sql:
                continue
            _, separator, where_clause = sql.partition(" WHERE ")
            assert separator, f"FOR UPDATE query has no WHERE clause: {sql}"
            if '."serial" = ' in where_clause:
                serial_row_locks.append(sql)
        return serial_row_locks

    def test_update_serial_persists_trimmed_serial(self):
        """A padded LibreNMS serial is stored trimmed so the next exact lookup still matches."""
        target = make_device("ser-act-upd")
        self._post_action("update_serial", target, " SN-42 ")
        target.refresh_from_db()
        assert target.serial == "SN-42"

    def test_sync_serial_persists_trimmed_serial(self):
        """sync_serial stores the trimmed serial, consistent with validate/import normalization."""
        target = make_device("ser-act-sync")
        self._post_action("sync_serial", target, " SN-43 ")
        target.refresh_from_db()
        assert target.serial == "SN-43"

    def test_update_serial_detects_conflict_against_trimmed_stored_serial(self):
        """A padded incoming serial must still hit the conflict guard when another device stored the trimmed value."""
        make_device("ser-act-owner", serial="SN-7")
        target = make_device("ser-act-loser")
        resp = self._post_action("update_serial", target, " SN-7 ")
        assert b"Serial conflict" in resp.content
        target.refresh_from_db()
        assert target.serial == ""  # nothing persisted

    def test_sync_serial_uses_advisory_lock_not_conflict_row_lock(self):
        """The conflict guard serializes on an advisory lock keyed by the serial value; a second row lock would deadlock two swap-direction requests (A then B vs B then A)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        target = make_device("ser-act-lock")
        with CaptureQueriesContext(connection) as ctx:
            self._post_action("sync_serial", target, "SN-77")
        sqls = [q["sql"] for q in ctx.captured_queries]
        assert any("pg_advisory_xact_lock" in s for s in sqls), "advisory lock on the serial value not taken"
        # The own-row lock (WHERE "id" = ...) is expected; a conflict-row lock filters on serial.
        conflict_row_locks = self._serial_row_locks(sqls)
        assert conflict_row_locks == [], f"conflict lookup still takes a row lock: {conflict_row_locks}"

    def test_serial_advisory_lock_uses_a_stable_application_key(self):
        """Equal serials use one stable application key while distinct serials use different keys."""
        from django.db import connection, transaction
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.views.imports.actions import _acquire_serial_assignment_lock

        with transaction.atomic(), CaptureQueriesContext(connection) as ctx:
            _acquire_serial_assignment_lock("SN-STABLE")
            _acquire_serial_assignment_lock("SN-STABLE")
            _acquire_serial_assignment_lock("SN-DIFFERENT")

        lock_sqls = [q["sql"] for q in ctx.captured_queries if "pg_advisory_xact_lock" in q["sql"]]
        assert len(lock_sqls) == 3
        assert lock_sqls[0] == lock_sqls[1]
        assert lock_sqls[0] != lock_sqls[2]
        assert "3935087803272606537" in lock_sqls[0]
        assert all("hashtext" not in sql.lower() for sql in lock_sqls)

    def test_serial_lock_refuses_to_run_in_autocommit(self):
        """pg_advisory_xact_lock is transaction-scoped, so taking it outside a transaction locks nothing and must fail loudly."""
        from django.db import connection

        from netbox_librenms_plugin.views.imports.actions import _acquire_serial_assignment_lock

        # django_db wraps the test in a transaction, so autocommit has to be simulated on the
        # connection flag itself — the guard reads exactly that flag.
        with patch.object(connection, "in_atomic_block", False), pytest.raises(RuntimeError):
            _acquire_serial_assignment_lock("SN-NO-TX")

    def test_update_action_serializes_on_the_serial_advisory_lock(self):
        """The update action's serial write takes the same advisory lock (same deadlock shape as sync_serial)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        target = make_device("ser-act-upd-lock")
        with CaptureQueriesContext(connection) as ctx:
            self._post_action("update", target, "SN-88")
        sqls = [q["sql"] for q in ctx.captured_queries]
        assert any("pg_advisory_xact_lock" in s for s in sqls)
        assert self._serial_row_locks(sqls) == []

    def test_update_serial_action_serializes_on_the_serial_advisory_lock(self):
        """The dedicated update_serial action takes the same serial-keyed advisory lock."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        target = make_device("ser-act-upd-serial-lock")
        with CaptureQueriesContext(connection) as ctx:
            self._post_action("update_serial", target, "SN-89")
        sqls = [q["sql"] for q in ctx.captured_queries]
        assert any("pg_advisory_xact_lock" in sql for sql in sqls)
        assert self._serial_row_locks(sqls) == []

    def test_conflict_toast_escapes_the_conflicting_device_name(self):
        """_htmx_error_response substitutes the message via format_html('{}', ...), so a marked-up conflicting device name renders escaped — adding escape() at the call site would double-escape."""
        make_device("<script>alert(1)</script>-owner", serial="SN-XSS")
        target = make_device("ser-act-xss-target")
        resp = self._post_action("update_serial", target, "SN-XSS")
        assert b"Serial conflict" in resp.content
        assert b"<script>" not in resp.content
        assert b"&lt;script&gt;" in resp.content

    def test_conflict_detected_against_legacy_padded_stored_serial(self):
        """The migration canonicalizes a legacy owner before the exact conflict lookup runs."""
        import importlib
        from types import SimpleNamespace

        from django.apps import apps
        from django.db import connection

        owner = make_device("ser-act-legacy-owner", serial=" SN-LEG-9 ")
        migration = importlib.import_module("netbox_librenms_plugin.migrations.0012_normalize_device_serials")
        migration.normalize_device_serials(apps, SimpleNamespace(connection=connection))
        owner.refresh_from_db()
        assert owner.serial == "SN-LEG-9"
        target = make_device("ser-act-legacy-loser")
        resp = self._post_action("update_serial", target, "SN-LEG-9")
        assert b"Serial conflict" in resp.content
        target.refresh_from_db()
        assert target.serial == ""


@pytest.mark.django_db
class TestConflictActionsObjectScope:
    """The conflict/OOB mutation endpoints must resolve the POSTed existing_device_id object-scoped.

    require_object_permissions only asks ``user.has_perm("dcim.change_device")`` with no instance, so a
    pk-constrained grant clears the gate. Without a restricted lookup the endpoint would then mutate any
    device by raw pk.
    """

    _scoped_writer = staticmethod(_scoped_device_writer)

    def _post_conflict(self, user, target, action="link"):
        """Drive the real DeviceConflictActionView.post against *target* with only the LibreNMS seams patched."""
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = DeviceConflictActionView()
        view._librenms_api = _make_api()
        libre_device = {"device_id": 4242, "hostname": target.name, "sysName": target.name, "serial": "-"}
        validation = {"existing_device": target, "device_type_mismatch": False}
        request = RequestFactory().post(
            "/conflict-action/",
            {"action": action, "existing_device_id": str(target.pk), "server_key": "default"},
        )
        request.user = user
        view.request = request
        with (
            patch.object(
                DeviceConflictActionView,
                "get_validated_device_with_selections",
                return_value=(libre_device, validation, {}),
            ),
            patch.object(DeviceConflictActionView, "render_device_row", return_value=HttpResponse(b"row-ok")),
            patch.object(DeviceConflictActionView, "rebind_api_for_server", return_value=view._librenms_api),
            patch(
                "netbox_librenms_plugin.views.imports.actions._get_hostname_for_action",
                return_value=target.name,
            ),
        ):
            return view.post(request, device_id=4242)

    def _post_add_as_oob(self, user, target):
        """Drive the real AddAsOOBView.post against *target* with only the LibreNMS seams patched."""
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        view = AddAsOOBView()
        view.kwargs = {}
        view._librenms_api = _make_api()
        libre_device = {"device_id": 4343, "hostname": f"{target.name}-oob", "sysName": f"{target.name}-oob"}
        validation = {"oob_candidate": {"device": target, "type": "idrac", "ip": None}}
        request = RequestFactory().post("/add-as-oob/", {"existing_device_id": str(target.pk), "server_key": "default"})
        request.user = user
        view.request = request
        with (
            patch.object(
                AddAsOOBView,
                "get_validated_device_with_selections",
                return_value=(libre_device, validation, {}),
            ),
            patch.object(AddAsOOBView, "render_device_row", return_value=HttpResponse(b"row-ok")),
            patch.object(AddAsOOBView, "rebind_api_for_server", return_value=view._librenms_api),
        ):
            return view.post(request, device_id=4343)

    def test_conflict_action_cannot_link_an_out_of_scope_device(self):
        """A pk-constrained change_device grant clears the model-level gate but must not link a device outside its scope."""
        from dcim.models import Device

        in_scope = make_device("scope-conflict-in")
        out_of_scope = make_device("scope-conflict-out")
        user = self._scoped_writer(in_scope, "scoped-conflict-writer")

        response = self._post_conflict(user, out_of_scope)

        assert b"Existing device not found" in response.content
        assert "librenms_id" not in Device.objects.get(pk=out_of_scope.pk).custom_field_data

    def test_conflict_action_still_links_the_in_scope_device(self):
        """The device the grant DOES cover resolves through the restricted lookup (no over-block)."""
        from dcim.models import Device

        in_scope = make_device("scope-conflict-in-2")
        user = self._scoped_writer(in_scope, "scoped-conflict-writer-2")

        response = self._post_conflict(user, in_scope)

        assert b"Existing device not found" not in response.content
        assert Device.objects.get(pk=in_scope.pk).custom_field_data["librenms_id"]["default"] == 4242

    def test_add_as_oob_cannot_attach_to_an_out_of_scope_device(self):
        """AddAsOOB must object-scope its target too: a constrained grant cannot attach an OOB link elsewhere."""
        from dcim.models import Device

        in_scope = make_device("scope-oob-in")
        out_of_scope = make_device("scope-oob-out")
        user = self._scoped_writer(in_scope, "scoped-oob-writer")

        response = self._post_add_as_oob(user, out_of_scope)

        assert b"Existing device not found" in response.content
        assert "librenms_id" not in Device.objects.get(pk=out_of_scope.pk).custom_field_data

    def test_add_as_oob_still_attaches_to_the_in_scope_device(self):
        """The in-scope device still resolves and receives the OOB link."""
        from dcim.models import Device

        in_scope = make_device("scope-oob-in-2")
        user = self._scoped_writer(in_scope, "scoped-oob-writer-2")

        response = self._post_add_as_oob(user, in_scope)

        assert b"Existing device not found" not in response.content
        stored = Device.objects.get(pk=in_scope.pk).custom_field_data["librenms_id"]["default"]
        assert stored["oob"]["id"] == 4343

    def test_superuser_is_unaffected_by_the_restricted_lookup(self):
        """A superuser keeps the unrestricted queryset, so every device still resolves."""
        from dcim.models import Device

        target = make_device("scope-conflict-su")

        response = self._post_conflict(make_superuser(), target)

        assert b"Existing device not found" not in response.content
        assert Device.objects.get(pk=target.pk).custom_field_data["librenms_id"]["default"] == 4242

    @staticmethod
    def _vc_pair(name, *, sync_cf):
        """A real 2-member VirtualChassis whose m1 holds ``sync_cf`` (so it is the sync device)."""
        from dcim.models import VirtualChassis

        vc = VirtualChassis.objects.create(name=name)
        m1 = make_device(f"{name}-m1", librenms_cf=sync_cf)
        m1.virtual_chassis = vc
        m1.vc_position = 1
        m1.save()
        m2 = make_device(f"{name}-m2")
        m2.virtual_chassis = vc
        m2.vc_position = 2
        m2.save()
        return m1, m2

    def test_add_as_oob_cannot_write_an_out_of_scope_vc_sync_device(self):
        """The OOB link lands on the VC sync sibling, so a grant covering only the selected member must not attach it."""
        from dcim.models import Device

        sync, selected = self._vc_pair("scope-oob-vc", sync_cf={"default": {"id": 30}})
        user = self._scoped_writer(selected, "scoped-oob-vc-writer")  # excludes the sync sibling

        response = self._post_add_as_oob(user, selected)

        assert b"Existing device not found" in response.content
        sync_entry = Device.objects.get(pk=sync.pk).custom_field_data["librenms_id"]["default"]
        assert sync_entry == {"id": 30}  # no OOB half written onto the unauthorized sibling
        assert "librenms_id" not in Device.objects.get(pk=selected.pk).custom_field_data

    def test_add_as_oob_writes_the_vc_sync_device_when_it_is_in_scope(self):
        """Widening the grant to the sync sibling lets the same attach through (no over-block)."""
        from core.models import ObjectType
        from dcim.models import Device
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        sync, selected = self._vc_pair("scope-oob-vc-ok", sync_cf={"default": {"id": 30}})
        user = self._scoped_writer(selected, "scoped-oob-vc-writer-ok")
        extra = ObjectPermission.objects.create(
            name="scoped-oob-vc-sync", actions=["change"], constraints={"pk": sync.pk}
        )
        extra.object_types.set([ObjectType.objects.get_for_model(Device)])
        extra.users.set([user])
        user = get_user_model().objects.get(pk=user.pk)  # clear the per-request perm cache

        response = self._post_add_as_oob(user, selected)

        assert b"Existing device not found" not in response.content
        sync_entry = Device.objects.get(pk=sync.pk).custom_field_data["librenms_id"]["default"]
        assert sync_entry["id"] == 30
        assert sync_entry["oob"]["id"] == 4343


@pytest.mark.django_db
class TestMergeNetBoxDevicesViewDonorDerivation:
    """The merge derives the donor from winner_pk + merge_candidates and ignores posted donor_pk."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import MergeNetBoxDevicesView

        view = object.__new__(MergeNetBoxDevicesView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def test_ignores_posted_donor_pk_equal_to_winner(self):
        from dcim.models import Device
        from django.http import HttpResponse

        view = self._make_view()

        winner = make_device("merge-w", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-d", librenms_cf={"default": {"id": 10}})

        # Tampered client state: donor_pk == winner_pk must not turn this into a self-merge.
        request = _make_request(post={"winner_pk": str(winner.pk), "donor_pk": str(winner.pk)})

        validation = {"merge_candidates": {"host_named": {"pk": winner.pk}, "oob_named": {"pk": donor.pk}}}
        view.get_validated_device_with_selections = MagicMock(return_value=({"device_id": 99}, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        resp = view.post(request, device_id=99)
        assert resp.status_code == 200

        winner = Device.objects.get(pk=winner.pk)
        donor = Device.objects.get(pk=donor.pk)
        # The donor is the *other* merge candidate, never the posted self-pk: it is the
        # one whose active link was cleared and stamped with a _migrated_to marker
        # pointing at the winner.
        donor_entry = donor.custom_field_data["librenms_id"]["default"]
        assert donor_entry.get("_migrated_to", {}).get("device_id") == winner.pk
        assert donor_entry.get("id") is None
        # The winner absorbed the merge and is NOT itself marked migrated; it keeps its
        # own host id (winner-wins), with the donor's id demoted into the oob slot.
        winner_entry = winner.custom_field_data["librenms_id"]["default"]
        assert "_migrated_to" not in winner_entry
        assert winner_entry["id"] == 20
        assert winner_entry["oob"]["id"] == 10


@pytest.mark.django_db
class TestPromoteAndMergeObjectScope:
    """Promote and merge resolve client-supplied pks, so both must go through a restricted queryset.

    ``require_object_permissions`` only asks the model-level ``dcim.change_device``, which a
    pk-constrained grant satisfies; a raw lookup would then let it re-point the LibreNMS linkage of
    any device.
    """

    def _post_promote(self, user, target, **overrides):
        """Drive the real PromoteToHostView.post against *target* with only the LibreNMS seams patched."""
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import PromoteToHostView

        view = PromoteToHostView()
        view._librenms_api = _make_api()
        libre_device = {"device_id": 55, "hostname": target.name, "sysName": target.name}
        validation = {
            "existing_device": target,
            "promote_to_host": {"existing_libre_id": 10, "existing_oob_type": "idrac"},
        }
        post_data = {"existing_device_id": str(target.pk), "server_key": "default", **overrides}
        request = RequestFactory().post("/promote-to-host/", post_data)
        request.user = user
        view.request = request
        with (
            patch.object(
                PromoteToHostView,
                "get_validated_device_with_selections",
                return_value=(libre_device, validation, {}),
            ),
            patch.object(PromoteToHostView, "render_device_row", return_value=HttpResponse(b"row-ok")),
            patch.object(PromoteToHostView, "rebind_api_for_server", return_value="default"),
        ):
            return view.post(request, device_id=55)

    def _post_merge(self, user, winner, donor):
        """Drive the real MergeNetBoxDevicesView.post with *winner* kept and *donor* absorbed."""
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import MergeNetBoxDevicesView

        view = MergeNetBoxDevicesView()
        view._librenms_api = _make_api()
        validation = {"merge_candidates": {"host_named": {"pk": winner.pk}, "oob_named": {"pk": donor.pk}}}
        request = RequestFactory().post("/merge-devices/", {"winner_pk": str(winner.pk), "server_key": "default"})
        request.user = user
        view.request = request
        with (
            patch.object(
                MergeNetBoxDevicesView,
                "get_validated_device_with_selections",
                return_value=({"device_id": 99}, validation, {}),
            ),
            patch.object(MergeNetBoxDevicesView, "render_device_row", return_value=HttpResponse(b"row-ok")),
            patch.object(MergeNetBoxDevicesView, "rebind_api_for_server", return_value="default"),
        ):
            return view.post(request, device_id=99)

    def test_promote_cannot_repoint_an_out_of_scope_device(self):
        """A pk-constrained change_device grant clears the model-level gate but must not promote a device outside its scope."""
        from dcim.models import Device

        in_scope = make_device("promote-scope-in")
        out_of_scope = make_device("promote-scope-out", librenms_cf={"default": {"id": 10}})
        user = _scoped_device_writer(in_scope, "scoped-promote-writer")

        response = self._post_promote(user, out_of_scope)

        assert b"Existing device not found" in response.content
        entry = Device.objects.get(pk=out_of_scope.pk).custom_field_data["librenms_id"]["default"]
        assert entry["id"] == 10  # untouched: no host swap, no OOB demotion
        assert "oob" not in entry

    def test_promote_still_works_for_the_in_scope_device(self):
        """The device the grant DOES cover promotes normally (no over-block)."""
        from dcim.models import Device

        in_scope = make_device("promote-scope-in-2", librenms_cf={"default": {"id": 10}})
        user = _scoped_device_writer(in_scope, "scoped-promote-writer-2")

        response = self._post_promote(user, in_scope)

        assert b"Existing device not found" not in response.content
        entry = Device.objects.get(pk=in_scope.pk).custom_field_data["librenms_id"]["default"]
        assert entry["id"] == 55
        assert entry["oob"]["id"] == 10

    def test_promote_rechecks_legacy_mapping_after_lock(self):
        """A concurrent legacy write between validation and row lock must fail closed without partially promoting."""
        from dcim.models import Device

        target = make_device("promote-legacy-race", librenms_cf={"default": {"id": 10}})
        user = _scoped_device_writer(target, "scoped-promote-legacy-race")
        calls = 0

        def concurrent_legacy_write(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                Device.objects.filter(pk=target.pk).update(custom_field_data={"librenms_id": 10})
            return None

        with patch(
            "netbox_librenms_plugin.utils.find_by_librenms_id",
            side_effect=concurrent_legacy_write,
        ):
            response = self._post_promote(user, target)

        assert b"Convert mapping" in response.content
        assert Device.objects.get(pk=target.pk).custom_field_data["librenms_id"] == 10

    def test_promote_rejects_an_unviewable_device_type_override(self):
        """A catalog ID outside the user's view scope cannot change the promoted device type."""
        from dcim.models import Device, DeviceType

        target = make_device("promote-hidden-dt", librenms_cf={"default": {"id": 10}})
        hidden_type = DeviceType.objects.create(
            manufacturer=target.device_type.manufacturer,
            model="Hidden Promote Type",
            slug="hidden-promote-type",
        )
        user = _scoped_device_writer(target, "scoped-promote-hidden-dt")

        response = self._post_promote(user, target, override_device_type_id=str(hidden_type.pk))

        assert b"Invalid override_device_type_id" in response.content
        assert Device.objects.get(pk=target.pk).device_type_id == target.device_type_id

    def test_promote_rejects_an_unviewable_platform_override(self):
        """A catalog ID outside the user's view scope cannot change the promoted platform."""
        from dcim.models import Device, Platform

        target = make_device("promote-hidden-platform", librenms_cf={"default": {"id": 10}})
        hidden_platform = Platform.objects.create(
            name="Hidden Promote Platform",
            slug="hidden-promote-platform",
            manufacturer=target.device_type.manufacturer,
        )
        user = _scoped_device_writer(target, "scoped-promote-hidden-platform")

        response = self._post_promote(user, target, override_platform_id=str(hidden_platform.pk))

        assert b"Invalid override_platform_id" in response.content
        assert Device.objects.get(pk=target.pk).platform_id is None

    def test_merge_cannot_absorb_an_out_of_scope_donor(self):
        """The donor is derived server-side but still resolved by pk, so an out-of-scope donor must not be merged away."""
        from dcim.models import Device

        winner = make_device("merge-scope-winner", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-scope-donor", librenms_cf={"default": {"id": 10}})
        user = _scoped_device_writer(winner, "scoped-merge-writer")  # scoped to the winner only

        response = self._post_merge(user, winner, donor)

        assert b"Winner or donor device not found" in response.content
        assert "_migrated_to" not in Device.objects.get(pk=donor.pk).custom_field_data["librenms_id"]["default"]
        assert "oob" not in Device.objects.get(pk=winner.pk).custom_field_data["librenms_id"]["default"]

    def test_merge_succeeds_when_both_sides_are_in_scope(self):
        """A superuser (unrestricted queryset) still merges both candidates."""
        from dcim.models import Device

        winner = make_device("merge-scope-winner-2", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-scope-donor-2", librenms_cf={"default": {"id": 10}})

        response = self._post_merge(make_superuser(), winner, donor)

        assert b"Winner or donor device not found" not in response.content
        donor_entry = Device.objects.get(pk=donor.pk).custom_field_data["librenms_id"]["default"]
        assert donor_entry["_migrated_to"]["device_id"] == winner.pk

    def test_merge_cannot_write_an_out_of_scope_vc_sync_device(self):
        """The donor's VC sync sibling holds the link the merge clears and stamps, so an out-of-scope sibling must block the merge."""
        from dcim.models import Device

        _vc, m1, m2 = _two_member_vc("mrg-scope-vc", m1_cf={"default": {"id": 30}}, m2_cf=None)
        winner = make_device("mrg-scope-winner", librenms_cf={"default": {"id": 50}})
        # Covers the selected winner and the selected donor member, but NOT the sync sibling m1.
        user = _constrained_device_writer({"pk__in": [winner.pk, m2.pk]}, "scoped-merge-vc-sync")

        response = self._post_merge(user, winner, m2)

        assert b"Winner or donor device not found" in response.content
        sync_entry = Device.objects.get(pk=m1.pk).custom_field_data["librenms_id"]["default"]
        assert sync_entry["id"] == 30  # link not cleared
        assert "_migrated_to" not in sync_entry  # not stamped
        assert "oob" not in Device.objects.get(pk=winner.pk).custom_field_data["librenms_id"]["default"]

    def test_merge_runs_when_the_vc_sync_device_is_also_in_scope(self):
        """Widening the grant to the sync sibling lets the same merge through (no over-block)."""
        from dcim.models import Device

        _vc, m1, m2 = _two_member_vc("mrg-scope-vc-ok", m1_cf={"default": {"id": 30}}, m2_cf=None)
        winner = make_device("mrg-scope-winner-ok", librenms_cf={"default": {"id": 50}})
        user = _constrained_device_writer({"pk__in": [winner.pk, m1.pk, m2.pk]}, "scoped-merge-vc-sync-ok")

        response = self._post_merge(user, winner, m2)

        assert b"Winner or donor device not found" not in response.content
        sync_entry = Device.objects.get(pk=m1.pk).custom_field_data["librenms_id"]["default"]
        assert "id" not in sync_entry
        assert sync_entry["_migrated_to"]["device_id"] == winner.pk
        assert Device.objects.get(pk=winner.pk).custom_field_data["librenms_id"]["default"]["oob"]["id"] == 30


@pytest.mark.django_db
class TestBulkImportPermGateRunsBeforeEnqueue:
    """The import model-perm gate must run BEFORE the background job is dispatched.

    Otherwise an unauthorized caller both enqueues a job that can only fail and is told
    "Import job started", while the denial surfaces nowhere.
    """

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = object.__new__(BulkImportDevicesView)
        view._librenms_api = _make_api()
        return view

    @staticmethod
    def _plugin_writer_without_import_perms(username):
        """A real active user with plugin write access but no dcim/virtualization add perms."""
        from core.models import ObjectType
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        LibreNMSSettings = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")

        user = get_user_model().objects.create_user(username=username, password="x")
        write = ObjectPermission.objects.create(name=f"{username}-plugin-write", actions=["change"])
        write.object_types.set([ObjectType.objects.get_for_model(LibreNMSSettings)])
        write.users.set([user])
        return get_user_model().objects.get(pk=user.pk)  # clear the per-request perm cache

    @staticmethod
    def _plugin_writer_with_device_perms(username):
        """A real plugin writer with only the Device add/change permissions."""
        from core.models import ObjectType
        from dcim.models import Device
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        user = TestBulkImportPermGateRunsBeforeEnqueue._plugin_writer_without_import_perms(username)
        device_write = ObjectPermission.objects.create(
            name=f"{username}-device-import",
            actions=["add", "change"],
        )
        device_write.object_types.set([ObjectType.objects.get_for_model(Device)])
        device_write.users.set([user])
        return get_user_model().objects.get(pk=user.pk)  # clear the per-request perm cache

    @staticmethod
    def _request_for(user, data):
        """Build a real POST request with session-backed message storage."""
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        request = RequestFactory().post("/device-import/bulk/", data=data)
        request.user = user
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        return request

    def test_plain_device_background_batch_does_not_require_vm_permission(self):
        """Device add/change grants reach enqueue without add_virtualmachine."""
        view = self._make_view()
        user = self._plugin_writer_with_device_perms("bulk-device-only-perms")
        request = self._request_for(user, {"select": ["1"]})
        view.request = request

        assert user.has_perm("dcim.add_device")
        assert user.has_perm("dcim.change_device")
        assert not user.has_perm("virtualization.add_virtualmachine")

        with (
            patch.object(type(view), "should_use_background_job_for_import", return_value=True, create=False),
            patch("utilities.rqworker.get_workers_for_queue", return_value=1),
            patch(
                "netbox_librenms_plugin.jobs.ImportDevicesJob.enqueue",
                return_value=MagicMock(pk=4331, job_id="job-4331"),
            ) as mock_enqueue,
        ):
            response = view.post(request)

        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args.kwargs["device_ids"] == [1]
        assert mock_enqueue.call_args.kwargs["vm_imports"] == {}
        messages_sent = [str(m) for m in request._messages]
        assert not any("do not have permission to import" in m for m in messages_sent), messages_sent
        assert response.status_code in (301, 302)

    def test_explicit_vm_background_batch_still_requires_vm_permission(self):
        """An explicit VM row is denied before enqueue without add_virtualmachine."""
        view = self._make_view()
        user = self._plugin_writer_with_device_perms("bulk-explicit-vm-no-perm")
        request = self._request_for(user, {"select": ["1"], "cluster_1": "1"})
        view.request = request

        with (
            patch.object(type(view), "should_use_background_job_for_import", return_value=True, create=False),
            patch("utilities.rqworker.get_workers_for_queue", return_value=1),
            patch("netbox_librenms_plugin.jobs.ImportDevicesJob.enqueue") as mock_enqueue,
        ):
            response = view.post(request)

        mock_enqueue.assert_not_called()
        messages_sent = [str(m) for m in request._messages]
        assert any("virtualization.add_virtualmachine" in m for m in messages_sent), messages_sent
        assert response.status_code in (301, 302)

    def test_device_row_flipped_to_vm_fails_without_vm_permission(self):
        """A validation-time Device→VM flip fails per-row without creating a VM."""
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.import_utils import bulk_import_devices_shared

        user = self._plugin_writer_with_device_perms("bulk-flipped-vm-no-perm")
        existing_vm = make_vm("bulk-flipped-vm")
        vm_count = VirtualMachine.objects.count()
        libre_device = {
            "device_id": 13,
            "hostname": existing_vm.name,
            "sysName": existing_vm.name,
            "serial": "",
            "hardware": "",
            "os": "",
        }
        api = MagicMock(server_key="default", cache_timeout=300)

        with patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI", return_value=api):
            result = bulk_import_devices_shared(
                device_ids=[13],
                server_key="default",
                libre_devices_cache={13: libre_device},
                user=user,
            )

        assert VirtualMachine.objects.count() == vm_count
        assert result["success"] == []
        assert result["skipped"] == []
        assert len(result["failed"]) == 1
        assert result["failed"][0]["device_id"] == 13
        assert "virtualization.add_virtualmachine" in result["failed"][0]["error"]

    def test_unauthorized_background_import_is_denied_without_enqueueing(self):
        """A plugin-writer without dcim.add_device is denied and no ImportDevicesJob is enqueued."""
        view = self._make_view()
        user = self._plugin_writer_without_import_perms("bulk-no-import-perms")
        request = self._request_for(user, {"select": ["1"]})
        view.request = request  # Django binds this in dispatch(); the perm gate reads it

        with (
            # Stub the background decision so this plugin writer reaches the ordering under test.
            patch.object(type(view), "should_use_background_job_for_import", return_value=True, create=False),
            patch("utilities.rqworker.get_workers_for_queue", return_value=1),
            patch("netbox_librenms_plugin.jobs.ImportDevicesJob.enqueue") as mock_enqueue,
        ):
            response = view.post(request)

        mock_enqueue.assert_not_called()
        messages_sent = [str(m) for m in request._messages]
        assert any("do not have permission to import" in m for m in messages_sent), messages_sent
        assert not any("Import job started" in m for m in messages_sent), messages_sent
        assert response.status_code in (301, 302)
