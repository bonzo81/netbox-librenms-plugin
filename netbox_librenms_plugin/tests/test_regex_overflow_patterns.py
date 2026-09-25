"""A pattern with a too large repeat count is an invalid pattern, not a crash (``re.compile`` raises OverflowError)."""

import pytest

from netbox_librenms_plugin.forms import ImportSettingsForm
from netbox_librenms_plugin.models import (
    CarrierAutoInstallRule,
    InventoryIgnoreRule,
    ModuleBayMapping,
    NormalizationRule,
    PortStackLagPattern,
)
from netbox_librenms_plugin.utils import apply_normalization_rules, parse_librenms_location

OVERFLOW_PATTERN = "a{4294967295}"


@pytest.mark.django_db
def test_the_settings_form_rejects_a_location_regex_with_a_too_large_repeat_count():
    form = ImportSettingsForm(
        data={"location_parse_pattern": f"(?P<site>{OVERFLOW_PATTERN})", "location_parse_is_regex": "on"}
    )

    assert not form.is_valid()
    assert form.errors["location_parse_pattern"][0].startswith("Invalid regular expression:")


def test_a_location_regex_with_a_too_large_repeat_count_parses_to_nothing():
    result = parse_librenms_location("NYC", f"(?P<site>{OVERFLOW_PATTERN})", is_regex=True)

    assert all(value is None for value in result.values())


def test_a_normalization_rule_with_a_too_large_repeat_count_is_skipped():
    rules = [
        NormalizationRule(scope=NormalizationRule.SCOPE_MODULE_TYPE, match_pattern=OVERFLOW_PATTERN, replacement="x"),
        NormalizationRule(scope=NormalizationRule.SCOPE_MODULE_TYPE, match_pattern="^SFP", replacement="XFP"),
    ]

    result = apply_normalization_rules(
        "SFP-10G",
        NormalizationRule.SCOPE_MODULE_TYPE,
        preloaded_rules={(NormalizationRule.SCOPE_MODULE_TYPE, None): rules},
    )

    assert result == "XFP-10G"


@pytest.mark.parametrize(
    "rule, attribute",
    [
        (ModuleBayMapping(librenms_name=OVERFLOW_PATTERN, is_regex=True), "_compiled_pattern"),
        (
            InventoryIgnoreRule(match_type=InventoryIgnoreRule.MATCH_REGEX, pattern=OVERFLOW_PATTERN),
            "_compiled_pattern",
        ),
        (CarrierAutoInstallRule(device_type_pattern=OVERFLOW_PATTERN), "_compiled_device_type_pattern"),
        (CarrierAutoInstallRule(librenms_child_name_pattern=OVERFLOW_PATTERN), "_compiled_child_name_pattern"),
        (CarrierAutoInstallRule(netbox_bay_name_pattern=OVERFLOW_PATTERN), "_compiled_bay_name_pattern"),
        (PortStackLagPattern(lag_name_pattern=OVERFLOW_PATTERN), "_compiled_pattern"),
        (PortStackLagPattern(sap_name_pattern=OVERFLOW_PATTERN), "_compiled_sap_pattern"),
        (PortStackLagPattern(bridge_name_pattern=OVERFLOW_PATTERN), "_compiled_bridge_pattern"),
    ],
)
def test_a_stored_pattern_with_a_too_large_repeat_count_compiles_to_none(rule, attribute):
    assert getattr(rule, attribute) is None


def test_an_ignore_rule_with_a_too_large_repeat_count_matches_no_name():
    rule = InventoryIgnoreRule(match_type=InventoryIgnoreRule.MATCH_REGEX, pattern=OVERFLOW_PATTERN)

    assert rule.matches_name("a") is False
