"""The inventory policy fingerprint: which inputs change it, which do not, and which rules refuse to load."""

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def chassis():
    """Return a two-member Virtual Chassis of two vendors, its page device first."""
    from dcim.models import Manufacturer

    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_virtual_chassis

    vendor = Manufacturer.objects.create(name="Policy Member Vendor", slug="policy-member-vendor")
    page = make_device_with_module_bays("policy-page", ["Slot 1"], serial="POLICY-A")
    member = make_device_with_module_bays("policy-member", ["Slot 1"], manufacturer=vendor, serial="POLICY-B")
    virtual_chassis = make_virtual_chassis("policy-vc", page, member)
    return page, member, virtual_chassis


def fingerprint(device):
    from netbox_librenms_plugin.inventory_policy import load_inventory_policy

    return load_inventory_policy(device.pk).fingerprint


def _rule(**fields):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return InventoryIgnoreRule.objects.create(
        **{"name": "Policy rule", "match_type": "contains", "pattern": "x", "action": "skip", **fields}
    )


def _update(model_class, pk, **fields):
    model_class.objects.filter(pk=pk).update(**fields)


def _rule_edit(field, value):
    def change(page, member, virtual_chassis):
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        rule = _rule(enabled=False)
        before = fingerprint(page)
        _update(InventoryIgnoreRule, rule.pk, **{field: value})
        return before

    return change


def _member_edit(**fields):
    def change(page, member, virtual_chassis):
        from dcim.models import Device

        before = fingerprint(page)
        _update(Device, member.pk, **fields)
        return before

    return change


def _member_type_edit(**fields):
    def change(page, member, virtual_chassis):
        from dcim.models import DeviceType

        before = fingerprint(page)
        _update(DeviceType, member.device_type_id, **fields)
        return before

    return change


def _member_manufacturer(page, member, virtual_chassis):
    from dcim.models import DeviceType, Manufacturer

    before = fingerprint(page)
    other = Manufacturer.objects.create(name="Policy Other Vendor", slug="policy-other-vendor")
    _update(DeviceType, member.device_type_id, manufacturer=other)
    return before


def _member_leaves(page, member, virtual_chassis):
    from dcim.models import Device

    before = fingerprint(page)
    _update(Device, member.pk, virtual_chassis=None, vc_position=None)
    return before


def _master(page, member, virtual_chassis):
    from dcim.models import VirtualChassis

    before = fingerprint(page)
    _update(VirtualChassis, virtual_chassis.pk, master=member)
    return before


def _rule_added(page, member, virtual_chassis):
    before = fingerprint(page)
    _rule()
    return before


def _rule_deleted(page, member, virtual_chassis):
    rule = _rule()
    before = fingerprint(page)
    rule.delete()
    return before


def _serial_rule(field, value):
    def change(page, member, virtual_chassis):
        from netbox_librenms_plugin.models import NormalizationRule

        rule = NormalizationRule.objects.create(scope="serial", match_pattern="^S/N ", replacement="SN-")
        before = fingerprint(page)
        _update(NormalizationRule, rule.pk, **{field: value})
        return before

    return change


CHANGES = {
    "rule-added": _rule_added,
    "rule-deleted": _rule_deleted,
    "disabled-rule-enabled": _rule_edit("enabled", True),
    "rule-action": _rule_edit("action", "transparent"),
    "rule-match-type": _rule_edit("match_type", "regex"),
    "rule-pattern": _rule_edit("pattern", "y"),
    "rule-name-filter": _rule_edit("name_pattern", "^x"),
    "rule-description-filter": _rule_edit("description_pattern", "^x"),
    "rule-model-filter": _rule_edit("model_pattern", "^x"),
    "rule-serial-guard": _rule_edit("require_serial_match_parent", False),
    "rule-label": _rule_edit("name", "Renamed"),
    "member-serial": _member_edit(serial="POLICY-C"),
    "member-position": _member_edit(vc_position=7),
    "member-leaves": _member_leaves,
    "master": _master,
    "member-type-model": _member_type_edit(model="Policy Model 2"),
    "member-type-part-number": _member_type_edit(part_number="PN-2"),
    "member-manufacturer": _member_manufacturer,
    "serial-rule-pattern": _serial_rule("match_pattern", "^SN "),
    "serial-rule-replacement": _serial_rule("replacement", "X"),
    "serial-rule-priority": _serial_rule("priority", 5),
}


