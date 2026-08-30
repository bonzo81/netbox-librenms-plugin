"""Permission coverage through real users, grants, requests, and import flows."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.test.utils import override_script_prefix

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_user_with_perms,
    message_texts,
)


pytestmark = pytest.mark.django_db

SERVER_KEY = "default"


def _user(username, permissions=()):
    """Create a real user with the specified NetBox object permissions."""
    user = get_user_model().objects.create_user(username=username, password="test-password")
    for action, model in permissions:
        user = grant(user, action, model)
    return user


def _plugin_user(username, *, write):
    """Create a real user with view-only or view-and-change plugin access."""
    settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
    user = _user(username)
    user = grant(user, "view", settings_model)
    if write:
        user = grant(user, "change", settings_model)
    return user


def _permission_view(*, user=None, requirements=None, request_method="post", request_kwargs=None):
    from netbox_librenms_plugin.views.mixins import (
        LibreNMSPermissionMixin,
        NetBoxObjectPermissionMixin,
    )

    class PermissionView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin):
        required_object_permissions = requirements or {}

    view = PermissionView()
    if user is not None:
        request_kwargs = request_kwargs or {}
        view.request = make_request(request_method, user=user, **request_kwargs)
    return view


@pytest.fixture
def librenms_server(settings):
    """Configure a real LibreNMS client against the local HTTP boundary."""
    with librenms_mock_server() as server:
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {
            SERVER_KEY: {
                "display_name": "Permission test server",
                "librenms_url": server.url,
                "api_token": "permission-test-token",
                "cache_timeout": 300,
                "verify_ssl": False,
            }
        }
        plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
        settings.PLUGINS_CONFIG = plugin_config
        yield server


class TestLibreNMSPermissionMixin:
    def test_real_plugin_grants_control_write_access(self):
        writer = _plugin_user("permission-writer", write=True)
        reader = _plugin_user("permission-reader", write=False)

        assert _permission_view(user=writer).has_write_permission() is True
        assert _permission_view(user=reader).has_write_permission() is False

    def test_missing_request_fails_closed_for_html_and_json(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        view = LibreNMSPermissionMixin()

        assert view.has_write_permission() is False
        assert view.require_write_permission().status_code == 403
        assert view.require_write_permission_json().status_code == 403

    def test_writer_passes_html_and_json_guards(self):
        writer = _plugin_user("permission-guard-writer", write=True)
        view = _permission_view(user=writer)

        assert view.require_write_permission() is None
        assert view.require_write_permission_json() is None

    def test_denied_regular_request_redirects_to_the_safe_referrer(self):
        reader = _plugin_user("permission-regular-reader", write=False)
        view = _permission_view(
            user=reader,
            request_kwargs={"path": "/write-action/", "HTTP_REFERER": "/objects/"},
        )

        response = view.require_write_permission()

        assert response.status_code == 302
        assert response.url == "/objects/"
        assert message_texts(view.request, "error") == ["You do not have permission to perform this action."]

    def test_denied_htmx_request_uses_hx_redirect(self):
        reader = _plugin_user("permission-htmx-reader", write=False)
        view = _permission_view(
            user=reader,
            request_kwargs={
                "path": "/write-action/",
                "HTTP_REFERER": "/objects/",
                "HTTP_HX_REQUEST": "true",
            },
        )

        response = view.require_write_permission()

        assert response.status_code == 200
        assert response["HX-Redirect"] == "/objects/"

    def test_json_denial_uses_default_and_custom_messages(self):
        reader = _plugin_user("permission-json-reader", write=False)
        view = _permission_view(user=reader)

        default = view.require_write_permission_json()
        custom = view.require_write_permission_json("Custom denied message")

        assert default.status_code == 403
        assert json.loads(default.content) == {"error": "You do not have permission to perform this action."}
        assert json.loads(custom.content) == {"error": "Custom denied message"}


class TestLibreNMSGenericPermissionMixins:
    """Generic view mixins must supplement NetBox permissions without replacing them."""

    def test_read_mixin_requires_plugin_view_permission(self):
        from netbox_librenms_plugin.constants import PERM_VIEW_PLUGIN
        from netbox_librenms_plugin.views.mixins import LibreNMSGenericPermissionMixin

        assert LibreNMSGenericPermissionMixin.additional_permissions == (PERM_VIEW_PLUGIN,)

    def test_write_mixin_requires_plugin_view_and_change_permissions(self):
        from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN
        from netbox_librenms_plugin.views.mixins import LibreNMSGenericWritePermissionMixin

        assert LibreNMSGenericWritePermissionMixin.additional_permissions == (
            PERM_VIEW_PLUGIN,
            PERM_CHANGE_PLUGIN,
        )

    def test_generic_mixins_do_not_inherit_django_permission_required_mixin(self):
        from django.contrib.auth.mixins import PermissionRequiredMixin

        from netbox_librenms_plugin.views.mixins import (
            LibreNMSGenericPermissionMixin,
            LibreNMSGenericWritePermissionMixin,
        )

        assert PermissionRequiredMixin not in LibreNMSGenericPermissionMixin.__mro__
        assert PermissionRequiredMixin not in LibreNMSGenericWritePermissionMixin.__mro__


class TestAPIPermissions:
    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    def test_safe_methods_require_the_real_view_grant(self, method):
        from netbox_librenms_plugin.api.views import LibreNMSPluginPermission

        reader = _plugin_user(f"api-safe-{method.lower()}", write=False)
        denied = _user(f"api-safe-denied-{method.lower()}")

        assert LibreNMSPluginPermission().has_permission(make_request(method.lower(), user=reader), None) is True
        assert LibreNMSPluginPermission().has_permission(make_request(method.lower(), user=denied), None) is False

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_mutating_methods_require_the_real_change_grant(self, method):
        from netbox_librenms_plugin.api.views import LibreNMSPluginPermission

        writer = _plugin_user(f"api-write-{method.lower()}", write=True)
        reader = _plugin_user(f"api-write-denied-{method.lower()}", write=False)

        assert LibreNMSPluginPermission().has_permission(make_request(method.lower(), user=writer), None) is True
        assert LibreNMSPluginPermission().has_permission(make_request(method.lower(), user=reader), None) is False


class TestPermissionConstants:
    def test_plugin_permission_names_are_stable(self):
        from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN

        assert PERM_VIEW_PLUGIN == "netbox_librenms_plugin.view_librenmssettings"
        assert PERM_CHANGE_PLUGIN == "netbox_librenms_plugin.change_librenmssettings"


class TestObjectPermissionHelpers:
    def test_real_grants_report_only_missing_permissions(self):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.import_utils import check_user_permissions

        user = _user("helper-partial", [("add", Device), ("change", Device), ("change", Interface)])

        has_all, missing = check_user_permissions(
            user,
            ["dcim.add_device", "dcim.change_device", "dcim.add_interface"],
        )

        assert has_all is False
        assert missing == ["dcim.add_interface"]

    def test_all_real_grants_pass(self):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.import_utils import check_user_permissions

        user = _user("helper-complete", [("add", Device), ("add", Interface)])

        assert check_user_permissions(user, ["dcim.add_device", "dcim.add_interface"]) == (True, [])

    def test_missing_user_is_rejected(self):
        from netbox_librenms_plugin.import_utils import check_user_permissions

        with pytest.raises(PermissionDenied, match="No user context"):
            check_user_permissions(None, ["dcim.add_device"])

    def test_require_permissions_names_the_action_and_every_missing_grant(self):
        from netbox_librenms_plugin.import_utils import require_permissions

        user = _user("helper-denied")

        with pytest.raises(PermissionDenied) as exc_info:
            require_permissions(user, ["dcim.add_device", "dcim.add_interface"], "import devices")

        assert str(exc_info.value) == (
            "You do not have permission to import devices. Missing permissions: dcim.add_device, dcim.add_interface"
        )

    def test_require_permissions_accepts_real_grants(self):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.import_utils import require_permissions

        user = _user("helper-allowed", [("add", Device), ("add", Interface)])

        assert require_permissions(user, ["dcim.add_device", "dcim.add_interface"], "import devices") is None

    @pytest.mark.parametrize(
        ("device_ids", "vm_imports", "expected"),
        [
            ([], {}, []),
            ([1], {}, ["dcim.add_device", "dcim.change_device"]),
            ([], {2: {}}, ["virtualization.add_virtualmachine"]),
            (
                [1],
                {2: {}},
                ["dcim.add_device", "dcim.change_device", "virtualization.add_virtualmachine"],
            ),
        ],
    )
    def test_required_import_permissions(self, device_ids, vm_imports, expected):
        from netbox_librenms_plugin.import_utils import required_import_permissions

        assert required_import_permissions(device_ids, vm_imports) == expected


class TestNetBoxObjectPermissionMixin:
    def test_restricted_queryset_locks_only_the_model_row_with_nullable_constraints(self):
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("permission-lock-nullable")
        user = make_user_with_perms(
            "permission-lock-nullable",
            [("change", Device)],
            constraints={"site__region": None},
        )
        view = _permission_view(user=user)

        with transaction.atomic():
            locked = view.restricted_queryset(Device, "change").select_for_update(of=("self",)).get(pk=device.pk)

        assert locked == device

    def test_real_object_grants_control_the_permission_matrix(self):
        from dcim.models import Interface

        granted = make_user_with_perms(
            "object-matrix-granted",
            [("add", Interface), ("change", Interface)],
        )
        partial = make_user_with_perms("object-matrix-partial", [("change", Interface)])
        requirements = {"POST": [("add", Interface), ("change", Interface)]}

        assert _permission_view(user=granted, requirements=requirements).check_object_permissions("POST") == (
            True,
            [],
        )
        assert _permission_view(user=partial, requirements=requirements).check_object_permissions("POST") == (
            False,
            ["dcim.add_interface"],
        )

    def test_no_requirements_pass_even_without_a_request(self):
        assert _permission_view().check_object_permissions("POST") == (True, [])

    def test_missing_request_fails_closed_with_real_model_permissions(self):
        from dcim.models import Interface

        view = _permission_view(requirements={"POST": [("add", Interface)]})

        assert view.require_object_permissions("POST").status_code == 403

    def test_real_grant_passes_html_and_json_object_guards(self):
        from dcim.models import Interface

        user = make_user_with_perms("object-guard-writer", [("delete", Interface)])
        view = _permission_view(user=user, requirements={"POST": [("delete", Interface)]})

        assert view.require_object_permissions("POST") is None
        assert view.require_object_permissions_json("POST") is None

    def test_denied_object_grant_redirects_and_records_the_missing_permission(self):
        from dcim.models import Interface

        user = make_user_with_perms("object-guard-reader", [])
        view = _permission_view(
            user=user,
            requirements={"POST": [("add", Interface)]},
            request_kwargs={"path": "/write-action/", "HTTP_REFERER": "/interfaces/"},
        )

        response = view.require_object_permissions("POST")

        assert response.status_code == 302
        assert response.url == "/interfaces/"
        assert message_texts(view.request, "error") == ["Missing permissions: dcim.add_interface"]

    def test_denied_object_grant_returns_htmx_and_json_contracts(self):
        from dcim.models import Interface

        user = make_user_with_perms("object-guard-htmx-reader", [])
        requirements = {"POST": [("delete", Interface)]}
        view = _permission_view(
            user=user,
            requirements=requirements,
            request_kwargs={"HTTP_REFERER": "/interfaces/", "HTTP_HX_REQUEST": "true"},
        )

        html = view.require_object_permissions("POST")
        json_response = view.require_object_permissions_json("POST")

        assert html["HX-Redirect"] == "/interfaces/"
        assert json.loads(json_response.content) == {"error": "Missing permissions: dcim.delete_interface"}

    def test_combined_guard_distinguishes_plugin_and_object_permissions(self):
        from dcim.models import Device

        allowed = make_user_with_perms("combined-allowed", [("change", Device)])
        plugin_denied = _user("combined-plugin-denied", [("change", Device)])
        object_denied = make_user_with_perms("combined-object-denied", [])
        requirements = {"POST": [("change", Device)]}

        assert _permission_view(user=allowed, requirements=requirements).require_all_permissions("POST") is None

        plugin_response = _permission_view(
            user=plugin_denied,
            requirements=requirements,
            request_kwargs={"HTTP_REFERER": "/devices/"},
        ).require_all_permissions("POST")
        object_view = _permission_view(
            user=object_denied,
            requirements=requirements,
            request_kwargs={"HTTP_REFERER": "/devices/"},
        )
        object_response = object_view.require_all_permissions("POST")

        assert plugin_response.status_code == 302
        assert object_response.status_code == 302
        assert message_texts(object_view.request, "error") == ["Missing permissions: dcim.change_device"]

    def test_combined_json_guard_returns_the_first_denial(self):
        from dcim.models import Device

        plugin_denied = _user("combined-json-plugin-denied", [("change", Device)])
        object_denied = make_user_with_perms("combined-json-object-denied", [])
        requirements = {"POST": [("change", Device)]}

        plugin_response = _permission_view(
            user=plugin_denied,
            requirements=requirements,
        ).require_all_permissions_json("POST")
        object_response = _permission_view(
            user=object_denied,
            requirements=requirements,
        ).require_all_permissions_json("POST")

        assert json.loads(plugin_response.content) == {"error": "You do not have permission to perform this action."}
        assert json.loads(object_response.content) == {"error": "Missing permissions: dcim.change_device"}


class TestBulkImportPermissions:
    def test_device_import_requires_add_and_change_but_not_virtual_chassis(self, librenms_server):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils import bulk_import_devices_shared

        user = _user("device-import-writer", [("add", Device), ("change", Device)])
        librenms_server.register("/api/v0/devices/7001", {"status": "error"}, status=404)

        result = bulk_import_devices_shared([7001], server_key=SERVER_KEY, user=user)

        assert result["total"] == 1
        assert result["success"] == []
        assert result["failed"][0]["device_id"] == 7001

    def test_device_import_reports_the_exact_missing_grant_before_api_work(self, librenms_server):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils import bulk_import_devices_shared

        user = _user("device-import-partial", [("add", Device)])

        with pytest.raises(PermissionDenied, match="dcim.change_device"):
            bulk_import_devices_shared([7002], server_key=SERVER_KEY, user=user)

    def test_device_import_uses_the_real_job_user(self, librenms_server):
        from dcim.models import Device

        from netbox_librenms_plugin.import_utils import bulk_import_devices_shared

        user = _user("device-import-job-user", [("add", Device), ("change", Device)])
        job = SimpleNamespace(job=SimpleNamespace(user=user), logger=None)

        result = bulk_import_devices_shared([], server_key=SERVER_KEY, job=job)

        assert result == {
            "total": 0,
            "success": [],
            "failed": [],
            "skipped": [],
            "virtual_chassis_created": 0,
            "cancelled": False,
        }

    def test_vm_import_requires_the_real_add_grant(self, librenms_server):
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.import_utils import bulk_import_vms
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        user = _user("vm-import-writer", [("add", VirtualMachine)])

        assert bulk_import_vms({}, LibreNMSAPI(SERVER_KEY), user=user) == {
            "success": [],
            "failed": [],
            "skipped": [],
        }

    def test_vm_import_rejects_a_user_without_the_add_grant(self, librenms_server):
        from netbox_librenms_plugin.import_utils import bulk_import_vms
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with pytest.raises(PermissionDenied, match="virtualization.add_virtualmachine"):
            bulk_import_vms({}, LibreNMSAPI(SERVER_KEY), user=_user("vm-import-denied"))

    def test_vm_import_uses_the_real_job_user(self, librenms_server):
        from virtualization.models import VirtualMachine

        from netbox_librenms_plugin.import_utils import bulk_import_vms
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        user = _user("vm-import-job-user", [("add", VirtualMachine)])
        job = SimpleNamespace(job=SimpleNamespace(user=user), logger=None)

        assert bulk_import_vms({}, LibreNMSAPI(SERVER_KEY), job=job)["success"] == []


class TestSafeRedirectUrl:
    def test_internal_and_relative_referrers_are_accepted(self):
        from netbox_librenms_plugin.views.mixins import _get_safe_redirect_url

        absolute = make_request(
            "get",
            path="/fallback/",
            HTTP_REFERER="http://testserver/some/page/",
        )
        relative = make_request("get", path="/fallback/", HTTP_REFERER="/original/page/")

        assert _get_safe_redirect_url(absolute) == "http://testserver/some/page/"
        assert _get_safe_redirect_url(relative) == "/original/page/"

    def test_external_referrer_is_rejected_for_get_and_post(self):
        from netbox_librenms_plugin.views.mixins import _get_safe_redirect_url

        get_request = make_request(
            "get",
            path="/safe/fallback/",
            HTTP_REFERER="http://malicious.example.test/attack",
        )
        post_request = make_request(
            "post",
            path="/post-only/",
            HTTP_REFERER="http://malicious.example.test/attack",
        )

        with override_script_prefix("/netbox/"):
            assert _get_safe_redirect_url(get_request) == "/safe/fallback/"
            assert _get_safe_redirect_url(post_request) == "/netbox/"

    def test_missing_referrer_uses_get_path_or_deployment_root(self):
        from netbox_librenms_plugin.views.mixins import _get_safe_redirect_url

        get_request = make_request("get", path="/current/page/")
        post_request = make_request("post", path="/post-only/")
        request_without_path = SimpleNamespace(META={})

        with override_script_prefix("/netbox/"):
            assert _get_safe_redirect_url(get_request) == "/current/page/"
            assert _get_safe_redirect_url(post_request) == "/netbox/"
            assert _get_safe_redirect_url(request_without_path) == "/netbox/"

    def test_permission_denials_reject_external_referrers_for_html_and_htmx(self):
        reader = _plugin_user("redirect-reader", write=False)
        common = {
            "path": "/post-only/",
            "HTTP_REFERER": "http://malicious.example.test/attack",
        }
        html_view = _permission_view(user=reader, request_kwargs=common)
        htmx_view = _permission_view(
            user=reader,
            request_kwargs={**common, "HTTP_HX_REQUEST": "true"},
        )

        with override_script_prefix("/netbox/"):
            html_response = html_view.require_write_permission()
            htmx_response = htmx_view.require_write_permission()

        assert html_response.url == "/netbox/"
        assert htmx_response["HX-Redirect"] == "/netbox/"


class TestObjectTypeValidation:
    @pytest.mark.parametrize(
        ("object_type", "expected"),
        [
            ("device", ("dcim.Device", "dcim.Interface")),
            ("virtualmachine", ("virtualization.VirtualMachine", "virtualization.VMInterface")),
        ],
    )
    def test_sync_interfaces_declares_owner_read_and_interface_writes(self, object_type, expected):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        permissions = SyncInterfacesView().get_required_permissions_for_object_type(object_type)

        assert [(model._meta.label, action) for action, model in permissions] == [
            (expected[0], "view"),
            (expected[1], "add"),
            (expected[1], "change"),
        ]

    @pytest.mark.parametrize(
        ("object_type", "expected"),
        [
            ("device", ("dcim.Device", "dcim.Interface")),
            ("virtualmachine", ("virtualization.VirtualMachine", "virtualization.VMInterface")),
        ],
    )
    def test_delete_interfaces_declares_owner_read_and_interface_delete(self, object_type, expected):
        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        permissions = DeleteNetBoxInterfacesView().get_required_permissions_for_object_type(object_type)

        assert [(model._meta.label, action) for action, model in permissions] == [
            (expected[0], "view"),
            (expected[1], "delete"),
        ]

    @pytest.mark.parametrize("view_class_name", ["SyncInterfacesView", "DeleteNetBoxInterfacesView"])
    def test_invalid_object_type_is_a_404(self, view_class_name):
        from django.http import Http404

        from netbox_librenms_plugin.views.sync import interfaces

        view = getattr(interfaces, view_class_name)()

        with pytest.raises(Http404):
            view.get_required_permissions_for_object_type("invalid")
