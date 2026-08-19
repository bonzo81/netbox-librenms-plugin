import json
import logging
from collections import defaultdict
from ipaddress import ip_interface
from urllib.parse import quote_plus

from dcim.models import Device
from django.contrib import messages
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views import View
from ipam.models import VRF, IPAddress
from virtualization.models import VirtualMachine

from netbox_librenms_plugin.constants import is_supported_interface_name_field
from netbox_librenms_plugin.ip_addressing import parse_address_with_prefix, parse_librenms_ip_entry
from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable
from netbox_librenms_plugin.utils import (
    cache_remaining_ttl,
    coerce_librenms_id,
    get_interface_name_field,
    get_librenms_device_id,
    resolve_create_missing_interfaces,
    resolve_set_primary_ip,
    same_host,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

logger = logging.getLogger(__name__)


class BaseIPAddressTableView(LibreNMSPermissionMixin, LibreNMSAPIMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """
    Base view for synchronizing IP address information from LibreNMS.
    """

    partial_template_name = "netbox_librenms_plugin/_ipaddress_sync_content.html"
    interface_name_field = None

    def get_object(self, pk):
        return self.restrict_object_or_404(self.model, pk=pk)

    def get_ip_addresses(self, obj):
        """Fetch IP address data from LibreNMS for the given object."""
        # Coerce before the HTTP call: a poisoned cached id (e.g. True or a
        # non-numeric string) must fail closed here rather than reach the
        # LibreNMS device-ip endpoint, where it would build a malformed URL.
        self.librenms_id = coerce_librenms_id(self.librenms_api.get_librenms_id(obj))
        if self.librenms_id is None:
            return False, "Device not found in LibreNMS"
        return self.librenms_api.get_device_ips(self.librenms_id)

    def enrich_ip_data(self, ip_data, obj, interface_name_field, mgmt_ip="", server_key=None, port_data_cache=None):
        """
        Enrich IP data with NetBox information in a more efficient manner.

        This optimized implementation caches port data to reduce API calls,
        pre-loads all relevant device data, and uses dictionary lookups instead of
        repeated iterations.

        Args:
            ip_data: The LibreNMS IP rows to enrich.
            obj: The NetBox device the IPs belong to.
            interface_name_field: The interface name field preference used for port
                lookups.
            mgmt_ip (str): The device's LibreNMS management IP, resolved once on the
                fresh-fetch path and passed back in on cached renders so this method
                never makes a live LibreNMS API call.
            server_key: The LibreNMS server key scoping per-server interface matching.
            port_data_cache: Optional pre-populated port map (keyed by port_id) so
                cached renders avoid live ``get_port_by_id()`` calls.

        Returns:
            list: The enriched IP entries.
        """
        # Only an address LibreNMS reported can match a row, so resolve them first and scope
        # the NetBox IP scan to them instead of loading every IPAddress in the deployment.
        candidate_addresses = set()
        for ip_entry in ip_data:
            if not isinstance(ip_entry, dict) or "port_id" not in ip_entry:
                continue
            try:
                candidate_addresses.add(str(parse_librenms_ip_entry(ip_entry)))
            except ValueError:
                continue

        # Prefetch all necessary data (scoped to the POST-resolved server when provided
        # so interface librenms_id matching uses the right per-server mapping).
        prefetched_data = self._prefetch_netbox_data(obj, candidate_addresses, server_key=server_key)
        # LibreNMS port data, keyed by port_id. Callers pass a map pre-populated from the
        # cache on warm-cache renders so _get_port_info() reads it instead of making N
        # live get_port_by_id() calls (the cached pipeline must read cache + NetBox only).
        if port_data_cache is None:
            port_data_cache = {}

        enriched_data = []

        # Process each IP address from LibreNMS
        for ip_entry in ip_data:
            # Skip invalid entries that are not dictionaries
            if not isinstance(ip_entry, dict):
                continue

            # Skip entries missing required fields
            if "port_id" not in ip_entry:
                continue

            # Get or fetch port data (with caching)
            port_info = self._get_port_info(ip_entry["port_id"], port_data_cache, interface_name_field)

            # Create enriched IP structure with base data
            enriched_ip = self._create_base_ip_entry(ip_entry, obj, prefetched_data["vrfs"])

            # Get LibreNMS interface name if available
            librenms_interface_name = None
            if port_info:
                librenms_interface_name = port_info.get(interface_name_field)
                enriched_ip["interface_name"] = librenms_interface_name

            # IP with mask is already calculated in _create_base_ip_entry
            ip_with_mask = enriched_ip["ip_with_mask"]
            ip_matches = prefetched_data["ip_addresses_map"].get(ip_with_mask, [])
            global_matches = [ip for ip in ip_matches if ip.vrf_id is None]
            ip_address = global_matches[0] if len(global_matches) == 1 else None
            if ip_address is None and len(ip_matches) == 1:
                ip_address = ip_matches[0]

            if ip_address:
                # Process existing IP
                self._enrich_existing_ip(
                    enriched_ip,
                    ip_address,
                    ip_entry["port_id"],
                    librenms_interface_name,
                    prefetched_data,
                )
            else:
                # New IP that doesn't exist in NetBox
                enriched_ip["exists"] = bool(ip_matches)
                enriched_ip["status"] = "update" if ip_matches else "sync"

            # Add interface information (regardless of IP status)
            self._add_interface_info_to_ip(
                enriched_ip,
                ip_entry["port_id"],
                librenms_interface_name,
                prefetched_data,
            )

            enriched_data.append(enriched_ip)

        self._flag_management_ip(enriched_data, mgmt_ip)
        return enriched_data

    def _resolve_management_ip(self):
        """
        Fetch the LibreNMS management/polling IP for ``self.librenms_id``.

        Called only on the fresh-fetch path; the result is cached alongside the IP
        rows so cached renders don't re-hit the LibreNMS API.

        Returns:
            str: The management IP, or an empty string on any failure (best-effort,
                so the sync table still renders).
        """
        librenms_id = getattr(self, "librenms_id", None)
        if not librenms_id:
            return ""
        try:
            success, info = self.librenms_api.get_device_info(librenms_id)
        except Exception:  # pragma: no cover - defensive
            return ""
        if not success or not isinstance(info, dict):
            return ""
        # Best-effort contract: a malformed-but-dict-shaped payload (e.g. {"ip": 123})
        # must not raise on .strip(); only strip a genuine string, else fall back to "".
        ip_value = info.get("ip")
        return ip_value.strip() if isinstance(ip_value, str) else ""

    def _flag_management_ip(self, enriched_data, mgmt_ip):
        """
        Mark the entry whose IP equals the device's LibreNMS management IP.

        Used by the "Set Primary IP" toggle on the IP-sync tab to auto-select the
        right row. *mgmt_ip* is resolved once on fetch and cached, so this makes no
        API call; an empty *mgmt_ip* simply flags nothing.

        Args:
            enriched_data: The enriched IP entries to scan; the matching entry is
                mutated in place (``is_mgmt_ip = True``).
            mgmt_ip (str): The device's LibreNMS management IP.

        Returns:
            None
        """
        if not mgmt_ip:
            return
        for entry in enriched_data:
            if same_host(entry.get("ip_address", ""), mgmt_ip):
                entry["is_mgmt_ip"] = True
                break

    def _prefetch_netbox_data(self, obj, candidate_addresses, server_key=None):
        """
        Prefetch all necessary NetBox data to minimize database queries.

        Args:
            obj: The NetBox device or virtual machine the IP rows belong to.
            candidate_addresses: The ``address/prefix`` strings LibreNMS reported. Only these
                can match a row, so the IPAddress scan is restricted to them.
            server_key: The LibreNMS server key scoping per-server interface matching.

        Returns:
            dict: The interface, IP, and VRF lookup maps used during enrichment.
        """
        # Get all interfaces for the device
        all_interfaces = list(obj.interfaces.all())

        # Create maps for efficient lookups (POST-resolved server when provided; else the
        # shared degrading resolver so a missing/misconfigured default can't 500 the GET render).
        server_key = server_key or self._render_server_key()
        # Fail closed on duplicate server-scoped LibreNMS IDs: if two NetBox interfaces share one,
        # keeping either would bind an IP to whichever was iterated last. Drop the ambiguous id so
        # _add_interface_info_to_ip() falls back to the (unambiguous) name match instead.
        interfaces_by_librenms_id = {}
        ambiguous_librenms_ids = set()
        for interface in all_interfaces:
            lib_id = get_librenms_device_id(interface, server_key, auto_save=False)
            if lib_id is None:
                continue
            lib_id_key = str(lib_id)
            if lib_id_key in ambiguous_librenms_ids:
                continue
            if lib_id_key in interfaces_by_librenms_id:
                ambiguous_librenms_ids.add(lib_id_key)
                interfaces_by_librenms_id.pop(lib_id_key, None)
                continue
            interfaces_by_librenms_id[lib_id_key] = interface

        interfaces_by_name = {interface.name: interface for interface in all_interfaces}

        # Get the NetBox rows for the reported addresses only
        ip_addresses_map = defaultdict(list)
        if candidate_addresses:
            for ip in IPAddress.objects.filter(address__in=list(candidate_addresses)).select_related(
                "assigned_object_type", "vrf"
            ):
                # NetBox renders through netaddr ("::192.0.2.1/128") while the candidate keys come
                # from the ipaddress module ("::c000:201/128"). Normalise with the same parser so
                # an IPv4-compatible IPv6 row still matches.
                ip_addresses_map[str(ip_interface(str(ip.address)))].append(ip)

        # Get all VRFs
        vrfs = list(VRF.objects.all())

        return {
            "interfaces_by_librenms_id": interfaces_by_librenms_id,
            "interfaces_by_name": interfaces_by_name,
            "all_interfaces": all_interfaces,
            "device": obj,
            "ip_addresses_map": ip_addresses_map,
            "vrfs": vrfs,
        }

    def _get_port_info(self, port_id, port_data_cache, interface_name_field):
        """Get port info from LibreNMS with caching to minimize API calls"""
        if port_id not in port_data_cache:
            success, port_data = self.librenms_api.get_port_by_id(port_id)
            # A truthy success can still carry a malformed payload: port_data=None ("port" in
            # None raises), {"port": ["bad"]} (a non-dict row that later crashes at
            # port_info.get(...)). Validate the shape and cache only a real dict row, else None
            # (issue #111, same class as #100).
            ports = port_data.get("port") if success and isinstance(port_data, dict) else None
            first_port = ports[0] if isinstance(ports, list) and ports else None
            port_data_cache[port_id] = first_port if isinstance(first_port, dict) else None

        return port_data_cache[port_id]

    def _create_base_ip_entry(self, ip_entry, obj, vrfs):
        """Create the base data structure for an IP entry"""
        parsed = parse_librenms_ip_entry(ip_entry)
        ip_address = str(parsed.ip)
        prefix_length = parsed.network.prefixlen
        ip_with_mask = str(parsed)

        return {
            "ip_address": ip_address,
            "prefix_length": prefix_length,
            "ip_with_mask": ip_with_mask,
            "port_id": ip_entry["port_id"],
            "device": obj.name,
            "device_url": obj.get_absolute_url(),
            "vrf_id": None,
            "vrfs": vrfs,
        }

    def _enrich_existing_ip(self, enriched_ip, ip_address, port_id, librenms_interface_name, prefetched_data):
        """Add information for IP addresses that exist in NetBox"""
        enriched_ip["ip_url"] = ip_address.get_absolute_url()
        enriched_ip["netbox_ip_id"] = ip_address.pk
        enriched_ip["original_vrf_id"] = ip_address.vrf_id
        enriched_ip["exists"] = True

        # Add VRF info if available
        if ip_address.vrf:
            enriched_ip["vrf_id"] = ip_address.vrf.pk
            enriched_ip["vrf"] = ip_address.vrf.name

        # Set initial status to update (will change to matched if criteria met)
        enriched_ip["status"] = "update"

        # Only proceed if IP is assigned to an object
        if not ip_address.assigned_object:
            return

        assigned_interface = ip_address.assigned_object

        # Check if interface matches by LibreNMS ID
        if str(port_id) in prefetched_data["interfaces_by_librenms_id"]:
            interface = prefetched_data["interfaces_by_librenms_id"][str(port_id)]
            if assigned_interface == interface:
                enriched_ip["status"] = "matched"
                return

        # Check if interface matches by name
        if librenms_interface_name and assigned_interface.name == librenms_interface_name:
            enriched_ip["status"] = "matched"
            # Add interface information
            enriched_ip["interface_name"] = assigned_interface.name
            enriched_ip["interface_url"] = assigned_interface.get_absolute_url()

    def _add_interface_info_to_ip(self, enriched_ip, port_id, librenms_interface_name, prefetched_data):
        """Add interface information to the IP entry regardless of IP status"""
        # First try to match by LibreNMS ID (highest priority)
        if str(port_id) in prefetched_data["interfaces_by_librenms_id"]:
            interface = prefetched_data["interfaces_by_librenms_id"][str(port_id)]
            enriched_ip["interface_name"] = interface.name
            enriched_ip["interface_url"] = interface.get_absolute_url()
            return

        # Then try to match by interface name
        if librenms_interface_name and librenms_interface_name in prefetched_data["interfaces_by_name"]:
            interface = prefetched_data["interfaces_by_name"][librenms_interface_name]
            # Don't overwrite the interface name from LibreNMS but do add the URL
            enriched_ip["interface_url"] = interface.get_absolute_url()

    def get_table(self, data, obj, request, server_key=None):
        """Get the table instance for the view."""
        table = IPAddressTable(data)
        server_key = server_key or self._render_server_key()
        # server_key is config data, not a guaranteed slug — URL-encode it so a key with
        # &/=/space doesn't build a broken query string and silently change server scope.
        table.htmx_url = f"{request.path}?tab=ipaddresses" + (
            f"&server_key={quote_plus(server_key)}" if server_key else ""
        )
        return table

    def _prepare_context(self, request, obj, interface_name_field, fetch_fresh=False, server_key=None):
        """Helper method to prepare the context data for IP address sync views."""
        table = None
        cache_expiry = None
        # Scoped to the POST-resolved server when provided; else the degrading resolver.
        server_key = server_key or self._render_server_key()

        if interface_name_field is None:
            interface_name_field = get_interface_name_field(request, obj)

        # Validate the per-row schema, not just the container shape: a dict row missing
        # port_id or any supported address/prefix pair would KeyError inside
        # _create_base_ip_entry() mid-enrichment and 500 the render. Shared by the
        # fresh-fetch and the cached-snapshot fail-closed paths so a malformed LibreNMS
        # payload or a corrupt cache entry both fail closed identically.
        def _valid_ip_row(item):
            if not isinstance(item, dict) or "port_id" not in item:
                return False
            # port_id is used as a cache-dict key in _get_port_info(); an unhashable
            # (e.g. {}/[]) or bool value would raise inside `port_id not in port_data_cache`
            # and 500 the render. Reject anything that is not a plain int/str.
            port_id = item.get("port_id")
            if isinstance(port_id, bool) or not isinstance(port_id, (int, str)):
                return False
            try:
                parse_librenms_ip_entry(item)
            except ValueError:
                return False
            return True

        if fetch_fresh:
            success, ip_data = self.get_ip_addresses(obj)

            # Bail out on a failed *or malformed* fetch instead of enriching an error payload
            # and rendering an empty table under a success banner. A success flag with a
            # non-list payload (dict/string) or a list with non-dict entries makes
            # enrich_ip_data() silently drop every row and caches that empty snapshot as
            # complete, so treat it as a fetch failure.
            if not success or not isinstance(ip_data, list) or any(not _valid_ip_row(item) for item in ip_data):
                # Purge any prior valid snapshot so this fail-closed actually takes effect: without
                # it, a bad-but-successful refresh leaves the previous rows in cache and the next GET
                # serves them as stale until the TTL expires.
                cache.delete(self.get_cache_key(obj, "ip_addresses", server_key))
                return None
            # Resolve the management IP once here (live LibreNMS call) and cache it
            # below so cached renders don't re-hit the API.
            mgmt_ip = self._resolve_management_ip()
            # Fresh fetch may call get_port_by_id() per port; collect those into this
            # map and cache it so warm-cache renders enrich without any live calls.
            port_data_cache = {}
        else:
            cache_key = self.get_cache_key(obj, "ip_addresses", server_key)
            cached_ip_data = cache.get(cache_key)
            if cached_ip_data is None:
                return None
            # Fail closed on a malformed/corrupt cache entry: a truthy non-dict (list/str), or a
            # dict whose "ip_addresses" isn't a list, would crash the cached-render derefs below
            # (cached_ip_data["mgmt_ip"], .get("ports_by_id")). Treat it as a cache miss — purge
            # it and bail — mirroring the fresh path's fail-closed validation above.
            if not isinstance(cached_ip_data, dict) or not isinstance(cached_ip_data.get("ip_addresses"), list):
                cache.delete(cache_key)
                return None
            # Container shape is valid, but the nested fields still need the same fail-closed
            # checks as the fresh path: a stale row missing port_id (or with a bool/unhashable
            # one) would KeyError in _create_base_ip_entry(); a truthy non-mapping ports_by_id
            # (e.g. a list) would raise in dict(...) below; a non-str mgmt_ip would break the
            # cached["mgmt_ip"] deref. Purge and treat as a miss instead of 500-ing the tab.
            cached_ports_by_id = cached_ip_data.get("ports_by_id")
            cached_interface_name_field = cached_ip_data.get("interface_name_field")
            if (
                any(not _valid_ip_row(item) for item in cached_ip_data["ip_addresses"])
                or ("mgmt_ip" in cached_ip_data and not isinstance(cached_ip_data["mgmt_ip"], str))
                or (cached_ports_by_id is not None and not isinstance(cached_ports_by_id, dict))
                or (
                    cached_interface_name_field is not None
                    and not is_supported_interface_name_field(cached_interface_name_field)
                )
            ):
                cache.delete(cache_key)
                return None
            ip_data = cached_ip_data.get("ip_addresses", [])
            # Pre-upgrade entries cached before mgmt_ip was stored lack the key entirely
            # (distinct from a present-but-empty "" meaning "no mgmt IP"). Resolve it now —
            # a one-time live call, mirroring the ports_by_id backfill below — so the
            # "Set Primary IP" auto-select works without forcing a manual refresh first.
            cached_mgmt_ip_missing = "mgmt_ip" not in cached_ip_data
            if cached_mgmt_ip_missing:
                # coerce_librenms_id fails closed on a poisoned cached value (bool/zero/garbage):
                # get_stored_librenms_id reads the device-id cache verbatim, so a stray True would
                # otherwise int() to 1 in _resolve_management_ip and fetch a stranger's mgmt IP.
                self.librenms_id = coerce_librenms_id(self.librenms_api.get_stored_librenms_id(obj))
                mgmt_ip = self._resolve_management_ip()
            else:
                mgmt_ip = cached_ip_data["mgmt_ip"]
            # Pre-populate the port map from cache so the cached render reads only
            # cache + NetBox and never re-hits LibreNMS (resilient when it's down).
            port_data_cache = dict(cached_ip_data.get("ports_by_id") or {})
            # Pre-upgrade entries lack ports_by_id; remember so we can backfill below.
            cached_had_ports_by_id = bool(cached_ip_data.get("ports_by_id"))
            cached_had_interface_name_field = cached_interface_name_field is not None

        cache_key = self.get_cache_key(obj, "ip_addresses", server_key)

        # Enrich data in both cases to ensure current NetBox state
        ip_data = self.enrich_ip_data(
            ip_data, obj, interface_name_field, mgmt_ip, server_key=server_key, port_data_cache=port_data_cache
        )

        if fetch_fresh:
            # Cache the fresh data after enrichment, including the port map gathered
            # during enrichment so cached renders don't re-fetch ports.
            cache.set(
                cache_key,
                {
                    "ip_addresses": ip_data,
                    "mgmt_ip": mgmt_ip,
                    "ports_by_id": port_data_cache,
                    "interface_name_field": interface_name_field,
                },
                timeout=self.librenms_api.cache_timeout,
            )
        elif (
            (not cached_had_ports_by_id and port_data_cache)
            or cached_mgmt_ip_missing
            or not cached_had_interface_name_field
        ):
            # Backfill: a pre-upgrade cache entry had no ports_by_id and/or no mgmt_ip, so they
            # were rebuilt above via live calls. Persist them under the
            # *remaining* TTL (don't extend the entry's lifetime) so subsequent cached
            # renders stop re-hitting LibreNMS until the entry would have expired anyway.
            # cache.ttl() isn't part of Django's core cache API (only django-redis-style backends
            # expose it). Guard it like base/modules_view does so a non-Redis backend degrades to
            # "no backfill" rather than raising AttributeError while rendering.
            remaining_ttl = cache_remaining_ttl(cache, cache_key)
            if remaining_ttl and remaining_ttl > 0:
                cache.set(
                    cache_key,
                    {
                        "ip_addresses": ip_data,
                        "mgmt_ip": mgmt_ip,
                        "ports_by_id": port_data_cache,
                        "interface_name_field": interface_name_field,
                    },
                    timeout=remaining_ttl,
                )

        # Calculate cache expiry
        cache_ttl = cache_remaining_ttl(cache, self.get_cache_key(obj, "ip_addresses", server_key))
        if cache_ttl is not None and cache_ttl > 0:
            cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl)

        # Generate the table
        table = self.get_table(ip_data, obj, request, server_key=server_key)

        table.configure(request)

        # Prepare and return the context
        return {
            "table": table,
            "object": obj,
            "cache_expiry": cache_expiry,
            "server_key": server_key,
            "set_primary_ip": resolve_set_primary_ip(request),
            "create_missing_interfaces": resolve_create_missing_interfaces(request),
            # Donor "Move IP to winner" candidates (empty unless this device carries a
            # _migrated_to marker for server_key); drives the migrated-mode action card.
            "movable_ips": self._movable_ips_for_migration(obj, server_key),
        }

    @staticmethod
    def _movable_ips_for_migration(obj, server_key):
        """
        List the donor's interface-assigned IPs as Move-to-winner candidates (empty unless migrated).

        Only NetBox Device ``Interface`` assignments are listed: MoveIPAddressToWinnerView re-homes an
        IP from a donor interface to the winner's same-named interface and rejects non-Interface (e.g.
        VMInterface) assignments, so VM-owned IPs are intentionally excluded. Gated on the marker so a
        non-migrated device pays no extra query.
        """
        from dcim.models import Device, Interface
        from django.contrib.contenttypes.models import ContentType

        from netbox_librenms_plugin.utils import get_migrated_to_marker

        if not isinstance(obj, Device) or not get_migrated_to_marker(obj, server_key or "default"):
            return []
        name_by_id = {iface.pk: iface.name for iface in obj.interfaces.all()}
        if not name_by_id:
            return []
        iface_ct = ContentType.objects.get_for_model(Interface)
        return [
            {"id": ip.pk, "address": str(ip.address), "interface_name": name_by_id.get(ip.assigned_object_id, "")}
            for ip in IPAddress.objects.filter(
                assigned_object_type=iface_ct, assigned_object_id__in=list(name_by_id)
            ).order_by("address")
        ]

    def get_context_data(self, request, obj):
        """Get the context data for the IP address sync view."""
        interface_name_field = get_interface_name_field(request, obj)
        # GET render: scope the cache read to ?server_key (mirrors the interfaces/cables/VLAN/
        # module tabs) so a non-default-server tab reads that server's IP cache, not the
        # default's — without this the IP tab renders empty after a successful refresh on a
        # non-default server. An unresolved non-blank key scopes to that key (cache miss →
        # empty) rather than falling back to the default server's cached IPs.
        scoped, unresolved = self.resolve_get_render_server_key(request)
        if unresolved:
            # ?server_key named a server that no longer resolves (deleted/misconfigured). Its IP
            # snapshot may still be cached, but the failed rebind left self.librenms_api bound to the
            # DEFAULT server; render an empty table scoped to the requested key rather than that
            # stale server's cached IPs (mirrors modules_view.get_context_data's unresolved guard).
            return {
                "table": None,
                "object": obj,
                "cache_expiry": None,
                "server_key": scoped,
                "set_primary_ip": resolve_set_primary_ip(request),
                "create_missing_interfaces": resolve_create_missing_interfaces(request),
                "movable_ips": self._movable_ips_for_migration(obj, scoped),
            }
        context = self._prepare_context(request, obj, interface_name_field, fetch_fresh=False, server_key=scoped)
        if context is None:
            # No data found; return context with empty table (still surface migrated move actions).
            context = {
                "table": None,
                "object": obj,
                "cache_expiry": None,
                "server_key": scoped,
                "set_primary_ip": resolve_set_primary_ip(request),
                "create_missing_interfaces": resolve_create_missing_interfaces(request),
                "movable_ips": self._movable_ips_for_migration(obj, scoped),
            }
        return context

    def post(self, request, pk):
        """Handle POST request for IP address sync view."""
        obj = self.get_object(pk)
        interface_name_field = get_interface_name_field(request, obj)
        # Rebind the API to the POSTed server so the live IP/management-IP fetches hit the
        # same LibreNMS instance the cached rows are namespaced under (multi-server tabs).
        posted_server_key = request.POST.get("server_key")
        server_key = self.rebind_api_for_server(posted_server_key)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            # rebind_api_for_server() returned None to avoid building a missing/misconfigured
            # default client; reading the lazy `librenms_api` property here would reconstruct it
            # and can raise (a 500 on this HTMX error path). Use the already-cached client's key.
            active_server_key = self.active_server_key
            # render_sync_partial injects the migrated-donor context (resolved from the active
            # session key, since the POSTed key is now known-invalid) so a stale server_key can't
            # silently re-enable IP sync on a migrated donor.
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {
                    "ip_sync": {
                        "object": obj,
                        "table": None,
                        "cache_expiry": None,
                        "server_key": None,
                        # Preserve the user's set-primary-IP preference: the template binds the
                        # checkbox to ip_sync.set_primary_ip, so omitting it silently unchecks it
                        # on this error re-render.
                        "set_primary_ip": resolve_set_primary_ip(request),
                        "create_missing_interfaces": resolve_create_missing_interfaces(request),
                        # Keep the "Move IP addresses to <winner>" card on this error re-render too:
                        # the per-row moves are pure NetBox operations, and the template gates the
                        # card on ip_sync.movable_ips — omitting it (as the fetch-failure and success
                        # branches do not) would make a migrated donor's move card vanish just
                        # because the POSTed server_key was stale. Resolved against active_server_key,
                        # matching the migrated context rendered on this branch.
                        "movable_ips": self._movable_ips_for_migration(obj, active_server_key),
                    },
                },
            )
        context = self._prepare_context(request, obj, interface_name_field, fetch_fresh=True, server_key=server_key)

        if context is None:
            # _prepare_context(fetch_fresh=True) only returns None when the live
            # LibreNMS fetch failed (a genuine empty result yields a context with an
            # empty table). Report the failure rather than a misleading "no data".
            messages.error(request, "Failed to fetch IP addresses from LibreNMS; see server logs for details.")
            return self.render_sync_partial(
                request,
                obj,
                server_key,
                {
                    "ip_sync": {
                        "object": obj,
                        "table": None,
                        "cache_expiry": None,
                        "server_key": server_key,
                        # Preserve the set-primary-IP checkbox state across a failed refresh
                        # (the template binds it to ip_sync.set_primary_ip).
                        "set_primary_ip": resolve_set_primary_ip(request),
                        "create_missing_interfaces": resolve_create_missing_interfaces(request),
                        # Keep the "Move IP addresses to <winner>" card available on a LibreNMS
                        # fetch failure: the per-row moves (MoveIPAddressToWinnerView) are pure
                        # NetBox operations that don't touch LibreNMS, and every other exit surfaces
                        # movable_ips — omitting it here would make a migrated donor's move card
                        # vanish just because LibreNMS was briefly unreachable.
                        "movable_ips": self._movable_ips_for_migration(obj, server_key),
                    },
                },
            )

        messages.success(request, "IP address data refreshed successfully.")
        return self.render_sync_partial(request, obj, server_key, {"ip_sync": context})


