from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0027_ipaddressmapping"),
    ]

    operations = [
        migrations.AddField(
            model_name="librenmssettings",
            name="platform_version_tracking",
            field=models.BooleanField(
                default=False,
                help_text="Give each device and virtual machine the version platform of its LibreNMS OS version: "
                "a child of the OS platform, created when it is missing",
            ),
        ),
    ]
