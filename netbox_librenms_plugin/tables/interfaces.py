import copy
import json as json_module
from functools import cached_property
from typing import NamedTuple

import django_tables2 as tables
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from netbox.tables.columns import BooleanColumn, ToggleColumn
from utilities.paginator import EnhancedPaginator
from utilities.templatetags.helpers import humanize_speed

from netbox_librenms_plugin.constants import OOB_INVENTORY_SOURCE
from netbox_librenms_plugin.interface_diff import (
    ABSENT,
    DIFFERS,
    MATCHES,
    ROW_ABSENT,
    ROW_IN_SYNC,
    compute_row_sync_state,
    interface_enabled_from_port,
    parse_vlan_group_id,
)
from netbox_librenms_plugin.models import InterfaceTypeMapping
from netbox_librenms_plugin.utils import (
    check_vlan_group_matches,
    convert_speed_to_kbps,
    format_mac_address,
    get_interface_name_field,
    get_librenms_device_id,
    get_missing_vlan_warning,
    get_table_paginate_count,
    get_tagged_vlan_css_class,
    get_untagged_vlan_css_class,
    interface_name_fallback_matches_port,
    normalize_librenms_port_id,
    render_vc_member_options,
    resolve_interface_row_device,
    select_interface_type_mapping,
)

# (colour, mdi icon, full status text) per relationship sync status. Colour + icon read at a
# glance; the text is the badge tooltip. Module-level so it isn't re-allocated on every
# _render_relationship_column call (up to three times per row).
_RELATIONSHIP_STATUS_MAP = {
    "match": ("success", "mdi-check-circle", "Match"),
    "mismatch": ("warning", "mdi-alert-circle", "Mismatch"),
    "missing_nb": ("info", "mdi-plus-circle", "Not in NetBox"),
    "missing_lnms": ("secondary", "mdi-database-off", "Not in LibreNMS"),
}


# Per-field sync verdict to the colour the sync tab's key explains: red "not present in NetBox",
# orange "mismatched values", green "matching values".
_VERDICT_CSS_CLASS = {ABSENT: "text-danger", DIFFERS: "text-warning", MATCHES: "text-success"}


class _VlanRowContext(NamedTuple):
    """One row's VLAN evidence plus the NetBox assignment it is compared against."""

    group_map: dict
    missing: list
    exists_in_netbox: bool
    netbox_untagged_vid: int | None
    netbox_untagged_group_id: int | None
    netbox_tagged_vids: set
    netbox_tagged_group_ids: dict


