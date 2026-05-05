import copy
import json

from dcim.models import Device
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from utilities.views import ViewTab, register_model_view

from netbox_librenms_plugin.constants import PERM_VIEW_PLUGIN
from netbox_librenms_plugin.tables.cables import (
    LibreNMSCableTable,
    VCCableTable,
)
from netbox_librenms_plugin.tables.interfaces import (
    LibreNMSInterfaceTable,
    VCInterfaceTable,
)
from netbox_librenms_plugin.tables.modules import LibreNMSModuleTable
from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    get_librenms_sync_device,
    get_missing_vlan_warning,
    get_tagged_vlan_css_class,
    get_untagged_vlan_css_class,
    get_vlan_sync_css_class,
)

from ..base.cables_view import BaseCableTableView
from ..base.interfaces_view import BaseInterfaceTableView
from ..base.ip_addresses_view import BaseIPAddressTableView
from ..base.librenms_sync_view import BaseLibreNMSSyncView
from ..base.modules_view import BaseModuleTableView
from ..base.vlan_table_view import BaseVLANTableView
from ..mixins import CacheMixin, LibreNMSAPIMixin, LibreNMSPermissionMixin


@register_model_view(Device, name="librenms_sync", path="librenms-sync")
class DeviceLibreNMSSyncView(BaseLibreNMSSyncView):
    """Device detail tab showing LibreNMS sync information."""

    queryset = Device.objects.all()
    model = Device
    tab = ViewTab(label="LibreNMS Sync", permission=PERM_VIEW_PLUGIN)

    def get_interface_context(self, request, obj):
        """Return interface sync context for the device."""
        interface_name_field = get_interface_name_field(request)
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


