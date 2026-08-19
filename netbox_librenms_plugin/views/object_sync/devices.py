import copy

from dcim.models import Device
from django.core.cache import cache
from django.http import JsonResponse
from django.urls import reverse
from django.views import View
from ipam.models import VLAN, VLANGroup
from utilities.views import ViewTab, register_model_view

from netbox_librenms_plugin.constants import PERM_VIEW_PLUGIN, is_supported_interface_name_field
from netbox_librenms_plugin.interface_relationships import (
    build_candidate_relationship_context,
    build_relationship_maps,
    resolve_relationship_row,
)
from netbox_librenms_plugin.tables.cables import (
    LibreNMSCableTable,
    VCCableTable,
)
from netbox_librenms_plugin.tables.interfaces import (
    LibreNMSInterfaceTable,
    VCInterfaceTable,
)
from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable, VCModuleTable
from netbox_librenms_plugin.utils import (
    build_migrated_context,
    cache_remaining_ttl,
    coerce_model_pk,
    get_interface_name_field,
    get_librenms_sync_device,
    get_missing_vlan_warning,
    get_tagged_vlan_css_class,
    get_untagged_vlan_css_class,
    get_interface_port_identity_sets,
    get_vlan_sync_css_class,
    is_valid_ports_payload,
    normalize_librenms_port_id,
)

from ..base.cables_view import BaseCableTableView
from ..base.interfaces_view import BaseInterfaceTableView
from ..base.ip_addresses_view import BaseIPAddressTableView
from ..base.librenms_sync_view import BaseLibreNMSSyncView
from ..base.modules_view import BaseModuleTableView, _check_ignore_rules
from ..base.vlan_table_view import BaseVLANTableView
from ..mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
    extract_cached_ports,
    parse_request_json,
)


@register_model_view(Device, name="librenms_sync", path="librenms-sync")
class DeviceLibreNMSSyncView(BaseLibreNMSSyncView):
    """Device detail tab showing LibreNMS sync information."""

    queryset = Device.objects.all()
    model = Device
    tab = ViewTab(label="LibreNMS Sync", permission=PERM_VIEW_PLUGIN)

    def get_interface_context(self, request, obj):
        """Return interface sync context for the device."""
        interface_name_field = get_interface_name_field(request, obj)
        interface_table_view = DeviceInterfaceTableView()
        interface_table_view.request = copy.copy(request)
        return interface_table_view.get_context_data(request, obj, interface_name_field)

    def get_cable_context(self, request, obj):
        """Return cable sync context for the device."""
        cable_table_view = DeviceCableTableView()
        cable_table_view.request = copy.copy(request)
        return cable_table_view.get_context_data(request, obj)

    def get_ip_context(self, request, obj):
        """Return IP address sync context for the device."""
        ipaddress_table_view = DeviceIPAddressTableView()
        ipaddress_table_view.request = copy.copy(request)
        return ipaddress_table_view.get_context_data(request, obj)

    def get_vlan_context(self, request, obj):
        vlan_table_view = DeviceVLANTableView()
        vlan_table_view.request = copy.copy(request)
        return vlan_table_view.get_vlan_context(request, obj)

    def get_module_context(self, request, obj):
        """Return module sync context for the device."""
        module_table_view = DeviceModuleTableView()
        module_table_view.request = copy.copy(request)
        return module_table_view.get_context_data(request, obj)


class DeviceInterfaceTableView(BaseInterfaceTableView):
    """Interface synchronization table for Devices."""

    model = Device

    def get_interfaces(self, obj):
        """Return all interfaces for the device."""
        return obj.interfaces.all()

    def get_redirect_url(self, obj):
        """Return the device interface sync redirect URL."""
        return reverse("plugins:netbox_librenms_plugin:device_interface_sync", kwargs={"pk": obj.pk})

    def get_table(self, data, obj, interface_name_field, vlan_groups=None):
        """Return the appropriate interface table, selecting VC variant if needed."""
        server_key = self.librenms_api.server_key
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            table = VCInterfaceTable(
                data,
                device=obj,
                interface_name_field=interface_name_field,
                vlan_groups=vlan_groups,
                server_key=server_key,
            )
        else:
            table = LibreNMSInterfaceTable(
                data,
                device=obj,
                interface_name_field=interface_name_field,
                vlan_groups=vlan_groups,
                server_key=server_key,
            )
        table.htmx_url = f"{self.request.path}?tab=interfaces" + (f"&server_key={server_key}" if server_key else "")
        return table


