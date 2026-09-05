import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models
from django.db.models.functions import Lower

import netbox_librenms_plugin.models

_PYTHON_STRIP_WHITESPACE = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0"
    "\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)

# The two vendors the serial cable-sync mapper shipped with while the map was a plugin
# setting. Seeded here so upgrades keep recognizing them; deleting a row is the supported
# way to disable a vendor (get_serial_sensor_type_patterns has no code-level fallback).
INITIAL_SERIAL_SENSOR_TYPES = [
    ("acsSerialPortTable", "ttyS{N}"),
    ("OLD-CISCO-TS-MIB::ltsLineTable", "Line {N}"),
]


def populate_types(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    SerialSensorTypePattern = apps.get_model("netbox_librenms_plugin", "SerialSensorTypePattern")
    for sensor_type, pattern in INITIAL_SERIAL_SENSOR_TYPES:
        SerialSensorTypePattern.objects.using(db_alias).get_or_create(
            sensor_type=sensor_type,
            defaults={"port_name_pattern": pattern},
        )


def remove_types(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    SerialSensorTypePattern = apps.get_model("netbox_librenms_plugin", "SerialSensorTypePattern")
    for sensor_type, pattern in INITIAL_SERIAL_SENSOR_TYPES:
        SerialSensorTypePattern.objects.using(db_alias).filter(
            sensor_type=sensor_type,
            port_name_pattern=pattern,
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        # Generated against the supported NetBox 4.2 floor. SerialSensorTypePattern only
        # references extras.Tag and extras.TaggedItem, which are available on that floor.
        ("extras", "0122_charfield_null_choices"),
        # Chain after the include-rule migration, which also depends on 0016. Without this
        # the app graph has two leaves and Django refuses to migrate.
        ("netbox_librenms_plugin", "0017_inventory_class_include_rule"),
    ]

    operations = [
        migrations.CreateModel(
            name="SerialSensorTypePattern",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "sensor_type",
                    models.CharField(
                        help_text=(
                            "LibreNMS sensor_type identifying a vendor's serial-port state sensors "
                            "(e.g. 'acsSerialPortTable', 'OLD-CISCO-TS-MIB::ltsLineTable'). "
                            "Matching is exact, including case."
                        ),
                        max_length=100,
                    ),
                ),
                (
                    "port_name_pattern",
                    models.CharField(
                        default="ttyS{N}",
                        help_text=(
                            "Local ConsoleServerPort name template; {N} is replaced by the "
                            "sensor's port number. Example: ttyS{N}"
                        ),
                        max_length=100,
                    ),
                ),
                ("description", models.TextField(blank=True)),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "Serial Sensor Type",
                "verbose_name_plural": "Serial Sensor Types",
                "ordering": ["sensor_type"],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        migrations.AddConstraint(
            model_name="serialsensortypepattern",
            constraint=models.UniqueConstraint(
                Lower(models.Func("sensor_type", models.Value(_PYTHON_STRIP_WHITESPACE), function="BTRIM")),
                name="unique_serialsensortypepattern_sensor_type_ci",
            ),
        ),
        migrations.RunPython(populate_types, remove_types),
    ]
