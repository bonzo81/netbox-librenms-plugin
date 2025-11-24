import django_tables2 as tables
from dcim.models import Device
from dcim.tables import DeviceTable
from django.urls import reverse
from django.utils.safestring import mark_safe
from django_tables2 import Column

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
    device_id = Column(verbose_name="LibreNMS ID", accessor="device_id", orderable=True)
    location = Column(verbose_name="Location", accessor="location", orderable=True)
    hardware = Column(verbose_name="Hardware", accessor="hardware", orderable=True)
    ip = Column(verbose_name="IP Address", accessor="ip", orderable=True)
    os = Column(verbose_name="OS", accessor="os", orderable=True)

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
        from virtualization.models import Cluster

        device_id = record.get("device_id")
        validation = record.get("_validation", {})
        existing = validation.get("existing_device")

        # If VM already exists, show its cluster
        if existing and hasattr(existing, "cluster") and existing.cluster:
            cluster = existing.cluster
            return mark_safe(
                f'<span class="badge bg-info text-white">{cluster.name}</span>'
            )

        # If Device already exists (not VM), show it's not a VM
        if existing:
            return mark_safe('<span class="text-muted small">Device (not VM)</span>')

        # Get all available clusters
        clusters = Cluster.objects.all().order_by("name")

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

        select_html = (
            f'<select class="form-select form-select-sm cluster-select" '
            f'name="cluster_{device_id}" '
            f'data-device-id="{device_id}" '
            f'hx-post="{update_url}" '
            f'hx-trigger="change" '
            f'hx-swap="none" '
            f'hx-include="[name=role_{device_id}]" '
            f'style="min-width: 180px;">'
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
        from dcim.models import DeviceRole

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

        # Get all available roles
        roles = DeviceRole.objects.all().order_by("name")

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

        select_html = (
            f'<select class="form-select form-select-sm device-role-select" '
            f'name="role_{device_id}" '
            f'data-device-id="{device_id}" '
            f'hx-post="{update_url}" '
            f'hx-trigger="change" '
            f'hx-swap="none" '
            f'hx-include="[name=cluster_{device_id}]" '
            f'style="min-width: 150px;">'
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

        buttons = []

        if existing:
            # Link to existing device in NetBox
            device_url = reverse("dcim:device", kwargs={"pk": existing.pk})
            buttons.append(
                f'<a href="{device_url}" class="btn btn-sm btn-secondary" '
                f'title="View in NetBox"><i class="mdi mdi-open-in-new"></i></a>'
            )
        elif is_ready:
            # Ready to import - show Import and Details buttons
            import_url = reverse(
                "plugins:netbox_librenms_plugin:device_import_execute",
                kwargs={"device_id": device_id},
            )
            details_url = reverse(
                "plugins:netbox_librenms_plugin:device_validation_details",
                kwargs={"device_id": device_id},
            )

            # Build query params for details URL based on import type
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

            if params:
                details_url += "?" + "&".join(params)

            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-success device-import-btn device-ready" '
                f'data-device-id="{device_id}" '
                f'data-import-url="{import_url}" '
                f'data-ready="true" '
                f'title="Import this device">'
                f'<i class="mdi mdi-download"></i> Import</button>'
            )
            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-outline-primary" '
                f'hx-get="{details_url}" '
                f'hx-include="[name=cluster_{device_id}], [name=role_{device_id}]" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="View details">'
                f'<i class="mdi mdi-information-outline"></i></button>'
            )
        elif can_import:
            # Has warnings - show Review button with Details
            details_url = reverse(
                "plugins:netbox_librenms_plugin:device_validation_details",
                kwargs={"device_id": device_id},
            )

            # Build query params for details URL based on import type
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

            if params:
                details_url += "?" + "&".join(params)

            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-warning" '
                f'hx-get="{details_url}" '
                f'hx-include="[name=cluster_{device_id}], [name=role_{device_id}]" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="Review and import">'
                f'<i class="mdi mdi-alert"></i> Review</button>'
            )
        else:
            # Cannot import (usually missing role) - show Import button (disabled until role selected) and Details
            import_url = reverse(
                "plugins:netbox_librenms_plugin:device_import_execute",
                kwargs={"device_id": device_id},
            )
            details_url = reverse(
                "plugins:netbox_librenms_plugin:device_validation_details",
                kwargs={"device_id": device_id},
            )

            # Build query params for details URL based on import type
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

            if params:
                details_url += "?" + "&".join(params)

            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-success device-import-btn" '
                f'data-device-id="{device_id}" '
                f'data-import-url="{import_url}" '
                f"disabled "
                f'title="Select a role to enable import">'
                f'<i class="mdi mdi-download"></i> Import</button>'
            )
            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-outline-danger" '
                f'hx-get="{details_url}" '
                f'hx-include="[name=cluster_{device_id}], [name=role_{device_id}]" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="View validation details">'
                f'<i class="mdi mdi-alert-circle"></i> Details</button>'
            )

        return mark_safe(
            '<div class="btn-group btn-group-sm">' + " ".join(buttons) + "</div>"
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
            "device_id",
            "location",
            "hardware",
            "ip",
            "os",
            "netbox_cluster",
            "netbox_role",
            "actions",
        )
        sequence = (
            "selection",
            "hostname",
            "sysname",
            "device_id",
            "location",
            "hardware",
            "ip",
            "os",
            "netbox_cluster",
            "netbox_role",
            "actions",
        )
        default_columns = fields
        orderable = True
        attrs = {
            "class": "table table-hover",
            "id": "device-import-table",
        }
