from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0014_portstacklagpattern_ci_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="librenmssettings",
            name="remember_interface_name_per_platform",
            field=models.BooleanField(
                default=False,
                help_text="Remember each user's ifName or ifDescr choice separately for each device platform",
            ),
        ),
    ]
