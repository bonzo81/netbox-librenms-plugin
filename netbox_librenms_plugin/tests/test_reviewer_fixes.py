"""Regression tests for reviewer-requested fixes.

Covers: _load_vc_member_name_pattern validation, _generate_vc_member_name pattern
handling, render_device_selection XSS escape, import_single_device lazy validation
api passthrough, CreateAndAssignPlatformView full_clean before save.
"""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _load_vc_member_name_pattern
# ---------------------------------------------------------------------------
class TestLoadVcMemberNamePattern:
    """_load_vc_member_name_pattern must return valid string or default."""

    DEFAULT = "-M{position}"

    def _call(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern

        return _load_vc_member_name_pattern()

    def _patch_settings(self, settings_obj):
        """Patch the deferred import of LibreNMSSettings inside the function."""
        return patch(
            "netbox_librenms_plugin.models.LibreNMSSettings.objects",
            **{"order_by.return_value.first.return_value": settings_obj},
        )

    def test_returns_valid_pattern(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = "-SW{position}"
        with self._patch_settings(settings):
            assert self._call() == "-SW{position}"

    def test_returns_default_for_none_pattern(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = None
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_for_empty_string(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = ""
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_for_whitespace_only(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = "   "
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_for_boolean(self):
        settings = MagicMock()
        settings.vc_member_name_pattern = True
        with self._patch_settings(settings):
            assert self._call() == self.DEFAULT

    def test_returns_default_when_no_settings(self):
        with self._patch_settings(None):
            assert self._call() == self.DEFAULT

    def test_returns_default_on_exception(self):
        with patch(
            "netbox_librenms_plugin.models.LibreNMSSettings.objects",
        ) as mock_objs:
            mock_objs.order_by.side_effect = RuntimeError("db error")
            assert self._call() == self.DEFAULT


# ---------------------------------------------------------------------------
# render_device_selection — XSS escape
# ---------------------------------------------------------------------------
class TestRenderDeviceSelectionEscape:
    """render_device_selection must HTML-escape member.name."""

    def test_member_name_is_escaped(self):
        from netbox_librenms_plugin.tables.cables import VCCableTable

        device = MagicMock()
        device.id = 1
        vc = MagicMock()
        member = MagicMock()
        member.id = 1
        member.name = '<script>alert("xss")</script>'
        vc.members.all.return_value = [member]
        device.virtual_chassis = vc

        table = VCCableTable([], device=device)
        record = {"local_port": "eth0", "local_port_id": "42"}

        with patch(
            "netbox_librenms_plugin.tables.cables.get_virtual_chassis_member",
            return_value=member,
        ):
            html = str(table.render_device_selection(None, record))

        # The raw <script> tag must NOT appear — it should be escaped
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# _generate_vc_member_name — pattern handling
# ---------------------------------------------------------------------------
class TestGenerateVcMemberName:
    """_generate_vc_member_name must respect caller-supplied pattern and catch format errors."""

    def _call(self, master_name, position, serial=None, pattern=None):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _generate_vc_member_name

        return _generate_vc_member_name(master_name, position, serial=serial, pattern=pattern)

    def test_explicit_pattern_used(self):
        """When pattern is passed, it should be used directly (no DB query)."""
        result = self._call("switch01", 2, pattern="-SW{position}")
        assert result == "switch01-SW2"

    def test_serial_in_pattern(self):
        result = self._call("switch01", 2, serial="ABC123", pattern=" [{serial}]")
        assert result == "switch01 [ABC123]"

    def test_none_pattern_loads_from_settings(self):
        """When pattern is None, _load_vc_member_name_pattern is called."""
        with patch(
            "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
            return_value="-STACK{position}",
        ):
            result = self._call("core01", 3, pattern=None)
        assert result == "core01-STACK3"

    def test_malformed_pattern_falls_back_to_default(self):
        """Invalid format spec falls back to -M{position}."""
        result = self._call("switch01", 2, pattern="{position!z}")
        assert result == "switch01-M2"

    def test_missing_key_falls_back_to_default(self):
        """Unknown placeholder falls back to -M{position}."""
        result = self._call("switch01", 2, pattern="-{unknown_key}")
        assert result == "switch01-M2"

    def test_default_pattern(self):
        result = self._call("switch01", 2, pattern="-M{position}")
        assert result == "switch01-M2"


# ---------------------------------------------------------------------------
# import_single_device — lazy validation passes api
# ---------------------------------------------------------------------------
class TestImportSingleDeviceLazyValidation:
    """import_single_device must pass api=api to validate_device_for_import when validation is None."""

    def test_api_passed_to_validate(self):
        from netbox_librenms_plugin.import_utils.device_operations import import_single_device

        mock_api = MagicMock()
        mock_api.server_key = "prod"

        mock_validation = {
            "existing_device": MagicMock(name="existing"),
            "can_import": False,
        }

        with (
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.LibreNMSAPI",
                return_value=mock_api,
            ),
            patch(
                "netbox_librenms_plugin.import_utils.device_operations.validate_device_for_import",
                return_value=mock_validation,
            ) as mock_validate,
        ):
            # Call with validation=None so lazy path triggers
            import_single_device(
                42,
                server_key="prod",
                sync_options={"use_sysname": True, "strip_domain": False},
                validation=None,
                libre_device={"device_id": 42, "hostname": "test"},
            )

            mock_validate.assert_called_once()
            # api must be passed as keyword arg
            assert mock_validate.call_args[1].get("api") is mock_api


# ---------------------------------------------------------------------------
# CreateAndAssignPlatformView — full_clean before save
# ---------------------------------------------------------------------------
class TestCreatePlatformFullClean:
    """CreateAndAssignPlatformView must call full_clean() so ValidationError is catchable."""

    def test_validation_error_caught_on_slug_collision(self):
        """When full_clean raises ValidationError, user sees error message instead of 500."""
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.views.sync.device_fields import CreateAndAssignPlatformView

        view = object.__new__(CreateAndAssignPlatformView)

        request = MagicMock()
        request.method = "POST"
        request.POST = {"platform_name": "test-platform"}
        request.user.has_perm.return_value = True
        view.request = request

        with (
            patch("netbox_librenms_plugin.views.sync.device_fields.get_object_or_404"),
            patch("netbox_librenms_plugin.views.sync.device_fields.Manufacturer"),
            patch("netbox_librenms_plugin.views.sync.device_fields.Platform") as MockPlatform,
            patch("netbox_librenms_plugin.views.sync.device_fields.transaction"),
            patch("netbox_librenms_plugin.views.sync.device_fields.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.sync.device_fields.redirect"),
        ):
            # objects.filter().exists() returns False (no existing platform)
            MockPlatform.objects.filter.return_value.exists.return_value = False
            # Simulate full_clean raising ValidationError (slug collision)
            platform_instance = MagicMock()
            platform_instance.full_clean.side_effect = ValidationError("Slug already exists")
            MockPlatform.return_value = platform_instance

            view.post(request, pk=1)

            # full_clean must have been called (not just .create())
            platform_instance.full_clean.assert_called_once()
            # save must NOT have been called (ValidationError raised before save)
            platform_instance.save.assert_not_called()
            # Error message should be shown to user with the actual validation detail
            mock_messages.error.assert_called_once()
            error_msg = mock_messages.error.call_args[0][1]
            assert "could not be created" in error_msg
            assert "Slug already exists" in error_msg
