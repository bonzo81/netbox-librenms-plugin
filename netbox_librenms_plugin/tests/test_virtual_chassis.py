"""Tests for import_utils/virtual_chassis.py."""

from unittest.mock import MagicMock, patch


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
