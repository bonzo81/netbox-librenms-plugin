"""Migration 0025: the class list becomes seeded include rules, and existing include rules keep their display."""

import importlib

import pytest

pytestmark = pytest.mark.django_db

_MIGRATION = importlib.import_module("netbox_librenms_plugin.migrations.0025_inventory_class_admission_rules")
_CLASS_RULE_NAMES = [rule["name"] for rule in _MIGRATION.CLASS_RULES]


def _run_data_step():
    """Run the migration's own data step against the current rows."""
    from django.apps import apps
    from django.db import connection, migrations

    (operation,) = [op for op in _MIGRATION.Migration.operations if isinstance(op, migrations.RunPython)]
    with connection.schema_editor() as editor:
        operation.code(apps, editor)


def _forget_the_seeds():
    """Return the rule table to its state before 0025: no class rule and no SCB rule."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    InventoryIgnoreRule.objects.filter(name__in=[*_CLASS_RULE_NAMES, _MIGRATION.SCB_RULE["name"]]).delete()
    InventoryIgnoreRule.objects.filter(action=InventoryIgnoreRule.ACTION_INCLUDE).update(prefer_description=False)


def _include(name, pattern, **fields):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return InventoryIgnoreRule.objects.create(
        name=name,
        match_type=InventoryIgnoreRule.MATCH_CLASS_IS,
        pattern=pattern,
        action=InventoryIgnoreRule.ACTION_INCLUDE,
        require_serial_match_parent=False,
        **fields,
    )


def _juniper(slug):
    from dcim.models import Manufacturer

    return Manufacturer.objects.create(name=f"Juniper {slug}", slug=slug)


def test_a_fresh_migrate_seeds_one_global_rule_per_built_in_class():
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    seeded = InventoryIgnoreRule.objects.filter(name__in=_CLASS_RULE_NAMES)

    assert sorted(seeded.values_list("pattern", flat=True)) == sorted(_MIGRATION.BUILT_IN_CLASSES)
    assert set(seeded.values_list("action", "match_type", "enabled", "require_serial_match_parent")) == {
        ("include", "class_is", True, False)
    }
    assert not seeded.filter(manufacturer__isnull=False).exists()
    assert not seeded.filter(prefer_description=True).exists()


def test_a_fresh_migrate_seeds_the_switch_control_board_rule_unscoped_without_juniper():
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    rule = InventoryIgnoreRule.objects.get(name=_MIGRATION.SCB_RULE["name"])

    assert (rule.pattern, rule.name_pattern, rule.prefer_description, rule.manufacturer_id) == (
        "backplane",
        "^SCB",
        True,
        None,
    )


def test_a_fresh_migrate_names_the_routing_engine_rows_by_their_description():
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    include = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")

    assert InventoryIgnoreRule.objects.get(name=include.DEFAULT_RULE["name"]).prefer_description is True


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("scoped", [False, True])
def test_an_include_rule_of_a_vendor_class_keeps_its_description_name(enabled, scoped):
    """Before 0025, a rule-admitted class named its rows by entPhysicalDescr; the flag keeps that."""
    _forget_the_seeds()
    rule = _include("Vendor sensors", "Sensor", enabled=enabled, manufacturer=_juniper("juniper") if scoped else None)

    _run_data_step()

    rule.refresh_from_db()
    assert rule.prefer_description is True


def test_an_include_rule_of_a_built_in_class_keeps_its_own_name():
    _forget_the_seeds()
    rule = _include("Operator modules", " Module ")

    _run_data_step()

    rule.refresh_from_db()
    assert rule.prefer_description is False


def test_a_class_that_an_operator_rule_already_admits_gets_no_seed():
    """The check reads what a rule admits, not its name: names are not unique."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    _forget_the_seeds()
    _include("Inventory class: fan", "MODULE")
    _include("Inventory class: fan", "fAn")

    _run_data_step()

    assert not InventoryIgnoreRule.objects.filter(name="Inventory class: module").exists()
    assert InventoryIgnoreRule.objects.filter(name="Inventory class: fan").count() == 2
    assert InventoryIgnoreRule.objects.filter(name__in=_CLASS_RULE_NAMES).count() == len(_CLASS_RULE_NAMES)


@pytest.mark.parametrize(
    "fields",
    [{"enabled": False}, {"scoped": True}, {"name_pattern": "^PSU"}],
    ids=["disabled", "scoped", "filtered"],
)
def test_an_operator_rule_that_does_not_admit_the_whole_class_everywhere_still_gets_the_seed(fields):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    _forget_the_seeds()
    fields = dict(fields)
    if fields.pop("scoped", False):
        fields["manufacturer"] = _juniper("juniper")
    _include("Operator supplies", "powerSupply", **fields)

    _run_data_step()

    seed = InventoryIgnoreRule.objects.get(name="Inventory class: powerSupply")
    assert (seed.enabled, seed.manufacturer_id, seed.name_pattern) == (True, None, "")


def test_running_the_data_step_twice_adds_nothing():
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    before = sorted(InventoryIgnoreRule.objects.values_list("name", "pattern", "manufacturer_id", "prefer_description"))

    _run_data_step()
    _run_data_step()

    assert sorted(
        InventoryIgnoreRule.objects.values_list("name", "pattern", "manufacturer_id", "prefer_description")
    ) == (before)


@pytest.mark.parametrize("slugs", [("juniper",), ("juniper", "juniper-networks")])
def test_the_switch_control_board_rule_is_scoped_to_each_juniper_manufacturer(slugs):
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    _forget_the_seeds()
    manufacturers = {_juniper(slug).pk for slug in slugs}
    # A slug outside 0019's JUNIPER_SLUGS gets no copy.
    _juniper("juniper-labs")

    _run_data_step()

    scoped = InventoryIgnoreRule.objects.filter(name=_MIGRATION.SCB_RULE["name"])
    assert set(scoped.values_list("manufacturer_id", flat=True)) == manufacturers
    assert set(scoped.values_list("name_pattern", "prefer_description", "enabled")) == {("^SCB", True, True)}


def test_an_existing_switch_control_board_rule_is_not_duplicated():
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    _forget_the_seeds()
    juniper = _juniper("juniper")
    _include("Operator SCB rule", "Backplane", name_pattern="^SCB", manufacturer=juniper, enabled=False)

    _run_data_step()

    assert not InventoryIgnoreRule.objects.filter(name=_MIGRATION.SCB_RULE["name"]).exists()


def test_the_migration_cannot_be_reversed():
    """The seeds and the converted flags cannot be told apart from operator edits, so 0025 has no reverse."""
    from django.db import connection
    from django.db.migrations.exceptions import IrreversibleError
    from django.db.migrations.executor import MigrationExecutor

    from netbox_librenms_plugin.models import InventoryIgnoreRule

    before = InventoryIgnoreRule.objects.count()
    executor = MigrationExecutor(connection)
    key = ("netbox_librenms_plugin", "0025_inventory_class_admission_rules")
    # 0026 can not be reversed either, so a migrate to 0024 stops there: unapply 0025 itself.
    migration = executor.loader.get_migration(*key)
    state = executor.loader.project_state(key)

    with connection.schema_editor() as editor, pytest.raises(IrreversibleError, match="0025_inventory_class"):
        migration.unapply(state, editor)

    assert InventoryIgnoreRule.objects.count() == before
    assert InventoryIgnoreRule.objects.filter(name_pattern="^SCB").exists()
