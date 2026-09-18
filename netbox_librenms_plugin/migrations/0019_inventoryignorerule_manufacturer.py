import importlib

import django.db.models.deletion
from django.db import migrations, models


# The rule that admits entPhysicalClass "other" exists for Juniper Routing Engines. Left global it
# changes what every other vendor's sync admits, so scope it to Juniper where that manufacturer is
# known. Read the name from the migration that seeds it so the two cannot drift.
_INCLUDE_RULE = importlib.import_module("netbox_librenms_plugin.migrations.0017_inventory_class_include_rule")
JUNIPER_SLUGS = ("juniper", "juniper-networks")


def _seeded_include_rules(apps, db_alias):
    """Return the queryset holding the seeded routing-engine rule, matched on its own signature."""
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    return InventoryIgnoreRule.objects.using(db_alias).filter(
        name=_INCLUDE_RULE.DEFAULT_RULE["name"],
        match_type=_INCLUDE_RULE.DEFAULT_RULE["match_type"],
        pattern=_INCLUDE_RULE.DEFAULT_RULE["pattern"],
        action=_INCLUDE_RULE.DEFAULT_RULE["action"],
    )


def scope_include_rule_to_juniper(apps, schema_editor):
    """
    Point the seeded routing-engine rule at Juniper, where that manufacturer is known.

    Left vendor-agnostic it makes one vendor's quirk admit an entPhysicalClass the built-in list
    omits for every other vendor.
    """
    db_alias = schema_editor.connection.alias
    Manufacturer = apps.get_model("dcim", "Manufacturer")
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")

    juniper_manufacturers = list(Manufacturer.objects.using(db_alias).filter(slug__in=JUNIPER_SLUGS).order_by("pk"))
    if not juniper_manufacturers:
        # Nothing to scope to. Leave the rule as it is rather than disable it: a NetBox that gains
        # its first Juniper device later would otherwise hide the Routing Engines again, with
        # nothing to point at. The field is on the form, so an operator can scope it by hand.
        return

    # Both Juniper slugs can exist in one NetBox. A rule scoped to one of them does not apply to
    # devices of the other, so give every matching manufacturer its own copy of the rule.
    seeded = _seeded_include_rules(apps, db_alias)
    for manufacturer in juniper_manufacturers:
        if seeded.filter(manufacturer=manufacturer).exists():
            continue
        unscoped = seeded.filter(manufacturer__isnull=True).order_by("pk").first()
        if unscoped is None:
            InventoryIgnoreRule.objects.using(db_alias).create(**_INCLUDE_RULE.DEFAULT_RULE, manufacturer=manufacturer)
        else:
            seeded.filter(pk=unscoped.pk).update(manufacturer=manufacturer)


def unscope_include_rule(apps, schema_editor):
    """Return the seeded rule to its vendor-agnostic state before the field is dropped."""
    db_alias = schema_editor.connection.alias
    Manufacturer = apps.get_model("dcim", "Manufacturer")
    seeded = _seeded_include_rules(apps, db_alias)

    survivor = seeded.order_by("pk").first()
    if survivor is None:
        return
    # Drop the per-manufacturer copies the forward migration cloned, then unscope the one that
    # remains, which is the single vendor-agnostic rule 0017 seeded.
    juniper_ids = list(Manufacturer.objects.using(db_alias).filter(slug__in=JUNIPER_SLUGS).values_list("pk", flat=True))
    seeded.filter(manufacturer_id__in=juniper_ids).exclude(pk=survivor.pk).delete()
    seeded.filter(pk=survivor.pk).update(manufacturer=None)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0018_librenmssettings_cable_sync"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventoryignorerule",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional: only apply this rule to devices from this manufacturer. "
                "Leave blank for vendor-agnostic rules.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="inventory_ignore_rules",
                to="dcim.manufacturer",
            ),
        ),
        migrations.RunPython(scope_include_rule_to_juniper, unscope_include_rule),
    ]
