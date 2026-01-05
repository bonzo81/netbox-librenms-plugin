import json

import django_tables2 as tables
from dcim.models import Device
from dcim.tables import DeviceTable
from django.urls import reverse
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django_tables2 import Column
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.utils import get_librenms_sync_device


class DeviceStatusTable(DeviceTable):
    """
    Table for displaying device LibreNMS status.
    """

    librenms_status = Column(
        verbose_name="LibreNMS Status",
        empty_values=(),
        accessor="librenms_status",
        orderable=False,
    )

    def render_librenms_status(self, value, record):
        sync_url = reverse(
            "plugins:netbox_librenms_plugin:device_librenms_sync",
            kwargs={"pk": record.pk},
        )

        # Check if device is VC member and redirect to sync device if different
        if hasattr(record, "virtual_chassis") and record.virtual_chassis:
            sync_device = get_librenms_sync_device(record)
            if sync_device and record.pk != sync_device.pk:
                sync_device_url = reverse(
                    "plugins:netbox_librenms_plugin:device_librenms_sync",
                    kwargs={"pk": sync_device.pk},
                )
                return mark_safe(
                    f'<a href="{sync_device_url}"><span class="text-info">'
                    f'<i class="mdi mdi-server-network"></i> See {sync_device.name}</span></a>'
                )
        if value:
            status = '<span class="text-success"><i class="mdi mdi-check-circle"></i> Found</span>'
        elif value is False:
            status = '<span class="text-danger"><i class="mdi mdi-close-circle"></i> Not Found</span>'
        else:
            status = '<span class="text-secondary"><i class="mdi mdi-help-circle"></i> Unknown</span>'

        return mark_safe(f'<a href="{sync_url}">{status}</a>')

    class Meta(DeviceTable.Meta):
        model = Device
        fields = (
            "pk",
            "name",
            "status",
            "tenant",
            "site",
            "location",
            "rack",
            "role",
            "manufacturer",
            "device_type",
            "device_role",
            "librenms_status",
        )
        default_columns = (
            "name",
            "status",
            "site",
            "location",
            "rack",
            "device_type",
            "role",
            "librenms_status",
        )


