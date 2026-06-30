"""Guard against model/migration state drift for the plugin's own models."""

import importlib


def test_migration_0011_field_help_text_matches_model():
    """Migration 0011's PortStackLagPattern fields must carry the same help_text as the model (else the migration state drifts and makemigrations tracks a phantom AlterField)."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    # Migration modules start with a digit (not a valid identifier), so import by string.
    mod = importlib.import_module("netbox_librenms_plugin.migrations.0011_portstacklagpattern")
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