class SingleInterfaceVerifyView(
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    LibreNMSAPIMixin,
    VlanAssignmentMixin,
    CacheMixin,
    View,
):
    """Verify single interface data for a device via cached LibreNMS payload."""

    # Read-only verify endpoint: require object-view permission (mirrors SingleModuleVerifyView).
    required_object_permissions = {"POST": [("view", Device)]}

    def post(self, request):
        """Verify interface data against cached LibreNMS ports for a device."""
        # Bind the request so require_object_permissions_json() (which reads self.request)
        # works even when post() is invoked directly rather than through dispatch().
        self.request = request

        # Gate before resolving the device: a read-only verify endpoint, this only needs
        # dcim.view_device (model-level, like SingleModuleVerifyView). Checking first means a
        # user without it can't probe device IDs by observing 404-vs-200 from get_object_or_404.
        if error := self.require_object_permissions_json("POST"):
            return error

        data, err = parse_request_json(request)
        if err:
            return err
        selected_device_id = coerce_model_pk(data.get("device_id"))
        posted_name_field = data.get("interface_name_field")

        if selected_device_id is None:
            return JsonResponse({"status": "error", "message": "No device ID provided"}, status=400)
        # Only honour a CONFIGURED string server_key (mirrors the cable/IP verify siblings):
        # the raw value scopes cache reads and the per-server cf lookups below, so a forged
        # key must not address another server's namespace — and a non-string value (e.g. a
        # JSON list) is unhashable, so cf_dict.get(server_key) would TypeError-500 this
        # endpoint. Anything unrecognized falls back to the degrading active-server resolve.
        server_key = self.resolve_requested_server_key(data)

        # Restrict the lookup to the caller's viewable devices: the gate above only checked the
        # model-level view_device perm, so a site-scoped grant would otherwise read another
        # device's cached verify payload by raw pk.
        selected_device = self.restrict_object_or_404(Device, pk=selected_device_id)
        interface_name_field = (
            posted_name_field
            if is_supported_interface_name_field(posted_name_field)
            else get_interface_name_field(request, selected_device)
        )
        origin_device = selected_device
        raw_origin_device_id = data.get("origin_device_id")
        if raw_origin_device_id is not None:
            origin_device_id = coerce_model_pk(raw_origin_device_id)
            if origin_device_id is None:
                return JsonResponse({"status": "error", "message": "A valid origin device ID is required."}, status=400)
            origin_device = self.restrict_object_or_404(Device, pk=origin_device_id)
            same_device = origin_device.pk == selected_device.pk
            same_chassis = (
                origin_device.virtual_chassis_id is not None
                and origin_device.virtual_chassis_id == selected_device.virtual_chassis_id
            )
            if not same_device and not same_chassis:
                return JsonResponse(
                    {"status": "error", "message": "The interface page and selected device do not match."},
                    status=400,
                )

        # Normalise to the VC sync device so cache keys match what the sync view stored
        if selected_device.virtual_chassis:
            primary_device = get_librenms_sync_device(selected_device, server_key=server_key)
            if primary_device is None:
                return JsonResponse(
                    {"status": "error", "message": "No sync device found for virtual chassis"}, status=404
                )
        else:
            primary_device = selected_device

        ports_cache_key = self.get_cache_key(primary_device, "ports", server_key)
        # Shape-guard the cached entry: a truthy but malformed snapshot must degrade to the
        # "Interface data not found" 404 below, not AttributeError-500 on .get("ports").
        cached_data = extract_cached_ports(cache.get(ports_cache_key), ports_cache_key)

        # Validate the shape before reading it: a truthy but malformed cache value (non-dict, or a
        # dict without a list "ports") would otherwise raise on .get(...). Treat it as a cache miss
        # and fall through to the controlled "not found" response, matching the base table view.
        if is_valid_ports_payload(cached_data):
            ports = cached_data.get("ports", [])
            unique_host_port_ids, unambiguous_name_port_ids = get_interface_port_identity_sets(
                ports, interface_name_field
            )
            # Prefer the stable port_id the client posts (data-port-id on the row): display
            # names can collide (an OOB controller can reuse a host interface name), so a
            # name-only match can recompute and patch the wrong cached row. Exclude OOB rows
            # from both paths so a host row is never shadowed by a same-named controller port.
            posted_port_id = normalize_librenms_port_id(data.get("port_id"))
            if posted_port_id is None:
                return JsonResponse(
                    {"status": "error", "message": "A valid LibreNMS port ID is required."},
                    status=404,
                )
            if posted_port_id not in unique_host_port_ids:
                return JsonResponse(
                    {"status": "error", "message": "Interface data is ambiguous. Refresh and retry."},
                    status=404,
                )
            # A supplied stable port_id is authoritative: match only by it. If it misses, do not
            # fall back to a display name that another host or OOB row can reuse.
            port_data = next(
                (
                    p
                    for p in ports
                    if normalize_librenms_port_id(p.get("port_id")) == posted_port_id and p.get("_source") != "oob"
                ),
                None,
            )

            if port_data:
                vlan_groups = self.get_vlan_groups_for_device(selected_device)
                vlan_lookup_maps = self._build_vlan_lookup_maps(vlan_groups)
                vlan_group_overrides = cache.get(self.get_vlan_overrides_key(primary_device, server_key)) or {}
                # Set before the selection call: it validates overrides against this row's groups.
                port_data["vlan_groups"] = vlan_groups
                self._add_vlan_group_selection(
                    port_data,
                    vlan_lookup_maps,
                    selected_device,
                    vlan_group_overrides,
                )
                self._add_missing_vlans_info(port_data, vlan_lookup_maps)
                # The caller keeps its existing VC member selector. The verify response only
                # repaints comparison and relationship cells, so it does not need another
                # member dropdown in the JSON payload.
                table = LibreNMSInterfaceTable(
                    [],
                    device=selected_device,
                    interface_name_field=interface_name_field,
                    vlan_groups=vlan_groups,
                    server_key=server_key,
                )
                # Mirror the main table render: a migrated donor's verify response must not
                # re-introduce the per-row LAG/parent sync button (which posts directly).
                table.migrated_to_marker = bool(
                    build_migrated_context(origin_device, server_key).get("migrated_to_marker")
                )
                raw_port_id = port_data.get("port_id")
                port_id = normalize_librenms_port_id(raw_port_id)
                if port_id is not None:
                    port_data["port_id"] = port_id
                name_fallback_allowed = port_id in unambiguous_name_port_ids
                relationship_maps = build_relationship_maps(cached_data)
                candidate_port_ids = [raw_port_id]
                candidate_names = [port_data.get(interface_name_field)] if name_fallback_allowed else []
                for related_port_id in (
                    relationship_maps.lag_members.get(port_id),
                    relationship_maps.sub_interfaces.get(port_id),
                ):
                    related_port = relationship_maps.ports_by_id.get(related_port_id)
                    if related_port is None:
                        continue
                    candidate_port_ids.append(related_port.get("port_id", related_port_id))
                    if related_port_id in unambiguous_name_port_ids:
                        candidate_names.append(related_port.get(interface_name_field))
                relationship_context = build_candidate_relationship_context(
                    selected_device,
                    server_key,
                    request.user,
                    self.has_write_permission(),
                    candidate_port_ids,
                    candidate_names,
                )
                resolve_relationship_row(
                    relationship_context,
                    port_data,
                    selected_device,
                    interface_name_field,
                    unique_host_port_ids,
                    unambiguous_name_port_ids,
                    relationship_maps,
                )
                formatted_row = table.format_interface_data(port_data, selected_device)
                return JsonResponse({"status": "success", "formatted_row": formatted_row})

        return JsonResponse({"status": "error", "message": "Interface data not found"}, status=404)


