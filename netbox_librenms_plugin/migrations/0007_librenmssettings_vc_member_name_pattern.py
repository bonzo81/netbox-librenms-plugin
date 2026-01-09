# Generated migration for adding vc_member_name_pattern field to LibreNMSSettings

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0006_interfacetypemapping_description"),
    ]

    operations = [
        migrations.AddField(
            model_name="librenmssettings",
            name="vc_member_name_pattern",
            field=models.CharField(
                default="-M{position}",
                help_text="Pattern for naming virtual chassis member devices. Available placeholders: {master_name}, {position}, {serial}. Example: '-M{position}' results in 'switch01-M2'",
                max_length=100,
            ),
        ),
    ]
