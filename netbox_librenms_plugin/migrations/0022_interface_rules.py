import django.db.models.deletion
from django.db import migrations, models

RULE_EXISTS = "A rule with the same platform, LibreNMS type, name pattern and speed already exists."


class Migration(migrations.Migration):
    """Turn interface type mappings into interface rules; existing rows become global Set type rules."""

    # dcim.Platform is reachable through 0010's dcim dependency.
    dependencies = [
        ("netbox_librenms_plugin", "0021_widen_linux_bridge_pattern"),
    ]

    operations = [
        # No reverse: it would turn platform- and pattern-scoped rules into global ones.
        migrations.RunPython(migrations.RunPython.noop, reverse_code=None),
        migrations.RemoveConstraint(
            model_name="interfacetypemapping",
            name="unique_interface_type_mapping",
        ),
        migrations.RemoveConstraint(
            model_name="interfacetypemapping",
            name="unique_interface_type_mapping_wildcard",
        ),
        migrations.AddField(
            model_name="interfacetypemapping",
            name="action",
            field=models.CharField(
                default="set_type",
                help_text="Set type gives matching ports a NetBox type. Ignore keeps them out of interface sync.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="interfacetypemapping",
            name="name_pattern",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Python regular expression, searched in ifName and in ifDescr (either match counts). "
                    "Case-sensitive; use (?i) to ignore case. Leave blank for any name."
                ),
                max_length=200,
            ),
        ),
        migrations.AddField(
            model_name="interfacetypemapping",
            name="platform",
            field=models.ForeignKey(
                blank=True,
                help_text="Apply only to interfaces of objects on this platform. Leave blank for every platform.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="librenms_interface_type_mappings",
                to="dcim.platform",
            ),
        ),
        migrations.AlterField(
            model_name="interfacetypemapping",
            name="librenms_type",
            field=models.CharField(
                blank=True,
                default="",
                help_text="LibreNMS ifType, matched exactly. Leave blank for any type.",
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name="interfacetypemapping",
            name="netbox_type",
            field=models.CharField(
                blank=True,
                help_text="The NetBox interface type a Set type rule writes. Leave blank on an Ignore rule.",
                max_length=50,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="interfacetypemapping",
            name="librenms_speed",
            field=models.BigIntegerField(
                blank=True,
                help_text="Minimum port speed in Kbps. Needs a LibreNMS type; not used on an Ignore rule.",
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacetypemapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("librenms_speed__isnull", False), ("platform__isnull", False)),
                fields=("platform", "librenms_type", "name_pattern", "librenms_speed"),
                name="unique_interface_type_mapping_platform_speed",
                violation_error_message=RULE_EXISTS,
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacetypemapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("librenms_speed__isnull", True), ("platform__isnull", False)),
                fields=("platform", "librenms_type", "name_pattern"),
                name="unique_interface_type_mapping_platform",
                violation_error_message=RULE_EXISTS,
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacetypemapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("librenms_speed__isnull", False), ("platform__isnull", True)),
                fields=("librenms_type", "name_pattern", "librenms_speed"),
                name="unique_interface_type_mapping_speed",
                violation_error_message=RULE_EXISTS,
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacetypemapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("librenms_speed__isnull", True), ("platform__isnull", True)),
                fields=("librenms_type", "name_pattern"),
                name="unique_interface_type_mapping_global",
                violation_error_message=RULE_EXISTS,
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacetypemapping",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("action", "set_type"),
                        ("netbox_type__isnull", False),
                        models.Q(("netbox_type", ""), _negated=True),
                    ),
                    models.Q(("action", "ignore"), ("netbox_type__isnull", True)),
                    _connector="OR",
                ),
                name="interface_type_mapping_action_output",
                violation_error_message="A Set type rule needs a NetBox type, and an Ignore rule has none.",
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacetypemapping",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("platform__isnull", False),
                    models.Q(("librenms_type", ""), _negated=True),
                    models.Q(("name_pattern", ""), _negated=True),
                    _connector="OR",
                ),
                name="interface_type_mapping_has_selector",
                violation_error_message="Give at least one of platform, LibreNMS type or name pattern.",
            ),
        ),
    ]
