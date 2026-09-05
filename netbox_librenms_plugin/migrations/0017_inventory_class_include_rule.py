from django.db import migrations, models

# Juniper reports both Routing Engines of an MX304 with entPhysicalClass "other", which the
# built-in class list does not carry, so neither reached the modules tab. Seed the rule that
# admits them; an operator can disable or delete it like any other rule.
DEFAULT_RULE = {
    "name": "Routing engines reported as class other",
    "match_type": "class_is",
    "pattern": "other",
    "action": "include",
    "require_serial_match_parent": False,
    "description": "Juniper reports Routing Engines with entPhysicalClass 'other'. "
    "Without this rule the modules tab drops them before any matching runs.",
}


# Juniper prefixes ENTITY-MIB serials with a literal "S/N ". Storing that verbatim gives
# NetBox a serial that never matches the hardware. A rule keeps the rewrite visible.
SERIAL_RULE = {
    "scope": "serial",
    # replacement is blank=False, so the kept text is captured rather than substituted away.
    "match_pattern": r"^S/N\s+(.+)$",
    "replacement": r"\1",
    "priority": 100,
    "description": "Juniper reports ENTITY-MIB serials as 'S/N BCFB9793'. Stored verbatim "
    "the serial never matches the hardware.",
}


def add_default_rule(apps, schema_editor):
    """Seed the rule, leaving an operator's existing rule of the same name alone."""
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    InventoryIgnoreRule.objects.using(schema_editor.connection.alias).get_or_create(
        name=DEFAULT_RULE["name"], defaults=DEFAULT_RULE
    )
    NormalizationRule = apps.get_model("netbox_librenms_plugin", "NormalizationRule")
    NormalizationRule.objects.using(schema_editor.connection.alias).get_or_create(
        scope=SERIAL_RULE["scope"], match_pattern=SERIAL_RULE["match_pattern"], defaults=SERIAL_RULE
    )


def remove_default_rule(apps, schema_editor):
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    InventoryIgnoreRule.objects.using(schema_editor.connection.alias).filter(
        name=DEFAULT_RULE["name"], pattern=DEFAULT_RULE["pattern"]
    ).delete()
    NormalizationRule = apps.get_model("netbox_librenms_plugin", "NormalizationRule")
    NormalizationRule.objects.using(schema_editor.connection.alias).filter(
        scope=SERIAL_RULE["scope"], match_pattern=SERIAL_RULE["match_pattern"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0016_portstacklagpattern_sap_name_pattern"),
    ]

    operations = [
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="action",
            field=models.CharField(
                choices=[
                    ("skip", "Skip (remove from table)"),
                    ("transparent", "Transparent (hide row, promote children to device level)"),
                    ("include", "Include (admit an entPhysicalClass the built-in list omits)"),
                ],
                default="skip",
                help_text="What to do when this rule matches: skip the item entirely, "
                "or hide its row and promote its children to device-level bay matching.",
                max_length=15,
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="match_type",
            field=models.CharField(
                choices=[
                    ("ends_with", "Ends with (entPhysicalName)"),
                    ("starts_with", "Starts with (entPhysicalName)"),
                    ("contains", "Contains (entPhysicalName)"),
                    ("regex", "Regex (entPhysicalName)"),
                    ("serial_matches_device", "Serial matches device (entPhysicalSerialNum = Device.serial)"),
                    ("class_is", "Class is (entPhysicalClass)"),
                ],
                default="ends_with",
                help_text="How to match the inventory item",
                max_length=25,
            ),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="scope",
            field=models.CharField(
                choices=[
                    ("module_type", "Module Type"),
                    ("device_type", "Device Type"),
                    ("module_bay", "Module Bay"),
                    ("serial", "Serial"),
                ],
                db_index=True,
                help_text="Which lookup this rule applies to. Serial rules rewrite the stored "
                "serial rather than a matching key.",
                max_length=50,
            ),
        ),
        migrations.RunPython(add_default_rule, remove_default_rule),
    ]
