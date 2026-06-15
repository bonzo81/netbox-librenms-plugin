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


@pytest.mark.django_db
class TestHasLagSignalsFieldSelection:
    """_has_lag_signals() must scan the user-selected interface_name_field (plus ifName/
    ifDescr), not just ifName. On an ifDescr-driven device the LAG/sub-interface signal lives
    in ifDescr; keying off ifName alone silently skips the port_stack fetch and empties the
    Parent/LAG column. Exercises the real method against a real (empty) PortStackLagPattern
    table via the pattern-free sub-interface detection path."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        return object.__new__(BaseInterfaceTableView)

    def test_subiface_signal_in_ifdescr_detected(self):
        """ifDescr-driven device: the sub-interface base+child names live only in ifDescr.
        The old ifName-only scan returned False here; now it must be detected."""
        view = self._make_view()
        ports = [
            {"ifName": "", "ifDescr": "ge-0/0/0"},  # parent
            {"ifName": "", "ifDescr": "ge-0/0/0.100"},  # sub-interface child
        ]
        # Default (ifName/ifDescr) already covers the ifDescr-driven case CR flagged.
        assert view._has_lag_signals(ports) is True
        assert view._has_lag_signals(ports, "ifDescr") is True

    def test_field_parameter_changes_outcome(self):
        """The signal lives only in a non-default field (ifAlias). It is detected ONLY when
        that field is the selected interface_name_field — proving the parameter is consulted
        rather than ifName being hardcoded."""
        view = self._make_view()
        ports = [
            {"ifAlias": "ae0"},  # parent
            {"ifAlias": "ae0.100"},  # sub-interface child
        ]
        # ifAlias is neither ifName nor ifDescr, so the default scan misses it...
        assert view._has_lag_signals(ports) is False
        # ...but passing it as the selected field surfaces the LAG signal.
        assert view._has_lag_signals(ports, "ifAlias") is True

    def test_no_signal_returns_false(self):
        """Plain access ports with no LAG/sub-interface signal stay False (not vacuously True)."""
        view = self._make_view()
        ports = [
            {"ifName": "Gi0/0", "ifDescr": "GigabitEthernet0/0", "ifType": "ethernetCsmacd"},
            {"ifName": "Gi0/1", "ifDescr": "GigabitEthernet0/1", "ifType": "ethernetCsmacd"},
        ]
        assert view._has_lag_signals(ports, "ifDescr") is False