class SingleModuleVerifyView(
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    LibreNMSAPIMixin,
    CacheMixin,
    View,
):
    """Verify module row data against cached LibreNMS inventory for a selected VC member."""

    # JSON endpoint that returns the rendered module row — only viewers of the
    # underlying Device should be able to surface its inventory data.
    required_object_permissions = {"POST": [("view", Device)]}

    def post(self, request):
        data, err = parse_request_json(request)
        if err:
            return err
        # Gate BEFORE resolving the device: without this an unauthorized caller could probe
        # arbitrary device IDs (existence via 404) through this endpoint (mirrors
        # SingleInterfaceVerifyView).
        if error := self.require_object_permissions_json("POST"):
            return error
        selected_device_id = data.get("device_id")
        ent_physical_index = data.get("ent_physical_index")
        # Configured-string-key-or-fallback, mirroring SingleInterfaceVerifyView above: a
        # forged/non-string key must neither probe another namespace nor TypeError-500.
        server_key = self.resolve_requested_server_key(data)
        row_depth = data.get("depth", 0)

        if not selected_device_id:
            return JsonResponse({"status": "error", "message": "No device ID provided"}, status=400)
        if ent_physical_index in (None, ""):
            return JsonResponse({"status": "error", "message": "No entPhysicalIndex provided"}, status=400)

        try:
            ent_physical_index = int(ent_physical_index)
        except (TypeError, ValueError):
            return JsonResponse({"status": "error", "message": "Invalid entPhysicalIndex"}, status=400)

        try:
            row_depth = int(row_depth)
        except (TypeError, ValueError):
            row_depth = 0

        # Restrict the lookup to the caller's viewable devices: the gate above only checked the
        # model-level view_device perm, so a site-scoped grant would otherwise read another
        # device's cached verify payload by raw pk.
        selected_device = self.restrict_object_or_404(Device, pk=selected_device_id)

        if selected_device.virtual_chassis:
            sync_device = get_librenms_sync_device(selected_device, server_key=server_key)
            if sync_device is None:
                return JsonResponse({"status": "error", "message": "No sync device found for VC"}, status=404)
        else:
            sync_device = selected_device

        cached_payload = cache.get(self.get_cache_key(sync_device, "inventory", server_key))
        if not isinstance(cached_payload, dict):
            return JsonResponse({"status": "error", "message": "No cached inventory data"}, status=404)

        inventory_data = cached_payload.get("inventory") or []
        index_map = {idx: item for item in inventory_data if (idx := item.get("entPhysicalIndex")) is not None}
        item = index_map.get(ent_physical_index)
        if not item:
            return JsonResponse({"status": "error", "message": "Inventory row not found"}, status=404)

        module_table_view = DeviceModuleTableView()
        # Shallow-copy the request so the child view can mutate request.GET /
        # request.POST without affecting this request object.
        module_table_view.request = copy.copy(request)
        # Thread the POST-resolved server_key through the row builder. _build_member_contexts
        # falls back to librenms_api.server_key (the default server) when _active_server_key is
        # unset, so without this the verify row's interface-binding / can_update_interface_binding
        # would be recomputed against the wrong server and disagree with the main modules tab.
        module_table_view._active_server_key = server_key

        from netbox_librenms_plugin.utils import (
            get_enabled_ignore_rules,
            load_bay_mappings,
            preload_normalization_rules,
        )

        module_table_view._exact_bay_mappings, module_table_view._regex_bay_mappings = load_bay_mappings()
        manufacturer = getattr(getattr(selected_device, "device_type", None), "manufacturer", None)
        module_table_view._norm_rules_bay = preload_normalization_rules("module_bay")
        module_table_view._norm_rules_type = preload_normalization_rules("module_type", manufacturer=manufacturer)

        children_by_parent = {}
        for inventory_item in inventory_data:
            parent_idx = inventory_item.get("entPhysicalContainedIn")
            if parent_idx is not None:
                children_by_parent.setdefault(parent_idx, []).append(inventory_item)

        ignore_rules = get_enabled_ignore_rules()
        device_serial = (getattr(selected_device, "serial", None) or "").strip()
        ignore_cache = {
            inventory_item["entPhysicalIndex"]: _check_ignore_rules(
                inventory_item,
                index_map.get(inventory_item.get("entPhysicalContainedIn")),
                ignore_rules,
                index_map,
                device_serial,
            )
            for inventory_item in inventory_data
            if inventory_item.get("entPhysicalIndex") is not None
        }

        module_types = module_table_view._get_module_types()
        transparent_indices = module_table_view._find_transparent_indices(inventory_data, ignore_cache)
        top_items = module_table_view._collect_top_items(
            inventory_data,
            index_map,
            ignore_rules,
            device_serial,
            transparent_indices,
            ignore_cache,
        )
        table_data = module_table_view._build_table_rows_for_member(
            selected_device,
            top_items,
            index_map,
            children_by_parent,
            ignore_rules,
            device_serial,
            module_types,
            manufacturer=manufacturer,
        )
        module_table_view._detect_serial_conflicts(table_data)

        # entPhysicalIndex should be unique, depth fallback handles malformed duplicates.
        row = next(
            (
                candidate
                for candidate in table_data
                if candidate.get("ent_physical_index") == ent_physical_index and candidate.get("depth", 0) == row_depth
            ),
            None,
        )
        if row is None:
            row = next(
                (candidate for candidate in table_data if candidate.get("ent_physical_index") == ent_physical_index),
                None,
            )
        if row is None:
            return JsonResponse({"status": "error", "message": "Inventory row not found"}, status=404)

        has_write_permission = self.has_write_permission()
        table_class = VCModuleTable if selected_device.virtual_chassis else LibreNMSModuleTable
        table = table_class(
            [],
            device=selected_device,
            server_key=server_key,
            has_write_permission=has_write_permission,
            can_add_module=has_write_permission and request.user.has_perm("dcim.add_module"),
            can_change_module=has_write_permission and request.user.has_perm("dcim.change_module"),
            can_change_interface=has_write_permission and request.user.has_perm("dcim.change_interface"),
            can_delete_module=has_write_permission and request.user.has_perm("dcim.delete_module"),
            can_add_module_bay_template=(has_write_permission and request.user.has_perm("dcim.add_modulebaytemplate")),
            can_add_module_type=(has_write_permission and request.user.has_perm("dcim.add_moduletype")),
            can_add_carrier_rule=(
                has_write_permission and request.user.has_perm("netbox_librenms_plugin.add_carrierautoinstallrule")
            ),
            can_add_module_bay_mapping=(
                has_write_permission and request.user.has_perm("netbox_librenms_plugin.add_modulebaymapping")
            ),
            can_add_module_type_mapping=(
                has_write_permission and request.user.has_perm("netbox_librenms_plugin.add_moduletypemapping")
            ),
        )
        table.configure(request)
        formatted_row = table.format_module_data(row)
        return JsonResponse({"status": "success", "formatted_row": formatted_row})