@pytest.mark.parametrize("change", list(CHANGES))
def test_each_policy_input_changes_the_fingerprint(chassis, change):
    page, member, virtual_chassis = chassis
    before = CHANGES[change](page, member, virtual_chassis)

    assert fingerprint(page) != before


def test_inputs_outside_the_policy_keep_the_fingerprint(chassis):
    """Bays, names and other rule scopes decide where a row installs, not whether the table renders it."""
    from dcim.models import Device, ModuleBay

    from netbox_librenms_plugin.models import NormalizationRule

    page, member, _virtual_chassis = chassis
    before = fingerprint(page)
    _update(ModuleBay, ModuleBay.objects.get(device=member, name="Slot 1").pk, name="Slot 9")
    _update(Device, member.pk, name="policy-member-renamed")
    NormalizationRule.objects.create(scope="module_bay", match_pattern="^Slot", replacement="Bay")

    assert fingerprint(page) == before


def test_restoring_an_input_restores_the_fingerprint(chassis):
    from dcim.models import Device

    page, member, _virtual_chassis = chassis
    before = fingerprint(page)
    _update(Device, member.pk, serial="POLICY-TEMP")
    assert fingerprint(page) != before

    _update(Device, member.pk, serial="POLICY-B")

    assert fingerprint(page) == before


def test_every_member_page_shares_one_device_set(chassis):
    from netbox_librenms_plugin.inventory_policy import load_inventory_policy

    page, member, _virtual_chassis = chassis

    assert [device.pk for device in load_inventory_policy(member.pk).members] == sorted([page.pk, member.pk])


@pytest.mark.parametrize(
    ("model", "fields", "field"),
    [
        ("InventoryIgnoreRule", {"match_type": "regex", "pattern": "["}, "pattern"),
        ("InventoryIgnoreRule", {"name_pattern": "("}, "name_pattern"),
        ("InventoryIgnoreRule", {"description_pattern": "("}, "description_pattern"),
        ("InventoryIgnoreRule", {"model_pattern": "("}, "model_pattern"),
        ("NormalizationRule", {"match_pattern": "("}, "match_pattern"),
        ("NormalizationRule", {"replacement": r"\3"}, "replacement"),
    ],
)
def test_a_rule_that_does_not_work_refuses_the_load(chassis, model, fields, field):
    from netbox_librenms_plugin import models
    from netbox_librenms_plugin.inventory_policy import load_inventory_policy

    page, _member, _virtual_chassis = chassis
    if model == "InventoryIgnoreRule":
        rule = _rule()
    else:
        rule = models.NormalizationRule.objects.create(
            scope="serial", match_pattern=r"^(S/N) (\S+)$", replacement=r"\2"
        )
    _update(getattr(models, model), rule.pk, **fields)

    with pytest.raises(models.InventoryRuleFilterError) as excinfo:
        load_inventory_policy(page.pk)

    assert (excinfo.value.rule_pk, excinfo.value.field, excinfo.value.rule_url) == (
        rule.pk,
        field,
        rule.get_absolute_url(),
    )


def test_a_disabled_rule_that_does_not_compile_does_not_refuse_the_load(chassis):
    from netbox_librenms_plugin.inventory_policy import load_inventory_policy
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    page, _member, _virtual_chassis = chassis
    rule = _rule(enabled=False)
    _update(InventoryIgnoreRule, rule.pk, match_type="regex", pattern="[")

    assert load_inventory_policy(page.pk).fingerprint


def test_rules_for_a_manufacturer_are_its_own_and_the_unscoped_ones_in_pk_order(chassis):
    from netbox_librenms_plugin.inventory_policy import load_inventory_policy
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    page, member, _virtual_chassis = chassis
    InventoryIgnoreRule.objects.all().delete()
    member_rule = _rule(manufacturer=member.device_type.manufacturer)
    global_rule = _rule()
    _rule(manufacturer=page.device_type.manufacturer)
    _rule(manufacturer=member.device_type.manufacturer, enabled=False)
    policy = load_inventory_policy(page.pk)

    assert policy.rules_for(member.device_type.manufacturer) == [member_rule, global_rule]
    assert policy.rules_for(None) == [global_rule]
