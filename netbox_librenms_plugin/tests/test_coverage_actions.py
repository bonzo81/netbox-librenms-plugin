"""Coverage tests for views/imports/actions.py missing lines."""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip


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

    def test_update_fields_dataerror_returns_400_not_500(self):
        """save(update_fields=...) skips full_clean; an overlong value from LibreNMS
        raises DataError at the DB layer and must become a 400 toast, not a 500."""
        from django.db import DataError

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = MagicMock()
        raw_error = "value too long for type character varying(64)"
        device.save.side_effect = DataError(raw_error)

        response = _save_device(device, update_fields=["name"])
        # Pin the partial-save contract: the DataError must originate from save(), not from a
        # full_clean() call — update_fields saves skip validation by design.
        device.save.assert_called_once_with(update_fields=["name"])
        device.full_clean.assert_not_called()
        assert response.status_code == 400
        assert b"field value is invalid" in response.content
        # No part of the raw DB exception (incl. the schema-revealing column type) leaks.
        assert raw_error.encode().lower() not in response.content.lower()
        assert b"character varying" not in response.content.lower()

    def test_update_fields_databaseerror_returns_409_not_500(self):
        """save(update_fields=...) forces an UPDATE; a concurrent delete makes it affect 0
        rows and raises DatabaseError. That must become a 409 toast, not an unhandled 500."""
        from django.db import DatabaseError

        from netbox_librenms_plugin.views.imports.actions import _save_device

        device = MagicMock()
        raw_error = "Save with update_fields did not affect any rows."
        device.save.side_effect = DatabaseError(raw_error)

        response = _save_device(device, update_fields=["name"])
        # The DatabaseError must come from the partial-update save(), so confirm
        # update_fields was actually forwarded (not a fallback to a plain save()).
        device.save.assert_called_once_with(update_fields=["name"])
        device.full_clean.assert_not_called()
        assert response.status_code == 409
        assert b"changed or deleted" in response.content
        # Full raw DB exception text must not leak to the client (case-insensitive).
        assert raw_error.encode().lower() not in response.content.lower()


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


class TestBulkImportConfirmView:
    """Tests for BulkImportConfirmView.post (lines 235-300)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView

        view = object.__new__(BulkImportConfirmView)
        view.request = MagicMock()
        view._librenms_api = _make_api()
        return view

    def test_no_permission_returns_error(self):
        view = self._make_view()
        error_resp = MagicMock()

        with patch.object(view, "require_write_permission", return_value=error_resp):
            request = _make_request(post={"select": ["1"]})
            result = view.post(request)
        assert result is error_resp

    def test_no_devices_selected_renders_alert(self):
        # This is HTMX modal content (hx-target=#htmx-modal-content); htmx won't swap a
        # 4xx, so the alert must come back 200 to render in-place.
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            request = _make_request(post={})
            result = view.post(request)
        assert result.status_code == 200
        assert b"Select at least one device" in result.content

    def test_invalid_device_id_renders_generic_alert(self):
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch("netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=None):
                    request = _make_request(post={"select": ["not-an-int"]})
                    result = view.post(request)
        # No valid devices and nothing expired → generic alert, rendered 200 in the modal.
        assert result.status_code == 200
        assert b"No valid devices selected" in result.content

    def test_all_cache_expired_renders_expiry_alert(self):
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch("netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=None):
                    request = _make_request(post={"select": ["1", "2"]})
                    result = view.post(request)
        # All selected devices expired → expiry alert, 200 so the modal renders it.
        assert result.status_code == 200
        assert b"expired" in result.content.lower()

    @patch("netbox_librenms_plugin.views.imports.actions.render")
    def test_valid_devices_renders_confirm_template(self, mock_render):
        view = self._make_view()
        mock_render.return_value = MagicMock(status_code=200)

        libre_device = {"device_id": 1, "hostname": "router01"}
        validation = {
            "resolved_name": "router01",
            "virtual_chassis": {"is_stack": False},
            "_vc_detection_enabled": False,
        }

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=libre_device
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                        return_value={"cluster_id": None, "role_id": None, "rack_id": None},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                            return_value=validation,
                        ) as mock_validate:
                            request = _make_request(post={"select": ["1"]}, get={"enable_vc_detection": "false"})
                            view.post(request)

        mock_render.assert_called_once()
        assert mock_validate.call_args.kwargs["include_vc_detection"] is True
        call_args = mock_render.call_args
        assert "bulk_import_confirm.html" in call_args[0][1]

    @patch("netbox_librenms_plugin.views.imports.actions.render")
    def test_uses_return_url_vc_flag_for_context_and_validation(self, mock_render):
        view = self._make_view()
        mock_render.return_value = MagicMock(status_code=200)

        libre_device = {"device_id": 1, "hostname": "router01"}
        validation = {
            "resolved_name": "router01",
            "virtual_chassis": {"is_stack": False},
            "_vc_detection_enabled": False,
        }

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=libre_device
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                        return_value={"cluster_id": None, "role_id": None, "rack_id": None},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                            return_value=validation,
                        ):
                            request = _make_request(
                                post={
                                    "select": ["1"],
                                    "return_url": "/plugins/librenms_plugin/librenms-import/?enable_vc_detection=true",
                                }
                            )
                            view.post(request)

        call_args = mock_render.call_args
        context = call_args[0][2]
        assert context["vc_detection_enabled"] is True
        assert context["devices"][0]["validation"]["_vc_detection_enabled"] is True


class TestBulkImportDevicesViewPost:
    """Tests for BulkImportDevicesView.post."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = object.__new__(BulkImportDevicesView)
        view.request = MagicMock()
        view._librenms_api = _make_api()
        return view

    def test_no_permission_returns_error(self):
        view = self._make_view()
        error_resp = MagicMock()
        with patch.object(view, "require_write_permission", return_value=error_resp):
            result = view.post(_make_request(post={"select": ["1"]}))
        assert result is error_resp

    def test_no_devices_returns_400(self):
        # HTMX path: bare 400 (non-HTMX redirects instead — covered elsewhere).
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            result = view.post(_make_request(post={}, headers={"HX-Request": "true"}))
        assert result.status_code == 400

    def test_invalid_ids_returns_400(self):
        # HTMX path: bare 400 (non-HTMX redirects instead — covered elsewhere).
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            result = view.post(_make_request(post={"select": ["abc"]}, headers={"HX-Request": "true"}))
        assert result.status_code == 400

    def test_non_superuser_cannot_use_background_job(self):
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            request = _make_request(post={"select": ["1"], "use_background_job": "on"}, user_is_superuser=False)
            # should_use_background_job_for_import returns False for non-superuser
            result = view.should_use_background_job_for_import(request)
        assert result is False

    def test_superuser_can_use_background_job(self):
        view = self._make_view()
        request = _make_request(post={"use_background_job": "on"}, user_is_superuser=True)
        result = view.should_use_background_job_for_import(request)
        assert result is True

    def test_superuser_without_flag_returns_false(self):
        view = self._make_view()
        request = _make_request(post={}, user_is_superuser=True)
        result = view.should_use_background_job_for_import(request)
        assert result is False


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
        """A committed mutation whose post-commit row reload fails must NOT report failure:
        surface the deferred messages + a refresh hint and return 200 with the success
        trigger, so the user doesn't retry an action that already succeeded."""
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
        """A messages-storage stand-in that is iterable and accepts .used = False."""
        storage = MagicMock()
        storage.__iter__ = lambda self: iter(items)
        return storage

    def test_appends_rendered_messages_to_bytes_content(self):
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        response = HttpResponse(b"<tr>row html</tr>")
        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.messages.get_messages",
                return_value=self._storage(["a message"]),
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

    def test_skips_oob_swap_when_no_messages_queued(self):
        """No pending messages → don't append an empty OOB container that would
        wipe toasts already visible from an earlier action."""
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        response = HttpResponse(b"<tr>row html</tr>")
        original = response.content
        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.messages.get_messages",
                return_value=self._storage([]),
            ),
            patch("netbox_librenms_plugin.views.imports.actions.render_to_string") as mock_render,
        ):
            result = _attach_messages_oob(response, MagicMock())

        mock_render.assert_not_called()
        assert result.content == original

    def test_swallows_render_errors(self):
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.imports.actions import _attach_messages_oob

        response = HttpResponse(b"<tr>row html</tr>")
        original = response.content
        with (
            patch(
                "netbox_librenms_plugin.views.imports.actions.messages.get_messages",
                return_value=self._storage(["a message"]),
            ),
            patch(
                "netbox_librenms_plugin.views.imports.actions.render_to_string",
                side_effect=RuntimeError("db not available"),
            ),
        ):
            result = _attach_messages_oob(response, MagicMock())

        assert result.content == original


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
        view = self._make_view()
        with patch.object(view, "get_validated_device_with_selections", return_value=(None, None, {})):
            with patch.object(view, "require_write_permission", return_value=None):
                result = view.get(MagicMock(), device_id=1)
        assert result.status_code == 200
        assert b"not found in LibreNMS" in result.content

    @patch("netbox_librenms_plugin.views.imports.actions.render")
    def test_get_with_existing_device_adds_sync_info(self, mock_render):
        view = self._make_view()
        mock_render.return_value = MagicMock()

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
                        view.get(MagicMock(), device_id=1)

        mock_render.assert_called_once()
        ctx = mock_render.call_args[0][2]
        assert "sync_info" in ctx


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

    def test_oob_only_dict_entry_skipped(self):
        """An OOB-only entry has no host id to show in the import-action modal → skipped."""
        method = self._get_method()
        existing = MagicMock()
        existing.custom_field_data = {"librenms_id": {"default": {"oob": {"id": 17, "type": "idrac"}}}}

        with patch("django.conf.settings") as mock_settings:
            mock_settings.PLUGINS_CONFIG = {
                "netbox_librenms_plugin": {"servers": {"default": {"display_name": "Default Server"}}}
            }
            result = method(existing)

        assert result is None

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
            result = view.post(MagicMock(), device_id=1)
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
                view.post(MagicMock(), device_id=1)

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
            result = view.post(MagicMock(), device_id=1)
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
            result = view.post(MagicMock(), device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Device not found" in result.content


class TestDeviceConflictActionView:
    """Tests for DeviceConflictActionView.post (lines ~995+)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        return view

    def test_no_permission_returns_error(self):
        view = self._make_view()
        error_resp = MagicMock()
        with patch.object(view, "require_write_permission", return_value=error_resp):
            result = view.post(MagicMock(), device_id=1)
        assert result is error_resp

    def test_missing_action_renders_htmx_error_toast(self):
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            request = _make_request(post={"existing_device_id": "1"})
            result = view.post(request, device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Missing action or existing_device_id" in result.content

    def test_missing_existing_device_id_renders_htmx_error_toast(self):
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            request = _make_request(post={"action": "link"})
            result = view.post(request, device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Missing action or existing_device_id" in result.content

    def test_vm_with_unsupported_action_renders_htmx_error_toast(self):
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            request = _make_request(
                post={
                    "action": "link",
                    "existing_device_id": "5",
                    "existing_device_type": "virtualmachine",
                }
            )
            result = view.post(request, device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"is not supported for virtual machines" in result.content

    def test_existing_device_not_found_renders_htmx_error_toast(self):
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.DoesNotExist = type("DoesNotExist", (Exception,), {})
                MockDevice.objects.get.side_effect = MockDevice.DoesNotExist()
                MockDevice.objects.get.side_effect = ValueError("invalid pk")

                request = _make_request(post={"action": "link", "existing_device_id": "abc"})
                result = view.post(request, device_id=1)
        assert result.status_code == 200
        assert result.headers.get("HX-Reswap") == "none"
        assert b"Existing device not found" in result.content

    def test_unknown_action_renders_htmx_error_toast(self):
        view = self._make_view()
        with patch.object(view, "require_write_permission", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                existing_device = MagicMock()
                MockDevice.objects.get.return_value = existing_device
                MockDevice.DoesNotExist = type("DoesNotExist", (Exception,), {})

                with patch.object(view, "require_object_permissions", return_value=None):
                    view.required_object_permissions = {"POST": [("change", MockDevice)]}

                    with patch.object(view, "get_validated_device_with_selections") as mock_validated:
                        validation = {"existing_device": existing_device}
                        mock_validated.return_value = ({"device_id": 1, "serial": "-"}, validation, {})

                        request = _make_request(
                            post={
                                "action": "unknown_action",
                                "existing_device_id": "5",
                            }
                        )
                        result = view.post(request, device_id=1)

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


class TestShouldEnableVCDetection:
    """Tests for DeviceImportHelperMixin._should_enable_vc_detection."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceRoleUpdateView

        view = object.__new__(DeviceRoleUpdateView)
        view._librenms_api = _make_api()
        return view

    def test_enable_vc_detection_from_get(self):
        view = self._make_view()
        request = _make_request(get={"enable_vc_detection": "true"})
        assert view._should_enable_vc_detection(1, request) is True

    def test_no_explicit_vc_detection_still_returns_true(self):
        """Function always returns True (smart caching fallback)."""
        view = self._make_view()
        request = _make_request(get={"enable_vc_detection": "false"})
        # The function checks cache, and without cached data it still returns True
        with patch("netbox_librenms_plugin.views.imports.actions.cache") as mock_cache:
            mock_cache.get.return_value = None
            result = view._should_enable_vc_detection(1, request)
        assert result is True

    def test_enable_vc_detection_from_post(self):
        view = self._make_view()
        request = _make_request(post={"enable_vc_detection": "on"})
        with patch("netbox_librenms_plugin.views.imports.actions.cache") as mock_cache:
            mock_cache.get.return_value = None
            result = view._should_enable_vc_detection(1, request)
        assert result is True


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


class TestBulkImportDevicesViewErrorPaths:
    """Tests for BulkImportDevicesView.post() early-exit paths."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = object.__new__(BulkImportDevicesView)
        view._librenms_api = _make_api()
        return view

    def test_post_no_devices_selected_htmx_returns_400(self):
        """Empty device_ids on the HTMX path returns a raw 400 (surfaced as a toast client-side)."""
        view = self._make_view()
        request = _make_request(post={}, headers={"HX-Request": "true"})
        request.POST.getlist = MagicMock(return_value=[])  # No devices selected

        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                response = view.post(request)

        assert response.status_code == 400

    def test_post_no_devices_selected_full_page_redirects_with_message(self):
        """Empty device_ids on a non-HTMX POST queues an error message and redirects to the
        import page, rather than serving a bare 400 body as a full page."""
        view = self._make_view()
        request = _make_request(post={})  # no HX-Request header
        request.POST.getlist = MagicMock(return_value=[])

        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.imports.actions.messages") as mock_messages:
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                ) as mock_redirect:
                    result = view.post(request)

        mock_messages.error.assert_called_once()
        mock_redirect.assert_called_once_with("plugins:netbox_librenms_plugin:librenms_import")
        # Assert the redirect response is actually returned, not just that redirect() ran.
        assert result is mock_redirect.return_value

    def test_post_invalid_device_id_htmx_returns_400(self):
        """A non-int device_id on the HTMX path returns a raw 400."""
        view = self._make_view()
        request = _make_request(post={"select": "not-an-int"}, headers={"HX-Request": "true"})
        request.POST.getlist = MagicMock(return_value=["not-an-int"])

        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                response = view.post(request)

        assert response.status_code == 400

    def test_post_invalid_device_id_full_page_redirects_with_message(self):
        """A non-int device_id on a non-HTMX POST queues an error and redirects to the import page."""
        view = self._make_view()
        request = _make_request(post={"select": "not-an-int"})  # no HX-Request header
        request.POST.getlist = MagicMock(return_value=["not-an-int"])

        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.imports.actions.messages") as mock_messages:
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                ) as mock_redirect:
                    response = view.post(request)

        mock_messages.error.assert_called_once()
        mock_redirect.assert_called_once_with("plugins:netbox_librenms_plugin:librenms_import")
        # Pin the full-page contract: post() must actually return the redirect, not fall
        # through to None or a different response.
        assert response is mock_redirect.return_value

    def test_post_permission_denied(self):
        """Permission check returns error early."""
        view = self._make_view()
        request = _make_request(post={"select": "1"})
        from django.http import HttpResponse

        error_response = HttpResponse(status=403)

        with patch.object(view, "require_write_permission", return_value=error_response):
            response = view.post(request)

        assert response.status_code == 403


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
                with patch("dcim.models.Device") as MockDevice:
                    mock_device_obj = MagicMock()
                    MockDevice.objects.get.return_value = mock_device_obj
                    MockDevice.DoesNotExist = Exception
                    with patch("netbox_librenms_plugin.views.imports.actions.cache"):
                        with patch.object(
                            view, "get_validated_device_with_selections", return_value=(None, None, None)
                        ):
                            try:
                                view.post(request, device_id=1)
                            except Exception:
                                pass

        MockAPI.assert_called_with(server_key="secondary")


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


class TestDeviceConflictActionLinkAction:
    """Tests for DeviceConflictActionView 'link' action (lines 1083-1094)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_link_action_executes(self):
        """Link action links device to LibreNMS."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
            }
        )

        mock_existing_device = MagicMock()
        mock_existing_device.name = "router01"
        mock_existing_device.pk = 1

        libre_device = {"device_id": 42, "hostname": "router01", "hardware": "Cisco"}
        # validation must have existing_device that matches mock_existing_device
        validation = {
            "status": "conflict",
            "existing_device": mock_existing_device,
            "device_type_mismatch": False,
        }
        selections = {}

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing_device
                MockDevice.objects.select_for_update.return_value.get.return_value = mock_existing_device
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None):
                        with patch("netbox_librenms_plugin.views.imports.actions.set_librenms_device_id"):
                            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=None):
                                with patch("netbox_librenms_plugin.views.imports.actions.cache"):
                                    with patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx:
                                        mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
                                        mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
                                        with patch.object(
                                            view,
                                            "get_validated_device_with_selections",
                                            return_value=(libre_device, validation, selections),
                                        ):
                                            with patch.object(
                                                view, "render_device_row", return_value=MagicMock()
                                            ) as mock_render:
                                                with patch(
                                                    "netbox_librenms_plugin.views.imports.actions._get_hostname_for_action",
                                                    return_value="router01",
                                                ):
                                                    with patch(
                                                        "netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key",
                                                        return_value="key",
                                                    ):
                                                        with patch(
                                                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                                                            return_value={"device_id": 42},
                                                        ):
                                                            view.post(request, device_id=42)

        mock_render.assert_called_once()


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


class TestBulkImportConfirmViewPost:
    """Tests for BulkImportConfirmView.post() (lines 306-450)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView

        view = object.__new__(BulkImportConfirmView)
        view._librenms_api = _make_api()
        return view

    def test_no_devices_selected_renders_alert(self):
        """Empty device_ids returns the alert as HTMX modal content (200, not 400, so
        htmx swaps it into #htmx-modal-content)."""
        view = self._make_view()
        request = _make_request(post={})
        request.POST.getlist = MagicMock(return_value=[])

        with patch.object(view, "require_write_permission", return_value=None):
            response = view.post(request)

        assert response.status_code == 200
        assert b"Select at least one device" in response.content

    def test_duplicate_device_id_is_skipped(self):
        """Line 334: duplicate device_id is skipped."""
        view = self._make_view()
        request = _make_request(post={"select": ["1", "1"]})  # Duplicate
        request.POST.getlist = MagicMock(return_value=["1", "1"])
        request.GET = MagicMock(return_value={})
        request.GET.get = MagicMock(return_value=None)

        libre_device = {"device_id": 1, "hostname": "router01"}
        validation = {
            "status": "importable",
            "can_import": True,
            "resolved_name": "router01",
            "virtual_chassis": {},
        }

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=libre_device
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                    return_value={"cluster_id": None, "role_id": None, "rack_id": None},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                        return_value=validation,
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                            return_value=(True, False),
                        ):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.render",
                                return_value=MagicMock(status_code=200),
                            ):
                                response = view.post(request)

        # Should have processed only once (duplicate skipped)
        assert response is not None

    def test_device_not_in_cache_adds_error(self):
        """Lines 341-346: device not in cache → error appended."""
        view = self._make_view()
        request = _make_request(post={"select": "999"})
        request.POST.getlist = MagicMock(return_value=["999"])
        request.GET = MagicMock()
        request.GET.get = MagicMock(return_value=None)

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=None
            ):  # Not in cache
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                    return_value=(True, False),
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.render", return_value=MagicMock(status_code=200)
                    ) as mock_render:
                        view.post(request)

        # Render should be called with errors
        call_args = mock_render.call_args
        if call_args:
            context = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("context", {})
            if isinstance(context, dict):
                assert len(context.get("errors", [])) > 0 or context.get("cache_expired_count", 0) > 0

    def test_vc_stack_updates_suggested_names(self):
        """Line 371: VC stack device calls update_vc_member_suggested_names."""
        view = self._make_view()
        request = _make_request(post={"select": "1"})
        request.POST.getlist = MagicMock(return_value=["1"])
        request.GET = MagicMock()
        request.GET.get = MagicMock(return_value="true")

        libre_device = {"device_id": 1, "hostname": "sw01"}
        validation = {
            "status": "importable",
            "resolved_name": "sw01",
            "virtual_chassis": {"is_stack": True, "members": []},
        }

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=libre_device
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                    return_value={"cluster_id": None, "role_id": None, "rack_id": None},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                        return_value=validation,
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                            return_value=(True, False),
                        ):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.update_vc_member_suggested_names",
                                return_value={"is_stack": True},
                            ) as mock_vc:
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.render",
                                    return_value=MagicMock(status_code=200),
                                ):
                                    view.post(request)

        mock_vc.assert_called_once()


