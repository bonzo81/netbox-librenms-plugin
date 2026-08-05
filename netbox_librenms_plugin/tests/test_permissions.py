from unittest.mock import MagicMock, patch

import pytest


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

    def test_has_write_permission_no_request_fails_closed(self):
        """Without self.request (invoked outside dispatch), has_write_permission() returns False instead of raising AttributeError on self.request.user."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()  # no .request assigned
        assert mixin.has_write_permission() is False

    def test_require_write_permission_no_request_returns_403(self):
        """The write-permission denial path must short-circuit to a 403 when there is no request, rather than crash in messages.error()/_safe_redirect_response()."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()  # no .request assigned
        response = mixin.require_write_permission()
        assert response is not None
        assert response.status_code == 403

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
        mixin.request.get_host.return_value = "testserver"
        mixin.request.is_secure.return_value = False
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
        mixin.request.get_host.return_value = "testserver"
        mixin.request.is_secure.return_value = False
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

    def test_require_object_permissions_fails_closed_without_request(self):
        """When the view is invoked outside dispatch() (no self.request), the denial path must return a 403 rather than crash dereferencing self.request."""
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()  # no .request assigned
        mock_model = MagicMock()
        mixin.required_object_permissions = {"POST": [("add", mock_model)]}

        with patch("netbox_librenms_plugin.views.mixins.get_permission_for_model") as mock_get:
            mock_get.return_value = "dcim.add_interface"
            response = mixin.require_object_permissions("POST")

        # check_object_permissions returns the perms as "missing" (fail closed),
        # and the denial handler short-circuits to a 403 instead of raising.
        assert response is not None
        assert response.status_code == 403

    def test_require_object_permissions_returns_redirect_response(self):
        """Returns redirect response with message when permissions missing."""
        from netbox_librenms_plugin.views.mixins import NetBoxObjectPermissionMixin

        mixin = NetBoxObjectPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.user.has_perm.return_value = False
        mixin.request.path = "/original/page/"
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.get_host.return_value = "testserver"
        mixin.request.is_secure.return_value = False
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
        mixin.request.path = "/original/page/"
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.get_host.return_value = "testserver"
        mixin.request.is_secure.return_value = False
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
        mixin.request.path = "/original/page/"
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.get_host.return_value = "testserver"
        mixin.request.is_secure.return_value = False
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
        mixin.request.path = "/original/page/"
        mixin.request.META = {"HTTP_REFERER": "/original/page/"}
        mixin.request.get_host.return_value = "testserver"
        mixin.request.is_secure.return_value = False
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

    def setup_method(self):
        # bulk_import_devices_shared preloads device_type NormalizationRule once (issue #90);
        # these are mock-based (no DB), so stub the preload to avoid real DB access.
        self._norm_patcher = patch(
            "netbox_librenms_plugin.import_utils.bulk_import.preload_normalization_rules",
            return_value={},
        )
        self._norm_patcher.start()

    def teardown_method(self):
        self._norm_patcher.stop()

    @patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions")
    @patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI")
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
        assert "dcim.change_device" in call_args[0][1]
        assert "dcim.add_interface" not in call_args[0][1]

    @patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions")
    @patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI")
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

    @patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions")
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

    @patch("netbox_librenms_plugin.import_utils.vm_operations.require_permissions")
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

    @patch("netbox_librenms_plugin.import_utils.permissions.check_user_permissions")
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

    @patch("netbox_librenms_plugin.import_utils.permissions.check_user_permissions")
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


