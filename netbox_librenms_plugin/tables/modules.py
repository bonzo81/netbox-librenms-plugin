from urllib.parse import urlencode, urlparse

import django_tables2 as tables
from django.urls import reverse
from django.utils.html import escape, format_html, mark_safe
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
        has_write_permission=False,
        can_add_module=False,
        can_change_module=False,
        can_delete_module=False,
        **kwargs,
    ):
        """Initialize table with optional device context."""
        self.device = device
        self.csrf_token = ""
        self.server_key = server_key
        self.has_write_permission = has_write_permission
        self.can_add_module = can_add_module
        self.can_change_module = can_change_module
        self.can_delete_module = can_delete_module
        super().__init__(*args, **kwargs)
        if not (has_write_permission and can_add_module) and hasattr(self, "columns"):
            self.columns["selection"].column.visible = False
        self.tab = "modules"
        self.htmx_url = None
        self.prefix = "modules_"

    def configure(self, request):
        """Configure pagination settings and CSRF token."""
        from django.middleware.csrf import get_token

        self.csrf_token = get_token(request)
        # Use HX-Current-URL (the real browser URL) when available so that
        # after saving a mapping the redirect lands on the browsable tab page
        # (which handles GET) rather than the HTMX-only POST endpoint.
        if request:
            # HX-Current-URL is the real browser URL (full absolute URL).
            # Extract only the path+query so return_url stays relative, which
            # is what NetBox's ObjectEditView expects.  Fall back to the HTMX
            # endpoint path when the header is absent (non-HTMX requests).
            htmx_current = request.headers.get("HX-Current-URL", "")
            if htmx_current:
                parsed = urlparse(htmx_current)
                relative = parsed.path
                if parsed.query:
                    relative = f"{relative}?{parsed.query}"
                self.return_url = relative
            else:
                self.return_url = request.get_full_path()
        else:
            self.return_url = ""
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
            "Installed": "bg-success text-white",
            "Matched": "bg-info text-white",
            "No Bay": "bg-warning text-dark",
            "No Type": "bg-warning text-dark",
            "Unmatched": "bg-secondary text-white",
            "Serial Mismatch": "bg-danger text-white",
            "Name Conflict": "bg-warning text-dark",
            "Type Mismatch": "bg-warning text-dark",
        }
        badge_class = badge_classes.get(value, "bg-secondary text-white")
        warning = record.get("model_warning")

        # More descriptive label when the parent module type simply has no bay templates.
        display_text = "No Bay on Parent" if record.get("no_bay_reason") == "empty_parent_bays" else value

        if warning:
            status_html = format_html(
                '<span class="badge {}" title="{}">{}</span>'
                ' <i class="mdi mdi-alert-outline text-warning" title="{}"></i>',
                badge_class,
                warning,
                display_text,
                warning,
            )
        else:
            status_html = format_html('<span class="badge {}">{}</span>', badge_class, display_text)

        # "Fix Model" badge on the parent row when its installed module type is missing bay templates.
        if record.get("model_incomplete"):
            url = record.get("model_incomplete_url", "")
            name = record.get("model_incomplete_name", "module type")
            title = f"Module type '{name}' has no bay templates — click to add them so sub-components can be installed"
            if url:
                fix_html = format_html(
                    ' <a href="{}" class="badge bg-warning text-dark" title="{}">'
                    '<i class="mdi mdi-wrench-outline"></i> Fix Model</a>',
                    url,
                    title,
                )
            else:
                fix_html = format_html(
                    ' <span class="badge bg-warning text-dark" title="{}">'
                    '<i class="mdi mdi-wrench-outline"></i> Fix Model</span>',
                    title,
                )
            return status_html + fix_html

        # "Fix Device Type" badge when the device type is missing bay templates for this component.
        if record.get("device_type_incomplete"):
            url = record.get("device_type_incomplete_url", "")
            name = record.get("device_type_incomplete_name", "device type")
            title = f"Device type '{name}' is missing bay templates for this component — click to add them"
            if url:
                fix_html = format_html(
                    ' <a href="{}" class="badge bg-warning text-dark" title="{}">'
                    '<i class="mdi mdi-wrench-outline"></i> Fix Device Type</a>',
                    url,
                    title,
                )
            else:
                fix_html = format_html(
                    ' <span class="badge bg-warning text-dark" title="{}">'
                    '<i class="mdi mdi-wrench-outline"></i> Fix Device Type</span>',
                    title,
                )
            return status_html + fix_html

        return status_html

    def render_actions(self, value, record):
        """Render install button for matched modules and install branch for parents."""
        if not self.device:
            return ""
        if not self.has_write_permission:
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
                    '<input type="hidden" name="selected_device_id" value="{}">'
                    '<input type="hidden" name="module_bay_id" value="{}">'
                    '<input type="hidden" name="module_type_id" value="{}">'
                    '<input type="hidden" name="serial" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-success" title="Install module in bay">'
                    '<i class="mdi mdi-download"></i> Install'
                    "</button></form>",
                    url,
                    self.csrf_token,
                    self.server_key,
                    record.get("selected_device_id") or self.device.pk,
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
                    '<input type="hidden" name="selected_device_id" value="{}">'
                    '<input type="hidden" name="parent_index" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-primary ms-1"'
                    ' title="Install this module and all installable children">'
                    '<i class="mdi mdi-file-tree"></i> Install Branch'
                    "</button></form>",
                    url,
                    self.csrf_token,
                    self.server_key,
                    record.get("selected_device_id") or self.device.pk,
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
                    '<input type="hidden" name="selected_device_id" value="{}">'
                    '<input type="hidden" name="module_id" value="{}">'
                    '<input type="hidden" name="serial" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-warning ms-1"'
                    ' title="Update serial in NetBox to match LibreNMS">'
                    '<i class="mdi mdi-sync"></i> Update Serial'
                    "</button></form>",
                    url,
                    self.csrf_token,
                    self.server_key,
                    record.get("selected_device_id") or self.device.pk,
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
                    ' data-selected-device-id="{}"'
                    ' data-preview-url="{}"'
                    ' title="Replace module — opens comparison dialog">'
                    '<i class="mdi mdi-swap-horizontal"></i> Replace'
                    "</button>",
                    record["installed_module_id"],
                    record.get("ent_physical_index", ""),
                    self.server_key or "",
                    record.get("selected_device_id") or self.device.pk,
                    preview_url,
                )
            )

        # Move button for can_install rows where a single serial conflict exists (requires change+delete)
        if (
            self.can_change_module
            and self.can_delete_module
            and record.get("can_move_from")
            and record.get("serial_conflict_module")
            and record.get("module_bay_id")
        ):
            move_url = reverse("plugins:netbox_librenms_plugin:move_module", kwargs={"pk": self.device.pk})
            conflict_module = record["serial_conflict_module"]
            buttons.append(
                format_html(
                    '<form method="post" action="{}" style="display:inline">'
                    '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
                    '<input type="hidden" name="server_key" value="{}">'
                    '<input type="hidden" name="selected_device_id" value="{}">'
                    '<input type="hidden" name="conflict_module_id" value="{}">'
                    '<input type="hidden" name="target_bay_id" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-info ms-1"'
                    ' title="Move module from {} / {} to this bay">'
                    '<i class="mdi mdi-arrow-right"></i> Move'
                    "</button></form>",
                    move_url,
                    self.csrf_token,
                    self.server_key,
                    record.get("selected_device_id") or self.device.pk,
                    conflict_module.pk,
                    record["module_bay_id"],
                    conflict_module.device.name,
                    conflict_module.module_bay.name,
                )
            )

        # "Add mapping" button for No Bay rows where we can suggest a mapping.
        # Opens the ModuleBayMapping create form pre-filled with the regex
        # capturing the trailing-number pattern (e.g. ^0/(\d+)$ -> Slot \1),
        # so one entry covers the whole device-type slot family rather than
        # one mapping per slot.
        if record.get("status") == "No Bay" and record.get("model_suggestion"):
            sug = record["model_suggestion"]
            base_url = reverse("plugins:netbox_librenms_plugin:modulebaymapping_add")
            return_url = getattr(self, "return_url", "") or ""
            qs = urlencode(
                {
                    "librenms_name": sug["librenms_name"],
                    "librenms_class": sug.get("librenms_class") or "",
                    "netbox_bay_name": sug["netbox_bay_name"],
                    "is_regex": "true" if sug.get("is_regex") else "false",
                    "description": sug.get("description") or "",
                    **({"return_url": return_url} if return_url else {}),
                }
            )
            buttons.append(
                format_html(
                    '<a href="{}?{}" class="btn btn-sm btn-outline-primary ms-1"'
                    ' title="Open the ModuleBayMapping create form pre-filled with a suggested regex'
                    " mapping that covers this slot family"
                    '">'
                    '<i class="mdi mdi-plus-box-outline"></i> Add Mapping'
                    "</a>",
                    base_url,
                    qs,
                )
            )

        return mark_safe("".join(buttons)) if buttons else ""

    def format_module_data(self, record):
        """Format a module row for verify endpoint partial updates."""
        return {
            "name": str(self.render_name(record.get("name"), record)),
            "model": str(self.render_model(record.get("model"), record)),
            "serial": str(self.render_serial(record.get("serial"), record)),
            "description": str(self.render_description(record.get("description"), record)),
            "item_class": str(self.render_item_class(record.get("item_class"), record)),
            "module_bay": str(self.render_module_bay(record.get("module_bay"), record)),
            "module_type": str(self.render_module_type(record.get("module_type"), record)),
            "status": str(self.render_status(record.get("status"), record)),
            "actions": str(self.render_actions(None, record)),
        }


