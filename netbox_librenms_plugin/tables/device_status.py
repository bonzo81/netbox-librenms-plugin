import json
from urllib.parse import quote_plus

import django_tables2 as tables
from dcim.models import Device
from dcim.tables import DeviceTable
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django_tables2 import Column
from netbox.tables.columns import ToggleColumn
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.import_plan import ImportObjectType, VMPlacementMethod, import_row_hx_include
from netbox_librenms_plugin.import_utils.disclosure import scope_validation_disclosures
from netbox_librenms_plugin.import_utils.naming import import_name_variants
from netbox_librenms_plugin.utils import (
    coerce_librenms_id,
    get_librenms_sync_device,
    netbox_allows_standalone_vm_host,
)

_IMPORT_NAMING_INCLUDE = "#use-sysname-toggle, #strip-domain-toggle"


class DeviceStatusTable(DeviceTable):
    """Table for displaying device LibreNMS status."""

    librenms_status = Column(
        verbose_name="LibreNMS Status",
        empty_values=(),
        accessor="librenms_status",
        orderable=False,
    )

    def render_librenms_status(self, value, record):
        """Render LibreNMS sync status with link to sync page."""
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
        """Meta options for DeviceStatusTable."""

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
    Display LibreNMS devices available for import.

    Shows validation status and provides import actions.
    Uses plain django_tables2.Table since we're working with dictionaries, not model instances.
    """

    name = "DeviceImportTable"  # Required by NetBox table utilities

    def __init__(self, *args, user, **kwargs):
        """
        Initialize table with cached querysets, scope each row's matches, and apply sorting.

        ``user`` is keyword-only and required: every row renders the identity of NetBox objects
        found by unrestricted conflict searches, so the table cannot be built without knowing whose
        view scope decides what may be shown.
        """
        # The server the import page was rendered against. Threaded onto the validation-details
        # modal URL so the modal-open HTMX GET fetches from this server rather than whatever
        # LibreNMSSettings.selected_server happens to be when the modal is opened.
        self.server_key = kwargs.pop("server_key", None)
        self.user = user
        super().__init__(*args, **kwargs)
        self.tab = "import"
        self.prefix = "import_"

        # Redact in place, after any caller has finished reading the unrestricted matches (bulk
        # collision detection keys on them) and written them to cache. One batch call: a per-row
        # call would add a permission query per matched row to every table render.
        scope_validation_disclosures(
            [record.get("_validation") for record in self.data if isinstance(record, dict)], user
        )

        # Cache querysets to avoid N queries per render
        from dcim.models import DeviceRole
        from virtualization.models import Cluster

        self._cached_clusters = list(Cluster.objects.restrict(user, "view").order_by("name"))
        self._cached_roles = list(DeviceRole.objects.restrict(user, "view").order_by("name"))

        # Apply sorting if order_by is specified
        # Since we're working with dictionaries, not QuerySets, we handle sorting manually
        if self.order_by:
            self._sort_data()

    def _server_key_hx_vals(self):
        """
        hx-vals attribute carrying the import page's server_key on row-update posts.

        The role/cluster/rack selects hx-post to DeviceRole/Cluster/RackUpdateView, which
        rebind to the POSTed server_key before re-fetching/re-validating the row. Without
        this value the rebind falls back to the global selected server. With overlapping
        device_ids across servers that live-fetches and caches the WRONG server's device.

        Returns:
            str: The escaped hx-vals attribute, or an empty string when no server key is set.

        """
        server_key = getattr(self, "server_key", None)
        if not server_key:
            return ""
        return f"hx-vals='{escape(json.dumps({'server_key': str(server_key)}))}' "

    def _sort_data(self):
        """Sort table data based on order_by parameter."""
        if not self.data:
            return

        # Get the ordering field and direction
        order_by = self.order_by[0] if isinstance(self.order_by, (list, tuple)) else self.order_by
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
            """Return lowercase sort value for a data field."""
            value = item.get(data_key, "")
            return (value or "").lower() if isinstance(value, str) else str(value or "")

        try:
            self.data.data.sort(key=sort_key, reverse=reverse)
        except (AttributeError, TypeError):
            # If data is a plain list, sort it directly
            if isinstance(self.data, list):
                self.data.sort(key=sort_key, reverse=reverse)

    selection = ToggleColumn(
        verbose_name="",
        default="",
        visible=True,
        orderable=False,
        accessor="device_id",
        attrs={
            "th": {"class": "w-1 import-select-column", "aria-label": "Select all"},
            "td": {"class": "w-1 import-select-column"},
            "input": {"name": "select"},
            "td__input": {
                "name": "select",
                "class": lambda record: (
                    "form-check-input device-select"
                    if record.get("_validation", {}).get("can_import", False)
                    else "form-check-input"
                ),
                "disabled": lambda record: (
                    None if record.get("_validation", {}).get("can_import", False) else "disabled"
                ),
                "title": lambda record: (
                    None if record.get("_validation", {}).get("can_import", False) else "Cannot import this device"
                ),
                "aria-label": lambda record: (
                    f"Select {record.get('hostname') or record.get('sysName') or record.get('device_id')}"
                ),
                "data-device-id": lambda record: record.get("device_id"),
                "data-hostname": lambda record: record.get("hostname", ""),
                "data-sysname": lambda record: record.get("sysName", ""),
            },
        },
    )

    netbox_object = Column(
        verbose_name="NetBox object",
        empty_values=(),
        orderable=False,
        accessor="device_id",
        attrs={"th": {"class": "import-name-column"}, "td": {"class": "import-name-column"}},
    )
    location = Column(
        verbose_name="Location",
        accessor="location",
        orderable=True,
        attrs={"th": {"data-import-column": "location"}, "td": {"data-import-column": "location"}},
    )
    hardware = Column(
        verbose_name="Hardware",
        accessor="hardware",
        orderable=True,
        attrs={"th": {"data-import-column": "hardware"}, "td": {"data-import-column": "hardware"}},
    )
    hostname = Column(
        verbose_name="Hostname",
        accessor="hostname",
        orderable=True,
        attrs={"th": {"data-import-column": "hostname"}, "td": {"data-import-column": "hostname"}},
    )
    sysname = Column(
        verbose_name="System Name",
        accessor="sysName",
        orderable=True,
        attrs={"th": {"data-import-column": "sysname"}, "td": {"data-import-column": "sysname"}},
    )
    import_setup = Column(
        verbose_name="Import setup",
        empty_values=(),
        orderable=False,
        accessor="device_id",
        attrs={"th": {"class": "import-setup-column"}, "td": {"class": "import-setup-column"}},
    )

    # Actions column
    actions = Column(
        verbose_name="Actions",
        empty_values=(),
        orderable=False,
        accessor="device_id",
        attrs={"th": {"class": "import-actions-column text-end"}, "td": {"class": "import-actions-column text-end"}},
    )

    def render_hostname(self, value, record):
        """Render hostname with link to LibreNMS if available."""
        return format_html("<strong>{}</strong>", value)

    def render_netbox_object(self, value, record):
        """
        Render the intended NetBox identity and any virtual-chassis summary.

        Args:
            value (object): The column value.
            record (dict): The LibreNMS device and its validation state.

        Returns:
            SafeString: The rendered NetBox object summary.

        """
        validation = record.get("_validation", {})
        existing = validation.get("existing_device")
        is_vm = validation.get("import_as_vm", False) or isinstance(existing, VirtualMachine)
        kind_label = "Virtual machine" if is_vm else "Device"
        kind_class = "bg-purple-lt" if is_vm else "bg-blue-lt"

        if existing:
            url_name = "virtualization:virtualmachine" if is_vm else "dcim:device"
            object_url = reverse(url_name, kwargs={"pk": existing.pk})
            name_html = format_html(
                '<a href="{}"><strong>{}</strong> <i class="mdi mdi-open-in-new"></i></a>',
                object_url,
                existing.name,
            )
            source_html = format_html(
                '<div class="text-secondary small">Existing NetBox {}</div>',
                "virtual machine" if is_vm else "device",
            )
        else:
            resolved_name = (
                validation.get("resolved_name")
                or record.get("sysName")
                or record.get("hostname")
                or f"device-{record.get('device_id')}"
            )
            name_html = format_html(
                '<strong data-import-name data-import-name-variants="{}">{}</strong>',
                json.dumps(import_name_variants(record), separators=(",", ":")),
                resolved_name,
            )
            criteria = validation.get("naming_criteria") or {}
            source = criteria.get("source")
            source_label = {"sysname": "sysName", "hostname": "hostname"}.get(source, "fallback name")
            suffix = ", domain removed" if criteria.get("strip_domain") else ""
            source_html = format_html(
                '<div class="text-secondary small" data-import-name-source>From {}{}</div>',
                source_label,
                suffix,
            )

        vc_data = validation.get("virtual_chassis") or {}
        vc_html = (
            self.render_virtual_chassis(None, record)
            if vc_data.get("is_stack") and vc_data.get("member_count", 0) > 1
            else ""
        )
        return format_html(
            '<div class="d-flex align-items-center gap-1 flex-wrap">{}<span class="badge {}">{}</span>{}</div>{}',
            name_html,
            kind_class,
            kind_label,
            vc_html,
            source_html,
        )

    def render_import_setup(self, value, record):
        """
        Render the required choice inline and attach optional row choices.

        Args:
            value (object): The column value.
            record (dict): The LibreNMS device and its validation state.

        Returns:
            SafeString: The rendered import setup controls.

        """
        validation = record.get("_validation", {})
        existing = validation.get("existing_device")
        if existing:
            return mark_safe(
                '<span class="badge bg-yellow-lt">Review existing match</span>'
                '<div class="small text-secondary">Import setup is not applicable</div>'
            )

        intent = validation.get("_import_intent")
        is_vm = intent.is_vm if intent is not None else validation.get("import_as_vm", False)
        device_id = record.get("device_id")
        if is_vm:
            placement = validation.get("vm_placement", {})
            legacy_cluster = validation.get("cluster", {}).get("cluster")
            placement_method = VMPlacementMethod(
                placement.get("method")
                or (VMPlacementMethod.CLUSTER if legacy_cluster is not None else VMPlacementMethod.SITE)
            )
            placement_found = bool(
                placement.get("found")
                if "found" in placement
                else legacy_cluster is not None or validation.get("site", {}).get("found")
            )
            label_class = "text-secondary" if placement_found else "text-danger"
            primary_label = "Placement" if placement_found else "Placement required"
            if placement_method is VMPlacementMethod.CLUSTER:
                primary_target = f"cluster_{device_id}"
                primary_control = self.render_netbox_cluster(None, record)
            elif placement_method is VMPlacementMethod.HOST:
                primary_target = f"host_device_{device_id}"
                primary_control = self.render_netbox_host(None, record)
            else:
                primary_target = None
                site = validation.get("site", {}).get("site")
                primary_control = format_html(
                    '<span class="badge {} import-placement-badge">{}</span>',
                    "bg-green-lt" if site else "bg-red-lt",
                    f"Matched site: {site.name}" if site else "Matched site unavailable",
                )
            role_control = self.render_netbox_role(None, record)
            placement_control = self.render_vm_placement_method(None, record)
            object_type_control = self.render_object_type(None, record)
            options_count = int(bool(validation.get("device_role", {}).get("role"))) + int(
                placement_method is not VMPlacementMethod.SITE
            )
            menu_html = format_html(
                '<div class="small fw-bold mb-2">VM options</div>'
                '<label class="form-label small" for="role_{}">VM role '
                '<span class="text-secondary">(optional)</span></label>{}'
                '<hr class="my-2"><label class="form-label small" for="vm_placement_{}">Placement method</label>{}'
                '<hr class="my-2"><label class="form-label small" for="object_type_{}">Object type</label>{}',
                device_id,
                role_control,
                device_id,
                placement_control,
                device_id,
                object_type_control,
            )
        else:
            role_found = bool(validation.get("device_role", {}).get("found"))
            label_class = "text-secondary" if role_found else "text-danger"
            primary_label = "Device role" if role_found else "Role required"
            primary_target = f"role_{device_id}"
            primary_control = self.render_netbox_role(None, record)
            rack_control = self.render_netbox_rack(None, record)
            object_type_control = self.render_object_type(None, record)
            options_count = int(bool(validation.get("rack", {}).get("rack")))
            menu_html = format_html(
                '<div class="small fw-bold mb-2">Optional placement</div>'
                '<label class="form-label small" for="rack_{}">Rack</label>{}'
                '<hr class="my-2"><div class="small fw-bold mb-2">Object type</div>'
                '<label class="form-label small" for="object_type_{}">Object type</label>{}',
                device_id,
                rack_control,
                device_id,
                object_type_control,
            )

        badge_html = (
            format_html('<span class="badge bg-primary-lt ms-1">{}</span>', options_count) if options_count else ""
        )
        options_html = format_html(
            '<div class="dropdown flex-shrink-0">'
            '<button class="btn btn-sm btn-outline-secondary dropdown-toggle" type="button" '
            'data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false" '
            'title="More import settings" aria-label="More import settings">'
            '<i class="mdi mdi-tune"></i>{}</button>'
            '<div class="dropdown-menu dropdown-menu-end p-3 import-row-options-menu">{}</div></div>',
            badge_html,
            menu_html,
        )
        if primary_target is None:
            primary_heading = format_html('<div class="import-source-label {}">{}</div>', label_class, primary_label)
        else:
            primary_heading = format_html(
                '<label class="import-source-label {}" for="{}">{}</label>',
                label_class,
                primary_target,
                primary_label,
            )
        return format_html(
            '{}<div class="d-flex gap-1 align-items-center">{}{}</div>',
            primary_heading,
            primary_control,
            options_html,
        )

    def _row_update_url(self, device_id, validation):
        """Return the shared row-update URL with virtual-chassis state."""
        update_url = reverse(
            "plugins:netbox_librenms_plugin:device_import_plan_update",
            kwargs={"device_id": device_id},
        )
        if validation.get("_vc_detection_enabled"):
            return f"{update_url}?enable_vc_detection=true"
        return update_url

    def render_object_type(self, value, record):
        """Render the explicit NetBox object-type selector."""
        device_id = record.get("device_id")
        validation = record.get("_validation", {})
        intent = validation.get("_import_intent")
        selected_type = (
            intent.object_type
            if intent is not None
            else ImportObjectType.VIRTUAL_MACHINE
            if validation.get("import_as_vm")
            else ImportObjectType.DEVICE
        )
        options = []
        for object_type, label in (
            (ImportObjectType.DEVICE, "Device"),
            (ImportObjectType.VIRTUAL_MACHINE, "Virtual machine"),
        ):
            selected = " selected" if object_type is selected_type else ""
            options.append(f'<option value="{object_type.value}"{selected}>{label}</option>')
        return mark_safe(
            f'<select class="form-select form-select-sm import-option-select" '
            f'id="object_type_{device_id}" name="object_type_{device_id}" '
            f'hx-post="{self._row_update_url(device_id, validation)}" hx-trigger="change" hx-swap="none" '
            f'{self._server_key_hx_vals()}hx-include="{import_row_hx_include(device_id)}">'
            f"{''.join(options)}</select>"
        )

    def render_vm_placement_method(self, value, record):
        """Render the virtual-machine placement-method selector."""
        device_id = record.get("device_id")
        validation = record.get("_validation", {})
        method = VMPlacementMethod(
            validation.get("vm_placement", {}).get("method")
            or (
                VMPlacementMethod.CLUSTER
                if validation.get("cluster", {}).get("cluster") is not None
                else VMPlacementMethod.SITE
            )
        )
        site = validation.get("site", {}).get("site")
        host_label = "Host device"
        if not netbox_allows_standalone_vm_host():
            host_label = "Host device (clustered host required)"
        options = []
        for option, label in (
            (VMPlacementMethod.SITE, f"Matched site ({site.name})" if site else "Matched site (unavailable)"),
            (VMPlacementMethod.CLUSTER, "Cluster"),
            (VMPlacementMethod.HOST, host_label),
        ):
            selected = " selected" if option is method else ""
            options.append(f'<option value="{option.value}"{selected}>{escape(label)}</option>')
        return mark_safe(
            f'<select class="form-select form-select-sm import-option-select" '
            f'id="vm_placement_{device_id}" name="vm_placement_{device_id}" '
            f'hx-post="{self._row_update_url(device_id, validation)}" hx-trigger="change" hx-swap="none" '
            f'{self._server_key_hx_vals()}hx-include="{import_row_hx_include(device_id)}">'
            f"{''.join(options)}</select>"
        )

    def render_netbox_cluster(self, value, record):
        """
        Render cluster selection dropdown.

        Cluster selection changes placement only. Object type is a separate control.

        Args:
            value (object): The column value.
            record (dict): The LibreNMS device and its validation state.

        Returns:
            SafeString: The rendered cluster selection HTML.

        """
        device_id = record.get("device_id")
        validation = record.get("_validation", {})
        existing = validation.get("existing_device")

        # Check if existing object is a VM
        if existing and isinstance(existing, VirtualMachine):
            # VM already exists - show its cluster. NetBox has allowed
            # VirtualMachine.cluster to be NULL since 3.3 (VMs can be assigned
            # directly to a site/device), so guard against the cluster-less case.
            cluster = existing.cluster
            if cluster is not None:
                return mark_safe(f'<span class="badge bg-info text-white">{cluster.name}</span>')
            return mark_safe('<span class="text-muted small">VM (no cluster)</span>')

        # If Device already exists (not VM), show it's not a VM
        if existing:
            return mark_safe('<span class="text-muted small">Device (not VM)</span>')

        # Use cached clusters to avoid N queries
        clusters = self._cached_clusters

        # Check if a cluster has been selected (from validation)
        selected_cluster_id = None
        if validation.get("cluster", {}).get("found") and validation.get("cluster", {}).get("cluster"):
            selected_cluster_id = validation["cluster"]["cluster"].pk

        # Build dropdown with HTMX attributes to update the row
        options = ['<option value="">-- Select cluster --</option>']
        for cluster in clusters:
            selected = " selected" if cluster.pk == selected_cluster_id else ""
            options.append(f'<option value="{cluster.pk}"{selected}>{cluster.name}</option>')

        select_html = (
            f'<select class="form-select form-select-sm cluster-select import-setup-select" '
            f'id="cluster_{device_id}" name="cluster_{device_id}" '
            f'data-device-id="{device_id}" '
            f'hx-post="{self._row_update_url(device_id, validation)}" '
            f'hx-trigger="change" '
            f'hx-swap="none" '
            f"{self._server_key_hx_vals()}"
            f'hx-include="{import_row_hx_include(device_id)}">'
            f"{''.join(options)}"
            f"</select>"
        )

        return mark_safe(select_html)

    def render_netbox_host(self, value, record):
        """Render NetBox's remote Device selector for VM host placement."""
        from django import forms
        from utilities.forms.fields import DynamicModelChoiceField

        device_id = record.get("device_id")
        validation = record.get("_validation", {})
        field_name = f"host_device_{device_id}"
        selected_host = validation.get("vm_placement", {}).get("host_device")
        form = forms.Form()
        field = DynamicModelChoiceField(
            queryset=Device.objects.restrict(self.user, "view"),
            required=False,
            selector=True,
            label="Host device",
        )
        field.widget.attrs.update(
            {
                "id": field_name,
                "class": "api-select form-select form-select-sm import-setup-select",
                "hx-post": self._row_update_url(device_id, validation),
                "hx-trigger": "change",
                "hx-swap": "none",
                "hx-include": import_row_hx_include(device_id),
            }
        )
        if self.server_key:
            field.widget.attrs["hx-vals"] = json.dumps({"server_key": str(self.server_key)})
        form.fields[field_name] = field
        if selected_host is not None:
            form.initial[field_name] = selected_host.pk
        bound_field = form[field_name]
        if selected_host is not None:
            field.widget.choices = [
                ("", field.empty_label),
                (field.prepare_value(selected_host), field.label_from_instance(selected_host)),
            ]
        return mark_safe(str(bound_field))

    def render_netbox_role(self, value, record):
        """
        Render role selection dropdown.

        For Devices: Role is required
        For VMs: Role is optional

        Args:
            value (object): The column value.
            record (dict): The LibreNMS device and its validation state.

        Returns:
            SafeString: The rendered role selection HTML.

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
                f'<span class="badge" style="background-color: #{color}; color: white;">{role.name}</span>'
            )

        # Use cached roles to avoid N queries
        roles = self._cached_roles

        # Check if a role has been selected (from validation)
        selected_role_id = None
        if validation.get("device_role", {}).get("found") and validation.get("device_role", {}).get("role"):
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

        select_html = (
            f'<select class="form-select form-select-sm device-role-select import-setup-select" '
            f'id="role_{device_id}" name="role_{device_id}" '
            f'data-device-id="{device_id}" '
            f'hx-post="{self._row_update_url(device_id, validation)}" '
            f'hx-trigger="change" '
            f'hx-swap="none" '
            f"{self._server_key_hx_vals()}"
            f'hx-include="{import_row_hx_include(device_id)}">'
            f"{''.join(options)}"
            f"</select>"
        )

        return mark_safe(select_html)

    def render_netbox_rack(self, value, record):
        """
        Render rack selection dropdown (optional).

        Shows racks for the matched site in "Location - Rack" format.
        Only shown for devices (not VMs) and when site is matched.

        Args:
            value (object): The column value.
            record (dict): The LibreNMS device and its validation state.

        Returns:
            SafeString: The rendered rack selection HTML.

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
            return mark_safe(f'<span class="badge bg-info text-white">{location_name} - {rack.name}</span>')

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
            options.append(f'<option value="{rack.pk}"{selected}>{escape(display_text)}</option>')

        select_html = (
            f'<select class="form-select form-select-sm rack-select import-option-select" '
            f'id="rack_{device_id}" name="rack_{device_id}" '
            f'data-device-id="{device_id}" '
            f'hx-post="{self._row_update_url(device_id, validation)}" '
            f'hx-trigger="change" '
            f'hx-swap="none" '
            f"{self._server_key_hx_vals()}"
            f'hx-include="{import_row_hx_include(device_id)}">'
            f"{''.join(options)}"
            f"</select>"
        )

        return mark_safe(select_html)

    def render_actions(self, value, record):
        """
        Render action buttons for import using HTMX.

        Shows Import button if can import, otherwise shows Preview/Configure.
        Permission checks are handled by backend require_write_permission() which shows toast.

        Args:
            value (object): The column value.
            record (dict): The LibreNMS device and its validation state.

        Returns:
            SafeString: The rendered import action HTML.

        """
        validation = record.get("_validation", {})
        device_id = record.get("device_id")
        is_ready = validation.get("is_ready", False)
        can_import = validation.get("can_import", False)
        existing = validation.get("existing_device")
        intent = validation.get("_import_intent")
        is_vm = intent.is_vm if intent is not None else validation.get("import_as_vm", False)

        vc_attributes = self._build_vc_attributes(validation, record)

        buttons = []

        if existing:
            # Link to existing device/VM in NetBox + details button for conflict resolution
            if isinstance(existing, VirtualMachine):
                url_name = "virtualization:virtualmachine"
                title = "View VM in NetBox"
            else:
                url_name = "dcim:device"
                title = "View Device in NetBox"

            device_url = reverse(url_name, kwargs={"pk": existing.pk})
            buttons.append(
                f'<a href="{device_url}" class="btn btn-sm btn-secondary" '
                f'title="{title}" aria-label="{title}"><i class="mdi mdi-open-in-new"></i></a>'
            )

            # Add details/conflict button for conflict resolution actions
            details_url = self._build_validation_details_url(device_id, validation)
            match_type = validation.get("existing_match_type", "")
            serial_action = validation.get("serial_action")
            has_mismatch = validation.get("device_type_mismatch", False)
            is_oob_candidate = serial_action == "oob_candidate"
            # 'oob_already_linked' is informational (re-import just updates the existing OOB
            # entry), not an actionable conflict — exclude it like 'oob_candidate' so a correctly
            # OOB-linked row doesn't render the warning "Conflict" button.
            is_oob_already_linked = serial_action == "oob_already_linked"
            is_oob_linked = match_type == "librenms_oob"
            has_actions = match_type == "hostname" or (
                match_type == "serial"
                and serial_action is not None
                and not is_oob_candidate
                and not is_oob_already_linked
            )
            has_name_sync = validation.get("name_sync_available", False)
            has_sync_needed = match_type == "librenms_id" and serial_action in ("update_serial", "conflict")

            existing_link = validation.get("existing_librenms_link")
            if not isinstance(existing_link, dict):
                # Malformed payload must not break the actions-column render for the page.
                existing_link = {}
            paired_oob_id = existing_link.get("oob_id")
            paired_host_id = existing_link.get("host_id")
            paired_oob_type = existing_link.get("oob_type") or "OOB"

            def _coerce_pair_id(value):
                # Strict coercion (rejects booleans and floats, unlike int()) so malformed
                # custom-field data can't make the host/OOB pair comparison hide or mislabel a
                # pair. Matches the coercion used by the refresh path / find_by_librenms_id.
                return coerce_librenms_id(value)

            # Coerce both pair ids once. A malformed id becomes None, so the host/OOB pair branches
            # below require a *valid* id before rendering a paired state — otherwise a bogus value
            # (e.g. "bad") would slip past the `!= host` check and print "LibreNMS #bad".
            paired_oob_id_int = _coerce_pair_id(paired_oob_id) if paired_oob_id is not None else None
            paired_host_id_int = _coerce_pair_id(paired_host_id) if paired_host_id is not None else None

            if is_oob_candidate:
                btn_class = "btn-outline-purple"
                btn_icon = "mdi-chip"
                btn_label = " OOB"
                btn_title = "Add as OOB controller"
            elif is_oob_linked:
                # This LibreNMS row is the OOB half of an existing pair.
                btn_class = "btn-outline-info"
                btn_icon = "mdi-chip"
                btn_label = " OOB"
                # Use the same strict coercion as the host-half branch (rejects bool/float) so the
                # OOB-linked and host-half titles share one ID contract — a malformed paired id
                # isn't shown as a bogus "LibreNMS #1".
                if paired_host_id_int is not None:
                    btn_title = f"Linked as OOB controller (paired host: LibreNMS #{paired_host_id_int})"
                else:
                    btn_title = "Linked as OOB controller"
            elif has_mismatch:
                btn_class = "btn-outline-danger"
                btn_icon = "mdi-alert-circle"
                btn_label = " Conflict"
                btn_title = "View conflict details"
            elif has_actions:
                btn_class = "btn-outline-warning"
                btn_icon = "mdi-alert"
                btn_label = " Conflict"
                btn_title = "View conflict details"
            elif (
                match_type == "librenms_id"
                and paired_host_id_int is not None
                and paired_oob_id_int is not None
                and paired_oob_id_int != paired_host_id_int
            ):
                # This LibreNMS row is the host half of an existing host/OOB
                # pair. Render it with the same info-tinted styling as the OOB
                # row so the user sees them as one paired device rather than
                # two unrelated statuses (one green "ready", one blue "OOB").
                # Evaluated BEFORE the generic name-sync/sync-needed branch so a
                # paired host that also needs review still shows the Host state
                # rather than falling back to the generic warning button.
                btn_class = "btn-outline-info"
                btn_icon = "mdi-server-network"
                btn_label = " Host"
                # paired_oob_type comes from a user-editable custom field
                # (librenms_id.<server>.oob.type) and is only string-type-checked,
                # not sanitised, on the read path. Escape before interpolating
                # into the title attribute to prevent stored XSS.
                # paired_oob_id_int is the strictly-coerced value (guaranteed not None by the
                # branch condition), so the title can never print a malformed raw id.
                btn_title = f"Linked as host (paired OOB: LibreNMS #{escape(str(paired_oob_id_int))}, {escape(paired_oob_type or '')})"
            elif has_name_sync or has_sync_needed:
                btn_class = "btn-outline-warning"
                btn_icon = "mdi-information-outline"
                btn_label = " Details"
                btn_title = "View details"
            elif match_type == "librenms_id" and validation.get("librenms_id_needs_migration"):
                btn_class = "btn-outline-warning"
                btn_icon = "mdi-database-alert"
                btn_label = " Legacy ID"
                btn_title = "View legacy ID migration details"
            else:
                btn_class = "btn-outline-success"
                btn_icon = "mdi-check-circle"
                btn_label = ""
                btn_title = "View details"
            aria_attr = f'aria-label="{btn_title}" '
            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm {btn_class}" '
                f"{aria_attr}"
                f'hx-get="{details_url}" '
                f'hx-include="{_IMPORT_NAMING_INCLUDE}" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="{btn_title}">'
                f'<i class="mdi {btn_icon}"></i><span class="device-import-action-label">{btn_label}</span></button>'
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
                f'<i class="mdi mdi-download"></i><span class="device-import-action-label"> Import</span></button>'
            )
            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-outline-primary" '
                f'aria-label="View details" '
                f'hx-get="{details_url}" '
                f'hx-include="{_IMPORT_NAMING_INCLUDE}" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="View details">'
                f'<i class="mdi mdi-information-outline"></i><span class="device-import-action-label visually-hidden"> Details</span></button>'
            )
        elif can_import:
            # Has warnings - show Review button with Details
            details_url = self._build_validation_details_url(device_id, validation)

            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-warning" '
                f'hx-get="{details_url}" '
                f'hx-include="{_IMPORT_NAMING_INCLUDE}" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="Review and import">'
                f'<i class="mdi mdi-alert"></i><span class="device-import-action-label"> Review</span></button>'
            )
        else:
            # Cannot import (usually missing role) - show Import button (disabled until role selected) and Details
            details_url = self._build_validation_details_url(device_id, validation)

            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-success device-import-btn" '
                f'data-device-id="{device_id}" '
                f"disabled{vc_attributes} "
                f'title="{"Select placement to enable import" if is_vm else "Select a role to enable import"}">'
                f'<i class="mdi mdi-download"></i><span class="device-import-action-label"> Import</span></button>'
            )
            buttons.append(
                f'<button type="button" '
                f'class="btn btn-sm btn-outline-danger" '
                f'hx-get="{details_url}" '
                f'hx-include="{_IMPORT_NAMING_INCLUDE}" '
                f'hx-target="#htmx-modal-content" '
                f'hx-swap="innerHTML" '
                f'title="View validation details">'
                f'<i class="mdi mdi-alert-circle"></i><span class="device-import-action-label"> Details</span></button>'
            )

        return mark_safe('<div class="btn-group btn-group-sm">' + " ".join(buttons) + "</div>")

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
        server_key = getattr(self, "server_key", None)
        if server_key:
            vc_url += f"?server_key={quote_plus(str(server_key))}"

        # Show error button if detection failed
        if vc_data.get("detection_error"):
            return format_html(
                '<button type="button" class="badge bg-yellow-lt border-0" '
                'hx-get="{}" hx-target="#htmx-modal-content" hx-swap="innerHTML" '
                'title="View virtual chassis error details">'
                '<i class="mdi mdi-alert"></i> Stack Error</button>',
                vc_url,
            )

        # Show member count button for valid multi-member stacks
        member_count = vc_data.get("member_count", 0)
        return format_html(
            '<button type="button" class="badge bg-cyan-lt border-0" '
            'hx-get="{}" hx-target="#htmx-modal-content" hx-swap="innerHTML" '
            'title="View virtual chassis details">'
            '<i class="mdi mdi-server-network"></i> Stack, {}</button>',
            vc_url,
            member_count,
        )

    def _build_validation_details_url(self, device_id: int, validation: dict) -> str:
        """
        Build a validation-details URL that preserves the complete import intent.

        The modal can open without access to the row's live controls, such as for an
        existing match whose setup controls are hidden. Carry the same field names as
        the import form so the details request cannot lose its model or placement.

        Args:
            device_id: LibreNMS device ID.
            validation: Validation dict from ``validate_device_for_import()``.

        Returns:
            str: Complete URL with query parameters.

        """
        details_url = reverse(
            "plugins:netbox_librenms_plugin:device_validation_details",
            kwargs={"device_id": device_id},
        )

        params = []

        # Scope the modal to the server the import page was rendered for, so the modal-open GET
        # (which reaches DeviceValidationDetailsView via its own URL, with no parent handler to
        # inject the import-scoped client) fetches from that server rather than the global
        # LibreNMSSettings.selected_server (which may have drifted).
        server_key = getattr(self, "server_key", None)
        if server_key:
            params.append(f"server_key={quote_plus(str(server_key))}")

        intent = validation.get("_import_intent")
        existing = validation.get("existing_device")
        is_vm = (
            intent.is_vm
            if intent is not None
            else validation.get("import_as_vm", False) or isinstance(existing, VirtualMachine)
        )
        object_type = ImportObjectType.VIRTUAL_MACHINE if is_vm else ImportObjectType.DEVICE
        params.append(f"object_type_{device_id}={object_type.value}")

        role = validation.get("device_role", {}).get("role")
        role_id = intent.role_id if intent is not None else getattr(role, "pk", None)
        if role_id is not None:
            params.append(f"role_{device_id}={role_id}")

        if is_vm:
            placement = validation.get("vm_placement", {})
            placement_method = intent.vm_placement_method if intent is not None else placement.get("method")
            if placement_method is None:
                if getattr(existing, "device_id", None) or placement.get("host_device") is not None:
                    placement_method = VMPlacementMethod.HOST
                elif getattr(existing, "cluster_id", None) or validation.get("cluster", {}).get("cluster") is not None:
                    placement_method = VMPlacementMethod.CLUSTER
                else:
                    placement_method = VMPlacementMethod.SITE
            placement_method = VMPlacementMethod(placement_method)
            params.append(f"vm_placement_{device_id}={placement_method.value}")

            if placement_method is VMPlacementMethod.CLUSTER:
                cluster = validation.get("cluster", {}).get("cluster")
                cluster_id = intent.cluster_id if intent is not None else getattr(cluster, "pk", None)
                if cluster_id is not None:
                    params.append(f"cluster_{device_id}={cluster_id}")
            elif placement_method is VMPlacementMethod.HOST:
                host_device = placement.get("host_device") or getattr(existing, "device", None)
                host_device_id = intent.host_device_id if intent is not None else getattr(host_device, "pk", None)
                if host_device_id is not None:
                    params.append(f"host_device_{device_id}={host_device_id}")
        else:
            rack = validation.get("rack", {}).get("rack")
            rack_id = intent.rack_id if intent is not None else getattr(rack, "pk", None)
            if rack_id is not None:
                params.append(f"rack_{device_id}={rack_id}")

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
        """Meta options for DeviceImportTable."""

        # No model - we're working with LibreNMS API dictionaries, not Django model instances
        # This prevents NetBoxTable from auto-adding custom fields from Device model

        # Add row attributes to give each row a unique ID for HTMX targeting
        row_attrs = {
            "id": lambda record: f"device-row-{record.get('device_id')}",
            "data-device-id": lambda record: record.get("device_id"),
            "data-hostname": lambda record: record.get("hostname", ""),
            "data-sysname": lambda record: record.get("sysName", ""),
        }

        fields = (
            "selection",
            "netbox_object",
            "location",
            "hardware",
            "hostname",
            "sysname",
            "import_setup",
            "actions",
        )
        sequence = (
            "selection",
            "netbox_object",
            "location",
            "hardware",
            "hostname",
            "sysname",
            "import_setup",
            "actions",
        )
        default_columns = fields
        orderable = True
        attrs = {
            "class": "table table-hover",
            "id": "device-import-table",
        }
