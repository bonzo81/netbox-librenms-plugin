from django.contrib import messages
from django.core.cache import cache
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.constants import LIBRENMS_VLAN_STATE_ACTIVE
from netbox_librenms_plugin.tables.vlans import LibreNMSVLANTable
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, request_actor_id
from netbox_librenms_plugin.utils import (
    cache_remaining_ttl,
    coerce_librenms_id,
    is_list_of_dicts,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
)

# Sentinel so _get_error_context can tell an *explicit* server_key=None (stale-server
# branch — must be preserved) apart from an omitted arg (fall back to session server).
_SERVER_KEY_UNSET = object()


class BaseVLANTableView(
    VlanAssignmentMixin, LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View
):
    """
    Base view for VLAN synchronization table.
    Fetches LibreNMS VLAN data and compares with NetBox.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_vlan_sync_content.html"

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine)."""
        return self.restrict_object_or_404(self.model, pk=pk)

    def post(self, request, pk):
        """Handle POST request to fetch and cache LibreNMS VLAN data."""
        obj = self.get_object(pk)

        # Rebind the API to the POSTed server BEFORE resolving librenms_id, so the id
        # (and the VLAN fetch/cache below) resolve against the right server in a
        # multi-server tab refresh rather than the session/default one.
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            # rebind_api_for_server() returned None precisely to avoid constructing a missing/
            # misconfigured default client, so don't touch the lazy `librenms_api` property here —
            # it would reconstruct LibreNMSAPI() and can raise, turning this HTMX error path into a
            # 500. Read the already-cached client's key (else "default").
            active_server_key = self.active_server_key
            # Pass server_key=None explicitly so the fragment doesn't silently fall back to
            # the session/default server (which would re-render with the wrong server selected
            # and let the next retry sync VLANs against the wrong LibreNMS instance).
            # Resolve the marker under the session/active server key — NOT the POSTed key, which
            # failed to rebind (stale/unconfigured) and would miss the marker entirely. render_sync_partial
            # always injects the migrated-context flags so the donor keeps its migration controls.
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {
                    "vlan_sync": self._get_error_context(
                        obj, "Selected LibreNMS server is no longer configured.", server_key=None
                    )
                },
            )

        # Get librenms_id (now scoped to the POSTed server). coerce_librenms_id fails closed on a
        # poisoned cached value (bool/zero/negative/garbage) — the device-id cache path of
        # get_librenms_id returns its value verbatim, so a stray True would otherwise int() to 1
        # and fetch a stranger's VLANs. Mirrors the cables/interfaces/IP views in this PR.
        self.librenms_id = coerce_librenms_id(self.librenms_api.get_librenms_id(obj))

        if self.librenms_id is None:
            # Drop any prior snapshot for this server: a failed refresh on a previously-synced
            # device must not leave a stale VLAN table the next GET can render and act on. The
            # cache is scoped by server_key, so evict the same scoped keys.
            cache.delete(self.get_cache_key(obj, "vlans", server_key))
            cache.delete(self.get_last_fetched_key(obj, "vlans", server_key))
            SyncCacheConsistency(obj).mark_refresh_failure(
                SyncTab.VLANS,
                server_key,
                actor_id=request_actor_id(request),
            )
            messages.error(request, "Device not found in LibreNMS.")
            return self.render_sync_partial(
                request,
                obj,
                server_key,
                {"vlan_sync": self._get_error_context(obj, "Device not found in LibreNMS", server_key=server_key)},
            )

        # Fetch VLAN data from LibreNMS
        success, error_msg = self._fetch_and_cache_vlan_data(obj, server_key)
        if not success:
            cache.delete(self.get_cache_key(obj, "vlans", server_key))
            cache.delete(self.get_last_fetched_key(obj, "vlans", server_key))
            SyncCacheConsistency(obj).mark_refresh_failure(
                SyncTab.VLANS,
                server_key,
                actor_id=request_actor_id(request),
            )
            messages.error(request, error_msg)
            return self.render_sync_partial(
                request, obj, server_key, {"vlan_sync": self._get_error_context(obj, error_msg, server_key=server_key)}
            )

        messages.success(request, "VLAN data refreshed successfully.")
        SyncCacheConsistency(obj).mark_refresh_success(
            SyncTab.VLANS,
            server_key,
            actor_id=request_actor_id(request),
        )

        return self.render_sync_partial(
            request, obj, server_key, {"vlan_sync": self.get_vlan_context(request, obj, server_key)}
        )

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

        # A success=True response can still carry a malformed-but-truthy payload (string, list
        # of scalars, etc.); caching it would later 500 in compare_vlans() on vlan.get(...).
        # Reject anything that isn't a list of dict rows before caching (issue #100). An empty
        # list is valid (a device with no VLANs).
        if not is_list_of_dicts(vlans_data):
            return False, "Unexpected response from LibreNMS (malformed VLAN payload)."

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

        Args:
            request (HttpRequest): The current request.
            obj (Device | VirtualMachine): The NetBox object to synchronize.
            server_key (str | None): The selected LibreNMS server key. Resolve it from the request if None.

        Returns:
            dict: Context with:
                - ``vlan_table``: LibreNMSVLANTable instance.
                - ``vlan_groups``: QuerySet of available VLAN groups.
        """
        vlan_table = None

        # Get cached data (scoped to the POST-resolved server when provided, else the GET-query
        # server on a page render — without the rebind a non-default-server tab reads the default
        # cache and renders empty after a successful refresh). Mirrors modules_view.get_context_data.
        if server_key is None:
            # GET render: rebind + scope the VLAN-cache read to ?server_key (shared helper) so a
            # non-default-server tab reads that server's cache, not the default's.
            server_key, unresolved = self.resolve_get_render_server_key(request)
            if unresolved:
                # The query named a server that no longer resolves (deleted/misconfigured). Its
                # VLAN snapshot may still be cached until TTL — render empty scoped to the
                # requested key instead of serving a removed server's VLANs as live data
                # (mirrors the interfaces/modules/cables tabs' unresolved guards).
                return {
                    "object": obj,
                    "vlan_table": None,
                    "vlan_groups": self.get_vlan_groups_for_device(obj),
                    "last_fetched": None,
                    "cache_expiry": None,
                    "server_key": server_key,
                }
            # No buildable client → no valid server scope: degrade to None (empty table) instead of
            # the "default" placeholder resolve_get_render_server_key falls back to, mirroring the
            # sibling tabs' _render_server_key() None fallback (develop hardening).
            if self._render_server_key() is None:
                server_key = None
        # Honour the POST-resolved server when provided; otherwise use the shared degrading resolver
        # (not a bare getattr on the lazy librenms_api property): on a missing/misconfigured default
        # the property raises KeyError/ValueError, which would 500 the VLAN tab on GET.
        server_key = server_key or self._render_server_key()
        cached_vlans = cache.get(self.get_cache_key(obj, "vlans", server_key))
        last_fetched = cache.get(self.get_last_fetched_key(obj, "vlans", server_key))

        # Fail closed on a stale/corrupt cached entry. compare_vlans() iterates the rows and
        # calls vlan.get(...), so a malformed snapshot (string, list of scalars) would 500 the
        # VLAN tab on render. _fetch_and_cache_vlan_data() guards the write side, but a pre-fix
        # entry can still be in the cache — drop and purge it, then render empty (mirrors the
        # interfaces read-path guard). An empty list is valid (device with no VLANs).
        if cached_vlans is not None and not is_list_of_dicts(cached_vlans):
            cache.delete(self.get_cache_key(obj, "vlans", server_key))
            cache.delete(self.get_last_fetched_key(obj, "vlans", server_key))
            cached_vlans = None
            last_fetched = None

        # Get available VLAN groups for this device
        vlan_groups = self.get_vlan_groups_for_device(obj)

        # Build lookup maps for VLAN matching
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)

        # `is not None` (not a bare truthiness check): an empty list is a valid successful refresh
        # (a device with no VLANs) and must still render an empty table — a truthy check would skip
        # construction and make a VLAN-less device look like it never loaded.
        if cached_vlans is not None:
            # Compare VLANs with NetBox (against all device-available VLANs)
            compared_vlans = self.compare_vlans(cached_vlans, lookup_maps, device=obj)

            vlan_table = LibreNMSVLANTable(compared_vlans, vlan_groups=vlan_groups)
            vlan_table.configure(request)

        # Calculate cache TTL
        cache_ttl = cache_remaining_ttl(cache, self.get_cache_key(obj, "vlans", server_key))
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
        """
        Build the template context for the VLAN error fragment.

        Keep the fragment's ``server_key`` on the POST-resolved server so a retry/action after an
        error targets the same scope. An explicit ``server_key=None`` (the stale-server branch) is
        preserved as-is — using ``or`` here would treat it as falsey and silently fall back to the
        session server, re-rendering the fragment on the wrong (still-configured) server. Only an
        *omitted* server_key falls back.

        Args:
            obj (Device | VirtualMachine): The object being synced.
            error_message (str): The error message to display in the fragment.
            server_key: POST-resolved server key; the ``_SERVER_KEY_UNSET`` sentinel means "omitted"
                and falls back to the session server (an explicit None is preserved as-is).

        Returns:
            dict: The render context for the VLAN error fragment.
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