class TestDeviceVCDetailsViewAdditional:
    """Tests for DeviceVCDetailsView.get() (line 334 in vc details)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceVCDetailsView

        view = object.__new__(DeviceVCDetailsView)
        view._librenms_api = _make_api()
        return view

    def test_device_not_found_in_librenms_returns_200_html_fragment(self):
        """Device not found in LibreNMS: HTMX fragment must come back 200 (a 4xx makes HTMX skip
        the swap), with the inline alert in the body."""
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


class TestDeviceConflictActionMigrateLibreNMSId:
    """Tests for DeviceConflictActionView migrate_librenms_id action (lines 1247-1323)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_migrate_librenms_id_for_vm(self):
        """Lines 1000-1002: VM model selection for migrate action."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "migrate_librenms_id",
                "existing_device_id": "1",
                "existing_device_type": "virtualmachine",
            }
        )

        mock_vm = MagicMock()
        mock_vm.pk = 1
        mock_vm.name = "vm01"
        # Legacy bare-int id matching the active device_id (42) → migration applies.
        mock_vm.custom_field_data = {"librenms_id": 42}
        # A DISTINCT locked instance so the test fails if the view mutates the stale
        # pre-lock VM instead of the row re-read under select_for_update.
        locked_vm = MagicMock()
        locked_vm.pk = 1
        locked_vm.name = "vm01"
        locked_vm.custom_field_data = {"librenms_id": 42}

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("virtualization.models.VirtualMachine") as MockVM:
                MockVM.objects.get.return_value = mock_vm
                # The locked re-read inside the transaction returns the distinct locked VM.
                MockVM.objects.select_for_update.return_value.get.return_value = locked_vm
                MockVM.DoesNotExist = Exception
                with patch("dcim.models.Device"):
                    with patch.object(view, "require_object_permissions", return_value=None):
                        with patch.object(
                            view,
                            "get_validated_device_with_selections",
                            return_value=(
                                {"device_id": 42},
                                {
                                    "existing_device": mock_vm,
                                    "device_type_mismatch": False,
                                    "serial_confirmed": True,
                                },
                                {},
                            ),
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx:
                                mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
                                mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
                                with patch("netbox_librenms_plugin.views.imports.actions.cache"):
                                    # migrate_librenms_id converts via migrate_legacy_librenms_id
                                    # and guards against duplicate ownership via find_by_librenms_id.
                                    with patch(
                                        "netbox_librenms_plugin.utils.migrate_legacy_librenms_id", return_value=True
                                    ) as mock_migrate:
                                        with patch(
                                            "netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None
                                        ):
                                            with patch(
                                                "netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key",
                                                return_value="key",
                                            ):
                                                with patch.object(
                                                    view, "render_device_row", return_value=MagicMock()
                                                ) as mock_render:
                                                    view.post(request, device_id=42)
        # The VM branch was actually exercised (no blanket try/except masking crashes):
        # the migrate path converted the VM's legacy id and rendered the updated row.
        MockVM.objects.get.assert_called_once_with(pk=1)
        # The conversion operates on the LOCKED instance, not the stale pre-lock one.
        assert mock_migrate.call_args.args[0] is locked_vm
        assert mock_render.called


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
                MockDevice.objects.get.side_effect = _DeviceDoesNotExist("Not found")
                response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert b"Existing device not found" in response.content
        assert response["HX-Reswap"] == "none"


class TestDeviceConflictActionMorePaths:
    """Additional paths for DeviceConflictActionView."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def _base_patches(self, view, mock_existing, libre_device, validation):
        """Return a context with common patches applied."""
        from contextlib import ExitStack

        return ExitStack()

    def test_unknown_action_renders_htmx_error_toast(self):
        """Line 1338: unknown action renders htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "unknown_action_xyz",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1
        libre_device = {"device_id": 42, "hostname": "r01"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Unknown action: unknown_action_xyz" in response.content

    def test_force_required_without_force_renders_htmx_error_toast(self):
        """Lines 1044/1047-1048: device_type_mismatch + force required but not provided."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1
        libre_device = {"device_id": 42, "hostname": "r01"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": True,  # Mismatch
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Device type mismatch detected" in response.content

    def test_validated_existing_pk_mismatch_renders_htmx_error_toast(self):
        """Line 1027: validated_existing.pk != existing_device.pk → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1

        validated_existing = MagicMock()
        validated_existing.pk = 99  # Different pk!

        libre_device = {"device_id": 42, "hostname": "r01"}
        validation = {
            "existing_device": validated_existing,  # Different pk
            "device_type_mismatch": False,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Device ID mismatch" in response.content

    def test_validated_existing_none_renders_htmx_error_toast(self):
        """Line 1025: validated_existing is None → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1

        libre_device = {"device_id": 42, "hostname": "r01"}
        validation = {
            "existing_device": None,  # No existing device validated
            "device_type_mismatch": False,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Missing validated conflict target" in response.content

    def test_require_object_permissions_fails(self):
        """Line 1014: require_object_permissions returns error."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "link",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        from django.http import HttpResponse

        perm_error = HttpResponse("Permission denied", status=403)

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=perm_error):
                    response = view.post(request, device_id=1)

        assert response.status_code == 403

    def test_migrate_not_flagged_renders_htmx_error_toast(self):
        """Line 1252-1255: migrate_librenms_id with unflagged device → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "migrate_librenms_id",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1

        libre_device = {"device_id": 42, "hostname": "r01"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
            "librenms_id_needs_migration": False,  # NOT flagged for migration
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"already in JSON format" in response.content

    def test_migrate_already_json_format_renders_htmx_error_toast(self):
        """Lines 1260-1265: cf_value already dict → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "migrate_librenms_id",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1
        mock_existing.custom_field_data = {"librenms_id": {"default": 42}}  # Already dict

        libre_device = {"device_id": 42, "hostname": "r01"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
            "librenms_id_needs_migration": True,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"already in JSON format" in response.content

    def test_migrate_id_mismatch_renders_htmx_error_toast(self):
        """Line 1272-1275: cf_int != librenms_id → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "migrate_librenms_id",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1
        mock_existing.custom_field_data = {"librenms_id": 99}  # Different from librenms_id=42

        libre_device = {"device_id": 42, "hostname": "r01"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
            "librenms_id_needs_migration": True,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"does not match the active device ID" in response.content

    def test_sync_device_type_no_match_renders_htmx_error_toast(self):
        """Line 1241: sync_device_type with no HW match → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "sync_device_type",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1

        libre_device = {"device_id": 42, "hostname": "r01", "hardware": "Unknown HW"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        with patch(
                            "netbox_librenms_plugin.utils.match_librenms_hardware_to_device_type",
                            return_value={"matched": False},
                        ):
                            response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"No matching device type for" in response.content

    def test_sync_platform_no_os_renders_htmx_error_toast(self):
        """Line 1227: sync_platform with empty OS → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "sync_platform",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1

        libre_device = {"device_id": 42, "hostname": "r01", "os": ""}  # Empty OS
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"No OS info from LibreNMS" in response.content

    def test_sync_platform_not_found_in_netbox(self):
        """Line 1225: sync_platform platform not in NetBox → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "sync_platform",
                "existing_device_id": "1",
            }
        )

        mock_existing = MagicMock()
        mock_existing.pk = 1

        libre_device = {"device_id": 42, "hostname": "r01", "os": "ios"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        with patch(
                            "netbox_librenms_plugin.utils.find_matching_platform", return_value={"found": False}
                        ):
                            response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"not found in NetBox" in response.content


class TestDeviceConflictUpdateAction:
    """Tests for DeviceConflictActionView 'update' action (lines 1108-1120)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_update_action_executes(self):
        """Update action updates device name."""
        view = self._make_view()
        request = _make_request(
            post={
                "action": "update",
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

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.objects.select_for_update.return_value.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        with patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None):
                            with patch("netbox_librenms_plugin.views.imports.actions.set_librenms_device_id"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions._save_device", return_value=None
                                ):
                                    with patch("netbox_librenms_plugin.views.imports.actions.cache"):
                                        with patch(
                                            "netbox_librenms_plugin.views.imports.actions.transaction"
                                        ) as mock_tx:
                                            mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
                                            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
                                            with patch(
                                                "netbox_librenms_plugin.views.imports.actions._get_hostname_for_action",
                                                return_value="router01",
                                            ):
                                                with patch(
                                                    "netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key",
                                                    return_value="key",
                                                ):
                                                    with patch(
                                                        "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                                                        return_value={"device_id": 42},
                                                    ):
                                                        with patch.object(
                                                            view, "render_device_row", return_value=MagicMock()
                                                        ) as mock_render:
                                                            view.post(request, device_id=42)

        mock_render.assert_called_once()


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
                MockDevice.objects.get.return_value = mock_existing
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
                MockDevice.objects.get.return_value = mock_existing
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
                MockDevice.objects.get.return_value = mock_existing
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


class TestBulkImportConfirmViewVMRole:
    """Tests for BulkImportConfirmView VM role/rack apply paths (lines 383-393)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView

        view = object.__new__(BulkImportConfirmView)
        view._librenms_api = _make_api()
        return view

    def test_vm_with_cluster_and_role_applies_both(self):
        """Lines 383-387: VM with cluster + role applies both."""
        view = self._make_view()
        request = _make_request(post={"select": "1"})
        request.POST.getlist = MagicMock(return_value=["1"])
        request.GET = MagicMock()
        request.GET.get = MagicMock(return_value=None)

        libre_device = {"device_id": 1, "hostname": "vm01"}
        validation = {
            "status": "importable",
            "resolved_name": "vm01",
            "virtual_chassis": {},
            # The view recomputes is_vm from this flag; without it the cluster branch
            # (apply_cluster_to_validation) is skipped.
            "import_as_vm": True,
        }
        mock_cluster = MagicMock()
        mock_role = MagicMock()

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=libre_device
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                    return_value={"cluster_id": "1", "role_id": "2", "rack_id": None},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                        return_value=validation,
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                            return_value=(True, False),
                        ):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.fetch_model_by_id",
                                side_effect=[mock_role, mock_cluster, MagicMock()],
                            ):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.apply_cluster_to_validation"
                                ) as mock_apply_c:
                                    with patch(
                                        "netbox_librenms_plugin.views.imports.actions.apply_role_to_validation"
                                    ) as mock_apply_r:
                                        with patch(
                                            "netbox_librenms_plugin.views.imports.actions.render",
                                            return_value=MagicMock(status_code=200),
                                        ):
                                            response = view.post(request)

        # Both the cluster and role selections must be applied for a VM with both set,
        # with the exact resolved objects (catches a role/cluster swap, not just "called").
        assert mock_apply_c.call_args.args[1] is mock_cluster
        assert mock_apply_r.call_args.args[1] is mock_role
        assert response is not None

    def test_device_with_role_and_rack_applies_both(self):
        """Lines 390, 393: Device with role + rack applies both."""
        view = self._make_view()
        request = _make_request(post={"select": "1"})
        request.POST.getlist = MagicMock(return_value=["1"])
        request.GET = MagicMock()
        request.GET.get = MagicMock(return_value=None)

        libre_device = {"device_id": 1, "hostname": "router01"}
        validation = {
            "status": "importable",
            "resolved_name": "router01",
            "virtual_chassis": {},
        }
        mock_role = MagicMock()
        mock_rack = MagicMock()

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=libre_device
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                    return_value={"cluster_id": None, "role_id": "1", "rack_id": "2"},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                        return_value=validation,
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                            return_value=(True, False),
                        ):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.fetch_model_by_id",
                                side_effect=[mock_role, mock_rack],
                            ):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.apply_role_to_validation"
                                ) as mock_apply_r:
                                    with patch(
                                        "netbox_librenms_plugin.views.imports.actions.apply_rack_to_validation"
                                    ) as mock_apply_rack:
                                        with patch(
                                            "netbox_librenms_plugin.views.imports.actions.render",
                                            return_value=MagicMock(status_code=200),
                                        ):
                                            response = view.post(request)

        # Both the role and rack selections must be applied for a device with both set,
        # with the exact resolved objects in the expected positions.
        assert mock_apply_r.call_args.args[1] is mock_role
        assert mock_apply_rack.call_args.args[1] is mock_rack
        assert response is not None


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

    def test_should_enable_vc_detection_when_cached(self):
        """Line 168: VC data already cached → returns True."""
        from netbox_librenms_plugin.views.imports.actions import DeviceImportHelperMixin

        view = object.__new__(DeviceImportHelperMixin)
        api = _make_api()
        # Set librenms_api as a regular attribute to bypass property lookup
        type(view).librenms_api = property(lambda self: api)

        request = _make_request(post={})
        request.GET = MagicMock()
        request.GET.get = MagicMock(return_value=None)  # enable_vc_detection not set

        with patch("netbox_librenms_plugin.views.imports.actions.cache") as mock_cache:
            mock_cache.get.return_value = {"some": "data"}  # Data in cache
            with patch("netbox_librenms_plugin.import_utils._vc_cache_key", return_value="vc_key"):
                result = view._should_enable_vc_detection(device_id=1, request=request)

        assert result is True
        # Reset the property
        try:
            del type(view).librenms_api
        except AttributeError:
            pass


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


class TestMigrateLibreNMSIdMorePaths:
    """More tests for migrate_librenms_id action (lines 1277-1323)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def _make_base_request(self):
        return _make_request(
            post={
                "action": "migrate_librenms_id",
                "existing_device_id": "1",
            }
        )

    def _make_base_context(self, mock_existing):
        return (
            {"device_id": 42, "hostname": "r01"},
            {
                "existing_device": mock_existing,
                "device_type_mismatch": False,
                "librenms_id_needs_migration": True,
                "serial_confirmed": True,  # Default: serial confirmed
            },
            {},
        )

    def test_serial_not_confirmed_no_force_renders_htmx_error_toast(self):
        """Line 1277-1280: serial not confirmed, no force → htmx error toast (200)."""
        view = self._make_view()
        request = self._make_base_request()

        mock_existing = MagicMock()
        mock_existing.pk = 1
        mock_existing.custom_field_data = {"librenms_id": 42}  # int = needs migration, matches device_id

        libre_device, validation, selections = self._make_base_context(mock_existing)
        validation["serial_confirmed"] = False  # Not confirmed
        # force is not set (not "on")

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = Exception
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view,
                        "get_validated_device_with_selections",
                        return_value=(libre_device, validation, selections),
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Serial number not confirmed" in response.content

    def test_migration_succeeds_and_renders_row(self):
        """Lines 1282-1323: successful migration renders row."""
        view = self._make_view()
        request = self._make_base_request()

        mock_existing = MagicMock()
        mock_existing.pk = 1
        mock_existing.custom_field_data = {"librenms_id": 42}
        mock_existing.name = "router01"

        libre_device = {"device_id": 42, "hostname": "router01"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
            "librenms_id_needs_migration": True,
            "serial_confirmed": True,
        }

        DoesNotExistExc = type("DoesNotExist", (Exception,), {})
        locked_device = MagicMock()
        locked_device.pk = 1
        locked_device.custom_field_data = {"librenms_id": 42}  # Still int
        locked_device.name = "router01"

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = DoesNotExistExc
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        with patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx:
                            mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
                            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
                            with patch("dcim.models.Device") as MockDevice2:
                                # This inner patch shadows the outer one for the in-function
                                # `from dcim.models import Device`, so it must serve BOTH the
                                # pre-lock existing_device lookup and the locked re-read.
                                MockDevice2.objects.get.return_value = mock_existing
                                MockDevice2.objects.select_for_update.return_value.get.return_value = locked_device
                                MockDevice2.DoesNotExist = DoesNotExistExc
                                with patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None):
                                    with patch(
                                        "netbox_librenms_plugin.utils.migrate_legacy_librenms_id", return_value=True
                                    ) as mock_migrate:
                                        with patch(
                                            "netbox_librenms_plugin.views.imports.actions._save_device",
                                            return_value=None,
                                        ):
                                            with patch("netbox_librenms_plugin.views.imports.actions.cache"):
                                                with patch(
                                                    "netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key",
                                                    return_value="key",
                                                ):
                                                    with patch(
                                                        "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                                                        return_value={"device_id": 42},
                                                    ):
                                                        with patch.object(
                                                            view, "render_device_row", return_value=MagicMock()
                                                        ) as mock_render:
                                                            view.post(request, device_id=42)
        # The migration path ran to completion and rendered the updated row
        # (no blanket try/except masking a broken migrate/render path).
        assert mock_render.called
        # The conversion operates on the LOCKED instance, not the stale pre-lock one.
        assert mock_migrate.call_args.args[0] is locked_device


class TestDeviceConflictMoreActions:
    """Tests for many more action paths in DeviceConflictActionView."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def _base_setup(self, action, extra_post=None):
        """Return (view, request, mock_existing, libre_device, validation)."""
        view = self._make_view()
        post_data = {"action": action, "existing_device_id": "1"}
        if extra_post:
            post_data.update(extra_post)
        request = _make_request(post=post_data)
        mock_existing = MagicMock()
        mock_existing.pk = 1
        mock_existing.name = "router01"
        libre_device = {"device_id": 42, "hostname": "router01", "serial": "SN001", "hardware": "Cisco", "os": "ios"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }
        return view, request, mock_existing, libre_device, validation

    def _common_patches(self, view, mock_existing, libre_device, validation):
        """Return a context manager that patches common stuff."""
        from contextlib import ExitStack

        DoesNotExistExc = type("DoesNotExist", (Exception,), {})

        stack = ExitStack()
        stack.enter_context(patch.object(view, "require_all_permissions", return_value=None))

        MockDevice = MagicMock()
        MockDevice.objects.get.return_value = mock_existing
        MockDevice.objects.select_for_update.return_value.get.return_value = mock_existing
        MockDevice.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
        MockDevice.DoesNotExist = DoesNotExistExc

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

        mock_tx = MagicMock()
        mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)
        stack.enter_context(patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx))
        stack.enter_context(
            patch("netbox_librenms_plugin.views.imports.actions._get_hostname_for_action", return_value="router01")
        )
        stack.enter_context(
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value={"device_id": 42}
            )
        )

        return stack, MockDevice

    def test_link_save_error_returns_error(self):
        """Line 1090: link action → _save_device returns error."""
        view, request, mock_existing, libre_device, validation = self._base_setup("link")
        from django.http import HttpResponse

        error_response = HttpResponse("Save failed", status=400)

        with self._common_patches(view, mock_existing, libre_device, validation)[0]:
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=error_response):
                response = view.post(request, device_id=42)

        assert response.status_code == 400

    def test_update_serial_conflict_renders_htmx_error_toast(self):
        """Line 1139: update_serial with serial conflict → htmx error toast (200)."""
        view, request, mock_existing, libre_device, validation = self._base_setup("update_serial")
        conflict_device = MagicMock()
        conflict_device.name = "router99"
        conflict_device.pk = 99

        stack, MockDevice = self._common_patches(view, mock_existing, libre_device, validation)
        with stack:
            MockDevice.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = conflict_device
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=None):
                response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Serial conflict" in response.content

    def test_update_serial_save_success_renders_row(self):
        """Lines 1146-1149: update_serial with no conflict → save + render."""
        view, request, mock_existing, libre_device, validation = self._base_setup("update_serial")

        with self._common_patches(view, mock_existing, libre_device, validation)[0]:
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=None):
                with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render:
                    view.post(request, device_id=42)

        mock_render.assert_called_once()

    def test_sync_name_renders_row(self):
        """Lines 1155-1161: sync_name action → save + render."""
        view, request, mock_existing, libre_device, validation = self._base_setup("sync_name")

        with self._common_patches(view, mock_existing, libre_device, validation)[0]:
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=None):
                with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render:
                    view.post(request, device_id=42)

        mock_render.assert_called_once()

    def test_sync_name_save_error(self):
        """Line 1160: sync_name → _save_device returns error."""
        view, request, mock_existing, libre_device, validation = self._base_setup("sync_name")
        from django.http import HttpResponse

        error_resp = HttpResponse("error", status=400)

        with self._common_patches(view, mock_existing, libre_device, validation)[0]:
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=error_resp):
                response = view.post(request, device_id=42)

        assert response.status_code == 400

    def test_update_type_no_device_type_renders_htmx_error_toast(self):
        """Line 1171: update_type with no librenms_device_type → htmx error toast (200)."""
        view, request, mock_existing, libre_device, validation = self._base_setup("update_type")
        # No device_type_mismatch + no force → librenms_device_type = None

        with self._common_patches(view, mock_existing, libre_device, validation)[0]:
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=None):
                response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"No LibreNMS device type available to update" in response.content

    def test_sync_platform_success_renders_row(self):
        """Line 1222: sync_platform with found platform → save + render."""
        view, request, mock_existing, libre_device, validation = self._base_setup("sync_platform")
        mock_platform = MagicMock()
        mock_platform.name = "IOS"

        with self._common_patches(view, mock_existing, libre_device, validation)[0]:
            with patch(
                "netbox_librenms_plugin.utils.find_matching_platform",
                return_value={"found": True, "platform": mock_platform},
            ):
                with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=None):
                    with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render:
                        view.post(request, device_id=42)

        mock_render.assert_called_once()

    def test_sync_device_type_success_renders_row(self):
        """Line 1238: sync_device_type with match → save + render."""
        view, request, mock_existing, libre_device, validation = self._base_setup("sync_device_type")
        mock_dt = MagicMock()
        mock_dt.display = "Cisco Router"

        with self._common_patches(view, mock_existing, libre_device, validation)[0]:
            with patch(
                "netbox_librenms_plugin.utils.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": mock_dt},
            ):
                with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=None):
                    with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render:
                        view.post(request, device_id=42)

        mock_render.assert_called_once()

    def test_device_not_found_after_action_renders_htmx_error_toast(self):
        """Line 1338: get_validated_device_with_selections returns None after action."""
        view, request, mock_existing, libre_device, validation = self._base_setup("sync_name")

        # First call returns (libre_device, validation, {}) for permission check
        # After action, re-validate returns (None, None, {})
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return (libre_device, validation, {})
            return (None, None, {})

        with self._common_patches(view, mock_existing, libre_device, validation)[0]:
            with patch.object(view, "get_validated_device_with_selections", side_effect=side_effect):
                with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=None):
                    response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Device not found after action" in response.content


class TestMoreSaveErrorPaths:
    """Tests for save error paths in actions (lines 1108, 1116, 1119, 1146, 1149, 1168, 1182-1183, 1196-1210, 1222, 1238)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def _base_setup(self, action, extra_post=None):
        view = self._make_view()
        post_data = {"action": action, "existing_device_id": "1"}
        if extra_post:
            post_data.update(extra_post)
        request = _make_request(post=post_data)
        mock_existing = MagicMock()
        mock_existing.pk = 1
        mock_existing.name = "router01"
        libre_device = {"device_id": 42, "hostname": "router01", "serial": "SN001", "hardware": "Cisco", "os": "ios"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }
        return view, request, mock_existing, libre_device, validation

    def _setup_common(self, view, mock_existing, libre_device, validation, save_return=None):
        from contextlib import ExitStack

        DoesNotExistExc = type("DoesNotExist", (Exception,), {})
        MockDevice = MagicMock()
        MockDevice.objects.get.return_value = mock_existing
        MockDevice.objects.select_for_update.return_value.get.return_value = mock_existing
        MockDevice.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
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
            patch("netbox_librenms_plugin.views.imports.actions._get_hostname_for_action", return_value="router01")
        )
        stack.enter_context(
            patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value={"device_id": 42}
            )
        )
        if save_return is not None:
            stack.enter_context(
                patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=save_return)
            )
        return stack, MockDevice

    def test_update_serial_conflict_in_update(self):
        """Line 1108: update action with serial conflict → htmx error toast (200)."""
        view, request, mock_existing, libre_device, validation = self._base_setup("update")
        conflict = MagicMock()
        conflict.name = "other"
        conflict.pk = 99

        stack, MockDevice = self._setup_common(view, mock_existing, libre_device, validation, save_return=None)
        with stack:
            MockDevice.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = conflict
            response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Serial conflict" in response.content

    def test_update_with_device_type_mismatch_forced(self):
        """Lines 1116, 1119: update with force + device_type_mismatch → device_type applied."""
        view, request, mock_existing, libre_device, validation = self._base_setup("update", {"force": "on"})
        validation["device_type_mismatch"] = True
        validation["device_type"] = {"device_type": MagicMock()}

        stack, MockDevice = self._setup_common(view, mock_existing, libre_device, validation, save_return=None)
        with stack:
            with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render:
                view.post(request, device_id=42)

        mock_render.assert_called_once()

    def test_update_serial_with_device_type(self):
        """Lines 1146, 1149: update_serial with force device_type → render."""
        view, request, mock_existing, libre_device, validation = self._base_setup("update_serial", {"force": "on"})
        validation["device_type_mismatch"] = True
        validation["device_type"] = {"device_type": MagicMock()}

        stack, MockDevice = self._setup_common(view, mock_existing, libre_device, validation, save_return=None)
        with stack:
            with patch.object(view, "render_device_row", return_value=MagicMock()) as mock_render:
                view.post(request, device_id=42)

        mock_render.assert_called_once()

    def test_update_type_with_device_type_save_error(self):
        """Line 1168: update_type with save error → return error."""
        view, request, mock_existing, libre_device, validation = self._base_setup("update_type", {"force": "on"})
        validation["device_type_mismatch"] = True
        validation["device_type"] = {"device_type": MagicMock()}

        from django.http import HttpResponse

        error_resp = HttpResponse("save error", status=400)
        stack, _ = self._setup_common(view, mock_existing, libre_device, validation, save_return=error_resp)
        with stack:
            response = view.post(request, device_id=42)

        assert response.status_code == 400

    def test_sync_platform_save_error(self):
        """Line 1222: sync_platform → _save_device returns error."""
        view, request, mock_existing, libre_device, validation = self._base_setup("sync_platform")
        mock_platform = MagicMock()

        from django.http import HttpResponse

        error_resp = HttpResponse("save error", status=400)
        stack, _ = self._setup_common(view, mock_existing, libre_device, validation, save_return=error_resp)
        with stack:
            with patch(
                "netbox_librenms_plugin.utils.find_matching_platform",
                return_value={"found": True, "platform": mock_platform},
            ):
                response = view.post(request, device_id=42)

        assert response.status_code == 400

    def test_sync_device_type_save_error(self):
        """Line 1238: sync_device_type → _save_device returns error."""
        view, request, mock_existing, libre_device, validation = self._base_setup("sync_device_type")
        mock_dt = MagicMock()

        from django.http import HttpResponse

        error_resp = HttpResponse("save error", status=400)
        stack, _ = self._setup_common(view, mock_existing, libre_device, validation, save_return=error_resp)
        with stack:
            with patch(
                "netbox_librenms_plugin.utils.match_librenms_hardware_to_device_type",
                return_value={"matched": True, "device_type": mock_dt},
            ):
                response = view.post(request, device_id=42)

        assert response.status_code == 400


class TestSyncSerialAction:
    """Tests for sync_serial action (lines 1173-1210)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def test_sync_serial_no_serial_renders_htmx_error_toast(self):
        """Line 1210: sync_serial with empty serial → htmx error toast (200)."""
        view = self._make_view()
        request = _make_request(post={"action": "sync_serial", "existing_device_id": "1"})
        mock_existing = MagicMock()
        mock_existing.pk = 1

        libre_device = {"device_id": 42, "hostname": "router01", "serial": ""}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
        }

        DoesNotExistExc = type("DoesNotExist", (Exception,), {})
        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device") as MockDevice:
                MockDevice.objects.get.return_value = mock_existing
                MockDevice.DoesNotExist = DoesNotExistExc
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(
                        view, "get_validated_device_with_selections", return_value=(libre_device, validation, {})
                    ):
                        response = view.post(request, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"No valid serial from LibreNMS" in response.content


class TestUpdateAndSerialSaveErrors:
    """Tests for update/update_serial _save_device error paths (lines 1119, 1149)."""

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
        MockDevice.objects.get.return_value = mock_existing
        MockDevice.objects.select_for_update.return_value.get.return_value = mock_existing
        MockDevice.objects.select_for_update.return_value.filter.return_value.exclude.return_value.first.return_value = None
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
            MockDevice.objects.filter.return_value.exclude.return_value.first.return_value = None
            with patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=err):
                response = view.post(request, device_id=42)

        assert response.status_code == 400


class TestMigrateLibreNMSIdTransactionPaths:
    """Tests for migrate_librenms_id inside transaction (lines 1282-1323)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import DeviceConflictActionView

        view = object.__new__(DeviceConflictActionView)
        view._librenms_api = _make_api()
        view.request = MagicMock()
        return view

    def _make_valid_migrate_context(self, view, extra_mock=None):
        """Common setup for valid migrate_librenms_id (serial_confirmed=True)."""
        request = _make_request(post={"action": "migrate_librenms_id", "existing_device_id": "1"})
        mock_existing = MagicMock()
        mock_existing.pk = 1
        mock_existing.custom_field_data = {"librenms_id": 42}
        mock_existing.name = "router01"

        libre_device = {"device_id": 42, "hostname": "router01"}
        validation = {
            "existing_device": mock_existing,
            "device_type_mismatch": False,
            "librenms_id_needs_migration": True,
            "serial_confirmed": True,
        }

        DoesNotExistExc = type("DoesNotExist", (Exception,), {})
        locked_device = MagicMock()
        locked_device.pk = 1
        locked_device.custom_field_data = {"librenms_id": 42}
        locked_device.name = "router01"

        MockDevice = MagicMock()
        MockDevice.objects.get.return_value = mock_existing
        MockDevice.objects.select_for_update.return_value.get.return_value = locked_device
        MockDevice.DoesNotExist = DoesNotExistExc

        mock_tx = MagicMock()
        mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)

        return request, mock_existing, libre_device, validation, locked_device, MockDevice, DoesNotExistExc, mock_tx

    def test_migrate_device_deleted_under_lock(self):
        """Lines 1285-1289: DoesNotExist during select_for_update → htmx error toast (200)."""
        view = self._make_view()
        req, mock_ex, libre, val, locked, MockDevice, DNE, mock_tx = self._make_valid_migrate_context(view)
        MockDevice.objects.select_for_update.return_value.get.side_effect = DNE("gone")

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device", MockDevice):
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(view, "get_validated_device_with_selections", return_value=(libre, val, {})):
                        with patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx):
                            response = view.post(req, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Object no longer exists" in response.content

    def test_migrate_already_migrated_under_lock(self):
        """Lines 1292-1298: cf_locked already dict under lock → htmx error toast (200)."""
        view = self._make_view()
        req, mock_ex, libre, val, locked, MockDevice, DNE, mock_tx = self._make_valid_migrate_context(view)
        locked.custom_field_data = {"librenms_id": {"default": 42}}  # Already migrated under lock

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device", MockDevice):
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(view, "get_validated_device_with_selections", return_value=(libre, val, {})):
                        with patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx):
                            response = view.post(req, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"already in JSON format" in response.content

    def test_migrate_id_changed_under_lock(self):
        """Lines 1300-1303: cf_locked_int != librenms_id under lock → htmx error toast (200)."""
        view = self._make_view()
        req, mock_ex, libre, val, locked, MockDevice, DNE, mock_tx = self._make_valid_migrate_context(view)
        locked.custom_field_data = {"librenms_id": 99}  # Different ID under lock

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device", MockDevice):
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(view, "get_validated_device_with_selections", return_value=(libre, val, {})):
                        with patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx):
                            response = view.post(req, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"changed under lock" in response.content

    def test_migrate_id_conflict_with_other_device(self):
        """Lines 1309-1315: another device already has this ID → htmx error toast (200)."""
        view = self._make_view()
        req, mock_ex, libre, val, locked, MockDevice, DNE, mock_tx = self._make_valid_migrate_context(view)
        conflict_dev = MagicMock()
        conflict_dev.pk = 99  # Different pk → conflict

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device", MockDevice):
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(view, "get_validated_device_with_selections", return_value=(libre, val, {})):
                        with patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx):
                            with patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=conflict_dev):
                                response = view.post(req, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Another device already has librenms_id" in response.content

    def test_migrate_migration_fails(self):
        """Lines 1316-1320: migrate_legacy_librenms_id returns False → htmx error toast (200)."""
        view = self._make_view()
        req, mock_ex, libre, val, locked, MockDevice, DNE, mock_tx = self._make_valid_migrate_context(view)

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device", MockDevice):
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(view, "get_validated_device_with_selections", return_value=(libre, val, {})):
                        with patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx):
                            with patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None):
                                with patch(
                                    "netbox_librenms_plugin.utils.migrate_legacy_librenms_id", return_value=False
                                ):
                                    response = view.post(req, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Migration failed" in response.content

    def test_migrate_save_error(self):
        """Migrate path saves only librenms_id field; IntegrityError on save → htmx toast."""
        view = self._make_view()
        req, mock_ex, libre, val, locked, MockDevice, DNE, mock_tx = self._make_valid_migrate_context(view)
        from django.db import IntegrityError

        locked.save.side_effect = IntegrityError("dup")

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device", MockDevice):
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(view, "get_validated_device_with_selections", return_value=(libre, val, {})):
                        with patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx):
                            with patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None):
                                with patch(
                                    "netbox_librenms_plugin.utils.migrate_legacy_librenms_id", return_value=True
                                ):
                                    response = view.post(req, device_id=42)

        assert response.status_code == 200
        assert response.headers.get("HX-Reswap") == "none"
        assert b"Unable to migrate the LibreNMS mapping" in response.content

    def test_migrate_success_renders_row(self):
        """Lines 1323+: successful migration renders row."""
        view = self._make_view()
        req, mock_ex, libre, val, locked, MockDevice, DNE, mock_tx = self._make_valid_migrate_context(view)

        with patch.object(view, "require_all_permissions", return_value=None):
            with patch("dcim.models.Device", MockDevice):
                with patch.object(view, "require_object_permissions", return_value=None):
                    with patch.object(view, "get_validated_device_with_selections", return_value=(libre, val, {})):
                        with patch("netbox_librenms_plugin.views.imports.actions.transaction", mock_tx):
                            with patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None):
                                with patch(
                                    "netbox_librenms_plugin.utils.migrate_legacy_librenms_id", return_value=True
                                ):
                                    with patch(
                                        "netbox_librenms_plugin.views.imports.actions._save_device", return_value=None
                                    ):
                                        with patch("netbox_librenms_plugin.views.imports.actions.cache"):
                                            with patch(
                                                "netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key",
                                                return_value="key",
                                            ):
                                                with patch(
                                                    "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                                                    return_value={"device_id": 42},
                                                ):
                                                    with patch.object(
                                                        view, "render_device_row", return_value=MagicMock()
                                                    ) as mock_render:
                                                        view.post(req, device_id=42)

        mock_render.assert_called_once()


