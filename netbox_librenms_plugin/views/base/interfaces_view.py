import logging
import re

from django.contrib import messages
from django.core.cache import cache
from django.utils import timezone
from django.views import View

from netbox_librenms_plugin.utils import (
    build_migrated_context,
    cache_remaining_ttl,
    coerce_librenms_id,
    get_interface_name_field,
    get_librenms_device_id,
    get_librenms_oob,
    get_librenms_sync_device,
    get_virtual_chassis_member,
    is_list_of_dicts,
    is_valid_ports_payload,
    normalize_librenms_port_id,
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

        For device interfaces (not VMs), also select_related the lag/parent FKs so
        _enrich_port_with_lag_parent() doesn't issue N+1 queries when reading
        nb_iface.lag / nb_iface.parent for each port. VMInterface has no such FKs.

        Args:
            obj: The NetBox device (or VM) whose interfaces are indexed.

        Returns:
            dict: ``{"by_name": {name: interface}, "by_librenms_id": {id: interface}}``;
                LibreNMS ids that map to more than one interface are dropped entirely.
        """
        by_name = {}
        by_librenms_id = {}
        duplicate_librenms_ids = set()

        # Prefetch the M2M relations the table renderers dereference per matched row
        # (render_vlans -> tagged_vlans, render_mac_address -> mac_addresses); without this each
        # rendered interface row issues its own query for these. Also select_related the lag/parent
        # FKs (render_parent dereferences them) — skipped for VMs, which have no such fields.
        related_field = self.get_select_related_field(obj)
        extra_related = [] if related_field == "virtual_machine" else ["lag", "parent"]
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

    @staticmethod
    def _build_relationship_maps(cached_data):
        """
        Build the normalized LAG/sub-interface maps and host-scoped port index from a cached snapshot.

        Returns ``(lag_members, sub_interfaces, by_port_id)``. Shared by get_context_data
        and SingleInterfaceVerifyView so the corruption guard, the key normalization, and
        the host-scoping live in exactly one place instead of being re-derived per call site.

        Fail-soft against a corrupt / partial-write / format-migrated cache: a None or
        non-dict ``port_stack_relationships`` (or a present-but-None nested ``lag_members`` /
        ``sub_interfaces``) collapses to ``{}`` — the ``.get(key, {})`` default only fills a
        *missing* key, so a present-but-None value would otherwise AttributeError on
        ``.items()``. Keys are normalized via ``normalize_librenms_port_id`` so the int-keyed
        lookups in ``_enrich_port_with_lag_parent`` never miss a stringified cache key, and
        ``by_port_id`` is scoped to host (``_source != "oob"``) rows so an OOB controller
        reusing a host port_id can't attach the wrong aggregate/parent during enrichment.

        Args:
            cached_data (dict): The cached LibreNMS snapshot (ports + port_stack_relationships).

        Returns:
            tuple: ``(lag_members, sub_interfaces, by_port_id)`` — all normalized-int keyed.
        """
        relationships = cached_data.get("port_stack_relationships") or {}
        if not isinstance(relationships, dict):
            relationships = {}
        lag_members_raw = relationships.get("lag_members") or {}
        if not isinstance(lag_members_raw, dict):
            lag_members_raw = {}
        sub_interfaces_raw = relationships.get("sub_interfaces") or {}
        if not isinstance(sub_interfaces_raw, dict):
            sub_interfaces_raw = {}
        lag_members = {normalize_librenms_port_id(k): v for k, v in lag_members_raw.items()}
        sub_interfaces = {normalize_librenms_port_id(k): v for k, v in sub_interfaces_raw.items()}
        by_port_id = {
            normalize_librenms_port_id(p.get("port_id")): p
            for p in cached_data.get("ports", [])
            if normalize_librenms_port_id(p.get("port_id")) is not None and p.get("_source") != "oob"
        }
        return lag_members, sub_interfaces, by_port_id

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
        # the not-success path instead (issue #100). An empty ports list stays valid.
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
        if self._has_lag_signals(host_ports_final, interface_name_field):
            # Cheap unscoped pre-check passed; resolve the device OS and re-check the signal
            # OS-scoped before paying for the port_stack fetch. A port name matching only
            # another platform's LAG regex is a false positive (resolve_port_relationships is
            # OS-scoped too, so it would resolve nothing), so skip the fetch entirely rather than
            # fetch it and render an empty Parent/LAG column. Best-effort: on a failed/odd
            # device-info fetch device_os stays None and the re-check falls back to all patterns.
            device_os = None
            info_success, device_info = self.librenms_api.get_device_info(self.librenms_id)
            if info_success and isinstance(device_info, dict):
                device_os = device_info.get("os")
            # Load + compile the OS-scoped patterns ONCE and share them with both the scoped signal
            # re-check and resolve_port_relationships, instead of each re-querying + re-compiling.
            from netbox_librenms_plugin.models import PortStackLagPattern

            scoped_patterns = PortStackLagPattern.compiled_patterns_for_os(device_os)
            if self._has_lag_signals(
                host_ports_final, interface_name_field, device_os=device_os, lag_patterns=scoped_patterns
            ):
                ps_success, ps_data = self.librenms_api.get_port_stack(self.librenms_id)
                if ps_success:
                    relationships = self.librenms_api.resolve_port_relationships(
                        host_ports_final,
                        ps_data,
                        device_os=device_os,
                        interface_name_field=interface_name_field,
                        compiled_lag_patterns=scoped_patterns,
                    )
                    librenms_data["port_stack_relationships"] = relationships
                else:
                    # The (OS-scoped) signal says this device has LAG/sub-interface relationships,
                    # so a failed fetch means the LAG/Parent column is silently incomplete. Warn
                    # (mirrors the OOB-incomplete handling) instead of dropping it without a trace,
                    # AND tag the snapshot so every later cached render surfaces the same warning —
                    # the transient Django message only reaches the user on this one refresh.
                    logger.warning("port_stack fetch failed for device %s: %s", self.librenms_id, ps_data)
                    librenms_data["relationship_data_incomplete"] = True
                    messages.warning(
                        request,
                        "Interfaces refreshed, but LAG/sub-interface relationship data could not be "
                        "fetched from LibreNMS; the Parent / LAG column may be incomplete. "
                        "See server logs for details.",
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
        # below degrades ports_data to [] and still builds an empty (but real) table (issue #100
        # site 4); a non-dict snapshot has no envelope to render and drops to None.
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

        # Get VLAN groups for dropdown
        vlan_groups = self.get_vlan_groups_for_device(obj)
        lookup_maps = self._build_vlan_lookup_maps(vlan_groups)

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

            # Build the normalized, host-scoped LAG/sub-interface maps from the cached snapshot.
            # _build_relationship_maps is the single home for the corruption guard + key
            # normalization + host-scoping, shared with SingleInterfaceVerifyView so the table
            # and the inline verify response can never diverge on how they read this cache.
            lag_members, sub_interfaces, by_port_id = self._build_relationship_maps(cached_data)

            # Pre-fetch all interfaces for all potential chassis members
            # (_build_interface_lookup_maps select_relateds lag/parent for devices). Materialise the
            # VC members once and index them by vc_position and id so the per-port member resolution
            # and the netbox-only pass below reuse them instead of issuing a members.get(...) query
            # per port / per netbox-only interface (N+1).
            interfaces_by_device = {}
            members_by_position = None
            members_by_id = None
            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                vc_members = list(obj.virtual_chassis.members.all())
                members_by_position = {member.vc_position: member for member in vc_members}
                members_by_id = {member.id: member for member in vc_members}
                for member in vc_members:
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
                    chassis_member = get_virtual_chassis_member(
                        obj, port.get(interface_name_field), members_by_position=members_by_position
                    )
                    device_interfaces = interfaces_by_device.get(
                        chassis_member.id if chassis_member else obj.id,
                        {"by_name": {}, "by_librenms_id": {}},
                    )
                else:
                    device_interfaces = interfaces_by_device.get(obj.id, {"by_name": {}, "by_librenms_id": {}})

                port_id = normalize_librenms_port_id(port.get("port_id"))
                # OOB-controller rows live on a SEPARATE LibreNMS device. Matching them against the
                # HOST device's interfaces (by port_id or name) would mislabel a shared-LOM OOB port
                # (e.g. both sides report "eth0"/"idrac0") as an in-sync host interface — rendering
                # its name green/"matched" and comparing its speed/MTU/MAC against an unrelated host
                # interface. The shared-name collision has its own signal (the "Shared LOM"
                # _dedup_conflict badge) and sync_selected_interfaces skips _source=="oob" rows, so
                # never bind an OOB row to a host interface: leave it unmatched (exists_in_netbox False).
                if port.get("_source") == "oob":
                    netbox_interface = None
                else:
                    netbox_interface = device_interfaces["by_librenms_id"].get(port_id) if port_id else None
                    if not netbox_interface:
                        netbox_interface = device_interfaces["by_name"].get(port.get(interface_name_field))
                port["exists_in_netbox"] = bool(netbox_interface)
                port["netbox_interface"] = netbox_interface
                # A bound interface is now always a genuine host match (OOB rows resolved to None
                # above), so it correctly counts toward the matched set used for netbox-only detection
                # without an OOB row ever hiding a same-named host interface.
                if netbox_interface is not None:
                    matched_interface_ids.add(netbox_interface.id)

                if port.get("ifAlias") in (port.get("ifDescr"), port.get("ifName")):
                    port["ifAlias"] = ""

                # Add VLAN group auto-selection data to port, applying any user overrides
                self._add_vlan_group_selection(port, lookup_maps, obj, vlan_group_overrides)

                # Add missing VLANs info for warning display
                self._add_missing_vlans_info(port, lookup_maps)

                # Enrich port with LAG/parent relationship context. Skip OOB rows: the
                # relationships come from the host device's port_stack, so applying them to
                # an OOB controller row (which can reuse a host port_id) would make the OOB
                # interface inherit the host's LAG/parent names and sync status.
                if port.get("_source") != "oob":
                    self._enrich_port_with_lag_parent(
                        port, lag_members, sub_interfaces, by_port_id, interface_name_field, server_key or ""
                    )

            table = self.get_table(ports_data, obj, interface_name_field, vlan_groups=vlan_groups)
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

        virtual_chassis_members = []
        if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
            virtual_chassis_members = obj.virtual_chassis.members.all()

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

    def _has_lag_signals(
        self, ports: list, interface_name_field: str = "ifName", device_os=None, lag_patterns=None
    ) -> bool:
        """
        Return True if any port appears to be a LAG interface or sub-interface.

        Used to trigger the lazy port_stack API fetch only when needed. Detects:

          - ifType == 'ieee8023adLag' (definitive)
          - Name matches a PortStackLagPattern regex (scoped to *device_os* — see below)
          - Any port name ends with '.<digits>' AND the base name also exists
            (sub-interface detection, e.g. ge-0/0/0.100 with ge-0/0/0 present)

        ``device_os`` scopes the name-pattern match to this device's OS, matching the resolver
        (resolve_port_relationships): a port name matching *another* platform's LAG regex is a
        false positive that would otherwise trigger the (wasted) port_stack fetch and leave an
        empty Parent/LAG column. The caller does a cheap unscoped pre-check (device_os None ->
        all patterns) and, only when that passes, fetches the OS and re-checks scoped before the
        port_stack fetch. The structural ieee8023adLag / sub-interface signals are OS-independent.

        'propVirtual' is deliberately NOT a signal: loopbacks, SVIs and tunnels are all
        propVirtual, so gating on it fired the extra get_port_stack()/get_device_info()
        round-trips for nearly every device. It also bought no real coverage — the resolver's
        _is_lag_aggregate() never treats propVirtual as an aggregate (only ieee8023adLag or a
        name-pattern match), so a propVirtual port-channel still has to match a configured
        PortStackLagPattern (e.g. the Po-channel regex) to be resolved, and that name-pattern
        check already gates it here.

        Name-based checks scan the user-selected ``interface_name_field`` as well as
        ifName/ifDescr: on an ifDescr-driven device the LAG/sub-interface name (which
        later enrichment renders and matches against) lives in ifDescr, so keying the
        signal off ifName alone would silently skip the port_stack fetch and leave the
        Parent/LAG column empty.

        Args:
            ports (list): The LibreNMS port dicts to scan.
            interface_name_field (str): The user-selected name field, scanned in
                addition to ifName/ifDescr.
            device_os (str | None): The device OS to scope the name-pattern match to.
            lag_patterns (list | None): Pre-compiled PortStackLagPattern list for *device_os*.
                Pass it when the caller already loaded the patterns (e.g. to share one load
                between the scoped pre-check and resolve_port_relationships) so this method does
                not re-query + re-compile them.

        Returns:
            bool: True if any port looks like a LAG aggregate or a sub-interface.
        """
        from netbox_librenms_plugin.models import PortStackLagPattern

        # OS-scoped compiled patterns (shared with the resolver via the model helper). Reuse the
        # caller's pre-loaded list when given so the DB read + regex compile happen once per refresh.
        if lag_patterns is None:
            lag_patterns = PortStackLagPattern.compiled_patterns_for_os(device_os)

        name_fields = {"ifName", "ifDescr", interface_name_field}

        def _names(port: dict) -> list:
            # Guard isinstance(str): a truthy non-string ifName/ifDescr (numeric/list from a
            # malformed payload) would otherwise reach pat.search()/sub_iface_re.match() below and
            # raise TypeError, 500-ing the whole refresh. Mirrors the resolver's _port_names, which
            # this method is documented to track.
            return [name for field in name_fields if isinstance(name := port.get(field), str) and name]

        # Compute each port's names once and reuse for both the base-name set and the per-port scan.
        names_per_port = [_names(p) for p in ports]
        port_names = {name for names in names_per_port for name in names}
        sub_iface_re = re.compile(r"^(.+)\.\d+$")

        for port, names in zip(ports, names_per_port):
            if port.get("ifType", "") == "ieee8023adLag":
                return True
            if any(pat.search(name) for pat in lag_patterns for name in names):
                return True
            # Sub-interface: a name ends with '.<digits>' and the parent name also present
            for name in names:
                m = sub_iface_re.match(name)
                if m and m.group(1) in port_names:
                    return True
        return False

    @staticmethod
    def _enrich_port_with_lag_parent(
        port: dict,
        port_id_to_lag: dict,
        port_id_to_parent: dict,
        by_id: dict,
        interface_name_field: str = "ifName",
        server_key: str = "",
    ) -> None:
        """
        Add LAG/parent context keys to a port dict in-place.

        Sets six keys on the port dict::

            port['librenms_lag_name']       -- LAG aggregate name in LibreNMS, or None
            port['librenms_lag_port_id']    -- LAG aggregate port_id, or None
            port['lag_sync_status']         -- match|mismatch|missing_nb|missing_lnms|None
            port['librenms_parent_name']    -- parent interface name, or None
            port['librenms_parent_port_id'] -- parent interface port_id, or None
            port['parent_sync_status']      -- same values as lag_sync_status

        Matching strategy (most-to-least reliable): stored librenms_id on the NetBox
        related interface equal to the LibreNMS port_id; then NetBox interface name
        equal to the LibreNMS ifName; then equal to ifDescr.

        Args:
            port (dict): The LibreNMS port dict to enrich (mutated in place).
            port_id_to_lag (dict): Map of port_id → LAG aggregate port_id.
            port_id_to_parent (dict): Map of port_id → parent port_id.
            by_id (dict): Map of port_id → LibreNMS port dict.
            interface_name_field (str): The name field used to render related names.
            server_key (str): The LibreNMS server key scoping stored-id lookups.

        Returns:
            None
        """
        # port_id_to_lag / port_id_to_parent arrive already key-normalized from the caller
        # (see the cached-data branch in get/post), so they're used directly here.
        port_id = normalize_librenms_port_id(port.get("port_id"))
        nb_iface = port.get("netbox_interface")

        def _related_iface_matches(nb_rel_iface, lnms_port_dict) -> bool:
            """Return True if nb_rel_iface corresponds to lnms_port_dict."""
            if nb_rel_iface is None or lnms_port_dict is None:
                return False
            # Primary: stored librenms_id (port_id) comparison — field-name-independent.
            # Default to "default" so the stable-id match is never silently disabled on the
            # default-server path (where server_key may arrive empty); name matching below is
            # the fragile fallback and must not pre-empt a stable id when one is stored.
            # Normalize both sides: get_librenms_device_id() can return a string-backed
            # custom-field value ("123"), so compare against the normalized port_id to avoid a
            # spurious "123" != 123 miss that would drop us to the fragile name fallback for a
            # renamed LAG/parent interface whose stable ids actually agree.
            stored_id = normalize_librenms_port_id(
                get_librenms_device_id(nb_rel_iface, server_key or "default", auto_save=False)
            )
            target = normalize_librenms_port_id(lnms_port_dict.get("port_id"))
            if stored_id is not None and target is not None:
                return stored_id == target
            # Fallback: name match — field-agnostic, INCLUDING the user-selected name field.
            # The displayed LAG/parent name above renders port.get(interface_name_field) and
            # _has_lag_signals scans it too, so on an ifAlias-driven device (NetBox names
            # synced from the selected field) an ifName/ifDescr-only compare would misreport
            # a genuine match as mismatch/missing_nb whenever no stored librenms_id exists.
            nb_name = nb_rel_iface.name
            return nb_name in (
                lnms_port_dict.get("ifName"),
                lnms_port_dict.get("ifDescr"),
                lnms_port_dict.get(interface_name_field),
            )

        # --- LAG ---
        lnms_lag_port_id = normalize_librenms_port_id(port_id_to_lag.get(port_id)) if port_id else None
        agg_port = by_id.get(lnms_lag_port_id) if lnms_lag_port_id else None
        lnms_lag_name = agg_port.get(interface_name_field) if agg_port else None

        port["librenms_lag_name"] = lnms_lag_name
        port["librenms_lag_port_id"] = lnms_lag_port_id

        nb_lag = getattr(nb_iface, "lag", None) if nb_iface else None
        if lnms_lag_port_id and nb_iface:
            if nb_lag and _related_iface_matches(nb_lag, agg_port):
                port["lag_sync_status"] = "match"
            elif nb_lag:
                port["lag_sync_status"] = "mismatch"
            else:
                port["lag_sync_status"] = "missing_nb"
        elif lnms_lag_port_id and not nb_iface:
            port["lag_sync_status"] = "missing_nb"
        elif not lnms_lag_port_id and nb_lag:
            port["lag_sync_status"] = "missing_lnms"
        else:
            port["lag_sync_status"] = None

        # --- Parent ---
        lnms_parent_port_id = normalize_librenms_port_id(port_id_to_parent.get(port_id)) if port_id else None
        parent_port = by_id.get(lnms_parent_port_id) if lnms_parent_port_id else None
        lnms_parent_name = parent_port.get(interface_name_field) if parent_port else None

        port["librenms_parent_name"] = lnms_parent_name
        port["librenms_parent_port_id"] = lnms_parent_port_id

        nb_parent = getattr(nb_iface, "parent", None) if nb_iface else None
        if lnms_parent_port_id and nb_iface:
            if nb_parent and _related_iface_matches(nb_parent, parent_port):
                port["parent_sync_status"] = "match"
            elif nb_parent:
                port["parent_sync_status"] = "mismatch"
            else:
                port["parent_sync_status"] = "missing_nb"
        elif lnms_parent_port_id and not nb_iface:
            port["parent_sync_status"] = "missing_nb"
        elif not lnms_parent_port_id and nb_parent:
            port["parent_sync_status"] = "missing_lnms"
        else:
            port["parent_sync_status"] = None
