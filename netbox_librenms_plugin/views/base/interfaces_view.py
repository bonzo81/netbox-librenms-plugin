import logging
import re

from django.contrib import messages
from django.core.cache import cache
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.interface_relationships import (
    RelationshipResolutionContext,
    build_relationship_maps,
    filter_interface_index,
    resolve_relationship_row,
)
from netbox_librenms_plugin.utils import (
    build_migrated_context,
    cache_remaining_ttl,
    coerce_librenms_id,
    get_interface_name_field,
    get_librenms_oob,
    get_librenms_sync_device,
    get_interface_port_identity_sets,
    is_list_of_dicts,
    is_valid_ports_payload,
    normalize_librenms_port_id,
    resolve_interface_row_device,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    VlanAssignmentMixin,
    redirect_with_server_key,
)

logger = logging.getLogger(__name__)


class BaseInterfaceTableView(
    VlanAssignmentMixin, LibreNMSAPIMixin, LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View
):
    """
    Base view for fetching interface data from LibreNMS and generating table data.
    Includes VLAN enrichment for interface VLAN sync functionality.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_interface_sync_content.html"
    interface_name_field = None

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine) the user may view."""
        # The plugin gate is model-level only, so scope the lookup or any pk is reachable.
        return self.restrict_object_or_404(self.model, pk=pk)

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
        """
        Redirect to the sync tab after a refresh failure, preserving the POST-scoped server_key.

        Preserving server_key keeps the user on the server they were working in (otherwise the next
        retry/sync can target the session/default LibreNMS instance instead).

        server_key is POST-derived, so the candidate URL is gated by Django's
        ``url_has_allowed_host_and_scheme`` (sink inside the validated branch) — the open-redirect
        barrier CodeQL recognises for py/url-redirection (CWE-601). The bare ``url`` fallback is a
        pure ``reverse()`` path with no user input.

        Args:
            request (HttpRequest): Current request, used for the host-allowlist check.
            obj (Device | VirtualMachine): Object whose sync tab to return to.
            server_key (str | None): POST-scoped server key to carry on the redirect URL.

        Returns:
            HttpResponseRedirect: Redirect to the sync tab (with server_key when it validates).
        """
        url = self.get_redirect_url(obj)
        return redirect_with_server_key(request, url, server_key)

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
        """
        Build name and LibreNMS ID indexes, dropping conflicting IDs entirely.

        Also select the relationship fields used during enrichment. Device interfaces have
        ``lag`` and ``parent``. VM interfaces have ``parent``.

        Args:
            obj: The NetBox device (or VM) whose interfaces are indexed.

        Returns:
            dict: Name and LibreNMS ID indexes plus the number of interfaces carrying each ID.
                LibreNMS IDs that map to more than one interface are dropped from the direct index.
        """
        by_name = {}
        by_librenms_id = {}
        by_librenms_id_matches = {}
        librenms_id_counts = {}
        duplicate_librenms_ids = set()

        # Prefetch the M2M relations the table renderers dereference per matched row
        # (render_vlans -> tagged_vlans, render_mac_address -> mac_addresses); without this each
        # rendered interface row issues its own query for these. Also select_related the lag/parent
        # FKs that render_parent and render_lag dereference.
        related_field = self.get_select_related_field(obj)
        extra_related = ["parent"] if related_field == "virtual_machine" else ["lag", "parent"]
        interfaces = (
            self.get_interfaces(obj)
            .select_related(related_field, *extra_related)
            .prefetch_related("tagged_vlans", "tagged_vlans__group", "mac_addresses")
        )
        for interface in interfaces:
            by_name[interface.name] = interface
            librenms_id = self._get_object_librenms_id(interface)
            if librenms_id is None:
                continue
            librenms_id_counts[librenms_id] = librenms_id_counts.get(librenms_id, 0) + 1
            by_librenms_id_matches.setdefault(librenms_id, []).append(interface)
            if librenms_id in by_librenms_id:
                duplicate_librenms_ids.add(librenms_id)
                continue
            by_librenms_id[librenms_id] = interface

        for librenms_id in duplicate_librenms_ids:
            by_librenms_id.pop(librenms_id, None)

        return {
            "by_name": by_name,
            "by_librenms_id": by_librenms_id,
            "by_librenms_id_matches": by_librenms_id_matches,
            "librenms_id_counts": librenms_id_counts,
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
            # This POST is HTMX (the success path swaps in the partial), so a bare redirect would
            # swap a full page into the partial target AND drop the migrated-donor context —
            # re-enabling sync controls on a migrated donor. Render the partial with migrated
            # context resolved under the active session key (the POSTed key is now known-invalid),
            # mirroring the ip/cables/modules/vlan stale-key error paths.
            # rebind_api_for_server() returned None precisely to avoid constructing a missing/
            # misconfigured default client, so don't touch the lazy `librenms_api` property here —
            # it could raise and turn this HTMX error path back into a 500. Read the already-cached
            # client's key (else "default").
            active_server_key = self.active_server_key
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {
                    "interface_sync": {"object": obj, "table": None, "cache_expiry": None, "server_key": None},
                    "interface_name_field": interface_name_field,
                },
            )

        # Resolve the VC sync device once (the priority member for a VC, else obj) and use it
        # for the main librenms_id lookup too — not just the cache/OOB scope below. On VC-member
        # pages the active librenms_id can live on the priority member, so reading it from the
        # viewed obj would fetch/cache another member's data. Mirrors cables_view.
        _server_key = post_server_key
        lookup_device = get_librenms_sync_device(obj, server_key=_server_key) or obj

        # A refresh must actually refresh: drop the previous snapshot up front so that if
        # the fetch below fails we fall back to an empty view + visible error, rather than
        # silently serving stale data (and letting the follow-up sync run on it). The
        # success path re-populates the cache below. This must precede the missing-librenms_id
        # return too — otherwise a failed refresh on a previously-synced device leaves the old
        # ports snapshot in place for the redirected tab and downstream sync actions to consume.
        cache.delete(self.get_cache_key(lookup_device, "ports", _server_key))
        cache.delete(self.get_last_fetched_key(lookup_device, "ports", _server_key))

        # Resolve librenms_id (scoped to the POSTed server + resolved member) after the cache
        # is already invalidated, so the missing-id path can't leave stale interface data behind.
        # coerce_librenms_id fails closed on a bool/zero/negative/garbage custom-field value (a
        # stored ``True`` would otherwise become id ``1`` and fetch a stranger's ports).
        self.librenms_id = coerce_librenms_id(self.librenms_api.get_librenms_id(lookup_device))

        if self.librenms_id is None:
            messages.error(request, "Device not found in LibreNMS.")
            return self._failure_redirect(request, obj, _server_key)

        success, librenms_data = self.librenms_api.get_ports(self.librenms_id)

        if not success:
            messages.error(request, librenms_data)
            return self._failure_redirect(request, obj, _server_key)

        # get_ports is an external boundary: a truthy success doesn't guarantee a dict with a list
        # of dict port rows (the OOB branch below guards the same way). The old cache was already
        # deleted, so a malformed 200 must fail closed with a warning rather than 500 on .get()/
        # enrichment below.
        if not is_valid_ports_payload(librenms_data):
            messages.error(request, "Unexpected response from LibreNMS (malformed ports payload).")
            return self._failure_redirect(request, obj, _server_key)

        # A success=True response can still carry a malformed-but-truthy body (string/list/
        # dict-with-non-list "ports"); dereferencing it would 500 the refresh. Fail closed like
        # the not-success path instead. An empty ports list stays valid.
        ports = librenms_data.get("ports", []) if isinstance(librenms_data, dict) else None
        if not is_list_of_dicts(ports):
            messages.error(request, "Unexpected response from LibreNMS (malformed ports payload).")
            return self._failure_redirect(request, obj, _server_key)

        # Enrich ports with VLAN data for trunk ports
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
        # Coerce the OOB controller id the same way the host id is coerced above: a non-numeric /
        # bool / zero / negative stored id must fail closed (oob_id=None → skip the OOB fetch),
        # never build a GET /devices/<garbage>/ports that 404s and silently drops OOB ports.
        oob_id = coerce_librenms_id(oob.get("id")) if oob else None
        oob_ports_failed = False
        if oob and oob_id is None:
            # An OOB controller IS linked, but its stored id is corrupt (non-numeric / bool / zero /
            # negative). Silently skipping here would cache a host-only snapshot that looks COMPLETE,
            # so the OOB rows / shared-LOM markers would vanish with no banner. Fail closed onto the
            # same partial-outcome path as a fetch failure: log the corrupt custom-field state, warn
            # the user, and tag the snapshot oob_incomplete below.
            logger.warning(
                "Invalid OOB controller id for device %s: %r",
                self.librenms_id,
                oob.get("id"),
            )
            messages.warning(
                request,
                "Interfaces refreshed, but OOB controller ports fetch failed; "
                "showing host interfaces only. See server logs for details.",
            )
            oob_ports_failed = True
        elif oob_id:
            oob_success, oob_raw = self.librenms_api.get_ports(oob_id)
            # Treat a malformed-but-truthy OOB payload the same as oob_success=False so the
            # host-only warning path runs instead of 500-ing on .get()/_enrich below. get_ports
            # is an external boundary: success does not guarantee a dict with a list of dict rows.
            if oob_success and is_valid_ports_payload(oob_raw):
                oob_ports = oob_raw.get("ports", [])
                oob_enriched = self._enrich_ports_with_vlan_data(oob_ports, interface_name_field)
                for port in oob_enriched:
                    port["_source"] = "oob"

                # Detect shared-LOM: same MAC seen on BOTH main and OOB sides.
                # Build separate per-source MAC sets so that within-source
                # duplicates are not falsely flagged as cross-source conflicts.
                def _normalized_mac(port):
                    # The ports payload is validated only as a list of dicts, so a malformed
                    # truthy ifPhysAddress (int/list) would 500 on .lower(). Treat any
                    # non-string MAC as absent rather than crashing the refresh.
                    mac = port.get("ifPhysAddress")
                    if not isinstance(mac, str):
                        return ""
                    # Strip to bare hex so colon/hyphen formatting differences don't matter, then
                    # drop placeholders: an all-zero (or non-12-digit) MAC is not a real address,
                    # so shared 00:00:00:00:00:00 fillers on both sides must not flag a shared-LOM
                    # conflict on otherwise-unrelated ports.
                    normalized = "".join(ch for ch in mac.lower() if ch in "0123456789abcdef")
                    if len(normalized) != 12 or normalized == "0" * 12:
                        return ""
                    return normalized

                # Normalize each port's MAC exactly once and carry it alongside the port, so the
                # set-building and the conflict-tagging pass below reuse it instead of re-deriving
                # the same value 2-3x per port on a high-port-count chassis.
                main_pairs = [(port, _normalized_mac(port)) for port in enriched_ports]
                oob_pairs = [(port, _normalized_mac(port)) for port in oob_enriched]
                main_macs: set[str] = {mac for _port, mac in main_pairs if mac}
                oob_macs: set[str] = {mac for _port, mac in oob_pairs if mac}
                shared_macs = main_macs & oob_macs
                if shared_macs:
                    for port, mac in main_pairs + oob_pairs:
                        if mac and mac in shared_macs:
                            port["_dedup_conflict"] = True
                librenms_data["ports"] = enriched_ports + oob_enriched
            else:
                # Surface the failure: silently caching main-only data under a
                # success banner would make OOB rows / shared-LOM markers vanish
                # with no indication. Main interfaces still render.
                logger.warning(
                    "OOB ports fetch failed for device %s (OOB id %s): %s",
                    self.librenms_id,
                    oob_id,
                    oob_raw,
                )
                messages.warning(
                    request,
                    "Interfaces refreshed, but OOB controller ports fetch failed; "
                    "showing host interfaces only. See server logs for details.",
                )
                oob_ports_failed = True
        # Lazy port_stack fetch — only when device has LAG or sub-interface relationships.
        # Enriches the host ports we fetched regardless of OOB outcome (it's independent of
        # the OOB controller); the oob_incomplete tagging below still applies on an OOB failure.
        # get_port_stack() is scoped to the main device, so LAG/sub-interface inference must
        # only consider host ports. An OOB-only row matching the ifType/name heuristic would
        # otherwise trigger a host port_stack fetch (and the "may be incomplete" warning) even
        # when the main device has no such relationships.
        host_ports_final = [p for p in librenms_data.get("ports", []) if p.get("_source") != "oob"]
        self._enrich_port_stack_relationships(
            request,
            librenms_data,
            host_ports_final,
            interface_name_field,
        )

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
            sync_device=lookup_device,
        )
        context = {"interface_sync": context}
        context["interface_name_field"] = interface_name_field
        # render_sync_partial injects the migrated-donor flags (hidden sync button + Migrate
        # column) so the HTMX tab refresh stays consistent with the full page.
        return self.render_sync_partial(request, obj, _server_key, context)

    def _enrich_port_stack_relationships(self, request, librenms_data, host_ports, interface_name_field):
        """Fetch and resolve host port relationships when the snapshot has a relevant signal."""
        from netbox_librenms_plugin.models import PortStackLagPattern

        structural_signal = self._has_structural_relationship_signals(host_ports, interface_name_field)
        unscoped_patterns = PortStackLagPattern.compiled_patterns_for_os(None)
        name_signal = self._has_lag_name_signals(host_ports, interface_name_field, unscoped_patterns)

        device_os = ""
        device_os_known = False
        scoped_patterns = []
        if name_signal:
            info_success, device_info = self.librenms_api.get_device_info(self.librenms_id)
            if info_success and isinstance(device_info, dict):
                raw_device_os = device_info.get("os")
                if isinstance(raw_device_os, str) and raw_device_os.strip():
                    device_os = raw_device_os.strip()
                    device_os_known = True
            scoped_patterns = PortStackLagPattern.compiled_patterns_for_os(device_os)

        scoped_name_signal = self._has_lag_name_signals(
            host_ports,
            interface_name_field,
            scoped_patterns,
        )
        relationship_fetch_failed = False
        if structural_signal or scoped_name_signal:
            ps_success, ps_data = self.librenms_api.get_port_stack(self.librenms_id)
            if ps_success:
                librenms_data["port_stack_relationships"] = self.librenms_api.resolve_port_relationships(
                    host_ports,
                    ps_data,
                    device_os=device_os,
                    interface_name_field=interface_name_field,
                    compiled_lag_patterns=scoped_patterns,
                )
            else:
                relationship_fetch_failed = True
                logger.warning("port_stack fetch failed for device %s: %s", self.librenms_id, ps_data)
                librenms_data["relationship_data_incomplete"] = True
                messages.warning(
                    request,
                    "Interfaces refreshed, but LAG/sub-interface relationship data could not be "
                    "fetched from LibreNMS; the Parent / LAG column may be incomplete. "
                    "See server logs for details.",
                )

        # A structural signal still gives a useful partial snapshot when the OS lookup fails.
        # Mark it incomplete because OS-scoped name patterns could describe additional edges.
        if name_signal and not device_os_known and not relationship_fetch_failed:
            logger.warning("Could not determine the LibreNMS device OS for device %s", self.librenms_id)
            librenms_data["relationship_data_incomplete"] = True
            messages.warning(
                request,
                "Interfaces refreshed, but the device OS could not be determined from LibreNMS. "
                "The Parent / LAG column may be incomplete. See server logs for details.",
            )

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

    def get_context_data(self, request, obj, interface_name_field, server_key=None, fresh_data=None, sync_device=None):
        """
        Build the context data for the interface sync view.

        ``fresh_data`` is a render-from-snapshot escape hatch: when given, the view
        renders from that in-memory dict instead of reading the ports cache (for a
        caller that built a response without a preceding cache write). No in-tree
        caller currently passes it.

        The OOB-ports-fetch-failure path does *not* use it: ``post()`` caches the
        host-only snapshot tagged ``oob_incomplete=True`` and renders from the cache
        like the normal flow; an inline banner (driven by that tag) surfaces the
        missing OOB rows.

        Args:
            request: The current HTTP request.
            obj: The NetBox device (or VC member) being synced.
            interface_name_field: The interface name field preference; resolved from
                the request when None.
            server_key: The LibreNMS server key; resolved from the API client when
                None.
            fresh_data: Optional in-memory ports snapshot to render from instead of
                the cache.

        Returns:
            dict: The template context (object, table, vlan_groups, server_key,
                oob_incomplete and related render state).
        """
        ports_data = []
        table = None
        netbox_only_interfaces = []

        if interface_name_field is None:
            interface_name_field = get_interface_name_field(request)

        unresolved = False
        if server_key is None:
            # GET render (no POST-resolved key threaded in): rebind + scope the ports-cache read
            # to ?server_key (shared helper) so a non-default-server tab reads that server's
            # cache, not the default's. An unresolved non-blank key scopes to that key (miss).
            server_key, unresolved = self.resolve_get_render_server_key(request)

        # Scope the ports cache to the VC sync device (not the viewed member) so all VC
        # members share one entry instead of fragmenting / re-fetching per member. Mirrors
        # cables_view; resolves to obj itself for non-VC devices. Must match post()'s key.
        # Reuse the device post() already resolved (passed as sync_device) to avoid a second
        # get_librenms_sync_device() VC-members query per refresh; the GET path passes None.
        cache_device = sync_device or get_librenms_sync_device(obj, server_key=server_key) or obj

        if fresh_data is not None:
            cached_data = fresh_data
            last_fetched = timezone.now()
        elif unresolved:
            # ?server_key named a server that no longer resolves (deleted/misconfigured). Its ports
            # snapshot may still be cached, but the failed rebind left self.librenms_api bound to the
            # DEFAULT server, so the per-interface librenms_id index (_get_object_librenms_id) is
            # keyed on a different server than this cache read would be — mismatching an
            # already-synced port as a new "NetBox only" row. Render empty, scoped to the requested
            # key, instead (mirrors modules_view.get_context_data's unresolved guard).
            cached_data = None
            last_fetched = None
        else:
            cached_data = cache.get(self.get_cache_key(cache_device, "ports", server_key))
            last_fetched = cache.get(self.get_last_fetched_key(cache_device, "ports", server_key))

        # Fail closed on a stale/corrupt cache entry: the enrichment below assumes a dict with a
        # list of dict ports (same contract post() validates before caching), so a malformed
        # snapshot would 500 the sync tab before the user could refresh. Purge it so a later render
        # re-fetches. A dict envelope with a malformed "ports" is kept so the isinstance(dict) block
        # below degrades ports_data to [] and still builds an empty table. A non-dict snapshot
        # has no envelope to render and drops to None.
        if cached_data is not None and not is_valid_ports_payload(cached_data):
            if fresh_data is None:
                cache.delete(self.get_cache_key(cache_device, "ports", server_key))
                cache.delete(self.get_last_fetched_key(cache_device, "ports", server_key))
            if not isinstance(cached_data, dict):
                cached_data = None
            last_fetched = None

        # A snapshot tagged oob_incomplete is host-only because the linked OOB controller's
        # ports could not be fetched on the last refresh. Surface it on every render of that
        # snapshot (via an inline banner) so the missing OOB rows are never silently absent.
        oob_incomplete = bool(cached_data.get("oob_incomplete")) if isinstance(cached_data, dict) else False

        # The refresh path tags a cached snapshot when its port_stack request fails. Keep that
        # warning attached to the snapshot so later cached renders do not silently show an
        # incomplete Parent / LAG column after the one-shot Django message has disappeared.
        relationship_data_incomplete = (
            bool(cached_data.get("relationship_data_incomplete")) if isinstance(cached_data, dict) else False
        )

        virtual_chassis_members = []
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            virtual_chassis_members = list(obj.virtual_chassis.members.all())

        # Include every member's scope so rows owned by another member can resolve their VLANs.
        vlan_scope_devices = virtual_chassis_members or [obj]
        vlan_groups = self.get_vlan_groups_for_devices(vlan_scope_devices)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)
        vlan_groups_by_device = {
            device.pk: self.filter_vlan_groups_for_device(vlan_groups, device) for device in vlan_scope_devices
        }
        vlan_lookup_maps_by_device = {
            device_id: self.restrict_vlan_lookup_maps(lookup_maps, device_vlan_groups)
            for device_id, device_vlan_groups in vlan_groups_by_device.items()
        }

        # Load any user VLAN group overrides from cache (set by "apply to all")
        # Read overrides under the same VC-scoped key SaveVlanGroupOverridesView writes
        # (the sync device, not the viewed member) — otherwise an "apply to all" made on a VC
        # member is lost on the next render/member switch. cache_device mirrors that writer.
        vlan_group_overrides = cache.get(self.get_vlan_overrides_key(cache_device, server_key)) or {}

        # isinstance(dict) guard (mirroring the IP/VLAN/Cables/Modules render paths and the
        # interfaces SYNC path): a truthy-but-malformed cache entry — a legacy/older-shape snapshot
        # or corrupt value — must degrade to an empty table, not AttributeError-500 on .get("ports").
        if isinstance(cached_data, dict):
            ports_data = cached_data.get("ports", [])
            # The isinstance(dict) guard above only proves the ENVELOPE is a dict; "ports" itself can
            # still be a present-but-null value or a list with non-dict items (legacy/corrupt snapshot).
            # `for port in ports_data` would then TypeError on None, and `port.get(...)`/`port["enabled"] = `
            # would AttributeError/TypeError on a non-dict item — 500ing the cached render. Reuse the
            # shared is_list_of_dicts guard (as the SYNC path does) and degrade to an empty table instead.
            if not is_list_of_dicts(ports_data):
                ports_data = []
            matched_interface_ids = set()

            relationship_maps = build_relationship_maps(cached_data)
            unique_host_port_ids, unambiguous_name_port_ids = get_interface_port_identity_sets(
                ports_data, interface_name_field
            )

            # Pre-fetch all interfaces for all potential chassis members
            # (_build_interface_lookup_maps select_relateds lag/parent for devices). Materialise the
            # VC members once and index them by vc_position and id so the per-port member resolution
            # and the netbox-only pass below reuse them instead of issuing a members.get(...) query
            # per port / per netbox-only interface (N+1).
            interfaces_by_device = {}
            interfaces_by_port_id = {}
            catalog_interfaces_by_port_id = {}
            catalog_interfaces_by_name = {}
            members_by_position = None
            members_by_id = None
            if virtual_chassis_members:
                members_by_position = {member.vc_position: member for member in virtual_chassis_members}
                members_by_id = {member.id: member for member in virtual_chassis_members}
                for member in virtual_chassis_members:
                    interfaces_by_device[member.id] = self._build_interface_lookup_maps(member)
            else:
                interfaces_by_device[obj.id] = self._build_interface_lookup_maps(obj)

            for interface_maps in interfaces_by_device.values():
                for port_id, interface in interface_maps["by_librenms_id"].items():
                    interfaces_by_port_id.setdefault(port_id, []).append(interface)
                for port_id, interfaces in interface_maps["by_librenms_id_matches"].items():
                    catalog_interfaces_by_port_id.setdefault(port_id, []).extend(interfaces)
                for interface_name, interface in interface_maps["by_name"].items():
                    catalog_interfaces_by_name.setdefault(interface_name, []).append(interface)

            interface_model = self.get_interfaces(obj).model
            owner_field = self.get_select_related_field(obj)
            actionable_owner_ids = set(
                obj.__class__.objects.restrict(request.user, "view")
                .filter(pk__in=interfaces_by_device)
                .values_list("pk", flat=True)
            )
            scoped_interfaces = interface_model.objects.filter(**{f"{owner_field}_id__in": interfaces_by_device})
            viewable_interface_ids = set(scoped_interfaces.restrict(request.user, "view").values_list("pk", flat=True))
            changeable_interface_ids = set(
                scoped_interfaces.restrict(request.user, "change").values_list("pk", flat=True)
            )
            related_interface_ids = viewable_interface_ids | changeable_interface_ids
            can_write_relationships = self.has_write_permission()
            related_interfaces_by_port_id = {}
            related_interfaces_by_name = {}
            for interface_maps in interfaces_by_device.values():
                for port_id, interfaces in interface_maps["by_librenms_id_matches"].items():
                    permitted = [
                        interface
                        for interface in interfaces
                        if interface.pk in related_interface_ids
                        and getattr(interface, f"{owner_field}_id") in actionable_owner_ids
                    ]
                    if permitted:
                        related_interfaces_by_port_id.setdefault(port_id, []).extend(permitted)
                for interface_name, interface in interface_maps["by_name"].items():
                    if (
                        interface.pk in related_interface_ids
                        and getattr(interface, f"{owner_field}_id") in actionable_owner_ids
                    ):
                        related_interfaces_by_name.setdefault(interface_name, []).append(interface)

            catalog_index = {
                "by_lnms_id": catalog_interfaces_by_port_id,
                "by_name": catalog_interfaces_by_name,
            }
            display_index = catalog_index
            related_index = {
                "by_lnms_id": related_interfaces_by_port_id,
                "by_name": related_interfaces_by_name,
            }
            relationship_context = RelationshipResolutionContext(
                obj=obj,
                server_key=server_key,
                catalog_index=catalog_index,
                display_index=display_index,
                related_index=related_index,
                source_index=filter_interface_index(related_index, changeable_interface_ids),
                actionable_owner_ids=actionable_owner_ids,
                changeable_interface_ids=changeable_interface_ids,
                can_write=can_write_relationships,
            )

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
                    chassis_member = resolve_interface_row_device(
                        obj,
                        port,
                        interface_name_field,
                        interfaces_by_port_id=interfaces_by_port_id,
                        members_by_position=members_by_position,
                        members_by_id=members_by_id,
                    )
                else:
                    chassis_member = obj

                netbox_interface = resolve_relationship_row(
                    relationship_context,
                    port,
                    chassis_member,
                    interface_name_field,
                    unique_host_port_ids,
                    unambiguous_name_port_ids,
                    relationship_maps,
                )
                port_id = normalize_librenms_port_id(port.get("port_id"))
                port["selected_object_id"] = chassis_member.pk
                port["sync_target_resolvable"] = (
                    chassis_member.pk in actionable_owner_ids and port_id in unique_host_port_ids
                )
                # A bound interface is now always a genuine host match (OOB rows resolved to None
                # above), so it correctly counts toward the matched set used for netbox-only detection
                # without an OOB row ever hiding a same-named host interface.
                if netbox_interface is not None:
                    matched_interface_ids.add(netbox_interface.id)

                if port.get("ifAlias") in (port.get("ifDescr"), port.get("ifName")):
                    port["ifAlias"] = ""

                # Add VLAN group auto-selection data to port, applying any user overrides
                if chassis_member.pk in actionable_owner_ids:
                    row_vlan_groups = vlan_groups_by_device.get(chassis_member.pk, [])
                    row_lookup_maps = vlan_lookup_maps_by_device.get(chassis_member.pk, {})
                else:
                    row_vlan_groups = []
                    row_lookup_maps = {}
                port["vlan_groups"] = row_vlan_groups
                self._add_vlan_group_selection(port, row_lookup_maps, chassis_member, vlan_group_overrides)

                # Add missing VLANs info for warning display
                self._add_missing_vlans_info(port, row_lookup_maps)

            table = self.get_table(ports_data, obj, interface_name_field, vlan_groups=vlan_groups)
            table.allowed_vc_member_ids = actionable_owner_ids
            # Propagate donor "migrated mode" so the table suppresses per-row LAG/parent sync
            # buttons (the bulk form is already hidden by the template; the row buttons POST
            # directly, so they must be stripped here too to keep a migrated donor read-only).
            table.migrated_to_marker = bool(build_migrated_context(obj, server_key).get("migrated_to_marker"))
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
                        # Get device name for the interface (reuse the pre-indexed members — the
                        # device_id keys come from interfaces_by_device, which was built from them —
                        # instead of a members.get(id=...) query per netbox-only interface).
                        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                            device = members_by_id.get(device_id) if members_by_id else None
                            device_name = device.name if device else obj.name
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

        # ``ttl()`` is Redis-specific; the Django cache API doesn't guarantee it. Guard with
        # getattr so non-Redis backends return None instead of raising AttributeError at render
        # (mirrors ip_addresses_view / modules_view).
        # On an unresolved key the render is empty (above); don't surface the stale server's TTL as
        # a misleading "cached until" on an empty table.
        cache_ttl = (
            None if unresolved else cache_remaining_ttl(cache, self.get_cache_key(cache_device, "ports", server_key))
        )
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
            "relationship_data_incomplete": relationship_data_incomplete,
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
                        try:
                            group = override_groups_by_id.get(int(override_group_id))
                        except (TypeError, ValueError):
                            group = None
                        # The row's in-scope groups, not only groups that already carry the VID:
                        # "apply to all" exists to put the VLAN into a group that lacks it.
                        allowed_group_ids = {candidate.pk for candidate in port.get("vlan_groups", [])}
                        if group and group.pk in allowed_group_ids:
                            vlan_group_map[vid] = {
                                "group_id": str(group.pk),
                                "group_name": group.name,
                                "is_ambiguous": False,
                            }
                        # Keep auto-selection when the group was deleted or is out of the row's scope.
                    elif (vid, None) in lookup_maps.get("vid_group_to_vlan", {}):
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

    @staticmethod
    def _relationship_port_names(ports, interface_name_field):
        """Return each port's distinct string names from the active and canonical fields."""
        name_fields = {"ifName", "ifDescr", interface_name_field}
        return [[name for field in name_fields if isinstance(name := port.get(field), str) and name] for port in ports]

    def _has_structural_relationship_signals(self, ports, interface_name_field="ifName"):
        """Return true for an explicit LAG type or a child name whose parent also exists."""
        names_per_port = self._relationship_port_names(ports, interface_name_field)
        port_names = {name for names in names_per_port for name in names}
        sub_iface_re = re.compile(r"^(.+)\.\d+$")
        return any(
            port.get("ifType", "") == "ieee8023adLag"
            or any((match := sub_iface_re.match(name)) and match.group(1) in port_names for name in names)
            for port, names in zip(ports, names_per_port)
        )

    def _has_lag_name_signals(self, ports, interface_name_field, lag_patterns):
        """Return true when an interface name matches one of the supplied OS-scoped patterns."""
        names_per_port = self._relationship_port_names(ports, interface_name_field)
        return any(pat.search(name) for names in names_per_port for pat in lag_patterns for name in names)
