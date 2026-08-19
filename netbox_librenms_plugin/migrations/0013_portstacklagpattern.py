import netbox.models.deletion
import netbox_librenms_plugin.models
import taggit.managers
import utilities.json
from django.db import migrations, models

INITIAL_LAG_PATTERNS = [
    ("ios", r"^Po\d+$"),
    ("iosxe", r"^Po\d+$"),
    ("iosxr", r"^Bundle-Ether\d+$"),
    ("timos", r"^lag-\d+$"),
    ("junos", r"^ae\d+$"),
    ("arcos", r"^bond\d+$"),
]


def populate_patterns(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    PortStackLagPattern = apps.get_model("netbox_librenms_plugin", "PortStackLagPattern")
    for os_name, pattern in INITIAL_LAG_PATTERNS:
        PortStackLagPattern.objects.using(db_alias).get_or_create(
            librenms_os=os_name,
            defaults={"lag_name_pattern": pattern},
        )


def remove_patterns(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    PortStackLagPattern = apps.get_model("netbox_librenms_plugin", "PortStackLagPattern")
    for os_name, pattern in INITIAL_LAG_PATTERNS:
        PortStackLagPattern.objects.using(db_alias).filter(
            librenms_os=os_name,
            lag_name_pattern=pattern,
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        # Pinned to the NetBox 4.2 floor declared by ``min_version`` in
        # ``netbox_librenms_plugin/__init__.py`` (and the 4.2–4.5 range in README), matching
        # sibling 0010. PortStackLagPattern is a plain NetBoxModel that only references
        # ``extras.Tag``/``TaggedItem`` (via taggit) — all present in 4.2.x — so it needs
        # nothing from the 4.3-era 0138. ``makemigrations`` will try to bump this to the dev
        # environment's NetBox tip; revert it unless we actually start depending on a newer field.
        ("extras", "0122_charfield_null_choices"),
        ("netbox_librenms_plugin", "0012_normalize_device_serials"),
    ]

    operations = [
        migrations.CreateModel(
            name="PortStackLagPattern",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "librenms_os",
                    models.CharField(
                        help_text="LibreNMS OS identifier (e.g. 'ios', 'timos', 'junos')",
                        max_length=50,
                        unique=True,
                    ),
                ),
                (
                    "lag_name_pattern",
                    models.CharField(
                        help_text=(
                            "Regular expression matching LAG aggregate interface names. "
                            "Used as fallback when ifType is not 'ieee8023adLag'. "
                            r"Example: ^Po\d+$"
                        ),
                        max_length=200,
                    ),
                ),
                ("description", models.TextField(blank=True)),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "Port Stack LAG Pattern",
                "verbose_name_plural": "Port Stack LAG Patterns",
                "ordering": ["librenms_os"],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        migrations.RunPython(populate_patterns, remove_patterns),
    ]
