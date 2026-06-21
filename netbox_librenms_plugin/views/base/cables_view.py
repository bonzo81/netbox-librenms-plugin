import logging

from dcim.models import Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import MultipleObjectsReturned
from django.db.models import Q
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.views import View

from netbox_librenms_plugin.utils import (
    cache_remaining_ttl,
    get_interface_name_field,
    get_librenms_oob,
    get_librenms_sync_device,
    get_virtual_chassis_member,
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


def _librenms_id_q(server_key: str, value) -> Q:
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

    def _paths(v) -> Q:
        return (
            Q(**{f"custom_field_data__librenms_id__{server_key}": v})
            | Q(**{f"custom_field_data__librenms_id__{server_key}__id": v})
            | Q(**{f"custom_field_data__librenms_id__{server_key}__oob__id": v})
            | Q(custom_field_data__librenms_id=v)
        )

    q = _paths(value)
    try:
        int_val = int(value)
        str_val = str(int_val)
        if int_val != value:  # value was a string; also add the integer variant
            q |= _paths(int_val)
        if str_val != value:  # value was an integer; also add the string variant
            q |= _paths(str_val)
    except (TypeError, ValueError):
        pass
    return q


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


class BaseCableTableView(LibreNMSPermissionMixin, LibreNMSAPIMixin, CacheMixin, View):
    """
    Base view for synchronizing cable information from LibreNMS.
    """

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_cable_sync_content.html"

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine)."""
        return get_object_or_404(self.model, pk=pk)

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
        ports_cache_key = self.get_cache_key(obj, "ports", server_key)
        # Shape-guard the cached entry (mirrors _extract_cached_links): a truthy but
        # malformed value would AttributeError-500 get_links_data's .get("ports") reads.
        cached_data = extract_cached_ports(cache.get(ports_cache_key), ports_cache_key)
        if cached_data:
            return cached_data
        success, data = self.librenms_api.get_ports(self.librenms_id)
        if not success:
            return {"ports": []}
        return data

    def get_links_data(self, obj, server_key=None):
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
        lookup_device = get_librenms_sync_device(obj, server_key=server_key) or obj
        self.librenms_id = self.librenms_api.get_librenms_id(lookup_device)
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
            if not success:
                # A failed fetch may carry the detail under "error" OR "message"; capture either,
                # else post() loses the real reason and can cache an empty "successful" refresh
                # over the existing cable snapshot.
                self._links_fetch_error = (
                    (data.get("error") or data.get("message") or str(data)) if isinstance(data, dict) else str(data)
                )
            elif isinstance(data, dict):
                self._links_fetch_error = (
                    data.get("message") or data.get("error") or "Unexpected response from LibreNMS"
                )
            else:
                self._links_fetch_error = "Unexpected response from LibreNMS (expected an object)."

        interface_name_field = get_interface_name_field(getattr(self, "request", None))
        # The alternate LibreNMS name field: when the user displays ifName, a NetBox
        # interface may still be named from ifDescr (and vice versa). Carrying the alternate
        # name lets enrich_local_port fall back to either field (issue #88).
        alt_name_field = "ifDescr" if interface_name_field == "ifName" else "ifName"
        ports_data = self.get_ports_data(lookup_device, server_key=server_key)
        local_ports_map = {}
        local_ports_alt_map = {}
        ports = ports_data.get("ports", []) if isinstance(ports_data, dict) else []
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
            local_ports_map[port_id] = port_name
            # Only record an alternate that differs from the displayed name (no point matching
            # the same string twice).
            alt_name = port.get(alt_name_field)
            if alt_name and alt_name != port_name:
                local_ports_alt_map[port_id] = alt_name

        # Only consume links when the LLDP fetch was OK. A dict-shaped body can still carry a
        # malformed "links" (null/object); treat that as no links (and record the error)
        # rather than crashing or returning early — the OOB merge below must still run.
        links = data.get("links") if lldp_ok else []
        if not isinstance(links, list):
            self._links_fetch_error = (
                self._links_fetch_error or "Unexpected response from LibreNMS (links must be a list)."
            )
            links = []
        links_data = []
        for link in links:
            if not isinstance(link, dict):
                continue
            link_local_port_id = str(link.get("local_port_id"))
            local_port_name = local_ports_map.get(link_local_port_id)
            if local_port_name is None:
                # Fall back to the LibreNMS-reported local_port name so name-based
                # resolution still works when the ports map misses or ports fetch failed.
                local_port_name = link.get("local_port")
            links_data.append(
                {
                    "local_port": local_port_name,
                    "local_port_alt": local_ports_alt_map.get(link_local_port_id),
                    "local_port_id": link.get("local_port_id"),
                    "remote_port": link.get("remote_port"),
                    "remote_device": link.get("remote_hostname"),
                    "remote_port_id": link.get("remote_port_id"),
                    "remote_device_id": link.get("remote_device_id"),
                    "_source": "main",
                }
            )

        # If an OOB controller is linked, fetch its LLDP links and merge. Reuse the sync
        # device resolved at the top so host + OOB data stay scoped to the same member.
        oob = get_librenms_oob(lookup_device, server_key=server_key)
        if oob and oob.get("id"):
            oob_success, oob_data = self.librenms_api.get_device_links(oob["id"])
            # Mirror the main-device branch: a 200 {"status": "error", ...} body is also a
            # failure (get_device_links returns the raw JSON), not just an "error" key.
            oob_ok = (
                oob_success
                and isinstance(oob_data, dict)
                and oob_data.get("status") != "error"
                and "error" not in oob_data
            )
            if oob_ok:
                # Build a port-id → name map for the OOB device using the same
                # interface_name_field as the main device so names are consistent.
                oob_ports_success, oob_ports_data = self.librenms_api.get_ports(oob["id"])
                oob_local_ports_map = {}
                oob_local_ports_alt_map = {}
                if oob_ports_success and isinstance(oob_ports_data, dict):
                    oob_ports = oob_ports_data.get("ports")
                    for port in oob_ports if isinstance(oob_ports, list) else []:
                        # Skip non-dict rows (see the main-branch guard above) so a
                        # malformed OOB ports payload can't 500 the refresh.
                        if not isinstance(port, dict):
                            continue
                        raw_port_id = port.get("port_id")
                        if raw_port_id is None:
                            continue
                        port_name = port.get(interface_name_field)
                        if port_name is None:
                            continue
                        oob_local_ports_map[str(raw_port_id)] = port_name
                        # Same alternate-field fallback as the main branch (issue #88).
                        alt_name = port.get(alt_name_field)
                        if alt_name and alt_name != port_name:
                            oob_local_ports_alt_map[str(raw_port_id)] = alt_name

                # Same malformed-payload guard as the main branch: oob_data is a dict here,
                # but its "links" can still be null/object. Treat that as an OOB fetch failure
                # (flag it, keep the main-device links already collected) rather than crashing.
                oob_links = oob_data.get("links")
                if not isinstance(oob_links, list):
                    self._oob_links_fetch_failed = True
                    logger.warning(
                        "OOB links fetch returned a malformed payload for device %s (OOB id %s): %s",
                        self.librenms_id,
                        oob["id"],
                        oob_data,
                    )
                    # Don't early-return: fall through to the final failure classification below.
                    # An early `return links_data` on an OOB-only device (empty links_data) would
                    # be read as a *successful* empty refresh and clear cached rows. The final guard
                    # returns None when _oob_links_fetch_failed, preserving the cache.
                else:
                    for link in oob_links:
                        if not isinstance(link, dict):
                            continue
                        oob_port_id = link.get("local_port_id")
                        oob_local_port = oob_local_ports_map.get(str(oob_port_id)) if oob_port_id else None
                        if oob_local_port is None:
                            oob_local_port = link.get("local_port")
                        oob_local_port_alt = oob_local_ports_alt_map.get(str(oob_port_id)) if oob_port_id else None
                        links_data.append(
                            {
                                "local_port": oob_local_port,
                                "local_port_alt": oob_local_port_alt,
                                "local_port_id": oob_port_id,
                                "remote_port": link.get("remote_port"),
                                "remote_device": link.get("remote_hostname"),
                                "remote_port_id": link.get("remote_port_id"),
                                "remote_device_id": link.get("remote_device_id"),
                                "_source": "oob",
                            }
                        )
            else:
                # Don't silently drop OOB cable rows on a fetch failure — flag it so
                # post() can warn the user (this method has no request to message on).
                self._oob_links_fetch_failed = True
                logger.warning(
                    "OOB links fetch failed for device %s (OOB id %s): %s",
                    self.librenms_id,
                    oob["id"],
                    oob_data.get("message") if isinstance(oob_data, dict) else oob_data,
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
            self.librenms_id is None and bool(oob and oob.get("id")) and not self._oob_links_fetch_failed
        )
        if not links_data and self._links_fetch_error and not host_mapping_absent_but_oob_scoped:
            return None
        return links_data

    def get_device_by_id_or_name(self, remote_device_id, hostname, server_key=None):
        """Try to find device in NetBox first by librenms_id custom field, then by name"""
        if server_key is None:
            server_key = self._render_server_key()
        # First try matching by LibreNMS ID
        if remote_device_id is not None:
            try:
                device = Device.objects.get(_librenms_id_q(server_key, remote_device_id))
                return device, True, None
            except Device.DoesNotExist:
                pass
            except MultipleObjectsReturned:
                return (
                    None,
                    False,
                    f"Multiple devices found with the same LibreNMS ID: {remote_device_id}.",
                )

        # Fall back to name matching if no device found by ID
        try:
            device = Device.objects.get(name=hostname)
            return device, True, None
        except Device.DoesNotExist:
            # Try without domain name
            simple_hostname = hostname.split(".")[0]
            try:
                device = Device.objects.get(name=simple_hostname)
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
        if local_port := link.get("local_port"):
            interface = None
            local_port_id = link.get("local_port_id")
            if server_key is None:
                server_key = self._render_server_key()

            # Name fallback is field-agnostic: a NetBox interface may be named from the
            # LibreNMS field the user is *not* currently displaying (issue #88 — e.g. the
            # interface carries the ifDescr value while interface_name_field selects ifName).
            # Match the displayed name plus the alternate field captured at fetch time,
            # mirroring the dual ifName/ifDescr fallback in
            # interfaces_view._enrich_port_with_lag_parent. The stable librenms_id match below
            # still wins when present; this only widens the fragile name fallback.
            name_candidates = [n for n in (local_port, link.get("local_port_alt")) if n]

            if hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                chassis_member = get_virtual_chassis_member(obj, local_port)

                if chassis_member:
                    # First try to find interface by librenms_id
                    if local_port_id:
                        interface = chassis_member.interfaces.filter(_librenms_id_q(server_key, local_port_id)).first()

                    # Only if librenms_id match fails, try matching by name
                    if not interface:
                        interface = chassis_member.interfaces.filter(name__in=name_candidates).first()
            else:
                # First try to find interface by librenms_id
                if local_port_id:
                    interface = obj.interfaces.filter(_librenms_id_q(server_key, local_port_id)).first()

                # Only if librenms_id match fails, try matching by name
                if not interface:
                    interface = obj.interfaces.filter(name__in=name_candidates).first()

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

            # Handle virtual chassis case
            if hasattr(device, "virtual_chassis") and device.virtual_chassis:
                # Get the appropriate chassis member based on the port name
                chassis_member = get_virtual_chassis_member(device, remote_port)

                if chassis_member:
                    # First try to find interface by librenms_id
                    if librenms_remote_port_id:
                        netbox_remote_interface = chassis_member.interfaces.filter(
                            _librenms_id_q(server_key, librenms_remote_port_id)
                        ).first()

                    # If not found by librenms_id, fall back to name matching on the correct chassis member
                    if not netbox_remote_interface:
                        netbox_remote_interface = chassis_member.interfaces.filter(name=remote_port).first()
            else:
                # Non-virtual chassis case
                # First try to find interface by librenms_id
                if librenms_remote_port_id:
                    netbox_remote_interface = device.interfaces.filter(
                        _librenms_id_q(server_key, librenms_remote_port_id)
                    ).first()

                # If not found by librenms_id, fall back to name matching
                if not netbox_remote_interface:
                    netbox_remote_interface = device.interfaces.filter(name=remote_port).first()

            if netbox_remote_interface:
                link["remote_port_url"] = reverse("dcim:interface", args=[netbox_remote_interface.pk])
                link["netbox_remote_interface_id"] = netbox_remote_interface.pk
                link["remote_port_name"] = netbox_remote_interface.name

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
                link.update({"cable_status": "No Cable", "can_create_cable": True})
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
        """Get the table instance for the view."""
        table = super().get_table(data, obj)
        server_key = self._render_server_key()
        table.htmx_url = f"{self.request.path}?tab=cables" + (f"&server_key={server_key}" if server_key else "")
        return table

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
            links_data = self.get_links_data(obj, server_key=server_key)
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
            _raw_keys = {
                "local_port",
                "local_port_alt",
                "local_port_id",
                "remote_port",
                "remote_device",
                "remote_port_id",
                "remote_device_id",
                "_source",
            }
            links_data = [{k: v for k, v in link.items() if k in _raw_keys} for link in links_data]

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
            if not partial_fetch_failed:
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
        context = self._prepare_context(request, obj, fetch_fresh=False)
        if context is None:
            # No data found; return context with empty table
            context = {"table": None, "object": obj, "cache_expiry": None, "server_key": self._render_server_key()}
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
            return render(
                request,
                self.partial_template_name,
                {
                    "cable_sync": {"object": obj, "table": None, "cache_expiry": None, "server_key": None},
                },
            )
        context = self._prepare_context(request, obj, fetch_fresh=True, server_key=server_key)

        if context is None:
            # Surface the real fetch failure (auth/network/server) when there was one;
            # only fall back to the empty-result message when the device genuinely has no links.
            if getattr(self, "_links_fetch_error", None):
                messages.error(request, f"Failed to fetch links from LibreNMS: {self._links_fetch_error}")
            else:
                messages.error(request, "No links found in LibreNMS")
            return render(
                request,
                self.partial_template_name,
                {
                    "cable_sync": {
                        "object": obj,
                        "table": None,
                        "cache_expiry": None,
                        "server_key": server_key,
                    },
                },
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
        return render(
            request,
            self.partial_template_name,
            {"cable_sync": context},
        )


class SingleCableVerifyView(NetBoxObjectPermissionMixin, BaseCableTableView):
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
                    _raw_keys = {
                        "local_port",
                        "local_port_alt",
                        "local_port_id",
                        "remote_port",
                        "remote_device",
                        "remote_port_id",
                        "remote_device_id",
                        "_source",
                    }
                    link_data = {k: v for k, v in link_data.items() if k in _raw_keys}

                    # The verify response returns formatted_row HTML directly (it does not pass
                    # through LibreNMSCableTable.render_local_port), so re-apply the OOB badge
                    # here to match the initial render — otherwise a verified OOB cable row loses
                    # the badge and looks like a plain host-port row. Markup mirrors
                    # tables/cables.py render_local_port.
                    oob_badge = (
                        ' <span class="badge bg-purple text-white ms-1" title="From OOB controller">OOB</span>'
                        if link_data.get("_source") == "oob"
                        else ""
                    )

                    # Re-enrich remote side from current NetBox state
                    remote_hostname = link_data.get("remote_device", "")
                    if remote_hostname:
                        link_data = self.process_remote_device(
                            link_data, remote_hostname, link_data.get("remote_device_id"), server_key=server_key
                        )

                    local_port = link_data.get("local_port", "")
                    formatted_row["local_port"] = local_port

                    # First try to find interface by librenms_id (handle VC members)
                    _sk = server_key
                    interface = None
                    lookup_device = selected_device
                    if local_port and hasattr(selected_device, "virtual_chassis") and selected_device.virtual_chassis:
                        chassis_member = get_virtual_chassis_member(selected_device, local_port)
                        if chassis_member:
                            lookup_device = chassis_member
                    if local_port_id:
                        interface = lookup_device.interfaces.filter(_librenms_id_q(_sk, local_port_id)).first()

                    # If not found by librenms_id, try the displayed name or the alternate
                    # LibreNMS field (issue #88) — mirror enrich_local_port's dual-name fallback
                    # so verify resolves a row whose NetBox interface is named from the field the
                    # user isn't currently displaying (ifName vs ifDescr).
                    if not interface:
                        name_candidates = [n for n in (local_port, link_data.get("local_port_alt")) if n]
                        if name_candidates:
                            interface = lookup_device.interfaces.filter(name__in=name_candidates).first()

                    if interface:
                        link_data["netbox_local_interface_id"] = interface.pk

                        # Check cable status if remote side was resolved
                        if link_data.get("netbox_remote_device_id"):
                            link_data = self.check_cable_status(link_data)

                        # Escape LibreNMS-sourced labels to prevent XSS
                        safe_local_port = escape(local_port)
                        remote_port_name = link_data.get("remote_port_name", link_data.get("remote_port", ""))
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
                        remote_port_name = link_data.get("remote_port_name", link_data.get("remote_port", ""))
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