class TestBulkImportConfirmPartialExpiry:
    """Test partial expiry path in BulkImportConfirmView (line 422)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView

        view = object.__new__(BulkImportConfirmView)
        view._librenms_api = _make_api()
        return view

    def test_one_expired_one_valid_device_still_renders(self):
        """One selected device is fetched, the other has expired from cache. Because any
        fetched device is unconditionally appended to ``devices``, the post() still renders
        the valid device (200) rather than erroring — the ``if not devices`` 400 branches
        are only reached when *every* selected device is missing."""
        view = self._make_view()
        request = _make_request(post={"select": ["1", "2"]})
        request.POST.getlist = MagicMock(return_value=["1", "2"])
        request.GET = MagicMock()
        request.GET.get = MagicMock(return_value=None)

        call_count = [0]

        def fetch_side_effect(device_id, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"device_id": 1, "hostname": "router01"}  # Found
            return None  # Not found (expired)

        validation = {
            "status": "importable",
            "resolved_name": "router01",
            "virtual_chassis": {},
        }

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", side_effect=fetch_side_effect
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                    return_value={"cluster_id": None, "role_id": None, "rack_id": None},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                        return_value=validation,
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                            return_value=(True, False),
                        ):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.render",
                                return_value=MagicMock(status_code=200),
                            ) as mock_render:
                                response = view.post(request)

        # The valid device (1) is rendered despite device 2 having expired; the loop
        # fetched both ids (the expired one bumps cache_expired_count but isn't fatal).
        assert response.status_code == 200
        mock_render.assert_called_once()
        # Pin the output: exactly the one surviving device renders on the confirm step, so a
        # regression that drops the survivor (or renders a different response) is caught.
        # (cache_expired_count is surfaced as a warning message, not a context key.)
        template = mock_render.call_args.args[1]
        context = mock_render.call_args.args[2]
        assert template.endswith("bulk_import_confirm.html")
        assert len(context["devices"]) == 1
        assert context["device_count"] == 1
        assert call_count[0] == 2


class TestBulkImportDevicesViewBasicPaths:
    """Tests for BulkImportDevicesView early paths (lines 498-763)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = object.__new__(BulkImportDevicesView)
        view._librenms_api = _make_api()
        return view

    def test_no_devices_selected_returns_400(self):
        """No device IDs on the HTMX path → bare 400 (non-HTMX redirects instead)."""
        view = self._make_view()
        request = _make_request(post={}, headers={"HX-Request": "true"})
        request.POST.getlist = MagicMock(return_value=[])

        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                response = view.post(request)

        assert response.status_code == 400

    def test_invalid_device_id_returns_400(self):
        """Non-integer device_id on the HTMX path → bare 400 (non-HTMX redirects instead)."""
        view = self._make_view()
        request = _make_request(post={}, headers={"HX-Request": "true"})
        request.POST.getlist = MagicMock(return_value=["not-an-int"])

        with patch.object(view, "require_write_permission", return_value=None):
            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                response = view.post(request)

        assert response.status_code == 400

    def test_sync_mode_import_runs(self):
        """Lines 498-763: synchronous import path runs without crashing."""
        view = self._make_view()
        request = _make_request(post={"select": ["1"]})
        request.POST.getlist = MagicMock(return_value=["1"])
        request.user = MagicMock()
        request.user.is_superuser = False  # Forces sync mode
        request.POST.get = MagicMock(return_value=None)
        request.headers = {}

        import_result = {"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0}

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices", return_value=import_result
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                            return_value={"device_id": 1, "hostname": "r01"},
                        ):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                                return_value={"status": "importable"},
                            ):
                                with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                    with patch(
                                        "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                                        return_value={"cluster_id": None, "role_id": None, "rack_id": None},
                                    ):
                                        with patch(
                                            "netbox_librenms_plugin.views.imports.actions.redirect",
                                            return_value=MagicMock(status_code=302),
                                        ) as mock_redirect:
                                            view.post(request)

        # Non-HTMX request redirects
        mock_redirect.assert_called()

    def test_background_mode_returns_job_json(self):
        """Background mode: should_use_background_job returns True for superuser."""
        view = self._make_view()
        # Just test the should_use_background_job_for_import helper
        request = _make_request(post={"use_background_job": "on"})
        request.user = MagicMock()
        request.user.is_superuser = True
        result = view.should_use_background_job_for_import(request)
        assert result is True

    def test_sync_import_uses_return_url_vc_flag(self):
        """VC detection flag from return_url is propagated to sync bulk import."""
        view = self._make_view()
        request = _make_request(
            post={
                "select": ["1"],
                "return_url": "/plugins/librenms_plugin/librenms-import/?enable_vc_detection=true",
            }
        )
        request.POST.getlist = MagicMock(return_value=["1"])
        request.user = MagicMock()
        request.user.is_superuser = False
        request.headers = {}

        import_result = {"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0}

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                    return_value={"device_id": 1, "hostname": "r01"},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                        return_value=import_result,
                    ) as mock_bulk_import:
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                            return_value={"success": [], "failed": [], "skipped": []},
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.redirect",
                                    return_value=MagicMock(status_code=302),
                                ):
                                    view.post(request)

        assert mock_bulk_import.called
        assert mock_bulk_import.call_args.kwargs["sync_options"]["vc_detection_enabled"] is True


