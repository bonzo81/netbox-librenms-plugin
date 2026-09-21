from django.db import migrations

# NetBox refuses LAG member assignment until the aggregate is typed "lag", so without this row
# every ieee8023adLag port arrives unmapped and the relationship sync has to promote the
# aggregate itself.
SEEDED_MAPPING = {
    "librenms_type": "ieee8023adLag",
    "librenms_speed": None,
    "netbox_type": "lag",
    "description": "Seeded by the plugin: a LAG aggregate must be typed 'lag' in NetBox.",
}


def seed_lag_mapping(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    InterfaceTypeMapping = apps.get_model("netbox_librenms_plugin", "InterfaceTypeMapping")
    InterfaceTypeMapping.objects.using(db_alias).get_or_create(
        librenms_type=SEEDED_MAPPING["librenms_type"],
        librenms_speed=SEEDED_MAPPING["librenms_speed"],
        defaults={
            "netbox_type": SEEDED_MAPPING["netbox_type"],
            "description": SEEDED_MAPPING["description"],
        },
    )


def remove_lag_mapping(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    InterfaceTypeMapping = apps.get_model("netbox_librenms_plugin", "InterfaceTypeMapping")
    # Match the seeded value too, so a row the user has since repointed survives the reverse.
    InterfaceTypeMapping.objects.using(db_alias).filter(
        librenms_type=SEEDED_MAPPING["librenms_type"],
        librenms_speed__isnull=True,
        netbox_type=SEEDED_MAPPING["netbox_type"],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_librenms_plugin", "0019_inventoryignorerule_manufacturer"),
    ]

    operations = [
        migrations.RunPython(seed_lag_mapping, remove_lag_mapping),
    ]
