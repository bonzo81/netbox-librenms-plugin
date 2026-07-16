"""Guard against model/migration state drift for the plugin's own models."""

import importlib

import pytest


def test_migration_0013_field_help_text_matches_model():
    """Migration 0013's PortStackLagPattern fields must carry the same help_text as the model (else the migration state drifts and makemigrations tracks a phantom AlterField)."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    # Migration modules start with a digit (not a valid identifier), so import by string.
    mod = importlib.import_module("netbox_librenms_plugin.migrations.0013_portstacklagpattern")
    create_op = next(
        op
        for op in mod.Migration.operations
        if op.__class__.__name__ == "CreateModel" and op.name == "PortStackLagPattern"
    )
    migration_fields = dict(create_op.fields)

    for field_name in ("librenms_os", "lag_name_pattern"):
        model_help = PortStackLagPattern._meta.get_field(field_name).help_text
        assert migration_fields[field_name].help_text == model_help, (
            f"{field_name}: migration help_text drifted from the model"
        )


def test_migration_0014_librenms_os_help_text_matches_model():
    """Migration 0014 re-declares librenms_os via AlterField, so 0014 (not 0013's CreateModel) is the authoritative migration state makemigrations compares librenms_os against — its help_text must match the model too."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0014_portstacklagpattern_ci_unique")
    alter_op = next(
        op
        for op in mod.Migration.operations
        if op.__class__.__name__ == "AlterField" and op.model_name == "portstacklagpattern" and op.name == "librenms_os"
    )
    model_help = PortStackLagPattern._meta.get_field("librenms_os").help_text
    assert alter_op.field.help_text == model_help, "0014 AlterField librenms_os help_text drifted from the model"


def test_migration_0019_bridge_help_text_matches_model():
    """Migration 0019 must keep the bridge field state equal to the model."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0019_portstacklagpattern_bridge_name_pattern")
    add_op = next(
        op
        for op in mod.Migration.operations
        if op.__class__.__name__ == "AddField" and op.model_name == "portstacklagpattern"
    )
    model_help = PortStackLagPattern._meta.get_field("bridge_name_pattern").help_text
    assert add_op.field.help_text == model_help, "0019 bridge_name_pattern help_text drifted from the model"


@pytest.mark.django_db
@pytest.mark.parametrize("operator_edit", ["custom-data", "tag"])
def test_reverse_bridge_seed_preserves_operator_data(operator_edit):
    """Rollback must keep a seeded row after an operator adds data."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from extras.models import Tag

    from netbox_librenms_plugin.models import PortStackLagPattern

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0019_portstacklagpattern_bridge_name_pattern")
    row = PortStackLagPattern.objects.get(librenms_os=mod.BRIDGE_OS)
    if operator_edit == "custom-data":
        PortStackLagPattern.objects.filter(pk=row.pk).update(custom_field_data={"operator-note": "keep"})
    else:
        row.tags.add(Tag.objects.create(name="Bridge operator tag", slug="bridge-operator-tag"))

    historical_apps = (
        MigrationExecutor(connection)
        .loader.project_state([("netbox_librenms_plugin", "0019_portstacklagpattern_bridge_name_pattern")])
        .apps
    )
    with connection.schema_editor() as editor:
        mod.clear_bridge_pattern(historical_apps, editor)

    row.refresh_from_db()
    if operator_edit == "custom-data":
        assert row.custom_field_data == {"operator-note": "keep"}
    else:
        assert row.tags.filter(slug="bridge-operator-tag").exists()
    assert row.bridge_name_pattern == ""


@pytest.mark.django_db
def test_reverse_bridge_seed_without_content_type():
    """Rollback must work before post-migrate creates the plugin content type."""
    from django.contrib.contenttypes.models import ContentType
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from netbox_librenms_plugin.models import PortStackLagPattern

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0019_portstacklagpattern_bridge_name_pattern")
    row = PortStackLagPattern.objects.get(librenms_os=mod.BRIDGE_OS)
    ContentType.objects.filter(
        app_label="netbox_librenms_plugin",
        model="portstacklagpattern",
    ).delete()
    historical_apps = (
        MigrationExecutor(connection)
        .loader.project_state([("netbox_librenms_plugin", "0019_portstacklagpattern_bridge_name_pattern")])
        .apps
    )

    with connection.schema_editor() as editor:
        mod.clear_bridge_pattern(historical_apps, editor)

    assert not PortStackLagPattern.objects.filter(pk=row.pk).exists()


@pytest.mark.django_db
def test_plugin_migrations_do_not_redeclare_squashed_core_ancestors():
    """A plugin migration must not repeat a squashed core dependency from its plugin parent."""
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection, ignore_no_migrations=True)
    plugin_migrations = {
        key: migration for key, migration in loader.disk_migrations.items() if key[0] == "netbox_librenms_plugin"
    }
    assert plugin_migrations, "No plugin migrations were loaded"

    for migration_key, migration in plugin_migrations.items():
        plugin_parents = [dependency for dependency in migration.dependencies if dependency[0] == migration_key[0]]
        inherited_dependencies = {
            ancestor for parent in plugin_parents for ancestor in loader.graph.forwards_plan(parent)
        }
        for dependency in migration.dependencies:
            dependency_migration = loader.disk_migrations.get(dependency)
            if (
                dependency[0] != migration_key[0]
                and dependency in inherited_dependencies
                and dependency_migration is not None
                and dependency_migration.replaces
            ):
                raise AssertionError(
                    f"{migration_key} repeats squashed core dependency {dependency}; "
                    "the plugin parent already reaches it"
                )


