from unittest.mock import MagicMock, patch


class TestLibreNMSPermissionMixin:
    """Tests for permission mixin functionality."""

    def test_has_write_permission_granted(self):
        """User with change permission has write access."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = True

        assert mixin.has_write_permission() is True

    def test_has_write_permission_denied(self):
        """User without change permission lacks write access."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False

        assert mixin.has_write_permission() is False

    def test_require_write_permission_allowed(self):
        """User with write permission gets None (allowed to proceed)."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = True

        result = mixin.require_write_permission()
        assert result is None

    def test_require_write_permission_denied(self):
        """User without write permission gets redirect response to referrer."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False
        mixin.request.path = "/some/path/"
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.headers = {}  # Not an HTMX request

        with patch("netbox_librenms_plugin.views.mixins.redirect") as mock_redirect:
            with patch("netbox_librenms_plugin.views.mixins.messages"):
                result = mixin.require_write_permission()

        mock_redirect.assert_called_once_with("/original/page/")
        assert result is not None

    def test_require_write_permission_denied_htmx(self):
        """HTMX request without write permission gets HX-Redirect response."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False
        mixin.request.path = "/some/path/"
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.headers = {"HX-Request": "true"}

        with patch("netbox_librenms_plugin.views.mixins.messages"):
            result = mixin.require_write_permission()

        # Should return HttpResponse with HX-Redirect header
        assert result is not None
        assert result["HX-Redirect"] == "/original/page/"

    def test_require_write_permission_json_allowed(self):
        """User with write permission gets None (allowed to proceed)."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = True

        result = mixin.require_write_permission_json()
        assert result is None

    def test_require_write_permission_json_denied(self):
        """User without write permission gets JsonResponse with 403."""
        import json
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False

        result = mixin.require_write_permission_json()

        assert result is not None
        assert result.status_code == 403
        content = json.loads(result.content)
        assert content["error"] == "You do not have permission to perform this action."

    def test_require_write_permission_json_custom_message(self):
        """Custom error message is returned in JsonResponse."""
        import json
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False

        result = mixin.require_write_permission_json(error_message="Custom denied message")

        assert result is not None
        assert result.status_code == 403
        content = json.loads(result.content)
        assert content["error"] == "Custom denied message"


class TestAPIPermissions:
    """Tests for API permission class."""

    def test_get_requires_view_permission(self):
        """GET requests require view permission."""
        from netbox_librenms_plugin.api.views import LibreNMSPluginPermission
        from netbox_librenms_plugin.constants import PERM_VIEW_PLUGIN

        permission = LibreNMSPluginPermission()
        request = MagicMock()
        request.method = "GET"
        request.user.has_perm.return_value = True

        assert permission.has_permission(request, None) is True
        request.user.has_perm.assert_called_with(PERM_VIEW_PLUGIN)

    def test_post_requires_change_permission(self):
        """POST requests require change permission."""
        from netbox_librenms_plugin.api.views import LibreNMSPluginPermission
        from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN

        permission = LibreNMSPluginPermission()
        request = MagicMock()
        request.method = "POST"
        request.user.has_perm.return_value = True

        assert permission.has_permission(request, None) is True
        request.user.has_perm.assert_called_with(PERM_CHANGE_PLUGIN)

    def test_put_requires_change_permission(self):
        """PUT requests require change permission."""
        from netbox_librenms_plugin.api.views import LibreNMSPluginPermission
        from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN

        permission = LibreNMSPluginPermission()
        request = MagicMock()
        request.method = "PUT"
        request.user.has_perm.return_value = True

        assert permission.has_permission(request, None) is True
        request.user.has_perm.assert_called_with(PERM_CHANGE_PLUGIN)

    def test_delete_requires_change_permission(self):
        """DELETE requests require change permission."""
        from netbox_librenms_plugin.api.views import LibreNMSPluginPermission
        from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN

        permission = LibreNMSPluginPermission()
        request = MagicMock()
        request.method = "DELETE"
        request.user.has_perm.return_value = True

        assert permission.has_permission(request, None) is True
        request.user.has_perm.assert_called_with(PERM_CHANGE_PLUGIN)

    def test_get_denied_without_view_permission(self):
        """GET requests denied without view permission."""
        from netbox_librenms_plugin.api.views import LibreNMSPluginPermission

        permission = LibreNMSPluginPermission()
        request = MagicMock()
        request.method = "GET"
        request.user.has_perm.return_value = False

        assert permission.has_permission(request, None) is False

    def test_post_denied_without_change_permission(self):
        """POST requests denied without change permission."""
        from netbox_librenms_plugin.api.views import LibreNMSPluginPermission

        permission = LibreNMSPluginPermission()
        request = MagicMock()
        request.method = "POST"
        request.user.has_perm.return_value = False

        assert permission.has_permission(request, None) is False


