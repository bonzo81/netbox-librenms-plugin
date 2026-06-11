import logging
from urllib.parse import quote_plus

from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from netbox_librenms_plugin.utils import (
    build_migrated_context,
    get_interface_name_field,
    get_librenms_oob,
    get_librenms_sync_device,
    get_virtual_chassis_member,
    normalize_librenms_port_id,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    VlanAssignmentMixin,
)

logger = logging.getLogger(__name__)


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

    def _failure_redirect(self, request, obj, server_key):
        """Redirect to the sync tab after a refresh failure, preserving the POST-scoped
        server_key so the user stays on the server they were working in (otherwise the next
        retry/sync can target the session/default LibreNMS instance instead).

        server_key is POST-derived, so the candidate URL is gated by Django's
        ``url_has_allowed_host_and_scheme`` (sink inside the validated branch) — the
        open-redirect barrier CodeQL recognises for py/url-redirection (CWE-601). The bare
        ``url`` fallback is a pure ``reverse()`` path with no user input."""
        url = self.get_redirect_url(obj)
        if server_key:
            sep = "&" if "?" in url else "?"
            candidate = f"{url}{sep}server_key={quote_plus(server_key)}"
            if url_has_allowed_host_and_scheme(
                candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                return redirect(candidate)
        return redirect(url)

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

    def _get_object_librenms_id(self, obj):
        """Resolve a cached/stored LibreNMS ID for any NetBox object without dynamic fallback noise."""
        librenms_id = self.librenms_api.get_stored_librenms_id(obj)
        if not isinstance(librenms_id, (int, str)) or isinstance(librenms_id, bool):
            return None
        return normalize_librenms_port_id(librenms_id)

    def _build_interface_lookup_maps(self, obj):
        """Build name and LibreNMS ID indexes, dropping conflicting IDs entirely."""
        by_name = {}
        by_librenms_id = {}
        duplicate_librenms_ids = set()

        for interface in self.get_interfaces(obj).select_related(self.get_select_related_field(obj)):
            by_name[interface.name] = interface
            librenms_id = self._get_object_librenms_id(interface)
            if librenms_id is None:
                continue
            if librenms_id in by_librenms_id:
                duplicate_librenms_ids.add(librenms_id)
                continue
            by_librenms_id[librenms_id] = interface

        for librenms_id in duplicate_librenms_ids:
            by_librenms_id.pop(librenms_id, None)

        return {
            "by_name": by_name,
            "by_librenms_id": by_librenms_id,
        }

    def post(self, request, pk):
        """Handle POST request to fetch and cache LibreNMS interface data for an object."""
        obj = self.get_object(pk)

        interface_name_field = get_interface_name_field(request)

        # Rebind the API to the POSTed server BEFORE fetching the id/ports, so the live
        # lookups AND the cache writes below all target the same server in a multi-server
        # tab refresh — otherwise data fetched from the session/default server is cached
        # under the POSTed key (wrong interface set). Mirrors cables/ip/modules/vlan views.
        post_server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if post_server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            return redirect(self.get_redirect_url(obj))

        # Get librenms_id at the start (now scoped to the POSTed server).
        self.librenms_id = self.librenms_api.get_librenms_id(obj)

        if not self.librenms_id:
            messages.error(request, "Device not found in LibreNMS.")
            return self._failure_redirect(request, obj, post_server_key)

        # Resolve the cache scope once (the VC sync device for a member, else obj) and use
        # it for the up-front clear below and the writes further down. Mirrors cables_view.
        _server_key = post_server_key
        lookup_device = get_librenms_sync_device(obj, server_key=_server_key) or obj

        # A refresh must actually refresh: drop the previous snapshot up front so that if
        # the fetch below fails we fall back to an empty view + visible error, rather than
        # silently serving stale data (and letting the follow-up sync run on it). The
        # success path re-populates the cache below.
        cache.delete(self.get_cache_key(lookup_device, "ports", _server_key))
        cache.delete(self.get_last_fetched_key(lookup_device, "ports", _server_key))

        success, librenms_data = self.librenms_api.get_ports(self.librenms_id)

        if not success:
            messages.error(request, librenms_data)
            return self._failure_redirect(request, obj, _server_key)

        # Enrich ports with VLAN data for trunk ports
        ports = librenms_data.get("ports", [])
        enriched_ports = self._enrich_ports_with_vlan_data(ports, interface_name_field)
        for port in enriched_ports:
            port["_source"] = "main"
        librenms_data["ports"] = enriched_ports

        # If an OOB controller is linked, fetch its ports and merge them in. The
        # POST-resolved server key (the API was rebound above) and the sync-device cache
        # scope (lookup_device) were resolved above, so the OOB fetch, cache writes, and
        # migrated context all stay on one server — no mismatch between cached data and
        # migrated mode. Resolving OOB from the sync device (not the viewed member) matters
        # for a VC member: the OOB relationship lives on the sync device, so
        # get_librenms_oob(obj) would miss it and drop OOB rows / shared-LOM flagging.
        oob = get_librenms_oob(lookup_device, server_key=_server_key)
        oob_ports_failed = False
        if oob and oob.get("id"):
            oob_success, oob_raw = self.librenms_api.get_ports(oob["id"])
            if oob_success:
                oob_ports = oob_raw.get("ports", [])
                oob_enriched = self._enrich_ports_with_vlan_data(oob_ports, interface_name_field)
                for port in oob_enriched:
                    port["_source"] = "oob"
                # Detect shared-LOM: same MAC seen on BOTH main and OOB sides.
                # Build separate per-source MAC sets so that within-source
                # duplicates are not falsely flagged as cross-source conflicts.
                main_macs: set[str] = set()
                for port in enriched_ports:
                    mac = (port.get("ifPhysAddress") or "").lower().strip()
                    if mac:
                        main_macs.add(mac)
                oob_macs: set[str] = set()
                for port in oob_enriched:
                    mac = (port.get("ifPhysAddress") or "").lower().strip()
                    if mac:
                        oob_macs.add(mac)
                shared_macs = main_macs & oob_macs
                if shared_macs:
                    for port in enriched_ports + oob_enriched:
                        mac = (port.get("ifPhysAddress") or "").lower().strip()
                        if mac in shared_macs:
                            port["_dedup_conflict"] = True
                librenms_data["ports"] = enriched_ports + oob_enriched
            else:
                # Surface the failure: silently caching main-only data under a
                # success banner would make OOB rows / shared-LOM markers vanish
                # with no indication. Main interfaces still render.
                logger.warning(
                    "OOB ports fetch failed for device %s (OOB id %s): %s",
                    self.librenms_id,
                    oob["id"],
                    oob_raw,
                )
                messages.warning(
                    request,
                    f"Interfaces refreshed, but OOB controller ports fetch failed (OOB id {oob['id']}); "
                    "showing host interfaces only. See server logs for details.",
                )
                oob_ports_failed = True
        # On an OOB-ports fetch failure the snapshot is host-only. Rather than dropping it
        # (which would leave downstream views — SingleInterfaceVerifyView,
        # SaveVlanGroupOverridesView — with no backing snapshot), tag it `oob_incomplete`
        # and still cache it. get_context_data surfaces an "OOB incomplete" banner whenever
        # such a snapshot is rendered, so the missing OOB rows / shared-LOM markers are
        # never silently absent on a later cached render.
        # Scope the ports cache to the VC sync device (lookup_device, resolved above), not the
        # viewed member, so all VC members share one entry. Must match get_context_data()'s key.
        if oob_ports_failed:
            librenms_data["oob_incomplete"] = True
        cache.set(
            self.get_cache_key(lookup_device, "ports", _server_key),
            librenms_data,
            timeout=self.librenms_api.cache_timeout,
        )
        cache.set(
            self.get_last_fetched_key(lookup_device, "ports", _server_key),
            timezone.now(),
            timeout=self.librenms_api.cache_timeout,
        )

        # On an OOB-fetch failure the warning above already conveys the partial outcome;
        # use an accurate success banner ("host" only) rather than a blanket "successfully".
        if oob_ports_failed:
            messages.success(request, "Host interface data refreshed successfully.")
        else:
            messages.success(request, "Interface data refreshed successfully.")

        context = self.get_context_data(
            request,
            obj,
            interface_name_field,
            _server_key,
        )
        context = {"interface_sync": context}
        context["interface_name_field"] = interface_name_field
        # Keep migrated-donor mode (hidden sync button + Migrate column) consistent
        # with the full page after an HTMX tab refresh.
        context.update(build_migrated_context(obj, _server_key))

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

    def get_context_data(self, request, obj, interface_name_field, server_key=None, fresh_data=None):
        """Get the context data for the interface sync view.

        ``fresh_data`` lets a caller render from an in-memory snapshot instead of the
        cache. The OOB-ports-fetch-failure path uses this: it intentionally drops the
        partial (main-only) cache entry so the next request re-fetches, but still needs
        to render *this* response from the host ports it just fetched — reading the
        now-deleted cache would render an empty table under a "showing host interfaces"
        banner.
        """
        ports_data = []
        table = None
        netbox_only_interfaces = []

        if interface_name_field is None:
            interface_name_field = get_interface_name_field(request)

        if server_key is None:
            server_key = getattr(self.librenms_api, "server_key", None)

        # Scope the ports cache to the VC sync device (not the viewed member) so all VC
        # members share one entry instead of fragmenting / re-fetching per member. Mirrors
        # cables_view; resolves to obj itself for non-VC devices. Must match post()'s key.
        cache_device = get_librenms_sync_device(obj, server_key=server_key) or obj

        if fresh_data is not None:
            cached_data = fresh_data
            last_fetched = timezone.now()
        else:
            cached_data = cache.get(self.get_cache_key(cache_device, "ports", server_key))
            last_fetched = cache.get(self.get_last_fetched_key(cache_device, "ports", server_key))

        # A snapshot tagged oob_incomplete is host-only because the linked OOB controller's
        # ports could not be fetched on the last refresh. Surface it on every render of that
        # snapshot (via an inline banner) so the missing OOB rows are never silently absent.
        oob_incomplete = bool(cached_data.get("oob_incomplete")) if isinstance(cached_data, dict) else False

        # Get VLAN groups for dropdown
        vlan_groups = self.get_vlan_groups_for_device(obj)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)

        # Load any user VLAN group overrides from cache (set by "apply to all")
        # Read overrides under the same VC-scoped key SaveVlanGroupOverridesView writes
        # (the sync device, not the viewed member) — otherwise an "apply to all" made on a VC
        # member is lost on the next render/member switch. cache_device mirrors that writer.
        vlan_group_overrides = cache.get(self.get_vlan_overrides_key(cache_device, server_key)) or {}

        if cached_data:
            ports_data = cached_data.get("ports", [])
            matched_interface_ids = set()

            # Pre-fetch all interfaces for all potential chassis members
            interfaces_by_device = {}
            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                for member in obj.virtual_chassis.members.all():
                    interfaces_by_device[member.id] = self._build_interface_lookup_maps(member)
            else:
                interfaces_by_device[obj.id] = self._build_interface_lookup_maps(obj)

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
                    device_interfaces = interfaces_by_device.get(
                        chassis_member.id if chassis_member else obj.id,
                        {"by_name": {}, "by_librenms_id": {}},
                    )
                else:
                    device_interfaces = interfaces_by_device.get(obj.id, {"by_name": {}, "by_librenms_id": {}})

                port_id = normalize_librenms_port_id(port.get("port_id"))
                netbox_interface = device_interfaces["by_librenms_id"].get(port_id) if port_id else None
                if not netbox_interface:
                    netbox_interface = device_interfaces["by_name"].get(port.get(interface_name_field))
                port["exists_in_netbox"] = bool(netbox_interface)
                port["netbox_interface"] = netbox_interface
                # OOB-controller rows live on a separate LibreNMS device; never
                # let them mark a main-device interface as matched, or a genuine
                # netbox-only interface gets hidden by a same-named OOB port.
                if netbox_interface is not None and port.get("_source") != "oob":
                    matched_interface_ids.add(netbox_interface.id)

                if port.get("ifAlias") in (port.get("ifDescr"), port.get("ifName")):
                    port["ifAlias"] = ""

                # Add VLAN group auto-selection data to port, applying any user overrides
                self._add_vlan_group_selection(port, lookup_maps, obj, vlan_group_overrides)

                # Add missing VLANs info for warning display
                self._add_missing_vlans_info(port, lookup_maps)

            table = self.get_table(ports_data, obj, interface_name_field, vlan_groups=vlan_groups)
            table.configure(request)

            # Identify NetBox-only interfaces (interfaces in NetBox but not in LibreNMS)
            # Exclude OOB-controller rows: their names belong to a different
            # device and must not suppress main-device netbox-only detection.
            librenms_interface_names = {
                port.get(interface_name_field)
                for port in ports_data
                if port.get(interface_name_field) and port.get("_source") != "oob"
            }

            netbox_only_interfaces = []
            for device_id, device_interface_maps in interfaces_by_device.items():
                for interface_name, interface in device_interface_maps["by_name"].items():
                    if interface.id in matched_interface_ids:
                        continue
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

        cache_ttl = cache.ttl(self.get_cache_key(cache_device, "ports", server_key))
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
            "oob_incomplete": oob_incomplete,
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
