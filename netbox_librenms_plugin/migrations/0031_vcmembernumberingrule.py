import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models

import netbox_librenms_plugin.models

# LibreNMS reports Cisco stack members from 0, and Cisco numbers its switches from 1.
CISCO_SLUGS = ("cisco", "cisco-systems")
RULE_EXISTS = "A numbering rule with this manufacturer and device type pattern already exists."


def seed_cisco_numbering_rule(apps, schema_editor):
    """Create a manufacturer-only "number from 1" rule for each Cisco manufacturer that has no such selector."""
    db_alias = schema_editor.connection.alias
    Manufacturer = apps.get_model("dcim", "Manufacturer")
    VCMemberNumberingRule = apps.get_model("netbox_librenms_plugin", "VCMemberNumberingRule")
    rules = VCMemberNumberingRule.objects.using(db_alias)
    for manufacturer_id in (
        Manufacturer.objects.using(db_alias).filter(slug__in=CISCO_SLUGS).order_by("pk").values_list("pk", flat=True)
    ):
        # An operator's rule for the same selector decides, whatever its mode.
        rules.get_or_create(
            manufacturer_id=manufacturer_id,
            device_type_pattern="",
            defaults={
                "numbering_mode": "one_based",
                "description": "LibreNMS reports Cisco stack members from 0; Cisco numbers switches from 1.",
            },
        )


class Migration(migrations.Migration):
    """Add VC member numbering rules and seed the Cisco rule."""

    dependencies = [
        # dcim.Manufacturer and extras.Tag exist on the 4.4 floor.
        ("dcim", "0200_populate_mac_addresses"),
        ("extras", "0122_charfield_null_choices"),
        ("netbox_librenms_plugin", "0030_serial_trigram_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="VCMemberNumberingRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, null=True, verbose_name="created")),
                ("last_updated", models.DateTimeField(auto_now=True, null=True, verbose_name="last updated")),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "device_type_pattern",
                    models.CharField(
                        blank=True,
                        help_text=(
                            "Optional regex (Python re.fullmatch, case-sensitive) on the device type model. "
                            "Leave blank to apply to every device type."
                        ),
                        max_length=255,
                    ),
                ),
                (
                    "numbering_mode",
                    models.CharField(
                        choices=[("reported", "Reported numbers"), ("one_based", "Number from 1")],
                        default="reported",
                        help_text=(
                            "Reported numbers: keep the numbers that LibreNMS reports. Number from 1: add 1 to "
                            "every member number when the stack is numbered from 0."
                        ),
                        max_length=20,
                    ),
                ),
                ("description", models.TextField(blank=True, help_text="Optional notes about this rule.")),
                (
                    "manufacturer",
                    models.ForeignKey(
                        blank=True,
                        help_text=(
                            "Optional: the manufacturer of the imported device type. "
                            "Leave blank to apply to every manufacturer."
                        ),
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="librenms_vc_numbering_rules",
                        to="dcim.manufacturer",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "VC member numbering rule",
                "ordering": ["pk"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("manufacturer__isnull", False)),
                        fields=("manufacturer", "device_type_pattern"),
                        name="unique_vc_member_numbering_rule",
                        violation_error_message=RULE_EXISTS,
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("manufacturer__isnull", True)),
                        fields=("device_type_pattern",),
                        name="unique_vc_member_numbering_rule_global",
                        violation_error_message=RULE_EXISTS,
                    ),
                ],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        # No reverse: a seeded rule cannot be told apart from an operator's copy of it.
        migrations.RunPython(seed_cisco_numbering_rule),
    ]