class LibreNMSInterfaceTable(tables.Table):
    """Table for displaying LibreNMS interface data."""

    # Show at most this many VLANs inline; the rest are summarised as "+N more".
    _MAX_INLINE_VLANS = 3

    # NetBox object class these rows sync against. Driven by the table subclass rather than
    # a runtime ``self.device.cluster`` probe — a cluster-less VM has a falsy ``cluster`` and
    # would otherwise be misclassified as a device, sending the sync POST to the wrong endpoint.
    sync_object_type = "device"

    class Meta:
        """Meta options for LibreNMSInterfaceTable."""

        sequence = [
            "selection",
            "name",
            "type",
            "speed",
            "vlans",
            "mac_address",
            "mtu",
            "enabled",
            "description",
            "librenms_id",
            "parent",
            "actions",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table",
        }

    def __init__(self, *args, device=None, interface_name_field=None, vlan_groups=None, server_key=None, **kwargs):
        """Initialize table with device context and interface name field."""
        self.device = device
        self.interface_name_field = interface_name_field or get_interface_name_field()
        self.vlan_groups = vlan_groups or []
        # Default the key so render_librenms_id's get_librenms_device_id(self.server_key) lookup
        # falls back to the "default" server entry; a None key would miss {"default": 42} values.
        self.server_key = server_key or "default"
        # Donor "migrated mode": when set, the bulk sync form is hidden and donors must
        # not mutate relationship state. Suppress the per-row relationship sync buttons too,
        # otherwise librenms_sync.js could still POST them and sync a migrated donor.
        self.migrated_to_marker = False
        # Lazily-built {(librenms_type, librenms_speed): mapping} cache so render_type doesn't run
        # 1-2 InterfaceTypeMapping queries for every interface row (the table is small and static).
        self._interface_type_mapping_cache = None

        # Retarget the two columns per instance. ``base_columns`` and ``_meta`` are class
        # attributes, so writing to them here would retarget every later table in this worker
        # process. ``extra_columns`` is applied to Table.__init__'s own copy, and the accessor
        # must be set before it runs because BoundColumn.accessor is cached during binding.
        # Submit the stable LibreNMS port ID: display names can collide when ifDescr is active.
        selection_column = copy.deepcopy(type(self).base_columns["selection"])
        selection_column.accessor = "port_id"
        name_column = copy.deepcopy(type(self).base_columns["name"])
        name_column.accessor = self.interface_name_field

        super().__init__(
            *args,
            extra_columns=[("selection", selection_column), ("name", name_column)],
            row_attrs={
                "data-interface": lambda record: record.get(self.interface_name_field),
                "data-name": lambda record: record.get(self.interface_name_field),
                "data-enabled": lambda record: (
                    str(record.get("ifAdminStatus")).lower() if record.get("ifAdminStatus") is not None else ""
                ),
                "data-port-id": lambda record: str(record.get("port_id") or ""),
                "data-member-of-lag": lambda record: str(record.get("librenms_lag_port_id") or ""),
                "data-lag-name": lambda record: str(record.get("librenms_lag_name") or ""),
                "data-parent-port-id": lambda record: str(record.get("librenms_parent_port_id") or ""),
                "data-parent-name": lambda record: str(record.get("librenms_parent_name") or ""),
                "data-bridge-port-id": lambda record: str(record.get("librenms_bridge_port_id") or ""),
                "data-bridge-name": lambda record: str(record.get("librenms_bridge_name") or ""),
            },
            **kwargs,
        )
        self.tab = "interfaces"
        self.htmx_url = None
        self.prefix = "interfaces_"

    selection = ToggleColumn(
        orderable=False,
        visible=True,
        attrs={
            "td": {"data-col": "selection"},
            "input": {
                "name": "select",
                "disabled": lambda record: None if record.get("sync_target_resolvable", True) else "disabled",
            },
        },
    )
    name = tables.Column(verbose_name="Name", attrs={"td": {"data-col": "name"}})
    type = tables.Column(
        accessor="ifType",
        verbose_name="Interface Type",
        attrs={"td": {"data-col": "type"}},
    )
    speed = tables.Column(accessor="ifSpeed", verbose_name="Speed", attrs={"td": {"data-col": "speed"}})
    mac_address = tables.Column(
        accessor="ifPhysAddress",
        verbose_name="MAC Address",
        attrs={"td": {"data-col": "mac_address"}},
    )
    mtu = tables.Column(accessor="ifMtu", verbose_name="MTU", attrs={"td": {"data-col": "mtu"}})
    enabled = BooleanColumn(verbose_name="Enabled", attrs={"td": {"data-col": "enabled"}})
    description = tables.Column(
        accessor="ifAlias",
        verbose_name="Description",
        attrs={"td": {"data-col": "description"}},
    )
    librenms_id = tables.Column(
        accessor="port_id",
        verbose_name="LibreNMS ID",
        attrs={"td": {"data-col": "librenms_id"}},
    )
    parent = tables.Column(
        verbose_name="Relationships",
        orderable=False,
        empty_values=(),
        attrs={"td": {"data-col": "parent"}},
    )
    vlans = tables.Column(
        verbose_name="VLANs",
        empty_values=(),
        orderable=False,
        attrs={"td": {"data-col": "vlans"}},
    )
    actions = tables.Column(
        verbose_name="",
        empty_values=(),
        orderable=False,
        attrs={"td": {"data-col": "actions"}},
    )

    def row_sync_state(self, record):
        """
        Return how one row compares to the NetBox interface it resolved to.

        Computed once per row and kept on the row, because every coloured column and the row's
        sync button read the same verdict. ``format_interface_data`` drops the cached value when
        it re-resolves a row, so a member switch cannot repaint against the previous member.

        Args:
            record (dict): The interface table row.

        Returns:
            RowSyncState: The row state and each field's verdict.

        """
        state = record.get("_sync_state")
        if state is None:
            state = compute_row_sync_state(
                record,
                interface_name_field=self.interface_name_field,
                server_key=self.server_key,
                netbox_type=self._row_netbox_type(record),
                vlan_context=self._vlan_row_context(record),
            )
            record["_sync_state"] = state
        return state

    def _row_netbox_type(self, record):
        """Return the NetBox type a sync would write for this row, or None when it has no opinion."""
        # Resolving it reads the mapping table, so skip that for a row with no typed interface to
        # compare against: VM rows carry no type, and an unmatched row compares nothing at all.
        interface = record.get("netbox_interface")
        if interface is None or not hasattr(interface, "type"):
            return None
        mapping = self.get_interface_mapping(record.get("ifType"), convert_speed_to_kbps(record.get("ifSpeed", 0)))
        return mapping.netbox_type if mapping else None

    def render_vlans(self, value, record):
        """
        Render VLANs column showing untagged and tagged VLANs.

        Format: "100(U), 200(T), 300(T)" or "100(U)" for access ports.

        Color logic:
        - Red + warning icon: VLAN not in any NetBox group (cannot sync)
        - Red: Not present in NetBox (no VLAN assigned on interface)
        - Orange: Mismatched (different untagged VLAN assigned)
        - Green: Matching (VLAN matches NetBox assignment)

        Compact display: shows up to 3 VLANs inline, then summarizes.
        An edit button opens the VLAN detail modal.
        Hidden inputs store per-VLAN group assignments for form submission.

        Args:
            value (object): The column value.
            record (dict): The interface table row.

        Returns:
            SafeString: The rendered VLAN summary and controls.

        """
        all_vlans = self._collect_row_vlans(record)
        if not all_vlans:
            return mark_safe("—")

        context = self._vlan_row_context(record)
        summary = self._render_vlan_summary(all_vlans, context)
        inherited = self._render_vlan_inherited_badge(record)

        # Keep the LibreNMS VLAN summary visible, but do not expose or submit NetBox scope
        # details for a row whose owner is outside the user's Device view scope.
        if not record.get("sync_target_resolvable", True):
            return format_html("{}{}", summary, inherited)

        interface_name = record.get(self.interface_name_field, "")
        row_key = self._vlan_row_key(record)
        return format_html(
            '<span title="{}">{}</span>{}{}{}',
            self._render_vlan_tooltip(all_vlans, context),
            summary,
            inherited,
            self._render_vlan_edit_button(record, all_vlans, context, interface_name, row_key),
            self._render_vlan_hidden_inputs(all_vlans, context, interface_name, row_key),
        )

    @staticmethod
    def _render_vlan_inherited_badge(record):
        """Mark a row whose VLANs were filled from the other end of its LAG, not reported."""
        donor = record.get("vlan_inherited_from")
        if not donor:
            return ""
        return format_html(
            ' <i class="mdi mdi-arrow-right-bottom text-muted" title="Inherited from {}:'
            ' LibreNMS reported no VLANs on this port"></i>',
            donor,
        )

    @staticmethod
    def _collect_row_vlans(record):
        """Return the row's VLANs as ``(type, vid)`` pairs: untagged first, then tagged by vid."""
        all_vlans = []
        untagged = record.get("untagged_vlan")
        if untagged:
            all_vlans.append(("U", untagged))
        all_vlans.extend(("T", vid) for vid in sorted(record.get("tagged_vlans", [])))
        return all_vlans

    @staticmethod
    def _vlan_row_key(record):
        """Return the port id the sync view reads back out of the rendered form field names."""
        # _sync_interface_vlans() reads vlan_group_<canonical port id>_<vid>, so a raw value such
        # as "010" would render a key the view never looks up and the override would be dropped.
        canonical_port_id = normalize_librenms_port_id(record.get("port_id"))
        return str(canonical_port_id) if canonical_port_id is not None else str(record.get("port_id") or "")

    @staticmethod
    def _vlan_row_context(record):
        """Bundle the row's VLAN evidence with the NetBox assignments it is compared against."""
        netbox_untagged_vid = None
        netbox_untagged_group_id = None
        netbox_tagged_vids = set()
        netbox_tagged_group_ids = {}
        netbox_interface = record.get("netbox_interface")
        if netbox_interface:
            if netbox_interface.untagged_vlan:
                netbox_untagged_vid = netbox_interface.untagged_vlan.vid
                netbox_untagged_group_id = netbox_interface.untagged_vlan.group_id
            for vlan in netbox_interface.tagged_vlans.all():
                netbox_tagged_vids.add(vlan.vid)
                netbox_tagged_group_ids[vlan.vid] = vlan.group_id
        return _VlanRowContext(
            group_map=record.get("vlan_group_map", {}),
            missing=record.get("missing_vlans", []),
            exists_in_netbox=record.get("exists_in_netbox", False),
            netbox_untagged_vid=netbox_untagged_vid,
            netbox_untagged_group_id=netbox_untagged_group_id,
            netbox_tagged_vids=netbox_tagged_vids,
            netbox_tagged_group_ids=netbox_tagged_group_ids,
        )

    def _vlan_css_class(self, context, vlan_type, vid):
        """Return one VLAN's colour class. The inline summary and the modal must agree on it."""
        selected_gid = parse_vlan_group_id(context.group_map.get(vid, {}).get("group_id", ""))
        group_matches = check_vlan_group_matches(
            vlan_type,
            vid,
            selected_gid,
            context.netbox_untagged_group_id,
            context.netbox_tagged_group_ids,
            context.netbox_untagged_vid,
            context.netbox_tagged_vids,
        )
        if vlan_type == "U":
            return get_untagged_vlan_css_class(
                vid, context.netbox_untagged_vid, context.exists_in_netbox, context.missing, group_matches
            )
        return get_tagged_vlan_css_class(
            vid, context.netbox_tagged_vids, context.exists_in_netbox, context.missing, group_matches
        )

    def _render_vlan_summary(self, all_vlans, context):
        """Render up to three coloured VLANs inline, then count the rest."""
        inline_parts = []
        for vlan_type, vid in all_vlans[: self._MAX_INLINE_VLANS]:
            # Escape the LibreNMS-sourced vid/vlan_type (XSS, issue #105 class). css is an
            # internal class name; warning is the static icon HTML from get_missing_vlan_warning,
            # so it is marked safe rather than escaped.
            inline_parts.append(
                format_html(
                    '<span class="{}">{}({}){}</span>',
                    self._vlan_css_class(context, vlan_type, vid),
                    vid,
                    vlan_type,
                    mark_safe(get_missing_vlan_warning(vid, context.missing)),
                )
            )
        # inline_parts are already escaped SafeStrings; join them and keep the result safe.
        summary = mark_safe(", ".join(str(part) for part in inline_parts))
        if len(all_vlans) > self._MAX_INLINE_VLANS:
            summary = format_html(
                '{} <span class="text-muted">+{} more</span>', summary, len(all_vlans) - self._MAX_INLINE_VLANS
            )
        return summary

    @staticmethod
    def _render_vlan_tooltip(all_vlans, context):
        """Render the title attribute naming the group each VLAN resolved to."""
        # Escape the LibreNMS-sourced vid/vlan_type and group_name; the "&#10;" separator is a
        # literal newline entity for the title attribute, so join the escaped lines and mark the
        # whole tooltip safe.
        tooltip_lines = []
        for vlan_type, vid in all_vlans:
            if vid in context.missing:
                tooltip_lines.append(format_html("VLAN {}({}) → ⚠ Not in NetBox", vid, vlan_type))
            else:
                group_name = context.group_map.get(vid, {}).get("group_name", "Global")
                tooltip_lines.append(format_html("VLAN {}({}) → {}", vid, vlan_type, group_name))
        return mark_safe("&#10;".join(str(line) for line in tooltip_lines))

    @staticmethod
    def _render_vlan_hidden_inputs(all_vlans, context, interface_name, row_key):
        """Render the per-VLAN group inputs the sync view reads on submit."""
        hidden_inputs = [
            format_html(
                '<input type="hidden" name="vlan_group_{}_{}" '
                'value="{}" class="vlan-group-hidden" '
                'data-interface="{}" data-vid="{}">',
                row_key,
                vid,
                context.group_map.get(vid, {}).get("group_id", ""),
                interface_name,
                vid,
            )
            for _vlan_type, vid in all_vlans
        ]
        return mark_safe("".join(str(field) for field in hidden_inputs))

    def _vlan_modal_json(self, all_vlans, context):
        """Serialize the per-VLAN state the edit modal renders."""
        vlan_json_items = []
        for vlan_type, vid in all_vlans:
            group_info = context.group_map.get(vid, {})
            is_missing = vid in context.missing
            vlan_json_items.append(
                {
                    "vid": vid,
                    "type": vlan_type,
                    "group_id": group_info.get("group_id", ""),
                    "group_name": "Not in NetBox" if is_missing else group_info.get("group_name", "Global"),
                    "css": self._vlan_css_class(context, vlan_type, vid),
                    "missing": is_missing,
                }
            )
        return json_module.dumps(vlan_json_items)

    def _vlan_group_options_json(self, record):
        """Serialize the group dropdown options, global first."""
        group_options = [{"id": "", "name": "-- No Group (Global) --", "scope": ""}]
        for group in record.get("vlan_groups", self.vlan_groups):
            scope_info = str(group.scope) if hasattr(group, "scope") and group.scope else ""
            group_options.append({"id": str(group.pk), "name": group.name, "scope": scope_info})
        return json_module.dumps(group_options)

    def _render_vlan_edit_button(self, record, all_vlans, context, interface_name, row_key):
        """Render the button that opens the VLAN group modal for this row."""
        device_id = record.get("selected_object_id") or (self.device.pk if self.device else "")
        return format_html(
            '<button type="button" class="btn btn-sm btn-link p-0 ms-1 vlan-edit-btn" '
            'data-interface="{}" '
            'data-row-key="{}" '
            'data-device-id="{}" '
            "data-vlans='{}' "
            "data-vlan-groups='{}' "
            'title="Edit VLAN group assignments">'
            '<i class="mdi mdi-pencil"></i></button>',
            interface_name,
            row_key,
            device_id,
            # Escape the JSON for safe embedding in the HTML attributes.
            escape(self._vlan_modal_json(all_vlans, context)),
            escape(self._vlan_group_options_json(record)),
        )

    def render_speed(self, value, record):
        """Render interface speed with appropriate styling based on comparison with NetBox."""
        kbps_value = convert_speed_to_kbps(value)
        return self._render_field(humanize_speed(kbps_value), record, "speed")

    def render_name(self, value, record):
        """Render interface name with appropriate styling based on comparison with NetBox."""
        # Row markers (OOB, Shared LOM) belong to the relationship column; see render_parent.
        return self._render_field(value, record, "name")

    def render_enabled(self, value, record):
        """Render the enabled state the sync would write, coloured by how NetBox compares."""
        # Read the row, not the column value: the two callers pass different things (the bound
        # column passes the row's parsed "enabled", the row re-render passes raw ifAdminStatus),
        # and only the row carries the rule the writer applies to an absent ifAdminStatus.
        display_value = "Enabled" if interface_enabled_from_port(record) else "Disabled"
        return self._render_field(display_value, record, "enabled")

    def render_description(self, value, record):
        """Render interface description with appropriate styling based on comparison with NetBox."""
        return self._render_field(value, record, "description")

    def render_mac_address(self, value, record):
        """Render MAC address with appropriate styling based on comparison with NetBox."""
        formatted_mac = format_mac_address(value)
        return self._render_field(formatted_mac, record, "mac_address")

    def render_mtu(self, value, record):
        """Render MTU with appropriate styling based on comparison with NetBox."""
        return self._render_field(value, record, "mtu")

    def render_librenms_id(self, value, record):
        """
        Render the LibreNMS port_id, coloured by how it compares to NetBox.

        Red when the interface doesn't exist in NetBox or carries no librenms_id custom
        field, orange when the stored id differs from this LibreNMS port_id, green when
        they match.

        Args:
            value: The LibreNMS port_id to render.
            record (dict): The table row, read for NetBox interface/existence state.

        Returns:
            SafeString: The coloured ``<span>`` markup for the port_id.

        """
        state = self.row_sync_state(record)
        if state.state == ROW_ABSENT:
            return format_html('<span class="text-danger">{}</span>', value)

        # The verdict decides whether a sync would write the id; the stored value is read only to
        # name it in the tooltip, and to split "never stored" from "stored something else".
        netbox_librenms_id = get_librenms_device_id(record["netbox_interface"], self.server_key, auto_save=False)
        if netbox_librenms_id is None:
            return format_html(
                '<span class="text-danger" title="No librenms_id custom field value found">{}</span>', value
            )
        if state.verdict("librenms_id") == DIFFERS:
            return format_html(
                '<span class="text-warning" title="Existing LibreNMS ID: {}">{}</span>', netbox_librenms_id, value
            )
        return format_html('<span class="text-success">{}</span>', value)

    def render_parent(self, value, record):
        """
        Render the combined relationship column.

        Show LAG, parent, and bridge relationships stacked vertically.
        each rendered as a single compact badge combining the relationship type, LibreNMS
        name, and status icon (see ``_render_relationship_column``). The sync buttons keep
        Each item keeps its relationship-specific CSS class for the shared JavaScript handler.

        Args:
            value: The cell value (unused; the row drives rendering).
            record (dict): The table row with relationship status and name fields.

        Returns:
            SafeString: The stacked relationship markup, or empty when neither LAG nor
                parent applies.

        """
        parts = []

        # Where the row came from, before what it is attached to. Both markers describe the row
        # itself rather than a NetBox relationship, so they lead the stack and carry no sync
        # button. The cable and module tables still badge their name column: those have no
        # relationship column to move into.
        if record.get("_source") == OOB_INVENTORY_SOURCE:
            parts.append(self._render_row_marker_pill("purple", "mdi-chip", "OOB", "From OOB controller"))
        if record.get("_dedup_conflict"):
            parts.append(
                self._render_row_marker_pill(
                    "warning",
                    "mdi-content-duplicate",
                    "Shared LOM",
                    "Same MAC seen on both main and OOB",
                )
            )

        if record.get("host_name_collision"):
            parts.append(
                self._render_row_marker_pill(
                    "danger",
                    "mdi-alert-circle",
                    "Name conflict",
                    "The host interface of the same name owns it; the OOB port is not synced",
                )
            )

        lag_status = record.get("lag_sync_status")
        # LAG membership is device-only — VMInterface has no `lag` field and SyncInterfaceLagView
        # 404s virtualmachine, so never render a LAG line/button on a VM table (it could only
        # error). Parent/sub-interface sync is still supported for VMs and rendered below.
        if lag_status is not None and self.sync_object_type != "virtualmachine":
            parts.append(
                self._render_relationship_column(
                    type_label="LAG",
                    lnms_name=record.get("librenms_lag_name"),
                    lnms_port_id=record.get("librenms_lag_port_id"),
                    sync_status=lag_status,
                    record=record,
                    btn_class="lag-sync-btn",
                    data_related_key="data-lag-port-id",
                    target_resolvable=record.get("lag_target_resolvable", True),
                )
            )

        parent_status = record.get("parent_sync_status")
        if parent_status is not None:
            parts.append(
                self._render_relationship_column(
                    type_label="Parent",
                    lnms_name=record.get("librenms_parent_name"),
                    lnms_port_id=record.get("librenms_parent_port_id"),
                    sync_status=parent_status,
                    record=record,
                    btn_class="parent-sync-btn",
                    data_related_key="data-parent-port-id",
                    target_resolvable=record.get("parent_target_resolvable", True),
                )
            )

        bridge_status = record.get("bridge_sync_status")
        if bridge_status is not None:
            parts.append(
                self._render_relationship_column(
                    type_label="Bridge",
                    lnms_name=record.get("librenms_bridge_name"),
                    lnms_port_id=record.get("librenms_bridge_port_id"),
                    sync_status=bridge_status,
                    record=record,
                    btn_class="bridge-sync-btn",
                    data_related_key="data-bridge-port-id",
                    target_resolvable=record.get("bridge_target_resolvable", True),
                )
            )

        if not parts:
            return mark_safe("")

        return mark_safe("".join(str(p) for p in parts))

    @cached_property
    def _vc_members(self):
        """
        Prefetch the chassis member Devices once per table render.

        Both :meth:`_vc_members_by_position` (per-row owner resolution) and
        :meth:`VCInterfaceTable.render_device_selection` (the per-row member dropdown) need the
        member list; resolving it here keeps ``members.all()`` to a single query per render
        instead of one per row (an N+1 on a large chassis table).

        Returns:
            list[Device]: The chassis members available to this table.

        """
        device = self.device
        if device is None or not getattr(device, "virtual_chassis", None):
            return []
        try:
            members = list(device.virtual_chassis.members.all())
            allowed_ids = getattr(self, "allowed_vc_member_ids", None)
            return members if allowed_ids is None else [member for member in members if member.pk in allowed_ids]
        except (TypeError, AttributeError):
            # A non-iterable or attribute-less stand-in device in a unit test.
            return []

    @cached_property
    def _vc_members_by_position(self):
        """
        Prefetch ``{vc_position: member Device}`` once per table render.

        :meth:`_resolve_row_member_id` is hit per row from BOTH the relationship sync button and
        the VC member dropdown, and its name-based fallback otherwise issues a
        ``members.get(vc_position=...)`` query per unresolved row. This creates a quadratic query
        load on a large chassis table. Resolving from this map keeps it O(1) per row (one prefetch total).

        Returns:
            dict[int, Device]: The chassis members keyed by virtual chassis position.

        """
        return {member.vc_position: member for member in self._vc_members if member.vc_position is not None}

    def _resolve_row_member_id(self, record):
        """
        Resolve the id of the device/VM that owns this row's interface.

        The relationship sync button (``data-object-id``) and the VC member dropdown
        (:meth:`VCInterfaceTable.render_device_selection`) must agree on the owner: the JS posts
        the dropdown's value as the object id, so if the button resolved a different device the
        sync POSTs to the wrong member and 404s (a non-ethernet sub-interface owned by another
        member is the classic case). Both call this. Preference, most to least authoritative:
        (1) the matched NetBox interface's device, (2) the row-selected object stamped during
        enrichment or the cross-page verify path, (3) the shared guarded name heuristic for an
        unbound physical row, (4) the viewed device.

        Args:
            record (dict): The interface table row whose owner is resolved.

        Returns:
            int | str: The owning object's ID, or an empty string when no device is available.

        """
        nb_iface = record.get("netbox_interface")
        if nb_iface is not None and getattr(nb_iface, "device_id", None):
            return nb_iface.device_id
        row_object_id = record.get("selected_object_id")
        if row_object_id:
            return row_object_id
        if self.device is not None and getattr(self.device, "virtual_chassis", None):
            return resolve_interface_row_device(
                self.device,
                record,
                self.interface_name_field,
                members_by_position=self._vc_members_by_position or None,
            ).pk
        return self.device.pk if self.device else ""

    @staticmethod
    def _render_row_marker_pill(color, icon, label, title):
        """
        Render one row-origin pill in the relationship column's badge language.

        Matches the wrapper and badge classes :meth:`_render_relationship_column` emits, so the
        markers stack with the LAG/Parent/Bridge pills instead of reading as a separate control.
        Tabler's light (``-lt``) variants ship their own readable text colour in both themes.

        Args:
            color (str): Tabler colour name, used as ``bg-<color>-lt``.
            icon (str): Material Design icon class.
            label (str): The short pill text.
            title (str): The hover description.

        Returns:
            SafeString: The pill markup.

        """
        return format_html(
            '<div class="text-nowrap lh-sm">'
            '<span class="badge bg-{}-lt fw-normal d-inline-flex align-items-center gap-1" title="{}">'
            '<i class="mdi {}"></i>{}</span></div>',
            color,
            title,
            icon,
            label,
        )

    def _render_relationship_column(
        self,
        lnms_name,
        lnms_port_id,
        sync_status,
        record,
        btn_class,
        data_related_key,
        type_label="",
        target_resolvable=True,
    ):
        """
        Render one compact relationship pill.

        Renders a Tabler light (``-lt``) badge holding a status icon + the relationship
        ``type_label`` + the LibreNMS name, with the full status text in the badge
        ``title``. Status is conveyed by colour + icon rather than a long inline word
        (e.g. "Not in LibreNMS"), so the column stays glanceable and doesn't clump/wrap
        to several lines on narrow screens. The ``-lt`` variants ship their own
        readable text colour in both light and dark themes (and are exempt from the
        bare-``bg-*`` badge guard).

        Args:
            lnms_name: The LibreNMS-side relationship name to display.
            lnms_port_id: The LibreNMS port_id of the related interface (drives the
                sync button).
            sync_status: The relationship sync status (match/mismatch/missing_nb/
                missing_lnms), or None to render nothing.
            record (dict): The table row, read for port/interface context.
            btn_class (str): The sync-button CSS class.
            data_related_key (str): The data attribute carrying the related port_id.
            type_label (str): The short relationship label ("LAG" / "Parent").
            target_resolvable: Whether the relationship target can be resolved for synchronization.

        Returns:
            SafeString: The pill markup (plus a sync button when applicable).

        """
        # Colour + icon read at a glance; the text is the tooltip. Map hoisted to the module-level
        # _RELATIONSHIP_STATUS_MAP so it isn't rebuilt on every call.
        color, icon, status_text = _RELATIONSHIP_STATUS_MAP.get(
            sync_status, ("secondary", "mdi-help-circle", sync_status)
        )
        badge_css = f"bg-{color}-lt"

        # format_html() escapes its args, so it's the single escape point for the name.
        # (A manual escape() here was redundant — it returns a SafeString that format_html's
        # conditional_escape passes through, so it didn't double-encode, just obscured intent.)
        display_name = lnms_name or ""
        if type_label and display_name:
            badge_text = format_html("{} {}", type_label, display_name)
        elif display_name:
            badge_text = display_name
        else:
            badge_text = type_label  # may be "" (e.g. missing_lnms with no name) → icon-only pill
        title = f"{type_label}: {status_text}" if type_label else status_text
        badge = format_html(
            '<span class="badge {} fw-normal d-inline-flex align-items-center gap-1" title="{}">'
            '<i class="mdi {}"></i>{}</span>',
            badge_css,
            title,
            icon,
            badge_text,
        )

        # Show the inline sync button when LibreNMS has a relationship to apply (lnms_port_id
        # set) and NetBox either lacks it (missing_nb) or holds a DIFFERENT one (mismatch) —
        # in both cases the row can be reconciled to the LibreNMS value from here. missing_lnms
        # is excluded by the lnms_port_id guard (nothing to sync to), and a migrated donor page
        # suppresses the control entirely: the per-row relationship buttons POST
        # directly via librenms_sync.js, so leaving it active would let a migrated donor mutate
        # parent/LAG state despite the bulk form being hidden.
        if (
            sync_status in ("missing_nb", "mismatch")
            and lnms_port_id
            and record.get("netbox_interface") is not None
            and record.get("relationship_source_resolvable", True)
            and target_resolvable
            and not self.migrated_to_marker
        ):
            port_id = record.get("port_id") or ""
            # Resolve the owning member the same way the VC member dropdown does, so the button's
            # data-object-id and the dropdown agree (the JS posts the dropdown value, so a
            # disagreement would 404). See _resolve_row_member_id.
            object_id = self._resolve_row_member_id(record)
            if not object_id:
                # No resolvable owner. reverse() would raise NoReverseMatch and take down the
                # whole table render, so degrade this one cell the way target_resolvable does.
                return format_html('<div class="text-nowrap lh-sm">{}</div>', badge)
            object_type = record.get("selected_object_type") or self.sync_object_type
            route_name = {
                "lag-sync-btn": "sync_interface_lag",
                "parent-sync-btn": "sync_interface_parent",
                "bridge-sync-btn": "sync_interface_bridge",
            }[btn_class]
            sync_url = reverse(
                f"plugins:netbox_librenms_plugin:{route_name}",
                kwargs={"object_type": object_type, "object_id": object_id},
            )
            # A mismatch click overwrites the differing NetBox relationship with the LibreNMS
            # value, so spell that out in the tooltip rather than the generic "Sync".
            sync_title = (
                f"Update {type_label or 'relationship'} to match LibreNMS"
                if sync_status == "mismatch"
                else "Sync relationship"
            )
            btn = format_html(
                ' <button type="button" class="btn btn-sm btn-link p-0 {}" '
                'data-port-id="{}" {}="{}" '
                'data-object-type="{}" data-object-id="{}" '
                'data-sync-url="{}" '
                'title="{}" aria-label="{}">'
                '<i class="mdi mdi-sync"></i></button>',
                btn_class,
                port_id,
                data_related_key,
                lnms_port_id,
                object_type,
                object_id,
                sync_url,
                sync_title,
                sync_title,
            )
            # text-nowrap keeps the pill + sync button on one line (no mid-line wrap); lh-sm keeps
            # the LAG/Parent lines tightly stacked.
            return format_html('<div class="text-nowrap lh-sm">{} {}</div>', badge, btn)

        return format_html('<div class="text-nowrap lh-sm">{}</div>', badge)

    def _field_css_class(self, record, field):
        """Return one field's colour, read from the row's single sync verdict."""
        return _VERDICT_CSS_CLASS[self.row_sync_state(record).verdict(field)]

    def _render_field(self, value, record, field):
        """Render a field value coloured by how a sync would treat it."""
        # value is an untrusted LibreNMS field (ifName, description, MAC, …). Use format_html so
        # it is auto-escaped — a device reporting e.g. ifName="<img src=x onerror=alert(1)>" must
        # not render as live HTML (stored XSS, issue #105). The class names stay literal.
        return format_html('<span class="{}">{}</span>', self._field_css_class(record, field), value)

    def render_actions(self, value, record):
        """
        Render the button that syncs this one row.

        A plain submit inside the tab's existing form, carrying the row's LibreNMS port ID the
        way the cables tab does, so the row goes through the same view, permissions and cache
        checks as the bulk action. Shown only where a sync would do something: the row must
        differ from NetBox, and its target must be one this user can write.

        Args:
            value (object): The column value, unused.
            record (dict): The interface table row.

        Returns:
            SafeString: The button markup, or an empty cell.

        """
        # A migrated donor renders no form at all, so a submit button here would do nothing.
        if self.migrated_to_marker or not record.get("sync_target_resolvable", True):
            return ""
        if record.get("_source") == OOB_INVENTORY_SOURCE and (
            record.get("host_name_collision") or record.get("_dedup_conflict")
        ):
            return ""
        if self.row_sync_state(record).state == ROW_IN_SYNC:
            return ""
        port_id = normalize_librenms_port_id(record.get("port_id"))
        if port_id is None:
            return ""
        return format_html(
            '<button type="submit" class="btn btn-sm btn-primary" name="sync_one" value="{}"'
            ' title="Sync only this interface">Sync</button>',
            port_id,
        )

    def render_type(self, value, record):
        """Render interface type with appropriate styling based on comparison with NetBox."""
        speed = convert_speed_to_kbps(record.get("ifSpeed", 0))
        mapping = self.get_interface_mapping(value, speed)
        tooltip_value, icon = self.render_mapping_tooltip(value, speed, mapping)

        combined_display = format_html("{} {}", tooltip_value, icon)

        state = self.row_sync_state(record)
        # An ifType with no mapping stays red even on a matched row: the sync holds no opinion on
        # the type, and the column is the only place that gap is visible. It is not a difference,
        # so it never puts the row's own state into "differs".
        if state.state == ROW_ABSENT or mapping is None:
            return format_html('<span class="text-danger">{}</span>', combined_display)
        return format_html('<span class="{}">{}</span>', self._field_css_class(record, "type"), combined_display)

    def get_interface_mapping(self, librenms_type, speed):
        """
        Get interface type mapping based on type and speed.

        Resolves from a single in-memory snapshot of the (small, static)
        InterfaceTypeMapping table, built on first use, so a table render doesn't
        issue 1-2 queries per interface row.

        Args:
            librenms_type (str): The LibreNMS interface type.
            speed (int | None): The interface speed in kilobits per second.

        Returns:
            InterfaceTypeMapping | None: The mapping the sync writer would apply, if any.

        """
        if getattr(self, "_interface_type_mapping_cache", None) is None:
            cache = {}
            for m in InterfaceTypeMapping.objects.all():
                cache.setdefault(m.librenms_type, []).append(m)
            self._interface_type_mapping_cache = cache

        # The writer's rule, not a table-local one, so the row cannot claim a gap the sync
        # does not have.
        return select_interface_type_mapping(self._interface_type_mapping_cache.get(librenms_type, ()), speed)

    def render_mapping_tooltip(self, value, speed, mapping):
        """Render tooltip for interface type mapping."""
        if mapping:
            display = mapping.netbox_type
            icon = format_html(
                '<i class="mdi mdi-link-variant" title="Mapped from LibreNMS type: {} (Speed: {})"></i>',
                value,
                speed,
            )
        else:
            display = value
            # Name the ifType: the gap is only fixable if the user knows which mapping to add.
            icon = format_html(
                '<i class="mdi mdi-link-variant-off" title="No InterfaceTypeMapping for ifType'
                ' {}; the sync leaves the NetBox type unchanged"></i>',
                value,
            )
        return display, icon

    def format_interface_data(self, port_data, device):
        """Format single interface data using table rendering logic."""
        # Add NetBox interface data
        interface_name = port_data.get(self.interface_name_field)

        # OOB-controller rows live on a SEPARATE LibreNMS device, so they must never bind to a
        # host interface BY NAME: a row-level re-render (the VC member dropdown via
        # SingleInterfaceVerifyView) would flip an unmatched row to green "matched" against an
        # unrelated host interface. A binding already resolved by the stable port_id is kept --
        # an OOB port syncs onto this device, so it can legitimately own an interface here.
        if port_data.get("_source") == OOB_INVENTORY_SOURCE:
            port_data.setdefault("netbox_interface", None)
        # Preserve a netbox_interface already resolved by the stable port_id (e.g. the single-
        # interface verify view resolves by port_id first). Only fall back to the fragile name
        # lookup when nothing has been resolved yet, so a display-name change or collision can't
        # clobber the correct port-id match with the wrong (or no) name-matched interface.
        elif not port_data.get("netbox_interface"):
            candidate = device.interfaces.filter(name=interface_name).first()
            port_data["netbox_interface"] = (
                candidate
                if candidate
                and port_data.get("name_fallback_allowed", False)
                and interface_name_fallback_matches_port(
                    candidate,
                    port_data.get("port_id"),
                    self.server_key,
                )
                else None
            )
        port_data["exists_in_netbox"] = bool(port_data["netbox_interface"])
        # This row has just been re-resolved against a different member, so any verdict cached
        # from the previous render is stale.
        port_data.pop("_sync_state", None)

        # Stamp the row's actual object so the relationship sync button targets it even when the
        # row has no matching NetBox interface yet (missing_nb). This is set here, where the
        # caller passes the row-selected device (e.g. the cross-page VC member switch), so the
        # missing_nb branch in _render_relationship_column can prefer it over the
        # name-based VC heuristic, which would otherwise post to the wrong device.
        port_data["selected_object_id"] = getattr(device, "pk", None)
        port_data["selected_object_type"] = self.sync_object_type

        # Clear description if it matches interface name
        if port_data["ifAlias"] == port_data["ifName"] or port_data["ifAlias"] == port_data["ifDescr"]:
            port_data["ifAlias"] = ""

        formatted_data = {
            "name": self.render_name(interface_name, port_data),
            "type": self.render_type(port_data["ifType"], port_data),
            "speed": self.render_speed(port_data["ifSpeed"], port_data),
            "mac_address": self.render_mac_address(port_data["ifPhysAddress"], port_data),
            "mtu": self.render_mtu(port_data["ifMtu"], port_data),
            "enabled": self.render_enabled(port_data["ifAdminStatus"], port_data),
            "description": self.render_description(port_data["ifAlias"], port_data),
            "vlans": self.render_vlans(None, port_data),
            # The librenms_id badge's colour is member-specific (it compares this port_id
            # against the resolved NetBox interface's device librenms_id), so a VC member
            # switch must repaint it too — otherwise it keeps the previous member's
            # match/mismatch state. The column accessor is "port_id" (see the column def).
            "librenms_id": self.render_librenms_id(port_data.get("port_id"), port_data),
            # Render from the relationship enrichment keys the caller stamps onto
            # port_data; absent enrichment it returns "" (safe empty cell).
            "parent": self.render_parent(None, port_data),
            # The row's own sync button: its visibility follows the re-resolved member's diff,
            # so a member switch that brings the row into sync must clear it.
            "actions": self.render_actions(None, port_data),
        }
        for relation in ("lag", "parent", "bridge"):
            for attribute in ("port_id", "name"):
                key = f"librenms_{relation}_{attribute}"
                formatted_data[key] = port_data.get(key)

        return formatted_data

    def configure(self, request):
        """Configure the table with pagination and other options."""
        paginate = {
            "paginator_class": EnhancedPaginator,
            "per_page": get_table_paginate_count(request, self.prefix),
        }

        tables.RequestConfig(request, paginate).configure(self)


