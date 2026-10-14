from django.db import migrations

# Fixed Cisco IOS XE and NX-OS platforms report their built-in units with the chassis PID.
CISCO_SLUGS = ("cisco", "cisco-systems")
FIXED_PLATFORM_RULE = {
    "name": "Fixed-platform built-in units",
    "match_type": "model_matches_device_type",
    "pattern": "",
    "action": "transparent",
    "require_serial_match_parent": False,
    "enabled": True,
    "description": "Fixed-form routers report built-in units as chassis-level modules whose model equals the device "
    "type's model or part number. Marking them transparent hides their rows and promotes their children to "
    "device-level bay matching.",
}


def seed_fixed_platform_rule(apps, schema_editor):
    """Create the rule once per Cisco manufacturer that no rule of the same match type and action covers."""
    db_alias = schema_editor.connection.alias
    Manufacturer = apps.get_model("dcim", "Manufacturer")
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    rules = InventoryIgnoreRule.objects.using(db_alias)
    # An operator's rule decides, enabled or not: a disabled one means the operator turned it off.
    equivalent = rules.filter(match_type=FIXED_PLATFORM_RULE["match_type"], action=FIXED_PLATFORM_RULE["action"])
    if equivalent.filter(manufacturer__isnull=True).exists():
        return
    covered = set(equivalent.values_list("manufacturer_id", flat=True))
    cisco = Manufacturer.objects.using(db_alias).filter(slug__in=CISCO_SLUGS).order_by("pk")
    for manufacturer_id in cisco.values_list("pk", flat=True):
        if manufacturer_id not in covered:
            rules.create(**FIXED_PLATFORM_RULE, manufacturer_id=manufacturer_id)


class Migration(migrations.Migration):
    """Seed the fixed-platform built-in units rule for each Cisco manufacturer."""

    dependencies = [
        ("netbox_librenms_plugin", "0025_inventory_class_admission_rules"),
    ]

    operations = [
        # No reverse: a seeded rule cannot be told apart from an operator's copy of it.
        migrations.RunPython(seed_fixed_platform_rule),
    ]
