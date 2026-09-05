import logging
import re
from urllib.parse import quote_plus

from dcim.models import Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import MultipleObjectsReturned
from django.db.models import Q
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.views import View

from netbox_librenms_plugin.utils import (
    cache_remaining_ttl,
    build_librenms_id_qs,
    coerce_librenms_id,
    get_interface_name_field,
    get_librenms_oob,
    get_librenms_sync_device,
    get_virtual_chassis_member,
    oob_badge_html,
)
from netbox_librenms_plugin.views.mixins import (
    CacheMixin,
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
    extract_cached_ports,
    parse_request_json,
)

logger = logging.getLogger(__name__)


def _librenms_id_q(server_key: str, value, *, include_oob: bool = True) -> Q:
    """
    Return a combined Q matching JSON-field and legacy bare-int librenms_id.

    Matches both integer and string representations, and both the scalar form
    (``{server_key: 42}``) and the dict form
    (``{server_key: {"id": 42, "oob": {"id": 99}}}``) so a device carrying OOB
    metadata or a merged link still resolves by LibreNMS ID. Mirrors the path
    coverage of :func:`utils.find_by_librenms_id`.

    Args:
        server_key (str): The LibreNMS server key whose JSON sub-key is matched.
        value: The LibreNMS id to match (int or string form).
        include_oob (bool): When True (default), also match the OOB-controller
            sub-key (``{server_key: {"oob": {"id": value}}}``). Pass ``False`` when
            resolving a device by its *own* LibreNMS identity (e.g. a cable's remote
            ``device_id``): the OOB path matches a *different* device that merely
            references this id as its controller, so including it would match both the
            real device and that referencer and raise ``MultipleObjectsReturned``.

    Returns:
        Q: A combined lookup matching any stored form of the id (matches nothing for
            a bool *value*).
    """
    # Match nothing for values that can't be a valid librenms_id. Reject bools (an int subclass)
    # and any non-int/str type up front: int() would truncate a float like 1.9 to 1 and match the
    # wrong device/interface — looser than find_by_librenms_id's int/str-only contract (issue
    # #103). Also reject blank strings and non-positive ids.
    _match_nothing = Q(pk__isnull=True) & Q(pk__isnull=False)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return _match_nothing
    if isinstance(value, str) and not value.strip():
        return _match_nothing
    try:
        if int(value) <= 0:
            return _match_nothing
    except (TypeError, ValueError):
        # Non-numeric string: it can't equal a numeric id, but keep the literal match below
        # rather than failing closed (no behaviour change for that case).
        pass

    # Single source of truth for the path coverage (host scalar / __id / legacy bare, + the OOB
    # sub-key), shared with utils.find_by_librenms_id so the two can't drift on which stored
    # shapes resolve.
    host_q, oob_q = build_librenms_id_qs(server_key, value)
    return host_q | oob_q if include_oob else host_q


_SUB_UNIT_RE = re.compile(r"^(?P<physical>.+)\.\d+$")


def _drop_masked_sub_units(rows):
    """Drop a neighbour row for a sub-unit whose own physical port is reported beside it.

    A router advertises LLDP from the physical port and from each sub-unit configured on
    it, so one local port can report the same neighbour several times. A cable terminates
    on the physical port only, so every sub-unit row renders as a mismatch.

    Masking is deliberately narrow: a sub-unit is dropped only when the SAME local port
    reports its exact parent name on the SAME remote device. A sub-unit reported on its
    own is kept, because it is then the only evidence of that neighbour.
    """
    physical_by_group = {}
    for row in rows:
        if not _SUB_UNIT_RE.match(row["remote_port"] or ""):
            physical_by_group.setdefault((row["local_port_id"], row["remote_device_id"]), set()).add(row["remote_port"])

    kept = []
    for row in rows:
        match = _SUB_UNIT_RE.match(row["remote_port"] or "")
        if match and match.group("physical") in physical_by_group.get(
            (row["local_port_id"], row["remote_device_id"]), ()
        ):
            continue
        kept.append(row)
    return kept


def _extract_cached_links(cached, cache_key=None):
    """
    Return the validated link list from a cached "links" entry, or None when malformed.

    A stale/corrupt but truthy cache value — a non-dict, a non-list ``links``, or any non-dict
    link row — would crash the cached GET render or the verify path on ``.get()`` / ``.items()``
    / iteration. Treat those as a cache miss: when ``cache_key`` is given, purge the bad entry so
    the next read doesn't keep serving garbage.

    Args:
        cached: The raw value read from the links cache key.
        cache_key: Optional cache key to delete when the entry is malformed.

    Returns:
        list | None: The list of (dict) link rows, or None if the entry is malformed.
    """
    if not isinstance(cached, dict) or not isinstance(cached.get("links"), list):
        if cache_key is not None:
            cache.delete(cache_key)
        return None
    links = cached["links"]
    if any(not isinstance(link, dict) for link in links):
        if cache_key is not None:
            cache.delete(cache_key)
        return None
    return links


def _resolve_local_interface(device, server_key, local_port_id, name_candidates):
    """
    Resolve a link row's local Interface on *device*: stable librenms_id first, then name.

    The id-beats-name precedence and the dual ifName/ifDescr candidate fallback (issue #88)
    are the drift-prone core shared by ``enrich_local_port`` and ``SingleCableVerifyView``'s
    re-resolution — one implementation so a fix in one path can't miss the other again (the
    issue #88 fallback and the OOB skip were each patched in both copies separately before
    this was extracted). VC-member selection and the OOB skip stay at the call sites: the two
    paths deliberately differ there (the initial render leaves a VC row unresolved when the
    member lookup fails; verify falls back to the selected device).

    Args:
        device: The NetBox device (or VC member) whose interfaces are searched.
        server_key (str): LibreNMS server key scoping the librenms_id match.
        local_port_id: The LibreNMS port_id; falsy skips the id match.
        name_candidates (list): Interface names for the fallback; empty skips the name match.

    Returns:
        Interface | None: The resolved interface, or None when neither match hits.
    """
    interface = None
    if local_port_id:
        interface = device.interfaces.filter(_librenms_id_q(server_key, local_port_id)).first()
    if not interface and name_candidates:
        interface = device.interfaces.filter(name__in=name_candidates).first()
    return interface


