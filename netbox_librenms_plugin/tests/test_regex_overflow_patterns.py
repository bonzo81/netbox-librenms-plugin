"""A pattern with a too large repeat count is an invalid pattern (``re.compile`` raises OverflowError), and its rule fails closed."""

import pytest

from netbox_librenms_plugin.forms import ImportSettingsForm
from netbox_librenms_plugin.models import (
    CarrierAutoInstallRule,
    InventoryIgnoreRule,
    ModuleBayMapping,
    NormalizationRule,
    PortStackLagPattern,
    RuleConfigurationError,
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


def test_a_location_regex_with_a_too_large_repeat_count_raises():
    with pytest.raises(OverflowError):
        parse_librenms_location("NYC", f"(?P<site>{OVERFLOW_PATTERN})", is_regex=True)


def _carrier(**fields):
    from dcim.models import Manufacturer, ModuleType

    manufacturer = Manufacturer.objects.create(name="Overflow Vendor", slug="overflow-vendor")
    module_type = ModuleType.objects.create(manufacturer=manufacturer, model="Overflow Carrier")
    values = {
        "librenms_child_class": "module",
        "librenms_child_name_pattern": "^Slot$",
        "netbox_bay_name_pattern": "^CMA$",
    }
    return CarrierAutoInstallRule(carrier_module_type=module_type, **{**values, **fields})


def _stored(rule):
    (row,) = type(rule).objects.bulk_create([rule])
    return row


@pytest.mark.django_db
@pytest.mark.parametrize(
    "build, field, evaluate",
    [
        (
            lambda: ModuleBayMapping(librenms_name=OVERFLOW_PATTERN, netbox_bay_name="Bay", is_regex=True),
            "librenms_name",
            lambda rule: rule._compiled_pattern,
        ),
        (
            lambda: InventoryIgnoreRule(
                name="Overflow", match_type=InventoryIgnoreRule.MATCH_REGEX, pattern=OVERFLOW_PATTERN
            ),
            "pattern",
            lambda rule: rule.matches_name("a"),
        ),
        (
            lambda: _carrier(device_type_pattern=OVERFLOW_PATTERN),
            "device_type_pattern",
            lambda rule: rule._compiled_device_type_pattern,
        ),
        (
            lambda: _carrier(librenms_child_name_pattern=OVERFLOW_PATTERN),
            "librenms_child_name_pattern",
            lambda rule: rule._compiled_child_name_pattern,
        ),
        (
            lambda: _carrier(netbox_bay_name_pattern=OVERFLOW_PATTERN),
            "netbox_bay_name_pattern",
            lambda rule: rule._compiled_bay_name_pattern,
        ),
        (
            lambda: PortStackLagPattern(librenms_os="overflow", lag_name_pattern=OVERFLOW_PATTERN),
            "lag_name_pattern",
            lambda rule: rule._compiled_pattern,
        ),
        (
            lambda: PortStackLagPattern(
                librenms_os="overflow", lag_name_pattern="^ae", sap_name_pattern=OVERFLOW_PATTERN
            ),
            "sap_name_pattern",
            lambda rule: rule._compiled_sap_pattern,
        ),
        (
            lambda: PortStackLagPattern(
                librenms_os="overflow", lag_name_pattern="^ae", bridge_name_pattern=OVERFLOW_PATTERN
            ),
            "bridge_name_pattern",
            lambda rule: rule._compiled_bridge_pattern,
        ),
        (
            lambda: NormalizationRule(
                scope=NormalizationRule.SCOPE_MODULE_TYPE, match_pattern=OVERFLOW_PATTERN, replacement="x"
            ),
            "match_pattern",
            lambda rule: apply_normalization_rules(
                "SFP-10G",
                NormalizationRule.SCOPE_MODULE_TYPE,
                preloaded_rules={(NormalizationRule.SCOPE_MODULE_TYPE, None): [rule]},
            ),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_a_stored_pattern_with_a_too_large_repeat_count_names_the_rule(build, field, evaluate):
    rule = _stored(build())

    with pytest.raises(RuleConfigurationError) as excinfo:
        evaluate(rule)

    assert (excinfo.value.rule_pk, excinfo.value.field) == (rule.pk, field)