class SingleVlanGroupVerifyView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """
    Verify VLAN assignments for an interface against a specific VLAN group.

    When user changes the VLAN group dropdown, this endpoint re-computes
    which VLANs are "missing" (don't exist in selected group) and returns
    updated HTML for the VLANs cell with correct colors.
    """

    # Read-only verify endpoint that surfaces a device's interface VLAN assignments —
    # require object-view permission on the underlying Device (mirrors the other verify views).
    required_object_permissions = {"POST": [("view", Device), ("view", VLANGroup), ("view", VLAN)]}

    def post(self, request):
        data, err = parse_request_json(request)
        if err:
            return err
        # Gate BEFORE resolving the device/VLAN group so an unauthorized caller can't probe
        # device IDs (existence via 404) through this endpoint.
        if error := self.require_object_permissions_json("POST"):
            return error
        device_id = data.get("device_id")
        interface_name = data.get("interface_name")
        vlan_group_id = data.get("vlan_group_id")
        vlan_type = data.get("vlan_type", "U")  # "U" or "T"
        vid_str = data.get("vid", "") or data.get("untagged_vlan", "")

        if not device_id:
            return JsonResponse({"status": "error", "message": "No device ID provided"}, status=400)
        if not vid_str:
            return JsonResponse({"status": "error", "message": "No VID provided"}, status=400)

        # Object-scope the lookup (see SingleInterfaceVerifyView): the gate only checked model-level
        # view_device, so an out-of-scope pk must 404 rather than expose the device.
        device = self.restrict_object_or_404(Device, pk=device_id)
        try:
            vid = int(vid_str)
        except (ValueError, TypeError):
            return JsonResponse({"status": "error", "message": "Invalid VID"}, status=400)

        try:
            selected_gid = int(vlan_group_id) if vlan_group_id else None
        except (ValueError, TypeError):
            return JsonResponse({"status": "error", "message": "Invalid VLAN group ID"}, status=400)

        # Build lookup for the selected group
        visible_vlans = self.restricted_queryset(VLAN)
        if selected_gid:
            vlan_group = self.restrict_object_or_404(VLANGroup, pk=selected_gid)
            # Get VLANs in selected group + global VLANs
            group_vids = set(visible_vlans.filter(group=vlan_group).values_list("vid", flat=True))
            global_vids = set(visible_vlans.filter(group__isnull=True).values_list("vid", flat=True))
            available_vids = group_vids | global_vids
        else:
            # No group selected - use global VLANs only
            available_vids = set(visible_vlans.filter(group__isnull=True).values_list("vid", flat=True))

        # Compute whether VID is missing from selected group
        is_missing = vid not in available_vids
        missing_vlans = [vid] if is_missing else []

        # Get NetBox interface for comparison
        netbox_interface = device.interfaces.filter(name=interface_name).first()
        exists_in_netbox = bool(netbox_interface)

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

        # Determine group match: selected group vs NetBox VLAN's actual group
        # Determine CSS class based on actual VLAN type
        if vlan_type == "U":
            # Group matches only matters when VIDs match
            group_matches = (netbox_untagged_group_id == selected_gid) if netbox_untagged_vid == vid else True
            css_class = get_untagged_vlan_css_class(
                vid, netbox_untagged_vid, exists_in_netbox, missing_vlans, group_matches
            )
        else:
            netbox_gid = netbox_tagged_group_ids.get(vid)
            group_matches = (netbox_gid == selected_gid) if vid in netbox_tagged_vids else True
            css_class = get_tagged_vlan_css_class(
                vid, netbox_tagged_vids, exists_in_netbox, missing_vlans, group_matches
            )

        # Also render formatted HTML for backward compatibility
        formatted_vlans = self._render_vlans_cell(
            vid if vlan_type == "U" else None,
            [vid] if vlan_type == "T" else [],
            missing_vlans,
            exists_in_netbox,
            netbox_untagged_vid,
            netbox_tagged_vids,
        )

        return JsonResponse(
            {
                "status": "success",
                "formatted_vlans": formatted_vlans,
                "css_class": css_class,
                "is_missing": is_missing,
            }
        )

    def _render_vlans_cell(
        self, untagged, tagged, missing_vlans, exists_in_netbox, netbox_untagged_vid, netbox_tagged_vids
    ):
        """
        Render the VLANs cell HTML with correct color coding.

        Reuses the same color logic as LibreNMSInterfaceTable.render_vlans().
        """
        from django.utils.safestring import mark_safe

        parts = []

        if untagged:
            css = get_untagged_vlan_css_class(untagged, netbox_untagged_vid, exists_in_netbox, missing_vlans)
            warning = get_missing_vlan_warning(untagged, missing_vlans)
            parts.append(f'<span class="{css}">{untagged}(U){warning}</span>')

        for vid in sorted(tagged):
            css = get_tagged_vlan_css_class(vid, netbox_tagged_vids, exists_in_netbox, missing_vlans)
            warning = get_missing_vlan_warning(vid, missing_vlans)
            parts.append(f'<span class="{css}">{vid}(T){warning}</span>')

        if not parts:
            return "—"

        return mark_safe(", ".join(parts))


