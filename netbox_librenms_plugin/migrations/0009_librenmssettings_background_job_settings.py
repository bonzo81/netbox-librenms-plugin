# Generated manually for netbox_librenms_plugin

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0008_librenmssettings_import_defaults"),
    ]

    operations = [
        migrations.AddField(
            model_name="librenmssettings",
            name="background_job_mode",
            field=models.CharField(
                choices=[
                    ("always", "Always use background jobs"),
                    ("never", "Never use background jobs"),
                    ("threshold", "Use threshold-based decision"),
                ],
                default="threshold",
                help_text="Control when to use background jobs for device filtering",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="librenmssettings",
            name="background_job_threshold",
            field=models.IntegerField(
                default=20,
                help_text="Number of devices that triggers background job processing (applies when mode is 'threshold')",
            ),
        ),
    ]