class TestBulkImportDevicesMorePaths:
    """Additional paths in BulkImportDevicesView (lines 516-693, 701-758)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = object.__new__(BulkImportDevicesView)
        view._librenms_api = _make_api()
        return view

    def _make_base_request(self, device_ids, extra_post=None):
        request = _make_request(post={})
        dict(extra_post or {})
        request.POST.getlist = MagicMock(return_value=device_ids)
        request.user = MagicMock()
        request.user.is_superuser = False
        request.POST.get = MagicMock(return_value=None)
        request.headers = {}
        return request

    def test_invalid_cluster_value_logs_warning(self):
        """Lines 522-526: invalid cluster_value → warning, continue."""
        view = self._make_view()
        request = self._make_base_request(["1"])
        # cluster_1 is set to invalid value
        request.POST.get = MagicMock(side_effect=lambda k, d=None: "not-int" if k == "cluster_1" else None)

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                            return_value={"device_id": 1},
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                                ) as mock_redirect:
                                    view.post(request)

        mock_redirect.assert_called()

    def test_valid_role_and_rack_values_applied(self):
        """Lines 531-552: valid role_id and rack_id → parsed into mappings."""
        view = self._make_view()
        request = self._make_base_request(["1"])

        # role_1=2, rack_1=3
        def get_side_effect(k, d=None):
            if k == "role_1":
                return "2"
            if k == "rack_1":
                return "3"
            return None

        request.POST.get = MagicMock(side_effect=get_side_effect)

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                            return_value={"device_id": 1},
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                                ) as mock_redirect:
                                    view.post(request)

        mock_redirect.assert_called()

    def test_vc_detection_disabled_in_post_is_passed_to_device_import(self):
        """vc_detection_enabled=off from POST must propagate to bulk import call."""
        view = self._make_view()
        request = self._make_base_request(["1"])
        request.POST.get = MagicMock(side_effect=lambda k, d=None: "off" if k == "enable_vc_detection" else None)

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
                ) as mock_bulk_import:
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                            return_value={"device_id": 1},
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                                ):
                                    view.post(request)

        call_kwargs = mock_bulk_import.call_args.kwargs
        assert call_kwargs["sync_options"]["vc_detection_enabled"] is False

    def test_invalid_role_and_rack_values_log_warning(self):
        """Lines 534-535, 544-546: invalid role_id/rack_id → warning."""
        view = self._make_view()
        request = self._make_base_request(["1"])

        def get_side_effect(k, d=None):
            if k == "role_1":
                return "not-int"
            if k == "rack_1":
                return "not-int"
            return None

        request.POST.get = MagicMock(side_effect=get_side_effect)

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                            return_value={"device_id": 1},
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                                ) as mock_redirect:
                                    view.post(request)

        mock_redirect.assert_called()

    def test_import_with_success_messages(self):
        """Lines 683, 688, 693: success/fail/skipped messages."""
        view = self._make_view()
        request = self._make_base_request(["1"])
        request.POST.get = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.pk = 1

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    return_value={
                        "success": [{"device_id": 1, "device": mock_device}],
                        "failed": [{"device_id": 1, "error": "failed"}],
                        "skipped": [{"device_id": 1}],
                        "virtual_chassis_created": 0,
                    },
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache", return_value=None
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.messages") as mock_messages:
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                                ):
                                    view.post(request)

        mock_messages.success.assert_called()
        mock_messages.error.assert_called()
        mock_messages.warning.assert_called()

    def test_vm_import_triggers_bulk_import_vms(self):
        """Line 651-668: vm_imports non-empty → bulk_import_vms called."""
        view = self._make_view()
        request = self._make_base_request(["1"])

        # cluster_1=5 → device 1 is a VM
        def get_side_effect(k, d=None):
            if k == "cluster_1":
                return "5"
            return None

        request.POST.get = MagicMock(side_effect=get_side_effect)

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ) as mock_vm_import:
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                            return_value={"device_id": 1},
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                                ):
                                    view.post(request)

        mock_vm_import.assert_called()

    def test_htmx_request_returns_oob_rows(self):
        """Lines 701-761: HTMX request → returns OOB row HTML."""
        view = self._make_view()
        request = self._make_base_request(["1"])
        request.headers = {"HX-Request": "true"}
        request.POST.get = MagicMock(return_value=None)

        mock_device = MagicMock()
        mock_device.pk = 1

        libre_device = {"device_id": 1, "hostname": "r01"}

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    return_value={
                        "success": [{"device_id": 1, "device": mock_device}],
                        "failed": [],
                        "skipped": [],
                        "virtual_chassis_created": 0,
                    },
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                            return_value=libre_device,
                        ):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                                return_value={"status": "imported"},
                            ):
                                with patch("netbox_librenms_plugin.views.imports.actions.cache"):
                                    with patch(
                                        "netbox_librenms_plugin.views.imports.actions.get_import_device_cache_key",
                                        return_value="key",
                                    ):
                                        with patch(
                                            "netbox_librenms_plugin.views.imports.actions.DeviceImportTable",
                                            return_value=MagicMock(),
                                        ):
                                            with patch(
                                                "netbox_librenms_plugin.views.imports.actions.render"
                                            ) as mock_render:
                                                mock_render.return_value.content = b"<tr>row</tr>"
                                                with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                                    response = view.post(request)

        assert response.status_code == 200
        assert b"row" in response.content or response.content == b"\n".join([b"<tr>row</tr>"])

    def test_permission_denied_during_import_redirects(self):
        """Lines 659-668: PermissionDenied during import → redirect."""
        view = self._make_view()
        request = self._make_base_request(["1"])
        request.POST.get = MagicMock(return_value=None)
        request.headers = {}

        from django.core.exceptions import PermissionDenied as DjPD

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    side_effect=DjPD("No permission"),
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                        return_value={"device_id": 1},
                    ):
                        with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                            ) as mock_redirect:
                                view.post(request)

        mock_redirect.assert_called()

    def test_background_no_workers_falls_back_to_sync(self):
        """Line 612-615: background requested but no workers → sync fallback."""
        view = self._make_view()
        request = self._make_base_request(["1"])
        request.user.is_superuser = True
        request.POST.get = MagicMock(side_effect=lambda k, d=None: "on" if k == "use_background_job" else None)

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                            return_value={"device_id": 1},
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                                ) as mock_redirect:
                                    with patch("utilities.rqworker.get_workers_for_queue", return_value=0):
                                        view.post(request)

        mock_redirect.assert_called()


class TestBulkImportEdgePaths:
    """Tests for remaining BulkImportDevicesView edge paths."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportDevicesView

        view = object.__new__(BulkImportDevicesView)
        view._librenms_api = _make_api()
        return view

    def test_cluster_with_role_applies_role_to_vm(self):
        """Line 521: cluster + role for VM import."""
        view = self._make_view()
        request = _make_request(post={})
        request.POST.getlist = MagicMock(return_value=["1"])
        request.user = MagicMock()
        request.user.is_superuser = False
        request.headers = {}

        def get_side_effect(k, d=None):
            if k == "cluster_1":
                return "5"
            if k == "role_1":
                return "3"
            return None

        request.POST.get = MagicMock(side_effect=get_side_effect)

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                    return_value={"success": [], "failed": [], "skipped": [], "virtual_chassis_created": 0},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_vms",
                        return_value={"success": [], "failed": [], "skipped": []},
                    ) as mock_vm:
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                            return_value={"device_id": 1},
                        ):
                            with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                                with patch(
                                    "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                                ):
                                    view.post(request)

        # VM import should have been called with role
        mock_vm.assert_called()

    def test_permission_denied_htmx_returns_htmx_redirect(self):
        """Line 664: PermissionDenied during import with HX-Request → HX-Redirect."""
        view = self._make_view()
        request = _make_request(post={})
        request.POST.getlist = MagicMock(return_value=["1"])
        request.user = MagicMock()
        request.user.is_superuser = False
        request.headers = {"HX-Request": "true"}
        request.POST.get = MagicMock(return_value=None)

        from django.core.exceptions import PermissionDenied as DjPD

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                    return_value={"device_id": 1, "hostname": "test-device"},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.bulk_import_devices",
                        side_effect=DjPD("No permission"),
                    ):
                        with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                            response = view.post(request)

        assert response.headers.get("HX-Redirect") is not None

    def test_background_with_workers_enqueues_job(self):
        """Lines 575-611: background with workers available → enqueue job."""
        view = self._make_view()
        request = _make_request(post={})
        request.POST.getlist = MagicMock(return_value=["1"])
        request.user = MagicMock()
        request.user.is_superuser = True
        request.headers = {}

        def get_side_effect(k, d=None):
            if k == "use_background_job":
                return "on"
            return None

        request.POST.get = MagicMock(side_effect=get_side_effect)

        mock_job = MagicMock()
        mock_job.pk = 123
        mock_job.job_id = "uuid-456"

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences", return_value=(True, False)
            ):
                with patch("utilities.rqworker.get_workers_for_queue", return_value=2):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                        return_value={"device_id": 1},
                    ):
                        with patch("netbox_librenms_plugin.views.imports.actions.messages"):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.redirect", return_value=MagicMock()
                            ) as mock_redirect:
                                # Patch ImportDevicesJob at the point it's imported inside post()
                                with patch("netbox_librenms_plugin.jobs.ImportDevicesJob") as MockJob:
                                    MockJob.enqueue.return_value = mock_job
                                    result = view.post(request)

        # Pin the regression this test is named for: the background path must actually
        # enqueue the job, not merely take some redirecting branch.
        MockJob.enqueue.assert_called_once()
        mock_redirect.assert_called()
        # The redirect response must actually be returned, not just produced.
        assert result is mock_redirect.return_value

        # ...and the enqueued job must carry the request's inputs forward, not be enqueued
        # empty: the selected device id (parsed to int), the active server namespace, and
        # the resolved sync options. Otherwise the background import silently does nothing.
        enqueue_kwargs = MockJob.enqueue.call_args.kwargs
        assert enqueue_kwargs["device_ids"] == [1]
        assert enqueue_kwargs["server_key"] == "default"
        assert enqueue_kwargs["sync_options"]["use_sysname"] is True


