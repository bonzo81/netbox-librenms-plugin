"""
Convert the ``librenms_id`` custom field type to JSON.

Multi-server support stores ``librenms_id`` as a JSON dict
(``{"server_key": device_id, …}``).  Installations created before this
change will have the custom field defined as type *integer* (documented)
or *text* (some users created it with the wrong type).  This migration
converts any non-JSON type to JSON so that NetBox validation accepts the
new dict format.

Existing bare-integer **values** on devices/VMs/interfaces are left
untouched — they are migrated to the dict format on a per-object basis
through the admin UI / API (see ``migrate_legacy_librenms_id`` in utils).
"""

from django.db import migrations


def _convert_librenms_id_to_json(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    CustomField = apps.get_model("extras", "CustomField")
    try:
        cf = CustomField.objects.using(db_alias).get(name="librenms_id")
    except CustomField.DoesNotExist:
        # Custom field hasn't been created yet — nothing to convert.
        return
    if cf.type != "json":
        cf.type = "json"
        cf.save(using=db_alias, update_fields=["type"])


def _revert_librenms_id_to_integer(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    CustomField = apps.get_model("extras", "CustomField")
    try:
        cf = CustomField.objects.using(db_alias).get(name="librenms_id")
    except CustomField.DoesNotExist:
        return

    # Prevent unsafe downgrade if JSON-scoped values already exist.
    models_to_check = [
        ("dcim", "Device"),
        ("virtualization", "VirtualMachine"),
        ("dcim", "Interface"),
        ("virtualization", "VMInterface"),
    ]
    for app_label, model_name in models_to_check:
        Model = apps.get_model(app_label, model_name)
        for value in (
            Model.objects.using(db_alias)
            .exclude(custom_field_data__librenms_id=None)
            .values_list("custom_field_data__librenms_id", flat=True)
            .iterator()
        ):
            if isinstance(value, dict):
                raise RuntimeError(
                    "Cannot reverse librenms_id CustomField to integer: "
                    "JSON-scoped values already exist. Migrate them back to "
                    "bare integers first."
                )

    if cf.type == "json":
        cf.type = "integer"
        cf.save(using=db_alias, update_fields=["type"])


class Migration(migrations.Migration):
    dependencies = [
        ("extras", "0001_initial"),
        ("netbox_librenms_plugin", "0008_librenmssettings_import_defaults"),
    ]

    operations = [
        migrations.RunPython(
            code=_convert_librenms_id_to_json,
            reverse_code=_revert_librenms_id_to_integer,
        ),
    ]
