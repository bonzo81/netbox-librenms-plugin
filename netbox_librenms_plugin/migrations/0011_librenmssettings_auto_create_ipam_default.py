from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0010_inventory_and_mapping_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="librenmssettings",
            name="auto_create_ipam_default",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, missing IP addresses reported by LibreNMS are "
                    "auto-created as global /32 (IPv4) or /128 (IPv6) IPAM records "
                    "during initial import, OOB-attach, and promote-to-host actions. "
                    "Existing IPAM records are always reused."
                ),
            ),
        ),
    ]