class SingleIPAddressVerifyView(NetBoxObjectPermissionMixin, LibreNMSPermissionMixin, CacheMixin, View):
    """
    View for verifying single IP address data with different VRF.
    """

    # Read-only verify endpoint: require object-view permission (mirrors the interface/module/
    # cable verify views). Without it any user with mere plugin-view rights could POST an
    # arbitrary device id and read back that object's name/url/cached IP rows. The effective
    # requirement is narrowed per-request in post() to the model the request actually targets
    # (Device vs VirtualMachine); this class-level default (both) is the fail-closed fallback the
    # permission-wiring test asserts.
    required_object_permissions = {"POST": [("view", Device), ("view", VirtualMachine)]}

    def require_object_permissions_json(self, method, *, object_id=None, object_type=None):
        """
        Resolve the per-object view permission for this verify request, then run the base gate.

        Computing the requirement here (rather than inline in :meth:`post`) keeps the DB
        model-resolution behind the same method the post-flow tests mock out — so those tests
        exercise the request flow without a DB hit, while the real gate path resolves Device vs
        VirtualMachine and checks the matching view permission.

        Args:
            method: HTTP method to gate (``"POST"``).
            object_id: The posted object id, used to resolve the model when no type is given.
            object_type: The posted ``object_type`` (``"device"``/``"virtualmachine"``), if any.

        Returns:
            None if permitted, or a 403 JsonResponse if denied.
        """
        self.required_object_permissions = {method: self._required_perms_for_object(object_id, object_type)}
        return super().require_object_permissions_json(method)

    def _required_perms_for_object(self, object_id, object_type):
        """
        Object-view perms a verify POST must hold for the object it targets.

        Mirrors :meth:`_get_object`'s resolution so the gate matches the model whose name/url/cache
        the response would expose: an explicit ``object_type`` gates on exactly that model; with no
        type, resolve the id to its model (an existence check that reads no object data) — Device
        first, then VirtualMachine. Fall back to requiring BOTH view perms (fail closed) when the id
        is unusable or resolves to neither, so a Device-only (or VM-only) caller can never read the
        other model's data through this endpoint.
        """
        if object_type == "device":
            return [("view", Device)]
        if object_type == "virtualmachine":
            return [("view", VirtualMachine)]
        try:
            if Device.objects.filter(pk=object_id).exists():
                return [("view", Device)]
            if VirtualMachine.objects.filter(pk=object_id).exists():
                return [("view", VirtualMachine)]
        except (ValueError, TypeError):
            pass
        return [("view", Device), ("view", VirtualMachine)]

    def _get_object(self, object_id, object_type=None):
        """
        Retrieve the object (Device or VirtualMachine) based on ID and optional type.

        Resolves through permission-restricted querysets: the POST gate only checks model-level
        view perms, so a constrained grant must not resolve an out-of-scope id here — it 404s
        instead of exposing the object's cached IP verify payload.
        """
        if object_type == "device":
            return self.restrict_object_or_404(Device, pk=object_id)
        elif object_type == "virtualmachine":
            return self.restrict_object_or_404(VirtualMachine, pk=object_id)
        else:
            # Try to find object without knowing its type (scoped to the caller's viewable objects).
            obj = self.restricted_queryset(Device).filter(pk=object_id).first()
            if obj:
                return obj

            obj = self.restricted_queryset(VirtualMachine).filter(pk=object_id).first()
            if obj:
                return obj

            raise Http404(f"Object with ID {object_id} not found in Device or VirtualMachine models")

    def _parse_ip_address(self, ip_address):
        """
        Parse IP address string into address and prefix length.
        Works with both IPv4 and IPv6 addresses.
        """
        parsed = parse_address_with_prefix(ip_address)
        return str(parsed.ip), parsed.network.prefixlen

    def _find_in_cache(self, cached_data, address, prefix_len):
        """Find IP address in cache data using unified fields only."""
        # Fail closed on a missing OR truthy-but-malformed entry (a legacy/corrupt non-dict such
        # as a list from an older snapshot shape): a bare ``cached_data.get(...)`` would raise
        # AttributeError on a list -> the verify POST's broad except returns 500 on every retry
        # until the entry expires. Treat it as a cache miss instead, mirroring the isinstance
        # fail-closed guard the GET-render path (_prepare_context) already uses.
        if not isinstance(cached_data, dict):
            return None, None, None

        target = str(parse_address_with_prefix(address, prefix_len))
        for ip_entry in cached_data.get("ip_addresses", []):
            # Per-item shape guard: a non-dict row (or one missing the unified fields) in a
            # corrupt/legacy snapshot would otherwise TypeError/KeyError here and be swallowed by
            # post()'s broad except as a 500. Skip it, mirroring _extract_cached_links /
            # extract_cached_ports which validate each row.
            if not isinstance(ip_entry, dict):
                continue
            try:
                cached_address = str(parse_address_with_prefix(ip_entry.get("ip_with_mask")))
            except (TypeError, ValueError):
                continue
            if cached_address == target:
                return (ip_entry, ip_entry.get("vrf_id"), ip_entry.get("port_id"))

        return None, None, None

    def _find_existing_ip(self, address_no_mask, prefix_len, vrf_id=None):
        """
        Find existing IP address in NetBox, optionally with specific VRF.
        """
        ip_with_mask = str(parse_address_with_prefix(address_no_mask, prefix_len))

        matches = list(IPAddress.objects.filter(address=ip_with_mask).order_by("pk"))
        if not matches:
            return False, False, None

        if vrf_id is not None:
            matching_vrf_rows = [row for row in matches if row.vrf_id == vrf_id]
        else:
            matching_vrf_rows = [row for row in matches if row.vrf_id is None]

        if len(matching_vrf_rows) == 1:
            return True, True, matching_vrf_rows[0].get_absolute_url()
        return True, False, matches[0].get_absolute_url() if len(matches) == 1 else None

    def _determine_status(self, exists_any_vrf, exists_specific_vrf, original_vrf_id, vrf_id):
        """
        Determine the status of an IP address based on existence and VRF.
        """
        if exists_any_vrf:
            # IP exists in NetBox
            if exists_specific_vrf:
                return "matched"
            else:
                return "update"
        else:
            # IP doesn't exist in NetBox, check if restoring to original VRF
            if original_vrf_id is not None and original_vrf_id == vrf_id:
                return "matched"
            else:
                return "sync"

    def post(self, request):
        """
        POST request to return json response with formatted IP address status.
        """
        # Parse the (caller-supplied) body first — this reads no object data — so the permission
        # gate can target the model the request actually addresses. _get_object() resolves a Device
        # OR a VirtualMachine, so a static Device-only gate would let a user with only
        # dcim.view_device read VM name/url/cache data (and vice versa). Gate BELOW still runs
        # before any object is resolved or any cached row is read.
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"status": "error", "message": "Invalid JSON payload"}, status=400)
        # json.loads accepts a bare list/string/number; calling .get() on those raises before the
        # permission gate and the broad handler below, so 500s on a non-object body. Reject it.
        if not isinstance(data, dict):
            return JsonResponse({"status": "error", "message": "JSON payload must be an object"}, status=400)
        object_id = data.get("device_id")
        object_type = data.get("object_type")
        if error := self.require_object_permissions_json("POST", object_id=object_id, object_type=object_type):
            return error
        try:
            from netbox_librenms_plugin.librenms_api import LibreNMSAPI

            ip_address = data.get("ip_address")
            vrf_id = data.get("vrf_id")
            # Validate the requested server key before using it as a cache namespace: an
            # unconfigured/forged key would otherwise let a caller probe arbitrary server-key cache
            # namespaces via get_cache_key(). Mirror the sync/cable membership check; anything
            # unrecognized falls back to "default" (the prior missing-key behaviour), so a caller
            # can only ever address a configured key or the fixed default — never an arbitrary one.
            requested_server_key = data.get("server_key")
            if isinstance(requested_server_key, str) and requested_server_key in LibreNMSAPI.get_available_servers():
                server_key = requested_server_key
            else:
                server_key = "default"

            if not ip_address:
                return JsonResponse({"status": "error", "message": "No IP address provided"}, status=400)

            # Reject JSON booleans explicitly before the falsy check: bool is an int
            # subclass, so True/False would otherwise pass int() and validate as IDs 1/0,
            # and object_id=False would be misreported as "No object ID provided".
            if isinstance(object_id, bool):
                return JsonResponse({"status": "error", "message": "Invalid object ID"}, status=400)
            # Reject JSON floats too: int(1.9) silently truncates to 1, so a fractional id
            # would coerce to a different object instead of returning a clean 400.
            if isinstance(object_id, float):
                return JsonResponse({"status": "error", "message": "Invalid object ID"}, status=400)

            if not object_id:
                return JsonResponse({"status": "error", "message": "No object ID provided"}, status=400)

            # Validate the client-supplied numeric IDs up front so a bad value returns a
            # clean 400 instead of raising deep in the ORM and being caught as a generic 500.
            try:
                object_id = int(object_id)
            except (TypeError, ValueError):
                return JsonResponse({"status": "error", "message": "Invalid object ID"}, status=400)
            if vrf_id in (None, ""):
                vrf_id = None
            elif isinstance(vrf_id, bool):
                return JsonResponse({"status": "error", "message": "Invalid VRF ID"}, status=400)
            elif isinstance(vrf_id, float):
                return JsonResponse({"status": "error", "message": "Invalid VRF ID"}, status=400)
            else:
                try:
                    vrf_id = int(vrf_id)
                except (TypeError, ValueError):
                    return JsonResponse({"status": "error", "message": "Invalid VRF ID"}, status=400)

            # Get the object (Device or VirtualMachine)
            try:
                obj = self._get_object(object_id, object_type)
            except Http404:
                return JsonResponse({"status": "error", "message": f"Object with ID {object_id} not found"}, status=404)

            # Parse IP address
            try:
                address_no_mask, prefix_len = self._parse_ip_address(ip_address)
            except ValueError:
                return JsonResponse(
                    {"status": "error", "message": "Invalid IP address: prefix length is missing or invalid"},
                    status=400,
                )

            cache_key = self.get_cache_key(obj, "ip_addresses", server_key)
            cached_data = cache.get(cache_key)

            # Basic record with default values
            updated_record = {
                "ip_address": address_no_mask,
                "prefix_length": prefix_len,
                "ip_with_mask": str(parse_address_with_prefix(address_no_mask, prefix_len)),
                "device": obj.name,
                "device_url": obj.get_absolute_url(),
                "vrf_id": vrf_id,
                "exists": False,
                "status": "sync",
            }

            # Try to find the IP in cache data
            cache_entry, original_vrf_id, original_port_id = self._find_in_cache(
                cached_data, address_no_mask, prefix_len
            )

            # Update record with cache data if found
            if cache_entry:
                # Update with all fields except vrf_id and status
                for key, value in cache_entry.items():
                    if key not in ["vrf_id", "status"]:
                        updated_record[key] = value

            # If no interface found in cache, use first device interface
            if original_port_id is None:
                interface = obj.interfaces.first()
                if interface:
                    updated_record["interface_name"] = interface.name
                    updated_record["interface_url"] = interface.get_absolute_url()

            # Check if IP exists in NetBox
            exists_any_vrf, exists_specific_vrf, ip_url = self._find_existing_ip(address_no_mask, prefix_len, vrf_id)

            if exists_any_vrf:
                updated_record["exists"] = True
                updated_record["ip_url"] = ip_url

            # Determine status based on existence and VRF
            updated_record["status"] = self._determine_status(
                exists_any_vrf, exists_specific_vrf, original_vrf_id, vrf_id
            )

            # Render status HTML
            table = IPAddressTable(data=[])
            status_html = table.render_status(updated_record["status"], updated_record)

            return JsonResponse(
                {
                    "status": "success",
                    "ip_address": ip_address,
                    "formatted_row": {"status": status_html},
                }
            )

        except Exception:
            logger.exception("Unexpected error in IP address sync")
            return JsonResponse(
                {"status": "error", "message": "An internal error occurred. Please check server logs."}, status=500
            )