class VCInterfaceTable(LibreNMSInterfaceTable):
    """Table for displaying Virtual Chassis interface data."""

    device_selection = tables.Column(
        verbose_name="Virtual Chassis member",
        accessor="device",
        orderable=False,
        empty_values=[],
        attrs={"td": {"data-col": "device_selection"}},
    )

    def __init__(self, *args, device=None, interface_name_field=None, vlan_groups=None, **kwargs):
        """Initialize VC interface table with device and name field."""
        super().__init__(
            *args, device=device, interface_name_field=interface_name_field, vlan_groups=vlan_groups, **kwargs
        )
        # Ensure device_selection column is visible
        if hasattr(self.device, "virtual_chassis") and self.device.virtual_chassis:
            self.columns.show("device_selection")

    def render_device_selection(self, value, record):
        """
        Render a device selection dropdown for virtual chassis members.

        The method determines the selected member based on interface type and name.
        It returns an HTML select element with appropriate member options.

        Args:
            value (object): The column value.
            record (dict): The interface table row.

        Returns:
            SafeString: The HTML select element with the available member options.

        """
        # Reuse the per-render member prefetch (see _vc_members) so the dropdown doesn't re-query
        # the chassis members for every row (N+1 on a large chassis).
        members = self._vc_members
        interface_name = record.get(self.interface_name_field)
        port_id = record.get("port_id") or ""

        # Default the dropdown to the same owner the relationship sync button resolves (matched
        # NetBox interface's device → cross-page selection → name heuristic), so the JS — which
        # posts this dropdown's value as the sync object id — can't disagree with the button and
        # 404. Previously non-ethernet rows always defaulted to the viewed member, breaking sync
        # for a sub-interface owned by a different VC member.
        selected_member_id = self._resolve_row_member_id(record) or self.device.id

        # Create unique base ID for TomSelect components
        base_id = f"device_selection_{port_id}"
        disabled = mark_safe(' disabled="disabled"') if not record.get("sync_target_resolvable", True) else ""

        return format_html(
            '<select name="device_selection_{0}" id="{1}" class="form-select vc-member-select" '
            'data-interface="{2}" data-row-id="{0}"{4}>{3}</select>',
            port_id,
            base_id,
            interface_name,
            render_vc_member_options(members, selected_member_id),
            disabled,
        )

    def format_interface_data(self, port_data, device):
        """Format interface data including VC device selection column."""
        formatted_data = super().format_interface_data(port_data, device)
        formatted_data["device_selection"] = self.render_device_selection(None, port_data)
        return formatted_data

    class Meta:
        """Meta options for VCInterfaceTable."""

        sequence = [
            "selection",
            "device_selection",
            "name",
            "type",
            "speed",
            "vlans",
            "mac_address",
            "mtu",
            "enabled",
            "description",
            "librenms_id",
            "parent",
            "actions",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table",
        }


class LibreNMSVMInterfaceTable(LibreNMSInterfaceTable):
    """Table for displaying LibreNMS VM interface data."""

    # These rows sync against VirtualMachine objects regardless of whether the VM has a cluster.
    sync_object_type = "virtualmachine"

    class Meta(LibreNMSInterfaceTable.Meta):
        """Meta options for LibreNMSVMInterfaceTable."""

        sequence = [
            "selection",
            "name",
            "vlans",
            "mac_address",
            "mtu",
            "enabled",
            "description",
            "librenms_id",
            # VMInterface supports sub-interface parents (LAG is skipped for VMs), and the
            # relationship sync path resolves VMInterface targets — so the Parent/LAG column
            # must be exposed here too, otherwise the feature is unreachable on VM pages.
            "parent",
            "actions",
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table-vm",
        }

    # Remove the type and speed column for VMs
    type = None
    speed = None
