import importlib

from django.db import migrations, models

# Reuse the Juniper slugs 0019 scopes the Routing Engine rule to, so the two lists cannot drift.
_SCOPING = importlib.import_module("netbox_librenms_plugin.migrations.0019_inventoryignorerule_manufacturer")

# The classes the Modules tab admitted from a code constant before admission became rules.
BUILT_IN_CLASSES = (
    "module",
    "powerSupply",
    "fan",
    "port",
    "container",
    "ioModule",
    "cpmModule",
    "mdaModule",
    "fabricModule",
    "xioModule",
)
FILTER_FIELDS = ("name_pattern", "description_pattern", "model_pattern")

CLASS_RULES = tuple(
    {
        "name": f"Inventory class: {phys_class}",
        "match_type": "class_is",
        "pattern": phys_class,
        "action": "include",
        "require_serial_match_parent": False,
        "enabled": True,
        "description": f"Admits inventory rows of entPhysicalClass '{phys_class}' to the Modules tab.",
    }
    for phys_class in BUILT_IN_CLASSES
)

# Other Juniper platforms report "backplane" rows that are not separate boards, so the name decides.
SCB_RULE = {
    "name": "Juniper MX switch control boards",
    "match_type": "class_is",
    "pattern": "backplane",
    "action": "include",
    "name_pattern": "^SCB",
    "description_pattern": "",
    "model_pattern": "",
    "prefer_description": True,
    "require_serial_match_parent": False,
    "enabled": True,
    "description": "Juniper MX480 and MX960 report each Switch Control Board as class 'backplane', "
    "named by its SKU (SCBE2-MX-S) and described by its slot (CB 0).",
}


def _rule_class(rule):
    return rule.pattern.strip().casefold()


def _has_filters(rule):
    return any(getattr(rule, field) for field in FILTER_FIELDS)


def _same_scb_rule(rule, manufacturer_id):
    """Return whether *rule* admits what the seeded SCB rule admits, in the same scope."""
    return (
        _rule_class(rule) == SCB_RULE["pattern"].casefold()
        and all(getattr(rule, field) == SCB_RULE[field] for field in FILTER_FIELDS)
        and rule.manufacturer_id == manufacturer_id
    )


def seed_inventory_class_rules(apps, schema_editor):
    """Turn the built-in class list into rules and keep what every existing include rule shows."""
    # Rule names are not unique (see 0017), so each check compares what a rule admits.
    db_alias = schema_editor.connection.alias
    Manufacturer = apps.get_model("dcim", "Manufacturer")
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    rules = InventoryIgnoreRule.objects.using(db_alias)
    include_rules = list(rules.filter(action="include", match_type="class_is"))

    # A rule-admitted class named its row by entPhysicalDescr; a built-in class did not.
    built_in = {phys_class.casefold() for phys_class in BUILT_IN_CLASSES}
    rules.filter(pk__in=[rule.pk for rule in include_rules if _rule_class(rule) not in built_in]).update(
        prefer_description=True
    )

    admitted = {
        _rule_class(rule)
        for rule in include_rules
        if rule.enabled and rule.manufacturer_id is None and not _has_filters(rule)
    }
    for rule in CLASS_RULES:
        if rule["pattern"].casefold() not in admitted:
            rules.create(**rule)

    juniper = Manufacturer.objects.using(db_alias).filter(slug__in=_SCOPING.JUNIPER_SLUGS).order_by("pk")
    for manufacturer_id in list(juniper.values_list("pk", flat=True)) or [None]:
        if not any(_same_scb_rule(rule, manufacturer_id) for rule in include_rules):
            rules.create(**SCB_RULE, manufacturer_id=manufacturer_id)


def _filter_field(source):
    return models.CharField(
        blank=True,
        default="",
        help_text=f"(Include rules only) Optional Python regex, searched in {source} and case-sensitive. "
        "Leave blank to admit any value.",
        max_length=200,
    )


class Migration(migrations.Migration):
    """Admit inventory classes through include rules, with optional name, description and model filters."""

    dependencies = [
        ("netbox_librenms_plugin", "0024_inventory_model_matches_device_type_rule"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventoryignorerule",
            name="name_pattern",
            field=_filter_field("entPhysicalName"),
        ),
        migrations.AddField(
            model_name="inventoryignorerule",
            name="description_pattern",
            field=_filter_field("entPhysicalDescr"),
        ),
        migrations.AddField(
            model_name="inventoryignorerule",
            name="model_pattern",
            field=_filter_field("entPhysicalModelName"),
        ),
        migrations.AddField(
            model_name="inventoryignorerule",
            name="prefer_description",
            field=models.BooleanField(
                default=False,
                help_text="(Include rules only) Name the row by entPhysicalDescr. Use it when entPhysicalName "
                "repeats the model, as on Juniper Routing Engines.",
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="action",
            field=models.CharField(
                choices=[
                    ("skip", "Skip (remove from table)"),
                    ("transparent", "Transparent (hide row, promote children to device level)"),
                    ("include", "Include (admit rows of an entPhysicalClass)"),
                ],
                default="skip",
                help_text="What to do when this rule matches: skip the item entirely, "
                "hide its row and promote its children to device-level bay matching, "
                "or admit rows of an entPhysicalClass to the Modules tab.",
                max_length=15,
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="pattern",
            field=models.CharField(
                blank=True,
                help_text="Pattern to match against entPhysicalName. Case-insensitive for ends_with / starts_with / "
                "contains; Python re syntax for regex. For class_is, the entPhysicalClass to admit. Not used for "
                "serial_matches_device or model_matches_device_type.",
                max_length=200,
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="require_serial_match_parent",
            field=models.BooleanField(
                default=True,
                help_text="(Name-based rules only) Only apply this rule if the item's serial number matches an "
                "ancestor entity's serial number.  Recommended to prevent false positives.  Ignored for "
                "serial_matches_device, model_matches_device_type and class_is rules.",
            ),
        ),
        # No reverse: the seeded rows and the converted flags cannot be told apart from operator edits.
        migrations.RunPython(seed_inventory_class_rules),
    ]
