import django_tables2 as tables
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from netbox.tables.columns import ToggleColumn

from netbox_librenms_plugin.constants import LIBRENMS_VLAN_STATE_ACTIVE
from netbox_librenms_plugin.sync_filters import FilterColumn, FilterSpec
from netbox_librenms_plugin.utils import (
    get_vlan_sync_css_class,
    note_icon,
    paginate_sync_table,
    render_vlan_sync_action,
    vlan_sync_action_label,
    vlan_sync_comparison,
)

# The values a plain column shows as the table default ("—") instead of calling its render method.
_EMPTY_VALUES = (None, "")

# The label of the group option that selects no group.
_NO_GROUP_LABEL = "-- No Group (Global) --"


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

    def filter_spec(self):
        """Return the filter columns of this table, each with the text its cell shows."""
        columns = self.columns
        return FilterSpec(
            prefix=self.prefix,
            columns=(
                FilterColumn("vlan_id", str(columns["vlan_id"].header), self.text_vlan_id),
                FilterColumn("name", str(columns["name"].header), self.text_name),
                FilterColumn("group", str(columns["vlan_group_selection"].header), self.text_group),
                FilterColumn("type", str(columns["type"].header), self.text_type),
                FilterColumn("state", str(columns["state"].header), self.text_state),
                FilterColumn("status", str(columns["status"].header), self.text_status, has_field=False),
            ),
            sync_state=self.filter_sync_state,
        )

    @staticmethod
    def filter_sync_state(record):
        """Return the row's sync-state filter value, or None for a row with an invalid or duplicate VID."""
        if record.get("vid_error"):
            return None
        return vlan_sync_comparison(record.get("exists_in_netbox", False), record.get("name_matches", True))

    def _cell_text(self, value):
        """Return the text a plain cell shows for *value*: the table default for an empty value."""
        return self.default if value in _EMPTY_VALUES else str(value)

    @staticmethod
    def _invalid_vid_parts(record):
        """Return the source VID text and the error badge text of a row with an invalid or duplicate VID."""
        raw_vid = record.get("raw_vlan_id")
        if raw_vid is None:
            raw_vid = "Missing"
        elif raw_vid == "":
            raw_vid = "Empty"
        return raw_vid, record["vid_error"]

    def text_vlan_id(self, record):
        """Return the visible text of the VLAN ID cell, with the error badge of an invalid row."""
        if record.get("vid_error"):
            return "{} {}".format(*self._invalid_vid_parts(record))
        return str(record.get("vlan_id"))

    def text_name(self, record):
        """Return the visible text of the name cell."""
        return self._cell_text(record.get("name"))

    def text_group(self, record):
        """Return the visible text of the group cell: the option its select shows."""
        if record.get("vid_error"):
            return ""
        group = self._selected_group(record)
        return self._group_label(group) if group is not None else _NO_GROUP_LABEL

    def text_type(self, record):
        """Return the visible text of the type cell."""
        return self._cell_text(record.get("type"))

    def text_state(self, record):
        """Return the visible text of the state cell."""
        value = record.get("state")
        if value in _EMPTY_VALUES:
            return self.default
        return "Active" if self._is_active(value) else "Inactive"

    def text_status(self, record):
        """Return the visible text of the status cell: the row action or the row state."""
        if record.get("vid_error"):
            return ""
        return vlan_sync_action_label(
            record.get("vlan_id"),
            record.get("exists_in_netbox", False),
            record.get("name_matches", True),
            actions_enabled=not self.migrated_to_marker,
        )

    @staticmethod
    def _is_active(value):
        """Return whether a LibreNMS VLAN state is active."""
        return value == LIBRENMS_VLAN_STATE_ACTIVE or value == "active"

    def _selected_group(self, record):
        """
        Return the VLAN group the row's dropdown selects, or None for no group.

        The dropdown selects the existing NetBox VLAN's group first, then a unique VID match. A group
        that is not in the dropdown selects no option, so the dropdown shows no group.
        """
        if record.get("exists_in_netbox") and record.get("netbox_vlan_group_id"):
            selected_group_id = record["netbox_vlan_group_id"]
        else:
            selected_group_id = record.get("auto_selected_group_id")
        return next((group for group in self.vlan_groups if group.pk == selected_group_id), None)

    @staticmethod
    def _group_label(group):
        """Return the text of one group option: the group name and its scope."""
        return f"{group.name} ({group.scope})" if group.scope else group.name

    def render_vlan_id(self, value, record):
        """Render VLAN ID with color based on sync status."""
        if record.get("vid_error"):
            raw_vid, vid_error = self._invalid_vid_parts(record)
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
        # A plain column calls render only for a non-empty value, so the text is the one text_name returns.
        text = value or ""

        # Add tooltip on name mismatch
        if record.get("exists_in_netbox") and not record.get("name_matches", True):
            netbox_name = record.get("netbox_vlan_name", "")
            tooltip = f"NetBox: {netbox_name} | LibreNMS: {value}"
            return format_html('<span class="{}" title="{}">{}</span>{}', css_class, tooltip, text, rule_note)

        return format_html('<span class="{}">{}</span>{}', css_class, text, rule_note)

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
        selected_group = self._selected_group(record)
        selected_pk = selected_group.pk if selected_group is not None else None

        # Build the select element using format_html_join to prevent XSS
        options_html = format_html_join(
            "",
            '<option value="{}" data-scope="{}"{}>{}</option>',
            [("", "", "", _NO_GROUP_LABEL)]
            + [
                (
                    group.pk,
                    group.scope_id if group.scope_id else "",
                    " selected" if group.pk == selected_pk else "",
                    self._group_label(group),
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
        if self._is_active(value):
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
