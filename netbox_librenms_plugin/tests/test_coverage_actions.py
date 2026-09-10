"""Coverage tests for views/imports/actions.py missing lines."""

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
    get as get_view,
    grant as grant_view_permission,
    make_request as make_view_request,
    make_user_with_perms as make_view_user,
    message_texts as view_message_texts,
    post as post_view,
)


@pytest.fixture(autouse=True)
def _configured_import_servers(settings):
    """Give action tests explicit local server keys unless a test replaces them."""
    configure_test_servers(
        settings,
        {
            key: {
                "librenms_url": f"http://127.0.0.1/{key}",
                "api_token": "test-token",
                "verify_ssl": False,
            }
            for key in ("default", "secondary", "prod")
        },
    )


def _make_request(post=None, get=None, headers=None, user_is_superuser=False):
    """Build a real Django request with QueryDict-backed POST and GET data."""
    from django.contrib.auth.models import AnonymousUser

    request = RequestFactory().post(
        "/actions/",
        {"server_key": "default", **(post or {})},
        headers=headers or {},
    )
    request.GET = (
        RequestFactory()
        .get(
            "/actions/",
            {"server_key": "default", **(get or {})},
        )
        .GET
    )
    request.user = make_superuser() if user_is_superuser else AnonymousUser()
    return request


def test_make_request_getlist_returns_a_copy_like_querydict():
    request = _make_request(post={"device_ids": ["1", "2"]})

    request.POST.getlist("device_ids").append("3")

    assert request.POST.getlist("device_ids") == ["1", "2"]


def test_make_request_getlist_matches_querydict_for_scalar_values():
    request = _make_request(get={"server_key": "secondary"})

    assert request.GET.getlist("server_key") == ["secondary"]


def _make_api():
    """Create the minimal API-shaped value needed by pure helper tests."""
    return Namespace(
        server_key="default",
        cache_timeout=300,
        librenms_url="http://127.0.0.1/default",
    )


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
    def test_update_fields_device_type_rack_overflow_is_blocked(self):
        """Changing to a 4U device type at U40 in a 42U rack is rejected when saving only ``device_type``."""
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
        """The update-fields check rejects a 0U device type at a rack position even when the space check passes."""
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
        """The update-fields check rejects a child device type on a rack face without a rack position."""
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


@pytest.mark.django_db
class TestResolveNamingPreferences:
    """Test the database fallbacks not covered by the real request/preference tests."""

    @staticmethod
    def _anonymous_request():
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get("/device-import/")
        request.user = AnonymousUser()
        return request

    def test_settings_fallback_when_no_pref(self):
        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        plugin_settings, _ = LibreNMSSettings.objects.get_or_create()
        plugin_settings.use_sysname_default = False
        plugin_settings.strip_domain_default = True
        plugin_settings.save(update_fields=["use_sysname_default", "strip_domain_default"])

        assert resolve_naming_preferences(self._anonymous_request()) == (False, True)

    def test_no_settings_defaults_to_true_false(self):
        from netbox_librenms_plugin.models import LibreNMSSettings
        from netbox_librenms_plugin.utils import resolve_naming_preferences

        LibreNMSSettings.objects.all().delete()

        assert resolve_naming_preferences(self._anonymous_request()) == (True, False)


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

    def test_colliding_rows_render_the_collision_block(self, settings, librenms_server):
        """Two LibreNMS rows that resolve to one NetBox device are blocked by the collision gate."""
        server_key = "confirm-integration-collision"
        target = make_device("confirm-integration-collision-target")
        for device_id, ip in ((71, "198.18.0.71"), (72, "198.18.0.72")):
            librenms_server.device_info_response(
                device_id=device_id,
                hostname=target.name,
                hardware="Test collision hardware",
                os="test-collision-os",
                serial="",
                ip=ip,
            )
            librenms_server.vc_inventory_callable(device_id, [], {})
        view = self._make_view(settings, librenms_server, server_key)
        user = make_view_user("confirm-integration-collision-user", [])
        request = make_view_request(
            "post",
            {
                "server_key": server_key,
                "select": ["71", "72"],
                "use_sysname": "true",
                "strip_domain": "false",
            },
            user=user,
            HTTP_HX_REQUEST="true",
        )

        response = post_view(view, request)

        assert response.status_code == 200
        assert b"Bulk import blocked" in response.content
        assert target.name.encode() in response.content
        assert b"Confirm Import" not in response.content


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
        from virtualization.models import VirtualMachine

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

        before = (Device.objects.count(), VirtualMachine.objects.count())
        response = post_view(view, request)

        assert response.status_code == 302
        assert response["Location"] == get_script_prefix()
        assert view_message_texts(request, "error") == ["You do not have permission to perform this action."]
        assert (Device.objects.count(), VirtualMachine.objects.count()) == before

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


