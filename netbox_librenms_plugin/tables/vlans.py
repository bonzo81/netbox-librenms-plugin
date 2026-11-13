import django_tables2 as tables
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from netbox.tables.columns import ToggleColumn

from netbox_librenms_plugin.constants import LIBRENMS_VLAN_STATE_ACTIVE
from netbox_librenms_plugin.utils import (
    get_vlan_sync_css_class,
    note_icon,
    paginate_sync_table,
    render_vlan_sync_action,
)


class _VLANSelectionColumn(ToggleColumn):
    """Render a selection control only for rows with a usable VLAN identity."""

    def render(self, value, bound_column, record):
        if record.get("vid_error"):
            return ""
        return super().render(value, bound_column, record)


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
            "status",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-vlan-table",
        }
        row_attrs = {
            "data-vlan-id": lambda record: None if record.get("vid_error") else record.get("vlan_id"),
        }

    def __init__(self, *args, vlan_groups=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tab = "vlans"
        self.prefix = "vlans_"
        self.vlan_groups = vlan_groups or []
        # Donor "migrated mode" renders no form, so per-row submit buttons must become status text.
        self.migrated_to_marker = False

    selection = _VLANSelectionColumn(
        orderable=False,
        visible=True,
        attrs={"td": {"data-col": "selection"}, "input": {"name": "select"}},
        accessor="vlan_id",
    )

    vlan_id = tables.Column(
        accessor="vlan_id",
        verbose_name="VLAN ID",
        attrs={"td": {"data-col": "vlan_id"}},
        empty_values=(),
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

    status = tables.Column(
        verbose_name="Status",
        attrs={"td": {"data-col": "status"}},
        empty_values=(),
        orderable=False,
    )

    def render_vlan_id(self, value, record):
        """Render VLAN ID with color based on sync status."""
        if vid_error := record.get("vid_error"):
            raw_vid = record.get("raw_vlan_id")
            if raw_vid is None:
                raw_vid = "Missing"
            elif raw_vid == "":
                raw_vid = "Empty"
            return format_html(
                '<span class="text-danger">{} <span class="badge text-bg-danger">{}</span></span>',
                raw_vid,
                vid_error,
            )
        css_class = get_vlan_sync_css_class(
            record.get("exists_in_netbox", False),
            record.get("name_matches", True),
        )
        return format_html('<span class="{}">{}</span>', css_class, value)

    def render_name(self, value, record):
        """Render VLAN name with color based on sync status, and the LibreNMS name when a rule changed it."""
        css_class = get_vlan_sync_css_class(
            record.get("exists_in_netbox", False),
            record.get("name_matches", True),
        )
        librenms_name = record.get("librenms_name")
        rule_note = (
            format_html(
                " {}",
                note_icon("mdi-auto-fix text-muted", f"LibreNMS name: {librenms_name}. A VLAN name rule changed it."),
            )
            if librenms_name is not None and librenms_name != value
            else ""
        )

        # Add tooltip on name mismatch
        if record.get("exists_in_netbox") and not record.get("name_matches", True):
            netbox_name = record.get("netbox_vlan_name", "")
            tooltip = f"NetBox: {netbox_name} | LibreNMS: {value}"
            return format_html(
                '<span class="{}" title="{}">{}</span>{}',
                css_class,
                tooltip,
                value or "",
                rule_note,
            )

        return format_html('<span class="{}">{}</span>{}', css_class, value or "", rule_note)

    def render_vlan_group_selection(self, value, record):
        """
        Render per-row VLAN group dropdown.

        Auto-selects based on matching priority:
        1. Existing NetBox VLAN's group (if exists_in_netbox)
        2. Unique VID match (if VID exists in exactly one group)
        3. No selection (with warning icon if ambiguous)
        """
        if record.get("vid_error"):
            return ""
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

        # The writer refuses a row whose proposed name is not the name this page shows.
        select_html = format_html(
            '<input type="hidden" name="expected_vlan_name_{}" value="{}">'
            '<select name="vlan_group_{}" class="form-select form-select-sm vlan-sync-group-select"'
            ' data-vlan-id="{}" data-vlan-name="{}">{}</select>',
            vlan_id,
            record.get("name") or "",
            vlan_id,
            vlan_id,
            record.get("name", ""),
            options_html,
        )

        # Add warning icon if ambiguous (VID exists in multiple groups at same priority level)
        if record.get("is_ambiguous") and not record.get("exists_in_netbox"):
            warning_html = note_icon(
                "mdi-alert text-warning ms-1",
                "VID exists in multiple groups at the same scope level. Please select the target group.",
            )
            return format_html("{}{}", select_html, warning_html)

        return select_html

    def render_state(self, value, record):
        """Render VLAN state (active/inactive)."""
        if value == LIBRENMS_VLAN_STATE_ACTIVE or value == "active":
            return mark_safe('<span class="text-success">Active</span>')
        return mark_safe('<span class="text-muted">Inactive</span>')

    def render_status(self, value, record):
        """Render the action or state for one VLAN synchronization row."""
        if record.get("vid_error"):
            return ""
        return render_vlan_sync_action(
            record.get("vlan_id"),
            record.get("exists_in_netbox", False),
            record.get("name_matches", True),
            actions_enabled=not self.migrated_to_marker,
        )

    def configure(self, request):
        """Configure the table with pagination."""
        paginate_sync_table(self, request)