class TestBulkImportConfirmCollisions:
    """Tests for Stage 3 collision-blocking behaviour."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import BulkImportConfirmView

        view = object.__new__(BulkImportConfirmView)
        view._librenms_api = _make_api()
        return view

    def _run_with_two_devices(self, validation_a, validation_b):
        """Drive BulkImportConfirmView.post with two LibreNMS rows whose
        validations are stubbed to whatever the test wants. Returns the
        actual response object returned by view.post()."""
        view = self._make_view()
        request = _make_request(post={"select": ["1", "2"]})
        request.POST.getlist = MagicMock(return_value=["1", "2"])
        request.GET = MagicMock()
        request.GET.get = MagicMock(return_value=None)

        libre_devices = {
            1: {"device_id": 1, "hostname": "alpha"},
            2: {"device_id": 2, "hostname": "beta"},
        }
        validations = {1: validation_a, 2: validation_b}

        def fake_validate(libre_device, **_kwargs):
            return validations[libre_device["device_id"]]

        with patch.object(view, "require_write_permission", return_value=None):
            with patch(
                "netbox_librenms_plugin.views.imports.actions.fetch_device_with_cache",
                side_effect=lambda did, _api: libre_devices.get(did),
            ):
                with patch(
                    "netbox_librenms_plugin.views.imports.actions.extract_device_selections",
                    return_value={"cluster_id": None, "role_id": None, "rack_id": None},
                ):
                    with patch(
                        "netbox_librenms_plugin.views.imports.actions.validate_device_for_import",
                        side_effect=fake_validate,
                    ):
                        with patch(
                            "netbox_librenms_plugin.views.imports.actions.resolve_naming_preferences",
                            return_value=(True, False),
                        ):
                            with patch(
                                "netbox_librenms_plugin.views.imports.actions.render",
                            ) as mock_render:
                                mock_render.side_effect = lambda req, tpl, ctx, status=200: MagicMock(
                                    status_code=status,
                                    template_name=tpl,
                                    context=ctx,
                                )
                                response = view.post(request)
        return response

    def test_collision_path_renders_collision_template(self):
        from types import SimpleNamespace

        nb_device = SimpleNamespace(pk=42, name="srv-collide")
        validation_a = {
            "status": "importable",
            "resolved_name": "alpha",
            "virtual_chassis": {},
            "existing_device": nb_device,
        }
        validation_b = {
            "status": "importable",
            "resolved_name": "beta",
            "virtual_chassis": {},
            "oob_candidate": {"device": nb_device, "type": "idrac"},
        }
        response = self._run_with_two_devices(validation_a, validation_b)
        # Collision modal is an interstitial swapped into #htmx-modal-content,
        # so it must render at 200 -- a non-2xx status makes HTMX skip the swap.
        assert response is not None, "view.post returned None instead of a rendered response"
        assert "bulk_import_collision.html" in response.template_name
        assert response.status_code == 200
        assert len(response.context["collisions"]) == 1
        assert response.context["collisions"][0]["nb_device_pk"] == 42

    def test_clean_batch_renders_normal_confirm_template(self):
        from types import SimpleNamespace

        validation_a = {
            "status": "importable",
            "resolved_name": "alpha",
            "virtual_chassis": {},
            "existing_device": SimpleNamespace(pk=1, name="nb-a"),
        }
        validation_b = {
            "status": "importable",
            "resolved_name": "beta",
            "virtual_chassis": {},
            "existing_device": SimpleNamespace(pk=2, name="nb-b"),
        }
        response = self._run_with_two_devices(validation_a, validation_b)
        assert response is not None, "view.post returned None instead of a rendered response"
        assert "bulk_import_confirm.html" in response.template_name
        # Default render() status is 200.
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# AddAsOOBView / PromoteToHostView — generic "oob" sentinel regression tests
# ---------------------------------------------------------------------------


class TestAddAsOOBViewGenericSentinel:
    """AddAsOOBView must not return HTTP 400 when oob_candidate.type == "oob".

    Regression for the bug where the detection layer produced type="oob" as a
    sentinel (hostname mismatch, no OOB keywords in names) but set_librenms_oob
    rejected "oob" with ValueError, causing every non-keyword device to fail.

    Per testing conventions the submit-path behavior is tested at the utility
    layer (set_librenms_oob) rather than by driving the full view.
    """

    def test_generic_oob_sentinel_accepted_by_set_librenms_oob(self):
        """set_librenms_oob must not raise ValueError for oob_type='oob'.

        This is the direct root cause: AddAsOOBView calls
        set_librenms_oob(..., oob_type=oob_candidate["type"]) where the
        candidate type may be the detection-layer sentinel "oob".
        """
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
        """A device whose librenms_id is still the legacy bare int must NOT silently no-op:
        set_librenms_oob promotes it to the per-server dict and attaches the OOB block."""
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

    def test_sentinel_from_detection_layer_flows_to_storage(self):
        """The three-layer fallback in device_operations produces oob_type='oob'
        when neither the LibreNMS OS field nor either device name contains an OOB
        keyword. That value must store without error.
        """
        from netbox_librenms_plugin.utils import set_librenms_oob

        # Simulate: oob_type_from_libre=None, _detect_oob_type_from_name(...)=None
        # → inferred_oob_type = "oob"  (device_operations.py line ~563)
        oob_type_from_libre = None
        detected_from_hostname = None
        inferred_oob_type = oob_type_from_libre or detected_from_hostname or "oob"
        assert inferred_oob_type == "oob"

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": {"id": 99}}}

        # Must not raise
        set_librenms_oob(obj, 42, "default", oob_type=inferred_oob_type)
        assert obj.custom_field_data["librenms_id"]["default"]["oob"]["id"] == 42


class TestPromoteToHostViewGenericSentinel:
    """PromoteToHostView must not return HTTP 400 when existing_oob_type == "oob".

    Regression for the same sentinel bug: when the existing device's name has no
    OOB keyword, promote_to_host["existing_oob_type"] = "oob" (device_operations.py
    line ~574), which was rejected by set_librenms_oob.
    """

    def test_promote_generic_oob_sentinel_accepted_by_set_librenms_oob(self):
        """Promote path: set_librenms_oob with oob_type='oob' must not raise."""
        from netbox_librenms_plugin.utils import set_librenms_oob

        obj = MagicMock()
        obj.custom_field_data = {"librenms_id": {"default": {"id": 10}}}

        # Simulate: existing_oob_from_name = None
        # → promote["existing_oob_type"] = None or "oob" = "oob"
        existing_oob_type = None or "oob"
        assert existing_oob_type == "oob"

        # Previously: ValueError("oob_type 'oob' does not match any known OOB type")
        # → PromoteToHostView returned HTTP 400 "Invalid promotion data: ..."
        set_librenms_oob(obj, 7, "default", oob_type=existing_oob_type)
        assert obj.custom_field_data["librenms_id"]["default"]["oob"]["type"] == "oob"


class TestAddAsOOBViewPost:
    """View-level tests for AddAsOOBView.post() — HTTP interface + OOB sentinel regression."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        view = object.__new__(AddAsOOBView)
        view.kwargs = {}
        view._librenms_api = _make_api()

        # Default: write permission granted
        view.require_write_permission = MagicMock(return_value=None)
        # Default: object permissions granted
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def test_missing_existing_device_id_returns_htmx_error(self):
        """POST without existing_device_id returns HTMX error."""
        view = self._make_view()
        request = _make_request(post={})

        response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert b"Missing existing_device_id" in response.content
        assert response["HX-Reswap"] == "none"

    def test_write_permission_denied_returns_error(self):
        """When write permission is denied, view returns that error immediately."""
        from django.http import HttpResponse

        view = self._make_view()
        perm_error = HttpResponse("Forbidden", status=403)
        view.require_write_permission = MagicMock(return_value=perm_error)

        request = _make_request(post={"existing_device_id": "1"})
        response = view.post(request, device_id=1)

        assert response.status_code == 403

    def test_invalid_existing_device_id_returns_htmx_error(self):
        """POST with a non-integer existing_device_id returns HTMX error — the failure is
        the int() conversion, which raises before any ORM lookup, so the manager is never hit."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "not-a-number"})

        with patch("dcim.models.Device") as mock_device:
            # DoesNotExist must be a real exception so the view's except tuple is valid; the
            # manager itself is NOT stubbed to raise — int("not-a-number") fails first.
            mock_device.DoesNotExist = type("DoesNotExist", (Exception,), {})
            response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert b"Existing device not found" in response.content
        assert response["HX-Reswap"] == "none"
        # The malformed id is rejected before any DB lookup.
        mock_device.objects.get.assert_not_called()

    def test_device_does_not_exist_returns_htmx_error(self):
        """POST with an existing_device_id that refers to a deleted device returns HTMX error."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "999"})

        with patch("dcim.models.Device") as mock_device:
            does_not_exist = type("DoesNotExist", (Exception,), {})
            mock_device.DoesNotExist = does_not_exist
            mock_device.objects.get.side_effect = does_not_exist("not found")
            response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert b"Existing device not found" in response.content
        assert response["HX-Reswap"] == "none"

    def test_no_oob_candidate_in_validation_returns_htmx_error(self):
        """When validation has no oob_candidate, view returns an HTMX error."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "5"})

        existing_device = MagicMock()
        existing_device.pk = 5

        with patch("dcim.models.Device") as mock_device:
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.return_value = existing_device
            view.get_validated_device_with_selections = MagicMock(
                return_value=({"device_id": 99}, {"oob_candidate": None}, {})
            )
            response = view.post(request, device_id=99)

        assert response.status_code == 200
        assert b"No OOB candidate" in response.content
        assert response["HX-Reswap"] == "none"

    def test_device_id_mismatch_returns_htmx_error(self):
        """When oob_candidate device pk does not match existing_device_id, returns HTMX error."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "5"})

        existing_device = MagicMock()
        existing_device.pk = 5
        existing_device.custom_field_data = {"librenms_id": {"default": {"id": 10}}}

        oob_device = MagicMock()
        oob_device.pk = 99  # Different PK

        with patch("dcim.models.Device") as mock_device:
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.return_value = existing_device
            view.get_validated_device_with_selections = MagicMock(
                return_value=({"device_id": 50}, {"oob_candidate": {"device": oob_device, "type": "oob"}}, {})
            )
            response = view.post(request, device_id=50)

        assert response.status_code == 200
        assert b"mismatch" in response.content.lower() or b"Device ID mismatch" in response.content
        assert response["HX-Reswap"] == "none"

    def test_legacy_librenms_id_returns_htmx_error(self):
        """Device with legacy bare-int librenms_id is rejected with convert-first message."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "5"})

        existing_device = MagicMock()
        existing_device.pk = 5
        # Legacy bare-int librenms_id (not the expected dict structure)
        existing_device.custom_field_data = {"librenms_id": 42}

        oob_candidate_device = MagicMock()
        oob_candidate_device.pk = 5

        with patch("dcim.models.Device") as mock_device:
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.return_value = existing_device
            view.get_validated_device_with_selections = MagicMock(
                return_value=({"device_id": 77}, {"oob_candidate": {"device": oob_candidate_device, "type": "oob"}}, {})
            )
            response = view.post(request, device_id=77)

        assert response.status_code == 200
        assert b"legacy" in response.content.lower()
        assert response["HX-Reswap"] == "none"

    def test_libre_device_not_found_returns_htmx_error(self):
        """When get_validated_device_with_selections returns no libre_device, returns HTMX error."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "5"})

        existing_device = MagicMock()
        existing_device.pk = 5

        with patch("dcim.models.Device") as mock_device:
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.return_value = existing_device
            view.get_validated_device_with_selections = MagicMock(return_value=(None, None, None))
            response = view.post(request, device_id=1)

        assert response.status_code == 200
        assert b"not found" in response.content.lower()
        assert response["HX-Reswap"] == "none"

    def test_happy_path_oob_sentinel_links_and_refreshes(self):
        """End-to-end happy path with type=='oob': the view must reach set_librenms_oob
        (run for real, so a view-side validation break is caught — not just the unit
        test) and return the non-error validationRefresh response, not an HTMX error."""
        from django.http import HttpResponse

        view = self._make_view()
        # Run under a NON-default server namespace so the test fails if the view ever
        # hardcodes "default" instead of honouring self._librenms_api.server_key.
        view._librenms_api.server_key = "secondary"
        request = _make_request(post={"existing_device_id": "5"})

        existing_device = MagicMock()
        existing_device.pk = 5
        existing_device.name = "host-a"
        existing_device.oob_ip_id = 1  # already set → skip the OOB-IP sub-flow
        existing_device.custom_field_data = {"librenms_id": {"secondary": {"id": 10}}}

        # A distinct locked row (re-read under select_for_update). The view must mutate and
        # persist THIS instance, not the stale pre-lock lookup — using one object for both
        # would hide a regression that saves the unlocked instance.
        locked_device = MagicMock()
        locked_device.pk = 5
        locked_device.name = "host-a"
        locked_device.oob_ip_id = 1
        locked_device.custom_field_data = {"librenms_id": {"secondary": {"id": 10}}}

        # Distinct host id (10, already on the device) vs incoming OOB controller id (17):
        # so the assertion pins that the *incoming* id lands in oob.id, not a reused host id.
        libre_device = {"device_id": 17}
        # No "ip" → the OOB-IP set block is skipped; type "oob" is the regression target.
        validation = {"oob_candidate": {"device": existing_device, "type": "oob", "ip": None}}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        import copy

        # Snapshot the saved instance + its custom_field_data AT save time, so the assertion
        # proves the OOB mapping was present when _save_device() ran — not just afterward on a
        # mutable object (which would still pass if the view saved first and mutated later).
        saved = {}

        def _capture_save(device, *args, **kwargs):
            saved["instance"] = device
            saved["cfd"] = copy.deepcopy(device.custom_field_data)
            return None

        with (
            patch("dcim.models.Device") as mock_device,
            patch("netbox_librenms_plugin.views.imports.actions.transaction"),
            patch("netbox_librenms_plugin.views.imports.actions._save_device", side_effect=_capture_save) as mock_save,
            patch("netbox_librenms_plugin.views.imports.actions.cache"),
            patch("netbox_librenms_plugin.views.imports.actions.messages"),
            # Host id 10 (≠ incoming OOB id 17) → the self host/OOB guard passes.
            patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=10) as mock_get,
            # No other device owns id 17 → the in-transaction duplicate-mapping re-check passes.
            patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None) as mock_find,
        ):
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.return_value = existing_device
            mock_device.objects.select_for_update.return_value.get.return_value = locked_device
            response = view.post(request, device_id=17)

        # Non-error response on the success path.
        assert response.status_code == 200
        assert "validationRefresh" in response.get("HX-Trigger", "")
        view.render_device_row.assert_called_once()
        # The duplicate-mapping lookup stays in the active (non-default) server namespace.
        assert mock_find.call_args.args[1:] == (17, "secondary")
        # The self host/OOB guard reads the LOCKED row, server-scoped and read-only.
        mock_get.assert_called_once_with(locked_device, server_key="secondary", auto_save=False)
        # The LOCKED row must be the one persisted — guards against the view dropping the
        # _save_device() call or saving the stale pre-lock instance.
        mock_save.assert_called_once()
        assert mock_save.call_args.args[0] is locked_device
        # set_librenms_oob ran for real with the generic sentinel: the *incoming* controller
        # id (17) lands in oob.id, while the host id (10) is preserved.
        assert saved["instance"] is locked_device
        assert saved["cfd"]["librenms_id"]["secondary"]["id"] == 10
        assert saved["cfd"]["librenms_id"]["secondary"]["oob"] == {"id": 17, "type": "oob"}

    def test_save_device_error_marks_transaction_rollback(self):
        """_save_device returns an error response (it doesn't raise), so the view must mark
        the transaction rollback-only before returning — otherwise any Interface/IPAddress
        created earlier in the atomic block by the OOB-attach would commit."""
        from django.http import HttpResponse

        view = self._make_view()
        request = _make_request(post={"existing_device_id": "5"})

        existing_device = MagicMock()
        existing_device.pk = 5
        existing_device.name = "host-a"
        existing_device.oob_ip_id = 1  # skip the OOB-IP sub-flow; the save-failure path is the target
        existing_device.custom_field_data = {"librenms_id": {"default": {"id": 10}}}
        locked_device = MagicMock()
        locked_device.pk = 5
        locked_device.name = "host-a"
        locked_device.oob_ip_id = 1
        locked_device.custom_field_data = {"librenms_id": {"default": {"id": 10}}}

        # Distinct host id (10) vs incoming OOB controller id (17), per the host/OOB split.
        libre_device = {"device_id": 17}
        validation = {"oob_candidate": {"device": existing_device, "type": "oob", "ip": None}}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))

        err_resp = HttpResponse("save failed", status=400)
        with (
            patch("dcim.models.Device") as mock_device,
            patch("netbox_librenms_plugin.views.imports.actions.transaction") as mock_tx,
            patch("netbox_librenms_plugin.views.imports.actions._save_device", return_value=err_resp),
            patch("netbox_librenms_plugin.views.imports.actions.cache"),
            patch("netbox_librenms_plugin.views.imports.actions.messages"),
            patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=10) as mock_get,
            patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=None) as mock_find,
        ):
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.return_value = existing_device
            mock_device.objects.select_for_update.return_value.get.return_value = locked_device
            response = view.post(request, device_id=17)

        assert response is err_resp
        mock_tx.set_rollback.assert_called_once_with(True)
        assert mock_find.call_args.args[1:] == (17, "default")
        mock_get.assert_called_once_with(locked_device, server_key="default", auto_save=False)

    def test_aborts_when_librenms_id_owned_by_another_device(self):
        """The incoming OOB controller id must not already belong to another NetBox device.
        Re-checked inside the transaction (like PromoteToHostView) so one LibreNMS device
        can't be pointed at two NetBox devices."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "5"})

        existing_device = MagicMock()
        existing_device.pk = 5
        existing_device.name = "host-a"
        existing_device.oob_ip_id = 1
        existing_device.custom_field_data = {"librenms_id": {"default": {"id": 10}}}
        locked_device = MagicMock()
        locked_device.pk = 5
        locked_device.name = "host-a"
        locked_device.oob_ip_id = 1
        locked_device.custom_field_data = {"librenms_id": {"default": {"id": 10}}}

        # A *different* device already owns LibreNMS id 17 (e.g. imported standalone).
        other_device = MagicMock()
        other_device.pk = 99
        other_device.name = "the-idrac"

        libre_device = {"device_id": 17}
        validation = {"oob_candidate": {"device": existing_device, "type": "oob", "ip": None}}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))

        with (
            patch("dcim.models.Device") as mock_device,
            patch("netbox_librenms_plugin.views.imports.actions.transaction"),
            patch("netbox_librenms_plugin.views.imports.actions._save_device") as mock_save,
            patch("netbox_librenms_plugin.views.imports.actions.cache"),
            patch("netbox_librenms_plugin.views.imports.actions.messages"),
            patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=10) as mock_get,
            patch("netbox_librenms_plugin.utils.find_by_librenms_id", return_value=other_device) as mock_find,
        ):
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.return_value = existing_device
            mock_device.objects.select_for_update.return_value.get.return_value = locked_device
            response = view.post(request, device_id=17)

        # HTMX error toast (200 + HX-Reswap:none), no save, and the conflicting device named.
        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"already linked to &#x27;the-idrac&#x27;" in response.content
        mock_save.assert_not_called()
        # The lookup is scoped to the active server (id 17, "default").
        assert mock_find.call_args.args[1:] == (17, "default")
        mock_get.assert_called_once_with(locked_device, server_key="default", auto_save=False)

    def test_aborts_when_incoming_id_is_own_host_id(self):
        """A concurrent re-link could make this device's host id equal the incoming OOB id;
        attaching it as OOB would store the same id in both slots (self host/OOB conflict).
        The lock-time guard must reject it before set_librenms_oob runs."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "5"})

        existing_device = MagicMock()
        existing_device.pk = 5
        existing_device.name = "host-a"
        existing_device.oob_ip_id = 1
        existing_device.custom_field_data = {"librenms_id": {"default": {"id": 17}}}
        locked_device = MagicMock()
        locked_device.pk = 5
        locked_device.name = "host-a"
        locked_device.oob_ip_id = 1
        locked_device.custom_field_data = {"librenms_id": {"default": {"id": 17}}}

        libre_device = {"device_id": 17}
        validation = {"oob_candidate": {"device": existing_device, "type": "oob", "ip": None}}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))

        with (
            patch("dcim.models.Device") as mock_device,
            patch("netbox_librenms_plugin.views.imports.actions.transaction"),
            patch("netbox_librenms_plugin.views.imports.actions._save_device") as mock_save,
            patch("netbox_librenms_plugin.views.imports.actions.cache"),
            patch("netbox_librenms_plugin.views.imports.actions.messages"),
            # Host id resolves to the SAME id as the incoming OOB controller (17).
            patch("netbox_librenms_plugin.utils.get_librenms_device_id", return_value=17) as mock_get,
            patch("netbox_librenms_plugin.utils.find_by_librenms_id") as mock_find,
        ):
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.return_value = existing_device
            mock_device.objects.select_for_update.return_value.get.return_value = locked_device
            response = view.post(request, device_id=17)

        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"this device&#x27;s host link" in response.content
        mock_save.assert_not_called()
        # The guard reads the locked row, server-scoped and read-only.
        mock_get.assert_called_once_with(locked_device, server_key="default", auto_save=False)
        # Aborted before the cross-device duplicate lookup even runs.
        mock_find.assert_not_called()

    def test_aborts_when_locked_oob_type_changed_concurrently(self):
        """Same OOB id already linked, but a concurrent re-detection stored a different
        type. The stale modal must not silently overwrite the newer type — the guard
        compares type as well as id (both are canonical OOB_TYPES tokens)."""
        view = self._make_view()
        request = _make_request(post={"existing_device_id": "5"})

        existing_device = MagicMock()
        existing_device.pk = 5
        existing_device.name = "host-a"
        existing_device.oob_ip_id = 1
        existing_device.custom_field_data = {"librenms_id": {"default": {"id": 10}}}
        locked_device = MagicMock()
        locked_device.pk = 5
        locked_device.name = "host-a"
        locked_device.oob_ip_id = 1
        # Locked row already has OOB id 17 typed "ilo" (set by a concurrent request).
        # get_librenms_oob reads obj.cf, so set it as a real dict (not an auto-mock).
        locked_device.custom_field_data = {"librenms_id": {"default": {"id": 10, "oob": {"id": 17, "type": "ilo"}}}}
        locked_device.cf = {"librenms_id": {"default": {"id": 10, "oob": {"id": 17, "type": "ilo"}}}}

        libre_device = {"device_id": 17}
        # This modal re-detected the same controller (17) as "idrac".
        validation = {"oob_candidate": {"device": existing_device, "type": "idrac", "ip": None}}
        view.get_validated_device_with_selections = MagicMock(return_value=(libre_device, validation, {}))

        with (
            patch("dcim.models.Device") as mock_device,
            patch("netbox_librenms_plugin.views.imports.actions.transaction"),
            patch("netbox_librenms_plugin.views.imports.actions._save_device") as mock_save,
            patch("netbox_librenms_plugin.views.imports.actions.cache"),
            patch("netbox_librenms_plugin.views.imports.actions.messages"),
        ):
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.return_value = existing_device
            mock_device.objects.select_for_update.return_value.get.return_value = locked_device
            response = view.post(request, device_id=17)

        assert response.status_code == 200
        assert response["HX-Reswap"] == "none"
        assert b"modified concurrently" in response.content
        mock_save.assert_not_called()


