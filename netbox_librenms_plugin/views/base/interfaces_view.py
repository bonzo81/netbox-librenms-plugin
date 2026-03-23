from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.utils import (
    get_interface_name_field,
    get_virtual_chassis_member,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    VlanAssignmentMixin,
)


class BaseInterfaceTableView(VlanAssignmentMixin, LibreNMSAPIMixin, LibreNMSPermissionMixin, CacheMixin, View):
    """
    Base view for fetching interface data from LibreNMS and generating table data.
    Includes VLAN enrichment for interface VLAN sync functionality.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_interface_sync_content.html"
    interface_name_field = None

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine)."""
        return get_object_or_404(self.model, pk=pk)

    def get_ip_address(self, obj):
        """Get the primary IP address for the object."""
        if obj.primary_ip:
            return str(obj.primary_ip.address.ip)
        return None

    def get_interfaces(self, obj):
        """
        Get interfaces related to the object.
        Should be implemented in subclasses.
        """
        raise NotImplementedError

    def get_redirect_url(self, obj):
        """
        Get the redirect URL for the object.
        Should be implemented in subclasses.
        """
        raise NotImplementedError

    def get_select_related_field(self, obj):
        """Determine the appropriate select_related field based on object type"""
        if self.model.__name__.lower() == "virtualmachine":
            return "virtual_machine"
        return "device"

    def get_table(self, data, obj, interface_name_field, vlan_groups=None):
        """
        Returns the table class to use for rendering interface data.
        Can be overridden by subclasses to use different tables.

        Args:
            data: List of port data dicts
            obj: Device or VirtualMachine object
            interface_name_field: Field to use for interface name ('ifName' or 'ifDescr')
            vlan_groups: List of VLANGroup objects for VLAN group dropdowns
        """
        raise NotImplementedError("Subclasses must implement get_table()")

    def post(self, request, pk):
        """Handle POST request to fetch and cache LibreNMS interface data for an object."""
        obj = self.get_object(pk)

        interface_name_field = get_interface_name_field(request)

        # Get librenms_id at the start
        self.librenms_id = self.librenms_api.get_librenms_id(obj)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS.")
            return redirect(self.get_redirect_url(obj))

        success, librenms_data = self.librenms_api.get_ports(self.librenms_id)

        if not success:
            messages.error(request, librenms_data)
            return redirect(self.get_redirect_url(obj))

        # Enrich ports with VLAN data for trunk ports
        ports = librenms_data.get("ports", [])
        enriched_ports = self._enrich_ports_with_vlan_data(ports, interface_name_field)
        librenms_data["ports"] = enriched_ports

        _server_key = self.librenms_api.server_key
        # Store data in cache (keyed by server to avoid cross-server collisions)
        cache.set(
            self.get_cache_key(obj, "ports", _server_key),
            librenms_data,
            timeout=self.librenms_api.cache_timeout,
        )
        last_fetched = timezone.now()
        cache.set(
            self.get_last_fetched_key(obj, "ports", _server_key),
            last_fetched,
            timeout=self.librenms_api.cache_timeout,
        )

        messages.success(request, "Interface data refreshed successfully.")

        context = self.get_context_data(request, obj, interface_name_field, _server_key)
        context = {"interface_sync": context}
        context["interface_name_field"] = interface_name_field

        return render(request, self.partial_template_name, context)

    def _enrich_ports_with_vlan_data(self, ports, interface_name_field):
        """
        Enrich port data with VLAN information from LibreNMS.

        With LibreNMS 24.2.0+, the get_ports() call with with_vlans=True returns
        detailed VLAN associations (tagged/untagged) for all ports. The
        parse_port_vlan_data() method handles both the new vlans array format
        and falls back to ifVlan for older LibreNMS versions.

        Args:
            ports: List of port dicts from get_ports(with_vlans=True)
            interface_name_field: Field to use for interface name

        Returns:
            List of enriched port dicts with VLAN data
        """
        enriched = []
        for port in ports:
            # Parse VLAN data - handles both vlans array (new) and ifVlan fallback (old)
            parsed = self.librenms_api.parse_port_vlan_data(port, interface_name_field)
            port.update(parsed)
            enriched.append(port)
        return enriched

    def get_context_data(self, request, obj, interface_name_field, server_key=None):
        """Get the context data for the interface sync view."""
        ports_data = []
        table = None
        netbox_only_interfaces = []

        if interface_name_field is None:
            interface_name_field = get_interface_name_field(request)

        if server_key is None:
            server_key = getattr(self.librenms_api, "server_key", None)

        cached_data = cache.get(self.get_cache_key(obj, "ports", server_key))
        last_fetched = cache.get(self.get_last_fetched_key(obj, "ports", server_key))

        # Get VLAN groups for dropdown
        vlan_groups = self.get_vlan_groups_for_device(obj)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)

        # Load any user VLAN group overrides from cache (set by "apply to all")
        vlan_group_overrides = cache.get(self.get_vlan_overrides_key(obj, server_key)) or {}

        if cached_data:
            ports_data = cached_data.get("ports", [])

            # Pre-fetch all interfaces for all potential chassis members
            interfaces_by_device = {}
            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                for member in obj.virtual_chassis.members.all():
                    interfaces_by_device[member.id] = {
                        interface.name: interface
                        for interface in self.get_interfaces(member).select_related(self.get_select_related_field(obj))
                    }
            else:
                interfaces_by_device[obj.id] = {
                    interface.name: interface
                    for interface in self.get_interfaces(obj).select_related(self.get_select_related_field(obj))
                }

            for port in ports_data:
                port["enabled"] = (
                    True
                    if port.get("ifAdminStatus") is None
                    else (
                        port["ifAdminStatus"].lower() == "up"
                        if isinstance(port["ifAdminStatus"], str)
                        else bool(port["ifAdminStatus"])
                    )
                )

                if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                    chassis_member = get_virtual_chassis_member(obj, port.get(interface_name_field))
                    device_interfaces = interfaces_by_device.get(chassis_member.id, {})
                else:
                    device_interfaces = interfaces_by_device[obj.id]

                netbox_interface = device_interfaces.get(port.get(interface_name_field))
                port["exists_in_netbox"] = bool(netbox_interface)
                port["netbox_interface"] = netbox_interface

                if port.get("ifAlias") in (port.get("ifDescr"), port.get("ifName")):
                    port["ifAlias"] = ""

                # Add VLAN group auto-selection data to port, applying any user overrides
                self._add_vlan_group_selection(port, lookup_maps, obj, vlan_group_overrides)

                # Add missing VLANs info for warning display
                self._add_missing_vlans_info(port, lookup_maps)

            table = self.get_table(ports_data, obj, interface_name_field, vlan_groups=vlan_groups)
            table.configure(request)

            # Identify NetBox-only interfaces (interfaces in NetBox but not in LibreNMS)
            librenms_interface_names = {
                port.get(interface_name_field) for port in ports_data if port.get(interface_name_field)
            }

            netbox_only_interfaces = []
            for device_id, device_interfaces in interfaces_by_device.items():
                for interface_name, interface in device_interfaces.items():
                    if interface_name not in librenms_interface_names:
                        # Get device name for the interface
                        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                            device = obj.virtual_chassis.members.get(id=device_id)
                            device_name = device.name
                        else:
                            device_name = obj.name

                        netbox_only_interfaces.append(
                            {
                                "id": interface.id,
                                "name": interface.name,
                                "device_name": device_name,
                                "device_id": device_id,
                                "type": str(interface.type)
                                if hasattr(interface, "type") and interface.type
                                else "Virtual"
                                if hasattr(interface, "virtual_machine")
                                else "Unknown",
                                "enabled": interface.enabled,
                                "description": interface.description or "",
                                "url": interface.get_absolute_url(),
                            }
                        )

        virtual_chassis_members = []
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            virtual_chassis_members = obj.virtual_chassis.members.all()

        cache_ttl = cache.ttl(self.get_cache_key(obj, "ports", server_key))
        cache_expiry = (
            timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl is not None and cache_ttl > 0 else None
        )

        return {
            "object": obj,
            "table": table,
            "vlan_groups": vlan_groups,
            "last_fetched": last_fetched,
            "cache_expiry": cache_expiry,
            "virtual_chassis_members": virtual_chassis_members,
            "interface_name_field": interface_name_field,
            "netbox_only_interfaces": netbox_only_interfaces,
            "server_key": server_key,
        }

    def _add_vlan_group_selection(self, port, lookup_maps, device, vlan_group_overrides=None):
        """
        Add per-VLAN group auto-selection data to port record.

        Sets:
        - vlan_group_map: {vid: {"group_id": str, "group_name": str, "is_ambiguous": bool}}
          Maps each VID to its auto-selected VLAN group based on scope hierarchy.
          If vlan_group_overrides contains a user selection for a VID, that takes
          precedence over auto-selection.
        """
        vid_to_groups = lookup_maps.get("vid_to_groups", {})
        untagged_vid = port.get("untagged_vlan")
        tagged_vids = port.get("tagged_vlans", [])

        all_vids = []
        if untagged_vid:
            all_vids.append(untagged_vid)
        all_vids.extend(tagged_vids)

        vlan_group_map = {}
        for vid in all_vids:
            groups = vid_to_groups.get(vid, [])
            if len(groups) == 1:
                vlan_group_map[vid] = {
                    "group_id": str(groups[0].pk),
                    "group_name": groups[0].name,
                    "is_ambiguous": False,
                }
            elif len(groups) > 1:
                most_specific = self._select_most_specific_group(groups, device)
                if most_specific:
                    vlan_group_map[vid] = {
                        "group_id": str(most_specific.pk),
                        "group_name": most_specific.name,
                        "is_ambiguous": False,
                    }
                else:
                    vlan_group_map[vid] = {
                        "group_id": "",
                        "group_name": "Ambiguous",
                        "is_ambiguous": True,
                    }
            else:
                vlan_group_map[vid] = {
                    "group_id": "",
                    "group_name": "Global",
                    "is_ambiguous": False,
                }

        # Apply user overrides from "apply to all" selections (persisted in cache)
        if vlan_group_overrides:
            from ipam.models import VLANGroup

            # Batch-fetch all referenced override group IDs to avoid N+1 queries
            override_group_ids = {
                vlan_group_overrides[str(vid)]
                for vid in all_vids
                if str(vid) in vlan_group_overrides and vlan_group_overrides[str(vid)]
            }
            override_groups_by_id = {}
            if override_group_ids:
                override_groups_by_id = VLANGroup.objects.in_bulk(list(override_group_ids))

            for vid in all_vids:
                vid_str = str(vid)
                if vid_str in vlan_group_overrides:
                    override_group_id = vlan_group_overrides[vid_str]
                    if override_group_id:
                        group = override_groups_by_id.get(int(override_group_id))
                        if group:
                            vlan_group_map[vid] = {
                                "group_id": str(group.pk),
                                "group_name": group.name,
                                "is_ambiguous": False,
                            }
                        # else: Override references deleted group; keep auto-selection
                    else:
                        # User explicitly chose "No Group (Global)"
                        vlan_group_map[vid] = {
                            "group_id": "",
                            "group_name": "Global",
                            "is_ambiguous": False,
                        }

        port["vlan_group_map"] = vlan_group_map

    def _add_missing_vlans_info(self, port, lookup_maps):
        """
        Add missing VLANs info to port record for warning display.

        Sets:
        - missing_vlans: List of VIDs not found in any NetBox VLAN group
        """
        vid_to_vlans = lookup_maps.get("vid_to_vlans", {})
        missing_vlans = []

        untagged_vid = port.get("untagged_vlan")
        tagged_vids = port.get("tagged_vlans", [])

        if untagged_vid and untagged_vid not in vid_to_vlans:
            missing_vlans.append(untagged_vid)

        for vid in tagged_vids:
            if vid not in vid_to_vlans:
                missing_vlans.append(vid)

        port["missing_vlans"] = missing_vlans
