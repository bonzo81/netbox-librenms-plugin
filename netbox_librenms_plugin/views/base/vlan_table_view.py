from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.constants import LIBRENMS_VLAN_STATE_ACTIVE
from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
from netbox_librenms_plugin.utils import build_migrated_context
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    VlanAssignmentMixin,
)

# Sentinel so _get_error_context can tell an *explicit* server_key=None (stale-server
# branch — must be preserved) apart from an omitted arg (fall back to session server).
_SERVER_KEY_UNSET = object()


class BaseVLANTableView(VlanAssignmentMixin, LibreNMSAPIMixin, LibreNMSPermissionMixin, CacheMixin, View):
    """
    Base view for VLAN synchronization table.
    Fetches LibreNMS VLAN data and compares with NetBox.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_vlan_sync_content.html"

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine)."""
        return get_object_or_404(self.model, pk=pk)

    def post(self, request, pk):
        """Handle POST request to fetch and cache LibreNMS VLAN data."""
        obj = self.get_object(pk)

        # Rebind the API to the POSTed server BEFORE resolving librenms_id, so the id
        # (and the VLAN fetch/cache below) resolve against the right server in a
        # multi-server tab refresh rather than the session/default one.
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            # Pass server_key=None explicitly so the fragment doesn't silently fall back to
            # the session/default server (which would re-render with the wrong server selected
            # and let the next retry sync VLANs against the wrong LibreNMS instance).
            context = {
                "vlan_sync": self._get_error_context(
                    obj, "Selected LibreNMS server is no longer configured.", server_key=None
                ),
                # Preserve migrated-context flags on this error exit too (every other POST
                # path includes them); otherwise a donor/winner device loses its migration
                # controls on a stale-server refresh until the next full reload.
                **build_migrated_context(obj, request.POST.get("server_key")),
            }
            return render(request, self.partial_template_name, context)
        migrated = build_migrated_context(obj, server_key)

        # Get librenms_id (now scoped to the POSTed server).
        self.librenms_id = self.librenms_api.get_librenms_id(obj)

        if not self.librenms_id:
            # Drop any prior snapshot for this server: a failed refresh on a previously-synced
            # device must not leave a stale VLAN table the next GET can render and act on. The
            # cache is scoped by server_key, so evict the same scoped keys.
            cache.delete(self.get_cache_key(obj, "vlans", server_key))
            cache.delete(self.get_last_fetched_key(obj, "vlans", server_key))
            messages.error(request, "Device not found in LibreNMS.")
            context = {
                "vlan_sync": self._get_error_context(obj, "Device not found in LibreNMS", server_key=server_key),
                **migrated,
            }
            return render(request, self.partial_template_name, context)

        # Fetch VLAN data from LibreNMS
        success, error_msg = self._fetch_and_cache_vlan_data(obj, server_key)
        if not success:
            cache.delete(self.get_cache_key(obj, "vlans", server_key))
            cache.delete(self.get_last_fetched_key(obj, "vlans", server_key))
            messages.error(request, error_msg)
            context = {"vlan_sync": self._get_error_context(obj, error_msg, server_key=server_key), **migrated}
            return render(request, self.partial_template_name, context)

        messages.success(request, "VLAN data refreshed successfully.")

        context = {"vlan_sync": self.get_vlan_context(request, obj, server_key), **migrated}
        return render(request, self.partial_template_name, context)

    def _fetch_and_cache_vlan_data(self, obj, server_key=None):
        """
        Fetch VLAN data from LibreNMS and cache it.

        Returns:
            tuple: (success: bool, error_message: str or None)
        """
        # Fetch device VLANs
        success, vlans_data = self.librenms_api.get_device_vlans(self.librenms_id)
        if not success:
            return False, f"Failed to fetch VLANs: {vlans_data}"

        # Cache VLANs (scoped to the POST-resolved server when provided).
        server_key = server_key or self.librenms_api.server_key
        cache.set(
            self.get_cache_key(obj, "vlans", server_key),
            vlans_data,
            timeout=self.librenms_api.cache_timeout,
        )
        cache.set(
            self.get_last_fetched_key(obj, "vlans", server_key),
            timezone.now(),
            timeout=self.librenms_api.cache_timeout,
        )

        return True, None

    def get_vlan_context(self, request, obj, server_key=None):
        """
        Build context for VLAN sync table.

        Returns context with:
        - vlan_table: LibreNMSVLANTable instance
        - vlan_groups: QuerySet of available VLAN groups
        """
        vlan_table = None

        # Get cached data (scoped to the POST-resolved server when provided).
        server_key = server_key or getattr(self.librenms_api, "server_key", None)
        cached_vlans = cache.get(self.get_cache_key(obj, "vlans", server_key))
        last_fetched = cache.get(self.get_last_fetched_key(obj, "vlans", server_key))

        # Get available VLAN groups for this device
        vlan_groups = self.get_vlan_groups_for_device(obj)

        # Build lookup maps for VLAN matching
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)

        if cached_vlans:
            # Compare VLANs with NetBox (against all device-available VLANs)
            compared_vlans = self.compare_vlans(cached_vlans, lookup_maps, device=obj)

            vlan_table = LibreNMSVLANTable(compared_vlans, vlan_groups=vlan_groups)
            vlan_table.configure(request)

        # Calculate cache TTL. ``ttl()`` is Redis-specific and not part of the Django cache
        # API; guard with getattr so non-Redis backends return None instead of raising.
        cache_ttl = getattr(cache, "ttl", lambda k: None)(self.get_cache_key(obj, "vlans", server_key))
        cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl) if cache_ttl and cache_ttl > 0 else None

        return {
            "object": obj,
            "vlan_table": vlan_table,
            "vlan_groups": vlan_groups,
            "last_fetched": last_fetched,
            "cache_expiry": cache_expiry,
            "server_key": server_key,
        }

    def _get_error_context(self, obj, error_message, server_key=_SERVER_KEY_UNSET):
        """Build context for error state.

        Keep the fragment's ``server_key`` on the POST-resolved server so a retry/action
        after an error targets the same scope. An explicit ``server_key=None`` (the
        stale-server branch) is preserved as-is — using ``or`` here would treat it as
        falsey and silently fall back to the session server, re-rendering the fragment
        on the wrong (still-configured) server. Only an *omitted* server_key falls back.
        """
        resolved = getattr(self.librenms_api, "server_key", None) if server_key is _SERVER_KEY_UNSET else server_key
        return {
            "object": obj,
            "error_message": error_message,
            "vlan_table": None,
            "vlan_groups": self.get_vlan_groups_for_device(obj),
            "server_key": resolved,
        }

    def compare_vlans(self, librenms_vlans, lookup_maps=None, device=None):
        """
        Compare LibreNMS VLANs against NetBox VLANs available to the device.

        Args:
            librenms_vlans: List of VLAN dicts from LibreNMS
            lookup_maps: Dict with vid_to_groups, vid_group_to_vlan, vid_to_vlans
            device: NetBox Device object for scope-based prioritization

        Adds comparison flags:
        - exists_in_netbox: bool
        - netbox_vlan: VLAN object or None
        - netbox_vlan_group: VLANGroup name or None
        - name_matches: bool
        - auto_selected_group_id: ID of auto-selected group or None
        - auto_selected_group_name: Name of auto-selected group or None
        - is_ambiguous: bool - True if VID exists in multiple groups with no clear priority
        """
        lookup_maps = lookup_maps or {}
        vid_to_groups = lookup_maps.get("vid_to_groups", {})
        vid_to_vlans = lookup_maps.get("vid_to_vlans", {})

        compared = []
        for vlan in librenms_vlans:
            vid = vlan.get("vlan_vlan")
            name = vlan.get("vlan_name", "")

            # Auto-selection logic for VLAN group dropdown
            auto_selected_group_id = None
            auto_selected_group_name = None
            is_ambiguous = False
            netbox_vlan = None

            # Check if VID exists in groups for auto-selection
            if vid in vid_to_groups:
                groups = vid_to_groups[vid]
                if len(groups) == 1:
                    auto_selected_group_id = groups[0].pk
                    auto_selected_group_name = groups[0].name
                    # Get the VLAN from this single group
                    vlans_for_vid = vid_to_vlans.get(vid, [])
                    if vlans_for_vid:
                        netbox_vlan = vlans_for_vid[0]
                elif len(groups) > 1:
                    # Try to select the most specific group based on device context
                    most_specific = self._select_most_specific_group(groups, device)
                    if most_specific:
                        auto_selected_group_id = most_specific.pk
                        auto_selected_group_name = most_specific.name
                        # Get the VLAN from the most specific group
                        vlans_for_vid = vid_to_vlans.get(vid, [])
                        for v in vlans_for_vid:
                            if v.group and v.group.pk == most_specific.pk:
                                netbox_vlan = v
                                break
                    else:
                        is_ambiguous = True
            else:
                # Check if it exists as a global VLAN (no group)
                vlans_for_vid = vid_to_vlans.get(vid, [])
                for v in vlans_for_vid:
                    if v.group is None:
                        netbox_vlan = v
                        break

            compared.append(
                {
                    "vlan_id": vid,
                    "name": name,
                    "type": vlan.get("vlan_type", "ethernet"),
                    "state": vlan.get("vlan_state", LIBRENMS_VLAN_STATE_ACTIVE),
                    "exists_in_netbox": bool(netbox_vlan),
                    "netbox_vlan_id": netbox_vlan.pk if netbox_vlan else None,
                    "netbox_vlan_name": netbox_vlan.name if netbox_vlan else None,
                    "netbox_vlan_group": netbox_vlan.group.name if netbox_vlan and netbox_vlan.group else None,
                    "netbox_vlan_group_id": netbox_vlan.group.pk if netbox_vlan and netbox_vlan.group else None,
                    "name_matches": netbox_vlan.name == name if netbox_vlan else False,
                    # Fields for per-row VLAN group selection
                    "auto_selected_group_id": auto_selected_group_id,
                    "auto_selected_group_name": auto_selected_group_name,
                    "is_ambiguous": is_ambiguous,
                }
            )

        return compared