class VerifyVlanSyncGroupView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, View):
    """
    Verify whether a VLAN (by VID) exists in a selected VLAN group.

    Called from the VLAN sync tab when the user changes the per-row
    VLAN group dropdown. Returns the correct CSS class so the JS can
    update row colors without a full page reload.
    """

    # Read-only verify endpoint that surfaces NetBox VLAN existence + names — require
    # object-view permission on VLAN (there is no device in scope here, unlike the other
    # verify views), so an unauthorized caller can't enumerate VLANs/groups.
    required_object_permissions = {"POST": [("view", VLAN), ("view", VLANGroup)]}

    def post(self, request):
        data, err = parse_request_json(request)
        if err:
            return err
        # Gate BEFORE resolving the VLAN group / querying VLANs (mirrors the other verify views).
        if error := self.require_object_permissions_json("POST"):
            return error
        vlan_group_id = data.get("vlan_group_id")
        vid_str = data.get("vid", "")
        librenms_name = data.get("name", "")

        if not vid_str:
            return JsonResponse({"status": "error", "message": "No VID provided"}, status=400)

        try:
            vid = int(vid_str)
        except (ValueError, TypeError):
            return JsonResponse({"status": "error", "message": "Invalid VID"}, status=400)

        try:
            selected_gid = int(vlan_group_id) if vlan_group_id else None
        except (ValueError, TypeError):
            return JsonResponse({"status": "error", "message": "Invalid VLAN group ID"}, status=400)

        # Check if VLAN exists in the selected group (or globally)
        visible_vlans = self.restricted_queryset(VLAN)
        if selected_gid:
            vlan_group = self.restrict_object_or_404(VLANGroup, pk=selected_gid)
            netbox_vlan = visible_vlans.filter(vid=vid, group=vlan_group).first()
        else:
            # No group = global VLANs
            netbox_vlan = visible_vlans.filter(vid=vid, group__isnull=True).first()

        exists_in_netbox = bool(netbox_vlan)
        name_matches = netbox_vlan.name == librenms_name if netbox_vlan else False
        css_class = get_vlan_sync_css_class(exists_in_netbox, name_matches)

        return JsonResponse(
            {
                "status": "success",
                "exists_in_netbox": exists_in_netbox,
                "name_matches": name_matches,
                "css_class": css_class,
                "netbox_vlan_name": netbox_vlan.name if netbox_vlan else None,
            }
        )


