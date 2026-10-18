from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the model_matches_device_type match type to InventoryIgnoreRule."""

    dependencies = [
        ("netbox_librenms_plugin", "0023_align_field_attributes_with_models"),
    ]

    operations = [
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="match_type",
            field=models.CharField(
                choices=[
                    ("ends_with", "Ends with (entPhysicalName)"),
                    ("starts_with", "Starts with (entPhysicalName)"),
                    ("contains", "Contains (entPhysicalName)"),
                    ("regex", "Regex (entPhysicalName)"),
                    ("serial_matches_device", "Serial matches device (entPhysicalSerialNum = Device.serial)"),
                    (
                        "model_matches_device_type",
                        "Model matches device type (entPhysicalModelName = DeviceType model or part number)",
                    ),
                    ("class_is", "Class is (entPhysicalClass)"),
                ],
                default="ends_with",
                help_text="How to match the inventory item",
                max_length=25,
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="pattern",
            field=models.CharField(
                blank=True,
                help_text="Pattern to match against entPhysicalName. Case-insensitive for ends_with / starts_with / contains; Python re syntax for regex. Not used for serial_matches_device or model_matches_device_type.",
                max_length=200,
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="require_serial_match_parent",
            field=models.BooleanField(
                default=True,
                help_text="(Name-based rules only) Only apply this rule if the item's serial number matches an ancestor entity's serial number.  Recommended to prevent false positives.  Ignored for serial_matches_device and model_matches_device_type rules.",
            ),
        ),
    ]