class TestSafeRedirectUrl:
    """Tests for the _get_safe_redirect_url helper."""

    def test_internal_referrer_is_accepted(self):
        """Internal referrer URL is returned when host matches."""
        from netbox_librenms_plugin.views.mixins import _get_safe_redirect_url

        request = MagicMock()
        request.META = {"HTTP_REFERER": "http://testserver/some/page/"}
        request.get_host.return_value = "testserver"
        request.is_secure.return_value = False
        request.path = "/fallback/"

        result = _get_safe_redirect_url(request)
        assert result == "http://testserver/some/page/"

    def test_external_referrer_is_rejected(self):
        """External referrer URL is rejected; a GET request falls back to request.path."""
        from netbox_librenms_plugin.views.mixins import _get_safe_redirect_url

        request = MagicMock()
        request.method = "GET"
        request.META = {"HTTP_REFERER": "http://evil.com/attack"}
        request.get_host.return_value = "testserver"
        request.is_secure.return_value = False
        request.path = "/safe/fallback/"

        result = _get_safe_redirect_url(request)
        assert result == "/safe/fallback/"

    def test_no_referrer_falls_back_to_path(self):
        """Missing referrer on a GET request falls back to request.path."""
        from netbox_librenms_plugin.views.mixins import _get_safe_redirect_url

        request = MagicMock()
        request.method = "GET"
        request.META = {}
        request.path = "/current/page/"

        result = _get_safe_redirect_url(request)
        assert result == "/current/page/"

    def test_non_get_with_no_referrer_falls_back_to_slash(self):
        """On a non-GET request (request.path is likely POST-only), a missing/rejected referrer falls back to a GET-safe "/" rather than the POST-only path."""
        from netbox_librenms_plugin.views.mixins import _get_safe_redirect_url

        request = MagicMock()
        request.method = "POST"
        request.META = {}
        request.path = "/post/only/action/"

        with patch("netbox_librenms_plugin.views.mixins.get_script_prefix", return_value="/netbox/"):
            result = _get_safe_redirect_url(request)
        assert result == "/netbox/"

    def test_no_referrer_no_path_falls_back_to_slash(self):
        """Missing referrer and no path attribute falls back to the deployment root."""
        from netbox_librenms_plugin.views.mixins import _get_safe_redirect_url

        request = MagicMock(spec=[])  # No attributes at all
        request.META = {}

        with patch("netbox_librenms_plugin.views.mixins.get_script_prefix", return_value="/netbox/"):
            result = _get_safe_redirect_url(request)
        assert result == "/netbox/"

    def test_relative_referrer_is_accepted(self):
        """Relative referrer path is accepted (no host to mismatch)."""
        from netbox_librenms_plugin.views.mixins import _get_safe_redirect_url

        request = MagicMock()
        request.META = {"HTTP_REFERER": "/original/page/"}
        request.get_host.return_value = "testserver"
        request.is_secure.return_value = False
        request.path = "/fallback/"

        result = _get_safe_redirect_url(request)
        assert result == "/original/page/"

    def test_write_permission_denied_rejects_external_referrer(self):
        """Write permission denial with external referrer falls back to a GET-safe "/" (request is a non-GET MagicMock, so request.path — often POST-only — is avoided)."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.method = "POST"  # explicit: exercise the non-GET fallback path on purpose
        mixin.request.user.has_perm.return_value = False
        mixin.request.path = "/safe/page/"
        mixin.request.META = {"HTTP_REFERER": "http://evil.com/steal"}
        mixin.request.get_host.return_value = "testserver"
        mixin.request.is_secure.return_value = False
        mixin.request.headers = {}

        with patch("netbox_librenms_plugin.views.mixins.redirect") as mock_redirect:
            with patch("netbox_librenms_plugin.views.mixins.messages"):
                with patch("netbox_librenms_plugin.views.mixins.get_script_prefix", return_value="/netbox/"):
                    mixin.require_write_permission()

        mock_redirect.assert_called_once_with("/netbox/")

    def test_htmx_rejects_external_referrer(self):
        """HTMX request with external referrer uses a GET-safe "/" fallback in HX-Redirect (non-GET MagicMock request, so the POST-only request.path is avoided)."""
        from netbox_librenms_plugin.views.mixins import LibreNMSPermissionMixin

        mixin = LibreNMSPermissionMixin()
        mixin.request = MagicMock()
        mixin.request.method = "POST"  # explicit: exercise the non-GET fallback path on purpose
        mixin.request.user.has_perm.return_value = False
        mixin.request.path = "/safe/page/"
        mixin.request.META = {"HTTP_REFERER": "http://evil.com/steal"}
        mixin.request.get_host.return_value = "testserver"
        mixin.request.is_secure.return_value = False
        mixin.request.headers = {"HX-Request": "true"}

        with patch("netbox_librenms_plugin.views.mixins.messages"):
            with patch("netbox_librenms_plugin.views.mixins.get_script_prefix", return_value="/netbox/"):
                result = mixin.require_write_permission()

        assert result["HX-Redirect"] == "/netbox/"


class TestBulkImportVCPermission:
    """Tests that bulk import checks virtualchassis permission."""

    def setup_method(self):
        self._norm_patcher = patch(
            "netbox_librenms_plugin.import_utils.bulk_import.preload_normalization_rules",
            return_value={},
        )
        self._norm_patcher.start()

    def teardown_method(self):
        self._norm_patcher.stop()

    @patch("netbox_librenms_plugin.import_utils.bulk_import.require_permissions")
    @patch("netbox_librenms_plugin.import_utils.bulk_import.LibreNMSAPI")
    def test_bulk_import_devices_checks_vc_permission(self, mock_api_class, mock_require):
        """bulk_import_devices_shared includes dcim.add_virtualchassis in required perms."""
        from netbox_librenms_plugin.import_utils import bulk_import_devices_shared

        user = MagicMock()
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api
        mock_api.get_device_info.return_value = (False, None)

        bulk_import_devices_shared(
            device_ids=[1],
            user=user,
            server_key="default",
        )

        mock_require.assert_called_once()
        call_args = mock_require.call_args
        # After Fix 6: interface/VC permissions removed from initial check
        assert "dcim.add_virtualchassis" not in call_args[0][1]
        assert "dcim.add_device" in call_args[0][1]


class TestObjectTypeValidation:
    """Tests that get_required_permissions_for_object_type validates object_type."""

    def test_sync_interfaces_device_type(self):
        """SyncInterfacesView returns correct perms for device type."""
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = SyncInterfacesView()
        perms = view.get_required_permissions_for_object_type("device")
        # The owner read is declared alongside the writes: the device is resolved through a
        # restricted queryset, so a missing view grant is a stated 403, not a 404 at the lookup.
        assert perms == [("view", Device), ("add", Interface), ("change", Interface)]

    def test_sync_interfaces_vm_type(self):
        """SyncInterfacesView returns correct perms for virtualmachine type."""
        from virtualization.models import VirtualMachine, VMInterface

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = SyncInterfacesView()
        perms = view.get_required_permissions_for_object_type("virtualmachine")
        assert perms == [("view", VirtualMachine), ("add", VMInterface), ("change", VMInterface)]

    def test_sync_interfaces_invalid_type_raises_404(self):
        """SyncInterfacesView raises Http404 for invalid object type."""
        import pytest
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = SyncInterfacesView()
        with pytest.raises(Http404):
            view.get_required_permissions_for_object_type("invalid")

    def test_delete_interfaces_device_type(self):
        """DeleteNetBoxInterfacesView returns correct perms for device type."""
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        view = DeleteNetBoxInterfacesView()
        perms = view.get_required_permissions_for_object_type("device")
        assert perms == [("view", Device), ("delete", Interface)]

    def test_delete_interfaces_vm_type(self):
        """DeleteNetBoxInterfacesView returns correct perms for virtualmachine type."""
        from virtualization.models import VirtualMachine, VMInterface

        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        view = DeleteNetBoxInterfacesView()
        perms = view.get_required_permissions_for_object_type("virtualmachine")
        assert perms == [("view", VirtualMachine), ("delete", VMInterface)]

    def test_delete_interfaces_invalid_type_raises_404(self):
        """DeleteNetBoxInterfacesView raises Http404 for invalid object type."""
        import pytest
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.interfaces import DeleteNetBoxInterfacesView

        view = DeleteNetBoxInterfacesView()
        with pytest.raises(Http404):
            view.get_required_permissions_for_object_type("invalid")


# ---------------------------------------------------------------------------
# Tests for RemoveServerMappingView error handling (device_fields.py)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRemoveServerMappingViewErrorHandling:
    """Test RemoveServerMappingView handles full_clean/save failures gracefully."""

    def _make_view(self, device, server_key, post_extra=None):
        """Return a (view, request) pair with permissions satisfied."""
        from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_view
        from netbox_librenms_plugin.views.sync.device_fields import RemoveServerMappingView

        request = make_request("post", {"server_key": server_key, "object_type": "device", **(post_extra or {})})
        return make_view(RemoveServerMappingView, request), request

    @staticmethod
    def _plugins_config(servers):
        from django.test import override_settings

        return override_settings(PLUGINS_CONFIG={"netbox_librenms_plugin": {"servers": servers}})

    def test_validation_error_returns_error_message(self):
        """ValidationError from full_clean leads to error message, not 500."""
        from dcim.models import Device
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import message_texts, post as _post

        dev = make_device("perm-rm-validation", librenms_cf={"orphan-server": 99})
        view, request = self._make_view(dev, "orphan-server")

        # The custom field accepts any dict, so the rejection has to be injected; the manager,
        # the lock and the transaction all stay real.
        with (
            self._plugins_config({}),  # orphan-server NOT configured
            patch.object(Device, "full_clean", side_effect=ValidationError("CF validation failed")),
        ):
            _post(view, request, pk=dev.pk)

        errors = message_texts(request, "error")
        assert len(errors) == 1
        assert "Validation error" in errors[0] or "CF validation failed" in errors[0]
        # Rolled back: the mapping the user tried to remove is still there.
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"orphan-server": 99}

    def test_configured_server_refused(self):
        """Configured server mapping cannot be removed — error message shown."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import message_texts, post as _post

        dev = make_device("perm-rm-configured", librenms_cf={"active-server": 5})
        view, request = self._make_view(dev, "active-server")

        with self._plugins_config({"active-server": {"librenms_url": "http://x"}}):
            _post(view, request, pk=dev.pk)

        errors = message_texts(request, "error")
        assert len(errors) == 1
        assert "Cannot remove" in errors[0]
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"active-server": 5}

    def test_successful_removal_mutates_and_saves(self):
        """Successful removal deletes the key from custom_field_data and saves the device."""
        from dcim.models import Device

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import message_texts, post as _post

        dev = make_device("perm-rm-ok", librenms_cf={"orphan-server": 42, "other-server": 7})
        view, request = self._make_view(dev, "orphan-server")

        with self._plugins_config({}):  # orphan-server NOT configured
            _post(view, request, pk=dev.pk)

        # Assert the exact shape of the persisted value so a misspelled key is caught.
        assert Device.objects.get(pk=dev.pk).custom_field_data["librenms_id"] == {"other-server": 7}
        assert len(message_texts(request, "success")) == 1