class VCModuleTable(LibreNMSModuleTable):
    """Module sync table variant with virtual chassis member selection."""

    device_selection = tables.Column(
        verbose_name="Virtual Chassis Member",
        accessor="selected_device_id",
        orderable=False,
        empty_values=(),
        attrs={"td": {"data-col": "device_selection"}},
        visible=False,
    )

    def __init__(self, *args, device=None, **kwargs):
        super().__init__(*args, device=device, **kwargs)
        if hasattr(self.device, "virtual_chassis") and self.device.virtual_chassis:
            self.columns.show("device_selection")

    def render_device_selection(self, value, record):
        members = self.device.virtual_chassis.members.all()
        selected_device_id = record.get("selected_device_id") or self.device.id
        ent_index = record.get("ent_physical_index", "")

        options = [
            (
                f'<option value="{member.id}"'
                f"{' selected' if str(member.id) == str(selected_device_id) else ''}>"
                f"{escape(member.name)}"
                "</option>"
            )
            for member in members
        ]

        return format_html(
            '<select name="device_selection_{0}" id="device_selection_{0}" '
            'class="form-select vc-member-select" data-module="{0}" data-row-id="{0}">{1}</select>',
            ent_index,
            mark_safe("".join(options)),
        )

    def format_module_data(self, record):
        formatted = super().format_module_data(record)
        if hasattr(self.device, "virtual_chassis") and self.device.virtual_chassis:
            formatted["device_selection"] = str(self.render_device_selection(record.get("selected_device_id"), record))
        return formatted

    class Meta(LibreNMSModuleTable.Meta):
        sequence = [
            "selection",
            "device_selection",
            "name",
            "model",
            "serial",
            "description",
            "item_class",
            "module_bay",
            "module_type",
            "status",
            "actions",
        ]
