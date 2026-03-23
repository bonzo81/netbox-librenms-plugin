import django_tables2 as tables
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from netbox.tables.columns import ToggleColumn
from utilities.paginator import EnhancedPaginator

from netbox_librenms_plugin.constants import LIBRENMS_VLAN_STATE_ACTIVE
from netbox_librenms_plugin.utils import get_table_paginate_count, get_vlan_sync_css_class


class LibreNMSVLANTable(tables.Table):
    """
    Table for displaying LibreNMS VLAN data for a device.
    Shows VLANs configured on the device and their sync status with NetBox.
    Includes per-row VLAN group selection dropdown.
    """

    class Meta:
        sequence = [
            "selection",
            "vlan_id",
            "name",
            "vlan_group_selection",
            "type",
            "state",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-vlan-table",
        }
        row_attrs = {
            "data-vlan-id": lambda record: record.get("vlan_id"),
        }

    def __init__(self, *args, vlan_groups=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.prefix = "vlans_"
        self.vlan_groups = vlan_groups or []

    selection = ToggleColumn(
        orderable=False,
        visible=True,
        attrs={"td": {"data-col": "selection"}, "input": {"name": "select"}},
        accessor="vlan_id",
    )

    vlan_id = tables.Column(
        accessor="vlan_id",
        verbose_name="VLAN ID",
        attrs={"td": {"data-col": "vlan_id"}},
    )

    name = tables.Column(
        accessor="name",
        verbose_name="Name",
        attrs={"td": {"data-col": "name"}},
    )

    vlan_group_selection = tables.Column(
        verbose_name="VLAN Group",
        empty_values=(),
        orderable=False,
        attrs={"td": {"data-col": "vlan_group_selection"}},
    )

    type = tables.Column(
        accessor="type",
        verbose_name="Type",
        attrs={"td": {"data-col": "type"}},
    )

    state = tables.Column(
        accessor="state",
        verbose_name="State",
        attrs={"td": {"data-col": "state"}},
    )

    def render_vlan_id(self, value, record):
        """Render VLAN ID with color based on sync status."""
        css_class = get_vlan_sync_css_class(
            record.get("exists_in_netbox", False),
            record.get("name_matches", True),
        )
        return format_html('<span class="{}">{}</span>', css_class, value)

    def render_name(self, value, record):
        """Render VLAN name with color based on sync status."""
        css_class = get_vlan_sync_css_class(
            record.get("exists_in_netbox", False),
            record.get("name_matches", True),
        )

        # Add tooltip on name mismatch
        if record.get("exists_in_netbox") and not record.get("name_matches", True):
            netbox_name = record.get("netbox_vlan_name", "")
            tooltip = f"NetBox: {netbox_name} | LibreNMS: {value}"
            return format_html(
                '<span class="{}" title="{}">{}</span>',
                css_class,
                tooltip,
                value or "",
            )

        return format_html('<span class="{}">{}</span>', css_class, value or "")

    def render_vlan_group_selection(self, value, record):
        """
        Render per-row VLAN group dropdown.

        Auto-selects based on matching priority:
        1. Existing NetBox VLAN's group (if exists_in_netbox)
        2. Unique VID match (if VID exists in exactly one group)
        3. No selection (with warning icon if ambiguous)
        """
        vlan_id = record.get("vlan_id")

        # Determine which group to auto-select
        selected_group_id = None

        # Priority 1: Existing NetBox VLAN group
        if record.get("exists_in_netbox") and record.get("netbox_vlan_group_id"):
            selected_group_id = record["netbox_vlan_group_id"]
        elif record.get("auto_selected_group_id"):
            # Priority 2: unique VID match
            selected_group_id = record["auto_selected_group_id"]

        # Build the select element using format_html_join to prevent XSS
        options_html = format_html_join(
            "",
            '<option value="{}" data-scope="{}"{}>{}{}</option>',
            [
                (
                    "",
                    "",
                    "",
                    "-- No Group (Global) --",
                    "",
                ),
            ]
            + [
                (
                    group.pk,
                    group.scope_id if group.scope_id else "",
                    " selected" if group.pk == selected_group_id else "",
                    group.name,
                    f" ({group.scope})" if group.scope else "",
                )
                for group in self.vlan_groups
            ],
        )

        select_html = format_html(
            '<select name="vlan_group_{}" class="form-select form-select-sm vlan-sync-group-select"'
            ' data-vlan-id="{}" data-vlan-name="{}" style="min-width: 180px;">{}</select>',
            vlan_id,
            vlan_id,
            record.get("name", ""),
            options_html,
        )

        # Add warning icon if ambiguous (VID exists in multiple groups at same priority level)
        if record.get("is_ambiguous") and not record.get("exists_in_netbox"):
            warning_html = mark_safe(
                '<i class="mdi mdi-alert text-warning ms-1" '
                'title="VID exists in multiple groups at the same scope level. Please select the target group."></i>'
            )
            return format_html("{}{}", select_html, warning_html)

        return select_html

    def render_state(self, value, record):
        """Render VLAN state (active/inactive)."""
        if value == LIBRENMS_VLAN_STATE_ACTIVE or value == "active":
            return mark_safe('<span class="text-success">Active</span>')
        return mark_safe('<span class="text-muted">Inactive</span>')

    def configure(self, request):
        """Configure the table with pagination."""
        paginate = {
            "paginator_class": EnhancedPaginator,
            "per_page": get_table_paginate_count(request, self.prefix),
        }
        tables.RequestConfig(request, paginate).configure(self)
