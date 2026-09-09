from django.db import migrations, models

# Nokia SR OS is the vendor whose port_stack rows carry service access points, written with a
# colon (lag-1:10, 1/1/1:100). The rule lives here as data so an operator can correct it, and so
# a vendor that spells a real interface with a colon (a Junos breakout channel is xe-1/1/3:1) is
# not caught by another vendor's notation.
INITIAL_SAP_PATTERNS = [
    ("timos", ":"),
]


def _normalized(value):
    """Normalize an OS name the way migration 0014's unique constraint and the model reader do."""
    return (value or "").strip().lower()


def populate_sap_patterns(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    PortStackLagPattern = apps.get_model("netbox_librenms_plugin", "PortStackLagPattern")
    wanted = {os_name: sap_pattern for os_name, sap_pattern in INITIAL_SAP_PATTERNS}
    # Match on the normalized name in Python rather than with __iexact: 0014's unique constraint
    # trims as well as lowercases, so a row stored as " TIMOS " is the same rule. Matching it any
    # other way would miss the row and then collide with that constraint on insert.
    for row in PortStackLagPattern.objects.using(db_alias).all():
        pattern = wanted.get(_normalized(row.librenms_os))
        if pattern is not None and not row.sap_name_pattern:
            row.sap_name_pattern = pattern
            row.save(using=db_alias, update_fields=["sap_name_pattern"])
    # A row an operator deleted stays deleted: recreating it would also restore the LAG regex
    # they removed, and reversing this migration would not take it away again.


def clear_sap_patterns(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    PortStackLagPattern = apps.get_model("netbox_librenms_plugin", "PortStackLagPattern")
    wanted = {os_name: sap_pattern for os_name, sap_pattern in INITIAL_SAP_PATTERNS}
    for row in PortStackLagPattern.objects.using(db_alias).all():
        if wanted.get(_normalized(row.librenms_os)) == row.sap_name_pattern:
            row.sap_name_pattern = ""
            row.save(using=db_alias, update_fields=["sap_name_pattern"])


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0015_librenmssettings_remember_interface_name_per_platform"),
    ]

    operations = [
        migrations.AddField(
            model_name="portstacklagpattern",
            name="sap_name_pattern",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Regular expression matching service-access-point names, whose port_stack rows are "
                    "skipped. Leave blank when the OS has no SAP notation. "
                    "Example: ':' for Nokia SR OS (lag-1:10)"
                ),
                max_length=200,
            ),
        ),
        migrations.RunPython(populate_sap_patterns, clear_sap_patterns),
    ]
