import re
from urllib.parse import urlencode, urlparse

import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html, mark_safe
from netbox.tables.columns import ToggleColumn
from utilities.paginator import EnhancedPaginator

from netbox_librenms_plugin.utils import get_table_paginate_count, oob_badge_html, render_vc_member_options


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
        can_change_interface=False,
        can_delete_module=False,
        can_add_module_bay_template=False,
        can_add_module_type=False,
        can_add_carrier_rule=False,
        can_add_module_bay_mapping=False,
        can_add_module_type_mapping=False,
        **kwargs,
    ):
        """Initialize table with optional device context."""
        self.device = device
        self.csrf_token = ""
        self.server_key = server_key
        self.has_write_permission = has_write_permission
        self.can_add_module = can_add_module
        self.can_change_module = can_change_module
        self.can_change_interface = can_change_interface
        self.can_delete_module = can_delete_module
        self.can_add_module_bay_template = can_add_module_bay_template
        self.can_add_module_type = can_add_module_type
        self.can_add_carrier_rule = can_add_carrier_rule
        self.can_add_module_bay_mapping = can_add_module_bay_mapping
        self.can_add_module_type_mapping = can_add_module_type_mapping
        super().__init__(*args, **kwargs)
        # Batch-load the installed modules (with module_type + interface templates) referenced by
        # the rows, so render_actions' VC "Report VC issue" diagnostic doesn't run a per-row
        # Module.objects.get() + interfacetemplates.all() — an N+1 over the whole module table.
        # Only VC devices reach that branch, so skip the query otherwise.
        self._installed_modules_by_id = {}
        if isinstance(getattr(self.device, "virtual_chassis_id", None), int):
            data_rows = args[0] if args else kwargs.get("data") or []
            installed_ids = {
                row["installed_module_id"]
                for row in data_rows
                if isinstance(row, dict) and row.get("installed_module_id")
            }
            if installed_ids:
                from dcim.models import Module
                from django.db import DatabaseError

                try:
                    self._installed_modules_by_id = {
                        m.pk: m
                        for m in Module.objects.select_related(
                            "module_type",
                            "module_type__manufacturer",
                            "module_bay",
                            "device",
                            "device__device_type",
                            "device__virtual_chassis",
                        )
                        .prefetch_related("module_type__interfacetemplates")
                        .filter(pk__in=installed_ids)
                    }
                except (DatabaseError, RuntimeError):
                    # RuntimeError: pytest "Database access not allowed" in unit-test contexts
                    # whose self.device is a MagicMock with a real-looking virtual_chassis_id.
                    self._installed_modules_by_id = {}
        if not (has_write_permission and can_add_module) and hasattr(self, "columns"):
            self.columns["selection"].column.visible = False
        self.tab = "modules"
        self.htmx_url = None
        self.prefix = "modules_"

    def configure(self, request):
        """Configure pagination settings and CSRF token."""
        from django.middleware.csrf import get_token
        from django.utils.http import url_has_allowed_host_and_scheme

        self.csrf_token = get_token(request)
        # Use HX-Current-URL (the real browser URL) when available so that
        # after saving a mapping the redirect lands on the browsable tab page
        # (which handles GET) rather than the HTMX-only POST endpoint.
        if request:
            # HX-Current-URL is the real browser URL (full absolute URL).
            # Extract only the path+query so return_url stays relative, which
            # is what NetBox's ObjectEditView expects. Validate via
            # url_has_allowed_host_and_scheme to prevent open-redirect attacks
            # from client-controlled values like "//@example.com/...". Fall
            # back to the HTMX endpoint path when the header is absent or the
            # value fails validation.
            htmx_current = request.headers.get("HX-Current-URL", "")
            safe_relative = ""
            if htmx_current and url_has_allowed_host_and_scheme(
                htmx_current,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                parsed = urlparse(htmx_current)
                safe_relative = parsed.path
                if parsed.query:
                    safe_relative = f"{safe_relative}?{parsed.query}"
            self.return_url = safe_relative or request.get_full_path()
        else:
            self.return_url = ""
        paginate = {"paginator_class": EnhancedPaginator, "per_page": get_table_paginate_count(request, self.prefix)}
        tables.RequestConfig(request, paginate).configure(self)

    def render_name(self, value, record):
        """Render inventory item name with tree indentation for sub-components."""
        display_name = value or "-"
        matched_interface_url = record.get("matched_interface_url")
        matched_interface_name = record.get("matched_interface_name") or display_name
        matched_interface_source = record.get("matched_interface_source")
        matched_interface_confidence = record.get("matched_interface_confidence")
        source_label = str(matched_interface_source or "").replace("_", " ").strip()
        confidence_label = str(matched_interface_confidence or "").replace("_", " ").strip()
        title_parts = []
        if source_label:
            title_parts.append(f"Matched by {source_label}")
        if confidence_label:
            title_parts.append(f"confidence {confidence_label}")
        title = ", ".join(title_parts)
        if matched_interface_url:
            rendered_name = format_html(
                '<a href="{}" title="{}">{}</a>',
                matched_interface_url,
                title,
                matched_interface_name,
            )
        else:
            rendered_name = display_name

        depth = record.get("depth", 0)
        oob_badge = oob_badge_html(record)
        if depth == 0:
            return format_html("{}{}", rendered_name, oob_badge)
        # Build visual tree prefix based on nesting depth
        padding_px = depth * 20
        prefix = "└─ "
        # Keep the OOB badge inside the padded container so it stays indented
        # with the module name on nested rows (was rendering at column 0).
        return format_html(
            '<span style="padding-left:{}px"><span style="white-space: nowrap;">{}{}</span>{}</span>',
            padding_px,
            prefix,
            rendered_name,
            oob_badge,
        )

    def render_model(self, value, record):
        """Render model with link to module type if matched."""
        if not value or value == "-":
            return "-"
        if url := record.get("module_type_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return format_html("{}", value)

    def render_serial(self, value, record):
        """Render serial number."""
        return format_html("{}", value or "-")

    def render_description(self, value, record):
        """Render description, truncated for display."""
        if not value:
            return "-"
        if len(value) > 60:
            return format_html('<span title="{}">{}&hellip;</span>', value, value[:57])
        return format_html("{}", value)

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
        if record.get("status") == "Integrated":
            # Informational rows for components fused into a parent module have
            # no bay of their own; show a plain placeholder, not a red warning,
            # to match the muted status badge and absent actions on these rows.
            rendered_value = "-"
        elif not value or value == "-":
            rendered_value = format_html('<span class="text-danger">{}</span>', "No matching bay")
        elif url := record.get("module_bay_url"):
            rendered_value = format_html('<a href="{}">{}</a>', url, value)
        else:
            rendered_value = value

        # Mirror Name-column hierarchy marker in Module Bay for child rows.
        depth = record.get("depth", 0)
        if depth > 0:
            padding_px = depth * 20
            return format_html('<span style="padding-left:{}px">└─ {}</span>', padding_px, rendered_value)

        return rendered_value

    def render_module_type(self, value, record):
        """Render module type match status."""
        if not value or value == "-":
            return format_html('<span class="text-warning">{}</span>', "No matching type")
        if url := record.get("module_type_url"):
            return format_html('<a href="{}">{}</a>', url, value)
        return format_html("{}", value)

    def render_status(self, value, record):
        """
        Render the sync-status badge alongside a hidden in-flight spinner badge.

        The live badge is wrapped so CSS (see ``_module_sync.html``) can swap it
        for the spinner badge while a row-action POST is in flight. The row forms
        set ``hx-indicator="closest tr"``, so HTMX marks the row with ``htmx-request``
        for the duration. The spinner label tracks the row's action: "Updating…" on an
        installed-module row offering Update Serial / Update Interface, "Installing…"
        on an install / install-branch / carrier-install row. It stays hidden in every
        other state, including the inline verify-endpoint cell updates.

        Args:
            value (str): The sync status to render.
            record (dict): The table row with the action and status details.

        Returns:
            SafeString: The live status badge and hidden in-flight spinner badge.
        """
        # An update action (Update Serial / Update Interface) acts on an already-installed module
        # and a row never offers it alongside an install-flavoured action, so a row with an update
        # flag and no install/branch/carrier action shows "Updating…"; everything else "Installing…".
        in_flight_label = (
            "Updating"
            if (
                record.get("installed_module_id")
                and (record.get("can_update_serial") or record.get("can_update_interface_binding"))
                and not record.get("can_install")
                and not record.get("has_installable_children")
                and not record.get("carrier_install_options")
            )
            else "Installing"
        )
        badge = self._status_badge_html(value, record)
        return format_html(
            '<span class="lnms-status-live">{}</span>'
            '<span class="lnms-installing badge bg-primary text-white" role="status" title="{}…">'
            '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>{}…</span>',
            badge,
            in_flight_label,
            in_flight_label,
        )

    def _status_badge_html(self, value, record):
        """Render sync status with badge."""
        # Promote No Bay → Missing Carrier when concrete carrier-install rules
        # produced suggestions for this row (one-click install offered below).
        carrier_options = record.get("carrier_install_options")
        if value == "No Bay" and carrier_options:
            value = "Missing Carrier"

        badge_classes = {
            "Installed": "bg-success text-white",
            "Matched": "bg-info text-white",
            "No Bay": "bg-warning text-dark",
            "No Type": "bg-warning text-dark",
            "Missing Carrier": "bg-warning text-dark",
            "Unmatched": "bg-secondary text-white",
            "Serial Mismatch": "bg-danger text-white",
            "Name Conflict": "bg-warning text-dark",
            "Type Mismatch": "bg-warning text-dark",
            "Integrated": "bg-light text-muted border",
        }
        badge_class = badge_classes.get(value, "bg-secondary text-white")
        warning = record.get("model_warning")

        # More descriptive labels for root-cause specific no-bay cases.
        no_bay_reason = record.get("no_bay_reason")
        if no_bay_reason == "empty_parent_bays":
            display_text = "No Bay on Parent"
        elif no_bay_reason == "interface_child":
            display_text = "Missing Child Bay"
        else:
            display_text = value

        # Small "Possible Carrier?" hint badge: holder hint fired but no
        # concrete CarrierAutoInstallRule matched. Encourages the user to add
        # a rule (button rendered in the actions column). Built upfront so
        # every return path below can append it.
        if record.get("holder_hint_present") and not record.get("carrier_install_options"):
            possible_carrier_html = mark_safe(
                ' <span class="badge bg-info text-white"'
                ' title="Possible missing carrier/holder module — see actions to add a rule">'
                '<i class="mdi mdi-puzzle-outline"></i> Possible Carrier?</span>'
            )
        else:
            possible_carrier_html = mark_safe("")

        if value == "Integrated":
            parent_name = record.get("integrated_in_name") or "parent module"
            tooltip = (
                f"Duplicate SNMP entry for the same physical card as '{parent_name}' "
                f"(matching entPhysicalSerialNum + entPhysicalModelName). "
                "No separate NetBox bay/type is needed — this row is informational."
            )
            return format_html(
                '<span class="badge {}" title="{}">Integrated in {}</span>',
                badge_class,
                tooltip,
                parent_name,
            )

        if value == "Name Conflict" and (conflict_reason := record.get("name_conflict_reason")):
            status_html = format_html(
                '<span class="badge {}">{}</span> <i class="mdi mdi-alert-outline text-warning" title="{}"></i>',
                badge_class,
                display_text,
                conflict_reason,
            )
        elif warning:
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
            target_pk = record.get("model_incomplete_target_pk")
            suggestion = record.get("model_incomplete_suggestion") or {}
            title = f"Module type '{name}' has no bay templates — click to add them so sub-components can be installed"
            fix_html = self._render_fix_bay_template_badge(
                title=title,
                target_kind="module_type",
                target_pk=target_pk,
                target_label=name,
                suggestion=suggestion,
                fallback_url=url,
                label="Fix Model",
            )
            return status_html + fix_html + possible_carrier_html

        # "Fix Device Type" badge when the device type is missing bay templates for this component.
        if record.get("device_type_incomplete"):
            url = record.get("device_type_incomplete_url", "")
            name = record.get("device_type_incomplete_name", "device type")
            target_pk = record.get("device_type_incomplete_target_pk")
            suggestion = record.get("device_type_incomplete_suggestion") or {}
            title = f"Device type '{name}' is missing bay templates for this component — click to add them"
            fix_html = self._render_fix_bay_template_badge(
                title=title,
                target_kind="device_type",
                target_pk=target_pk,
                target_label=name,
                suggestion=suggestion,
                fallback_url=url,
                label="Fix Device Type",
            )
            return status_html + fix_html + possible_carrier_html

        return status_html + possible_carrier_html

    def _render_fix_bay_template_badge(
        self, *, title, target_kind, target_pk, target_label, suggestion, fallback_url, label
    ):
        """
        Render a "Fix Model" / "Fix Device Type" badge.

        When this table has a bound device, a numeric ``target_pk`` and the
        viewer has ``dcim.add_modulebaytemplate``, the badge becomes an HTMX
        trigger that opens the Add Bay Template modal pre-filled with the
        LibreNMS-derived suggestion.

        When the viewer cannot add bay templates, the badge is hidden so it
        does not act as a dead-end control (the modal would only return a 403
        for them). The ``<a href>`` and ``<span>`` fallbacks below are kept
        for callers that do not have a bound device (e.g. unit tests built via
        ``object.__new__``) or have no ``target_pk`` / URL available.

        Args:
            title (str): The badge tooltip text.
            target_kind (str): The target model kind sent to the modal.
            target_pk (int | None): The target object's primary key, or ``None`` when it is unavailable.
            target_label (str): The target label. This function does not use it.
            suggestion (dict): The LibreNMS-derived bay template fields.
            fallback_url (str): The URL for the linked fallback, or an empty string when it is unavailable.
            label (str): The text shown on the badge.

        Returns:
            SafeString: The HTMX button, empty safe string, linked fallback, or non-interactive fallback markup.
        """
        device = getattr(self, "device", None)
        can_add_template = getattr(self, "can_add_module_bay_template", False)
        if device and target_pk and can_add_template:
            modal_url = reverse(
                "plugins:netbox_librenms_plugin:add_bay_template",
                kwargs={"pk": device.pk},
            )
            params = urlencode(
                {
                    "target_kind": target_kind,
                    "target_pk": target_pk,
                    "suggested_name": suggestion.get("name", ""),
                    "suggested_position": suggestion.get("position", ""),
                    "suggested_label": suggestion.get("label", ""),
                    "librenms_name": suggestion.get("librenms_name", ""),
                    "librenms_class": suggestion.get("librenms_class", ""),
                    "server_key": getattr(self, "server_key", "") or "",
                }
            )
            return format_html(
                ' <button type="button" class="badge bg-warning text-dark border-0"'
                ' title="{}"'
                ' hx-get="{}?{}"'
                ' hx-target="#htmx-modal-content"'
                ' hx-swap="innerHTML">'
                '<i class="mdi mdi-wrench-outline"></i> {}</button>',
                title,
                modal_url,
                params,
                label,
            )
        if device and not can_add_template:
            # Viewer lacks dcim.add_modulebaytemplate — don't render a clickable
            # badge that would only surface a permission error. mark_safe("")
            # preserves the SafeString-ness of the surrounding concatenation
            # (`status_html + fix_html + possible_carrier_html`); a bare ""
            # would downgrade the result to a plain str and trigger escaping.
            return mark_safe("")
        if fallback_url:
            return format_html(
                ' <a href="{}" class="badge bg-warning text-dark" title="{}">'
                '<i class="mdi mdi-wrench-outline"></i> {}</a>',
                fallback_url,
                title,
                label,
            )
        return format_html(
            ' <span class="badge bg-warning text-dark" title="{}"><i class="mdi mdi-wrench-outline"></i> {}</span>',
            title,
            label,
        )

    def render_actions(self, value, record):
        """Render install button for matched modules and install branch for parents."""
        if not self.device:
            return ""
        if not self.has_write_permission:
            return ""
        # "Integrated" rows are duplicate SNMP entries for a single physical
        # card (parent + integrated child sharing serial+model) — there's
        # nothing to install, so no actions.
        if record.get("status") == "Integrated":
            return ""

        buttons = []

        # Single install button (requires add permission)
        if self.can_add_module and record.get("can_install"):
            url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": self.device.pk})
            buttons.append(
                format_html(
                    # hx-post: the view answers with the module tab fragment, swapped into
                    # #module-sync-content; method/action keep it working without JS.
                    '<form method="post" action="{}" hx-post="{}"'
                    ' hx-target="#module-sync-content" hx-swap="innerHTML"'
                    ' hx-indicator="closest tr" hx-disabled-elt="find button" style="display:inline">'
                    '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
                    '<input type="hidden" name="server_key" value="{}">'
                    '<input type="hidden" name="selected_device_id" value="{}">'
                    '<input type="hidden" name="ent_index" value="{}">'
                    '<input type="hidden" name="librenms_port_id" value="{}">'
                    '<input type="hidden" name="librenms_ifname" value="{}">'
                    '<input type="hidden" name="librenms_ifdescr" value="{}">'
                    '<input type="hidden" name="inventory_name" value="{}">'
                    '<input type="hidden" name="inventory_descr" value="{}">'
                    '<input type="hidden" name="module_bay_id" value="{}">'
                    '<input type="hidden" name="module_type_id" value="{}">'
                    '<input type="hidden" name="serial" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-success" title="Install module in bay">'
                    '<i class="mdi mdi-download"></i> Install'
                    "</button></form>",
                    url,
                    url,
                    self.csrf_token,
                    self.server_key,
                    record.get("selected_device_id") or self.device.pk,
                    record.get("ent_physical_index", ""),
                    record.get("librenms_port_id", ""),
                    record.get("librenms_ifname") or "",
                    record.get("librenms_ifdescr") or "",
                    record.get("name") or "",
                    record.get("description") or "",
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
                    # hx-post: the view answers with the module tab fragment, swapped into
                    # #module-sync-content; method/action keep it working without JS.
                    '<form method="post" action="{}" hx-post="{}"'
                    ' hx-target="#module-sync-content" hx-swap="innerHTML"'
                    ' hx-indicator="closest tr" hx-disabled-elt="find button" style="display:inline">'
                    '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
                    '<input type="hidden" name="server_key" value="{}">'
                    '<input type="hidden" name="selected_device_id" value="{}">'
                    '<input type="hidden" name="parent_index" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-primary ms-1"'
                    ' title="Install this module and all installable children">'
                    '<i class="mdi mdi-file-tree"></i> Install Branch'
                    "</button></form>",
                    url,
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
                    # hx-post: the view answers with the module tab fragment, swapped into
                    # #module-sync-content; method/action keep it working without JS.
                    '<form method="post" action="{}" hx-post="{}"'
                    ' hx-target="#module-sync-content" hx-swap="innerHTML"'
                    ' hx-indicator="closest tr" hx-disabled-elt="find button" style="display:inline">'
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
                    url,
                    self.csrf_token,
                    self.server_key,
                    record.get("selected_device_id") or self.device.pk,
                    record["installed_module_id"],
                    record.get("serial") or "",
                )
            )

        if (
            getattr(self, "can_change_interface", False)
            and record.get("can_update_interface_binding")
            and record.get("installed_module_id")
        ):
            url = reverse("plugins:netbox_librenms_plugin:update_module_interface", kwargs={"pk": self.device.pk})
            buttons.append(
                format_html(
                    # hx-post: the view answers with the module tab fragment, swapped into
                    # #module-sync-content; method/action keep it working without JS.
                    '<form method="post" action="{}" hx-post="{}"'
                    ' hx-target="#module-sync-content" hx-swap="innerHTML"'
                    ' hx-indicator="closest tr" hx-disabled-elt="find button" style="display:inline">'
                    '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
                    '<input type="hidden" name="server_key" value="{}">'
                    '<input type="hidden" name="selected_device_id" value="{}">'
                    '<input type="hidden" name="module_id" value="{}">'
                    '<input type="hidden" name="ent_index" value="{}">'
                    '<input type="hidden" name="librenms_port_id" value="{}">'
                    '<input type="hidden" name="librenms_ifname" value="{}">'
                    '<input type="hidden" name="librenms_ifdescr" value="{}">'
                    '<input type="hidden" name="inventory_name" value="{}">'
                    '<input type="hidden" name="inventory_descr" value="{}">'
                    '<button type="submit" class="btn btn-sm btn-outline-warning ms-1"'
                    ' title="Associate matching NetBox interface with installed module">'
                    '<i class="mdi mdi-link-variant"></i> Update Interface'
                    "</button></form>",
                    url,
                    url,
                    self.csrf_token,
                    self.server_key,
                    record.get("selected_device_id") or self.device.pk,
                    record["installed_module_id"],
                    record.get("ent_physical_index", ""),
                    record.get("librenms_port_id", ""),
                    record.get("librenms_ifname") or "",
                    record.get("librenms_ifdescr") or "",
                    record.get("name") or "",
                    record.get("description") or "",
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

        # "Install Carrier" buttons for No Bay rows where one or more
        # CarrierAutoInstallRule rows match. One button per (rule, empty bay)
        # candidate. Suggest-only — the user clicks to install.
        if self.can_add_module and record.get("carrier_install_options"):
            install_url = reverse("plugins:netbox_librenms_plugin:install_module", kwargs={"pk": self.device.pk})
            for opt in record["carrier_install_options"]:
                buttons.append(
                    format_html(
                        # hx-post: install_module answers with the module tab fragment, swapped
                        # into #module-sync-content; method/action keep it working without JS.
                        '<form method="post" action="{}" hx-post="{}"'
                        ' hx-target="#module-sync-content" hx-swap="innerHTML"'
                        ' hx-indicator="closest tr" hx-disabled-elt="find button" style="display:inline">'
                        '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
                        '<input type="hidden" name="server_key" value="{}">'
                        '<input type="hidden" name="selected_device_id" value="{}">'
                        '<input type="hidden" name="module_bay_id" value="{}">'
                        '<input type="hidden" name="module_type_id" value="{}">'
                        '<input type="hidden" name="serial" value="">'
                        '<button type="submit" class="btn btn-sm btn-success ms-1"'
                        " title=\"Install carrier {} into empty bay '{}'\">"
                        '<i class="mdi mdi-puzzle-plus-outline"></i> Install {} into &#39;{}&#39;'
                        "</button></form>",
                        install_url,
                        install_url,
                        self.csrf_token,
                        self.server_key,
                        record.get("selected_device_id") or self.device.pk,
                        opt["bay_id"],
                        opt["module_type_id"],
                        opt["module_type_name"],
                        opt["bay_name"],
                        opt["module_type_name"],
                        opt["bay_name"],
                    )
                )

        # "Add Carrier Rule" prefilled link when the holder hint fires but no
        # concrete CarrierAutoInstallRule matched. Pre-fills the form with
        # manufacturer / class / regex for the orphan child name and a regex
        # alternation across the device's empty bay names so the user only
        # needs to pick the carrier ModuleType.
        if (
            record.get("status") == "No Bay"
            and record.get("holder_hint_present")
            and not record.get("carrier_install_options")
            and getattr(self, "can_add_carrier_rule", False)
        ):
            base_url = reverse("plugins:netbox_librenms_plugin:carrierautoinstallrule_add")
            return_url = getattr(self, "return_url", "") or ""
            params = {}
            mfr = getattr(getattr(self.device, "device_type", None), "manufacturer", None)
            if mfr is not None:
                params["manufacturer"] = mfr.pk
            phys_class = (record.get("item_class") or "").strip()
            if phys_class:
                params["librenms_child_class"] = phys_class
            child_name = (record.get("name") or "").strip()
            if child_name:
                params["librenms_child_name_pattern"] = "^" + re.escape(child_name) + "$"
            empty_names = sorted(record.get("device_empty_bay_names") or [])
            if empty_names:
                params["netbox_bay_name_pattern"] = "^(" + "|".join(re.escape(n) for n in empty_names) + ")$"
            if return_url:
                params["return_url"] = return_url
            qs = urlencode(params)
            buttons.append(
                format_html(
                    '<a href="{}?{}" class="btn btn-sm btn-outline-info ms-1"'
                    ' title="Open the Carrier Auto-Install Rule create form pre-filled'
                    " with this device's manufacturer, the orphan child class/name"
                    " and the device's empty bay names so you only need to pick the"
                    ' carrier ModuleType">'
                    '<i class="mdi mdi-puzzle-plus-outline"></i> Add Carrier Rule'
                    "</a>",
                    base_url,
                    qs,
                )
            )

        # "Add mapping" button for No Bay rows where we can suggest a mapping.
        # Opens the ModuleBayMapping create form pre-filled with the regex
        # capturing the trailing-number pattern (e.g. ^0/(\d+)$ -> Slot \1),
        # so one entry covers the whole device-type slot family rather than
        # one mapping per slot.
        if (
            record.get("status") == "No Bay"
            and record.get("model_suggestion")
            and getattr(self, "can_add_module_bay_mapping", False)
        ):
            sug = record["model_suggestion"]
            base_url = reverse("plugins:netbox_librenms_plugin:modulebaymapping_add")
            return_url = getattr(self, "return_url", "") or ""
            params = {
                "librenms_name": sug["librenms_name"],
                "librenms_class": sug.get("librenms_class") or "",
                "netbox_bay_name": sug["netbox_bay_name"],
                "is_regex": "true" if sug.get("is_regex") else "false",
                "description": sug.get("description") or "",
            }
            # Pre-fill manufacturer FK so the new mapping is auto-scoped to the
            # device's vendor; user can clear it in the form to make it global.
            if sug.get("manufacturer"):
                params["manufacturer"] = sug["manufacturer"]
            if return_url:
                params["return_url"] = return_url
            qs = urlencode(params)
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

        # "Add mapping" button for No Type rows where we can suggest a mapping.
        # Opens the ModuleTypeMapping create form pre-filled with the LibreNMS
        # model name and a helpful description so the user only needs to pick
        # or create the matching NetBox ModuleType.
        if (
            record.get("status") == "No Type"
            and record.get("type_suggestion")
            and getattr(self, "can_add_module_type_mapping", False)
        ):
            sug = record["type_suggestion"]
            base_url = reverse("plugins:netbox_librenms_plugin:moduletypemapping_add")
            return_url = getattr(self, "return_url", "") or ""
            params = {
                "librenms_model": sug["librenms_model"],
                "description": sug.get("description") or "",
            }
            if sug.get("manufacturer"):
                params["manufacturer"] = sug["manufacturer"]
            if return_url:
                params["return_url"] = return_url
            qs = urlencode(params)
            buttons.append(
                format_html(
                    '<a href="{}?{}" class="btn btn-sm btn-outline-primary ms-1"'
                    ' title="Open the ModuleTypeMapping create form pre-filled with the LibreNMS'
                    " model name; select or create the matching NetBox ModuleType to complete the mapping"
                    '">'
                    '<i class="mdi mdi-plus-box-outline"></i> Add Mapping'
                    "</a>",
                    base_url,
                    qs,
                )
            )

        # "Add Module Type" button for No Type rows — opens NetBox's native
        # ModuleType create form pre-filled with details we know from LibreNMS
        # (manufacturer, model, part number, description). This is the
        # alternative to "Add Mapping": rather than aliasing the LibreNMS
        # model string to an existing NetBox type, the user creates the
        # missing ModuleType directly so subsequent matches work natively.
        if (
            record.get("status") == "No Type"
            and record.get("module_type_create")
            and getattr(self, "can_add_module_type", False)
        ):
            create = record["module_type_create"]
            base_url = reverse("dcim:moduletype_add")
            return_url = getattr(self, "return_url", "") or ""
            params = {k: v for k, v in create.items() if v not in ("", None)}
            if return_url:
                params["return_url"] = return_url
            qs = urlencode(params)
            buttons.append(
                format_html(
                    '<a href="{}?{}" class="btn btn-sm btn-outline-success ms-1"'
                    ' title="Open the NetBox ModuleType create form pre-filled with the LibreNMS'
                    " model, part number, manufacturer and description so the missing type can be"
                    ' created in one step">'
                    '<i class="mdi mdi-plus-circle-outline"></i> Add Module Type'
                    "</a>",
                    base_url,
                    qs,
                )
            )

        # Report-VC-normalization-issue button: appears only on installed-module rows when
        # the page device is a VC member AND template-name VC rewriting would no-op for
        # the installed module's interface templates (typically: unsupported vendor naming).
        # `virtual_chassis_id` is an int in production (or None); MagicMock-only tests
        # see a MagicMock here, which the isinstance check correctly skips.
        if record.get("installed_module_id") and isinstance(getattr(self.device, "virtual_chassis_id", None), int):
            from netbox_librenms_plugin.utils import detect_vc_normalization_noop

            # Served from the __init__ batch prefetch (with interface templates), so this
            # diagnostic adds no per-row query. A missing id (deleted concurrently, or a
            # unit-test MagicMock device that skipped the prefetch) yields None → no button.
            installed_module = self._installed_modules_by_id.get(record["installed_module_id"])

            if installed_module is not None and detect_vc_normalization_noop(installed_module.device, installed_module):
                report_url = reverse(
                    "plugins:netbox_librenms_plugin:vc_normalization_report",
                    kwargs={"pk": self.device.pk},
                )
                buttons.append(
                    format_html(
                        '<button type="button" class="btn btn-sm btn-outline-secondary ms-1 vc-report-btn"'
                        ' data-report-url="{}" data-module-id="{}" data-selected-device-id="{}"'
                        ' title="Report VC naming-convention issue — opens a copyable diagnostic'
                        " for a GitHub issue"
                        '">'
                        '<i class="mdi mdi-bug-outline"></i> Report VC issue'
                        "</button>",
                        report_url,
                        record["installed_module_id"],
                        record.get("selected_device_id") or self.device.pk,
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
        # Cache VC members once so render_device_selection doesn't re-query for
        # every row in large module tables.
        self._vc_members = []
        if hasattr(self.device, "virtual_chassis") and self.device.virtual_chassis:
            self._vc_members = list(self.device.virtual_chassis.members.all())
            self.columns.show("device_selection")

    def render_device_selection(self, value, record):
        selected_device_id = record.get("selected_device_id") or self.device.id
        ent_index = record.get("ent_physical_index", "")

        return format_html(
            '<select name="device_selection_{0}" id="device_selection_{0}" '
            'class="form-select vc-member-select" data-module="{0}" data-row-id="{0}">{1}</select>',
            ent_index,
            render_vc_member_options(self._vc_members, selected_device_id),
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
