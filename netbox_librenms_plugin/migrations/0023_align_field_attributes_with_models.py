import dcim.choices
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Copy the model field choices, help_text and verbose_name that NetBox hides from makemigrations."""

    dependencies = [
        ("netbox_librenms_plugin", "0022_interface_rules"),
    ]

    operations = [
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional: scope this rule to one manufacturer (matches the device's device_type.manufacturer). Leave blank to apply across vendors.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="librenms_carrier_install_rules",
                to="dcim.manufacturer",
            ),
        ),
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="device_type_pattern",
            field=models.CharField(
                blank=True,
                help_text="Optional regex (Python re.fullmatch) on the device_type model name. Leave blank to apply to all device types of the selected manufacturer.",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="librenms_child_class",
            field=models.CharField(
                help_text="Exact entPhysicalClass match for the orphan child reported by LibreNMS (e.g. cpmModule, mdaModule, fabricModule).",
                max_length=50,
            ),
        ),
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="librenms_child_name_pattern",
            field=models.CharField(
                help_text="Regex (Python re.fullmatch) on the orphan child's entPhysicalName (e.g. '^Slot [AB]$').",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="netbox_bay_name_pattern",
            field=models.CharField(
                help_text="Regex (Python re.fullmatch) on the chassis-level empty module bay name where the carrier should be installed (e.g. '^CMA$' or '^Carrier \\d+$'). All matching empty bays will be offered as install targets.",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="carrier_module_type",
            field=models.ForeignKey(
                help_text="The NetBox ModuleType to suggest installing into the matching empty bay.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="librenms_carrier_install_rules",
                to="dcim.moduletype",
            ),
        ),
        migrations.AlterField(
            model_name="carrierautoinstallrule",
            name="description",
            field=models.TextField(blank=True, help_text="Optional notes about this rule."),
        ),
        migrations.AlterField(
            model_name="devicetypemapping",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="devicetypemapping",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="devicetypemapping",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="devicetypemapping",
            name="librenms_hardware",
            field=models.CharField(
                help_text="Hardware string as reported by LibreNMS (e.g., 'Juniper MX480 Internet Backbone Router')",
                max_length=255,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="devicetypemapping",
            name="netbox_device_type",
            field=models.ForeignKey(
                help_text="The NetBox DeviceType this hardware string maps to",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="librenms_device_type_mappings",
                to="dcim.devicetype",
            ),
        ),
        migrations.AlterField(
            model_name="devicetypemapping",
            name="description",
            field=models.TextField(blank=True, help_text="Optional description or notes about this mapping"),
        ),
        migrations.AlterField(
            model_name="interfacetypemapping",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="interfacetypemapping",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="interfacetypemapping",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="interfacetypemapping",
            name="action",
            field=models.CharField(
                choices=[("set_type", "Set type"), ("ignore", "Ignore")],
                default="set_type",
                help_text="Set type gives matching ports a NetBox type. Ignore keeps them out of interface sync.",
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="interfacetypemapping",
            name="netbox_type",
            field=models.CharField(
                blank=True,
                choices=dcim.choices.InterfaceTypeChoices,
                help_text="The NetBox interface type a Set type rule writes. Leave blank on an Ignore rule.",
                max_length=50,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="name",
            field=models.CharField(help_text="Short descriptive label for this rule", max_length=100),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="pattern",
            field=models.CharField(
                blank=True,
                help_text="Pattern to match against entPhysicalName. Case-insensitive for ends_with / starts_with / contains; Python re syntax for regex. Not used for serial_matches_device.",
                max_length=200,
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="require_serial_match_parent",
            field=models.BooleanField(
                default=True,
                help_text="(Name-based rules only) Only apply this rule if the item's serial number matches an ancestor entity's serial number.  Recommended to prevent false positives.  Ignored for serial_matches_device rules.",
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="enabled",
            field=models.BooleanField(
                db_index=True, default=True, help_text="Uncheck to temporarily disable this rule without deleting it"
            ),
        ),
        migrations.AlterField(
            model_name="inventoryignorerule",
            name="description",
            field=models.TextField(
                blank=True, help_text="Optional notes about this rule (vendor, firmware version, etc.)"
            ),
        ),
        migrations.AlterField(
            model_name="librenmssettings",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="librenmssettings",
            name="selected_server",
            field=models.CharField(
                default="default",
                help_text="The key of the selected LibreNMS server from configuration",
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name="librenmssettings",
            name="vc_member_name_pattern",
            field=models.CharField(
                default="-M{position}",
                help_text="Pattern for naming virtual chassis member devices. Available placeholders: {position}, {serial}. Example: '-M{position}' results in 'switch01-M2'",
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name="librenmssettings",
            name="location_parse_pattern",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Pattern describing the structure of the LibreNMS location string. Available placeholders: {region}, {site}, {location}, {rack}, {tenant}. Literal text between placeholders is treated as a separator. Example: '{site} - {rack}' parses 'NYC - R1' into site='NYC', rack='R1'. Leave blank to match the whole location string against site and location.",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="librenmssettings",
            name="location_parse_is_regex",
            field=models.BooleanField(
                default=False,
                help_text="Treat the location parse pattern as a raw regular expression with named groups (e.g. '(?P<site>[^-]+)-(?P<rack>.+)') instead of placeholders",
            ),
        ),
        migrations.AlterField(
            model_name="locationmapping",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="locationmapping",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="locationmapping",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="locationmapping",
            name="field_type",
            field=models.CharField(
                choices=[("site", "Site"), ("location", "Location"), ("rack", "Rack"), ("tenant", "Tenant")],
                help_text="Which type of NetBox object the LibreNMS value maps to",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="locationmapping",
            name="librenms_value",
            field=models.CharField(
                help_text="Value parsed from the LibreNMS location string (e.g. 'NYC', 'East')", max_length=255
            ),
        ),
        migrations.AlterField(
            model_name="locationmapping",
            name="description",
            field=models.TextField(blank=True, help_text="Optional description or notes about this mapping"),
        ),
        migrations.AlterField(
            model_name="modulebaymapping",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="modulebaymapping",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="modulebaymapping",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="modulebaymapping",
            name="librenms_name",
            field=models.CharField(
                help_text="Name from LibreNMS inventory (entPhysicalName). When 'Use Regex' is enabled, this is a Python regex pattern.",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="modulebaymapping",
            name="librenms_class",
            field=models.CharField(
                blank=True,
                help_text="Optional entPhysicalClass filter (e.g. 'powerSupply', 'fan', 'module')",
                max_length=50,
            ),
        ),
        migrations.AlterField(
            model_name="modulebaymapping",
            name="netbox_bay_name",
            field=models.CharField(
                help_text="NetBox module bay name to match. With regex, supports backreferences (\\1, \\2, etc.).",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="modulebaymapping",
            name="is_regex",
            field=models.BooleanField(
                default=False, help_text="Treat LibreNMS Name as a regex pattern with backreferences in NetBox Bay Name"
            ),
        ),
        migrations.AlterField(
            model_name="modulebaymapping",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional: scope this mapping to one manufacturer (matches the device's device_type.manufacturer). Leave blank to apply across vendors. When both a vendor-scoped and a global mapping match, the vendor-scoped one wins.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="librenms_module_bay_mappings",
                to="dcim.manufacturer",
            ),
        ),
        migrations.AlterField(
            model_name="modulebaymapping",
            name="description",
            field=models.TextField(blank=True, help_text="Optional description or notes about this mapping"),
        ),
        migrations.AlterField(
            model_name="moduletypemapping",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="moduletypemapping",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="moduletypemapping",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="moduletypemapping",
            name="librenms_model",
            field=models.CharField(
                help_text="Model name from LibreNMS inventory (entPhysicalModelName)", max_length=255
            ),
        ),
        migrations.AlterField(
            model_name="moduletypemapping",
            name="netbox_module_type",
            field=models.ForeignKey(
                help_text="The NetBox ModuleType this model name maps to",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="librenms_module_type_mappings",
                to="dcim.moduletype",
            ),
        ),
        migrations.AlterField(
            model_name="moduletypemapping",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional: scope this mapping to one manufacturer (matches the device's device_type.manufacturer). Leave blank to apply across vendors. When both a manufacturer-scoped and a global mapping exist for the same librenms_model, the manufacturer-scoped row wins for devices of that vendor.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="librenms_module_type_mappings",
                to="dcim.manufacturer",
            ),
        ),
        migrations.AlterField(
            model_name="moduletypemapping",
            name="description",
            field=models.TextField(blank=True, help_text="Optional description or notes about this mapping"),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="manufacturer",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional: only apply this rule to items from this manufacturer. Leave blank for vendor-agnostic rules.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="normalization_rules",
                to="dcim.manufacturer",
            ),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="match_pattern",
            field=models.CharField(
                help_text="Regex pattern to match against input string (Python re syntax)", max_length=500
            ),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="replacement",
            field=models.CharField(
                help_text="Replacement string (supports regex back-references \\1, \\2, …)", max_length=500
            ),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="priority",
            field=models.PositiveIntegerField(
                default=100,
                help_text="Lower values run first. Rules chain: each transforms the output of the previous.",
            ),
        ),
        migrations.AlterField(
            model_name="normalizationrule",
            name="description",
            field=models.TextField(blank=True, help_text="Optional description or notes about this rule"),
        ),
        migrations.AlterField(
            model_name="platformmapping",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="platformmapping",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="platformmapping",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="platformmapping",
            name="librenms_os",
            field=models.CharField(
                help_text="OS string as reported by LibreNMS (e.g., 'ios', 'eos', 'junos')", max_length=255, unique=True
            ),
        ),
        migrations.AlterField(
            model_name="platformmapping",
            name="netbox_platform",
            field=models.ForeignKey(
                help_text="The NetBox Platform this OS string maps to",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="librenms_platform_mappings",
                to="dcim.platform",
            ),
        ),
        migrations.AlterField(
            model_name="platformmapping",
            name="description",
            field=models.TextField(blank=True, help_text="Optional description or notes about this mapping"),
        ),
        migrations.AlterField(
            model_name="portstacklagpattern",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="portstacklagpattern",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="portstacklagpattern",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
        migrations.AlterField(
            model_name="serialsensortypepattern",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterField(
            model_name="serialsensortypepattern",
            name="created",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="created"),
        ),
        migrations.AlterField(
            model_name="serialsensortypepattern",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, null=True, verbose_name="last updated"),
        ),
    ]
