import django_tables2 as tables
from django.utils.html import format_html
from netbox.tables import NetBoxTable, columns

from netbox_librenms_plugin.models import (
    DeviceTypeMapping,
    InterfaceTypeMapping,
    InventoryIgnoreRule,
    ModuleBayMapping,
    ModuleTypeMapping,
    NormalizationRule,
    PlatformMapping,
)


class InterfaceTypeMappingTable(NetBoxTable):
    """
    Table for displaying InterfaceTypeMapping data.
    """

    librenms_type = tables.Column(verbose_name="LibreNMS Type")
    librenms_speed = tables.Column(verbose_name="LibreNMS Speed (Kbps)")
    netbox_type = tables.Column(verbose_name="NetBox Type")
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        """Meta options for InterfaceTypeMappingTable."""

        model = InterfaceTypeMapping
        fields = (
            "id",
            "librenms_type",
            "librenms_speed",
            "netbox_type",
            "description",
            "actions",
        )
        default_columns = (
            "id",
            "librenms_type",
            "librenms_speed",
            "netbox_type",
            "description",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}


class DeviceTypeMappingTable(NetBoxTable):
    """Table for displaying DeviceTypeMapping data."""

    librenms_hardware = tables.Column(verbose_name="LibreNMS Hardware", linkify=True)
    netbox_device_type = tables.Column(verbose_name="NetBox Device Type", linkify=True)
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        """Meta options for DeviceTypeMappingTable."""

        model = DeviceTypeMapping
        fields = (
            "id",
            "librenms_hardware",
            "netbox_device_type",
            "description",
            "actions",
        )
        default_columns = (
            "id",
            "librenms_hardware",
            "netbox_device_type",
            "description",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}


class ModuleTypeMappingTable(NetBoxTable):
    """Table for displaying ModuleTypeMapping data."""

    librenms_model = tables.Column(verbose_name="LibreNMS Model", linkify=True)
    netbox_module_type = tables.Column(verbose_name="NetBox Module Type", linkify=True)
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        """Meta options for ModuleTypeMappingTable."""

        model = ModuleTypeMapping
        fields = (
            "id",
            "librenms_model",
            "netbox_module_type",
            "description",
            "actions",
        )
        default_columns = (
            "id",
            "librenms_model",
            "netbox_module_type",
            "description",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}


class ModuleBayMappingTable(NetBoxTable):
    """Table for displaying ModuleBayMapping data."""

    librenms_name = tables.Column(verbose_name="LibreNMS Name", linkify=True)
    librenms_class = tables.Column(verbose_name="LibreNMS Class")
    netbox_bay_name = tables.Column(verbose_name="NetBox Bay Name")
    is_regex = columns.BooleanColumn(verbose_name="Regex")
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        """Meta options for ModuleBayMappingTable."""

        model = ModuleBayMapping
        fields = (
            "id",
            "librenms_name",
            "librenms_class",
            "netbox_bay_name",
            "is_regex",
            "description",
            "actions",
        )
        default_columns = (
            "id",
            "librenms_name",
            "librenms_class",
            "netbox_bay_name",
            "is_regex",
            "description",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}


class NormalizationRuleTable(NetBoxTable):
    """Table for displaying NormalizationRule data."""

    scope = tables.Column(verbose_name="Scope", linkify=True)
    manufacturer = tables.Column(verbose_name="Manufacturer", linkify=True)
    match_pattern = tables.Column(verbose_name="Match Pattern")
    replacement = tables.Column(verbose_name="Replacement")
    priority = tables.Column(verbose_name="Priority")
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        """Meta options for NormalizationRuleTable."""

        model = NormalizationRule
        fields = (
            "id",
            "scope",
            "manufacturer",
            "match_pattern",
            "replacement",
            "priority",
            "description",
            "actions",
        )
        default_columns = (
            "id",
            "scope",
            "manufacturer",
            "match_pattern",
            "replacement",
            "priority",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}


class InventoryIgnoreRuleTable(NetBoxTable):
    """Table for displaying InventoryIgnoreRule data."""

    name = tables.Column(verbose_name="Name", linkify=True)
    match_type = tables.Column(verbose_name="Match Type")
    action = tables.Column(verbose_name="Action")
    pattern = tables.Column(verbose_name="Pattern", empty_values=())
    require_serial_match_parent = tables.BooleanColumn(verbose_name="Require Serial Match")
    enabled = tables.BooleanColumn(verbose_name="Enabled")
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    def render_action(self, value, record):
        """Display the human-readable action label."""
        return record.get_action_display()

    def render_pattern(self, value, record):
        """Show dash for serial_matches_device rules where pattern is unused."""
        if record.match_type == InventoryIgnoreRule.MATCH_SERIAL_DEVICE:
            return format_html('<span class="text-muted">—</span>')
        return format_html("<code>{}</code>", value) if value else "—"

    def render_require_serial_match_parent(self, value, record):
        """Show the actual stored boolean for require_serial_match_parent."""
        return (
            format_html('<span class="text-success">Yes</span>')
            if value
            else format_html('<span class="text-danger">No</span>')
        )

    class Meta:
        """Meta options for InventoryIgnoreRuleTable."""

        model = InventoryIgnoreRule
        fields = (
            "id",
            "name",
            "match_type",
            "action",
            "pattern",
            "require_serial_match_parent",
            "enabled",
            "description",
            "actions",
        )
        default_columns = (
            "id",
            "name",
            "match_type",
            "action",
            "pattern",
            "require_serial_match_parent",
            "enabled",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}


class PlatformMappingTable(NetBoxTable):
    """Table for displaying PlatformMapping data."""

    librenms_os = tables.Column(verbose_name="LibreNMS OS", linkify=True)
    netbox_platform = tables.Column(verbose_name="NetBox Platform", linkify=True)
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        """Meta options for PlatformMappingTable."""

        model = PlatformMapping
        fields = (
            "id",
            "librenms_os",
            "netbox_platform",
            "description",
            "actions",
        )
        default_columns = (
            "id",
            "librenms_os",
            "netbox_platform",
            "description",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}