class SingleInterfaceVerifyView(LibreNMSPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """Verify single interface data for a device via cached LibreNMS payload."""

    def post(self, request):
        """Verify interface data against cached LibreNMS ports for a device."""
        data = json.loads(request.body)
        selected_device_id = data.get("device_id")
        interface_name = data.get("interface_name")
        interface_name_field = data.get("interface_name_field") or get_interface_name_field()
        server_key = data.get("server_key")

        if not selected_device_id:
            return JsonResponse({"status": "error", "message": "No device ID provided"}, status=400)
        if not server_key:
            server_key = self.librenms_api.server_key

        selected_device = get_object_or_404(Device, pk=selected_device_id)

        # Normalise to the VC sync device so cache keys match what the sync view stored
        if selected_device.virtual_chassis:
            primary_device = get_librenms_sync_device(selected_device, server_key=server_key)
            if primary_device is None:
                return JsonResponse(
                    {"status": "error", "message": "No sync device found for virtual chassis"}, status=404
                )
        else:
            primary_device = selected_device

        cached_data = cache.get(self.get_cache_key(primary_device, "ports", server_key))

        if cached_data:
            port_data = next(
                (port for port in cached_data.get("ports", []) if port.get(interface_name_field) == interface_name),
                None,
            )

            if port_data:
                table_class = VCInterfaceTable if selected_device.virtual_chassis else LibreNMSInterfaceTable
                table = table_class(
                    [],
                    device=selected_device,
                    interface_name_field=interface_name_field,
                    server_key=server_key,
                )
                formatted_row = table.format_interface_data(port_data, selected_device)
                return JsonResponse({"status": "success", "formatted_row": formatted_row})

        return JsonResponse({"status": "error", "message": "Interface data not found"}, status=404)


class SingleVlanGroupVerifyView(LibreNMSPermissionMixin, CacheMixin, View):
    """
    Verify VLAN assignments for an interface against a specific VLAN group.

    When user changes the VLAN group dropdown, this endpoint re-computes
    which VLANs are "missing" (don't exist in selected group) and returns
    updated HTML for the VLANs cell with correct colors.
    """

    def post(self, request):
        from ipam.models import VLAN, VLANGroup

        data = json.loads(request.body)
        device_id = data.get("device_id")
        interface_name = data.get("interface_name")
        vlan_group_id = data.get("vlan_group_id")
        vlan_type = data.get("vlan_type", "U")  # "U" or "T"
        vid_str = data.get("vid", "") or data.get("untagged_vlan", "")

        if not device_id:
            return JsonResponse({"status": "error", "message": "No device ID provided"}, status=400)
        if not vid_str:
            return JsonResponse({"status": "error", "message": "No VID provided"}, status=400)

        device = get_object_or_404(Device, pk=device_id)
        try:
            vid = int(vid_str)
        except (ValueError, TypeError):
            return JsonResponse({"status": "error", "message": "Invalid VID"}, status=400)

        # Build lookup for the selected group
        if vlan_group_id:
            vlan_group = get_object_or_404(VLANGroup, pk=vlan_group_id)
            # Get VLANs in selected group + global VLANs
            group_vids = set(VLAN.objects.filter(group=vlan_group).values_list("vid", flat=True))
            global_vids = set(VLAN.objects.filter(group__isnull=True).values_list("vid", flat=True))
            available_vids = group_vids | global_vids
        else:
            # No group selected - use global VLANs only
            available_vids = set(VLAN.objects.filter(group__isnull=True).values_list("vid", flat=True))

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
        selected_gid = int(vlan_group_id) if vlan_group_id else None

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


class VerifyVlanSyncGroupView(LibreNMSPermissionMixin, View):
    """
    Verify whether a VLAN (by VID) exists in a selected VLAN group.

    Called from the VLAN sync tab when the user changes the per-row
    VLAN group dropdown. Returns the correct CSS class so the JS can
    update row colors without a full page reload.
    """

    def post(self, request):
        from ipam.models import VLAN, VLANGroup

        data = json.loads(request.body)
        vlan_group_id = data.get("vlan_group_id")
        vid_str = data.get("vid", "")
        librenms_name = data.get("name", "")

        if not vid_str:
            return JsonResponse({"status": "error", "message": "No VID provided"}, status=400)

        try:
            vid = int(vid_str)
        except (ValueError, TypeError):
            return JsonResponse({"status": "error", "message": "Invalid VID"}, status=400)

        # Check if VLAN exists in the selected group (or globally)
        if vlan_group_id:
            vlan_group = get_object_or_404(VLANGroup, pk=vlan_group_id)
            netbox_vlan = VLAN.objects.filter(vid=vid, group=vlan_group).first()
        else:
            # No group = global VLANs
            netbox_vlan = VLAN.objects.filter(vid=vid, group__isnull=True).first()

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


class SaveVlanGroupOverridesView(LibreNMSPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
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

        data = json.loads(request.body)
        device_id = data.get("device_id")
        vid_group_map = data.get("vid_group_map", {})
        server_key = data.get("server_key")

        if not device_id:
            return JsonResponse({"status": "error", "message": "No device ID provided"}, status=400)
        if not server_key:
            server_key = self.librenms_api.server_key

        device = get_object_or_404(Device, pk=device_id)

        # Normalise to the VC sync device so cache keys match what the sync view stored
        sync_device = get_librenms_sync_device(device, server_key=server_key)
        if sync_device is None:
            sync_device = device

        # Use the remaining TTL of the ports cache so both expire together
        ports_ttl = cache.ttl(self.get_cache_key(sync_device, "ports", server_key))
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
        table = LibreNMSModuleTable(
            data,
            device=obj,
            server_key=self.librenms_api.server_key,
            has_write_permission=has_write_permission,
            can_add_module=has_write_permission and user.has_perm("dcim.add_module"),
            can_change_module=has_write_permission and user.has_perm("dcim.change_module"),
            can_delete_module=has_write_permission and user.has_perm("dcim.delete_module"),
        )
        server_key = self.librenms_api.server_key
        table.htmx_url = f"{self.request.path}?tab=modules" + (f"&server_key={server_key}" if server_key else "")
        return table
