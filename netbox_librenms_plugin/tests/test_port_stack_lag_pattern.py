"""Tests for PortStackLagPattern model."""

import pytest
from django.core.exceptions import ValidationError


@pytest.mark.django_db
class TestPortStackLagPattern:
    """Exercise the real ORM path: save() -> FullCleanOnSaveMixin.full_clean() -> clean() plus the unique librenms_os constraint, rather than a __new__ + patched-clean stand-in that bypasses validation entirely."""

    def _model(self):
        from netbox_librenms_plugin.models import PortStackLagPattern

        return PortStackLagPattern

    def test_migration_seeded_the_lowercased_default_patterns(self):
        """Migration 0011's RunPython seed actually committed rows through the ORM (e.g. the lower-cased 'ios' default) — a real-DB check the __new__/patched-clean stand-in could never make."""
        assert self._model().objects.filter(librenms_os="ios", lag_name_pattern=r"^Po\d+$").exists()

    def test_save_normalizes_os_and_pattern(self):
        """A real save lower-cases/strips librenms_os and strips lag_name_pattern; the normalized row round-trips from the DB."""
        obj = self._model().objects.create(librenms_os="  ZZNORM  ", lag_name_pattern=r"  ^Po\d+$  ")
        obj.refresh_from_db()
        assert obj.librenms_os == "zznorm"
        assert obj.lag_name_pattern == r"^Po\d+$"

    def test_str_representation(self):
        obj = self._model()(librenms_os="ios", lag_name_pattern=r"^Po\d+$")
        assert str(obj) == r"ios -> ^Po\d+$"

    def test_save_rejects_invalid_regex_and_does_not_persist(self):
        """An invalid regex must raise on save (via full_clean) AND leave no row behind."""
        model = self._model()
        with pytest.raises(ValidationError) as exc_info:
            model.objects.create(librenms_os="zzbadregex", lag_name_pattern="[invalid(regex")
        assert "lag_name_pattern" in exc_info.value.message_dict
        assert not model.objects.filter(librenms_os="zzbadregex").exists()

    def test_save_rejects_blank_os(self):
        with pytest.raises(ValidationError) as exc_info:
            self._model().objects.create(librenms_os="   ", lag_name_pattern=r"^Po\d+$")
        assert "librenms_os" in exc_info.value.message_dict

    def test_save_rejects_blank_pattern(self):
        with pytest.raises(ValidationError) as exc_info:
            self._model().objects.create(librenms_os="zzblankpat", lag_name_pattern="   ")
        assert "lag_name_pattern" in exc_info.value.message_dict

    def test_unique_librenms_os_rejects_case_variant_duplicate(self):
        """librenms_os is unique AND lower-cased in clean(), so a case-variant duplicate ('ZZDUP' for an existing 'zzdup') must be rejected by full_clean's unique check — not silently inserted as a second row."""
        model = self._model()
        model.objects.create(librenms_os="zzdup", lag_name_pattern=r"^Po\d+$")
        with pytest.raises(ValidationError) as exc_info:
            model.objects.create(librenms_os="ZZDUP", lag_name_pattern=r"^Bundle-Ether\d+$")
        assert "librenms_os" in exc_info.value.message_dict
        assert model.objects.filter(librenms_os="zzdup").count() == 1


@pytest.mark.django_db
class TestHasLagSignalsFieldSelection:
    """_has_lag_signals() must scan the user-selected interface_name_field (plus ifName/ ifDescr), not just ifName."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        return object.__new__(BaseInterfaceTableView)

    def test_subiface_signal_in_ifdescr_detected(self):
        """ifDescr-driven device: the sub-interface base+child names live only in ifDescr."""
        view = self._make_view()
        ports = [
            {"ifName": "", "ifDescr": "ge-0/0/0"},  # parent
            {"ifName": "", "ifDescr": "ge-0/0/0.100"},  # sub-interface child
        ]
        # Default (ifName/ifDescr) already covers the ifDescr-driven case CR flagged.
        assert view._has_lag_signals(ports) is True
        assert view._has_lag_signals(ports, "ifDescr") is True

    def test_field_parameter_changes_outcome(self):
        """The signal lives only in a non-default field (ifAlias)."""
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

    def test_propvirtual_alone_does_not_trigger_fetch(self):
        """A propVirtual ifType is NOT a LAG signal: loopbacks/SVIs/tunnels are propVirtual, so gating on it fired the lazy port_stack/device_info round-trips for nearly every device. Only ieee8023adLag, a name-pattern match, or a real sub-interface should gate the fetch — matching what the resolver can actually classify."""
        view = self._make_view()
        # Lone propVirtual ports whose names match no PortStackLagPattern and have no
        # sub-interface child: must NOT trigger the fetch (the old code returned True here).
        virtuals = [
            {"ifName": "Loopback0", "ifType": "propVirtual"},
            {"ifName": "Vlan100", "ifType": "propVirtual"},
        ]
        assert view._has_lag_signals(virtuals) is False
        # A structural aggregate (ieee8023adLag) still triggers it, regardless of name.
        assert view._has_lag_signals([{"ifName": "agg-x", "ifType": "ieee8023adLag"}]) is True
        # A propVirtual port-channel whose name matches a seeded pattern (^Po\\d+$) still
        # triggers it via the name-pattern branch — so real IOS LAGs are unaffected.
        assert view._has_lag_signals([{"ifName": "Po10", "ifType": "propVirtual"}]) is True
