"""
Convert the ``librenms_id`` custom field type from integer to JSON.

Multi-server support stores ``librenms_id`` as a JSON dict
(``{"server_key": device_id, …}``).  Installations created before this
change will have the custom field defined as type *integer*.  This
migration updates the CustomField type so that NetBox validation accepts
the new dict format.

Existing bare-integer **values** on devices/VMs/interfaces are left
untouched — they are migrated to the dict format on a per-object basis
through the admin UI / API (see ``migrate_legacy_librenms_id`` in utils).
"""

from django.db import migrations


def _convert_librenms_id_to_json(apps, schema_editor):
    CustomField = apps.get_model("extras", "CustomField")
    try:
        cf = CustomField.objects.get(name="librenms_id")
    except CustomField.DoesNotExist:
        # Custom field hasn't been created yet — nothing to convert.
        return
    if cf.type == "integer":
        cf.type = "json"
        cf.save(update_fields=["type"])


def _revert_librenms_id_to_integer(apps, schema_editor):
    CustomField = apps.get_model("extras", "CustomField")
    try:
        cf = CustomField.objects.get(name="librenms_id")
    except CustomField.DoesNotExist:
        return
    if cf.type == "json":
        cf.type = "integer"
        cf.save(update_fields=["type"])


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
