"""Legacy stored serials: they keep matching under the seeded rule, and the report lists the rows a rule changes."""

from io import StringIO

import pytest


def _rules_only(*rules):
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
    for pattern in rules:
        NormalizationRule.objects.create(scope="serial", match_pattern=pattern, replacement=r"\1")


def _report():
    from django.core.management import call_command

    out = StringIO()
    call_command("librenms_serial_report", stdout=out)
    return out.getvalue().splitlines()


@pytest.mark.django_db
class TestSerialReportCommand:
    """The legacy stored serials and the report of the rows that a rule changes."""

    def test_legacy_rule_output_keeps_matching_the_librenms_text_under_the_seeded_rule(self):
        """An older release stored "LEGACY-SN1" for "S/N LEGACY-SN1" on Modules and on chassis members."""
        from netbox_librenms_plugin.serial_identity import resolve_device, resolve_module
        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_device_with_module_bays

        _rules_only(r"^S/N\s+(.+)$")
        device = make_device_with_module_bays("legacy-module-holder", ["Slot 1"])
        module = install_module(device, "Slot 1", "LEGACY-CARD", serial="LEGACY-SN1")
        member = make_device("legacy-keyed-member", serial="MEMBER-SN1")

        assert resolve_module("S/N LEGACY-SN1").object == module
        assert resolve_device("S/N MEMBER-SN1").object == member

    def test_the_report_lists_the_rows_whose_stored_text_the_rules_change(self):
        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_device_with_module_bays

        _rules_only(r"^0(.+)$", r"^S/N\s+(.+)$")
        # "00ABC12" was stored as its rule output "0ABC12"; the rule matches that output again.
        rematched = make_device("report-rematched", serial="0ABC12")
        decorated = make_device("report-decorated", serial="S/N REPORT-SN1")
        make_device("report-plain", serial="PLAIN-SN1")
        short = make_device("report-short", serial="0AB")
        holder = make_device_with_module_bays("report-module-holder", ["Slot 1"])
        module = install_module(holder, "Slot 1", "REPORT-CARD", serial="0MOD-SN1")

        lines = _report()

        expected = [f"device {rematched.pk}", f"device {decorated.pk}", f"device {short.pk}", f"module {module.pk}"]
        assert sorted(line.split(" (", 1)[0] for line in lines[:-1]) == sorted(expected)
        assert lines[-1].startswith(f"{len(expected)} row(s)")

    def test_the_report_lists_a_padded_or_placeholder_stored_text(self):
        """A writer stores neither padding nor a placeholder, so the key of such a stored text differs from it."""
        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_device_with_module_bays

        _rules_only(r"^S/N\s+(.+)$")
        padded = make_device("report-padded", serial="  PADDED-SN1  ")
        placeholder = make_device("report-placeholder", serial="N/A")
        make_device("report-exact", serial="EXACT-SN1")
        holder = make_device_with_module_bays("report-padded-holder", ["Slot 1"])
        module = install_module(holder, "Slot 1", "REPORT-PADDED-CARD", serial="MOD-SN1 ")

        lines = _report()

        expected = [f"device {padded.pk}", f"device {placeholder.pk}", f"module {module.pk}"]
        assert sorted(line.split(" (", 1)[0] for line in lines[:-1]) == sorted(expected)
        rows = {line.split(" (", 1)[0]: line for line in lines[:-1]}
        assert "stored '  PADDED-SN1  ' keys to 'PADDED-SN1'" in rows[f"device {padded.pk}"]
        assert "stored 'N/A' no hardware identity" in rows[f"device {placeholder.pk}"]
        assert lines[-1].startswith("3 row(s)")

    def test_a_broken_rule_reports_the_rows_of_its_chain_and_the_scan_goes_on(self):
        from dcim.models import DeviceType, Manufacturer

        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_device_with_module_bays

        _rules_only(r"^S/N\s+(.+)$")
        broken_holder = make_device("report-broken-chain", serial="BROKEN-SN1")
        broken_module = install_module(
            make_device_with_module_bays("report-broken-holder", ["Slot 1"]), "Slot 1", "BROKEN-CARD", serial="CARD-SN1"
        )
        other_maker = Manufacturer.objects.create(name="Report Other Maker", slug="report-other-maker")
        other_type = DeviceType.objects.create(manufacturer=other_maker, model="Report Other", slug="report-other")
        decorated = make_device("report-other-chain", serial="S/N OTHER-SN1")
        decorated.device_type = other_type
        decorated.save()
        rule = NormalizationRule.objects.create(
            scope="serial",
            manufacturer=broken_holder.device_type.manufacturer,
            match_pattern=r"^(.+)$",
            replacement=r"\1",
        )
        # A QuerySet.update() skips clean(), as an upgrade from an older release can leave the rule.
        NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"X\1")

        lines = _report()

        expected = [f"device {broken_holder.pk}", f"module {broken_module.pk}", f"device {decorated.pk}"]
        assert sorted(line.split(" (", 1)[0] for line in lines[:-1]) == sorted(expected)
        rows = {line.split(" (", 1)[0]: line for line in lines[:-1]}
        assert f"(ID {rule.pk})" in rows[f"device {broken_holder.pk}"]
        assert f"(ID {rule.pk})" in rows[f"module {broken_module.pk}"]
        assert "keys to 'OTHER-SN1'" in rows[f"device {decorated.pk}"]
        assert lines[-1].startswith("3 row(s)")

    def test_the_report_does_not_cache_serials_from_the_scan(self):
        from dcim.models import Device, Module

        from netbox_librenms_plugin.serial_identity import SerialRules, is_identity, report_rows
        from netbox_librenms_plugin.tests.conftest import make_device, make_module_bay, make_module_type

        _rules_only(r"^S/N\s+(.+)$")
        holder = make_device("report-scan-holder")
        module_type = make_module_type("REPORT-SCAN-CARD")
        Device.objects.bulk_create(
            [
                Device(
                    name=f"report-scan-device-{number}",
                    serial=f"DEVICE-SN{number}",
                    device_type=holder.device_type,
                    role=holder.role,
                    site=holder.site,
                    status=holder.status,
                )
                for number in range(501)
            ]
        )
        bays = [make_module_bay(holder, f"Slot {number}") for number in range(501)]
        Module.objects.bulk_create(
            [
                Module(
                    device=holder, module_bay=bay, module_type=module_type, serial=f"MODULE-SN{number}", status="active"
                )
                for number, bay in enumerate(bays)
            ]
        )
        rules = SerialRules.load()
        context = rules.for_device(holder)
        assert is_identity("KNOWN-SN1", context)

        assert list(report_rows(rules)) == []
        assert set(context._keys) == {"KNOWN-SN1"}

    def test_an_unchecked_row_counts_as_reported(self):
        from netbox_librenms_plugin.models import NormalizationRule
        from netbox_librenms_plugin.tests.conftest import make_device

        _rules_only(r"^(.+)$")
        rule = NormalizationRule.objects.get(scope="serial")
        NormalizationRule.objects.filter(pk=rule.pk).update(replacement=r"X\1")
        make_device("report-unchecked", serial="UNCHECKED-SN1")

        lines = _report()

        assert f"(ID {rule.pk})" in lines[0]
        assert lines[-1] == "1 row(s) reported."

    def test_a_rule_that_matches_its_own_output_can_stop_a_legacy_row_from_matching(self):
        """The documented limit: the original "00ABC12" no longer finds its stored rule output "0ABC12"."""
        from netbox_librenms_plugin.serial_identity import ResolutionKind, resolve_device
        from netbox_librenms_plugin.tests.conftest import make_device

        _rules_only(r"^0(.+)$")
        make_device("rematch-limit", serial="0ABC12")

        assert resolve_device("00ABC12").kind is ResolutionKind.NO_MATCH