def test_plugin_migrations_have_one_leaf():
    """Every plugin migration must belong to one ordered migration graph."""
    from django.db.migrations.graph import MigrationGraph
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, load=False)
    loader.load_disk()
    graph = MigrationGraph()
    migrations = {key: value for key, value in loader.disk_migrations.items() if key[0] == "netbox_librenms_plugin"}
    assert migrations, "No plugin migrations were loaded"
    for key, migration in migrations.items():
        graph.add_node(key, migration)
    for key, migration in migrations.items():
        for dependency in migration.dependencies:
            if dependency[0] == key[0] and not dependency[1].startswith("__"):
                graph.add_dependency(migration, key, dependency)
    graph.validate_consistency()
    graph.ensure_not_cyclic()

    leaves = graph.leaf_nodes()
    assert len(leaves) == 1, f"Plugin migrations have multiple leaves: {leaves}"


@pytest.mark.django_db
@pytest.mark.parametrize("operator_edited", [False, True])
def test_reverse_inventory_seed_preserves_operator_rules(operator_edited):
    """Rollback cannot identify whether matching rules belong to the operator."""
    from django.apps import apps
    from django.db import connection, migrations
    from netbox_librenms_plugin.models import InventoryIgnoreRule, NormalizationRule

    module = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")
    InventoryIgnoreRule.objects.filter(name=module.DEFAULT_RULE["name"]).delete()
    NormalizationRule.objects.filter(scope="serial", match_pattern=module.SERIAL_RULE["match_pattern"]).delete()
    inventory_rule = InventoryIgnoreRule.objects.create(**module.DEFAULT_RULE)
    serial_rule = NormalizationRule.objects.create(**module.SERIAL_RULE)
    if operator_edited:
        inventory_rule.description = "Operator-owned inventory rule"
        inventory_rule.save()
        serial_rule.replacement = r"serial-\1"
        serial_rule.save()
    before_inventory = InventoryIgnoreRule.objects.filter(pk=inventory_rule.pk).values().get()
    before_serial = NormalizationRule.objects.filter(pk=serial_rule.pk).values().get()
    operation = next(op for op in module.Migration.operations if isinstance(op, migrations.RunPython))
    with connection.schema_editor() as editor:
        operation.code(apps, editor)
        operation.reverse_code(apps, editor)
    assert InventoryIgnoreRule.objects.filter(pk=inventory_rule.pk).values().get() == before_inventory
    assert NormalizationRule.objects.filter(pk=serial_rule.pk).values().get() == before_serial


@pytest.mark.django_db
def test_inventory_seed_survives_duplicate_operator_rules():
    """Neither seeded model enforces uniqueness, so the seed lookups must not assume one row."""
    from django.apps import apps
    from django.db import connection, migrations

    from netbox_librenms_plugin.models import InventoryIgnoreRule, NormalizationRule

    module = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")
    InventoryIgnoreRule.objects.filter(name=module.DEFAULT_RULE["name"]).delete()
    NormalizationRule.objects.filter(scope="serial", match_pattern=module.SERIAL_RULE["match_pattern"]).delete()
    # An operator may keep two rules that share the seed's lookup fields; both are valid rows.
    for _ in range(2):
        InventoryIgnoreRule.objects.create(**module.DEFAULT_RULE)
        NormalizationRule.objects.create(**module.SERIAL_RULE)

    operation = next(op for op in module.Migration.operations if isinstance(op, migrations.RunPython))
    with connection.schema_editor() as editor:
        operation.code(apps, editor)

    # The seed found existing rows, so it must not have added a third of either.
    assert InventoryIgnoreRule.objects.filter(name=module.DEFAULT_RULE["name"]).count() == 2
    assert (
        NormalizationRule.objects.filter(scope="serial", match_pattern=module.SERIAL_RULE["match_pattern"]).count() == 2
    )


def test_migration_0014_serial_sensor_field_help_text_matches_model():
    """Same drift guard for SerialSensorTypePattern: migration 0013's fields must carry the model's help_text, else makemigrations tracks a phantom AlterField."""
    from netbox_librenms_plugin.models import SerialSensorTypePattern

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0014_serialsensortypepattern")
    create_op = next(
        op
        for op in mod.Migration.operations
        if op.__class__.__name__ == "CreateModel" and op.name == "SerialSensorTypePattern"
    )
    migration_fields = dict(create_op.fields)

    for field_name in ("sensor_type", "port_name_pattern"):
        model_help = SerialSensorTypePattern._meta.get_field(field_name).help_text
        assert migration_fields[field_name].help_text == model_help, (
            f"{field_name}: migration help_text drifted from the model"
        )


def test_migration_0015_field_help_text_matches_model():
    """Same drift guard for LibreNMSSettings's cable-sync fields: migration 0015's AddField ops must carry the model's help_text, else makemigrations tracks a phantom AlterField."""
    from netbox_librenms_plugin.models import LibreNMSSettings

    mod = importlib.import_module("netbox_librenms_plugin.migrations.0015_librenmssettings_cable_sync")

    for field_name in ("cable_sync_tag", "cable_sync_tag_color", "cable_sync_description"):
        add_op = next(
            op
            for op in mod.Migration.operations
            if op.__class__.__name__ == "AddField" and op.model_name == "librenmssettings" and op.name == field_name
        )
        model_help = LibreNMSSettings._meta.get_field(field_name).help_text
        assert add_op.field.help_text == model_help, f"{field_name}: migration help_text drifted from the model"