class TestPermissionConstants:
    """Tests for permission constants."""

    def test_view_permission_constant(self):
        """View permission constant is correct."""
        from netbox_librenms_plugin.constants import PERM_VIEW_PLUGIN

        assert PERM_VIEW_PLUGIN == "netbox_librenms_plugin.view_librenmssettings"

    def test_change_permission_constant(self):
        """Change permission constant is correct."""
        from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN

        assert PERM_CHANGE_PLUGIN == "netbox_librenms_plugin.change_librenmssettings"


# =============================================================================
# Phase 2: Object Permission Tests
# =============================================================================


class TestObjectPermissionHelpers:
    """Tests for Phase 2 object permission helper functions."""

    def test_check_user_permissions_all_granted(self):
        """Returns True when user has all permissions."""
        from netbox_librenms_plugin.import_utils import check_user_permissions

        user = MagicMock()
        user.has_perm.return_value = True

        has_all, missing = check_user_permissions(user, ["dcim.add_device", "dcim.add_interface"])

        assert has_all is True
        assert missing == []
        assert user.has_perm.call_count == 2

    def test_check_user_permissions_some_missing(self):
        """Returns False with list of missing permissions."""
        from netbox_librenms_plugin.import_utils import check_user_permissions

        user = MagicMock()
        user.has_perm.side_effect = lambda p: p != "dcim.add_interface"

        has_all, missing = check_user_permissions(user, ["dcim.add_device", "dcim.add_interface"])

        assert has_all is False
        assert missing == ["dcim.add_interface"]

    def test_check_user_permissions_all_missing(self):
        """Returns False with all permissions listed as missing."""
        from netbox_librenms_plugin.import_utils import check_user_permissions

        user = MagicMock()
        user.has_perm.return_value = False

        has_all, missing = check_user_permissions(user, ["dcim.add_device", "dcim.add_interface"])

        assert has_all is False
        assert "dcim.add_device" in missing
        assert "dcim.add_interface" in missing

    def test_check_user_permissions_no_user(self):
        """Raises PermissionDenied when user is None."""
        import pytest
        from django.core.exceptions import PermissionDenied

        from netbox_librenms_plugin.import_utils import check_user_permissions

        with pytest.raises(PermissionDenied, match="No user context"):
            check_user_permissions(None, ["dcim.add_device"])

    def test_require_permissions_passes_when_granted(self):
        """Does not raise when user has all permissions."""
        from netbox_librenms_plugin.import_utils import require_permissions

        user = MagicMock()
        user.has_perm.return_value = True

        # Should not raise
        require_permissions(user, ["dcim.add_device", "dcim.add_interface"], "import devices")

    def test_require_permissions_raises_on_missing(self):
        """Raises PermissionDenied with descriptive message."""
        import pytest
        from django.core.exceptions import PermissionDenied

        from netbox_librenms_plugin.import_utils import require_permissions

        user = MagicMock()
        user.has_perm.return_value = False

        with pytest.raises(PermissionDenied) as exc_info:
            require_permissions(user, ["dcim.add_device"], "import devices")

        # Check error message contains action description and missing permission
        assert "import devices" in str(exc_info.value)
        assert "dcim.add_device" in str(exc_info.value)

    def test_require_permissions_lists_multiple_missing(self):
        """Error message includes all missing permissions."""
        import pytest
        from django.core.exceptions import PermissionDenied

        from netbox_librenms_plugin.import_utils import require_permissions

        user = MagicMock()
        user.has_perm.return_value = False

        with pytest.raises(PermissionDenied) as exc_info:
            require_permissions(
                user,
                ["dcim.add_device", "dcim.add_interface"],
                "import devices",
            )

        error_msg = str(exc_info.value)
        assert "dcim.add_device" in error_msg
        assert "dcim.add_interface" in error_msg