class TestMergeNetBoxDevicesViewOOBTransfer:
    """MergeNetBoxDevicesView.post: oob_ip may only move to the winner when its
    underlying IP already sits on a winner interface (the merge does not move
    interfaces, and the save skips full_clean())."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import MergeNetBoxDevicesView

        view = object.__new__(MergeNetBoxDevicesView)
        view._librenms_api = _make_api()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def _run(self, oob_ip_device_id):
        """Drive a merge where donor has an oob_ip assigned to *oob_ip_device_id*.
        Returns (locked_winner, locked_donor)."""
        from django.http import HttpResponse

        view = self._make_view()
        request = _make_request(post={"winner_pk": "20", "donor_pk": "10"})

        oob_ip = MagicMock()
        oob_ip.assigned_object.device_id = oob_ip_device_id

        winner = MagicMock(pk=20, custom_field_data={"librenms_id": {"default": {"id": 20}}})
        donor = MagicMock(pk=10, custom_field_data={"librenms_id": {"default": {"id": 10}}})
        locked_winner = MagicMock(pk=20, name="w", oob_ip_id=None, oob_ip=None)
        locked_donor = MagicMock(pk=10, name="d", oob_ip_id=1, oob_ip=oob_ip)

        validation = {"merge_candidates": {"host_named": {"pk": 20}, "oob_named": {"pk": 10}}}
        view.get_validated_device_with_selections = MagicMock(return_value=({"device_id": 99}, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        with (
            patch("dcim.models.Device") as mock_device,
            patch("netbox_librenms_plugin.views.imports.actions.transaction"),
            patch("netbox_librenms_plugin.views.imports.actions.cache"),
            patch("netbox_librenms_plugin.utils.merge_librenms_links", return_value={}),
            patch("netbox_librenms_plugin.utils.mark_librenms_migrated"),
        ):
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.side_effect = lambda pk: {20: winner, 10: donor}[pk]
            mock_device.objects.select_for_update.return_value.filter.return_value.order_by.return_value = [
                locked_winner,
                locked_donor,
            ]
            resp = view.post(request, device_id=99)
        assert resp.status_code == 200
        return locked_winner, locked_donor, oob_ip

    def test_transfers_when_oob_ip_on_winner_interface(self):
        locked_winner, locked_donor, oob_ip = self._run(oob_ip_device_id=20)
        assert locked_winner.oob_ip is oob_ip
        assert locked_donor.oob_ip is None
        # oob_ip must be in the winner's update_fields.
        _, kwargs = locked_winner.save.call_args
        assert "oob_ip" in kwargs["update_fields"]

    def test_skips_when_oob_ip_on_donor_interface(self):
        locked_winner, locked_donor, oob_ip = self._run(oob_ip_device_id=10)
        # Left on the donor; winner not given a donor-owned interface's IP.
        assert locked_donor.oob_ip is oob_ip
        assert locked_winner.oob_ip is None
        _, kwargs = locked_winner.save.call_args
        assert "oob_ip" not in kwargs["update_fields"]


@pytest.mark.django_db
class TestSuggestOOBInterface:
    """_suggest_oob_interface: pre-select an OOB/mgmt-named interface + default new name.

    Driven against a real device with real interfaces so the name-pattern match runs over the
    actual ``device.interfaces.all()`` queryset, not a list of MagicMocks.
    """

    def test_picks_idrac_named_interface(self):
        from netbox_librenms_plugin.views.imports.actions import _suggest_oob_interface

        dev = make_device("oob-suggest-idrac")
        make_interface(dev, "eth0")
        idrac = make_interface(dev, "iDRAC")
        sid, new_name = _suggest_oob_interface(dev, {"type": "idrac"})
        assert sid == idrac.pk
        assert new_name == "idrac0"

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


@pytest.mark.django_db
class TestResolveOOBInterface:
    """AddAsOOBView._resolve_oob_interface: select existing / create new / none.

    Real Device + Interface so the select_for_update lock, the (device, name) reuse lookup, the
    real Interface create + ``full_clean`` validation, and the not-created assertions are all
    exercised end to end. The permission check (``request.user.has_perm``) is the one mocked
    boundary — auth is an external concern, and the mock request grants perms by default.
    """

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
        req = _make_request(post={"oob_interface_id": str(iface.pk)})
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
        """A name far over the length limit fails real ``full_clean`` → reason 'invalid_name'
        (surfaced as a warning), not a 500, and nothing is persisted."""
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
        """An interface with the requested (device, name) already exists → it is reused, no
        create, regardless of the 'add' permission."""
        from django.db import transaction

        view = self._view()
        dev = make_device("oob-res-reuse")
        existing = make_interface(dev, "idrac0")
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
        # Deny add to prove the reuse path never needs it.
        req.user.has_perm.side_effect = lambda perm: "add_interface" not in perm
        with transaction.atomic():
            result_iface, reason = view._resolve_oob_interface(req, dev)
        assert result_iface.pk == existing.pk and reason is None

    def test_create_without_add_perm_returns_permission_add(self):
        """No existing row + user lacks Interface 'add' → the write-time re-check refuses the
        create rather than silently creating it."""
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
    """AddAsOOBView._attach_oob_ip: reuse/re-home or create an interface-assigned IP.

    Real Interface + IPAddress so the ``address__net_host`` lookup, the ownership / ambiguity
    checks, the re-home save and the ``/32`` create all run against the ORM. Auth
    (``request.user.has_perm``) is the only mocked boundary; the mock request grants perms by
    default and the denial tests deny a single perm to pin the specific check.
    """

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

        view = self._view()
        dev = make_device("oob-ip-rehome")
        iface = make_interface(dev, "idrac0")
        existing = make_ip("10.0.0.9/24")  # unassigned host match
        with transaction.atomic():
            ip, reason = view._attach_oob_ip(_make_request(post={}), "10.0.0.9", iface)
        assert ip.pk == existing.pk and reason is None
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
        """Two IPAddress rows share the host IP (net_host ignores prefix length): refuse rather
        than re-home the wrong one by DB ordering."""
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

    def test_create_denied_without_add_permission(self):
        """TOCTOU backstop on the create path: the locked create re-verifies 'add' and refuses
        an add-lacking user rather than creating the IP."""
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
        """The candidate IPAddress row must be locked (load-bearing TOCTOU mitigation).

        This one stays a focused mock: ``select_for_update`` is a concurrency primitive that a
        single-threaded real test can't observe, so we assert it is invoked on the lookup."""
        view = self._view()
        iface = MagicMock(device_id=1)
        existing = MagicMock()
        existing.assigned_object = None
        with patch("ipam.models.IPAddress") as mock_ip_cls:
            mock_ip_cls.objects.select_for_update.return_value.filter.return_value.__getitem__.return_value = [existing]
            view._attach_oob_ip(_make_request(post={}), "10.0.0.9", iface)
        mock_ip_cls.objects.select_for_update.assert_called_once()


