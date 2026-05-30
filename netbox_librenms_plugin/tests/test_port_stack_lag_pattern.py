"""Tests for PortStackLagPattern model."""

import pytest
from django.core.exceptions import ValidationError
from unittest.mock import patch


class TestPortStackLagPattern:
    def _make(self, librenms_os="ios", lag_name_pattern=r"^Po\d+$"):
        from netbox_librenms_plugin.models import PortStackLagPattern

        obj = PortStackLagPattern.__new__(PortStackLagPattern)
        obj.librenms_os = librenms_os
        obj.lag_name_pattern = lag_name_pattern
        obj.description = ""
        return obj

    def test_str_representation(self):
        obj = self._make(librenms_os="ios", lag_name_pattern=r"^Po\d+$")
        assert str(obj) == r"ios -> ^Po\d+$"

    def test_clean_rejects_invalid_regex(self):
        obj = self._make(lag_name_pattern="[invalid(regex")
        with patch("netbox.models.NetBoxModel.clean"):
            with pytest.raises(ValidationError) as exc_info:
                obj.clean()
        assert "lag_name_pattern" in exc_info.value.message_dict

    def test_clean_accepts_valid_regex(self):
        obj = self._make()
        with patch("netbox.models.NetBoxModel.clean"):
            obj.clean()  # should not raise

    def test_clean_rejects_blank_os(self):
        obj = self._make(librenms_os="")
        with patch("netbox.models.NetBoxModel.clean"):
            with pytest.raises(ValidationError) as exc_info:
                obj.clean()
        assert "librenms_os" in exc_info.value.message_dict

    def test_clean_rejects_blank_pattern(self):
        obj = self._make(lag_name_pattern="")
        with patch("netbox.models.NetBoxModel.clean"):
            with pytest.raises(ValidationError) as exc_info:
                obj.clean()
        assert "lag_name_pattern" in exc_info.value.message_dict

    def test_clean_normalizes_os(self):
        obj = self._make(librenms_os="  IOS  ")
        with patch("netbox.models.NetBoxModel.clean"):
            obj.clean()
        assert obj.librenms_os == "ios"

    def test_clean_normalizes_lag_pattern(self):
        obj = self._make(lag_name_pattern=r"  ^Po\d+$  ")
        with patch("netbox.models.NetBoxModel.clean"):
            obj.clean()
        assert obj.lag_name_pattern == r"^Po\d+$"