class TestNetBoxObjectPermissionMixin:
    """Tests for the NetBoxObjectPermissionMixin class."""

    def test_check_object_permissions_all_granted(self):
        """Returns True when user has all object permissions."""
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = True

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("add", mock_model), ("change", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            mock_get.side_effect = ["dcim.add_interface", "dcim.change_interface"]
            has_all, missing = mixin.check_object_permissions("POST")

        assert has_all is True
        assert missing == []

    def test_check_object_permissions_some_missing(self):
        """Returns False with missing permission strings."""
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.side_effect = lambda p: p != "dcim.add_interface"

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("add", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            mock_get.return_value = "dcim.add_interface"
            has_all, missing = mixin.check_object_permissions("POST")

        assert has_all is False
        assert "dcim.add_interface" in missing

    def test_check_object_permissions_no_requirements(self):
        """Returns True when no permissions required for method."""
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()
        mixin.request = MagicMock()
        mixin.required_object_permissions = {}  # No requirements

        has_all, missing = mixin.check_object_permissions("POST")

        assert has_all is True
        assert missing == []

    def test_require_object_permissions_returns_none_when_granted(self):
        """Returns None when all permissions are granted."""
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = True

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("add", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            mock_get.return_value = "dcim.add_cable"
            response = mixin.require_object_permissions("POST")

        assert response is None

    def test_require_object_permissions_returns_redirect_response(self):
        """Returns redirect response with message when permissions missing."""
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.headers = {}  # Not an HTMX request

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("add", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            with patch("netbox_librenms_plugin.views.mixins.messages") as mock_messages:
                with patch("netbox_librenms_plugin.views.mixins.redirect") as mock_redirect:
                    mock_get.return_value = "dcim.add_cable"
                    response = mixin.require_object_permissions("POST")

        assert response is not None
        # Verify error message was added
        mock_messages.error.assert_called_once()
        error_msg = mock_messages.error.call_args[0][1]
        assert "dcim.add_cable" in error_msg
        # Verify redirect was called
        mock_redirect.assert_called_once_with("/original/page/")

    def test_require_object_permissions_htmx_returns_hx_redirect(self):
        """HTMX request returns HX-Redirect header when permissions missing."""
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.headers = {"HX-Request": "true"}

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("add", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            with patch("netbox_librenms_plugin.views.mixins.messages"):
                mock_get.return_value = "dcim.add_cable"
                response = mixin.require_object_permissions("POST")

        assert response is not None
        assert response["HX-Redirect"] == "/original/page/"

    def test_require_object_permissions_json_allowed(self):
        """Returns None when all object permissions are granted."""
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = True

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("delete", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            mock_get.return_value = "dcim.delete_interface"
            response = mixin.require_object_permissions_json("POST")

        assert response is None

    def test_require_object_permissions_json_denied(self):
        """Returns JsonResponse with 403 when object permissions missing."""
        import json

        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("delete", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            mock_get.return_value = "dcim.delete_interface"
            response = mixin.require_object_permissions_json("POST")

        assert response is not None
        assert response.status_code == 403
        content = json.loads(response.content)
        assert "dcim.delete_interface" in content["error"]

    def test_require_all_permissions_allowed(self):
        """Returns None when both write and object permissions granted."""
        from netbox_librenms_plugin.views.mixins import (
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
        )

        class TestView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin):
            pass

        mixin = TestView()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = True

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("change", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            mock_get.return_value = "dcim.change_device"
            response = mixin.require_all_permissions("POST")

        assert response is None

    def test_require_all_permissions_denied_write(self):
        """Returns error when write permission denied (doesn't check object perms)."""
        from netbox_librenms_plugin.views.mixins import (
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
        )

        class TestView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin):
            pass

        mixin = TestView()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.headers = {}

        mixin.required_object_permissions = {"POST": []}

        with patch("netbox_librenms_plugin.views.mixins.redirect") as mock_redirect:
            with patch("netbox_librenms_plugin.views.mixins.messages"):
                response = mixin.require_all_permissions("POST")

        assert response is not None
        mock_redirect.assert_called_once_with("/original/page/")

    def test_require_all_permissions_denied_object(self):
        """Returns error when object permissions denied (write passes)."""
        from netbox_librenms_plugin.views.mixins import (
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
        )

        class TestView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin):
            pass

        mixin = TestView()
        mixin.request = MagicMock()
        # has_write_permission passes, but object perms fail
        mixin.request.user.has_perm.side_effect = lambda p: p == "netbox_librenms_plugin.change_librenmssettings"
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.headers = {}

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("add", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            with patch("netbox_librenms_plugin.views.mixins.messages"):
                with patch("netbox_librenms_plugin.views.mixins.redirect") as mock_redirect:
                    mock_get.return_value = "dcim.add_device"
                    response = mixin.require_all_permissions("POST")

        assert response is not None
        mock_redirect.assert_called_once()

    def test_require_all_permissions_json_allowed(self):
        """Returns None when both write and object permissions granted (JSON variant)."""
        from netbox_librenms_plugin.views.mixins import (
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
        )

        class TestView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin):
            pass

        mixin = TestView()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = True

        mock_model = MagicMock()
        mixin.required_object_permissions = {
            "POST": [("delete", mock_model)],
        }

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            mock_get.return_value = "dcim.delete_interface"
            response = mixin.require_all_permissions_json("POST")

        assert response is None

    def test_require_all_permissions_json_denied_write(self):
        """Returns JSON 403 when write permission denied (JSON variant)."""
        import json

        from netbox_librenms_plugin.views.mixins import (
            LibreNMSPermissionMixin,
            NetBoxObjectPermissionMixin,
        )

        class TestView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin):
            pass

        mixin = TestView()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False

        response = mixin.require_all_permissions_json("POST")

        assert response is not None
        assert response.status_code == 403
        content = json.loads(response.content)
        assert "error" in content


class TestBulkImportPermissions:
    """Tests for permission checks in bulk import functions."""

    @patch("netbox_librenms_plugin.import_utils.require_permissions")
    @patch("netbox_librenms_plugin.import_utils.LibreNMSAPI")
    def test_bulk_import_devices_checks_permissions(self, mock_api_class, mock_require):
        """bulk_import_devices_shared calls require_permissions."""
        from netbox_librenms_plugin.import_utils import bulk_import_devices_shared

        user = MagicMock()
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # Set up API to return empty device so loop completes quickly
        mock_api.get_device_info.return_value = (False, None)

        bulk_import_devices_shared(
            device_ids=[1],
            user=user,
            server_key="default",
        )

        mock_require.assert_called_once()
        call_args = mock_require.call_args
        assert user == call_args[0][0]
        assert "dcim.add_device" in call_args[0][1]
        assert "dcim.add_interface" in call_args[0][1]

    @patch("netbox_librenms_plugin.import_utils.require_permissions")
    @patch("netbox_librenms_plugin.import_utils.LibreNMSAPI")
    def test_bulk_import_devices_extracts_user_from_job(self, mock_api_class, mock_require):
        """bulk_import_devices_shared extracts user from job if not provided."""
        from netbox_librenms_plugin.import_utils import bulk_import_devices_shared

        job_user = MagicMock()
        job = MagicMock()
        job.job.user = job_user

        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.get_device_info.return_value = (False, None)

        bulk_import_devices_shared(
            device_ids=[1],
            job=job,
            server_key="default",
        )

        mock_require.assert_called_once()
        call_args = mock_require.call_args
        assert job_user == call_args[0][0]

    @patch("netbox_librenms_plugin.import_utils.require_permissions")
    def test_bulk_import_vms_checks_permissions(self, mock_require):
        """bulk_import_vms calls require_permissions."""
        from netbox_librenms_plugin.import_utils import bulk_import_vms

        user = MagicMock()
        api = MagicMock()
        api.server_key = "default"

        # Empty vm_imports to complete quickly
        bulk_import_vms(
            vm_imports={},
            api=api,
            user=user,
        )

        mock_require.assert_called_once()
        call_args = mock_require.call_args
        assert user == call_args[0][0]
        assert "virtualization.add_virtualmachine" in call_args[0][1]

    @patch("netbox_librenms_plugin.import_utils.require_permissions")
    def test_bulk_import_vms_extracts_user_from_job(self, mock_require):
        """bulk_import_vms extracts user from job if not provided."""
        from netbox_librenms_plugin.import_utils import bulk_import_vms

        job_user = MagicMock()
        job = MagicMock()
        job.job.user = job_user

        api = MagicMock()
        api.server_key = "default"

        bulk_import_vms(
            vm_imports={},
            api=api,
            job=job,
        )

        mock_require.assert_called_once()
        call_args = mock_require.call_args
        assert job_user == call_args[0][0]


class TestBulkImportPermissionDenied:
    """Tests for permission denied behavior in bulk import."""

    @patch("netbox_librenms_plugin.import_utils.check_user_permissions")
    def test_bulk_import_devices_raises_on_missing_permissions(self, mock_check):
        """bulk_import_devices_shared raises PermissionDenied when permissions missing."""
        import pytest
        from django.core.exceptions import PermissionDenied

        from netbox_librenms_plugin.import_utils import bulk_import_devices_shared

        mock_check.return_value = (False, ["dcim.add_device"])

        user = MagicMock()

        with pytest.raises(PermissionDenied):
            bulk_import_devices_shared(
                device_ids=[1],
                user=user,
                server_key="default",
            )

    @patch("netbox_librenms_plugin.import_utils.check_user_permissions")
    def test_bulk_import_vms_raises_on_missing_permissions(self, mock_check):
        """bulk_import_vms raises PermissionDenied when permissions missing."""
        import pytest
        from django.core.exceptions import PermissionDenied

        from netbox_librenms_plugin.import_utils import bulk_import_vms

        mock_check.return_value = (False, ["virtualization.add_virtualmachine"])

        user = MagicMock()
        api = MagicMock()

        with pytest.raises(PermissionDenied):
            bulk_import_vms(
                vm_imports={1: {"cluster_id": 1}},
                api=api,
                user=user,
            )