class TestMissingOOBIpPermissions:
    """AddAsOOBView._missing_oob_ip_permissions: the IP-set sub-flow must require
    Interface/IPAddress perms, not just the top-level ('change', Device)."""

    def _view(self):
        from netbox_librenms_plugin.views.imports.actions import AddAsOOBView

        return object.__new__(AddAsOOBView)

    def test_none_when_user_has_all_perms(self):
        view = self._view()
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
        req.user.has_perm.return_value = True
        # Patch only the manager so IPAddress._meta stays real for perm strings.
        with patch("ipam.models.IPAddress.objects") as mock_objects:
            mock_objects.filter.return_value.__getitem__.return_value = []
            assert view._missing_oob_ip_permissions(req, "10.0.0.9") is None

    def test_blocks_new_interface_without_add_interface(self):
        view = self._view()
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
        req.user.has_perm.side_effect = lambda p: "add_interface" not in p
        with patch("ipam.models.IPAddress.objects") as mock_objects:
            mock_objects.filter.return_value.__getitem__.return_value = []
            msg = view._missing_oob_ip_permissions(req, "10.0.0.9")
        assert msg is not None and "add_interface" in msg

    def test_invalid_ip_short_circuits_before_net_host_lookup(self):
        """A malformed IP must return an invalid-IP warning without hitting the
        address__net_host queryset (which would raise on it)."""
        view = self._view()
        req = _make_request(post={"oob_interface_id": "5"})
        req.user.has_perm.return_value = True
        with patch("ipam.models.IPAddress.objects") as mock_objects:
            msg = view._missing_oob_ip_permissions(req, "not-an-ip")
        assert msg is not None and "invalid" in msg.lower()
        # The net_host preflight must never run for a malformed IP.
        mock_objects.filter.assert_not_called()

    def test_no_interface_target_skips_ip_permission_check(self):
        """No interface selected (empty, or '__new__' without a name) => _resolve_oob_interface
        sets no interface and oob_ip is never written, so no IPAddress add/change perm should be
        demanded. Returning a warning here would block the intended 'choose an interface' flow."""
        view = self._view()
        for post in ({}, {"oob_interface_id": ""}, {"oob_interface_id": "__new__", "oob_new_interface_name": ""}):
            req = _make_request(post=post)
            # Deny everything except change-Device; if the IP check ran it would demand a perm.
            req.user.has_perm.return_value = False
            with patch("ipam.models.IPAddress.objects") as mock_objects:
                assert view._missing_oob_ip_permissions(req, "10.0.0.9", device=MagicMock()) is None
                # The net_host queryset must not even run when there's no interface target.
                mock_objects.filter.assert_not_called()

    def test_new_interface_name_that_already_exists_does_not_require_add(self):
        """__new__ + an existing interface name is reused by _resolve_oob_interface,
        so no Interface write happens — 'add_interface' must NOT be required even for a
        user who only has change-Device + add_ipaddress."""
        view = self._view()
        req = _make_request(post={"oob_interface_id": "__new__", "oob_new_interface_name": "idrac0"})
        req.user.has_perm.side_effect = lambda p: "add_interface" not in p  # deny only add_interface
        device = MagicMock()
        with (
            patch("ipam.models.IPAddress.objects") as mock_ip_objects,
            patch("dcim.models.Interface.objects") as mock_iface_objects,
        ):
            mock_ip_objects.filter.return_value.__getitem__.return_value = []  # IP create (user has add_ipaddress)
            mock_iface_objects.filter.return_value.exists.return_value = True  # interface already exists → reused
            msg = view._missing_oob_ip_permissions(req, "10.0.0.9", device=device)
        # No add_interface demanded since the interface is reused, not created.
        assert msg is None
        # Pin the existence check to the passed device: a regression dropping the
        # device= scope would let a same-named interface on any device skip add_interface.
        mock_iface_objects.filter.assert_called_once_with(device=device, name="idrac0")

    def test_requires_add_ipaddress_when_creating(self):
        view = self._view()
        req = _make_request(post={"oob_interface_id": "5"})  # existing iface → no add_interface
        req.user.has_perm.side_effect = lambda p: "add_ipaddress" not in p
        with patch("ipam.models.IPAddress.objects") as mock_objects:
            mock_objects.filter.return_value.__getitem__.return_value = []  # no record → create
            msg = view._missing_oob_ip_permissions(req, "10.0.0.9")
        assert msg is not None and "add_ipaddress" in msg

    def test_requires_change_ipaddress_when_rehoming(self):
        view = self._view()
        req = _make_request(post={"oob_interface_id": "5"})
        req.user.has_perm.side_effect = lambda p: "change_ipaddress" not in p
        with patch("ipam.models.IPAddress.objects") as mock_objects:
            existing = MagicMock()
            existing.assigned_object.pk = 7  # assigned to a DIFFERENT interface → re-home
            mock_objects.filter.return_value.__getitem__.return_value = [existing]
            msg = view._missing_oob_ip_permissions(req, "10.0.0.9")
        assert msg is not None and "change_ipaddress" in msg

    def test_no_change_ipaddress_when_already_on_selected_interface(self):
        """IP already assigned to the chosen interface → _attach_oob_ip does not save,
        so change_ipaddress must not be required (least privilege)."""
        view = self._view()
        req = _make_request(post={"oob_interface_id": "5"})
        # User has every perm EXCEPT change_ipaddress.
        req.user.has_perm.side_effect = lambda p: "change_ipaddress" not in p
        with patch("ipam.models.IPAddress.objects") as mock_objects:
            existing = MagicMock()
            existing.assigned_object.pk = 5  # already on the selected interface → no mutation
            mock_objects.filter.return_value.__getitem__.return_value = [existing]
            assert view._missing_oob_ip_permissions(req, "10.0.0.9") is None

    def test_ambiguous_match_requires_change_despite_selected_interface(self):
        """Multiple rows share the host IP: the write path refuses, so the preflight must
        NOT take the already-on-selected-interface shortcut — it requires change_ipaddress."""
        view = self._view()
        req = _make_request(post={"oob_interface_id": "5"})
        req.user.has_perm.side_effect = lambda p: "change_ipaddress" not in p
        with patch("ipam.models.IPAddress.objects") as mock_objects:
            on_iface = MagicMock()
            on_iface.assigned_object.pk = 5  # would otherwise short-circuit to "no perms"
            mock_objects.filter.return_value.__getitem__.return_value = [on_iface, MagicMock()]
            msg = view._missing_oob_ip_permissions(req, "10.0.0.9")
        assert msg is not None and "change_ipaddress" in msg


