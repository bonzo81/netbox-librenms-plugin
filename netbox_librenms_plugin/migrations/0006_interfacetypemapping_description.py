# Generated migration for adding description field to InterfaceTypeMapping

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0005_remove_librenmssettings_created_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="interfacetypemapping",
            name="description",
            field=models.TextField(
                blank=True,
                help_text="Optional description or notes about this interface type mapping",
            ),
        ),
    ]
