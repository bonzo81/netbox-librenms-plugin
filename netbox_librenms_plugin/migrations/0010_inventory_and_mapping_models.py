# Consolidated inventory + mapping models migration.
#
# This single migration creates every model and constraint introduced by the
# inventory-core feature set: DeviceTypeMapping, ModuleTypeMapping,
# ModuleBayMapping, NormalizationRule, InventoryIgnoreRule, PlatformMapping,
# CarrierAutoInstallRule, and the wildcard/global uniqueness constraints on
# the mapping tables. It also seeds two default InventoryIgnoreRule entries
# that the modules-sync code relies on (Cisco IOS-XR IDPROM duplicates and
# embedded RP/fixed-chassis system boards).
#
# Earlier branches contained six separate migrations (0010..0015) for the same
# end state; they were squashed before merge because they only ever shipped to
# the devcontainer, never to a release. If you have an environment with the
# old 0010..0015 history applied, run:
#
#     python manage.py migrate netbox_librenms_plugin 0009 --fake
#     python manage.py migrate netbox_librenms_plugin --fake
#
# to rewrite the migration history without touching the schema (the end state
# is identical), then run regular ``migrate`` for any subsequent migrations.

import django.db.models.deletion
import netbox.models.deletion
import netbox_librenms_plugin.models
import taggit.managers
import utilities.json
from django.db import migrations, models


def _insert_default_inventory_ignore_rules(apps, schema_editor):
    """Seed the two InventoryIgnoreRules the modules sync expects out of the box."""
    db_alias = schema_editor.connection.alias
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    InventoryIgnoreRule.objects.using(db_alias).create(
        name="Cisco IOS-XR IDPROM entries",
        match_type="ends_with",
        pattern="IDPROM",
        action="skip",
        require_serial_match_parent=True,
        enabled=True,
        description=(
            "Cisco IOS-XR reports every hardware component's EEPROM as a child entity "
            'whose entPhysicalName ends in "IDPROM". These entries duplicate the parent '
            "module's serial number and are not real installable modules. "
            "The serial-match guard ensures only genuine EEPROM duplicates are skipped \u2014 "
            'a module whose name happens to end in "IDPROM" but has a different serial '
            "will not be filtered."
        ),
    )
    InventoryIgnoreRule.objects.using(db_alias).create(
        name="Embedded RP / fixed-chassis system board",
        match_type="serial_matches_device",
        pattern="",
        action="transparent",
        require_serial_match_parent=False,
        enabled=True,
        description=(
            "Fixed-form routers report the built-in RP as an ENTITY-MIB module whose "
            "serial number equals the device's own serial. Marking it transparent hides "
            "the RP row in the sync table while promoting its children (transceivers, "
            "fans, PSUs) to device-level bay matching. No pattern is needed \u2014 detection "
            "is purely serial-based."
        ),
    )


