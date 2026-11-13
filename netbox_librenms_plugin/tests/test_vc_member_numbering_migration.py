"""Migration 0031: the "number from 1" rule, seeded once per Cisco manufacturer (A8)."""

import importlib

import pytest

pytestmark = pytest.mark.django_db

_MIGRATION = importlib.import_module("netbox_librenms_plugin.migrations.0031_vcmembernumberingrule")


def _run_data_step():
    """Run the migration's own data step against the current rows."""
    from django.apps import apps
    from django.db import connection, migrations

    (operation,) = [op for op in _MIGRATION.Migration.operations if isinstance(op, migrations.RunPython)]
    with connection.schema_editor() as editor:
        operation.code(apps, editor)


def _manufacturer(slug):
    from dcim.models import Manufacturer

    return Manufacturer.objects.create(name=f"Vendor {slug}", slug=slug)


def _rules():
    from netbox_librenms_plugin.models import VCMemberNumberingRule

    return VCMemberNumberingRule.objects.order_by("pk")


def test_a_fresh_migrate_seeds_no_rule_without_a_cisco_manufacturer():
    assert not _rules().exists()


@pytest.mark.parametrize("slugs", [("cisco",), ("cisco-systems",), ("cisco", "cisco-systems")])
def test_each_cisco_manufacturer_gets_one_manufacturer_rule_that_numbers_from_1(slugs):
    cisco = {_manufacturer(slug).pk for slug in slugs}
    _manufacturer("cisco-meraki")

    _run_data_step()

    assert set(_rules().values_list("manufacturer_id", "device_type_pattern", "numbering_mode")) == {
        (pk, "", "one_based") for pk in cisco
    }


def test_an_operator_rule_for_the_same_selector_is_kept():
    from netbox_librenms_plugin.models import VCMemberNumberingRule

    cisco = _manufacturer("cisco")
    operator = VCMemberNumberingRule.objects.create(manufacturer=cisco, numbering_mode="reported")

    _run_data_step()

    assert list(_rules().values_list("pk", "numbering_mode")) == [(operator.pk, "reported")]


def test_running_the_data_step_twice_adds_nothing():
    _manufacturer("cisco")
    _manufacturer("cisco-systems")

    _run_data_step()
    first = list(_rules().values_list("pk", "manufacturer_id"))
    _run_data_step()

    assert list(_rules().values_list("pk", "manufacturer_id")) == first
    assert len(first) == 2


def test_the_seeded_rule_imports_a_cisco_stack_from_1(live_librenms):
    """End to end: the seeded rule, not a test copy, numbers the Cisco recording 1, 2, 3."""
    from netbox_librenms_plugin.tests.test_vc_member_numbering import (
        _bulk_import,
        _chassis,
        _cisco_recording,
        _cisco_type,
        _serve,
    )

    _cisco_type()
    _run_data_step()
    row = _serve(live_librenms.server, _cisco_recording(), "seeded-cisco", "MODEL-f93a92")

    _bulk_import([row])

    assert sorted(_chassis(row)[1]) == [1, 2, 3]
