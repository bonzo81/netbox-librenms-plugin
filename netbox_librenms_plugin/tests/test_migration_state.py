"""Guard against model/migration state drift for the plugin's own models."""

import importlib


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
