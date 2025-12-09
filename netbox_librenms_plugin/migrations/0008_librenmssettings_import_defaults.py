# Generated migration for adding import default settings

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0007_librenmssettings_vc_member_name_pattern"),
    ]

    operations = [
        migrations.AddField(
            model_name="librenmssettings",
            name="use_sysname_default",
            field=models.BooleanField(
                default=True,
                help_text="Use SNMP sysName instead of LibreNMS hostname when importing devices",
            ),
        ),
        migrations.AddField(
            model_name="librenmssettings",
            name="strip_domain_default",
            field=models.BooleanField(
                default=False,
                help_text="Remove domain suffix from device names during import",
            ),
        ),
    ]