def _delete_default_inventory_ignore_rules(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    InventoryIgnoreRule = apps.get_model("netbox_librenms_plugin", "InventoryIgnoreRule")
    InventoryIgnoreRule.objects.using(db_alias).filter(
        name__in=(
            "Cisco IOS-XR IDPROM entries",
            "Embedded RP / fixed-chassis system board",
        )
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dcim", "0227_alter_interface_speed_bigint"),
        ("extras", "0134_owner"),
        ("netbox_librenms_plugin", "0009_convert_librenms_id_to_json"),
    ]

    operations = [
        migrations.CreateModel(
            name="CarrierAutoInstallRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("device_type_pattern", models.CharField(blank=True, max_length=255)),
                ("librenms_child_class", models.CharField(max_length=50)),
                ("librenms_child_name_pattern", models.CharField(max_length=255)),
                ("netbox_bay_name_pattern", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["manufacturer__name", "librenms_child_class", "librenms_child_name_pattern"],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name="DeviceTypeMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("librenms_hardware", models.CharField(max_length=255, unique=True)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["librenms_hardware"],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name="InventoryIgnoreRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("name", models.CharField(max_length=100)),
                ("match_type", models.CharField(default="ends_with", max_length=25)),
                ("pattern", models.CharField(blank=True, max_length=200)),
                ("action", models.CharField(default="skip", max_length=15)),
                ("require_serial_match_parent", models.BooleanField(default=True)),
                ("enabled", models.BooleanField(db_index=True, default=True)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["name", "pk"],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name="ModuleBayMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("librenms_name", models.CharField(max_length=255)),
                ("librenms_class", models.CharField(blank=True, max_length=50)),
                ("netbox_bay_name", models.CharField(max_length=255)),
                ("is_regex", models.BooleanField(default=False)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["librenms_name"],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name="ModuleTypeMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("librenms_model", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["librenms_model"],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name="NormalizationRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("scope", models.CharField(db_index=True, max_length=50)),
                ("match_pattern", models.CharField(max_length=500)),
                ("replacement", models.CharField(max_length=500)),
                ("priority", models.PositiveIntegerField(default=100)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["scope", "priority", "pk"],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name="PlatformMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                ("librenms_os", models.CharField(max_length=255, unique=True)),
                ("description", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["librenms_os"],
            },
            bases=(
                netbox_librenms_plugin.models.FullCleanOnSaveMixin,
                netbox.models.deletion.DeleteMixin,
                models.Model,
            ),
        ),
        migrations.AlterModelOptions(
            name="interfacetypemapping",
            options={"ordering": ["librenms_type", "librenms_speed"]},
        ),
        migrations.AlterUniqueTogether(
            name="interfacetypemapping",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="interfacetypemapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("librenms_speed__isnull", False)),
                fields=("librenms_type", "librenms_speed"),
                name="unique_interface_type_mapping",
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacetypemapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("librenms_speed__isnull", True)),
                fields=("librenms_type",),
                name="unique_interface_type_mapping_wildcard",
            ),
        ),
        migrations.AddField(
            model_name="carrierautoinstallrule",
            name="carrier_module_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="librenms_carrier_install_rules",
                to="dcim.moduletype",
            ),
        ),
        migrations.AddField(
            model_name="carrierautoinstallrule",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="librenms_carrier_install_rules",
                to="dcim.manufacturer",
            ),
        ),
        migrations.AddField(
            model_name="carrierautoinstallrule",
            name="tags",
            field=taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag"),
        ),
        migrations.AddField(
            model_name="devicetypemapping",
            name="netbox_device_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="librenms_device_type_mappings",
                to="dcim.devicetype",
            ),
        ),
        migrations.AddField(
            model_name="devicetypemapping",
            name="tags",
            field=taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag"),
        ),
        migrations.AddField(
            model_name="inventoryignorerule",
            name="tags",
            field=taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag"),
        ),
        migrations.AddField(
            model_name="modulebaymapping",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="librenms_module_bay_mappings",
                to="dcim.manufacturer",
            ),
        ),
        migrations.AddField(
            model_name="modulebaymapping",
            name="tags",
            field=taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag"),
        ),
        migrations.AddField(
            model_name="moduletypemapping",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="librenms_module_type_mappings",
                to="dcim.manufacturer",
            ),
        ),
        migrations.AddField(
            model_name="moduletypemapping",
            name="netbox_module_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="librenms_module_type_mappings",
                to="dcim.moduletype",
            ),
        ),
        migrations.AddField(
            model_name="moduletypemapping",
            name="tags",
            field=taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag"),
        ),
        migrations.AddField(
            model_name="normalizationrule",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="normalization_rules",
                to="dcim.manufacturer",
            ),
        ),
        migrations.AddField(
            model_name="normalizationrule",
            name="tags",
            field=taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag"),
        ),
        migrations.AddField(
            model_name="platformmapping",
            name="netbox_platform",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="librenms_platform_mappings",
                to="dcim.platform",
            ),
        ),
        migrations.AddField(
            model_name="platformmapping",
            name="tags",
            field=taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag"),
        ),
        migrations.AddConstraint(
            model_name="carrierautoinstallrule",
            constraint=models.UniqueConstraint(
                condition=models.Q(("manufacturer__isnull", False)),
                fields=(
                    "manufacturer",
                    "device_type_pattern",
                    "librenms_child_class",
                    "librenms_child_name_pattern",
                    "netbox_bay_name_pattern",
                ),
                name="unique_carrier_auto_install_rule",
            ),
        ),
        migrations.AddConstraint(
            model_name="carrierautoinstallrule",
            constraint=models.UniqueConstraint(
                condition=models.Q(("manufacturer__isnull", True)),
                fields=(
                    "device_type_pattern",
                    "librenms_child_class",
                    "librenms_child_name_pattern",
                    "netbox_bay_name_pattern",
                ),
                name="unique_carrier_auto_install_rule_global",
            ),
        ),
        migrations.AddConstraint(
            model_name="modulebaymapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("manufacturer__isnull", False)),
                fields=("librenms_name", "librenms_class", "manufacturer"),
                name="unique_module_bay_mapping",
            ),
        ),
        migrations.AddConstraint(
            model_name="modulebaymapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("manufacturer__isnull", True)),
                fields=("librenms_name", "librenms_class"),
                name="unique_module_bay_mapping_global",
            ),
        ),
        migrations.AddConstraint(
            model_name="moduletypemapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("manufacturer__isnull", False)),
                fields=("librenms_model", "manufacturer"),
                name="unique_module_type_mapping",
            ),
        ),
        migrations.AddConstraint(
            model_name="moduletypemapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("manufacturer__isnull", True)),
                fields=("librenms_model",),
                name="unique_module_type_mapping_global",
            ),
        ),
        migrations.RunPython(
            _insert_default_inventory_ignore_rules,
            reverse_code=_delete_default_inventory_ignore_rules,
        ),
    ]