# The raw (un-enriched) link fields a cached/replayed link is stripped down to before
# re-enrichment — derived fields (netbox_*_id, *_url, cable_status, …) are dropped so stale
# IDs/URLs can't cause DoesNotExist after the underlying NetBox objects are deleted. Defined
# once so the strip in _prepare_context and in SingleCableVerifyView stay in lock-step.
_RAW_LINK_KEYS = frozenset(
    {
        "local_port",
        "local_port_alt",
        "local_port_id",
        "remote_port",
        "remote_device",
        "remote_port_id",
        "remote_device_id",
        "_source",
    }
)


class BaseCableTableView(LibreNMSPermissionMixin, LibreNMSAPIMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    """
    Base view for synchronizing cable information from LibreNMS.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_cable_sync_content.html"

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine)."""
        return self.restrict_object_or_404(self.model, pk=pk)

    def get_ip_address(self, obj):
        """Get the primary IP address for the object."""
        if obj.primary_ip:
            return str(obj.primary_ip.address.ip)
        return None

    def get_ports_data(self, obj, server_key=None):
        """Get ports data without affecting cache"""
        # Scope to the POST-resolved server when provided; else the shared degrading resolver
        # (avoids a GET 500 on a missing/misconfigured default).
        server_key = server_key or self._render_server_key()
        # self.librenms_id is set as a side effect of get_links_data(); read it defensively so a
        # caller that reaches this public method first (or a subclass that doesn't run links) gets
        # the OOB-only/no-host behaviour instead of an AttributeError.
        librenms_id = getattr(self, "librenms_id", None)
        # No host LibreNMS id (OOB-only device): return empty BEFORE consulting the cache. A stale
        # host-ports snapshot cached from a previous (mapped) refresh must not resurface and feed
        # the new render now that the host mapping is gone. There is also nothing to fetch —
        # get_ports(None) would issue a GET /devices/None/ports that always 404s.
        if librenms_id is None:
            return {"ports": []}
        ports_cache_key = self.get_cache_key(obj, "ports", server_key)
        # Shape-guard the cached entry (mirrors _extract_cached_links): a truthy but
        # malformed value would AttributeError-500 get_links_data's .get("ports") reads.
        cached_data = extract_cached_ports(cache.get(ports_cache_key), ports_cache_key)
        if cached_data:
            return cached_data
        success, data = self.librenms_api.get_ports(librenms_id)
        if not success:
            return {"ports": []}
        return data

    @staticmethod
    def _build_cable_port_name_maps(ports_data, interface_name_field, alt_name_field):
        """
        Build ``{port_id: name}`` and ``{port_id: alt_name}`` maps from a get_ports payload.

        Shared by the host and OOB cable branches so they name local ports identically. A
        malformed payload (non-dict, non-list ports, non-dict rows) yields empty maps rather
        than crashing.
        """
        name_map = {}
        alt_map = {}
        ports = ports_data.get("ports") if isinstance(ports_data, dict) else None
        for port in ports if isinstance(ports, list) else []:
            # A malformed LibreNMS/cache payload can carry non-dict rows (strings/nulls);
            # dereferencing .get() on those would 500 the refresh, so skip them.
            if not isinstance(port, dict):
                continue
            raw_port_id = port.get("port_id")
            if raw_port_id is None:
                continue
            port_id = str(raw_port_id)
            port_name = port.get(interface_name_field)
            if port_name is None:
                continue
            name_map[port_id] = port_name
            # Only record an alternate that differs from the displayed name (issue #88).
            alt_name = port.get(alt_name_field)
            if alt_name and alt_name != port_name:
                alt_map[port_id] = alt_name
        return name_map, alt_map

    @staticmethod
    def _collect_cable_links(links, name_map, alt_map, source):
        """
        Turn LibreNMS LLDP link rows into the table's row dicts, tagged with *source*.

        Shared by the host (``source='main'``) and OOB (``source='oob'``) branches. Non-dict
        rows are skipped; an unmapped local_port_id falls back to the LibreNMS-reported name.
        """
        rows = []
        for link in links:
            if not isinstance(link, dict):
                continue
            port_id = link.get("local_port_id")
            key = str(port_id)
            local_port_name = name_map.get(key)
            if local_port_name is None:
                # Fall back to the LibreNMS-reported local_port name so name-based resolution
                # still works when the ports map misses or the ports fetch failed.
                local_port_name = link.get("local_port")
            rows.append(
                {
                    "local_port": local_port_name,
                    "local_port_alt": alt_map.get(key),
                    "local_port_id": port_id,
                    "remote_port": link.get("remote_port"),
                    "remote_device": link.get("remote_hostname"),
                    "remote_port_id": link.get("remote_port_id"),
                    "remote_device_id": link.get("remote_device_id"),
                    "_source": source,
                }
            )
        return _drop_masked_sub_units(rows)

    @staticmethod
    def _classify_links_fetch_error(success, data):
        """Extract a human-readable error string from a failed/garbled get_device_links response."""
        if not success:
            # A failed fetch may carry the detail under "error" OR "message"; capture either,
            # else post() loses the real reason and can cache an empty "successful" refresh.
            return (data.get("error") or data.get("message") or str(data)) if isinstance(data, dict) else str(data)
        if isinstance(data, dict):
            return data.get("message") or data.get("error") or "Unexpected response from LibreNMS"
        return "Unexpected response from LibreNMS (expected an object)."

    def _merge_oob_cable_links(self, links_data, lookup_device, server_key, interface_name_field, alt_name_field):
        """
        Append the linked OOB controller's LLDP links (``_source='oob'``) to *links_data*.

        Returns True when an OOB controller is linked, so the caller's empty-refresh
        classification can tell an OOB-only mapping from a truly unmapped device. Sets
        ``self._oob_links_fetch_failed`` on a failed/malformed OOB fetch so post() can warn
        rather than silently dropping OOB rows.
        """
        oob = get_librenms_oob(lookup_device, server_key=server_key)
        if not oob:
            # No OOB controller linked — a genuinely unmapped/host-only device.
            return False
        # Coerce the OOB controller id like the host id: a non-numeric/bool/zero/negative stored
        # id fails closed (skip the fetch) rather than building a GET /devices/<garbage>/... that
        # 404s and silently drops OOB rows.
        oob_id = coerce_librenms_id(oob.get("id"))
        if not oob_id:
            # An OOB controller IS linked, but its stored id is corrupt. Mirror interfaces_view's
            # fail-closed pattern: flag + warn and return True (OOB linked) so post() surfaces the
            # dropped OOB rows instead of showing a "successful" banner over silently-missing rows.
            self._oob_links_fetch_failed = True
            logger.warning(
                "OOB controller linked for device %s but its stored id is invalid (%r); skipping OOB links",
                self.librenms_id,
                oob.get("id"),
            )
            return True
        oob_success, oob_data = self.librenms_api.get_device_links(oob_id)
        # Mirror the main-device branch: a 200 {"status": "error", ...} body is also a failure.
        oob_ok = (
            oob_success and isinstance(oob_data, dict) and oob_data.get("status") != "error" and "error" not in oob_data
        )
        if not oob_ok:
            self._oob_links_fetch_failed = True
            logger.warning(
                "OOB links fetch failed for device %s (OOB id %s): %s",
                self.librenms_id,
                oob_id,
                oob_data.get("message") if isinstance(oob_data, dict) else oob_data,
            )
            return True
        oob_ports_success, oob_ports_data = self.librenms_api.get_ports(oob_id)
        oob_map, oob_alt_map = self._build_cable_port_name_maps(
            oob_ports_data if oob_ports_success else {}, interface_name_field, alt_name_field
        )
        oob_links = oob_data.get("links")
        if not isinstance(oob_links, list):
            # Malformed "links" (null/object): flag the failure and keep the host links already
            # collected rather than crashing on iteration.
            self._oob_links_fetch_failed = True
            logger.warning(
                "OOB links fetch returned a malformed payload for device %s (OOB id %s): %s",
                self.librenms_id,
                oob_id,
                oob_data,
            )
            return True
        links_data.extend(self._collect_cable_links(oob_links, oob_map, oob_alt_map, "oob"))
        return True

    def get_links_data(self, obj, server_key=None, sync_device=None):
        """Fetch links data from LibreNMS for the device and add local port names."""
        # Scope DB lookups (sync device / OOB) and the ports cache to the POST-resolved
        # server when provided (fallback: session server). The LibreNMS fetch itself
        # still goes through self.librenms_api (the session-active server), by design.
        server_key = server_key or self.librenms_api.server_key
        # Reset per-call so a prior request's OOB failure doesn't leak into this one.
        self._oob_links_fetch_failed = False
        # Distinguish a real LibreNMS fetch failure (auth/network/server) from a device
        # that simply has no links, so the caller can surface the actual error instead of
        # always saying "No links found". Reset per-call to avoid leaking a prior error.
        self._links_fetch_error = None
        # Resolve the VC sync device once and use it for the main LibreNMS id + ports too —
        # not just the OOB branch below. On VC-member pages the active librenms_id/mapping can
        # live on the priority member, so reading it from the viewed `obj` would fetch one
        # member's cables and cache them under another member's key (mismatched verify/sync).
        # Reuse the device the caller (_prepare_context) already resolved to avoid a second
        # get_librenms_sync_device() VC-members query per request; falls back to resolving here.
        lookup_device = sync_device or get_librenms_sync_device(obj, server_key=server_key) or obj
        # coerce_librenms_id fails closed on a bool/zero/negative/garbage value (a poisoned
        # id-cache can return True/0 verbatim — get_librenms_id only coerces the custom-field
        # and discovery paths), so a falsy non-None id resolves to None here instead of being
        # passed to get_device_links()/get_ports() as device id 1 (int(True)) — fetching a
        # stranger's links. Mirrors the GET/interfaces-POST contract.
        self.librenms_id = coerce_librenms_id(self.librenms_api.get_librenms_id(lookup_device))
        if self.librenms_id is None:
            # OOB-only / unmapped device: skip the host LLDP call. get_device_links(None) would
            # GET /devices/None/links and always 404 for no benefit. Synthesize the same not-ok
            # result the reconciliation below already handles — an OOB-only mapping still falls
            # through to render its rows; a device with neither host id nor OOB still resolves to
            # "No links found".
            success, data = False, "Device has no LibreNMS host mapping."
        else:
            success, data = self.librenms_api.get_device_links(self.librenms_id)
        # A failed/garbled host LLDP call (including an OOB-only device whose host
        # librenms_id is None) must not abort the whole fetch: fall through (don't return
        # None) so the OOB merge below still runs and OOB-only devices render their rows.
        # get_device_links returns the raw JSON body, so a 200 can still carry an
        # application-level error ({"status": "error", ...}) or a non-object payload
        # (list/null/scalar) — treat all of those as not-ok.
        lldp_ok = success and isinstance(data, dict) and "error" not in data and data.get("status") != "error"
        if not lldp_ok:
            # Capture the real fetch failure so that when there's ultimately nothing to show,
            # post() can surface the actual LibreNMS error instead of a generic "No links found".
            self._links_fetch_error = self._classify_links_fetch_error(success, data)

        interface_name_field = get_interface_name_field(getattr(self, "request", None), obj)
        # The alternate LibreNMS name field: when the user displays ifName, a NetBox
        # interface may still be named from ifDescr (and vice versa). Carrying the alternate
        # name lets enrich_local_port fall back to either field (issue #88).
        alt_name_field = "ifDescr" if interface_name_field == "ifName" else "ifName"
        ports_data = self.get_ports_data(lookup_device, server_key=server_key)
        local_ports_map, local_ports_alt_map = self._build_cable_port_name_maps(
            ports_data, interface_name_field, alt_name_field
        )

        # Only consume links when the LLDP fetch was OK. A dict-shaped body can still carry a
        # malformed "links" (null/object); treat that as no links (and record the error)
        # rather than crashing or returning early — the OOB merge below must still run.
        links = data.get("links") if lldp_ok else []
        if not isinstance(links, list):
            self._links_fetch_error = (
                self._links_fetch_error or "Unexpected response from LibreNMS (links must be a list)."
            )
            links = []
        links_data = self._collect_cable_links(links, local_ports_map, local_ports_alt_map, "main")

        # If an OOB controller is linked, fetch its LLDP links and merge. Reuse the sync device
        # resolved at the top so host + OOB data stay scoped to the same member.
        oob_linked = self._merge_oob_cable_links(
            links_data, lookup_device, server_key, interface_name_field, alt_name_field
        )

        # Distinguish a *successful* zero-row refresh ([] — flows through to the success path in
        # _prepare_context(), where an OOB-fetch warning can still be surfaced) from a genuine
        # fetch failure (None — mislabeled "No links found" otherwise). A refresh is a failure
        # only when nothing was collected AND a fetch error was recorded (host LLDP failure or a
        # malformed payload). An empty-but-valid host result, or OOB-failure with a host success,
        # records no host error and must return [] so the warning isn't dropped. Any collected
        # rows (host / OOB / serial) always come back.
        #
        # Exception: an OOB-only mapping has no host librenms_id, so the host get_device_links()
        # call always records _links_fetch_error even though no host fetch was meaningfully
        # attempted. If the OOB controller validly returns no links, that's a *successful* empty
        # refresh — return [] so _prepare_context() overwrites the cache with the empty snapshot
        # (otherwise stale OOB rows linger after a genuine empty refresh). But this exemption
        # only holds when the OOB fetch itself SUCCEEDED: a failed/malformed OOB fetch
        # (_oob_links_fetch_failed) on an OOB-only mapping collects zero rows too, and treating
        # that as a successful empty refresh would overwrite the cache with [] and drop the
        # very rows we couldn't re-fetch. So fall back to None (failure) in that case.
        host_mapping_absent_but_oob_scoped = (
            self.librenms_id is None and oob_linked and not self._oob_links_fetch_failed
        )
        if not links_data and self._links_fetch_error and not host_mapping_absent_but_oob_scoped:
            return None
        return links_data

    def get_device_by_id_or_name(self, remote_device_id, hostname, server_key=None):
        """Try to find device in NetBox first by librenms_id custom field, then by name"""
        if server_key is None:
            server_key = self._render_server_key()
        # First try matching by LibreNMS ID. The remote device_id is the remote device's OWN
        # identity, so exclude the OOB-controller path: matching it would also select a different
        # device that references this id as its controller, tripping MultipleObjectsReturned.
        if remote_device_id is not None:
            try:
                device = Device.objects.get(_librenms_id_q(server_key, remote_device_id, include_oob=False))
                return device, True, None
            except Device.DoesNotExist:
                pass
            except MultipleObjectsReturned:
                return (
                    None,
                    False,
                    f"Multiple devices found with the same LibreNMS ID: {remote_device_id}.",
                )

        # Fall back to name matching if no device found by ID. LibreNMS reports the neighbour
        # hostname as the device advertises it, which is commonly all lower case, while NetBox
        # holds the operator's capitalisation. Match case insensitively or the remote end only
        # ever resolves through librenms_id.
        try:
            device = Device.objects.get(name__iexact=hostname)
            return device, True, None
        except Device.DoesNotExist:
            # Try without domain name
            simple_hostname = hostname.split(".")[0]
            try:
                device = Device.objects.get(name__iexact=simple_hostname)
                return device, True, None
            except Device.DoesNotExist:
                return None, False, None
            except MultipleObjectsReturned:
                return (
                    None,
                    False,
                    f"Multiple devices found with the same name: {hostname}.",
                )
        except MultipleObjectsReturned:
            return (
                None,
                False,
                f"Multiple devices found with the same name: {hostname}.",
            )

    def enrich_local_port(self, link, obj, server_key=None):
        """Add local port URL if interface exists in NetBox"""
        # Merged OOB-controller rows are context-only: their local port lives on the
        # CONTROLLER, not the host, so a shared name (or colliding stored librenms_id)
        # must not bind a host interface — that would render a wrong local_port_url and
        # cable state. Sync and the actions column already refuse OOB rows; leave the
        # local end unresolved here too.
        if link.get("_source") == "oob":
            return
        if local_port := link.get("local_port"):
            interface = None
            local_port_id = link.get("local_port_id")
            if server_key is None:
                server_key = self._render_server_key()

            # Name fallback is field-agnostic: a NetBox interface may be named from the
            # LibreNMS field the user is not currently displaying. For example, the
            # interface carries the ifDescr value while interface_name_field selects ifName).
            # Match the displayed name plus the alternate field captured at fetch time,
            # mirroring the dual ifName/ifDescr fallback in
            # interface relationship enrichment. The stable librenms_id match below
            # still wins when present; this only widens the fragile name fallback.
            name_candidates = [n for n in (local_port, link.get("local_port_alt")) if n]

            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                chassis_member = get_virtual_chassis_member(obj, local_port)

                if chassis_member:
                    interface = _resolve_local_interface(chassis_member, server_key, local_port_id, name_candidates)
            else:
                interface = _resolve_local_interface(obj, server_key, local_port_id, name_candidates)

            if interface:
                link["local_port_url"] = reverse("dcim:interface", args=[interface.pk])
                link["netbox_local_interface_id"] = interface.pk

    def enrich_remote_port(self, link, device, server_key=None):
        """Add remote port URL if device and interface exist in NetBox"""
        if remote_port := link.get("remote_port"):
            netbox_remote_interface = None
            librenms_remote_port_id = link.get("remote_port_id")
            if server_key is None:
                server_key = self._render_server_key()

            # Same id-beats-name resolution as the local end — reuse the shared resolver so a fix
            # in one path can't miss the other again (the drift risk _resolve_local_interface was
            # extracted to close). VC-member selection stays here because the remote side leaves the
            # interface unresolved when the member lookup fails.
            if hasattr(device, "virtual_chassis") and device.virtual_chassis:
                chassis_member = get_virtual_chassis_member(
                    device,
                    remote_port,
                    return_device_on_failure=False,
                )
                if chassis_member:
                    netbox_remote_interface = _resolve_local_interface(
                        chassis_member, server_key, librenms_remote_port_id, [remote_port]
                    )
            else:
                netbox_remote_interface = _resolve_local_interface(
                    device, server_key, librenms_remote_port_id, [remote_port]
                )

            if netbox_remote_interface:
                link["remote_port_url"] = reverse("dcim:interface", args=[netbox_remote_interface.pk])
                link["netbox_remote_interface_id"] = netbox_remote_interface.pk
                link["remote_port_name"] = netbox_remote_interface.name

        # Return the link even when remote_port is empty (or unresolved): callers assign the
        # result back (link = process_remote_device(...)) and then dereference it, so returning
        # None here would crash enrich_links_data with an AttributeError and take down the whole
        # Cables tab for LLDP/CDP neighbors that advertise a remote device but no remote port.
        return link

    def check_cable_status(self, link):
        """Check cable status and add cable URL if cable exists in NetBox"""
        local_interface_id = link.get("netbox_local_interface_id")
        remote_interface_id = link.get("netbox_remote_interface_id")

        # Default state
        link["can_create_cable"] = False

        if local_interface_id and remote_interface_id:
            local_interface = Interface.objects.get(pk=local_interface_id)
            remote_interface = Interface.objects.get(pk=remote_interface_id)
            existing_cable = local_interface.cable or remote_interface.cable

            if existing_cable:
                link.update(
                    {
                        "cable_status": "Cable Found",
                        "cable_url": reverse("dcim:cable", args=[existing_cable.pk]),
                    }
                )
            else:
                link["cable_status"] = "No Cable"
                # OOB-controller rows are context-only (shared-LOM detection) and are skipped by
                # SyncCablesView.process_single_interface, so they must never offer a Sync Cable
                # action: an OOB row whose shared-name local port resolves to a host interface
                # would otherwise present a dead button (in both the table render and the verify
                # response, which both gate the action on can_create_cable).
                link["can_create_cable"] = link.get("_source") != "oob"
        else:
            link["cable_status"] = (
                "Both Interfaces Not Found in Netbox"
                if not (local_interface_id or remote_interface_id)
                else "Local Interface Not Found in Netbox"
                if not local_interface_id
                else "Remote Interface Not Found in Netbox"
            )

        return link

    def process_remote_device(self, link, remote_hostname, remote_device_id, server_key=None):
        """Process remote device data and add remote device URL if device exists in NetBox"""
        device, found, error_message = self.get_device_by_id_or_name(
            remote_device_id, remote_hostname, server_key=server_key
        )
        if found:
            link.update(
                {
                    "remote_device_url": reverse("dcim:device", args=[device.pk]),
                    "netbox_remote_device_id": device.pk,
                }
            )
            return self.enrich_remote_port(link, device, server_key=server_key)

        link.update(
            {
                "remote_port_name": link["remote_port"],
                "cable_status": error_message if error_message else "Device Not Found in NetBox",
                "can_create_cable": False,
            }
        )
        return link

    def enrich_links_data(self, links_data, obj, server_key=None):
        """Enrich links data with local and remote port URLs and cable status."""
        for link in links_data:
            self.enrich_local_port(link, obj, server_key=server_key)
            link["device_id"] = obj.id

            if remote_hostname := link.get("remote_device"):
                link = self.process_remote_device(
                    link, remote_hostname, link.get("remote_device_id"), server_key=server_key
                )
                if link.get("netbox_remote_device_id"):
                    link = self.check_cable_status(link)

        return links_data

    def get_table(self, data, obj):
        """Return the cable table for *data*; concrete subclasses choose the table class."""
        raise NotImplementedError

    def _prepare_context(self, request, obj, fetch_fresh=False, server_key=None):
        """Helper method to prepare the context data for cable sync views."""
        table = None
        cache_expiry = None
        # Scoped to the POST-resolved server when provided; else the degrading resolver.
        server_key = server_key or self._render_server_key()
        # For VC devices, cache under the sync device's key so SingleCableVerifyView reads the same entry.
        cache_device = get_librenms_sync_device(obj, server_key=server_key) or obj

        if fetch_fresh:
            # Always fetch new data when requested
            links_data = self.get_links_data(obj, server_key=server_key, sync_device=cache_device)
            # Only a true fetch failure returns None. An empty list ([]) is a valid result
            # (device has no host links) and must flow through: get_links_data() may have
            # collected zero host links yet still set _oob_links_fetch_failed, and post()
            # surfaces that OOB warning only on the success path — `if not links_data`
            # would discard it and mislabel it "No links found".
            if links_data is None:
                return None
        else:
            # Try to use cached data
            links_cache_key = self.get_cache_key(cache_device, "links", server_key)
            cached_links_data = cache.get(links_cache_key)
            if cached_links_data:
                # Fail closed on a malformed/corrupt cache entry (non-dict, non-list "links", or a
                # non-dict link row) instead of crashing the cached render below on .items().
                links_data = _extract_cached_links(cached_links_data, links_cache_key)
                if links_data is None:
                    return None
            else:
                return None

        if not fetch_fresh:
            # Strip derived fields so re-enrichment starts from raw link data;
            # without this, stale IDs/URLs persist when NetBox objects are
            # deleted and cause DoesNotExist in check_cable_status().
            links_data = [{k: v for k, v in link.items() if k in _RAW_LINK_KEYS} for link in links_data]

        # Enrich data in both cases to ensure current NetBox state
        links_data = self.enrich_links_data(links_data, obj, server_key=server_key)

        # Cache after enrichment so verify/sync views read current NetBox state
        cache_key = self.get_cache_key(cache_device, "links", server_key)
        # Don't persist a PARTIAL fresh snapshot: a host fetch failure on a device that has a host
        # id, or any OOB fetch failure, drops one side's cable rows — caching it would make later
        # cached renders / verify actions silently serve the incomplete set. An OOB-only mapping
        # (no host id) legitimately records _links_fetch_error for the absent host, so keep caching
        # that successful OOB refresh (mirrors the host_mapping_absent_but_oob_scoped guard above).
        partial_fetch_failed = fetch_fresh and (
            bool(getattr(self, "_oob_links_fetch_failed", False))
            or (bool(getattr(self, "_links_fetch_error", None)) and getattr(self, "librenms_id", None) is not None)
        )
        if fetch_fresh:
            if partial_fetch_failed:
                # Don't just skip writing the partial snapshot — also drop any prior FULL snapshot.
                # The rendered table is built from the new partial links_data, but verify/sync
                # actions resolve rows from this cache key, so a leftover full snapshot would let a
                # user act on cable data the partial refresh just superseded.
                cache.delete(cache_key)
            else:
                cache.set(
                    cache_key,
                    {"links": links_data},
                    timeout=self.librenms_api.cache_timeout,
                )
        else:
            # Write enriched data back, preserving original TTL
            remaining_ttl = cache_remaining_ttl(cache, cache_key)
            if remaining_ttl and remaining_ttl > 0:
                cache.set(cache_key, {"links": links_data}, timeout=remaining_ttl)

        # Calculate cache expiry
        cache_ttl = None if partial_fetch_failed else cache_remaining_ttl(cache, cache_key)
        if cache_ttl is not None and cache_ttl > 0:
            cache_expiry = timezone.now() + timezone.timedelta(seconds=cache_ttl)
        # Generate the table
        table = self.get_table(links_data, obj)
        # Build the follow-up HTMX URL (pagination/sorting) on the RESOLVED server scope —
        # not the lazy session client, which can point at a different server after a failed
        # rebind or a global switch, silently swapping the dataset mid-view. Set here rather
        # than in a get_table override: DeviceCableTableView overrides get_table without
        # calling super, so a base-class override never ran for the device tab (htmx_url
        # stayed None). quote_plus mirrors ip_addresses_view (a key isn't a guaranteed slug).
        table.htmx_url = f"{request.path}?tab=cables" + (f"&server_key={quote_plus(server_key)}" if server_key else "")

        table.configure(request)

        # Prepare and return the context
        return {
            "table": table,
            "object": obj,
            "cache_expiry": cache_expiry,
            "server_key": server_key,
        }

    def get_context_data(self, request, obj):
        """Get the context data for the cable sync view."""
        # GET render: rebind + scope the cache read to ?server_key (shared helper) so a
        # non-default-server tab reads that server's cache, not the default's.
        scoped, unresolved = self.resolve_get_render_server_key(request)
        if unresolved:
            # The query named a server that no longer resolves (deleted/misconfigured). Its
            # links snapshot may still be cached until TTL — render empty scoped to the
            # requested key instead of serving a removed server's cable rows as a live table
            # (mirrors the interfaces/modules/IP tabs' unresolved guards).
            return {"table": None, "object": obj, "cache_expiry": None, "server_key": scoped}
        context = self._prepare_context(request, obj, fetch_fresh=False, server_key=scoped)
        if context is None:
            # No data found; return context with empty table
            context = {"table": None, "object": obj, "cache_expiry": None, "server_key": scoped}
        return context

    def post(self, request, pk):
        """Handle POST request for cable sync view."""
        obj = self.get_object(pk)
        posted_server_key = request.POST.get("server_key")
        # Rebind the API to the POSTed server so live link/port fetches hit the same
        # LibreNMS instance the cached rows are namespaced under (multi-server tabs).
        server_key = self.rebind_api_for_server(posted_server_key)
        if server_key is None:
            messages.error(request, "Selected LibreNMS server is no longer configured.")
            # rebind_api_for_server() returned None to avoid building a missing/misconfigured
            # default client; reading the lazy `librenms_api` property here would reconstruct it
            # and can raise (a 500 on this HTMX error path). Use the already-cached client's key.
            active_server_key = self.active_server_key
            # render_sync_partial injects the migrated-donor context (resolved from the active
            # session key, since the POSTed key is now known-invalid) so a stale server_key can't
            # silently re-enable cable sync on a migrated donor.
            return self.render_sync_partial(
                request,
                obj,
                active_server_key,
                {"cable_sync": {"object": obj, "table": None, "cache_expiry": None, "server_key": None}},
            )
        context = self._prepare_context(request, obj, fetch_fresh=True, server_key=server_key)

        if context is None:
            # Surface the real fetch failure (auth/network/server) when there was one;
            # only fall back to the empty-result message when the device genuinely has no links.
            if getattr(self, "_links_fetch_error", None):
                messages.error(request, f"Failed to fetch links from LibreNMS: {self._links_fetch_error}")
            else:
                messages.error(request, "No links found in LibreNMS")
            return self.render_sync_partial(
                request,
                obj,
                server_key,
                {"cable_sync": {"object": obj, "table": None, "cache_expiry": None, "server_key": server_key}},
            )

        messages.success(request, "Cable data refreshed successfully.")
        # A host LLDP failure no longer aborts the refresh (OOB/serial rows can still surface it
        # as "successful"), so warn when the host fetch failed but we had a host id to query —
        # otherwise host-side cables are silently omitted under a success banner. Skip for an
        # OOB-only device (librenms_id is None), where a host fetch failure is expected.
        if getattr(self, "_links_fetch_error", None) and getattr(self, "librenms_id", None) is not None:
            logger.warning(
                "Host links fetch failed for device %s: %s",
                self.librenms_id,
                self._links_fetch_error,
            )
            messages.warning(
                request,
                "Cables refreshed, but host links fetch failed; showing available cable rows only. "
                "See server logs for details.",
            )
        if getattr(self, "_oob_links_fetch_failed", False):
            messages.warning(
                request,
                "Cables refreshed, but OOB controller links fetch failed; "
                "showing host cables only. See server logs for details.",
            )
        return self.render_sync_partial(request, obj, server_key, {"cable_sync": context})


class SingleCableVerifyView(BaseCableTableView):
    """
    View to verify a single cable link between two devices.
    """

    # Read-only verify endpoint: require object-view permission (mirrors the interface/module
    # verify views). Without it any user with mere plugin-view rights could POST an arbitrary
    # device id and read back that device's rendered cable/topology rows.
    required_object_permissions = {"POST": [("view", Device)]}

    def post(self, request):
        data, err = parse_request_json(request)
        if err:
            return err
        # Gate BEFORE resolving the device / touching the LibreNMS client: an unauthorized caller
        # must not be able to probe device IDs or trigger work through this endpoint.
        if error := self.require_object_permissions_json("POST"):
            return error
        selected_device_id = data.get("device_id")
        local_port_id = data.get("local_port_id")
        # Read server_key from POST so we use the exact server the user was viewing, but only honour
        # it when it names a configured server: the raw value scopes the links cache and the
        # _librenms_id_q() JSONField lookups below, so a forged/unconfigured key must not address
        # another server's namespace (mirrors the interfaces POST path, issues #108/#109). Fall back
        # to the active server when the POSTed key isn't configured.
        # get_available_servers() is a dict, so the membership test hashes requested_server_key;
        # a forged JSON array/object would raise TypeError (unhashable). Require a str first so a
        # malformed key falls back to the active server instead of crashing the endpoint.
        # Resolve the configured-server set via the CLASSMETHOD, not self.librenms_api: the lazy
        # ``librenms_api`` property builds ``LibreNMSAPI()``, whose constructor raises KeyError/
        # ValueError on a missing/misconfigured default server — so probing membership through the
        # instance would 500 this verify POST exactly where the sibling IP-verify path degrades.
        # get_available_servers() needs no instance.
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        requested_server_key = data.get("server_key")
        if isinstance(requested_server_key, str) and requested_server_key in LibreNMSAPI.get_available_servers():
            server_key = requested_server_key
        else:
            server_key = self._render_server_key()

        formatted_row = {
            "local_port": "",
            "remote_port": "",
            "remote_device": "",
            "cable_status": "Missing Ports",
            "actions": "",
        }

        if selected_device_id:
            # Object-scope the lookup: the gate only checked model-level view_device, so an
            # out-of-scope pk must 404 rather than expose that device's cached cable row.
            selected_device = self.restrict_object_or_404(Device, pk=selected_device_id)

            # Use the same sync-device resolution as the GET path so the cache
            # key matches what _prepare_context wrote. When the VC has no
            # resolvable sync device, return an empty row rather than crashing.
            if selected_device.virtual_chassis:
                primary_device = get_librenms_sync_device(selected_device, server_key=server_key)
                if primary_device is None:
                    return JsonResponse({"status": "success", "formatted_row": formatted_row})
            else:
                primary_device = selected_device

            links_cache_key = self.get_cache_key(primary_device, "links", server_key)
            cached_links = cache.get(links_cache_key)

            # Same fail-closed guard as the cached GET render: a malformed entry (non-dict, non-list
            # "links", or a non-dict link row) is purged and treated as no cache, so the verify path
            # returns the empty formatted_row instead of crashing on .get()/.items().
            valid_links = _extract_cached_links(cached_links, links_cache_key) if cached_links else None
            if valid_links:
                link_data = next(
                    (link for link in valid_links if str(link.get("local_port_id", "")) == str(local_port_id)),
                    None,
                )
                if link_data:
                    # Strip derived fields from cached data to avoid stale
                    # IDs/URLs when NetBox objects are deleted after caching.
                    link_data = {k: v for k, v in link_data.items() if k in _RAW_LINK_KEYS}

                    # The verify response returns formatted_row HTML directly (it does not pass
                    # through LibreNMSCableTable.render_local_port), so re-apply the OOB badge
                    # here to match the initial render — otherwise a verified OOB cable row loses
                    # the badge and looks like a plain host-port row. Same helper as the table
                    # render, so the two can't drift.
                    oob_badge = oob_badge_html(link_data, leading_space=True)

                    # Re-enrich remote side from current NetBox state
                    remote_hostname = link_data.get("remote_device", "")
                    if remote_hostname:
                        link_data = self.process_remote_device(
                            link_data, remote_hostname, link_data.get("remote_device_id"), server_key=server_key
                        )

                    # `or ""` (not a .get default): the OOB-merge path stores local_port=None when
                    # the port name can't be resolved, and a present-but-None value would otherwise
                    # render the literal string "None" via escape() below.
                    local_port = link_data.get("local_port") or ""
                    formatted_row["local_port"] = local_port

                    # Resolve the local interface (handle VC members)
                    interface = None
                    lookup_device = selected_device
                    # Merged OOB-controller rows are context-only: their local port lives on the
                    # CONTROLLER, so a shared name (or colliding stored librenms_id) must not bind
                    # a HOST interface here — mirrors enrich_local_port's guard on the initial
                    # render. Left unresolved, the row takes the labelled, badge-carrying
                    # unresolved branch below instead of linking the wrong interface.
                    if link_data.get("_source") != "oob":
                        if (
                            local_port
                            and hasattr(selected_device, "virtual_chassis")
                            and selected_device.virtual_chassis
                        ):
                            chassis_member = get_virtual_chassis_member(selected_device, local_port)
                            if chassis_member:
                                lookup_device = chassis_member
                        # Shared id→dual-name resolution core (issue #88 fallback included), so
                        # this path can't drift from enrich_local_port's again.
                        name_candidates = [n for n in (local_port, link_data.get("local_port_alt")) if n]
                        interface = _resolve_local_interface(lookup_device, server_key, local_port_id, name_candidates)

                    if interface:
                        link_data["netbox_local_interface_id"] = interface.pk

                        # Check cable status if remote side was resolved
                        if link_data.get("netbox_remote_device_id"):
                            link_data = self.check_cable_status(link_data)

                        # Escape LibreNMS-sourced labels to prevent XSS
                        safe_local_port = escape(local_port)
                        remote_port_name = link_data.get("remote_port_name") or link_data.get("remote_port") or ""
                        safe_remote_port = escape(remote_port_name)
                        remote_device_name = link_data.get("remote_device", "")
                        safe_remote_device = escape(remote_device_name)
                        safe_cable_status = escape(link_data.get("cable_status", "Missing Ports"))

                        formatted_row["cable_status"] = safe_cable_status
                        formatted_row["local_port"] = (
                            f'<a href="{reverse("dcim:interface", args=[interface.pk])}">{safe_local_port}</a>{oob_badge}'
                        )
                        formatted_row["remote_port"] = (
                            f'<a href="{link_data["remote_port_url"]}">{safe_remote_port}</a>'
                            if link_data.get("remote_port_url")
                            else safe_remote_port
                        )
                        formatted_row["remote_device"] = (
                            f'<a href="{link_data["remote_device_url"]}">{safe_remote_device}</a>'
                            if link_data.get("remote_device_url")
                            else safe_remote_device
                        )
                        if link_data.get("cable_url"):
                            formatted_row["cable_status"] = (
                                f'<a href="{link_data["cable_url"]}">{safe_cable_status}</a>'
                            )

                        if link_data.get("can_create_cable"):
                            csrf_token = get_token(request)
                            server_key_input = (
                                f'<input type="hidden" name="server_key" value="{escape(str(server_key))}">'
                                if server_key
                                else ""
                            )
                            formatted_row["actions"] = f"""
                                <form method="post" action="{reverse("plugins:netbox_librenms_plugin:sync_device_cables", args=[selected_device.id])}">
                                    <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
                                    <input type="hidden" name="select" value="{escape(str(local_port_id))}">
                                    {server_key_input}
                                    <button type="submit" class="btn btn-sm btn-primary">Sync Cable</button>
                                </form>
                            """
                    else:
                        formatted_row["local_port"] = f"{escape(local_port)}{oob_badge}"
                        # Keep remote port name visible, add URL if available
                        remote_port_name = link_data.get("remote_port_name") or link_data.get("remote_port") or ""
                        safe_remote_port = escape(remote_port_name)
                        formatted_row["remote_port"] = (
                            f'<a href="{link_data["remote_port_url"]}">{safe_remote_port}</a>'
                            if link_data.get("remote_port_url")
                            else safe_remote_port
                        )
                        # Keep remote device name visible, add URL if available
                        remote_device_name = link_data.get("remote_device", "")
                        safe_remote_device = escape(remote_device_name)
                        formatted_row["remote_device"] = (
                            f'<a href="{link_data["remote_device_url"]}">{safe_remote_device}</a>'
                            if link_data.get("remote_device_url")
                            else safe_remote_device
                        )

                        # First check if remote device exists in NetBox
                        if remote_device_name and not link_data.get("remote_device_url"):
                            formatted_row["cable_status"] = "Device Not Found in NetBox"
                        # Then check interface status
                        elif link_data.get("remote_device_url") and link_data.get("remote_port_url"):
                            formatted_row["cable_status"] = "Local Interface Not Found in NetBox"
                        else:
                            formatted_row["cable_status"] = "Missing Interface"

                        formatted_row["actions"] = ""

        return JsonResponse({"status": "success", "formatted_row": formatted_row})
