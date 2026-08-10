from django.db import migrations, models

import utilities.fields


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0015_serialsensortypepattern"),
    ]

    operations = [
        # Cable-sync provenance settings move from PLUGINS_CONFIG to the settings singleton:
        # operator preferences belong in the DB/UI (no NetBox restart to change), mirroring the
        # serial_sensor_types setting → SerialSensorTypePattern move. Defaults match the values
        # the plugin previously shipped in default_settings, so an upgrade keeps stamping cables
        # identically until someone edits the Settings page.
        migrations.AddField(
            model_name="librenmssettings",
            name="cable_sync_tag",
            field=models.CharField(
                default="librenms",
                help_text="Provenance tag added to cables that cable sync creates or adopts",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="librenmssettings",
            name="cable_sync_tag_color",
            field=utilities.fields.ColorField(
                default="009688",
                help_text="Color of the auto-created provenance tag and of the cables themselves",
                max_length=6,
            ),
        ),
        migrations.AddField(
            model_name="librenmssettings",
            name="cable_sync_description",
            field=models.CharField(
                default="Synced from LibreNMS",
                help_text="Cable description; the acting server key is appended, e.g. 'Synced from LibreNMS (production)'",
                max_length=200,
            ),
        ),
    ]
