import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html, mark_safe
from netbox.tables.columns import ToggleColumn
from utilities.paginator import EnhancedPaginator

from netbox_librenms_plugin.utils import get_table_paginate_count


class LibreNMSModuleTable(tables.Table):
    """Table for displaying LibreNMS inventory items mapped to NetBox modules."""

    selection = ToggleColumn(
        orderable=False,
        visible=True,
        accessor="ent_physical_index",
        attrs={"td": {"data-col": "selection"}, "input": {"name": "select"}},
    )
    name = tables.Column(
        verbose_name="Name",
        empty_values=(),
        attrs={
            "td": {"data-col": "name"},
            "th": {
                "title": "Name from ENTITY-MIB (entPhysicalName). May differ from interface names in ifDescr/ifName."
            },
        },
    )
    model = tables.Column(verbose_name="Model", empty_values=(), attrs={"td": {"data-col": "model"}})
    serial = tables.Column(verbose_name="Serial", empty_values=(), attrs={"td": {"data-col": "serial"}})
    description = tables.Column(verbose_name="Description", empty_values=(), attrs={"td": {"data-col": "description"}})
    item_class = tables.Column(verbose_name="Class", empty_values=(), attrs={"td": {"data-col": "item_class"}})
    module_bay = tables.Column(verbose_name="Module Bay", empty_values=(), attrs={"td": {"data-col": "module_bay"}})
    module_type = tables.Column(verbose_name="Module Type", empty_values=(), attrs={"td": {"data-col": "module_type"}})
    status = tables.Column(verbose_name="Status", empty_values=(), attrs={"td": {"data-col": "status"}})
    actions = tables.Column(
        verbose_name="Actions", orderable=False, empty_values=(), attrs={"td": {"data-col": "actions"}}
    )

    class Meta:
        attrs = {"class": "table table-hover object-list", "id": "librenms-module-table"}
        row_attrs = {
            "class": lambda record: record.get("row_class", ""),
            "data-ent-index": lambda record: record.get("ent_physical_index", ""),
            "data-status": lambda record: record.get("status", ""),
            "data-depth": lambda record: str(record.get("depth", 0)),
            "data-item-class": lambda record: record.get("item_class", ""),
        }

    def __init__(
        self,
        *args,
        device=None,
        server_key="",
        can_add_module=False,
        can_change_module=False,
        can_delete_module=False,
        **kwargs,
    ):
        """Initialize table with optional device context."""
        self.device = device
        self.csrf_token = ""
        self.server_key = server_key
        self.can_add_module = can_add_module
        self.can_change_module = can_change_module
        self.can_delete_module = can_delete_module
        super().__init__(*args, **kwargs)
        if not can_add_module and hasattr(self, "columns"):
            self.columns["selection"].column.visible = False
        self.tab = "modules"
        self.htmx_url = None
        self.prefix = "modules_"

    def configure(self, request):
        """Configure pagination settings and CSRF token."""
        from django.middleware.csrf import get_token

        self.csrf_token = get_token(request)
        paginate = {"paginator_class": EnhancedPaginator, "per_page": get_table_paginate_count(request, self.prefix)}
        tables.RequestConfig(request, paginate).configure(self)

    def render_name(self, value, record):
        """Render inventory item name with tree indentation for sub-components."""
        depth = record.get("depth", 0)
        if depth == 0:
            return value or "-"
        # Build visual tree prefix based on nesting depth
        padding_px = depth * 20
        prefix = "└─ "
        return format_html('<span style="padding-left:{}px">{}{}</span>', padding_px, prefix, value or "-")

    def render_model(self, value, record):
        """Render model with link to module type if matched."""
        if not value or value == "-":
            return "-"
        if url := record.get("module_type_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_serial(self, value, record):
        """Render serial number."""
        return value or "-"

    def render_description(self, value, record):
        """Render description, truncated for display."""
        if not value:
            return "-"
        if len(value) > 60:
            return format_html('<span title="{}">{}&hellip;</span>', value, value[:57])
        return value

    def render_item_class(self, value, record):
        """Render the entPhysicalClass with an icon."""
        icons = {
            "module": "mdi-expansion-card",
            "ioModule": "mdi-expansion-card",
            "cpmModule": "mdi-expansion-card",
            "mdaModule": "mdi-expansion-card",
            "fabricModule": "mdi-expansion-card",
            "xioModule": "mdi-expansion-card",
            "powerSupply": "mdi-power-plug",
            "fan": "mdi-fan",
            "port": "mdi-ethernet",
            "other": "mdi-card-outline",
        }
        icon = icons.get(value, "mdi-card-outline")
        return format_html('<i class="mdi {} me-1"></i> {}', icon, value)

    def render_module_bay(self, value, record):
        """Render module bay with link if found in NetBox."""
        if not value or value == "-":
            return format_html('<span class="text-danger">{}</span>', "No matching bay")
        if url := record.get("module_bay_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_module_type(self, value, record):
        """Render module type match status."""
        if not value or value == "-":
            return format_html('<span class="text-warning">{}</span>', "No matching type")
        if url := record.get("module_type_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return value

    def render_status(self, value, record):
        """Render sync status with badge."""
        badge_classes = {
            "Installed": "bg-success",
            "Matched": "bg-info",
            "No Bay": "bg-warning",
            "No Type": "bg-warning",
            "Unmatched": "bg-secondary",
            "Serial Mismatch": "bg-danger",
            "Name Conflict": "bg-warning",
            "Type Mismatch": "bg-warning",
        }
        badge_class = badge_classes.get(value, "bg-secondary")
        if warning := record.get("name_conflict_warning"):
            return format_html(
                '<span class="badge {}" title="{}">{}</span>'
                ' <i class="mdi mdi-alert-outline text-warning" title="{}"></i>',
                badge_class,
                warning,
                value,
                warning,
            )
        return format_html('<span class="badge {}">{}</span>', badge_class, value)

    def render_actions(self, value, record):
        """Render install button for matched modules and install branch for parents."""
        if not self.device:
            return ""
        if not self.can_add_module and not self.can_change_module:
            return ""

        buttons = []

        # Single install button (requires add permission)
        if self.can_add_module and record.get("can_install"):
            url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": self.device.pk})
            buttons.append(
                format_html(
                    '<form method="post" action="{}" style="display:inline">'
                    '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
                    '<input type="hidden" name="server_key" value="{}">'
                    '<input type="hidden" name="module_bay_id" value="{}">'
                    '<input type="hidden" name="module_type_id" value="{}">'
                    '<input type="hidden" name="serial" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-success" title="Install module in bay">'
                    '<i class="mdi mdi-download"></i> Install'
                    "</button></form>",
                    url,
                    self.csrf_token,
                    self.server_key,
                    record.get("module_bay_id", ""),
                    record.get("module_type_id", ""),
                    record.get("serial") or "",
                )
            )

        # Install branch button for parents with installable children (requires add)
        if self.can_add_module and record.get("has_installable_children") and record.get("ent_physical_index"):
            url = reverse("plugins:netbox_librenms_plugin:install_branch", kwargs={"pk": self.device.pk})
            buttons.append(
                format_html(
                    '<form method="post" action="{}" style="display:inline">'
                    '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
                    '<input type="hidden" name="server_key" value="{}">'
                    '<input type="hidden" name="parent_index" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-primary ms-1"'
                    ' title="Install this module and all installable children">'
                    '<i class="mdi mdi-file-tree"></i> Install Branch'
                    "</button></form>",
                    url,
                    self.csrf_token,
                    self.server_key,
                    record.get("ent_physical_index", ""),
                )
            )

        # Update serial button for serial mismatch rows (requires change)
        if self.can_change_module and record.get("can_update_serial") and record.get("installed_module_id"):
            url = reverse("plugins:netbox_librenms_plugin:update_module_serial", kwargs={"pk": self.device.pk})
            buttons.append(
                format_html(
                    '<form method="post" action="{}" style="display:inline">'
                    '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
                    '<input type="hidden" name="server_key" value="{}">'
                    '<input type="hidden" name="module_id" value="{}">'
                    '<input type="hidden" name="serial" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-warning ms-1"'
                    ' title="Update serial in NetBox to match LibreNMS">'
                    '<i class="mdi mdi-sync"></i> Update Serial'
                    "</button></form>",
                    url,
                    self.csrf_token,
                    self.server_key,
                    record["installed_module_id"],
                    record.get("serial") or "",
                )
            )

        # Replace button for type/serial mismatch rows (requires add+change+delete)
        if (
            self.can_add_module
            and self.can_change_module
            and self.can_delete_module
            and record.get("can_replace")
            and record.get("installed_module_id")
        ):
            preview_url = reverse(
                "plugins:netbox_librenms_plugin:module_mismatch_preview", kwargs={"pk": self.device.pk}
            )
            buttons.append(
                format_html(
                    '<button type="button" class="btn btn-sm btn-danger ms-1 module-replace-btn"'
                    ' data-module-id="{}" data-ent-index="{}" data-server-key="{}"'
                    ' data-preview-url="{}"'
                    ' title="Replace module — opens comparison dialog">'
                    '<i class="mdi mdi-swap-horizontal"></i> Replace'
                    "</button>",
                    record["installed_module_id"],
                    record.get("ent_physical_index", ""),
                    self.server_key or "",
                    preview_url,
                )
            )

        return mark_safe("".join(buttons)) if buttons else ""
