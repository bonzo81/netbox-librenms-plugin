"""Legacy stored serials: they keep matching under the seeded rule, and the report lists the rows a rule changes."""

from io import StringIO

import pytest
from django.core.management import call_command

from netbox_librenms_plugin.serial_identity import ResolutionKind, resolve_device, resolve_module
from netbox_librenms_plugin.tests.conftest import install_module, make_device, make_device_with_module_bays

pytestmark = pytest.mark.django_db


def _rules_only(*rules):
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.filter(scope=NormalizationRule.SCOPE_SERIAL).delete()
    for pattern in rules:
        NormalizationRule.objects.create(scope="serial", match_pattern=pattern, replacement=r"\1")


def _report():
    out = StringIO()
    call_command("librenms_serial_report", stdout=out)
    return out.getvalue().splitlines()


def test_legacy_rule_output_keeps_matching_the_librenms_text_under_the_seeded_rule():
    """An older release stored "LEGACY-SN1" for "S/N LEGACY-SN1" on Modules and on chassis members."""
    _rules_only(r"^S/N\s+(.+)$")
    device = make_device_with_module_bays("legacy-module-holder", ["Slot 1"])
    module = install_module(device, "Slot 1", "LEGACY-CARD", serial="LEGACY-SN1")
    member = make_device("legacy-keyed-member", serial="MEMBER-SN1")

    assert resolve_module("S/N LEGACY-SN1").object == module
    assert resolve_device("S/N MEMBER-SN1").object == member


def test_the_report_lists_the_rows_whose_stored_text_the_rules_change():
    _rules_only(r"^0(.+)$", r"^S/N\s+(.+)$")
    # "00ABC12" was stored as its rule output "0ABC12"; the rule matches that output again.
    rematched = make_device("report-rematched", serial="0ABC12")
    decorated = make_device("report-decorated", serial="S/N REPORT-SN1")
    plain = make_device("report-plain", serial="PLAIN-SN1")
    short = make_device("report-short", serial="0AB")
    holder = make_device_with_module_bays("report-module-holder", ["Slot 1"])
    module = install_module(holder, "Slot 1", "REPORT-CARD", serial="0MOD-SN1")

    lines = _report()

    listed = {line.split(" (", 1)[0] for line in lines[:-1]}
    assert f"device {rematched.pk}" in listed
    assert f"device {decorated.pk}" in listed
    assert f"device {short.pk}" in listed
    assert f"module {module.pk}" in listed
    assert f"device {plain.pk}" not in listed
    assert lines[-1].startswith(f"{len(listed)} row(s)")


def test_a_rule_that_matches_its_own_output_can_stop_a_legacy_row_from_matching():
    """The documented limit: the original "00ABC12" no longer finds its stored rule output "0ABC12"."""
    _rules_only(r"^0(.+)$")
    make_device("rematch-limit", serial="0ABC12")

    assert resolve_device("00ABC12").kind is ResolutionKind.NO_MATCH