@pytest.mark.django_db
class TestDeviceImportHelperMixin:
    """Exercise validation, row rendering, and fallback messages through real seams."""

    @pytest.fixture
    def librenms_server(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as server:
            yield server

    @staticmethod
    def _view(settings, server, server_key):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.imports.actions import DeviceRoleUpdateView

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
        view = DeviceRoleUpdateView()
        view._librenms_api = LibreNMSAPI(server_key=server_key)
        return view

    def test_real_device_is_validated_and_rendered(self, settings, librenms_server):
        server_key = "helper-real-device"
        device_id = 7
        make_device("helper-infrastructure")
        librenms_server.device_info_response(
            device_id=device_id,
            hostname="helper-new-device",
            hardware="TestDT",
            os="-",
            serial="",
            ip="198.18.0.7",
            location="TestSite",
        )
        librenms_server.vc_inventory_callable(device_id, [], {})
        view = self._view(settings, librenms_server, server_key)
        request = make_view_request("post", {"server_key": server_key})

        libre_device, validation, selections = view.get_validated_device_with_selections(device_id, request)
        response = view.render_device_row(request, libre_device, validation, selections)

        assert libre_device["device_id"] == device_id
        assert validation is not None
        assert selections == {"cluster_id": None, "role_id": None, "rack_id": None}
        assert response.status_code == 200
        assert b"helper-new-device" in response.content
        assert any(item["path"] == f"/api/v0/devices/{device_id}" for item in librenms_server.requests)

    def test_post_commit_refresh_fallback_renders_real_messages(self):
        from django.contrib import messages
        from netbox_librenms_plugin.views.imports.actions import DeviceRoleUpdateView

        request = make_view_request("post")

        response = DeviceRoleUpdateView().post_commit_refresh_fallback(
            request,
            "closeModal",
            deferred_messages=[(messages.INFO, "OOB attached")],
        )

        assert response.status_code == 200
        assert response["HX-Trigger"] == "closeModal"
        assert response["HX-Reswap"] == "none"
        assert b"OOB attached" in response.content
        assert b"could not be reloaded" in response.content


@pytest.mark.django_db
class TestAttachMessagesOob:
    """Exercise the OOB message helper with Django's real message storage and template."""

    def test_returns_none_when_response_is_none(self):
        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        assert _attach_messages_oob(None, make_view_request("get")) is None

    def test_appends_the_real_messages_fragment(self):
        from django.contrib import messages
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        request = make_view_request("get")
        messages.info(request, "A queued action message")
        response = HttpResponse(b"<tr>row html</tr>")

        result = _attach_messages_oob(response, request)

        assert result.content.startswith(b"<tr>row html</tr>")
        assert b'<div id="django-messages"' in result.content
        assert b"A queued action message" in result.content

    def test_no_messages_leave_the_response_unchanged(self):
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        request = make_view_request("get")
        response = HttpResponse(b"<tr>row html</tr>")
        original = response.content

        result = _attach_messages_oob(response, request)

        assert result.content == original


@pytest.mark.django_db
class TestDeviceValidationDetailsView:
    """Exercise validation details through real server binding, validation, and rendering."""

    @pytest.fixture(autouse=True)
    def _configure_librenms_servers(self, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as primary, run_librenms_server() as secondary:
            configure_test_servers(
                settings,
                {
                    "default": {
                        "librenms_url": primary.url,
                        "api_token": "test-token",
                        "verify_ssl": False,
                    },
                    "secondary": {
                        "librenms_url": secondary.url,
                        "api_token": "test-token",
                        "verify_ssl": False,
                    },
                },
            )
            self.primary_server = primary
            self.secondary_server = secondary
            yield

    @staticmethod
    def _get(server_key, device_id=1):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        request = make_view_request(
            "get",
            {"server_key": server_key},
            user=make_superuser(),
            HTTP_HX_REQUEST="true",
        )
        view = DeviceValidationDetailsView()
        response = get_view(view, request, device_id=device_id)
        return view, response

    def test_get_device_not_found_returns_200_html_fragment(self):
        _view, response = self._get("default")

        assert response.status_code == 200
        assert b"not found in LibreNMS" in response.content
        assert [request["path"] for request in self.primary_server.requests] == ["/api/v0/devices/1"]

    def test_get_with_existing_device_renders_sync_info(self):
        existing = make_device(
            "validation-details-existing",
            serial="SN001",
            librenms_cf={"default": {"id": 1}},
        )
        self.primary_server.device_info_response(
            device_id=1,
            hostname=existing.name,
            hardware=existing.device_type.model,
            os="ios",
            serial=existing.serial,
            ip="198.18.0.1",
        )
        self.primary_server.vc_inventory_callable(1, [], {})

        _view, response = self._get("default")

        assert response.status_code == 200
        assert existing.name.encode() in response.content
        assert response.content.count(existing.serial.encode()) >= 2
        assert b"mdi-check-circle" in response.content

    def test_get_rebinds_to_request_server_key(self):
        view, response = self._get("secondary")

        assert response.status_code == 200
        assert view.librenms_api.server_key == "secondary"
        assert self.primary_server.requests == []
        assert [request["path"] for request in self.secondary_server.requests] == ["/api/v0/devices/1"]

    def test_get_unresolved_server_key_fails_closed(self):
        _view, response = self._get("ghost")

        assert response.status_code == 200
        assert b"no longer configured" in response.content
        assert self.primary_server.requests == []
        assert self.secondary_server.requests == []


@pytest.mark.django_db
class TestBuildSyncInfo:
    """Tests for _build_sync_info (lines 828-886)."""

    def _get_method(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        return DeviceValidationDetailsView._build_sync_info

    def test_serial_matches(self):
        build_sync_info = self._get_method()
        libre_device = {"serial": "SN001", "os": "-", "hardware": "-"}
        existing = make_device("sync-info-serial-match", serial="SN001")

        result = build_sync_info(libre_device, existing)

        assert result["serial_synced"] is True

    def test_serial_mismatch(self):
        build_sync_info = self._get_method()
        libre_device = {"serial": "SN_LIBRENMS", "os": "-", "hardware": "-"}
        existing = make_device("sync-info-serial-mismatch", serial="SN_NETBOX")

        result = build_sync_info(libre_device, existing)
        assert result["serial_synced"] is False

    def test_padded_incoming_serial_counts_as_synced(self):
        """A whitespace-padded LibreNMS serial equal to the stored trimmed value must not report drift."""
        build_sync_info = self._get_method()
        libre_device = {"serial": " SN001 ", "os": "-", "hardware": "-"}
        existing = make_device("sync-info-padded-incoming", serial="SN001")

        result = build_sync_info(libre_device, existing)

        assert result["serial_synced"] is True

    def test_padded_stored_serial_counts_as_synced(self):
        """A real device whose STORED serial is legacy-padded must not report serial drift in the details modal."""
        build_sync_info = self._get_method()
        existing = make_device("sync-info-padded-serial", serial=" SN-STORED-1 ")
        libre_device = {"serial": "SN-STORED-1", "os": "-", "hardware": "-"}

        result = build_sync_info(libre_device, existing)

        assert result["serial_synced"] is True, "padded stored serial reported as drift"
        assert result["all_synced"] is True

    def test_platform_synced_when_matching(self):
        from dcim.models import Platform

        build_sync_info = self._get_method()
        platform = Platform.objects.create(name="sync-info-ios", slug="sync-info-ios")
        existing = make_device("sync-info-platform")
        existing.platform = platform
        existing.save(update_fields=["platform"])
        libre_device = {"serial": "-", "os": platform.name, "hardware": "-"}

        result = build_sync_info(libre_device, existing)

        assert result["platform_synced"] is True
        assert result["platform_info"]["matching_platform"] == platform

    def test_device_type_synced_when_matched(self):
        build_sync_info = self._get_method()
        existing = make_device("sync-info-type-match")
        libre_device = {"serial": "-", "os": "-", "hardware": existing.device_type.model}

        result = build_sync_info(libre_device, existing)

        assert result["device_type_synced"] is True
        assert result["librenms_device_type"] == existing.device_type

    def test_device_type_not_synced_when_mismatch(self):
        from dcim.models import DeviceType

        build_sync_info = self._get_method()
        existing = make_device("sync-info-type-mismatch")
        librenms_type = DeviceType.objects.create(
            manufacturer=existing.device_type.manufacturer,
            model="SyncInfoOtherType",
            slug="sync-info-other-type",
        )
        libre_device = {"serial": "-", "os": "-", "hardware": librenms_type.model}

        result = build_sync_info(libre_device, existing)

        assert result["device_type_synced"] is False
        assert result["librenms_device_type"] == librenms_type

    def test_missing_platform_and_hardware_are_already_synced(self):
        build_sync_info = self._get_method()
        existing = make_device("sync-info-no-platform")

        result = build_sync_info(
            {"serial": "-", "os": "-", "hardware": "-"},
            existing,
        )

        assert result["platform_synced"] is True
        assert result["device_type_synced"] is True
        assert result["all_synced"] is True


class TestBuildIdServerInfo:
    """Tests for _build_id_server_info (lines 888-924)."""

    def _get_method(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceValidationDetailsView

        return DeviceValidationDetailsView._build_id_server_info

    @staticmethod
    def _object(mapping):
        return Namespace(custom_field_data={"librenms_id": mapping})

    @staticmethod
    def _configure(settings, plugin_config):
        settings.PLUGINS_CONFIG = {"netbox_librenms_plugin": plugin_config}

    def test_legacy_int_returns_none(self):
        method = self._get_method()

        assert method(self._object(42)) is None

    def test_none_cf_returns_none(self):
        method = self._get_method()

        assert method(Namespace(custom_field_data={})) is None

    def test_dict_cf_returns_list(self, settings):
        method = self._get_method()
        self._configure(settings, {"servers": {"default": {"display_name": "Default Server"}}})

        result = method(self._object({"default": 42}))

        assert result is not None
        assert result[0]["server_key"] == "default"
        assert result[0]["device_id"] == 42

    def test_bool_value_skipped(self, settings):
        method = self._get_method()
        self._configure(settings, {"servers": {"other": {"display_name": "Other"}}})

        result = method(self._object({"default": True, "other": 99}))

        assert result is not None
        assert len(result) == 1
        assert result[0]["server_key"] == "other"

    def test_dict_entry_uses_host_id(self, settings):
        """New dict form {server_key: {"id": N, "oob": {...}}} renders the host id, not None."""
        method = self._get_method()
        self._configure(settings, {"servers": {"default": {"display_name": "Default Server"}}})

        result = method(self._object({"default": {"id": 42, "oob": {"id": 17, "type": "idrac"}}}))

        assert result is not None
        assert result[0]["device_id"] == 42

    def test_oob_only_dict_entry_surfaced_with_controller_id(self, settings):
        """An OOB-only entry is still a real link → surfaced with the OOB controller's id."""
        method = self._get_method()
        self._configure(settings, {"servers": {"default": {"display_name": "Default Server"}}})

        result = method(self._object({"default": {"oob": {"id": 17, "type": "idrac"}}}))

        # Mirrors the device-sync modal (_build_all_server_mappings): the OOB-only link is shown
        # using the OOB controller's id rather than dropped (which would risk a duplicate re-import).
        assert result == [{"server_key": "default", "display_name": "Default Server", "device_id": 17}]

    def test_default_key_fallback_display_name(self, settings):
        """'default' with no servers config uses root display_name."""
        method = self._get_method()
        self._configure(settings, {"display_name": "My LibreNMS", "servers": {}})

        result = method(self._object({"default": 55}))

        assert result is not None
        assert result[0]["display_name"] == "My LibreNMS"

    @pytest.mark.parametrize("stored_id", ["77", " 77 "])
    def test_string_device_id_converted(self, settings, stored_id):
        method = self._get_method()
        self._configure(settings, {"servers": {"default": {"display_name": "D"}}})

        result = method(self._object({"default": stored_id}))

        assert result[0]["device_id"] == 77

    def test_non_dict_servers_config_is_treated_as_empty(self, settings):
        method = self._get_method()
        self._configure(settings, {"servers": "not-a-dict"})

        result = method(self._object({"default": 42}))

        assert result == [{"server_key": "default", "display_name": "default", "device_id": 42}]

    def test_non_numeric_id_is_skipped(self, settings):
        method = self._get_method()
        self._configure(settings, {"servers": {}})

        result = method(self._object({"default": "not-a-number", "main": 42}))

        assert result == [{"server_key": "main", "display_name": "main", "device_id": 42}]


@pytest.mark.django_db
class TestRowUpdateViews:
    """Exercise role, cluster, and rack row updates through real server rebinding."""

    VIEWS = ["DeviceRoleUpdateView", "DeviceClusterUpdateView", "DeviceRackUpdateView"]

    @pytest.fixture
    def librenms_server(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as server:
            yield server

    @staticmethod
    def _view(view_name):
        from netbox_librenms_plugin.views.imports import actions

        return getattr(actions, view_name)()

    @staticmethod
    def _configure(settings, server, server_key):
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

    @pytest.mark.parametrize("view_name", VIEWS)
    def test_stale_server_key_fails_closed_without_http(self, settings, librenms_server, view_name):
        self._configure(settings, librenms_server, "row-active")
        request = make_view_request("post", {"server_key": "ghost"})

        result = post_view(self._view(view_name), request, device_id=42)

        assert result.headers.get("HX-Reswap") == "none"
        assert b"no longer configured" in result.content
        assert librenms_server.requests == []

    @pytest.mark.parametrize("view_name", VIEWS)
    def test_rebinds_before_real_missing_device_lookup(self, settings, librenms_server, view_name):
        server_key = f"row-{view_name.lower()}"
        self._configure(settings, librenms_server, server_key)
        view = self._view(view_name)
        request = make_view_request("post", {"server_key": server_key})

        result = post_view(view, request, device_id=42)

        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Device not found" in result.content
        assert view.librenms_api.server_key == server_key
        assert [(item["method"], item["path"]) for item in librenms_server.requests] == [("GET", "/api/v0/devices/42")]


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
                "action": "update_serial",
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


@pytest.mark.django_db
class TestSaveUserPrefView:
    """Exercise the preference endpoint through URL dispatch and a real user config."""

    @staticmethod
    def _post(client, user, body):
        client.force_login(user)
        return client.post(
            url_for("plugins:netbox_librenms_plugin:save_user_pref"),
            data=body,
            content_type="application/json",
        )

    @pytest.mark.parametrize(
        "body",
        [
            b"not-json",
            b'{"key":"disallowed_key","value":true}',
            b"[1,2,3]",
            b'"hello"',
            b"42",
        ],
    )
    def test_invalid_payload_returns_400(self, client, body):
        user = make_view_user("pref-invalid-user", [])

        response = self._post(client, user, body)

        assert response.status_code == 400

    def test_valid_pref_is_persisted(self, client, django_user_model):
        user = make_view_user("pref-valid-user", [])

        response = self._post(client, user, b'{"key":"use_sysname","value":true}')

        assert response.status_code == 200
        stored_user = django_user_model.objects.get(pk=user.pk)
        assert stored_user.config.get("plugins.netbox_librenms_plugin.use_sysname") is True


@pytest.mark.django_db
class TestDeviceVCDetailsView:
    """Render VC details through the real client, inventory detector, and template."""

    @pytest.fixture
    def librenms_server(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as server:
            yield server

    @staticmethod
    def _view(settings, server, server_key):
        from netbox_librenms_plugin.views.imports.actions import DeviceVCDetailsView

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
        return DeviceVCDetailsView()

    def test_device_not_found_returns_200_html_fragment(self, settings, librenms_server):
        server_key = "vc-details-missing"
        view = self._view(settings, librenms_server, server_key)
        request = make_view_request("get", {"server_key": server_key})

        result = get_view(view, request, device_id=1)

        assert result.status_code == 200
        assert b"not found in LibreNMS" in result.content
        assert [(item["method"], item["path"]) for item in librenms_server.requests] == [("GET", "/api/v0/devices/1")]

    def test_stack_response_renders_real_vc_details(self, settings, librenms_server):
        server_key = "vc-details-stack"
        device_id = 42
        view = self._view(settings, librenms_server, server_key)
        librenms_server.device_info_response(device_id=device_id, hostname="stack-master", serial="MEMBER-1")
        librenms_server.vc_inventory_callable(
            device_id,
            [{"entPhysicalClass": "stack", "entPhysicalIndex": 100}],
            {
                100: [
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 201,
                        "entPhysicalParentRelPos": 2,
                        "entPhysicalSerialNum": "MEMBER-2",
                        "entPhysicalModelName": "Model 2",
                    },
                    {
                        "entPhysicalClass": "chassis",
                        "entPhysicalIndex": 200,
                        "entPhysicalParentRelPos": 1,
                        "entPhysicalSerialNum": "MEMBER-1",
                        "entPhysicalModelName": "Model 1",
                    },
                ]
            },
        )
        request = make_view_request("get", {"server_key": server_key})

        result = get_view(view, request, device_id=device_id)

        assert result.status_code == 200
        assert b"Virtual Chassis: stack-master" in result.content
        assert b"2-member" in result.content
        assert b"stack-master-M1" in result.content
        assert b"stack-master-M2" in result.content


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
        assert (
            result["Location"] == f"{url_for('plugins:netbox_librenms_plugin:librenms_import')}?server_key={server_key}"
        )
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
        assert (
            response["Location"]
            == f"{url_for('plugins:netbox_librenms_plugin:librenms_import')}?server_key={server_key}"
        )
        assert view_message_texts(request, "error") == ["Invalid device identifier supplied"]


class TestDeviceConflictActionViewVMGuard:
    """Tests for the DeviceConflictActionView VM action guard."""

    @pytest.mark.django_db
    def test_device_only_action_for_vm_renders_htmx_error_toast(self, client, settings):
        """A VM cannot run a Device-only serial action."""
        from django.urls import reverse
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.tests.import_server_helpers import configure_servers
        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        configure_servers(settings)
        vm = make_vm("vm-device-only-action")
        user = make_user_with_perms(
            "vm-device-only-action-user",
            [("change", VirtualMachine)],
        )
        client.force_login(user)
        url = reverse(
            "plugins:netbox_librenms_plugin:device_conflict_action",
            kwargs={"device_id": 1},
        )

        response = client.post(
            url,
            {
                "action": "update_serial",
                "existing_device_id": vm.pk,
                "existing_device_type": "virtualmachine",
                "server_key": "secondary",
            },
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"is not supported for virtual machines" in response.content


@pytest.mark.django_db
class TestApplyUserSelectionsToValidation:
    """Apply selected real NetBox rows and recalculate the validation state."""

    def test_vm_with_cluster_and_role(self):
        from netbox_librenms_plugin.views.imports.actions import _apply_user_selections_to_validation

        cluster = make_cluster("selection-cluster")
        role = make_device("selection-vm-role-source").role
        validation = {
            "cluster": {"found": False, "cluster": None},
            "device_role": {"found": False, "role": None},
            "issues": ["Cluster must be selected", "Device role must be selected"],
        }
        selections = {"cluster_id": str(cluster.pk), "role_id": str(role.pk), "rack_id": None}

        _apply_user_selections_to_validation(validation, selections, is_vm=True)

        assert validation["cluster"] == {"found": True, "cluster": cluster}
        assert validation["device_role"] == {"found": True, "role": role}
        assert validation["issues"] == []
        assert validation["can_import"] is True
        assert validation["is_ready"] is True

    def test_device_with_role_and_rack(self):
        from dcim.models import Rack

        from netbox_librenms_plugin.views.imports.actions import _apply_user_selections_to_validation

        source = make_device("selection-device-role-source")
        rack = Rack.objects.create(name="Selection Rack", site=source.site, status="active")
        validation = {
            "site": {"found": True},
            "device_type": {"found": True},
            "device_role": {"found": False, "role": None},
            "issues": ["Device role must be selected"],
        }
        selections = {"cluster_id": None, "role_id": str(source.role.pk), "rack_id": str(rack.pk)}

        _apply_user_selections_to_validation(validation, selections, is_vm=False)

        assert validation["device_role"] == {"found": True, "role": source.role}
        assert validation["rack"] == {"found": True, "rack": rack}
        assert validation["issues"] == []
        assert validation["can_import"] is True
        assert validation["is_ready"] is True


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

        view.get_validated_device_with_selections = validate_then_mutate
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

    def test_vm_migration_rejects_a_device_claim_created_after_validation(self):
        """Reject a Device claim that appears after validation and keep the VM mapping unchanged."""
        from dcim.models import Device
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from virtualization.models import VirtualMachine

        view = self._make_view()
        device = make_device(
            "migrate-cross-model-owner",
            librenms_cf={self.server_key: 99},
        )
        vm = make_vm("migrate-cross-model-vm")
        vm.custom_field_data["librenms_id"] = 42
        vm.save(update_fields=["custom_field_data"])
        self.librenms_server.device_info_response(
            device_id=42,
            hostname="migrate-cross-model-vm",
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
            user=make_view_user("migrate-cross-model-user", [("change", VirtualMachine), ("view", Device)]),
            HTTP_HX_REQUEST="true",
        )

        with CaptureQueriesContext(connection) as queries:
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
        assert b"already assigned to device" in response.content
        assert any("pg_advisory_xact_lock" in query["sql"] for query in queries.captured_queries)
        assert VirtualMachine.objects.get(pk=vm.pk).custom_field_data["librenms_id"] == 42
        assert Device.objects.get(pk=device.pk).custom_field_data["librenms_id"] == {self.server_key: 42}

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

    def _request(self, action, device, username, perms=None, **extra_post):
        from dcim.models import Device

        return make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "action": action,
                "existing_device_id": str(device.pk),
                **extra_post,
            },
            user=make_view_user(username, perms if perms is not None else [("change", Device)]),
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

        view.get_validated_device_with_selections = validate_around_mutation
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
        # The VM is in the caller's view scope, so the disclosure gate keeps it bound and the
        # cross-model pk check below is the thing that refuses.
        request = self._request(
            "sync_name",
            target,
            "branches-type-mismatch-user",
            perms=[("change", Device), ("view", VirtualMachine)],
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
            "Object has a legacy bare-integer librenms_id; use 'Convert mapping' "
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

    def test_sync_serial_reports_a_real_save_error(self):
        from dcim.models import Device

        view = self._make_view()
        target = make_device(
            "branches-sync-serial-save-error-target",
            serial="BRANCHES-SYNC-SERIAL-KEEP",
        )
        serial_limit = Device._meta.get_field("serial").max_length
        self._register_device(42, target.name, serial="S" * (serial_limit + 1))
        request = self._request(
            "sync_serial",
            target,
            "branches-sync-serial-save-error-user",
        )

        response = post_view(view, request, device_id=42)

        self._assert_htmx_error(response, "Could not save: a field value is invalid")
        assert Device.objects.get(pk=target.pk).serial == "BRANCHES-SYNC-SERIAL-KEEP"

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
        assert (
            response["Location"]
            == f"{url_for('plugins:netbox_librenms_plugin:librenms_import')}?server_key={server_key}"
        )

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
        assert (
            response["Location"]
            == f"{url_for('plugins:netbox_librenms_plugin:librenms_import')}?server_key=bulk-more-invalid-device-mapping"
        )

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
        assert (
            response["Location"]
            == f"{url_for('plugins:netbox_librenms_plugin:librenms_import')}?server_key=bulk-more-valid-mapping"
        )


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
        assert (
            response["Location"]
            == f"{url_for('plugins:netbox_librenms_plugin:librenms_import')}?server_key={server_key}"
        )


@pytest.mark.django_db
class TestAddDeviceTypeMappingIntegration:
    """Exercise device-type mapping writes through real cache, HTTP, ORM, and render paths."""

    @pytest.fixture(autouse=True)
    def _configure_librenms_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"device-type-mapping-{request.node.name}".replace("_", "-")
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

    def _make_device_type(self):
        from dcim.models import DeviceType, Manufacturer

        mfr = Manufacturer.objects.create(name="Cisco-66", slug="cisco-66")
        return DeviceType.objects.create(manufacturer=mfr, model="C9300-66", slug="c9300-66")

    def _seed_device(self, device_id, hardware, *, timeout=300):
        from django.core.cache import cache

        from netbox_librenms_plugin.import_utils.cache import get_import_device_cache_key

        hostname = f"mapping-device-{device_id}"
        make_device(hostname, librenms_cf={self.server_key: {"id": device_id}})
        libre_device = {
            "device_id": device_id,
            "hardware": hardware,
            "sysName": hostname,
            "hostname": hostname,
            "os": "ios",
            "serial": "",
            "ip": "198.18.0.66",
        }
        cache_key = get_import_device_cache_key(device_id, self.server_key)
        cache.set(cache_key, libre_device, timeout=timeout)
        self.librenms_server.device_info_response(
            device_id=device_id,
            hostname=hostname,
            hardware=hardware,
            os="ios",
            serial="",
            ip="198.18.0.66",
        )
        self.librenms_server.vc_inventory_callable(device_id, [], {})
        return cache_key

    def _prewarm_vc_cache(self, device_id):
        from netbox_librenms_plugin.import_utils.virtual_chassis import get_virtual_chassis_data
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        get_virtual_chassis_data(LibreNMSAPI(server_key=self.server_key), device_id)
        self.librenms_server.requests.clear()

    def _post(self, device_id, device_type):
        from netbox_librenms_plugin.views.imports.actions import AddDeviceTypeMappingView

        request = make_view_request(
            "post",
            {"device_type_id": str(device_type.pk), "server_key": self.server_key},
            user=make_superuser(),
            HTTP_HX_REQUEST="true",
        )
        return post_view(AddDeviceTypeMappingView(), request, device_id=device_id)

    def test_post_reuses_cached_device_no_second_librenms_call(self):
        from django.core.cache import cache

        from netbox_librenms_plugin.models import DeviceTypeMapping

        device_id = 4242
        device_type = self._make_device_type()
        cache_key = self._seed_device(device_id, "WS-C9300-66")
        self._prewarm_vc_cache(device_id)

        response = self._post(device_id, device_type)

        assert DeviceTypeMapping.objects.filter(librenms_hardware="ws-c9300-66").exists()
        assert self.librenms_server.requests == []
        assert b"no longer configured" not in response.content
        assert cache.get(cache_key) is not None
        assert response.status_code == 200

    def test_cache_repopulation_preserves_remaining_ttl_on_ttl_backends(self):
        """The Redis-backed snapshot must keep its remaining TTL after repopulation."""
        from django.core.cache import cache

        device_id = 4444
        device_type = self._make_device_type()
        cache_key = self._seed_device(device_id, "WS-C9300-TTL", timeout=120)
        ttl_before = cache.ttl(cache_key)

        response = self._post(device_id, device_type)

        ttl_after = cache.ttl(cache_key)
        assert response.status_code == 200
        assert 0 < ttl_after <= ttl_before <= 120

    def test_mapping_persisted_under_normalised_hardware_key(self):
        """The mapping must use the normalised hardware string."""
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        device_id = 4343
        device_type = self._make_device_type()

        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(.+)$", replacement=r"\1", priority=10
        )
        self._seed_device(device_id, "WS-C9300-66")

        self._post(device_id, device_type)

        assert DeviceTypeMapping.objects.filter(librenms_hardware="c9300-66").exists()
        assert not DeviceTypeMapping.objects.filter(librenms_hardware="ws-c9300-66").exists()

    def test_normalised_hardware_is_trimmed_before_lookup(self):
        """The normalised key must be trimmed."""
        from dcim.models import DeviceType, Manufacturer
        from netbox_librenms_plugin.models import DeviceTypeMapping, NormalizationRule

        device_id = 4444
        mfr = Manufacturer.objects.create(name="Cisco-trim", slug="cisco-trim")
        dt_old = DeviceType.objects.create(manufacturer=mfr, model="C9300-old", slug="c9300-old")
        dt_new = DeviceType.objects.create(manufacturer=mfr, model="C9300-new", slug="c9300-new")

        existing = DeviceTypeMapping.objects.create(librenms_hardware="c9300-44", netbox_device_type=dt_old)
        NormalizationRule.objects.create(
            scope="device_type", match_pattern=r"^WS-(.+)$", replacement=r" \1 ", priority=10
        )
        self._seed_device(device_id, "WS-C9300-44")

        self._post(device_id, dt_new)

        assert DeviceTypeMapping.objects.filter(librenms_hardware="c9300-44").count() == 1
        existing.refresh_from_db()
        assert existing.netbox_device_type_id == dt_new.pk


@pytest.mark.django_db
class TestCreatePlatformAssignmentIndependence:
    """Exercise platform creation and assignment through real HTTP, ORM, and permission seams."""

    @pytest.fixture(autouse=True)
    def _configure_librenms_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"platform-{request.node.name}".replace("_", "-")
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

    def _mapped_device(self, name, device_id=42):
        device = make_device(name, librenms_cf={self.server_key: {"id": device_id}})
        self.librenms_server.device_info_response(
            device_id=device_id,
            hostname=name,
            hardware=device.device_type.model,
            os="test-os",
            serial=device.serial,
            ip="198.18.0.42",
        )
        self.librenms_server.vc_inventory_callable(device_id, [], {})
        return device

    def _post(self, device, platform_name, manufacturer_id):
        from netbox_librenms_plugin.views.imports.actions import CreatePlatformFromImportView

        request = make_view_request(
            "post",
            {
                "server_key": self.server_key,
                "platform_name": platform_name,
                "manufacturer": str(manufacturer_id),
                "device_pk": str(device.pk),
            },
            user=make_superuser(),
            HTTP_HX_REQUEST="true",
        )
        return post_view(CreatePlatformFromImportView(), request, device_id=42)

    def test_platform_persists_and_error_is_surfaced_when_legacy_target_is_invalid(self):
        """A failed optional assignment keeps the platform and returns an error instead of a success swap."""
        from dcim.models import Device, Platform

        target = self._mapped_device("platform-legacy-target")
        manufacturer = target.device_type.manufacturer
        Device.objects.filter(pk=target.pk).update(rack=None, position=1, face="front")

        response = self._post(target, "Legacy Target OS", manufacturer.pk)

        assert Platform.objects.filter(name="Legacy Target OS").exists()
        target.refresh_from_db()
        assert target.platform is None
        assert b"could not be assigned" in response.content
        assert b"htmx-modal-content" not in response.content

    def test_invalid_manufacturer_id_is_rejected(self):
        from dcim.models import Manufacturer, Platform

        target = self._mapped_device("platform-missing-manufacturer-target")
        missing_pk = (Manufacturer.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 1000

        response = self._post(target, "Missing Manufacturer OS", missing_pk)

        assert b"Selected manufacturer not found" in response.content
        assert not Platform.objects.filter(name="Missing Manufacturer OS").exists()

    def test_device_platform_manufacturer_mismatch_is_surfaced_and_platform_is_kept(self):
        from dcim.models import Manufacturer, Platform

        target = self._mapped_device("platform-manufacturer-mismatch-target")
        other_manufacturer = Manufacturer.objects.create(name="Other Platform Manufacturer", slug="other-platform-mfr")

        response = self._post(target, "Mismatch OS", other_manufacturer.pk)

        assert Platform.objects.filter(name="Mismatch OS", manufacturer=other_manufacturer).exists()
        target.refresh_from_db()
        assert target.platform is None
        assert b"could not be assigned" in response.content

    def test_device_platform_manufacturer_match_assigns_and_renders_updates(self):
        from dcim.models import Platform

        target = self._mapped_device("platform-manufacturer-match-target")

        response = self._post(target, "Matching OS", target.device_type.manufacturer_id)

        platform = Platform.objects.get(name="Matching OS")
        target.refresh_from_db()
        assert target.platform_id == platform.pk
        assert response.status_code == 200
        assert b' id="htmx-modal-content"' in response.content
        assert b"hx-swap-oob" in response.content


@pytest.mark.django_db
class TestBulkImportRunsInlineWithoutWorkers:
    """A background import with no RQ worker runs inline and says so as information.

    The wording matters: this notice sits beside the per-row success toast, so a warning-level
    "no workers available" reads as a failed import when the rows actually imported.
    """

    @staticmethod
    def _view(settings, server_key, server_url):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        configure_test_servers(
            settings,
            {server_key: {"librenms_url": server_url, "api_token": "test-token", "verify_ssl": False}},
        )
        return BulkImportDevicesView()

    def test_the_fallback_is_reported_as_information(self, settings, monkeypatch):
        """Only the RQ worker registry is faked; the view, the import and the DB are real."""
        from unittest.mock import patch

        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        server_key = "bulk-no-workers"
        existing = make_device("fallback-existing")
        user = make_superuser("bulk-no-workers-user")

        with run_librenms_server() as server:
            server.device_info_response(
                device_id=1,
                hostname=existing.name,
                serial="fallback-serial",
                ip="198.18.4.1",
            )
            view = self._view(settings, server_key, server.url)
            request = make_view_request(
                "post",
                {"server_key": server_key, "select": ["1"], "use_background_job": "on"},
                user=user,
            )
            with patch("utilities.rqworker.get_workers_for_queue", return_value=0):
                response = post_view(view, request)

        assert response.status_code == 302
        infos = view_message_texts(request, "info")
        # Precondition: the request really took the synchronous path, not the job path.
        assert not any("Import job started" in message for message in infos)
        assert any("directly instead of in the background" in message for message in infos)
        # The notice must not arrive as a warning, and must not use the old failure-sounding wording.
        every_message = infos + view_message_texts(request, "warning") + view_message_texts(request, "error")
        assert not any("no workers" in message.lower() for message in every_message)


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
            response = post_view(view, request)

        assert view_message_texts(request, "warning") == ["Skipped 2 existing devices"]
        assert response.status_code == 302
        assert (
            response["Location"]
            == f"{url_for('plugins:netbox_librenms_plugin:librenms_import')}?server_key={server_key}"
        )

    def test_colliding_batch_non_htmx_message_is_object_neutral(self, settings, monkeypatch):
        """The non-HTMX collision message uses ``NetBox object`` for batches that can contain VMs."""
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
            before = type(colliding_device).objects.count()
            response = post_view(view, request)

        assert type(colliding_device).objects.count() == before
        errors = view_message_texts(request, "error")
        assert len(errors) == 1
        toast = errors[0]
        assert "NetBox object" in toast
        assert "NetBox device" not in toast
        assert "colliding device" not in toast
        assert response.status_code == 302
        assert (
            response["Location"]
            == f"{url_for('plugins:netbox_librenms_plugin:librenms_import')}?server_key={server_key}"
        )


# ---------------------------------------------------------------------------
# AddAsOOBView / PromoteToHostView — generic "oob" sentinel regression tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAddAsOOBViewGenericSentinel:
    """AddAsOOBView must not return HTTP 400 when oob_candidate.type == "oob"."""

    def test_generic_oob_sentinel_accepted_by_set_librenms_oob(self):
        """set_librenms_oob must not raise ValueError for oob_type='oob'."""
        from netbox_librenms_plugin.utils import get_librenms_oob, set_librenms_oob

        obj = make_device("generic-oob-storage")
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

        obj = make_device("legacy-oob-storage")
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
        obj = make_device("detected-oob-storage")
        obj.custom_field_data = {"librenms_id": {"default": {"id": 99}}}

        set_librenms_oob(obj, 42, "default", oob_type="oob")  # must not raise
        assert obj.custom_field_data["librenms_id"]["default"]["oob"]["id"] == 42
        assert obj.custom_field_data["librenms_id"]["default"]["oob"]["type"] == "oob"


@pytest.mark.django_db
class TestSetLibreNMSOOBGenericSentinel:
    """set_librenms_oob must accept the generic "oob" sentinel oob_type."""

    def test_promote_generic_oob_sentinel_accepted_by_set_librenms_oob(self):
        """The generic 'oob' sentinel from the promote path's existing_oob_type fallback must not raise in set_librenms_oob."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = make_device("promoted-oob-storage")
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

        view.get_validated_device_with_selections = validate_then_mutate
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
        """An OOB link matched through a non-sync virtual-chassis member is stored on the resolved sync device."""
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
        """The locked-row check rejects a legacy ID written during the race window before OOB link promotion."""
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
            user=grant_view_permission(
                self._device_writer("oob-owner-race-user"), "view", Device, constraints={"pk": conflicting_device.pk}
            ),
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
        assert b"already assigned to device &#x27;&lt;script&gt;the-idrac&lt;/script&gt;&#x27;" in response.content
        assert b"&amp;lt;script&amp;gt;" not in response.content
        # Nothing attached: the host device's entry gained no oob sub-block.
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 10}
        conflict_entry = Device.objects.get(pk=conflicting_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert conflict_entry == {"id": 17}

    def test_aborts_when_librenms_id_is_owned_by_a_vm(self):
        """An incoming OOB ID owned by a VM must not be attached to a Device."""
        from dcim.models import Device

        view = self._make_view()
        existing_device = make_device(
            "oob-vm-owner-target",
            serial="OOB-VM-OWNER-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        conflicting_vm = make_vm("oob-vm-owner")
        self._register_oob_device(17, "controller-node", serial=existing_device.serial, generic=True)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=grant_view_permission(
                self._device_writer("oob-vm-owner-user"),
                "view",
                type(conflicting_vm),
                constraints={"pk": conflicting_vm.pk},
            ),
            HTTP_HX_REQUEST="true",
        )

        def claim_incoming_id():
            type(conflicting_vm).objects.filter(pk=conflicting_vm.pk).update(
                custom_field_data={"librenms_id": {self.server_key: {"id": 17}}}
            )

        response = self._post_after_validation(view, request, 17, claim_incoming_id)

        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"already assigned to VM" in response.content
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 10}

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

        view.get_validated_device_with_selections = validate_then_mutate
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
            user=self._device_writer("promote-conflict-race-user", (("view", Device),)),
            HTTP_HX_REQUEST="true",
        )

        def claim_incoming_id():
            Device.objects.filter(pk=conflicting_device.pk).update(
                custom_field_data={"librenms_id": {self.server_key: {"id": 17}}}
            )

        response = self._post_after_validation(view, request, 17, claim_incoming_id)

        assert response.status_code == 200
        assert b"already assigned to device" in response.content
        assert b"&lt;script&gt;promote-conflict&lt;/script&gt;" in response.content
        assert b"&amp;lt;script&amp;gt;" not in response.content
        # The source device must be left unchanged (still host id 10, no OOB).
        existing_device.refresh_from_db()
        assert existing_device.custom_field_data["librenms_id"][self.server_key] == {"id": 10}
        conflicting_device.refresh_from_db()
        assert conflicting_device.custom_field_data["librenms_id"][self.server_key] == {"id": 17}

    def test_promote_rejected_when_a_vm_claimed_the_host_id_after_validation(self):
        """A VM claim created after validation must block the Device promotion."""
        view = self._make_view()
        existing_device = make_device(
            "promote-vm-conflict-controller",
            serial="PROMOTE-VM-CONFLICT-SERIAL",
            librenms_cf={self.server_key: {"id": 10}},
        )
        conflicting_vm = make_vm("promote-vm-conflict-owner")
        self._register_host_device(17, "promote-vm-conflict-host", serial=existing_device.serial)
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "existing_device_id": str(existing_device.pk)},
            user=self._device_writer("promote-vm-conflict-user", (("view", type(conflicting_vm)),)),
            HTTP_HX_REQUEST="true",
        )

        def claim_incoming_id():
            type(conflicting_vm).objects.filter(pk=conflicting_vm.pk).update(
                custom_field_data={"librenms_id": {self.server_key: {"id": 17}}}
            )

        response = self._post_after_validation(view, request, 17, claim_incoming_id)

        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"already assigned to VM" in response.content
        existing_device.refresh_from_db()
        assert existing_device.custom_field_data["librenms_id"][self.server_key] == {"id": 10}

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
            user=self._device_writer("promote-owner-race-user", (("view", Device),)),
            HTTP_HX_REQUEST="true",
        )

        def claim_incoming_id():
            Device.objects.filter(pk=conflicting_device.pk).update(
                custom_field_data={"librenms_id": {self.server_key: {"id": 17}}}
            )

        response = self._post_after_validation(view, request, 17, claim_incoming_id)

        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"already assigned to device &#x27;promote-thief&#x27;" in response.content
        # Nothing committed: the host slot is unchanged and no OOB slot was written.
        entry = Device.objects.get(pk=existing_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 10}
        conflict_entry = Device.objects.get(pk=conflicting_device.pk).custom_field_data["librenms_id"][self.server_key]
        assert conflict_entry == {"id": 17}


@pytest.mark.django_db
class _MergeViewHarness:
    """Drive merge actions through real validation, permissions, HTTP, and ORM state."""

    @pytest.fixture(autouse=True)
    def _configure_merge_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"merge-{request.node.name}".replace("_", "-")
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

    def _post_merge(self, winner, donor, *, device_id=99):
        from dcim.models import Device
        from netbox_librenms_plugin.views.imports.actions import MergeNetBoxDevicesView

        if not donor.serial:
            donor.serial = f"MERGE-{donor.pk}"
            donor.save(update_fields=["serial"])
        self.librenms_server.device_info_response(
            device_id=device_id,
            hostname=winner.name,
            hardware=winner.device_type.model,
            os="ios",
            serial=donor.serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(device_id, [], {})
        request = make_view_request(
            "post",
            {"server_key": self.server_key, "winner_pk": str(winner.pk)},
            user=make_view_user(
                f"merge-user-{winner.pk}-{donor.pk}",
                [("change", Device)],
            ),
            HTTP_HX_REQUEST="true",
        )
        return post_view(MergeNetBoxDevicesView(), request, device_id=device_id)


class TestMergeNetBoxDevicesViewOOBTransfer(_MergeViewHarness):
    """MergeNetBoxDevicesView.post: oob_ip may only move to the winner when its underlying IP already sits on a winner interface (the merge does not move interfaces, and the save skips full_clean())."""

    def _run(self, *, oob_on_winner):
        """Drive a merge where the donor's oob_ip sits on an interface owned by the winner (``oob_on_winner=True``) or by the donor (``False``)."""
        from dcim.models import Device
        from netbox_librenms_plugin.tests.conftest import ip_on

        winner = make_device("merge-winner", librenms_cf={self.server_key: {"id": 20}})
        donor = make_device("merge-donor", librenms_cf={self.server_key: {"id": 10}})

        # The donor carries an oob_ip whose underlying IP is assigned to an interface
        # owned by whichever device the scenario dictates. save() (not full_clean) lets
        # us seed the winner-interface case the view is designed to resolve.
        oob_host = winner if oob_on_winner else donor
        oob_ip = ip_on(oob_host, "192.0.2.7/32", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()

        resp = self._post_merge(winner, donor)
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
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import ip_on

        winner = make_device("merge-winner-lock", librenms_cf={self.server_key: {"id": 20}})
        donor = make_device("merge-donor-lock", librenms_cf={self.server_key: {"id": 10}})
        oob_ip = ip_on(winner, "192.0.2.9/32", "mgmt0")  # IP on a WINNER interface → transfer path runs
        donor.oob_ip = oob_ip
        donor.save()

        with CaptureQueriesContext(connection) as ctx:
            resp = self._post_merge(winner, donor)
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

    def test_save_failure_rolls_back_donor_oob_release_and_marker(self, monkeypatch):
        """Forced persist failure mid-merge must roll back the donor's already-executed save."""
        from dcim.models import Device
        from django.db import IntegrityError
        from netbox_librenms_plugin.tests.conftest import ip_on

        winner = make_device("merge-winner-fail", librenms_cf={self.server_key: {"id": 20}})
        donor = make_device("merge-donor-fail", librenms_cf={self.server_key: {"id": 10}})
        # oob_ip on a winner-owned interface → the transfer path runs (oob_ip in update_fields).
        oob_ip = ip_on(winner, "192.0.2.8/32", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()

        # Force the SECOND Device.save (the winner's, per the donor-then-winner order) to fail,
        # so the donor's release is already written inside the savepoint when rollback fires.
        real_save = Device.save
        save_calls = []

        def flaky_save(self, *args, **kwargs):
            save_calls.append(self.pk)
            if len(save_calls) == 2:
                raise IntegrityError("forced winner save failure")
            return real_save(self, *args, **kwargs)

        monkeypatch.setattr(Device, "save", flaky_save)
        resp = self._post_merge(winner, donor)

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
        entry = donor.custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 10}
        assert "_migrated_to" not in entry

    def test_transfer_survives_interface_move_onto_winner_between_read_and_lock(self, monkeypatch):
        """A concurrent interface move ONTO the winner, landing between the assigned_object read and the interface lock, must not spuriously fail the merge: the locked IP's GFK cache is refreshed to the freshly-locked (winner-owned) interface before set_device_ip_fk re-checks ownership."""
        from dcim.models import Interface
        from netbox_librenms_plugin.tests.conftest import ip_on

        winner = make_device("merge-winner-race", librenms_cf={self.server_key: {"id": 20}})
        donor = make_device("merge-donor-race", librenms_cf={self.server_key: {"id": 10}})
        # The IP starts on a DONOR interface, so the merge's assigned_object read caches device_id=donor.
        oob_ip = ip_on(donor, "192.0.2.11/32", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()
        iface_pk = oob_ip.assigned_object_id

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

        monkeypatch.setattr(Interface.objects, "select_for_update", moving_sfu)
        resp = self._post_merge(winner, donor)

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
class TestMergeNetBoxDevicesViewVCSyncDevice(_MergeViewHarness):
    """MergeNetBoxDevicesView.post: when a merge candidate is a Virtual Chassis member, the LibreNMS link (host id / OOB) and the ``_migrated_to`` marker must be merged on the VC's sync device (``get_librenms_sync_device``), not the raw selected member. Writing to a non-sync member either split-brains a VC that already has a linked member, or leaves the donor's real link (on its sync sibling) uncleared."""

    def _run_merge(self, *, winner, donor):
        resp = self._post_merge(winner, donor)
        assert resp.status_code == 200
        return resp

    def _entry(self, device):
        device.refresh_from_db()
        return (device.custom_field_data.get("librenms_id") or {}).get(self.server_key) or {}

    def test_winner_is_non_sync_vc_member_link_lands_on_sync_device(self):
        """Winner is a non-sync VC member whose sync sibling already holds a host id; the donor's id must merge onto the sync sibling (into its OOB half) and NEVER onto the raw winner member — otherwise two members of one chassis hold ``librenms_id`` (split brain)."""
        _vc, m1, m2 = _two_member_vc("mrg-vc-win", m1_cf={self.server_key: {"id": 30}}, m2_cf=None)
        donor = make_device("mrg-vc-win-donor", librenms_cf={self.server_key: {"id": 40}})

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
        _vc, m1, m2 = _two_member_vc("mrg-vc-don", m1_cf={self.server_key: {"id": 30}}, m2_cf=None)
        winner = make_device("mrg-vc-don-winner", librenms_cf={self.server_key: {"id": 50}})

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
        winner = make_device("mrg-vc-legacy-winner", librenms_cf={self.server_key: {"id": 50}})

        response = self._run_merge(winner=winner, donor=selected_member)

        assert b"Donor device has a legacy bare-integer librenms_id" in response.content
        assert b"Convert mapping" in response.content

    def test_merge_locks_every_vc_member_before_resolving_the_sync_device(self):
        """The merge must lock every VC member (incl. bystanders) before resolving the sync device."""
        import re

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        vc, m1, m2 = _two_member_vc("mrg-vc-lock", m1_cf={self.server_key: {"id": 30}}, m2_cf=None)
        # A bystander member: not the selected winner (m2) and not the sync device (m1).
        m3 = make_device("mrg-vc-lock-m3", librenms_cf=None)
        m3.virtual_chassis = vc
        m3.vc_position = 3
        m3.save()
        donor = make_device("mrg-vc-lock-donor", librenms_cf={self.server_key: {"id": 40}})

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
class TestMergeNetBoxDevicesViewFailClosed(_MergeViewHarness):
    """Merge preparation failures must return a toast and leave the donor unmigrated."""

    def test_orphan_host_id_merge_fails_closed_and_leaves_donor_unmigrated(self):
        """A winner holding both host id + oob and a donor with a distinct host-id-only link fails closed."""
        winner = make_device(
            "merge-orphan-winner",
            librenms_cf={self.server_key: {"id": 100, "oob": {"id": 50, "type": "idrac"}}},
        )
        donor = make_device(
            "<script>merge-orphan-donor</script>",
            librenms_cf={self.server_key: {"id": 200}},
        )

        resp = self._post_merge(winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"Cannot merge" in resp.content
        assert b"&lt;script&gt;merge-orphan-donor&lt;/script&gt;" in resp.content
        assert b"&amp;lt;script&amp;gt;" not in resp.content
        # Donor's link is preserved and it was NOT marked migrated (no orphaned LibreNMS host).
        donor.refresh_from_db()
        entry = donor.custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 200}
        assert "_migrated_to" not in entry

    def test_corrupt_donor_oob_id_with_winner_oob_fails_closed_not_500(self):
        """A donor oob id merge_librenms_links skipped (winner already has an oob) fails closed at the marker, not a 500."""
        winner = make_device(
            "merge-f2-winner",
            librenms_cf={self.server_key: {"id": 5, "oob": {"id": 9, "type": "idrac"}}},
        )
        # Same host id (so the orphan guard doesn't fire) but a corrupt donor oob id. Because the
        # winner already holds an oob, merge_librenms_links() skips validating the donor oob id —
        # mark_librenms_migrated() is the one that rejects it, and that call must be guarded too.
        donor = make_device(
            "merge-f2-donor",
            librenms_cf={self.server_key: {"id": 5, "oob": {"id": "abc"}}},
        )

        resp = self._post_merge(winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"Cannot merge" in resp.content
        donor.refresh_from_db()
        entry = donor.custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 5, "oob": {"id": "abc"}}
        assert "_migrated_to" not in entry

    def test_oob_transfer_valueerror_fails_closed_and_rolls_back(self, monkeypatch):
        """A ValueError from the oob_ip transfer (the TOCTOU race the lock guards) fails closed with rollback, not a 500."""
        import netbox_librenms_plugin.views.imports.actions as actions_mod
        from netbox_librenms_plugin.tests.conftest import ip_on

        winner = make_device("merge-f5-winner", librenms_cf={self.server_key: {"id": 20}})
        donor = make_device("merge-f5-donor", librenms_cf={self.server_key: {"id": 10}})
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

        monkeypatch.setattr(actions_mod, "set_device_ip_fk", racy)
        resp = self._post_merge(winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"Cannot merge" in resp.content
        # Rolled back: donor keeps its oob_ip and link, winner never claimed it, no marker stamped.
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.oob_ip_id == oob_ip.pk
        assert winner.oob_ip_id is None
        entry = donor.custom_field_data["librenms_id"][self.server_key]
        assert entry == {"id": 10}
        assert "_migrated_to" not in entry

    def test_oob_ip_lock_database_error_fails_closed_without_leaking_backend_text(self, monkeypatch):
        """A DB failure while acquiring the OOB-IP lock returns a safe toast and rolls back."""
        from django.db import DatabaseError

        from ipam.models import IPAddress
        from netbox_librenms_plugin.tests.conftest import ip_on

        winner = make_device("merge-db-lock-winner", librenms_cf={self.server_key: {"id": 20}})
        donor = make_device("merge-db-lock-donor", librenms_cf={self.server_key: {"id": 10}})
        oob_ip = ip_on(winner, "192.0.2.12/32", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()

        def fail_oob_lock():
            raise DatabaseError("forced lock timeout with backend detail")

        monkeypatch.setattr(IPAddress.objects, "select_for_update", fail_oob_lock)
        resp = self._post_merge(winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"database operation failed" in resp.content
        assert b"forced lock timeout" not in resp.content
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.oob_ip_id == oob_ip.pk
        assert winner.oob_ip_id is None
        assert donor.custom_field_data["librenms_id"][self.server_key] == {"id": 10}

    def test_device_lock_database_error_fails_closed_without_leaking_backend_text(self, monkeypatch):
        """A DB failure while locking the merge pair returns a safe retry toast, not a 500."""
        from dcim.models import Device
        from django.db import DatabaseError

        winner = make_device("merge-device-lock-winner", librenms_cf={self.server_key: {"id": 20}})
        donor = make_device("merge-device-lock-donor", librenms_cf={self.server_key: {"id": 10}})

        def fail_device_lock():
            raise DatabaseError("forced primary lock timeout with backend detail")

        monkeypatch.setattr(Device.objects, "select_for_update", fail_device_lock)
        resp = self._post_merge(winner, donor)

        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"database operation failed" in resp.content
        assert b"forced primary lock timeout" not in resp.content
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.custom_field_data["librenms_id"][self.server_key] == {"id": 10}
        assert winner.custom_field_data["librenms_id"][self.server_key] == {"id": 20}


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

    def test_a_non_ascii_digit_id_is_refused(self):
        """Only plain ASCII digits name a pk; int() also reads fullwidth digits."""
        from django.db import transaction

        from dcim.models import Interface

        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms

        view = self._view()
        dev = make_device("oob-res-digit-forms")
        iface = make_interface(dev, "eth0")
        user = make_user_with_perms("oob-res-digit-forms", [("view", Interface)])
        # int() reads fullwidth digits too, so this string spells the real pk.
        fullwidth = "".join(chr(ord(digit) - ord("0") + 0xFF10) for digit in str(iface.pk))
        req = make_request("post", {"oob_interface_id": fullwidth}, user=user)

        with transaction.atomic():
            result_iface, reason = view._resolve_oob_interface(req, dev)

        assert (result_iface, reason) == (None, None)

    def test_create_new_interface(self):
        from django.db import transaction

        from dcim.models import Interface

        view = self._view()
        dev = make_device("oob-res-create")
        req = _make_request(
            post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"},
            user_is_superuser=True,
        )
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
        req = _make_request(
            post={"oob_interface_id": "__new__", "oob_new_interface_name": long_name},
            user_is_superuser=True,
        )
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

    def test_explicit_pk_outside_the_view_grant_reports_the_scope_reason(self):
        """An explicit PK the caller may not view must report the scope reason, not "no selection"."""
        from dcim.models import Interface
        from django.db import transaction

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request, make_user_with_perms

        view = self._view()
        device = make_device("oob-res-pk-scope")
        hidden = make_interface(device, "idrac0")
        allowed = make_interface(device, "eth0")
        user = make_user_with_perms("oob-interface-pk-scope", [("add", Interface)])
        user = grant(user, "view", Interface, constraints={"pk": allowed.pk})
        request = make_request("post", {"oob_interface_id": str(hidden.pk)}, user=user)

        with transaction.atomic():
            result_iface, reason = view._resolve_oob_interface(request, device)

        # The caller DID select an interface, so the "no selection made" pair would make the
        # message chain tell the operator to choose one.
        assert result_iface is None and reason == "name_out_of_scope"

    def test_create_without_add_perm_returns_permission_add(self):
        """No existing row + user lacks Interface 'add' → the write-time re-check refuses the create rather than silently creating it."""
        from django.db import transaction

        from dcim.models import Interface

        view = self._view()
        dev = make_device("oob-res-noperm")
        req = make_view_request(
            "post",
            {"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"},
            user=make_view_user("oob-res-no-add-user", [("view", Interface)]),
        )
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
            ip, reason = view._attach_oob_ip(
                _make_request(post={}, user_is_superuser=True),
                "10.0.0.9",
                iface,
            )
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
            ip, reason = view._attach_oob_ip(
                _make_request(post={}, user_is_superuser=True),
                "10.0.0.9",
                iface,
            )
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
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(req, "10.0.0.9", iface)
        assert ip is None and reason == "permission_add"
        assert not IPAddress.objects.filter(address__net_host="10.0.0.9").exists()

    def test_locks_candidate_row_with_select_for_update(self):
        """The candidate IP address row is locked through the caller's change-scoped queryset in the emitted SQL."""
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
        from dcim.models import Interface
        from ipam.models import IPAddress
        from netbox_librenms_plugin.tests.conftest import make_device

        view = self._view()
        device = make_device("oob-perm-all")  # no idrac0 interface, no IP for 10.0.0.9 yet
        req = make_view_request(
            "post",
            {"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"},
            user=make_view_user(
                "oob-perm-all-user",
                [("add", Interface), ("add", IPAddress), ("change", IPAddress)],
            ),
        )
        assert view._missing_oob_ip_permissions(req, "10.0.0.9", device=device) is None

    def test_blocks_new_interface_without_add_interface(self):
        from netbox_librenms_plugin.tests.conftest import make_device

        view = self._view()
        # idrac0 does NOT exist on the device → _resolve_oob_interface would create it → add_interface.
        device = make_device("oob-perm-noaddiface")
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
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
            # AnonymousUser denies everything. If the IP check ran, it would return a warning.
            with CaptureQueriesContext(connection) as ctx:
                assert view._missing_oob_ip_permissions(req, "10.0.0.9", device=device) is None
            assert not any("ipam_ipaddress" in q["sql"].lower() for q in ctx.captured_queries)

    def test_new_interface_name_that_already_exists_does_not_require_add(self):
        """__new__ + an existing interface name is reused by _resolve_oob_interface, so no Interface write happens — 'add_interface' must NOT be required for a user with change-Device + add_ipaddress."""
        from ipam.models import IPAddress
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        view = self._view()
        device = make_device("oob-perm-reuse")
        make_interface(device, "idrac0")  # already exists on THIS device → reused, no create
        # A same-named interface on ANOTHER device must not count (the existence check is device-scoped).
        make_interface(make_device("oob-perm-reuse-other"), "idrac0")
        req = make_view_request(
            "post",
            {"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"},
            user=make_view_user("oob-perm-reuse-user", [("add", IPAddress)]),
        )
        assert view._missing_oob_ip_permissions(req, "10.0.0.9", device=device) is None

    def test_requires_add_ipaddress_when_creating(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        view = self._view()
        device = make_device("oob-perm-addip")
        iface = make_interface(device, "eth0")  # existing iface → no add_interface; no IP → create
        req = _make_request(post={"oob_interface_id": str(iface.pk)})
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
        req = make_view_request(
            "post",
            {"oob_interface_id": str(iface.pk)},
            user=make_view_user("oob-perm-vrf-user", [("add", IPAddress)]),
        )
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
        # AnonymousUser has no change permission, which is not needed for this no-op.
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
        msg = view._missing_oob_ip_permissions(req, "10.0.0.9", device=device)
        assert msg is not None and "change_ipaddress" in msg


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
class TestMappingChangeScope:
    """Natural-key mapping updates must remain inside constrained change grants."""

    @pytest.fixture(autouse=True)
    def _configure_librenms_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"mapping-scope-{request.node.name}".replace("_", "-")
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

    def _register_device(self, name, *, hardware="Mapping Scope Hardware", os="mapping-scope-os"):
        device_id = 73
        target = make_device(name, librenms_cf={self.server_key: {"id": device_id}})
        self.librenms_server.device_info_response(
            device_id=device_id,
            hostname=name,
            hardware=hardware,
            os=os,
            serial=target.serial,
            ip="198.18.0.73",
        )
        self.librenms_server.vc_inventory_callable(device_id, [], {})
        return target, device_id

    def _post(self, view, user, device_id, **data):
        request = make_view_request(
            "post",
            {"server_key": self.server_key, **data},
            user=user,
            HTTP_HX_REQUEST="true",
        )
        return post_view(view, request, device_id=device_id)

    def test_device_type_mapping_outside_change_grant_is_not_updated(self):
        from dcim.models import DeviceType

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
        _target, device_id = self._register_device("mapping-scope-hidden-device", hardware=raw_hardware)
        user = make_user_with_perms("mapping-change-scope", [("view", type(old_type))])
        user = grant(user, "change", DeviceTypeMapping, constraints={"pk": allowed.pk})

        response = self._post(
            AddDeviceTypeMappingView(),
            user,
            device_id,
            device_type_id=str(new_type.pk),
        )

        # The refusal text, not just the unchanged row: a failed permission gate, a rebind
        # failure and the broad except all leave the mapping alone too.
        assert b"Existing mapping is no longer available." in response.content
        hidden.refresh_from_db()
        assert hidden.netbox_device_type_id == old_type.pk

    def test_platform_mapping_outside_change_grant_is_not_updated(self):
        from dcim.models import Platform

        from netbox_librenms_plugin.models import PlatformMapping
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.imports.actions import AddPlatformMappingView

        old_platform = Platform.objects.create(name="Mapping Scope Old", slug="mapping-scope-old")
        new_platform = Platform.objects.create(name="Mapping Scope New", slug="mapping-scope-new")
        allowed = PlatformMapping.objects.create(librenms_os="allowed-os", netbox_platform=old_platform)
        hidden = PlatformMapping.objects.create(librenms_os="hidden-os", netbox_platform=old_platform)
        _target, device_id = self._register_device("platform-mapping-scope-hidden-device", os=hidden.librenms_os)
        user = make_user_with_perms("platform-mapping-change-scope", [("view", Platform)])
        user = grant(user, "change", PlatformMapping, constraints={"pk": allowed.pk})

        response = self._post(
            AddPlatformMappingView(),
            user,
            device_id,
            platform_id=str(new_platform.pk),
        )

        assert b"Existing mapping is no longer available." in response.content
        hidden.refresh_from_db()
        assert hidden.netbox_platform_id == old_platform.pk

    def test_device_type_mapping_inside_change_grant_is_updated(self):
        """An in-scope device-type mapping still updates, proving the restricted path does not skip all writes."""
        from dcim.models import DeviceType

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
        _target, device_id = self._register_device("mapping-scope-allowed-device", hardware=raw_hardware)
        user = make_user_with_perms("mapping-change-control", [("view", type(old_type))])
        user = grant(user, "change", DeviceTypeMapping, constraints={"pk": allowed.pk})

        response = self._post(
            AddDeviceTypeMappingView(),
            user,
            device_id,
            device_type_id=str(new_type.pk),
        )

        assert b"Existing mapping is no longer available." not in response.content
        assert b' id="htmx-modal-content"' in response.content
        allowed.refresh_from_db()
        assert allowed.netbox_device_type_id == new_type.pk

    def test_platform_mapping_inside_change_grant_is_updated(self):
        """Control for the platform refusal above (see the device-type control)."""
        from dcim.models import Platform

        from netbox_librenms_plugin.models import PlatformMapping
        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms
        from netbox_librenms_plugin.views.imports.actions import AddPlatformMappingView

        old_platform = Platform.objects.create(name="Mapping Control Old", slug="mapping-control-old")
        new_platform = Platform.objects.create(name="Mapping Control New", slug="mapping-control-new")
        allowed = PlatformMapping.objects.create(librenms_os="allowed-control-os", netbox_platform=old_platform)
        _target, device_id = self._register_device("platform-mapping-scope-allowed-device", os=allowed.librenms_os)
        user = make_user_with_perms("platform-mapping-change-control", [("view", Platform)])
        user = grant(user, "change", PlatformMapping, constraints={"pk": allowed.pk})

        response = self._post(
            AddPlatformMappingView(),
            user,
            device_id,
            platform_id=str(new_platform.pk),
        )

        assert b"Existing mapping is no longer available." not in response.content
        assert b' id="htmx-modal-content"' in response.content
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
        resp = _rebind_or_htmx_error(view, request)

        assert resp is not None
        assert resp.status_code == 200
        assert resp["HX-Reswap"] == "none"
        assert b"no longer configured" in resp.content

    def test_resolved_server_key_returns_none_and_binds(self):
        from netbox_librenms_plugin.views.imports.actions import _rebind_or_htmx_error

        view = self._view()
        request = RequestFactory().post("/", {"server_key": "prod"})
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

    @pytest.fixture(autouse=True)
    def _configure_serial_server(self, request, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        self.server_key = f"serial-{request.node.name}".replace("_", "-")
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

    def _post_action(self, action, target, serial):
        """Drive the real conflict action through LibreNMS HTTP and ORM validation."""
        from dcim.models import Device
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        self.librenms_server.device_info_response(
            device_id=10,
            hostname=target.name,
            hardware=target.device_type.model,
            os="ios",
            serial=serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(10, [], {})
        request = make_view_request(
            "post",
            {
                "action": action,
                "existing_device_id": str(target.pk),
                "server_key": self.server_key,
            },
            user=make_view_user(
                f"serial-user-{target.pk}-{action}",
                [("change", Device)],
            ),
            HTTP_HX_REQUEST="true",
        )
        return post_view(DeviceConflictActionView(), request, device_id=10)

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
    """Conflict and out-of-band mutation endpoints resolve the posted ``existing_device_id`` with object scope."""

    _scoped_writer = staticmethod(_scoped_device_writer)

    @pytest.fixture(autouse=True)
    def _configure_scope_server(self, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as server:
            configure_test_servers(
                settings,
                {
                    "default": {
                        "librenms_url": server.url,
                        "api_token": "test-token",
                        "verify_ssl": False,
                    }
                },
            )
            self.librenms_server = server
            yield

    def _post_conflict(self, user, target, action="link"):
        """Drive the conflict action through real HTTP validation and object permissions."""
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        self.librenms_server.device_info_response(
            device_id=4242,
            hostname=target.name,
            hardware=target.device_type.model,
            os="ios",
            serial=target.serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(4242, [], {})
        request = make_view_request(
            "post",
            {"action": action, "existing_device_id": str(target.pk), "server_key": "default"},
            user=user,
            HTTP_HX_REQUEST="true",
        )
        return post_view(DeviceConflictActionView(), request, device_id=4242)

    @pytest.mark.parametrize("visible", [False, True])
    def test_id_conflict_identity_respects_view_scope(self, visible):
        """Only callers who can view the ID owner receive its identity."""
        from unittest.mock import patch

        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        target = make_device("conflict-scope-target")
        conflict = VirtualMachine.objects.create(
            pk=9000001, name="private-conflict-owner", cluster=make_cluster("conflict-scope"), status="active"
        )
        user = self._scoped_writer(target, "conflict-identity-reader")
        if visible:
            user = grant_view_permission(user, "view", VirtualMachine, constraints={"pk": conflict.pk})
        assert VirtualMachine.objects.restrict(user, "view").filter(pk=conflict.pk).exists() is visible

        get_inventory = LibreNMSAPI.get_device_inventory

        def claim_during_inventory_fetch(api, *args, **kwargs):
            result = get_inventory(api, *args, **kwargs)
            conflict.custom_field_data = {"librenms_id": {"default": 4242}}
            conflict.save(update_fields=["custom_field_data"])
            return result

        with patch.object(LibreNMSAPI, "get_device_inventory", claim_during_inventory_fetch):
            response = self._post_conflict(user, target)

        body = response.content.decode()
        if visible:
            assert conflict.name in body
            assert f"(ID: {conflict.pk})" in body
        else:
            assert conflict.name not in body
            assert str(conflict.pk) not in body
            assert "already assigned to another object outside your view scope" in body
        target.refresh_from_db()
        assert not target.custom_field_data.get("librenms_id")

    def _post_add_as_oob(self, user, target):
        """Drive OOB attachment through real HTTP validation and object permissions."""
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        if not target.serial:
            target.serial = f"SCOPE-OOB-{target.pk}"
            target.save(update_fields=["serial"])
        self.librenms_server.device_info_response(
            device_id=4343,
            hostname=f"{target.name}-idrac",
            hardware="Integrated Remote Access Controller",
            os="idrac",
            serial=target.serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(4343, [], {})
        request = make_view_request(
            "post",
            {"existing_device_id": str(target.pk), "server_key": "default"},
            user=user,
            HTTP_HX_REQUEST="true",
        )
        return post_view(AddAsOOBView(), request, device_id=4343)

    @pytest.mark.parametrize("visible", [False, True])
    def test_oob_id_conflict_identity_respects_view_scope(self, visible):
        """Only callers who can view the OOB ID owner receive its identity."""
        from unittest.mock import patch

        from django.utils.html import escape
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        target = make_device("oob-conflict-scope-target")
        conflict = VirtualMachine.objects.create(
            pk=9000001, name="private-oob-conflict-owner", cluster=make_cluster("oob-conflict-scope"), status="active"
        )
        user = self._scoped_writer(target, "oob-conflict-identity-reader")
        assert not user.is_superuser
        if visible:
            user = grant_view_permission(user, "view", VirtualMachine, constraints={"pk": conflict.pk})
        assert VirtualMachine.objects.restrict(user, "view").filter(pk=conflict.pk).exists() is visible

        get_inventory = LibreNMSAPI.get_device_inventory

        def claim_during_inventory_fetch(api, *args, **kwargs):
            result = get_inventory(api, *args, **kwargs)
            conflict.custom_field_data = {"librenms_id": {"default": 4343}}
            conflict.save(update_fields=["custom_field_data"])
            return result

        with patch.object(LibreNMSAPI, "get_device_inventory", claim_during_inventory_fetch):
            response = self._post_add_as_oob(user, target)

        body = response.content.decode()
        if visible:
            assert (
                escape(f"LibreNMS device #4343 is already assigned to VM '{conflict.name}'; refresh and retry.") in body
            )
        else:
            assert conflict.name not in body
            assert str(conflict.pk) not in body
            assert "already assigned to another object outside your view scope" in body
        target.refresh_from_db()
        assert not target.custom_field_data.get("librenms_id")

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
class TestPromoteAndMergeObjectScope:
    """Promote and merge resolve client-supplied device IDs through a restricted queryset."""

    @pytest.fixture(autouse=True)
    def _configure_scope_server(self, settings, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        with run_librenms_server() as server:
            configure_test_servers(
                settings,
                {
                    "default": {
                        "librenms_url": server.url,
                        "api_token": "test-token",
                        "verify_ssl": False,
                    }
                },
            )
            self.librenms_server = server
            yield

    def _post_promote(self, user, target, **overrides):
        """Drive promotion through real HTTP validation and object permissions."""
        from netbox_librenms_plugin.views.imports.actions import PromoteToHostView

        if not target.serial:
            target.serial = f"SCOPE-PROMOTE-{target.pk}"
            target.save(update_fields=["serial"])
        self.librenms_server.device_info_response(
            device_id=55,
            hostname=f"{target.name}-host",
            hardware=target.device_type.model,
            os="ios",
            serial=target.serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(55, [], {})
        post_data = {"existing_device_id": str(target.pk), "server_key": "default", **overrides}
        request = make_view_request(
            "post",
            post_data,
            user=user,
            HTTP_HX_REQUEST="true",
        )
        return post_view(PromoteToHostView(), request, device_id=55)

    def _post_merge(self, user, winner, donor):
        """Drive merge through real HTTP validation and object permissions."""
        from netbox_librenms_plugin.views.imports.actions import MergeNetBoxDevicesView

        if not donor.serial:
            donor.serial = f"SCOPE-MERGE-{donor.pk}"
            donor.save(update_fields=["serial"])
        self.librenms_server.device_info_response(
            device_id=99,
            hostname=winner.name,
            hardware=winner.device_type.model,
            os="ios",
            serial=donor.serial,
            ip="",
        )
        self.librenms_server.vc_inventory_callable(99, [], {})
        request = make_view_request(
            "post",
            {"winner_pk": str(winner.pk), "server_key": "default"},
            user=user,
            HTTP_HX_REQUEST="true",
        )
        return post_view(MergeNetBoxDevicesView(), request, device_id=99)

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
        from django.db import connection

        target = make_device("promote-legacy-race", librenms_cf={"default": {"id": 10}})
        user = _scoped_device_writer(target, "scoped-promote-legacy-race")
        concurrent_write_applied = False

        def concurrent_legacy_write(execute, sql, params, many, context):
            nonlocal concurrent_write_applied
            if not concurrent_write_applied and "FOR UPDATE" in sql and 'FROM "dcim_device"' in sql:
                concurrent_write_applied = True
                Device.objects.filter(pk=target.pk).update(custom_field_data={"librenms_id": 10})
            return execute(sql, params, many, context)

        with connection.execute_wrapper(concurrent_legacy_write):
            response = self._post_promote(user, target)

        assert concurrent_write_applied
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
        """An out-of-scope donor must not be merged away, and is not offered as a candidate either."""
        from dcim.models import Device

        winner = make_device("merge-scope-winner", librenms_cf={"default": {"id": 20}})
        donor = make_device("merge-scope-donor", librenms_cf={"default": {"id": 10}})
        user = _scoped_device_writer(winner, "scoped-merge-writer")  # scoped to the winner only

        response = self._post_merge(user, winner, donor)

        # The disclosure gate withdraws the whole suggestion when either candidate is out of
        # scope, so the refusal now happens before the winner/donor pks are resolved.
        assert b"does not match the validation result" in response.content
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