class SaveVlanGroupOverridesView(
    LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, CacheMixin, View
):
    """
    Persist user VLAN-group-override selections in cache.

    When the user edits VLAN group assignments in the modal and checks
    "Apply to all interfaces", the JS posts the {vid: group_id} map here
    so that subsequent table pages render with the same choices.
    The overrides are stored with the same remaining TTL as the ports
    cache so they expire together.
    """

    def post(self, request):
        # Require plugin write permission to persist VLAN group overrides
        if error := self.require_write_permission_json():
            return error

        data, err = parse_request_json(request)
        if err:
            return err
        device_id = data.get("device_id")
        vid_group_map = data.get("vid_group_map", {})

        if not device_id:
            return JsonResponse({"status": "error", "message": "No device ID provided"}, status=400)
        # Configured-string-key-or-fallback, mirroring the verify views: this WRITES the
        # overrides into a server-scoped cache namespace, so a forged/non-string key must
        # neither address an arbitrary namespace nor TypeError-500 the cf lookups below.
        server_key = self.resolve_requested_server_key(data)

        # Object-scope the lookup: require_write_permission_json above only checks plugin-wide write
        # access, so without this any plugin-writer could persist VLAN overrides for a device they
        # can't even view (out-of-scope pk). Restrict fail-closes it to a 404 like a nonexistent id.
        device = self.restrict_object_or_404(Device, pk=device_id)

        # Normalise to the VC sync device so cache keys match what the sync view stored
        sync_device = get_librenms_sync_device(device, server_key=server_key)
        if sync_device is None:
            sync_device = device

        # Use the remaining TTL of the ports cache so both expire together. .ttl() is a
        # django-redis extension; degrade to None on a backend without it (e.g. LocMemCache)
        # so a non-Redis deployment gets a graceful "refresh first" 400 instead of a 500.
        ports_ttl = cache_remaining_ttl(cache, self.get_cache_key(sync_device, "ports", server_key))
        if ports_ttl is None or ports_ttl <= 0:
            return JsonResponse(
                {"status": "error", "message": "No cached port data; refresh interfaces first"},
                status=400,
            )

        # Merge with any existing overrides (user may save multiple times)
        existing = cache.get(self.get_vlan_overrides_key(sync_device, server_key)) or {}
        existing.update(vid_group_map)

        cache.set(self.get_vlan_overrides_key(sync_device, server_key), existing, timeout=ports_ttl)

        return JsonResponse({"status": "success"})


