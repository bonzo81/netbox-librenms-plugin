"""Tests for import_utils/virtual_chassis.py."""

import pytest


@pytest.mark.django_db
class TestLoadVcMemberNamePattern:
    """_load_vc_member_name_pattern must return valid string or default."""

    DEFAULT = "-M{position}"

    @staticmethod
    def _settings_model():
        from netbox_librenms_plugin.models import LibreNMSSettings

        return LibreNMSSettings

    def _call(self):
        from netbox_librenms_plugin.import_utils.virtual_chassis import _load_vc_member_name_pattern

        return _load_vc_member_name_pattern()

    def _store_pattern(self, pattern):
        """Persist a single real LibreNMSSettings row carrying the given pattern."""
        settings_model = self._settings_model()
        settings_model.objects.all().delete()
        settings_model.objects.create(vc_member_name_pattern=pattern)

    def test_returns_valid_pattern(self):
        """A configured non-empty pattern is read back verbatim from the real settings row."""
        self._store_pattern("-SW{position}")
        assert self._call() == "-SW{position}"

    def test_returns_default_for_empty_string(self):
        """An empty-string pattern in the real settings row falls back to the default."""
        self._store_pattern("")
        assert self._call() == self.DEFAULT

    def test_returns_default_for_whitespace_only(self):
        """A whitespace-only pattern in the real settings row falls back to the default."""
        self._store_pattern("   ")
        assert self._call() == self.DEFAULT

    def test_returns_default_when_no_settings(self):
        """With no settings row persisted, the loader falls back to the default."""
        self._settings_model().objects.all().delete()
        assert self._call() == self.DEFAULT

    def test_returns_default_on_exception(self, monkeypatch):
        """A DB error while loading settings falls back to the default."""
        settings_model = self._settings_model()

        def fail_query(*_args, **_kwargs):
            raise RuntimeError("db error")

        monkeypatch.setattr(settings_model.objects, "order_by", fail_query)
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

    @pytest.mark.django_db
    def test_none_pattern_loads_from_settings(self):
        """When pattern is None, the persisted pattern is loaded."""
        from netbox_librenms_plugin.models import LibreNMSSettings

        LibreNMSSettings.objects.all().delete()
        LibreNMSSettings.objects.create(vc_member_name_pattern="-STACK{position}")
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
