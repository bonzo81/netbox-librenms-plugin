"""
Regression tests for nullable-manufacturer UniqueConstraint splits (issue #71).

PostgreSQL treats NULL ≠ NULL, so a single UniqueConstraint that includes a
nullable FK does not actually enforce uniqueness for "global" rows where the
FK is NULL. NetBox 4.2 still supports PostgreSQL 12-14, so we cannot use
``UniqueConstraint(..., nulls_distinct=False)`` (PG 15+ / Django 5.2+) and
must split into a pair of conditional constraints instead.

These tests pin the structure so a future refactor cannot silently revert
to the unsafe single-constraint form.
"""

from django.db.models import Q, UniqueConstraint


def _condition_keys(constraint):
    """Return the dict of (field__lookup -> value) tuples used in the Q tree."""
    if not isinstance(constraint, UniqueConstraint):
        return None
    cond = constraint.condition
    if not isinstance(cond, Q):
        return None
    return dict(cond.children)


class TestModuleBayMappingUniqueConstraints:
    def test_split_into_two_conditional_constraints(self):
        from netbox_librenms_plugin.models import ModuleBayMapping

        constraints = [c for c in ModuleBayMapping._meta.constraints if isinstance(c, UniqueConstraint)]
        assert len(constraints) == 2

        conditions = [_condition_keys(c) for c in constraints]
        assert {"manufacturer__isnull": True} in conditions
        assert {"manufacturer__isnull": False} in conditions

    def test_global_constraint_excludes_manufacturer_field(self):
        from netbox_librenms_plugin.models import ModuleBayMapping

        for c in ModuleBayMapping._meta.constraints:
            if not isinstance(c, UniqueConstraint):
                continue
            if _condition_keys(c) == {"manufacturer__isnull": True}:
                assert "manufacturer" not in c.fields
                assert "librenms_name" in c.fields
                assert "librenms_class" in c.fields


class TestCarrierAutoInstallRuleUniqueConstraints:
    def test_split_into_two_conditional_constraints(self):
        from netbox_librenms_plugin.models import CarrierAutoInstallRule

        constraints = [c for c in CarrierAutoInstallRule._meta.constraints if isinstance(c, UniqueConstraint)]
        assert len(constraints) == 2

        conditions = [_condition_keys(c) for c in constraints]
        assert {"manufacturer__isnull": True} in conditions
        assert {"manufacturer__isnull": False} in conditions

    def test_global_constraint_excludes_manufacturer_field(self):
        from netbox_librenms_plugin.models import CarrierAutoInstallRule

        for c in CarrierAutoInstallRule._meta.constraints:
            if not isinstance(c, UniqueConstraint):
                continue
            if _condition_keys(c) == {"manufacturer__isnull": True}:
                assert "manufacturer" not in c.fields
                assert "librenms_child_class" in c.fields
                assert "netbox_bay_name_pattern" in c.fields