class DeviceCableTableView(BaseCableTableView):
    """Cable synchronization view for Devices."""

    model = Device

    def get_table(self, data, obj):
        """Return the appropriate cable table, selecting VC variant if needed."""
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            return VCCableTable(data, device=obj)
        return LibreNMSCableTable(data, device=obj)


class DeviceIPAddressTableView(BaseIPAddressTableView):
    """IP address synchronization view for Devices."""

    model = Device


class DeviceVLANTableView(BaseVLANTableView):
    """VLAN synchronization table view for Devices."""

    model = Device


class DeviceModuleTableView(BaseModuleTableView):
    """Module/inventory synchronization view for Devices."""

    model = Device

    def get_table(self, data, obj):
        """Return the module sync table."""
        user = self.request.user
        has_write_permission = self.has_write_permission()
        table_class = VCModuleTable if hasattr(obj, "virtual_chassis") and obj.virtual_chassis else LibreNMSModuleTable
        table = table_class(
            data,
            device=obj,
            server_key=self.librenms_api.server_key,
            has_write_permission=has_write_permission,
            can_add_module=has_write_permission and user.has_perm("dcim.add_module"),
            can_change_module=has_write_permission and user.has_perm("dcim.change_module"),
            can_change_interface=has_write_permission and user.has_perm("dcim.change_interface"),
            can_delete_module=has_write_permission and user.has_perm("dcim.delete_module"),
            can_add_module_bay_template=(has_write_permission and user.has_perm("dcim.add_modulebaytemplate")),
            can_add_module_type=(has_write_permission and user.has_perm("dcim.add_moduletype")),
            can_add_carrier_rule=(
                has_write_permission and user.has_perm("netbox_librenms_plugin.add_carrierautoinstallrule")
            ),
            can_add_module_bay_mapping=(
                has_write_permission and user.has_perm("netbox_librenms_plugin.add_modulebaymapping")
            ),
            can_add_module_type_mapping=(
                has_write_permission and user.has_perm("netbox_librenms_plugin.add_moduletypemapping")
            ),
        )
        server_key = self.librenms_api.server_key
        table.htmx_url = f"{self.request.path}?tab=modules" + (f"&server_key={server_key}" if server_key else "")
        return table