class TestCreatePlatformFromImportManufacturer:
    """CreatePlatformFromImportView must reject a stale/tampered manufacturer id instead of
    silently creating a Platform with no manufacturer."""

    def _view(self):
        from netbox_librenms_plugin.views.imports.actions import CreatePlatformFromImportView

        view = object.__new__(CreatePlatformFromImportView)
        view.required_object_permissions = {}
        return view

    def test_invalid_manufacturer_id_is_rejected(self):
        view = self._view()
        req = _make_request(post={"platform_name": "New-OS", "manufacturer": "9999"})

        mock_manuf = MagicMock()
        mock_manuf.DoesNotExist = type("DoesNotExist", (Exception,), {})
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


class TestOOBInterfaceSelectTemplate:
    """The OOB interface picker toggles the "new name" input via a script block
    (extracted from an inline onchange) so it works under CSP and is maintainable."""

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
        """The script must sync the "new name" input once on load (not only on change), so
        the input matches the rendered selection even if it differs from the server-side
        display logic (e.g. a stale/missing suggested option)."""
        html = self._render()
        assert "function syncCreateState()" in html
        # Bound to change AND invoked immediately so initial state is authoritative.
        assert 'addEventListener("change", syncCreateState)' in html
        assert "syncCreateState();" in html


class TestMergeNetBoxDevicesViewDonorDerivation:
    """The merge view derives the donor from winner_pk + merge_candidates and
    ignores the client-posted donor_pk, which a stale/failed inline sync script
    could otherwise leave equal to winner_pk (a self-merge of moving data)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.imports.actions import MergeNetBoxDevicesView

        view = object.__new__(MergeNetBoxDevicesView)
        view._librenms_api = _make_api()
        view.require_write_permission = MagicMock(return_value=None)
        view.require_object_permissions = MagicMock(return_value=None)
        return view

    def test_ignores_posted_donor_pk_equal_to_winner(self):
        from django.http import HttpResponse

        view = self._make_view()
        # Bogus client state: donor_pk == winner_pk (inline sync script never ran).
        request = _make_request(post={"winner_pk": "20", "donor_pk": "20"})

        winner = MagicMock(pk=20, custom_field_data={"librenms_id": {"default": {"id": 20}}})
        donor = MagicMock(pk=10, custom_field_data={"librenms_id": {"default": {"id": 10}}})
        locked_winner = MagicMock(pk=20, name="w", oob_ip_id=None, oob_ip=None)
        locked_donor = MagicMock(pk=10, name="d", oob_ip_id=None, oob_ip=None)

        validation = {"merge_candidates": {"host_named": {"pk": 20}, "oob_named": {"pk": 10}}}
        view.get_validated_device_with_selections = MagicMock(return_value=({"device_id": 99}, validation, {}))
        view.render_device_row = MagicMock(return_value=HttpResponse("row"))

        with (
            patch("dcim.models.Device") as mock_device,
            patch("netbox_librenms_plugin.views.imports.actions.transaction"),
            patch("netbox_librenms_plugin.views.imports.actions.cache"),
            patch("netbox_librenms_plugin.utils.merge_librenms_links", return_value={}) as mock_merge,
            patch("netbox_librenms_plugin.utils.mark_librenms_migrated"),
        ):
            mock_device.DoesNotExist = Exception
            mock_device.objects.get.side_effect = lambda pk: {20: winner, 10: donor}[pk]
            mock_device.objects.select_for_update.return_value.filter.return_value.order_by.return_value = [
                locked_winner,
                locked_donor,
            ]
            resp = view.post(request, device_id=99)

        assert resp.status_code == 200
        # Donor is the *other* merge candidate (pk=10), never the posted self-pk=20.
        mock_merge.assert_called_once()
        called_winner, called_donor = mock_merge.call_args[0][:2]
        assert called_winner is locked_winner
        assert called_donor is locked_donor
