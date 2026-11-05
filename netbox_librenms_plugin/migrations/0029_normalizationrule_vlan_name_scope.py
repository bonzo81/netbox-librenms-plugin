from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0028_librenmssettings_platform_version_tracking"),
    ]

    operations = [
        migrations.AlterField(
            model_name="normalizationrule",
            name="scope",
            field=models.CharField(
                choices=[
                    ("module_type", "Module Type"),
                    ("device_type", "Device Type"),
                    ("module_bay", "Module Bay"),
                    ("serial", "Serial"),
                    ("vlan_name", "VLAN Name"),
                ],
                db_index=True,
                help_text="Which lookup this rule applies to. Serial rules rewrite the stored serial, and VLAN name "
                "rules the proposed VLAN name, rather than a matching key.",
                max_length=50,
            ),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="match_pattern",
            field=models.CharField(
                help_text="Regex pattern to match against input string (Python re syntax). In a VLAN name rule, "
                "{vid} stands for the VLAN ID of the row.",
                max_length=500,
            ),
        ),
    ]
