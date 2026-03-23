import json as json_module

import django_tables2 as tables
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from netbox.tables.columns import BooleanColumn, ToggleColumn
from utilities.paginator import EnhancedPaginator
from utilities.templatetags.helpers import humanize_speed

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
    get_virtual_chassis_member,
)


class LibreNMSInterfaceTable(tables.Table):
    """
    Table for displaying LibreNMS interface data.
    """

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
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table",
        }

    def __init__(self, *args, device=None, interface_name_field=None, vlan_groups=None, server_key="default", **kwargs):
        """Initialize table with device context and interface name field."""
        self.device = device
        self.interface_name_field = interface_name_field or get_interface_name_field()
        self.vlan_groups = vlan_groups or []
        self.server_key = server_key

        # Update column accessors after initialization
        for column in ["selection", "name"]:
            self.base_columns[column].accessor = self.interface_name_field

        # Set row attributes using interface_name_field
        self._meta.row_attrs = {
            "data-interface": lambda record: record.get(self.interface_name_field),
            "data-name": lambda record: record.get(self.interface_name_field),
            "data-enabled": lambda record: (
                str(record.get("ifAdminStatus")).lower() if record.get("ifAdminStatus") is not None else ""
            ),
        }

        super().__init__(*args, **kwargs)
        self.tab = "interfaces"
        self.htmx_url = None
        self.prefix = "interfaces_"

    selection = ToggleColumn(
        orderable=False,
        visible=True,
        attrs={"td": {"data-col": "selection"}, "input": {"name": "select"}},
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
    vlans = tables.Column(
        verbose_name="VLANs",
        empty_values=(),
        orderable=False,
        attrs={"td": {"data-col": "vlans"}},
    )

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
        """
        untagged = record.get("untagged_vlan")
        tagged = record.get("tagged_vlans", [])
        missing_vlans = record.get("missing_vlans", [])

        # Get NetBox interface for comparison
        exists_in_netbox = record.get("exists_in_netbox", False)
        netbox_interface = record.get("netbox_interface")

        # Get NetBox VLAN assignments (VID + group for group-aware comparison)
        netbox_untagged_vid = None
        netbox_untagged_group_id = None
        netbox_tagged_vids = set()
        netbox_tagged_group_ids = {}
        if netbox_interface:
            if netbox_interface.untagged_vlan:
                netbox_untagged_vid = netbox_interface.untagged_vlan.vid
                netbox_untagged_group_id = netbox_interface.untagged_vlan.group_id
            for v in netbox_interface.tagged_vlans.all():
                netbox_tagged_vids.add(v.vid)
                netbox_tagged_group_ids[v.vid] = v.group_id

        all_vlans = []
        if untagged:
            all_vlans.append(("U", untagged))
        for vid in sorted(tagged):
            all_vlans.append(("T", vid))

        if not all_vlans:
            return mark_safe("—")

        interface_name = record.get(self.interface_name_field, "")
        safe_name = interface_name.replace("/", "_").replace(":", "_")

        # Build compact colored summary (show up to 3 VLANs, summarize rest)
        vlan_group_map = record.get("vlan_group_map", {})
        MAX_INLINE = 3
        inline_parts = []
        for vlan_type, vid in all_vlans[:MAX_INLINE]:
            selected_gid = self._parse_group_id(vlan_group_map.get(vid, {}).get("group_id", ""))
            group_matches = check_vlan_group_matches(
                vlan_type,
                vid,
                selected_gid,
                netbox_untagged_group_id,
                netbox_tagged_group_ids,
                netbox_untagged_vid,
                netbox_tagged_vids,
            )
            if vlan_type == "U":
                css = get_untagged_vlan_css_class(
                    vid, netbox_untagged_vid, exists_in_netbox, missing_vlans, group_matches
                )
            else:
                css = get_tagged_vlan_css_class(vid, netbox_tagged_vids, exists_in_netbox, missing_vlans, group_matches)
            warning = get_missing_vlan_warning(vid, missing_vlans)
            inline_parts.append(f'<span class="{css}">{vid}({vlan_type}){warning}</span>')

        summary = ", ".join(inline_parts)
        if len(all_vlans) > MAX_INLINE:
            extra = len(all_vlans) - MAX_INLINE
            summary += f' <span class="text-muted">+{extra} more</span>'

        # Build tooltip showing auto-selected VLAN group per VLAN
        tooltip_lines = []
        for vlan_type, vid in all_vlans:
            if vid in missing_vlans:
                tooltip_lines.append(f"VLAN {vid}({vlan_type}) → ⚠ Not in NetBox")
            else:
                group_info = vlan_group_map.get(vid, {})
                group_name = group_info.get("group_name", "Global")
                tooltip_lines.append(f"VLAN {vid}({vlan_type}) → {escape(group_name)}")
        tooltip_text = "&#10;".join(tooltip_lines)

        # Build hidden inputs for per-VLAN group selections (submitted with form)
        hidden_inputs = []
        for vlan_type, vid in all_vlans:
            group_info = vlan_group_map.get(vid, {})
            group_id = group_info.get("group_id", "")
            hidden_inputs.append(
                format_html(
                    '<input type="hidden" name="vlan_group_{}_{}" '
                    'value="{}" class="vlan-group-hidden" '
                    'data-interface="{}" data-vid="{}">',
                    safe_name,
                    vid,
                    group_id,
                    interface_name,
                    vid,
                )
            )

        # Build JSON data for modal (use proper json serialization for safety)
        vlan_json_items = []
        for vlan_type, vid in all_vlans:
            group_info = vlan_group_map.get(vid, {})
            is_missing = vid in missing_vlans
            selected_gid = self._parse_group_id(group_info.get("group_id", ""))
            group_matches = check_vlan_group_matches(
                vlan_type,
                vid,
                selected_gid,
                netbox_untagged_group_id,
                netbox_tagged_group_ids,
                netbox_untagged_vid,
                netbox_tagged_vids,
            )
            if vlan_type == "U":
                css = get_untagged_vlan_css_class(
                    vid, netbox_untagged_vid, exists_in_netbox, missing_vlans, group_matches
                )
            else:
                css = get_tagged_vlan_css_class(vid, netbox_tagged_vids, exists_in_netbox, missing_vlans, group_matches)
            display_group_name = "Not in NetBox" if is_missing else group_info.get("group_name", "Global")
            vlan_json_items.append(
                {
                    "vid": vid,
                    "type": vlan_type,
                    "group_id": group_info.get("group_id", ""),
                    "group_name": display_group_name,
                    "css": css,
                    "missing": is_missing,
                }
            )
        vlan_json = json_module.dumps(vlan_json_items)

        device_id = self.device.pk if self.device else ""

        # Build vlan_groups JSON for modal dropdowns
        group_options = [{"id": "", "name": "-- No Group (Global) --", "scope": ""}]
        for group in self.vlan_groups:
            scope_info = str(group.scope) if hasattr(group, "scope") and group.scope else ""
            group_options.append({"id": str(group.pk), "name": group.name, "scope": scope_info})

        groups_json = json_module.dumps(group_options)

        # Escape JSON for safe embedding in HTML attributes
        escaped_vlan_json = escape(vlan_json)
        escaped_groups_json = escape(groups_json)

        edit_btn = format_html(
            '<button type="button" class="btn btn-sm btn-link p-0 ms-1 vlan-edit-btn" '
            'data-interface="{}" '
            'data-safe-name="{}" '
            'data-device-id="{}" '
            "data-vlans='{}' "
            "data-vlan-groups='{}' "
            'title="Edit VLAN group assignments">'
            '<i class="mdi mdi-pencil"></i></button>',
            interface_name,
            safe_name,
            device_id,
            escaped_vlan_json,
            escaped_groups_json,
        )

        hidden_inputs_html = mark_safe("".join(str(h) for h in hidden_inputs))

        return format_html(
            '<span title="{}">{}</span>{}{}',
            mark_safe(tooltip_text),
            mark_safe(summary),
            edit_btn,
            hidden_inputs_html,
        )

    @staticmethod
    def _parse_group_id(group_id_str):
        """Normalize a group ID string to int or None for comparison."""
        return int(group_id_str) if group_id_str else None

    def render_speed(self, value, record):
        """Render interface speed with appropriate styling based on comparison with NetBox"""
        kbps_value = convert_speed_to_kbps(value)
        return self._render_field(humanize_speed(kbps_value), record, "ifSpeed", "speed")

    def render_name(self, value, record):
        """Render interface name with appropriate styling based on comparison with NetBox"""
        return self._render_field(value, record, self.interface_name_field, "name")

    def _get_interface_status_display(self, enabled, record):
        """
        Determine interface status display and CSS class based on enabled state and NetBox comparison.

        Args:
            enabled (bool): Interface enabled state.
            record (dict): Interface data record.

        Returns:
            tuple: (display_value, css_class)
        """
        display_value = "Enabled" if enabled else "Disabled"

        if not record.get("exists_in_netbox"):
            return display_value, "text-danger"

        netbox_interface = record.get("netbox_interface")
        if netbox_interface:
            netbox_enabled = netbox_interface.enabled
            if enabled == netbox_enabled:
                return display_value, "text-success"
            return display_value, "text-warning"

        return display_value, "text-danger"

    def _parse_enabled_status(self, value):
        """Convert interface status value to boolean enabled state"""
        if isinstance(value, str):
            return value.lower() == "up"
        return bool(value)

    def render_enabled(self, value, record):
        """Render interface enabled status with appropriate styling based on comparison with NetBox"""
        enabled = self._parse_enabled_status(value)
        display_value, css_class = self._get_interface_status_display(enabled, record)
        return format_html('<span class="{}">{}</span>', css_class, display_value)

    def render_description(self, value, record):
        """Render interface description with appropriate styling based on comparison with NetBox"""
        return self._render_field(value, record, "ifAlias", "description")

    def render_mac_address(self, value, record):
        """Render MAC address with appropriate styling based on comparison with NetBox"""
        formatted_mac = format_mac_address(value)
        return self._render_field(formatted_mac, record, "ifPhysAddress", "mac_address")

    def render_mtu(self, value, record):
        """Render MTU with appropriate styling based on comparison with NetBox"""
        return self._render_field(value, record, "ifMtu", "mtu")

    def render_librenms_id(self, value, record):
        """Render the 'librenms_id' field with appropriate styling based on comparison with NetBox."""

        if not record.get("exists_in_netbox"):
            return mark_safe(f'<span class="text-danger">{value}</span>')

        netbox_interface = record.get("netbox_interface")
        if not netbox_interface:
            return mark_safe(f'<span class="text-danger">{value}</span>')

        netbox_librenms_id = get_librenms_device_id(netbox_interface, self.server_key, auto_save=False)

        if netbox_librenms_id is None:
            return mark_safe(
                f'<span class="text-danger" title="No librenms_id custom field value found">{value}</span>'
            )

        # Compare the IDs
        if str(value) != str(netbox_librenms_id):
            # IDs do not match
            return mark_safe(
                f'<span class="text-warning" title="Existing LibreNMS ID: {netbox_librenms_id}">{value}</span>'
            )
        else:
            # IDs match
            return mark_safe(f'<span class="text-success">{value}</span>')

    def _compare_mac_addresses(self, librenms_mac, netbox_interface):
        """
        Compare LibreNMS MAC address against all MAC addresses on NetBox interface.

        Args:
            librenms_mac (str): MAC address from LibreNMS.
            netbox_interface (Interface): NetBox interface record.

        Returns:
            True if MAC exists on interface.
        """
        if not netbox_interface:
            return False

        interface_macs = [mac.mac_address for mac in netbox_interface.mac_addresses.all()]
        return librenms_mac in interface_macs

    def _render_field(self, value, record, librenms_key, netbox_key):
        """Render a field value with appropriate styling based on the comparison with NetBox."""

        if not record.get("exists_in_netbox"):
            return mark_safe(f'<span class="text-danger">{value}</span>')

        netbox_interface = record.get("netbox_interface")
        if not netbox_interface:
            return mark_safe(f'<span class="text-danger">{value}</span>')

        if librenms_key == "ifPhysAddress":
            mac_matches = self._compare_mac_addresses(value, netbox_interface)
            css_class = "text-success" if mac_matches else "text-warning"
            return mark_safe(f'<span class="{css_class}">{value}</span>')

        netbox_value = getattr(netbox_interface, netbox_key, None)
        librenms_value = record.get(librenms_key)

        if librenms_key == "ifSpeed":
            librenms_value = convert_speed_to_kbps(librenms_value)

        if librenms_value != netbox_value:
            return mark_safe(f'<span class="text-warning">{value}</span>')

        return mark_safe(f'<span class="text-success">{value}</span>')

    def render_type(self, value, record):
        """Render interface type with appropriate styling based on comparison with NetBox"""
        speed = convert_speed_to_kbps(record.get("ifSpeed", 0))
        mapping = self.get_interface_mapping(value, speed)
        tooltip_value, icon = self.render_mapping_tooltip(value, speed, mapping)

        combined_display = format_html("{} {}", tooltip_value, icon)

        if not record.get("exists_in_netbox"):
            return format_html('<span class="text-danger">{}</span>', combined_display)

        netbox_interface = record.get("netbox_interface")

        if netbox_interface:
            netbox_type = getattr(netbox_interface, "type", None)
            if mapping and mapping.netbox_type == netbox_type:
                return format_html('<span class="text-success">{}</span>', combined_display)
            elif mapping:
                return format_html('<span class="text-warning">{}</span>', combined_display)

        return format_html('<span class="text-danger">{}</span>', combined_display)

    def get_interface_mapping(self, librenms_type, speed):
        """Get interface type mapping based on type and speed"""

        # First try exact match with type and speed
        mapping = InterfaceTypeMapping.objects.filter(librenms_type=librenms_type, librenms_speed=speed).first()

        # If no match found, fall back to type-only match
        if not mapping:
            mapping = InterfaceTypeMapping.objects.filter(
                librenms_type=librenms_type, librenms_speed__isnull=True
            ).first()

        return mapping

    def render_mapping_tooltip(self, value, speed, mapping):
        """Render tooltip for interface type mapping"""
        if mapping:
            display = mapping.netbox_type
            icon = format_html(
                '<i class="mdi mdi-link-variant" title="Mapped from LibreNMS type: {} (Speed: {})"></i>',
                value,
                speed,
            )
        else:
            display = value
            icon = mark_safe('<i class="mdi mdi-link-variant-off" title="No mapping to NetBox type"></i>')
        return display, icon

    def format_interface_data(self, port_data, device):
        """Format single interface data using table rendering logic"""

        # Add NetBox interface data
        interface_name = port_data.get(self.interface_name_field)

        port_data["netbox_interface"] = device.interfaces.filter(name=interface_name).first()
        port_data["exists_in_netbox"] = bool(port_data["netbox_interface"])

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
        }

        return formatted_data

    def configure(self, request):
        """Configure the table with pagination and other options"""
        paginate = {
            "paginator_class": EnhancedPaginator,
            "per_page": get_table_paginate_count(request, self.prefix),
        }

        tables.RequestConfig(request, paginate).configure(self)


class VCInterfaceTable(LibreNMSInterfaceTable):
    """
    Table for displaying Virtual Chassis interface data.
    """

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
            # Update selection column accessor to match interface_name_field
            self.base_columns["selection"].accessor = self.interface_name_field

    def render_device_selection(self, value, record):
        """
        Renders a device selection dropdown for virtual chassis members.
        Determines the selected member based on interface type and name.
        Returns an HTML select element with appropriate member options.
        """
        members = self.device.virtual_chassis.members.all()
        if_type = record.get("ifType", "").lower()
        interface_name = record.get(self.interface_name_field)

        if "ethernet" in if_type:
            chassis_member = get_virtual_chassis_member(self.device, interface_name)
            selected_member_id = chassis_member.id if chassis_member else self.device.id
        else:
            selected_member_id = self.device.id

        # Create unique base ID for TomSelect components
        base_id = f"device_selection_{interface_name}_{hash(interface_name)}"

        options = [
            f'<option value="{member.id}"{" selected" if member.id == selected_member_id else ""}>{member.name}</option>'
            for member in members
        ]

        return format_html(
            '<select name="device_selection_{0}" id="{1}" class="form-select vc-member-select" data-interface="{0}" data-row-id="{0}">{2}</select>',
            interface_name,
            base_id,
            mark_safe("".join(options)),
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
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table",
        }


class LibreNMSVMInterfaceTable(LibreNMSInterfaceTable):
    """
    Table for displaying LibreNMS VM interface data.
    """

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
        ]
        attrs = {
            "class": "table table-hover object-list",
            "id": "librenms-interface-table-vm",
        }

    # Remove the type and speed column for VMs
    type = None
    speed = None
