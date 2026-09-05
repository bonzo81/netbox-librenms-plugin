"""Manufacturer scoping for InventoryIgnoreRule."""

import pytest


def _manufacturer(name, slug):
    from dcim.models import Manufacturer

    return Manufacturer.objects.create(name=name, slug=slug)


def _include_rule(name, manufacturer=None):
    """Create an enabled rule that admits entPhysicalClass "other"."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return InventoryIgnoreRule.objects.create(
        name=name,
        match_type=InventoryIgnoreRule.MATCH_CLASS_IS,
        pattern="other",
        action=InventoryIgnoreRule.ACTION_INCLUDE,
        require_serial_match_parent=False,
        manufacturer=manufacturer,
    )


@pytest.mark.django_db
class TestGetEnabledIgnoreRules:
    """The loader scopes rules the way apply_normalization_rules scopes its own."""

    def test_a_vendor_takes_its_own_rules_and_the_unscoped_ones(self):
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules

        juniper = _manufacturer("Scope Juniper", "scope-juniper")
        cisco = _manufacturer("Scope Cisco", "scope-cisco")
        mine = _include_rule("scope-juniper-only", juniper)
        theirs = _include_rule("scope-cisco-only", cisco)
        shared = _include_rule("scope-all-vendors")

        loaded = get_enabled_ignore_rules(juniper)

        assert mine in loaded
        assert shared in loaded
        assert theirs not in loaded

    def test_no_manufacturer_takes_only_the_unscoped_rules(self):
        """A caller without a manufacturer must not inherit one vendor's rule."""
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules

        juniper = _manufacturer("Bare Juniper", "bare-juniper")
        scoped = _include_rule("bare-juniper-only", juniper)
        shared = _include_rule("bare-all-vendors")

        loaded = get_enabled_ignore_rules(None)

        assert shared in loaded
        assert scoped not in loaded

    def test_a_disabled_rule_is_excluded_whatever_its_scope(self):
        from netbox_librenms_plugin.models import InventoryIgnoreRule
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules

        juniper = _manufacturer("Off Juniper", "off-juniper")
        rule = _include_rule("off-juniper-only", juniper)
        InventoryIgnoreRule.objects.filter(pk=rule.pk).update(enabled=False)

        assert rule not in get_enabled_ignore_rules(juniper)


@pytest.mark.django_db
class TestScopedIncludeRuleAdmission:
    """The reported problem: one vendor's include rule changed every other vendor's sync."""

    def test_another_vendors_class_rule_does_not_admit_the_row(self):
        from netbox_librenms_plugin.models import InventoryIgnoreRule
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules
        from netbox_librenms_plugin.views.base.modules_view import _class_is_included

        # The seeded rule is vendor-agnostic until migration 0019 runs against a NetBox that
        # knows Juniper, and its own scoping is covered by TestSeededRuleScoping.
        InventoryIgnoreRule.objects.filter(
            match_type=InventoryIgnoreRule.MATCH_CLASS_IS, manufacturer__isnull=True
        ).delete()
        juniper = _manufacturer("Admit Juniper", "admit-juniper")
        cisco = _manufacturer("Admit Cisco", "admit-cisco")
        _include_rule("admit-juniper-routing-engines", juniper)
        item = {"entPhysicalClass": "other", "entPhysicalName": "RE0"}

        assert _class_is_included(item, get_enabled_ignore_rules(juniper)) is True
        assert _class_is_included(item, get_enabled_ignore_rules(cisco)) is False

    def test_an_unscoped_rule_still_admits_every_vendor(self):
        """Existing installs have vendor-agnostic rules, which must keep working."""
        from netbox_librenms_plugin.utils import get_enabled_ignore_rules
        from netbox_librenms_plugin.views.base.modules_view import _class_is_included

        cisco = _manufacturer("Global Cisco", "global-cisco")
        _include_rule("global-any-vendor")
        item = {"entPhysicalClass": "other", "entPhysicalName": "RE0"}

        assert _class_is_included(item, get_enabled_ignore_rules(cisco)) is True


@pytest.mark.django_db
class TestSeededRuleScoping:
    """Migration 0019 points the seeded routing-engine rule at Juniper."""

    def _run_migration_scoping(self):
        from netbox_librenms_plugin.tests.conftest import restore_inventory_rule_scoping

        restore_inventory_rule_scoping()

    def test_the_seeded_rule_is_scoped_when_juniper_exists(self):
        import importlib

        from netbox_librenms_plugin.models import InventoryIgnoreRule

        include = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")
        juniper = _manufacturer("Juniper", "juniper")
        InventoryIgnoreRule.objects.filter(name=include.DEFAULT_RULE["name"]).update(manufacturer=None)

        self._run_migration_scoping()

        rule = InventoryIgnoreRule.objects.get(name=include.DEFAULT_RULE["name"])
        assert rule.manufacturer == juniper

    def test_the_seeded_rule_is_left_enabled_when_juniper_is_absent(self):
        """With nothing to scope to, the rule keeps working.

        Disabling it would hide the Routing Engines again for a NetBox that gains its first
        Juniper device after this migration ran, with nothing to point the operator at.
        """
        import importlib

        from dcim.models import Manufacturer
        from netbox_librenms_plugin.models import InventoryIgnoreRule

        include = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")
        assert not Manufacturer.objects.filter(slug__in=("juniper", "juniper-networks")).exists()
        InventoryIgnoreRule.objects.filter(name=include.DEFAULT_RULE["name"]).update(manufacturer=None, enabled=True)

        self._run_migration_scoping()

        rule = InventoryIgnoreRule.objects.get(name=include.DEFAULT_RULE["name"])
        assert rule.manufacturer is None
        assert rule.enabled is True

    def test_a_scoped_rule_is_not_disabled(self):
        """Re-running the scoping must not switch off a rule an operator already scoped."""
        import importlib

        from netbox_librenms_plugin.models import InventoryIgnoreRule

        include = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")
        vendor = _manufacturer("Kept Vendor", "kept-vendor")
        InventoryIgnoreRule.objects.filter(name=include.DEFAULT_RULE["name"]).update(manufacturer=vendor, enabled=True)

        self._run_migration_scoping()

        rule = InventoryIgnoreRule.objects.get(name=include.DEFAULT_RULE["name"])
        assert rule.manufacturer == vendor
        assert rule.enabled is True