class DeviceImportTable(tables.Table):
    """
    Table for displaying LibreNMS devices available for import.
    Shows validation status and provides import actions.
    Uses plain django_tables2.Table since we're working with dictionaries, not model instances.
    """

    name = "DeviceImportTable"  # Required by NetBox table utilities

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Cache querysets to avoid N queries per render
        from dcim.models import DeviceRole
        from virtualization.models import Cluster

        self._cached_clusters = list(Cluster.objects.all().order_by("name"))
        self._cached_roles = list(DeviceRole.objects.all().order_by("name"))

        # Apply sorting if order_by is specified
        # Since we're working with dictionaries, not QuerySets, we handle sorting manually
        if self.order_by:
            self._sort_data()

    def _sort_data(self):
        """Sort table data based on order_by parameter."""
        if not self.data:
            return

        # Get the ordering field and direction
        order_by = (
            self.order_by[0]
            if isinstance(self.order_by, (list, tuple))
            else self.order_by
        )
        reverse = order_by.startswith("-")
        field = order_by.lstrip("-")

        # Map column names to data keys
        field_map = {
            "hostname": "hostname",
            "sysname": "sysName",
            "location": "location",
            "hardware": "hardware",
        }

        data_key = field_map.get(field)
        if not data_key:
            return  # Unknown field, skip sorting

        # Sort the data list in place
        # Handle None values by treating them as empty strings for sorting
        def sort_key(item):
            value = item.get(data_key, "")
            return (value or "").lower() if isinstance(value, str) else str(value or "")

        try:
            self.data.data.sort(key=sort_key, reverse=reverse)
        except (AttributeError, TypeError):
            # If data is a plain list, sort it directly
            if isinstance(self.data, list):
                self.data.sort(key=sort_key, reverse=reverse)

    # Selection checkbox
    selection = Column(
        verbose_name="",
        empty_values=(),
        orderable=False,
        accessor="device_id",
    )

    # LibreNMS device fields
    hostname = Column(verbose_name="Hostname", accessor="hostname", orderable=True)
    sysname = Column(verbose_name="System Name", accessor="sysName", orderable=True)
    location = Column(verbose_name="Location", accessor="location", orderable=True)
    hardware = Column(verbose_name="Hardware", accessor="hardware", orderable=True)

    # Cluster selection - if selected, import as VM; otherwise import as Device
    netbox_cluster = Column(
        verbose_name="NetBox Cluster",
        empty_values=(),
        orderable=False,
        accessor="device_id",
    )

    # NetBox role selection (for devices only)
    netbox_role = Column(
        verbose_name="NetBox Role",
        empty_values=(),
        orderable=False,
        accessor="device_id",
    )

    # NetBox rack selection (for devices only, optional)
    netbox_rack = Column(
        verbose_name="NetBox Rack",
        empty_values=(),
        orderable=False,
        accessor="device_id",
    )

    # Virtual Chassis detection column
    virtual_chassis = Column(
        verbose_name="Virtual Chassis",
        empty_values=(),
        orderable=False,
        accessor="device_id",
    )

    # Actions column
    actions = Column(
        verbose_name="Actions",
        empty_values=(),
        orderable=False,
        accessor="device_id",
    )

    def render_selection(self, value, record):
        """
        Render selection checkbox.
        Disabled if device can't be imported.
        """
        validation = record.get("_validation", {})
        can_import = validation.get("can_import", False)
        device_id = record.get("device_id")
        hostname = record.get("hostname", "")
        sysname = record.get("sysName", "")

        if can_import:
            return mark_safe(
                f'<input type="checkbox" name="select" value="{device_id}" '
                f'class="form-check-input device-select" data-device-id="{device_id}" '
                f'data-hostname="{hostname}" data-sysname="{sysname}">'
            )
        else:
            return mark_safe(
                '<input type="checkbox" disabled '
                'class="form-check-input" title="Cannot import this device">'
            )

    def render_hostname(self, value, record):
        """Render hostname with link to LibreNMS if available."""
        return mark_safe(f"<strong>{value}</strong>")

    def render_netbox_cluster(self, value, record):
        """
        Render cluster selection dropdown.
        Default is "-- Device (not VM) --" (empty value).
        If a cluster is selected, the device will be imported as a VM.
        If no cluster is selected, the device will be imported as a Device.
        """
        device_id = record.get("device_id")
        validation = record.get("_validation", {})
        existing = validation.get("existing_device")

        # Check if existing object is a VM
        if existing and isinstance(existing, VirtualMachine):
            # VM already exists - show its cluster (cluster is required for VMs)
            cluster = existing.cluster
            return mark_safe(
                f'<span class="badge bg-info text-white">{cluster.name}</span>'
            )

        # If Device already exists (not VM), show it's not a VM
        if existing:
            return mark_safe('<span class="text-muted small">Device (not VM)</span>')

        # Use cached clusters to avoid N queries
        clusters = self._cached_clusters

        # Check if a cluster has been selected (from validation)
        selected_cluster_id = None
        if validation.get("cluster", {}).get("found") and validation.get(
            "cluster", {}
        ).get("cluster"):
            selected_cluster_id = validation["cluster"]["cluster"].pk

        # Build dropdown with HTMX attributes to update the row
        options = ['<option value="">-- Device (not VM) --</option>']
        for cluster in clusters:
            selected = " selected" if cluster.pk == selected_cluster_id else ""
            options.append(
                f'<option value="{cluster.pk}"{selected}>{cluster.name}</option>'
            )

        # Add HTMX attributes to update the entire row when cluster is selected
        from django.urls import reverse

        update_url = reverse(
            "plugins:netbox_librenms_plugin:device_cluster_update",
            kwargs={"device_id": device_id},
        )

        # Include VC detection flag in URL if present in validation (from initial load)
        vc_detection_flag = ""
        if validation.get("_vc_detection_enabled"):
            vc_detection_flag = "?enable_vc_detection=true"

        select_html = (
            f'<select class="form-select form-select-sm cluster-select" '
            f'name="cluster_{device_id}" '
            f'data-device-id="{device_id}" '
            f'hx-post="{update_url}{vc_detection_flag}" '
            f'hx-trigger="change" '
            f'hx-swap="none" '
            f'hx-include="[name=role_{device_id}], [name=rack_{device_id}]" '
            f'style="width: 180px;">'
            f"{''.join(options)}"
            f"</select>"
        )

        return mark_safe(select_html)

    def render_netbox_role(self, value, record):
        """
        Render role selection dropdown.
        For Devices: Role is required
        For VMs: Role is optional
        """
        device_id = record.get("device_id")
        validation = record.get("_validation", {})
        is_vm = validation.get("import_as_vm", False)
        existing = validation.get("existing_device")

        # If device/VM already exists, show its role with NetBox's defined color
        if existing and hasattr(existing, "role") and existing.role:
            role = existing.role
            # Use the role's color if available, otherwise fallback to info
            color = role.color if hasattr(role, "color") and role.color else "6c757d"
            return mark_safe(
                f'<span class="badge" style="background-color: #{color}; color: white;">'
                f"{role.name}</span>"
            )

        # Use cached roles to avoid N queries
        roles = self._cached_roles

        # Check if a role has been selected (from validation)
        selected_role_id = None
        if validation.get("device_role", {}).get("found") and validation.get(
            "device_role", {}
        ).get("role"):
            selected_role_id = validation["device_role"]["role"].pk

        # Build dropdown with different text based on import type
        if is_vm:
            placeholder = "-- Select Role (Optional) --"
        else:
            placeholder = "-- Select Role --"

        options = [f'<option value="">{placeholder}</option>']
        for role in roles:
            selected = " selected" if role.pk == selected_role_id else ""
            options.append(f'<option value="{role.pk}"{selected}>{role.name}</option>')

        # Add HTMX attributes to update the entire row when role is selected
        from django.urls import reverse

        update_url = reverse(
            "plugins:netbox_librenms_plugin:device_role_update",
            kwargs={"device_id": device_id},
        )

        # Include VC detection flag in URL if present in validation (from initial load)
        vc_detection_flag = ""
        if validation.get("_vc_detection_enabled"):
            vc_detection_flag = "?enable_vc_detection=true"

        select_html = (
            f'<select class="form-select form-select-sm device-role-select" '
            f'name="role_{device_id}" '
            f'data-device-id="{device_id}" '
            f'hx-post="{update_url}{vc_detection_flag}" '
            f'hx-trigger="change" '
            f'hx-swap="none" '
            f'hx-include="[name=cluster_{device_id}], [name=rack_{device_id}]" '
            f'style="width: 150px;">'
            f"{''.join(options)}"
            f"</select>"
        )

        return mark_safe(select_html)

    def render_netbox_rack(self, value, record):
        """
        Render rack selection dropdown (optional).
        Shows racks for the matched site in "Location - Rack" format.
        Only shown for devices (not VMs) and when site is matched.
        """
        device_id = record.get("device_id")
        validation = record.get("_validation", {})
        is_vm = validation.get("import_as_vm", False)
        existing = validation.get("existing_device")

        # Don't show rack dropdown for VMs
        if is_vm:
            return mark_safe('<span class="text-muted small">N/A (VM)</span>')

        # If device already exists, show its rack
        if existing and hasattr(existing, "rack") and existing.rack:
            rack = existing.rack
            location_name = rack.location.name if rack.location else "No Location"
            return mark_safe(
                f'<span class="badge bg-info text-white">{location_name} - {rack.name}</span>'
            )

        # If device exists but no rack assigned
        if existing:
            return mark_safe('<span class="text-muted small">No rack</span>')

        # Check if site is matched - rack selection only available when site is known
        site_found = validation.get("site", {}).get("found", False)
        if not site_found:
            return mark_safe('<span class="text-muted small">--</span>')

        # Get available racks from validation (cached)
        available_racks = validation.get("rack", {}).get("available_racks", [])

        # Check if a rack has been selected
        selected_rack_id = None
        if validation.get("rack", {}).get("rack"):
            selected_rack_id = validation["rack"]["rack"].pk

        # Build dropdown with HTMX attributes
        options = ['<option value="">--</option>']
        for rack in available_racks:
            location_name = rack.location.name if rack.location else "No Location"
            display_text = f"{location_name} - {rack.name}"
            selected = " selected" if rack.pk == selected_rack_id else ""
            options.append(
                f'<option value="{rack.pk}"{selected}>{escape(display_text)}</option>'
            )

        # Add HTMX attributes to update the entire row when rack is selected
        from django.urls import reverse

        update_url = reverse(
            "plugins:netbox_librenms_plugin:device_rack_update",
            kwargs={"device_id": device_id},
        )

        # Include VC detection flag in URL if present in validation (from initial load)
        vc_detection_flag = ""
        if validation.get("_vc_detection_enabled"):
            vc_detection_flag = "?enable_vc_detection=true"

        select_html = (
            f'<select class="form-select form-select-sm rack-select" '
            f'name="rack_{device_id}" '
            f'data-device-id="{device_id}" '
            f'hx-post="{update_url}{vc_detection_flag}" '
            f'hx-trigger="change" '
            f'hx-swap="none" '
            f'hx-include="[name=cluster_{device_id}], [name=role_{device_id}]" '
            f'style="width: 200px;">'
            f"{''.join(options)}"
            f"</select>"
        )

        return mark_safe(select_html)

    def render_actions(self, value, record):
        """
        Render action buttons for import using HTMX.
        Shows Import button if can import, otherwise shows Preview/Configure.
        """
        validation = record.get("_validation", {})
        device_id = record.get("device_id")
        is_ready = validation.get("is_ready", False)
        can_import = validation.get("can_import", False)
        existing = validation.get("existing_device")

        vc_attributes = self._build_vc_attributes(validation, record)

        buttons = []

        if existing:
            # Link to existing device/VM in NetBox
            if isinstance(existing, VirtualMachine):
                url_name = "virtualization:virtualmachine"
                title = "View VM in NetBox"
            else:
                url_name = "dcim:device"
                title = "View Device in NetBox"

            device_url = reverse(url_name, kwargs={"pk": existing.pk})
            buttons.append(
                f'<a href="{device_url}" class="btn btn-sm btn-secondary" '
                f'title="{title}"><i class="mdi mdi-open-in-new"></i></a>'
            )
        elif is_ready:
            # Ready to import - show Import and Details buttons
            details_url = self._build_validation_details_url(device_id, validation)

            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-success device-import-btn device-ready" '
                f'data-device-id="{device_id}" '
                f'data-import-mode="single"{vc_attributes} '
                f'title="Import this device">'
                f'<i class="mdi mdi-download"></i> Import</button>'
            )
            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-outline-primary" '
                f'hx-get="{details_url}" '
                f'hx-include="[name=cluster_{device_id}], [name=role_{device_id}], [name=rack_{device_id}]" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="View details">'
                f'<i class="mdi mdi-information-outline"></i></button>'
            )
        elif can_import:
            # Has warnings - show Review button with Details
            details_url = self._build_validation_details_url(device_id, validation)

            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-warning" '
                f'hx-get="{details_url}" '
                f'hx-include="[name=cluster_{device_id}], [name=role_{device_id}], [name=rack_{device_id}]" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="Review and import">'
                f'<i class="mdi mdi-alert"></i> Review</button>'
            )
        else:
            # Cannot import (usually missing role) - show Import button (disabled until role selected) and Details
            details_url = self._build_validation_details_url(device_id, validation)

            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-success device-import-btn" '
                f'data-device-id="{device_id}" '
                f"disabled{vc_attributes} "
                f'title="Select a role to enable import">'
                f'<i class="mdi mdi-download"></i> Import</button>'
            )
            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-outline-danger" '
                f'hx-get="{details_url}" '
                f'hx-include="[name=cluster_{device_id}], [name=role_{device_id}], [name=rack_{device_id}]" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="View validation details">'
                f'<i class="mdi mdi-alert-circle"></i> Details</button>'
            )

        return mark_safe(
            '<div class="btn-group btn-group-sm">' + " ".join(buttons) + "</div>"
        )

    def render_virtual_chassis(self, value, record):
        """Render Virtual Chassis status and details button."""
        validation = record.get("_validation", {})
        vc_data = validation.get("virtual_chassis", {})
        device_id = record.get("device_id")

        # Show dash for non-VC or single member stacks
        if not vc_data.get("is_stack") or vc_data.get("member_count", 0) <= 1:
            return mark_safe('<span class="text-muted">—</span>')

        vc_url = reverse(
            "plugins:netbox_librenms_plugin:device_vc_details",
            kwargs={"device_id": device_id},
        )

        # Show error button if detection failed
        if vc_data.get("detection_error"):
            return mark_safe(
                f'<button type="button" '
                f'class="btn btn-sm btn-outline-warning" '
                f'hx-get="{vc_url}" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="View virtual chassis error details">'
                f'<i class="mdi mdi-alert"></i> Error</button>'
            )

        # Show member count button for valid multi-member stacks
        member_count = vc_data.get("member_count", 0)
        return mark_safe(
            f'<button type="button" '
            f'class="btn btn-sm btn-outline-info" '
            f'hx-get="{vc_url}" '
            f'hx-target="#htmx-modal-content" '
            f'hx-swap="innerHTML" '
            f'title="View virtual chassis details">'
            f'<i class="mdi mdi-server-network"></i> {member_count} members</button>'
        )

    @staticmethod
    def _build_validation_details_url(device_id: int, validation: dict) -> str:
        """
        Build validation details URL with appropriate query parameters.

        Constructs the URL for the device validation details modal, adding
        cluster_id, role_id, and VC detection flag as query parameters.

        Args:
            device_id: LibreNMS device ID
            validation: Validation dict from validate_device_for_import()

        Returns:
            str: Complete URL with query parameters
        """
        details_url = reverse(
            "plugins:netbox_librenms_plugin:device_validation_details",
            kwargs={"device_id": device_id},
        )

        # Build query params based on import type
        params = []

        # Add cluster_id if this is a VM import
        if validation.get("cluster", {}).get("found") and validation.get(
            "cluster", {}
        ).get("cluster"):
            cluster_id = validation["cluster"]["cluster"].id
            params.append(f"cluster_id={cluster_id}")
        # Add role_id if device role is found
        elif validation.get("device_role", {}).get("found") and validation.get(
            "device_role", {}
        ).get("role"):
            role_id = validation["device_role"]["role"].id
            params.append(f"role_id={role_id}")

        # Add VC detection flag if it was enabled during initial load
        if validation.get("_vc_detection_enabled"):
            params.append("enable_vc_detection=true")

        if params:
            details_url += "?" + "&".join(params)

        return details_url

    @staticmethod
    def _build_vc_attributes(validation: dict, record: dict) -> str:
        vc_data = validation.get("virtual_chassis") or {}
        if not vc_data.get("is_stack"):
            return ' data-vc-is-stack="false"'

        members_payload = []
        for member in vc_data.get("members", []):
            members_payload.append(
                {
                    "position": member.get("position"),
                    "serial": member.get("serial"),
                    "suggested_name": member.get("suggested_name"),
                }
            )

        payload = {
            "member_count": vc_data.get("member_count", len(members_payload)),
            "members": members_payload,
            "detection_error": vc_data.get("detection_error"),
        }

        payload_json = escape(json.dumps(payload))
        master_name = record.get("hostname") or record.get("sysName") or ""
        master_value = escape(master_name)

        return (
            ' data-vc-is-stack="true"'
            f' data-vc-member-count="{payload["member_count"]}"'
            f' data-vc-info="{payload_json}"'
            f' data-vc-master="{master_value}"'
        )

    class Meta:
        # No model - we're working with LibreNMS API dictionaries, not Django model instances
        # This prevents NetBoxTable from auto-adding custom fields from Device model

        # Add row attributes to give each row a unique ID for HTMX targeting
        row_attrs = {
            "id": lambda record: f"device-row-{record.get('device_id')}",
        }

        fields = (
            "selection",
            "hostname",
            "sysname",
            "location",
            "hardware",
            "netbox_cluster",
            "netbox_role",
            "netbox_rack",
            "virtual_chassis",
            "actions",
        )
        sequence = (
            "selection",
            "hostname",
            "sysname",
            "location",
            "hardware",
            "netbox_cluster",
            "netbox_role",
            "netbox_rack",
            "virtual_chassis",
            "actions",
        )
        default_columns = fields
        orderable = True
        attrs = {
            "class": "table table-hover",
            "id": "device-import-table",
        }
