import django.db.models.deletion
import ipam.fields
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models

import netbox_librenms_plugin.models

IP_MAPPING_EXISTS = "An IP mapping for this prefix and VRF already exists."


class Migration(migrations.Migration):
    """Add IP mappings, which keep the LibreNMS addresses in a prefix out of IP sync."""

    dependencies = [
        # makemigrations bumps these to the dev NetBox tip; ipam.VRF and extras.Tag exist on the 4.4 floor.
        ("extras", "0122_charfield_null_choices"),
        ("ipam", "0073_charfield_null_choices"),
        ("netbox_librenms_plugin", "0026_seed_cisco_fixed_platform_rule"),
    ]

    operations = [
        migrations.CreateModel(
            name="IPAddressMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, null=True, verbose_name="created")),
                ("last_updated", models.DateTimeField(auto_now=True, null=True, verbose_name="last updated")),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "prefix",
                    ipam.fields.IPNetworkField(
                        help_text="IPv4 or IPv6 prefix, for example fe80::/10. A host address is stored as its network."
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[("ignore", "Ignore")],
                        default="ignore",
                        help_text="Ignore keeps matching addresses out of IP sync.",
                        max_length=10,
                    ),
                ),
                (
                    "description",
                    models.TextField(blank=True, help_text="Optional description or notes about this IP mapping"),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
                (
                    "vrf",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        help_text=(
                            "Apply only to addresses synced into this VRF. "
                            "Leave blank for every VRF and the global table."
                        ),
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="librenms_ip_address_mappings",
                        to="ipam.vrf",
                    ),
                ),
            ],
            options={
                "verbose_name": "IP mapping",
                "verbose_name_plural": "IP mappings",
                "ordering": ["prefix", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("vrf__isnull", False)),
                        fields=("prefix", "vrf"),
                        name="unique_ip_address_mapping_vrf",
                        violation_error_message=IP_MAPPING_EXISTS,
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("vrf__isnull", True)),
                        fields=("prefix",),
                        name="unique_ip_address_mapping_global",
                        violation_error_message=IP_MAPPING_EXISTS,
                    ),
                ],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
    ]
