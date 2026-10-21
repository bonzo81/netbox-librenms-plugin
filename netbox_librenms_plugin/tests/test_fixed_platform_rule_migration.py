"""Migration 0026: the fixed-platform built-in units rule, seeded once per Cisco manufacturer."""

import importlib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.django_db

_MIGRATION = importlib.import_module("netbox_librenms_plugin.migrations.0026_seed_cisco_fixed_platform_rule")
_RULE = _MIGRATION.FIXED_PLATFORM_RULE


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


def _model_rules():
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return InventoryIgnoreRule.objects.filter(match_type=_RULE["match_type"], action=_RULE["action"])


def _operator_rule(manufacturer=None, **fields):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    values = {
        "name": "Operator built-ins",
        "match_type": "model_matches_device_type",
        "pattern": "",
        "action": "transparent",
        "require_serial_match_parent": False,
        "manufacturer": manufacturer,
        **fields,
    }
    return InventoryIgnoreRule.objects.create(**values)


def test_a_fresh_migrate_seeds_no_rule_without_a_cisco_manufacturer():
    assert not _model_rules().exists()


@pytest.mark.parametrize("slugs", [("cisco",), ("cisco", "cisco-systems")])
def test_each_cisco_manufacturer_gets_one_scoped_rule(slugs):
    cisco = {_manufacturer(slug).pk for slug in slugs}
    # A slug outside CISCO_SLUGS gets no copy.
    _manufacturer("cisco-meraki")

    _run_data_step()

    seeded = _model_rules()
    assert sorted(seeded.values_list("manufacturer_id", flat=True)) == sorted(cisco)
    assert set(seeded.values_list("name", "enabled", "require_serial_match_parent", "description")) == {
        (_RULE["name"], True, False, _RULE["description"])
    }


@pytest.mark.parametrize("enabled", [True, False])
def test_an_equivalent_scoped_rule_keeps_its_manufacturer_out(enabled):
    cisco = _manufacturer("cisco")
    cisco_systems = _manufacturer("cisco-systems")
    operator = _operator_rule(cisco, enabled=enabled)

    _run_data_step()

    rules = dict(_model_rules().values_list("pk", "manufacturer_id"))
    assert rules.pop(operator.pk) == cisco.pk
    assert list(rules.values()) == [cisco_systems.pk]


@pytest.mark.parametrize("field", ["name_pattern", "description_pattern", "model_pattern"])
@pytest.mark.parametrize("scoped", [True, False])
def test_a_filtered_rule_is_no_equivalent(field, scoped):
    """A filter narrows the rule to some units, so the other built-in units still need the seed."""
    cisco = _manufacturer("cisco")
    operator = _operator_rule(cisco if scoped else None, **{field: "^Built"})

    _run_data_step()

    seeded = _model_rules().exclude(pk=operator.pk)
    assert list(seeded.values_list("manufacturer_id", flat=True)) == [cisco.pk]


@pytest.mark.parametrize("enabled", [True, False])
def test_an_equivalent_global_rule_keeps_every_manufacturer_out(enabled):
    _manufacturer("cisco")
    _manufacturer("cisco-systems")
    operator = _operator_rule(enabled=enabled)

    _run_data_step()

    assert list(_model_rules().values_list("pk", flat=True)) == [operator.pk]


@pytest.mark.parametrize(
    ("match_type", "action"), [("model_matches_device_type", "skip"), ("serial_matches_device", "transparent")]
)
def test_a_rule_of_another_match_type_or_action_is_no_equivalent(match_type, action):
    cisco = _manufacturer("cisco")
    _operator_rule(cisco, match_type=match_type, action=action)

    _run_data_step()

    assert list(_model_rules().values_list("manufacturer_id", flat=True)) == [cisco.pk]


def test_running_the_data_step_twice_adds_nothing():
    _manufacturer("cisco")
    _manufacturer("cisco-systems")

    _run_data_step()
    first = sorted(_model_rules().values_list("pk", "manufacturer_id"))
    _run_data_step()

    assert sorted(_model_rules().values_list("pk", "manufacturer_id")) == first
    assert len(first) == 2


def test_the_seed_is_the_contrib_example():
    """The seeded rule and the documented example are the same rule."""
    contrib = Path(__file__).resolve().parents[2] / "contrib" / "inventory_ignore_rules.yaml"
    (record,) = [r for r in yaml.safe_load(contrib.read_text(encoding="utf-8")) if r["name"] == _RULE["name"]]

    assert {**record, "description": record["description"].strip()} == _RULE


def test_the_seeded_rule_hides_the_built_in_units_of_a_cisco_c8300(settings, librenms_server):
    """End to end: the seeded rule, not a test copy, makes the C8300 built-in units transparent."""
    from netbox_librenms_plugin.tests.test_inventory_rule_filters import (
        MODULE_0,
        OTHER_BUILT_INS,
        _recording,
        c8300_device,
        rows_by_index,
    )
    from netbox_librenms_plugin.tests.test_modules_view import _two_server_keys

    server_key = _two_server_keys(settings, librenms_server, "fixed-platform-seed")[0]
    recording = _recording()
    librenms_server.load_recording(recording)
    device = c8300_device("c8300-seeded-rule", recording, server_key)
    assert MODULE_0 in rows_by_index(device, server_key)

    _run_data_step()

    assert list(_model_rules().values_list("manufacturer__slug", flat=True)) == ["cisco"]
    assert not {MODULE_0, *OTHER_BUILT_INS} & set(rows_by_index(device, server_key))


def test_the_migration_cannot_be_reversed():
    """A seeded rule cannot be told apart from an operator's copy of it, so 0026 has no reverse."""
    from django.db import connection
    from django.db.migrations.exceptions import IrreversibleError
    from django.db.migrations.executor import MigrationExecutor

    from netbox_librenms_plugin.models import InventoryIgnoreRule

    _manufacturer("cisco")
    _run_data_step()
    before = InventoryIgnoreRule.objects.count()
    executor = MigrationExecutor(connection)
    key = ("netbox_librenms_plugin", "0026_seed_cisco_fixed_platform_rule")
    # A migrate to 0025 would first reverse every later migration: unapply 0026 itself.
    migration = executor.loader.get_migration(*key)
    state = executor.loader.project_state(key)

    with connection.schema_editor() as editor, pytest.raises(IrreversibleError, match=key[1]):
        migration.unapply(state, editor)

    assert InventoryIgnoreRule.objects.count() == before
    assert _model_rules().exists()
