import logging
import re
from collections import defaultdict
from urllib.parse import quote_plus
from uuid import uuid4

from dcim.constants import VIRTUAL_IFACE_TYPES
from dcim.models import Cable, ConsolePort, ConsoleServerPort, Device, Interface
from django.contrib import messages
from django.core.cache import cache
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.views import View

from netbox_librenms_plugin.utils import (
    apply_cable_manual_picks,
    assign_cable_row_ids,
    cache_remaining_ttl,
    cable_manual_pick_cache_key,
    cable_snapshot_token,
    build_librenms_id_qs,
    cable_far_terminations,
    cable_has_librenms_tag,
    cable_is_point_to_point,
    cable_path_reaches,
    coerce_librenms_id,
    get_interface_name_field,
    get_librenms_cable_tag,
    get_librenms_device_id,
    get_migrated_to_marker,
    get_librenms_oob,
    get_librenms_sync_device,
    get_virtual_chassis_member,
    oob_badge_html,
    resolve_interface_on_device,
)
from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, request_actor_id
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
    """
    Drop a neighbour row for a sub-unit whose own physical port is reported beside it.

    A router advertises LLDP from the physical port and from each sub-unit configured on
    it, so one local port can report the same neighbour several times. A cable terminates
    on the physical port only, so every sub-unit row renders as a mismatch.

    Masking is deliberately narrow: a sub-unit is dropped only when the SAME local port
    reports its exact parent name on the SAME remote device. A sub-unit reported on its
    own is kept, because it is then the only evidence of that neighbour.
    """

    def group_key(row):
        local_port_id = coerce_librenms_id(row["local_port_id"])
        if local_port_id is None:
            return None
        remote_device = coerce_librenms_id(row["remote_device_id"])
        hostname = row.get("remote_device")
        if remote_device is not None:
            remote_identity = ("id", remote_device)
        elif isinstance(hostname, str) and hostname.strip():
            # The hostname lookup below is name__iexact, so the grouping identity folds case too.
            remote_identity = ("hostname", hostname.strip().casefold())
        else:
            return None
        return local_port_id, remote_identity

    physical_by_group = {}
    for row in rows:
        remote_port = row["remote_port"]
        key = group_key(row)
        if key is not None and isinstance(remote_port, str) and not _SUB_UNIT_RE.match(remote_port):
            physical_by_group.setdefault(key, set()).add(remote_port)

    kept = []
    for row in rows:
        remote_port = row["remote_port"]
        match = _SUB_UNIT_RE.match(remote_port) if isinstance(remote_port, str) else None
        if match and match.group("physical") in physical_by_group.get(group_key(row), ()):
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


# The raw (un-enriched) link fields a cached/replayed link is stripped down to before
# re-enrichment — derived fields (netbox_*_id, *_url, cable_status, …) are dropped so stale
# IDs/URLs can't cause DoesNotExist after the underlying NetBox objects are deleted. Defined
# once so the strip in _prepare_context and in SingleCableVerifyView stay in lock-step.
_RAW_LINK_KEYS = frozenset(
    {
        "local_port",
        "local_port_alt",
        "local_port_id",
        "row_id",
        "link_id",
        "protocol",
        "remote_port",
        "remote_device",
        "remote_port_id",
        "remote_device_id",
        "_source",
        # Serial-specific source fields (not derived; must survive the cache strip so the
        # read-only Avocent serial rows on the Cables tab re-render after a cached replay).
        "sensor_id",
        "sensor_index_int",
        "is_configured",
        # device_id must also survive the strip for serial rows: enrich_links_data re-sets it to
        # obj.id for host/OOB rows, but the serial branch `continue`s before that (a serial row must
        # keep its CSP-owning sync device_id, not obj.id). Dropping it here would leave a cached
        # serial row with no device_id, and the Cables-tab render reads record["device_id"]
        # (tables/cables.py) — a KeyError that 500s the whole tab on any cached replay.
        "device_id",
    }
)


class BaseCableTableView(
    NetBoxObjectPermissionMixin,
    LibreNMSPermissionMixin,
    LibreNMSAPIMixin,
    CacheMixin,
    View,
):
    """Base view for synchronizing cable information from LibreNMS."""

    model = None  # To be defined in subclasses
    partial_template_name = "netbox_librenms_plugin/_cable_sync_content.html"
    required_object_permissions = {"GET": [("view", Device)], "POST": [("view", Device)]}

    def get_object(self, pk):
        """Retrieve the object (Device or VirtualMachine)."""
        return self.restrict_object_or_404(self.model, pk=pk)

    def _viewable_queryset(self, model):
        """Return a read-scoped queryset during a real authenticated request."""
        user = getattr(getattr(self, "request", None), "user", None)
        if getattr(user, "is_authenticated", False) is True:
            return model.objects.restrict(user, "view")
        return model.objects.all()

    def _changeable_queryset(self, model):
        """Return a change-scoped queryset during a real authenticated request."""
        user = getattr(getattr(self, "request", None), "user", None)
        if getattr(user, "is_authenticated", False) is True:
            return model.objects.restrict(user, "change")
        return model.objects.all()

    def _object_is_viewable(self, obj):
        """Return whether the request user may view one current NetBox object."""
        return self._viewable_queryset(type(obj)).filter(pk=obj.pk).exists()

    def _viewable_sync_device(self, obj, server_key):
        """
        Return the chassis sync owner only when the request may view it.

        A chassis with no resolvable sync member falls back to the page object, which the caller
        already resolved through a restricted queryset. A member that resolves but is out of the
        request's scope returns None instead, so the page cannot act under a hidden owner.

        Args:
            obj: The page object (Device or VirtualMachine) the tab was opened on.
            server_key (str): LibreNMS server key scoping the sync-member lookup.

        Returns:
            Device | None: The viewable sync owner, or None when it is out of scope.
        """
        sync_device = get_librenms_sync_device(obj, server_key=server_key) or obj
        if not self._object_is_viewable(sync_device):
            return None
        return sync_device

    def _apply_termination_change_scope(self, links, preloaded_changeable_ids=None):
        """Disable cable writes unless both resolved terminations are changeable."""
        required_ids = defaultdict(set)
        row_requirements = []
        for link in links:
            requirements = []
            if link.get("can_create_cable"):
                if link.get("_source") == "serial":
                    requirements = [
                        (ConsoleServerPort, coerce_librenms_id(link.get("netbox_local_interface_id"))),
                        (ConsolePort, coerce_librenms_id(link.get("netbox_remote_interface_id"))),
                    ]
                else:
                    requirements = [
                        (Interface, coerce_librenms_id(link.get("netbox_local_interface_id"))),
                        (Interface, coerce_librenms_id(link.get("netbox_remote_interface_id"))),
                    ]
                for model, object_id in requirements:
                    if object_id is not None:
                        required_ids[model].add(object_id)
            row_requirements.append((link, requirements))

        changeable_ids = {model: set(object_ids) for model, object_ids in (preloaded_changeable_ids or {}).items()}
        for model, object_ids in required_ids.items():
            if model not in changeable_ids:
                changeable_ids[model] = set(
                    self._changeable_queryset(model).filter(pk__in=object_ids).values_list("pk", flat=True)
                )
        for link, requirements in row_requirements:
            if requirements and any(
                object_id is None or object_id not in changeable_ids.get(model, set())
                for model, object_id in requirements
            ):
                link["can_create_cable"] = False

    def _cable_has_provenance(self, cable):
        """Check the request's provenance tag without repeating settings/tag queries per row."""
        if not hasattr(self, "_cable_provenance_tag"):
            self._cable_provenance_tag = get_librenms_cable_tag(create=False)
        return cable_has_librenms_tag(cable, self._cable_provenance_tag)

    def _build_trace_visibility(self, paths):
        """Resolve every object permission used by a page of cable traces in bounded queries."""
        object_ids = defaultdict(set)
        for path in paths:
            for near, segment_cables, far in path or []:
                for termination in (*list(near or []), *list(far or [])):
                    object_ids[type(termination)].add(termination.pk)
                    device = getattr(termination, "device", None)
                    if device is not None:
                        object_ids[type(device)].add(device.pk)
                cables = segment_cables if isinstance(segment_cables, (list, tuple)) else [segment_cables]
                for cable in cables:
                    if cable is not None:
                        object_ids[type(cable)].add(cable.pk)
        return {
            model: set(self._viewable_queryset(model).filter(pk__in=ids).values_list("pk", flat=True))
            for model, ids in object_ids.items()
        }

    def _trace_is_viewable(self, path, visible_ids=None):
        """Return whether every cable, termination, and owning device in a trace is viewable."""

        def is_viewable(obj):
            if visible_ids is None:
                return self._object_is_viewable(obj)
            return obj.pk in visible_ids.get(type(obj), set())

        for near, segment_cables, far in path or []:
            for termination in (*list(near or []), *list(far or [])):
                if not is_viewable(termination):
                    return False
                device = getattr(termination, "device", None)
                if device is not None and not is_viewable(device):
                    return False
            cables = segment_cables if isinstance(segment_cables, (list, tuple)) else [segment_cables]
            if any(cable is not None and not is_viewable(cable) for cable in cables):
                return False
        return True

    @staticmethod
    def _first_catalog_match(matches, visible_ids):
        """Return one visible catalog match, while hidden or duplicate evidence blocks fallback."""
        if len(matches) != 1:
            return None, bool(matches)
        match = matches[0]
        return (match if match.pk in visible_ids else None), True

    def _build_normal_link_context(self, links, obj, server_key):  # noqa: C901
        """Load normal LLDP/CDP resolution and permission candidates once per snapshot."""
        normal_links = [link for link in links if link.get("_source") != "serial"]
        if not normal_links:
            return None

        remote_names = {name for link in normal_links if isinstance((name := link.get("remote_device")), str) and name}
        remote_names.update(name.split(".")[0] for name in tuple(remote_names))
        remote_ids = {
            remote_id
            for link in normal_links
            if (remote_id := coerce_librenms_id(link.get("remote_device_id"))) is not None
        }
        device_pks = set(Device.objects.filter(name__in=remote_names).values_list("pk", flat=True))
        sorted_remote_ids = sorted(remote_ids)
        for offset in range(0, len(sorted_remote_ids), 32):
            id_q = Q(pk__isnull=True) & Q(pk__isnull=False)
            for remote_id in sorted_remote_ids[offset : offset + 32]:
                id_q |= _librenms_id_q(server_key, remote_id, include_oob=False)
            device_pks.update(Device.objects.filter(id_q).values_list("pk", flat=True))
        catalog_devices = list(Device.objects.filter(pk__in=device_pks).select_related("virtual_chassis"))
        visible_device_ids = set(
            self._viewable_queryset(Device)
            .filter(pk__in=[device.pk for device in catalog_devices])
            .values_list("pk", flat=True)
        )
        devices_by_name = defaultdict(list)
        devices_by_librenms_id = defaultdict(list)
        for device in catalog_devices:
            devices_by_name[device.name].append(device)
            device_librenms_id = get_librenms_device_id(device, server_key, auto_save=False)
            if device_librenms_id is not None:
                devices_by_librenms_id[device_librenms_id].append(device)

        remote_device_by_link = {}
        for link in normal_links:
            remote_device = None
            blocked = False
            remote_id = coerce_librenms_id(link.get("remote_device_id"))
            if remote_id is not None:
                remote_device, blocked = self._first_catalog_match(
                    devices_by_librenms_id.get(remote_id, []), visible_device_ids
                )
            hostname = link.get("remote_device")
            if not blocked and isinstance(hostname, str) and hostname:
                remote_device, blocked = self._first_catalog_match(
                    devices_by_name.get(hostname, []), visible_device_ids
                )
                if not blocked:
                    remote_device, _blocked = self._first_catalog_match(
                        devices_by_name.get(hostname.split(".")[0], []), visible_device_ids
                    )
            remote_device_by_link[id(link)] = remote_device

        remote_chassis_ids = {
            device.virtual_chassis_id
            for device in remote_device_by_link.values()
            if device is not None and device.virtual_chassis_id is not None
        }
        remote_members_by_chassis = defaultdict(dict)
        if remote_chassis_ids:
            for member in Device.objects.filter(virtual_chassis_id__in=remote_chassis_ids):
                if member.vc_position is not None:
                    remote_members_by_chassis[member.virtual_chassis_id][member.vc_position] = member
        remote_owner_by_link = {}
        for link in normal_links:
            remote_device = remote_device_by_link.get(id(link))
            if remote_device is None:
                remote_owner = None
            elif remote_device.virtual_chassis_id is None:
                remote_owner = remote_device
            else:
                remote_owner = get_virtual_chassis_member(
                    remote_device,
                    link.get("remote_port"),
                    members_by_position=remote_members_by_chassis[remote_device.virtual_chassis_id],
                    return_device_on_failure=False,
                )
            remote_owner_by_link[id(link)] = remote_owner

        if getattr(obj, "virtual_chassis_id", None):
            chassis_members = list(obj.virtual_chassis.members.all())
            members_by_position = {
                member.vc_position: member for member in chassis_members if member.vc_position is not None
            }
        else:
            chassis_members = [obj]
            members_by_position = {}
        local_owner_by_link = {}
        for link in normal_links:
            local_owner_by_link[id(link)] = get_virtual_chassis_member(
                obj,
                link.get("local_port"),
                members_by_position=members_by_position,
            )

        manual_ids = {
            manual_id
            for link in normal_links
            if (manual_id := coerce_librenms_id(link.get("manual_remote_id"))) is not None
        }
        owner_ids = {device.pk for device in chassis_members}
        owner_ids.update(device.pk for device in remote_owner_by_link.values() if device is not None)
        candidate_specs = defaultdict(lambda: {"names": set(), "ids": set()})
        for link in normal_links:
            local_owner = local_owner_by_link.get(id(link))
            if local_owner is not None:
                candidate_specs[local_owner.pk]["names"].update(
                    name
                    for name in (link.get("local_port"), link.get("local_port_alt"))
                    if isinstance(name, str) and name
                )
                if (local_id := coerce_librenms_id(link.get("local_port_id"))) is not None:
                    candidate_specs[local_owner.pk]["ids"].add(local_id)
            remote_owner = remote_owner_by_link.get(id(link))
            if remote_owner is not None:
                remote_name = link.get("remote_port")
                if isinstance(remote_name, str) and remote_name:
                    candidate_specs[remote_owner.pk]["names"].add(remote_name)
                if (remote_id := coerce_librenms_id(link.get("remote_port_id"))) is not None:
                    candidate_specs[remote_owner.pk]["ids"].add(remote_id)

        candidate_pks = set(manual_ids)
        named_candidate_items = [
            (owner_id, evidence["names"]) for owner_id, evidence in sorted(candidate_specs.items()) if evidence["names"]
        ]
        for offset in range(0, len(named_candidate_items), 64):
            candidate_q = Q(pk__isnull=True) & Q(pk__isnull=False)
            for owner_id, names in named_candidate_items[offset : offset + 64]:
                candidate_q |= Q(device_id=owner_id, name__in=names)
            candidate_pks.update(Interface.objects.filter(candidate_q).values_list("pk", flat=True))
        id_candidate_items = [
            (owner_id, port_id)
            for owner_id, evidence in sorted(candidate_specs.items())
            for port_id in sorted(evidence["ids"])
        ]
        for offset in range(0, len(id_candidate_items), 32):
            candidate_q = Q(pk__isnull=True) & Q(pk__isnull=False)
            for owner_id, port_id in id_candidate_items[offset : offset + 32]:
                candidate_q |= Q(device_id=owner_id) & _librenms_id_q(server_key, port_id)
            candidate_pks.update(Interface.objects.filter(candidate_q).values_list("pk", flat=True))
        catalog_interfaces = list(
            Interface.objects.filter(pk__in=candidate_pks)
            .select_related("device", "cable")
            .prefetch_related("cable__tags", "cable__terminations__termination")
        )
        owner_ids.update(interface.device_id for interface in catalog_interfaces if interface.pk in manual_ids)
        interface_ids_by_device = defaultdict(list)
        interfaces_by_name = defaultdict(list)
        interfaces_by_pk = {}
        for interface in catalog_interfaces:
            interfaces_by_pk[interface.pk] = interface
            interfaces_by_name[(interface.device_id, interface.name)].append(interface)
            interface_librenms_id = get_librenms_device_id(interface, server_key, auto_save=False)
            if interface_librenms_id is not None:
                interface_ids_by_device[(interface.device_id, interface_librenms_id)].append(interface)
        visible_interface_ids = set(
            self._viewable_queryset(Interface).filter(pk__in=interfaces_by_pk).values_list("pk", flat=True)
        )
        visible_owner_ids = set(self._viewable_queryset(Device).filter(pk__in=owner_ids).values_list("pk", flat=True))
        cable_ids = {interface.cable_id for interface in catalog_interfaces if interface.cable_id is not None}
        visible_cable_ids = set(self._viewable_queryset(Cable).filter(pk__in=cable_ids).values_list("pk", flat=True))
        context = {
            "local_owner_by_link": local_owner_by_link,
            "remote_device_by_link": remote_device_by_link,
            "remote_owner_by_link": remote_owner_by_link,
            "interfaces_by_pk": interfaces_by_pk,
            "interfaces_by_name": interfaces_by_name,
            "interface_ids_by_device": interface_ids_by_device,
            "visible_interface_ids": visible_interface_ids,
            "visible_owner_ids": visible_owner_ids,
            "visible_cable_ids": visible_cable_ids,
        }
        trace_paths = {}
        for link in normal_links:
            local_interface = self._resolve_context_interface(
                context,
                local_owner_by_link.get(id(link)),
                link.get("local_port_id"),
                [link.get("local_port"), link.get("local_port_alt")],
            )
            manual_id = coerce_librenms_id(link.get("manual_remote_id"))
            if manual_id is not None:
                remote_interface = interfaces_by_pk.get(manual_id)
            else:
                remote_interface = self._resolve_context_interface(
                    context,
                    remote_owner_by_link.get(id(link)),
                    link.get("remote_port_id"),
                    [link.get("remote_port")],
                )
            if (
                local_interface is not None
                and remote_interface is not None
                and local_interface.cable_id is not None
                and remote_interface.cable_id is not None
                and local_interface.cable_id != remote_interface.cable_id
                and local_interface.cable_id in visible_cable_ids
                and remote_interface.cable_id in visible_cable_ids
            ):
                if local_interface.pk not in trace_paths:
                    trace_paths[local_interface.pk] = local_interface.trace()
        context["trace_paths"] = trace_paths
        context["trace_visibility"] = self._build_trace_visibility(trace_paths.values())
        return context

    @staticmethod
    def _resolve_context_interface(context, device, port_id, name_candidates):
        """Resolve one prefetched Interface with the existing stable-ID-first precedence."""
        if context is None or device is None or device.pk not in context["visible_owner_ids"]:
            return None
        normalized_id = coerce_librenms_id(port_id)
        if normalized_id is not None:
            matches = context["interface_ids_by_device"].get((device.pk, normalized_id), [])
            if len(matches) == 1:
                interface = matches[0]
                return interface if interface.pk in context["visible_interface_ids"] else None
            if matches:
                return None
        candidate_names = {name for name in name_candidates if isinstance(name, str) and name}
        matches = {
            interface.pk: interface
            for (device_id, name), named_interfaces in context["interfaces_by_name"].items()
            if device_id == device.pk and name in candidate_names
            for interface in named_interfaces
        }
        if len(matches) != 1:
            return None
        interface = next(iter(matches.values()))
        return interface if interface.pk in context["visible_interface_ids"] else None

    def get_ip_address(self, obj):
        """Get the primary IP address for the object."""
        if obj.primary_ip:
            return str(obj.primary_ip.address.ip)
        return None

    def get_ports_data(self, obj, server_key=None):
        """Get ports data without affecting cache."""
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

        Args:
            ports_data (dict): A get_ports payload.
            interface_name_field (str): The field that supplies each displayed port name.
            alt_name_field (str): The field that supplies alternate port names.

        Returns:
            tuple[dict, dict]: Maps from port IDs to displayed names and alternate names.
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

        Args:
            links (list): LibreNMS LLDP link rows.
            name_map (dict): A map from local port IDs to displayed names.
            alt_map (dict): A map from local port IDs to alternate names.
            source (str): The source tag for each table row.

        Returns:
            list[dict]: Table rows tagged with the source.
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
                    "link_id": link.get("id"),
                    "protocol": link.get("protocol"),
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

        Args:
            links_data (list): The table rows to extend with OOB links.
            lookup_device (Device): The device that can have a linked OOB controller.
            server_key (str): The LibreNMS server key for the OOB lookup.
            interface_name_field (str): The field that supplies each displayed port name.
            alt_name_field (str): The field that supplies alternate port names.

        Returns:
            bool: True when an OOB controller is linked, otherwise False.
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
        # Same for a serial-sensor fetch failure: flag it so post() can warn rather than
        # silently dropping serial rows under a success banner (parity with OOB).
        self._serial_links_fetch_failed = False
        self._serial_source_skipped = False
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
        lookup_device = sync_device or self._viewable_sync_device(obj, server_key)
        if lookup_device is None:
            return None
        # coerce_librenms_id fails closed on a bool/zero/negative/garbage value (a poisoned
        # id-cache can return True/0 verbatim — get_librenms_id only coerces the custom-field
        # and discovery paths), so a falsy non-None id resolves to None here instead of being
        # passed to get_device_links()/get_ports() as device id 1 (int(True)) — fetching a
        # stranger's links. Mirrors the GET/interfaces-POST contract.
        self.librenms_id, lookup_error = self.resolve_librenms_id(lookup_device)
        self._librenms_id_unresolved = lookup_error is not None
        if lookup_error is not None:
            self._links_fetch_error = lookup_error.message
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
            self._links_fetch_error = self._links_fetch_error or self._classify_links_fetch_error(success, data)

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

        # Append serial rows only when this request can view at least one ConsoleServerPort on the
        # sync device. The sensor endpoint is instance-wide, so a plain Device grant must not
        # authorize that expensive inventory request. Store the complete raw sensor response when
        # the request is allowed to fetch it; request-local enrichment filters individual CSP rows.
        viewable_serial_port_names = (
            set(self._viewable_queryset(ConsoleServerPort).filter(device=lookup_device).values_list("name", flat=True))
            if isinstance(lookup_device, Device)
            else set()
        )
        if isinstance(lookup_device, Device) and not viewable_serial_port_names:
            self._serial_source_skipped = ConsoleServerPort.objects.filter(device=lookup_device).exists()
        if self.librenms_id is not None and viewable_serial_port_names:
            from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

            serial_success, serial_sensors = self.librenms_api.get_serial_port_sensors(self.librenms_id)
            if not serial_success:
                # Don't silently drop serial rows on a fetch failure — flag it so post() can warn
                # the user (this method has no request to message on). Mirrors the OOB branch.
                self._serial_links_fetch_failed = True
                logger.warning(
                    "Serial port sensor fetch failed for device %s: %s",
                    self.librenms_id,
                    serial_sensors,
                )
            elif serial_sensors and isinstance(serial_sensors, list):
                # get_serial_port_sensors() guarantees a list on success, but guard the call site
                # with isinstance(list) like every other LibreNMS payload handled in this method
                # (host links, OOB ports, OOB links): a malformed non-list success payload is then
                # skipped rather than iterated — a non-iterable would otherwise crash mapping.
                links_data.extend(map_sensors_to_serial_links(serial_sensors, device_id=lookup_device.id))

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
            self.librenms_id is None and lookup_error is None and oob_linked and not self._oob_links_fetch_failed
        )
        if (
            not links_data
            and self._links_fetch_error
            and not host_mapping_absent_but_oob_scoped
            and not self._serial_source_skipped
        ):
            return None
        return links_data

    def get_device_by_id_or_name(self, remote_device_id, hostname, server_key=None, queryset=None):
        """Try to find device in NetBox first by librenms_id custom field, then by name."""
        if server_key is None:
            server_key = self._render_server_key()
        queryset = queryset if queryset is not None else Device.objects.all()

        def resolve_catalog_match(catalog, ambiguous_message):
            match_ids = list(catalog.order_by("pk").values_list("pk", flat=True)[:2])
            if len(match_ids) > 1:
                return None, False, ambiguous_message, True
            if not match_ids:
                return None, False, None, False
            device = queryset.filter(pk=match_ids[0]).first()
            if device is None:
                # A stronger catalog match exists but is outside the requester's scope. Do not
                # fall through to a weaker name match and bind a different visible device.
                return None, False, None, True
            return device, True, None, True

        # First try matching by LibreNMS ID. The remote device_id is the remote device's OWN
        # identity, so exclude the OOB-controller path: matching it would also select a different
        # device that references this id as its controller, tripping MultipleObjectsReturned.
        if remote_device_id is not None:
            result = resolve_catalog_match(
                Device.objects.filter(_librenms_id_q(server_key, remote_device_id, include_oob=False)),
                f"Multiple devices found with the same LibreNMS ID: {remote_device_id}.",
            )
            if result[3]:
                return result[:3]

        if not isinstance(hostname, str) or not hostname.strip():
            return None, False, None
        # The grouping identity trims, so the lookups below must see the same value or a padded
        # hostname groups with its neighbour yet still reports "Device Not Found in NetBox".
        hostname = hostname.strip()

        # Fall back to name matching if no device found by ID. LibreNMS reports the neighbour
        # hostname as the device advertises it, which is commonly all lower case, while NetBox
        # holds the operator's capitalisation. Match case insensitively or the remote end only
        # ever resolves through librenms_id.
        if not isinstance(hostname, str) or not hostname:
            return None, False, None
        ambiguous_name = f"Multiple devices found with the same name: {hostname}."
        exact_result = resolve_catalog_match(Device.objects.filter(name__iexact=hostname), ambiguous_name)
        if exact_result[3]:
            return exact_result[:3]
        simple_hostname = hostname.split(".")[0]
        simple_result = resolve_catalog_match(Device.objects.filter(name__iexact=simple_hostname), ambiguous_name)
        return simple_result[:3]

    def enrich_local_port(
        self,
        link,
        obj,
        server_key=None,
        sync_device=None,
        serial_ports_by_name=None,
        normal_context=None,
    ):
        """
        Add the local-port URL if the interface/CSP exists in NetBox.

        Returns the resolved ConsoleServerPort for a serial row (so the caller can reuse it for
        the cable-status check without a second fetch by pk), else None.

        Args:
            link (dict): The cable row to enrich.
            obj (Device): The device shown on the cable page.
            server_key (str | None): The LibreNMS server key, or None to resolve it.
            sync_device (Device | None): The pre-resolved LibreNMS sync device.
            serial_ports_by_name (dict[str, ConsoleServerPort] | None): Preloaded serial ports
                keyed by name.
            normal_context (dict | None): Preloaded interface and chassis lookup data.

        Returns:
            ConsoleServerPort | None: The resolved serial port for a serial row, or None.
        """
        # Merged OOB-controller rows are context-only: their local port lives on the
        # CONTROLLER, not the host, so a shared name (or colliding stored librenms_id)
        # must not bind a host interface — that would render a wrong local_port_url and
        # cable state. Sync and the actions column already refuse OOB rows; leave the
        # local end unresolved here too.
        if link.get("_source") == "oob":
            return None
        if local_port := link.get("local_port"):
            # Serial rows map to ConsoleServerPort, not Interface. Resolve the CSP on the
            # LibreNMS sync device, not the viewed obj/selected_device: get_links_data builds
            # serial rows from get_librenms_sync_device(obj), so on a VC-member page the CSP
            # lives on the priority member — querying obj would drop the row to "Console Server
            # Port Not Found" and lose its Sync Cable action. sync_device may be passed in by the
            # caller (resolved once for the whole links loop) to avoid re-resolving it per row.
            if link.get("_source") == "serial":
                if server_key is None:
                    server_key = self.librenms_api.server_key
                if sync_device is None:
                    sync_device = self._viewable_sync_device(obj, server_key)
                if sync_device is None:
                    return None
                csp = (
                    serial_ports_by_name.get(local_port)
                    if serial_ports_by_name is not None
                    else self._viewable_queryset(ConsoleServerPort).filter(device=sync_device, name=local_port).first()
                )
                if csp:
                    link["local_port_url"] = reverse("dcim:consoleserverport", args=[csp.pk])
                    link["netbox_local_interface_id"] = csp.pk
                    link["netbox_local_device_id"] = csp.device_id
                return csp

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

            if normal_context is not None:
                chassis_member = normal_context["local_owner_by_link"].get(id(link))
                interface = self._resolve_context_interface(
                    normal_context,
                    chassis_member,
                    local_port_id,
                    name_candidates,
                )
            elif hasattr(obj, "virtual_chassis") and obj.virtual_chassis:
                chassis_member = get_virtual_chassis_member(obj, local_port)

                if chassis_member:
                    interface = resolve_interface_on_device(
                        chassis_member,
                        server_key,
                        local_port_id,
                        name_candidates,
                    )
            else:
                interface = resolve_interface_on_device(obj, server_key, local_port_id, name_candidates)

            if interface:
                if normal_context is None and not self._object_is_viewable(interface):
                    return None
                link["local_port_url"] = reverse("dcim:interface", args=[interface.pk])
                link["netbox_local_interface_id"] = interface.pk
                link["netbox_local_device_id"] = interface.device_id

    def enrich_remote_port(self, link, device, server_key=None, normal_context=None):
        """Add remote port URL if device and interface exist in NetBox."""
        remote_port = link.get("remote_port")
        if isinstance(remote_port, str) and remote_port:
            netbox_remote_interface = None
            librenms_remote_port_id = link.get("remote_port_id")
            if server_key is None:
                server_key = self._render_server_key()

            # Same id-beats-name resolution as the local end — reuse the shared resolver so a fix
            # in one path can't miss the other again (the drift risk _resolve_local_interface was
            # extracted to close). VC-member selection stays here because the remote side leaves the
            # interface unresolved when the member lookup fails.
            if normal_context is not None:
                netbox_remote_interface = self._resolve_context_interface(
                    normal_context,
                    normal_context["remote_owner_by_link"].get(id(link)),
                    librenms_remote_port_id,
                    [remote_port],
                )
            elif hasattr(device, "virtual_chassis") and device.virtual_chassis:
                chassis_member = get_virtual_chassis_member(
                    device,
                    remote_port,
                    return_device_on_failure=False,
                )
                if chassis_member:
                    netbox_remote_interface = resolve_interface_on_device(
                        chassis_member, server_key, librenms_remote_port_id, [remote_port]
                    )
            else:
                netbox_remote_interface = resolve_interface_on_device(
                    device, server_key, librenms_remote_port_id, [remote_port]
                )

            if netbox_remote_interface:
                if normal_context is None and (
                    not self._object_is_viewable(netbox_remote_interface)
                    or not self._viewable_queryset(Device).filter(pk=netbox_remote_interface.device_id).exists()
                ):
                    return link
                link["remote_port_url"] = reverse("dcim:interface", args=[netbox_remote_interface.pk])
                link["netbox_remote_interface_id"] = netbox_remote_interface.pk
                link["netbox_remote_device_id"] = netbox_remote_interface.device_id
                link["remote_device_display"] = netbox_remote_interface.device.name
                link["remote_device_url"] = reverse("dcim:device", args=[netbox_remote_interface.device_id])
                link["remote_port_name"] = netbox_remote_interface.name

        # Return the link even when remote_port is empty (or unresolved): callers assign the
        # result back (link = process_remote_device(...)) and then dereference it, so returning
        # None here would crash enrich_links_data with an AttributeError and take down the whole
        # Cables tab for LLDP/CDP neighbors that advertise a remote device but no remote port.
        return link

    def check_cable_status(self, link, normal_context=None):
        """
        Check cable status against the LibreNMS-desired connection and set the sync affordance.

        States (``cable_status`` / ``can_create_cable``):

        - ``"No Cable"`` / True: neither end cabled; sync creates the cable.
        - ``"Cable Found"`` / untagged-only: the desired connection is already cabled directly.
          When the cable lacks the librenms tag a sync merely adopts it (``tag_only`` in
          :func:`~netbox_librenms_plugin.utils.classify_cable_action`); when already tagged
          there is nothing to do.
        - ``"Connected via Patch Path"`` / False: both ends cabled and the traced path reaches
          the LibreNMS target through patch panels: a remodel is a better model of the same
          link, so no re-sync is offered.
        - ``"Cable Mismatch"`` / True: cabled somewhere that does NOT reach the target; a
          re-sync is offered and the exact current cable must be confirmed before replacement.

        Args:
            link (dict): The cable row to update.
            normal_context (dict | None): Preloaded interface, cable, and trace data.

        Returns:
            dict: The cable row with its status and sync affordance.
        """
        local_interface_id = link.get("netbox_local_interface_id")
        remote_interface_id = link.get("netbox_remote_interface_id")

        # Default state. OOB-controller rows are context-only (shared-LOM detection) and are
        # skipped by SyncCablesView.process_single_interface, so they must never offer a Sync
        # Cable action in ANY state: an OOB row whose shared-name local port resolves to a host
        # interface would otherwise present a dead button (in both the table render and the
        # verify response, which both gate the action on can_create_cable).
        link["can_create_cable"] = False
        actionable = link.get("_source") != "oob"

        if local_interface_id and remote_interface_id:
            if normal_context is not None:
                local_interface = normal_context["interfaces_by_pk"].get(local_interface_id)
                remote_interface = normal_context["interfaces_by_pk"].get(remote_interface_id)
                visible_interface_ids = normal_context["visible_interface_ids"]
                if (
                    local_interface is None
                    or remote_interface is None
                    or local_interface.pk not in visible_interface_ids
                    or remote_interface.pk not in visible_interface_ids
                ):
                    link["cable_status"] = "Interface Not Available"
                    return link
            else:
                try:
                    local_interface = self._viewable_queryset(Interface).get(pk=local_interface_id)
                    remote_interface = self._viewable_queryset(Interface).get(pk=remote_interface_id)
                except Interface.DoesNotExist:
                    link["cable_status"] = "Interface Not Available"
                    return link
            local_cable = local_interface.cable
            remote_cable = remote_interface.cable
            if normal_context is not None:
                cable_state_hidden = any(
                    cable is not None and cable.pk not in normal_context["visible_cable_ids"]
                    for cable in {local_cable, remote_cable}
                )
            else:
                cable_state_hidden = any(
                    cable is not None and not self._object_is_viewable(cable) for cable in {local_cable, remote_cable}
                )
            if cable_state_hidden:
                link["cable_status"] = "Cable State Not Available"
                return link

            unsupported_cable = next(
                (
                    cable
                    for cable in (local_cable, remote_cable)
                    if cable is not None and not cable_is_point_to_point(cable)
                ),
                None,
            )
            if unsupported_cable is not None:
                link.update(
                    {
                        "cable_status": "Multi-termination Cable Not Supported",
                        "cable_url": reverse("dcim:cable", args=[unsupported_cable.pk]),
                        "_multi_termination_unsupported": True,
                    }
                )
                return link

            if local_cable is None and remote_cable is None:
                link["cable_status"] = "No Cable"
                link["can_create_cable"] = actionable
            elif local_cable is not None and local_cable == remote_cable:
                # The desired connection is already cabled directly. Offer a sync only while
                # the cable is untagged — it then just adopts (tags) the cable, never recreates.
                link.update(
                    {
                        "cable_status": "Cable Found",
                        "cable_url": reverse("dcim:cable", args=[local_cable.pk]),
                    }
                )
                link["can_create_cable"] = actionable and not self._cable_has_provenance(local_cable)
            elif local_cable is not None and remote_cable is not None:
                path = (
                    normal_context["trace_paths"].get(local_interface.pk)
                    if normal_context is not None
                    else local_interface.trace()
                )
                if not self._trace_is_viewable(
                    path,
                    (normal_context or {}).get("trace_visibility"),
                ):
                    link["cable_status"] = "Cable State Not Available"
                    return link
                if cable_path_reaches(local_interface, remote_termination=remote_interface, path=path):
                    # A remodeled multi-segment path (through patch panels) that still reaches
                    # the LibreNMS target: the link is correct, just modeled in more detail.
                    link.update(
                        {
                            "cable_status": "Connected via Patch Path",
                            "cable_url": reverse("dcim:cable", args=[local_cable.pk]),
                        }
                    )
                else:
                    link.update(
                        {
                            "cable_status": "Cable Mismatch",
                            "cable_url": reverse("dcim:cable", args=[local_cable.pk]),
                        }
                    )
                    link["can_create_cable"] = actionable
            else:
                # Cabled somewhere that does not reach the LibreNMS target: offer a re-sync,
                # gated by classify_cable_action and explicit confirmation of the current cable.
                occupying_cable = local_cable or remote_cable
                link.update(
                    {
                        "cable_status": "Cable Mismatch",
                        "cable_url": reverse("dcim:cable", args=[occupying_cable.pk]),
                    }
                )
                link["can_create_cable"] = actionable
        else:
            link["cable_status"] = (
                "Both Interfaces Not Found in Netbox"
                if not (local_interface_id or remote_interface_id)
                else "Local Interface Not Found in Netbox"
                if not local_interface_id
                else "Remote Interface Not Found in Netbox"
            )

        return link

    def check_serial_cable_status(self, link, csp=None, remote_context=None):
        """
        Check cable status for a serial ConsoleServerPort row.

        ``csp`` may be the ConsoleServerPort already loaded by ``enrich_local_port`` for this
        row; when it matches the resolved id it's reused instead of re-fetching by pk.

        Args:
            link (dict): The serial cable row to update.
            csp (ConsoleServerPort | None): The preloaded local console server port.
            remote_context (dict | None): Preloaded cable visibility data.

        Returns:
            dict: The serial cable row with its status and sync affordance.
        """
        csp_id = link.get("netbox_local_interface_id")
        link["can_create_cable"] = False
        if not csp_id:
            link["cable_status"] = "Console Server Port Not Found in NetBox"
            return link
        if csp is None or csp.pk != csp_id:
            try:
                csp = self._viewable_queryset(ConsoleServerPort).get(pk=csp_id)
            except ConsoleServerPort.DoesNotExist:
                link["cable_status"] = "Console Server Port Not Found in NetBox"
                return link
        if csp.cable:
            visible_cable_ids = (remote_context or {}).get("visible_cable_ids")
            cable_is_visible = (
                csp.cable_id in visible_cable_ids
                if visible_cable_ids is not None
                else self._object_is_viewable(csp.cable)
            )
            if not cable_is_visible:
                link["cable_status"] = "Cable State Not Available"
                link["_serial_cable_hidden"] = True
                return link
            link.update(
                {
                    "cable_status": "Cable Found",
                    "cable_url": reverse("dcim:cable", args=[csp.cable.pk]),
                }
            )
            if not cable_is_point_to_point(csp.cable):
                link["cable_status"] = "Multi-termination Cable Not Supported"
                link["_multi_termination_unsupported"] = True
        else:
            link["cable_status"] = "No Cable"
        return link

    def enrich_serial_remote(self, link, claimed_cp_ids=None, csp=None, remote_context=None):  # noqa: C901
        """
        Resolve the remote ConsolePort for a serial row using the Avocent label.

        Called whenever the local ConsoleServerPort is found. Matches the label to a NetBox
        device by name, then resolves against the CSP's cable state:

        - CSP un-cabled: pick the first uncabled ConsolePort on the label device and set
          ``can_create_cable = True`` so the sync action can create the cable.
        - CSP cabled directly to a ConsolePort ON the label device: the desired connection
          already exists — resolve the remote to THAT port (never a fresh free one) and offer
          a sync only while the cable is untagged (it then just adopts/tags the cable).
        - CSP cabled and the traced path reaches the label device through patch panels: a
          remodel of the same link — mark ``"Connected via Patch Path"``, no action.
        - CSP cabled somewhere that does not reach the label device: if the cable carries the
          librenms tag it is TRUSTED over the label (the label is only a hint; a tagged cable
          was placed deliberately, possibly via a manual pick — flipping it to a mismatch
          would offer a re-point of a correct cable). Otherwise ``"Cable Mismatch"``: pick a free
          ConsolePort as the re-point target. The sync requires exact cable confirmation before
          replacement.

        A manually picked remote (``manual_remote_id``, set by CableRemotePickerView) wins over
        the label hint entirely — the label mismatching is exactly why the user picked by hand.
        The manual states mirror the label states but match at PORT level, and the tag-trust
        rule does not apply (the pick carries the user's explicit intent to re-point).

        Args:
            link (dict): The serial cable-sync row, mutated in place with the resolved
                remote device/port (or a ``cable_status`` note on failure).
            claimed_cp_ids (set | None): ConsolePort pks already picked by an earlier serial
                row in this response. The pick excludes them (and adds its own), so two serial
                rows resolving to the same remote device don't both target the same uncabled
                ConsolePort — which would create one cable and then misreport the second as a
                "duplicate" while a genuinely free port stays uncabled.
            csp: The ConsoleServerPort already loaded for this row (reused when it matches the
                resolved id, saving a re-fetch by pk).
            remote_context: Optional preloaded device, port, cable, and trace visibility data.

        Returns:
            None
        """
        csp_id = link.get("netbox_local_interface_id")
        # No resolved local end -> nothing to resolve a remote against (call sites gate on the
        # id, so this is a defensive guard, not a reachable state on the render path).
        if not csp_id:
            return
        if csp is None or csp.pk != csp_id:
            try:
                csp = self._viewable_queryset(ConsoleServerPort).get(pk=csp_id)
            except ConsoleServerPort.DoesNotExist:
                return
        if link.get("_serial_cable_hidden") or link.get("_multi_termination_unsupported"):
            return

        # A manual pick resolves at port level and skips label matching (see docstring). If the
        # picked port is no longer available, fail closed. Falling back to the label would replace
        # the user's explicit target with a different endpoint during sync.
        if "manual_remote_id" in link:
            manual_pk = coerce_librenms_id(link.get("manual_remote_id"))
            manual_cp = (remote_context or {}).get("manual_ports", {}).get(manual_pk)
            if remote_context is None:
                manual_cp = (
                    self._viewable_queryset(ConsolePort)
                    .filter(pk=manual_pk, device__in=self._viewable_queryset(Device))
                    .select_related("device")
                    .first()
                )
            if manual_cp is not None:
                # Reserve the pick in the shared dedup set so a sibling auto-matched row cannot
                # target the manually picked endpoint in the same batch.
                manual_is_actionable = remote_context is None or (
                    csp.pk in remote_context["changeable_console_server_port_ids"]
                    and manual_cp.pk in remote_context["changeable_console_port_ids"]
                )
                if claimed_cp_ids is not None and manual_is_actionable:
                    claimed_cp_ids.add(manual_cp.pk)
                self._apply_serial_remote_target(
                    link,
                    csp,
                    manual_cp,
                    manual=True,
                    remote_context=remote_context,
                )
                if not manual_is_actionable:
                    link["can_create_cable"] = False
                return
            link.update(
                {
                    "manual_remote": True,
                    "cable_status": "Selected remote port is no longer available",
                    "can_create_cable": False,
                }
            )
            return

        # The default label on an unconfigured sensor identifies only the local appliance
        # port. It is not evidence for a remote NetBox Device with the same name. Keep the
        # manual picker available, but do not auto-select or write a remote endpoint.
        if link.get("is_configured") is False:
            if csp.cable is not None:
                self._display_serial_cable_far_end(
                    link,
                    csp,
                    path=(remote_context or {}).get("trace_paths", {}).get(csp.pk),
                    visible_ids=(remote_context or {}).get("trace_visibility"),
                )
            return

        label = link.get("remote_device")
        if not label:
            # Cabled but no hint at all: still show where the cable really goes.
            if csp.cable is not None:
                self._display_serial_cable_far_end(
                    link,
                    csp,
                    path=(remote_context or {}).get("trace_paths", {}).get(csp.pk),
                    visible_ids=(remote_context or {}).get("trace_visibility"),
                )
            return

        if remote_context is not None:
            device = remote_context["devices_by_label"].get(label)
            found = device is not None
        else:
            device, found, _ = self.get_device_by_id_or_name(
                None,
                label,
                queryset=self._viewable_queryset(Device),
            )
        if not found:
            # A dead label must not leave a cabled row's remote columns empty — the cable
            # knows its far end; show (and link) reality. Display only, no sync target.
            if csp.cable is not None:
                self._display_serial_cable_far_end(
                    link,
                    csp,
                    path=(remote_context or {}).get("trace_paths", {}).get(csp.pk),
                    visible_ids=(remote_context or {}).get("trace_visibility"),
                )
            return

        link["remote_device_url"] = reverse("dcim:device", args=[device.pk])
        link["netbox_remote_device_id"] = device.pk

        # A cabled row resolves against its cable state; only a mismatch falls through to the
        # free-port pick below (the re-sync re-points at the label device).
        if csp.cable is not None and self._resolve_cabled_serial_row(
            link,
            csp,
            device,
            remote_context=remote_context,
        ):
            return

        if claimed_cp_ids is None:
            claimed_cp_ids = set()
        # Order explicitly so the picked port is deterministic (the label is only a hint and
        # the user confirms before sync) and stable regardless of the model's default ordering;
        # exclude ports already claimed by a sibling serial row this response.
        if remote_context is not None:
            local_is_actionable = csp.pk in remote_context["changeable_console_server_port_ids"]
            uncabled_cp = next(
                (
                    port
                    for port in remote_context["ports_by_device"].get(device.pk, ())
                    if port.cable_id is None
                    and port.pk not in claimed_cp_ids
                    and (not local_is_actionable or port.pk in remote_context["changeable_console_port_ids"])
                ),
                None,
            )
        else:
            uncabled_cp = (
                self._viewable_queryset(ConsolePort)
                .filter(device=device, cable__isnull=True)
                .exclude(pk__in=claimed_cp_ids)
                .order_by("name")
                .first()
            )
        if uncabled_cp:
            can_create = remote_context is None or (
                csp.pk in remote_context["changeable_console_server_port_ids"]
                and uncabled_cp.pk in remote_context["changeable_console_port_ids"]
            )
            if can_create:
                claimed_cp_ids.add(uncabled_cp.pk)
            link["netbox_remote_interface_id"] = uncabled_cp.pk
            link["remote_port_name"] = uncabled_cp.name
            link["remote_port_url"] = reverse("dcim:consoleport", args=[uncabled_cp.pk])
            link["can_create_cable"] = can_create
        else:
            link["cable_status"] = "Console Port Not Found in NetBox"

    def _resolve_cabled_serial_row(self, link, csp, device, remote_context=None) -> bool:
        """
        Resolve a cabled serial row against the label-matched *device*.

        Adopted match (cable lands on a ConsolePort of the label device), remodeled path
        (traced path reaches the device through patch panels), and trusted tag (a plugin-tagged
        cable is deliberate, so a wrong-name label must not offer a re-point) all fully
        resolve the row. A mismatch does not: the caller falls through to the free-port pick
        that re-points at the label device.

        Args:
            link (dict): The serial cable-sync row, mutated in place.
            csp: The row's resolved (and cabled) local ConsoleServerPort.
            device: The label-matched NetBox device.
            remote_context: Optional preloaded port, cable, and trace visibility data.

        Returns:
            bool: True when the row is fully resolved; False to fall through (mismatch).
        """
        visible_console_port_ids = (remote_context or {}).get("visible_console_port_ids")
        far_cp = next(
            (
                termination
                for termination in cable_far_terminations(csp.cable, csp)
                if isinstance(termination, ConsolePort)
                and termination.device_id == device.pk
                and (
                    termination.pk in visible_console_port_ids
                    if visible_console_port_ids is not None
                    else self._object_is_viewable(termination)
                )
            ),
            None,
        )
        if far_cp is not None:
            link["netbox_remote_interface_id"] = far_cp.pk
            link["remote_port_name"] = far_cp.name
            link["remote_port_url"] = reverse("dcim:consoleport", args=[far_cp.pk])
            link["can_create_cable"] = not self._cable_has_provenance(csp.cable)
            return True
        # Trace once and reuse it for both the reach check and the far-end display — trace()
        # walks the full cable path in the DB, so the reach+display branches would otherwise
        # pay for it twice per cabled row.
        if remote_context is not None and csp.pk in remote_context["trace_paths"]:
            path = remote_context["trace_paths"][csp.pk]
        else:
            path = csp.trace()
        trace_visibility = (remote_context or {}).get("trace_visibility")
        if not self._trace_is_viewable(path, trace_visibility):
            link["cable_status"] = "Cable State Not Available"
            link["can_create_cable"] = False
            link["_serial_cable_hidden"] = True
            return True
        if cable_path_reaches(
            csp,
            remote_device=device,
            path=path,
            remote_termination_type=ConsolePort,
        ):
            # Show the END of the traced path (the real console), not the panel port the
            # first segment lands on.
            link["cable_status"] = "Connected via Patch Path"
            self._display_serial_cable_far_end(
                link,
                csp,
                path=path,
                visible_ids=trace_visibility,
            )
            return True
        if self._cable_has_provenance(csp.cable):
            # Trust rule: a plugin-tagged cable was placed deliberately; the wrong-name label
            # must not flip it to a mismatch offering a re-point. Display where it
            # really goes (overriding the label-device link the caller set).
            self._display_serial_cable_far_end(
                link,
                csp,
                path=path,
                visible_ids=trace_visibility,
            )
            return True
        link["cable_status"] = "Cable Mismatch"
        return False

    def _display_serial_cable_far_end(self, link, csp, path=None, visible_ids=None):
        """
        Fill *link*'s remote display fields from where the CSP's cable path actually ends.

        Used when a cabled serial row has no resolvable sync target (dead label, trusted tag,
        remodeled path): the row must still show — and link — the real far-end device and
        port instead of dead columns. Follows patch-panel pass-throughs, so a remodeled path
        displays the end console rather than the panel port the first segment lands on.
        Display only: ``netbox_remote_interface_id`` is NOT set (there is nothing to sync at).

        Args:
            link (dict): The serial cable-sync row, mutated in place.
            csp: The row's resolved (and cabled) local ConsoleServerPort.
            path: An already-computed ``csp.trace()`` result to reuse (avoids re-tracing).
            visible_ids: Optional visible object IDs grouped by model.
        """
        if path is None:
            path = csp.trace()
        if path and not self._trace_is_viewable(path, visible_ids):
            link["cable_status"] = "Cable State Not Available"
            link["can_create_cable"] = False
            link["_serial_cable_hidden"] = True
            return
        far = (path[-1][2] if path else None) or cable_far_terminations(csp.cable, csp)
        termination = far[0] if far else None
        termination_is_viewable = termination is not None and (
            termination.pk in visible_ids.get(type(termination), set())
            if visible_ids is not None
            else self._object_is_viewable(termination)
        )
        if not termination_is_viewable:
            return
        device = getattr(termination, "device", None)
        device_is_viewable = device is not None and (
            device.pk in visible_ids.get(type(device), set())
            if visible_ids is not None
            else self._object_is_viewable(device)
        )
        if not device_is_viewable:
            return
        # DISPLAY-ONLY key: never overwrite the raw ``remote_device`` label — it survives
        # the cache strip, so a leaked far-end name would resolve as a "label" on the next
        # cached re-render and flip the row's status (fresh vs cached renders disagreeing).
        link["remote_device_display"] = device.name
        link["remote_device_url"] = reverse("dcim:device", args=[device.pk])
        link["netbox_remote_device_id"] = device.pk
        link["remote_port_name"] = getattr(termination, "name", str(termination))
        if hasattr(termination, "get_absolute_url"):
            link["remote_port_url"] = termination.get_absolute_url()

    def _apply_serial_remote_target(self, link, csp, target_cp, manual=False, remote_context=None):
        """
        Resolve *link*'s remote to *target_cp* and derive status/affordance from the cable state.

        Port-level counterpart of the label path in :meth:`enrich_serial_remote`, used for a
        manually picked remote: un-cabled → sync creates; cabled directly to the target →
        "Cable Found" (adopt offered while untagged); traced path reaches the target →
        "Connected via Patch Path"; cabled elsewhere → "Cable Mismatch" re-pointing at the
        target (the classify gate still protects non-owned cables at sync time).

        Args:
            link (dict): The serial cable-sync row, mutated in place.
            csp: The row's resolved local ConsoleServerPort.
            target_cp: The ConsolePort the remote should resolve to.
            manual (bool): Mark the row as manually picked (rendered as a hint in the table).
            remote_context: Optional preloaded port, cable, and trace visibility data.
        """
        link["netbox_remote_device_id"] = target_cp.device_id
        # Show the picked device's real name (display-only key: the raw ``remote_device`` label
        # survives the cache strip and must stay pristine for re-enrichment) — the LibreNMS
        # label mismatching is exactly why the user picked by hand.
        link["remote_device_display"] = target_cp.device.name
        link["remote_device_url"] = reverse("dcim:device", args=[target_cp.device_id])
        link["netbox_remote_interface_id"] = target_cp.pk
        link["remote_port_name"] = target_cp.name
        link["remote_port_url"] = reverse("dcim:consoleport", args=[target_cp.pk])
        if manual:
            link["manual_remote"] = True

        cable = csp.cable
        target_cable = target_cp.cable
        visible_cable_ids = (remote_context or {}).get("visible_cable_ids")
        cable_state_hidden = any(
            current is not None
            and (
                current.pk not in visible_cable_ids
                if visible_cable_ids is not None
                else not self._object_is_viewable(current)
            )
            for current in (cable, target_cable)
        )
        if cable_state_hidden:
            link["cable_status"] = "Cable State Not Available"
            link["can_create_cable"] = False
            return
        unsupported_cable = next(
            (
                current
                for current in (cable, target_cable)
                if current is not None and not cable_is_point_to_point(current)
            ),
            None,
        )
        if unsupported_cable is not None:
            link["cable_status"] = "Multi-termination Cable Not Supported"
            link["cable_url"] = reverse("dcim:cable", args=[unsupported_cable.pk])
            link["can_create_cable"] = False
            link["_multi_termination_unsupported"] = True
            link.pop("picker_url", None)
            return
        if cable is None and target_cable is None:
            link["can_create_cable"] = True  # status is already "No Cable"
            return
        if cable is not None and any(termination == target_cp for termination in cable_far_terminations(cable, csp)):
            # The desired connection already exists — offer adopt while untagged.
            link["can_create_cable"] = not self._cable_has_provenance(cable)
            return
        if cable is not None:
            if remote_context is not None and csp.pk in remote_context["trace_paths"]:
                path = remote_context["trace_paths"][csp.pk]
            else:
                path = csp.trace()
            if not self._trace_is_viewable(path, (remote_context or {}).get("trace_visibility")):
                link["cable_status"] = "Cable State Not Available"
                link["can_create_cable"] = False
                return
            if cable_path_reaches(csp, remote_termination=target_cp, path=path):
                link["cable_status"] = "Connected via Patch Path"
                return
        link["cable_status"] = "Cable Mismatch"
        link["can_create_cable"] = True

    def _set_remote_picker_affordance(self, link, obj, server_key):
        """
        Attach a ``picker_url`` to every row where manually picking the remote end is possible.

        Remote matching is name-based (serial label / LLDP port name) and often fails, so any
        row with a resolved local end gets the pick-remote action — including satisfied rows
        (tagged "Cable Found", "Connected via Patch Path"), where the pick RE-POINTS the
        existing cable: a manual re-point always goes through the force-confirm modal at sync
        time, so offering it here is safe. Only context-only OOB rows (never syncable) and
        rows without a local end are excluded.

        Args:
            link (dict): The enriched cable row, mutated in place.
            obj: The page device (URL scope for the picker endpoint).
            server_key: The active LibreNMS server key, carried in the picker URL.
        """
        if (
            not self.has_write_permission()
            or link.get("_source") == "oob"
            or link.get("_multi_termination_unsupported")
            or not link.get("netbox_local_interface_id")
        ):
            return
        url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[obj.pk])
        query = f"row_id={quote_plus(str(link.get('row_id', '')))}"
        if server_key:
            query += f"&server_key={quote_plus(server_key)}"
        link["picker_url"] = f"{url}?{query}"

    def _remote_picker_action_html(self, link, obj, server_key):
        """Render the picker action shared by table-verify response paths."""
        self._set_remote_picker_affordance(link, obj, server_key)
        if not (picker_url := link.get("picker_url")):
            return ""
        return f"""
            <button type="button" class="btn btn-sm btn-outline-secondary" title="Pick remote end"
                    aria-label="Pick remote end"
                    data-cable-picker-url="{escape(picker_url)}">
                <i class="mdi mdi-connection"></i>
            </button>
        """

    def _build_serial_remote_context(self, links, serial_ports):
        """Bulk-load serial label targets and free ports for one table render."""
        labels = {link.get("remote_device") for link in links if link.get("_source") == "serial"}
        labels.discard(None)
        labels.discard("")
        candidate_names = labels | {label.split(".")[0] for label in labels}
        catalog_devices = list(Device.objects.filter(name__in=candidate_names).order_by("pk"))
        visible_device_ids = set(
            self._viewable_queryset(Device)
            .filter(pk__in=[device.pk for device in catalog_devices])
            .values_list("pk", flat=True)
        )
        devices_by_name = defaultdict(list)
        for device in catalog_devices:
            devices_by_name[device.name].append(device)

        devices_by_label = {}
        for label in labels:
            exact = devices_by_name[label]
            simple = devices_by_name[label.split(".")[0]]
            matches = exact if exact else simple
            devices_by_label[label] = matches[0] if len(matches) == 1 and matches[0].pk in visible_device_ids else None

        manual_ids = {
            normalized
            for link in links
            if link.get("_source") == "serial"
            if (normalized := coerce_librenms_id(link.get("manual_remote_id"))) is not None
        }
        target_device_ids = {device.pk for device in devices_by_label.values() if device is not None}
        console_ports = list(
            self._viewable_queryset(ConsolePort)
            .filter(Q(device_id__in=target_device_ids) | Q(pk__in=manual_ids))
            .filter(device__in=self._viewable_queryset(Device))
            .select_related("device", "cable")
            .order_by("device_id", "name", "pk")
        )
        ports_by_device = defaultdict(list)
        manual_ports = {}
        for port in console_ports:
            ports_by_device[port.device_id].append(port)
            if port.pk in manual_ids:
                manual_ports[port.pk] = port

        # Load each cable ONCE with its tags and terminations, then hand the same instance to both
        # ends. A serial row's local ConsoleServerPort and its remote ConsolePort share a cable, so
        # prefetching per queryset walked the same tag/termination sets twice per render.
        cable_ids = {port.cable_id for port in [*serial_ports, *console_ports] if port.cable_id is not None}
        visible_cables = {
            cable.pk: cable
            for cable in self._viewable_queryset(Cable)
            .filter(pk__in=cable_ids)
            .prefetch_related("tags", "terminations__termination")
        }
        visible_cable_ids = set(visible_cables)
        for port in (*serial_ports, *console_ports):
            if (shared_cable := visible_cables.get(port.cable_id)) is not None:
                port.cable = shared_cable
        changeable_console_port_ids = set(
            self._changeable_queryset(ConsolePort)
            .filter(pk__in=[port.pk for port in console_ports])
            .values_list("pk", flat=True)
        )
        changeable_console_server_port_ids = set(
            self._changeable_queryset(ConsoleServerPort)
            .filter(pk__in=[port.pk for port in serial_ports])
            .values_list("pk", flat=True)
        )
        links_by_local_port = defaultdict(list)
        for link in links:
            if link.get("_source") == "serial":
                links_by_local_port[link.get("local_port")].append(link)
        trace_paths = {}
        for port in serial_ports:
            if port.cable_id is None or port.cable_id not in visible_cable_ids:
                continue
            direct_far = cable_far_terminations(port.cable, port)
            needs_path = False
            for link in links_by_local_port.get(port.name, ()):
                manual_id = coerce_librenms_id(link.get("manual_remote_id"))
                if manual_id is not None:
                    manual_port = manual_ports.get(manual_id)
                    needs_path = manual_port is not None and manual_port not in direct_far
                else:
                    label_device = devices_by_label.get(link.get("remote_device"))
                    needs_path = (
                        link.get("is_configured") is False
                        or label_device is None
                        or not any(
                            isinstance(far, ConsolePort) and far.device_id == label_device.pk for far in direct_far
                        )
                    )
                if needs_path:
                    break
            if not needs_path:
                continue
            trace_paths[port.pk] = (
                [([port], [port.cable], direct_far)]
                if any(isinstance(far, ConsolePort) for far in direct_far)
                else port.trace()
            )
        return {
            "devices_by_label": devices_by_label,
            "ports_by_device": ports_by_device,
            "manual_ports": manual_ports,
            "visible_console_port_ids": {port.pk for port in console_ports},
            "visible_cable_ids": visible_cable_ids,
            "changeable_console_port_ids": changeable_console_port_ids,
            "changeable_console_server_port_ids": changeable_console_server_port_ids,
            "trace_paths": trace_paths,
            "trace_visibility": self._build_trace_visibility(trace_paths.values()),
        }

    def process_remote_device(self, link, remote_hostname, remote_device_id, server_key=None, normal_context=None):
        """Process remote device data and add remote device URL if device exists in NetBox."""
        if normal_context is not None:
            device = normal_context["remote_device_by_link"].get(id(link))
            found = device is not None
            error_message = None
        else:
            device, found, error_message = self.get_device_by_id_or_name(
                remote_device_id,
                remote_hostname,
                server_key=server_key,
                queryset=self._viewable_queryset(Device),
            )
        if found:
            link.update(
                {
                    "remote_device_url": reverse("dcim:device", args=[device.pk]),
                    "netbox_remote_device_id": device.pk,
                }
            )
            return self.enrich_remote_port(
                link,
                device,
                server_key=server_key,
                normal_context=normal_context,
            )

        link.update(
            {
                "remote_port_name": link["remote_port"],
                "cable_status": error_message if error_message else "Device Not Found in NetBox",
                "can_create_cable": False,
            }
        )
        return link

    def _apply_manual_interface_target(self, link, normal_context=None):
        """Resolve one request-scoped manual Interface pick into the shared row shape."""
        if "manual_remote_id" not in link:
            return False
        manual_pk = coerce_librenms_id(link.get("manual_remote_id"))
        if normal_context is not None:
            manual_iface = normal_context["interfaces_by_pk"].get(manual_pk)
            if manual_iface is not None and (
                manual_iface.pk not in normal_context["visible_interface_ids"]
                or manual_iface.device_id not in normal_context["visible_owner_ids"]
            ):
                manual_iface = None
        else:
            manual_iface = (
                self._viewable_queryset(Interface)
                .filter(pk=manual_pk, device__in=self._viewable_queryset(Device))
                .select_related("device")
                .first()
            )
        if manual_iface is None:
            link.update(
                {
                    "manual_remote": True,
                    "cable_status": "Selected remote port is no longer available",
                    "can_create_cable": False,
                }
            )
            return True
        link.update(
            {
                "netbox_remote_device_id": manual_iface.device_id,
                "remote_device_display": manual_iface.device.name,
                "remote_device_url": reverse("dcim:device", args=[manual_iface.device_id]),
                "netbox_remote_interface_id": manual_iface.pk,
                "remote_port_name": manual_iface.name,
                "remote_port_url": reverse("dcim:interface", args=[manual_iface.pk]),
                "manual_remote": True,
            }
        )
        return True

    def enrich_links_data(self, links_data, obj, server_key=None, sync_device=None):
        """Enrich links data with local and remote port URLs and cable status."""
        if server_key is None:
            # Use the degrading resolver (mirrors the other render sites) so a broken/missing
            # default server yields None instead of raising ValueError and 500ing a cached render.
            server_key = self._render_server_key()
        # Resolve the serial sync device once for the whole loop (loop-invariant) instead of
        # per serial row. Reuse the device the caller (_prepare_context) already resolved to
        # avoid a second get_librenms_sync_device() VC-members query per request; falls back to
        # resolving here when called without one.
        serial_sync_device = sync_device or self._viewable_sync_device(obj, server_key)
        serial_links_present = any(link.get("_source") == "serial" for link in links_data)
        serial_ports = (
            list(self._viewable_queryset(ConsoleServerPort).filter(device=serial_sync_device).select_related("cable"))
            if serial_links_present and serial_sync_device is not None
            else []
        )
        serial_ports_by_name = {port.name: port for port in serial_ports}
        serial_remote_context = (
            self._build_serial_remote_context(links_data, serial_ports) if serial_links_present else None
        )
        normal_context = self._build_normal_link_context(links_data, obj, server_key)
        # ConsolePorts already auto-picked by an earlier serial row this response, so two rows
        # resolving to the same remote device don't collide on one port (see enrich_serial_remote).
        # Reserve later manual picks before row-order auto matching, but only when that row still
        # has a current, viewable local CSP. A stale pick for a deleted/hidden CSP must not consume
        # a valid row's only free remote port.
        manual_ports = (serial_remote_context or {}).get("manual_ports", {})
        changeable_console_port_ids = (serial_remote_context or {}).get("changeable_console_port_ids", set())
        changeable_console_server_port_ids = (serial_remote_context or {}).get(
            "changeable_console_server_port_ids", set()
        )
        claimed_remote_cp_ids = {
            manual_id
            for link in links_data
            if link.get("_source") == "serial"
            and (local_csp := serial_ports_by_name.get(link.get("local_port"))) is not None
            and local_csp.pk in changeable_console_server_port_ids
            if (manual_id := coerce_librenms_id(link.get("manual_remote_id"))) in manual_ports
            and manual_id in changeable_console_port_ids
        }
        for link in links_data:
            csp = self.enrich_local_port(
                link,
                obj,
                server_key=server_key,
                sync_device=serial_sync_device,
                serial_ports_by_name=serial_ports_by_name,
                normal_context=normal_context,
            )

            # Serial rows: check CSP cable status, then try to resolve remote ConsolePort.
            if link.get("_source") == "serial":
                # Serial rows already carry the CSP-owning sync device_id from
                # map_sensors_to_serial_links; don't overwrite it with the viewed obj.id —
                # on a VC-member page that would default the per-row member dropdown (and the
                # sync target) to the wrong member. Reuse the CSP enrich_local_port just loaded.
                self.check_serial_cable_status(link, csp=csp, remote_context=serial_remote_context)
                # If CSP is found, resolve the remote against the label — for cabled rows too,
                # so the row can offer adopt (matched untagged) or re-sync (mismatch) actions.
                if link.get("netbox_local_interface_id"):
                    self.enrich_serial_remote(
                        link,
                        claimed_cp_ids=claimed_remote_cp_ids,
                        csp=csp,
                        remote_context=serial_remote_context,
                    )
                self._set_remote_picker_affordance(link, obj, server_key)
                continue

            link["device_id"] = obj.id

            # A manually picked remote Interface wins over LLDP name matching entirely (the
            # names mismatching is why the user picked by hand). check_cable_status then derives
            # the matched / patch-path / mismatch state at termination level as usual. A vanished
            # picked port falls back to the name-matching path below.
            if self._apply_manual_interface_target(link, normal_context=normal_context):
                link = self.check_cable_status(link, normal_context=normal_context)
            elif remote_hostname := link.get("remote_device"):
                link = self.process_remote_device(
                    link,
                    remote_hostname,
                    link.get("remote_device_id"),
                    server_key=server_key,
                    normal_context=normal_context,
                )
                if link.get("netbox_remote_device_id"):
                    link = self.check_cable_status(link, normal_context=normal_context)
            self._set_remote_picker_affordance(link, obj, server_key)

        # Drop a serial row only when its ConsoleServerPort exists but is out of view scope. A
        # sensor with no NetBox port at all is LibreNMS data, not NetBox data: it renders as
        # "Console Server Port Not Found in NetBox" and tells the operator which port to create,
        # so hiding it would give a granted user a shorter table than an administrator.
        if serial_links_present and serial_sync_device is not None:
            # Only a row that did NOT resolve can be hiding a port: everything else already
            # matched a viewable one. When every row resolved, this costs no query at all.
            unresolved_names = {
                name
                for link in links_data
                if link.get("_source") == "serial" and (name := link.get("local_port")) not in serial_ports_by_name
            }
            hidden_serial_port_names = (
                set(
                    ConsoleServerPort.objects.filter(device=serial_sync_device, name__in=unresolved_names).values_list(
                        "name", flat=True
                    )
                )
                if unresolved_names
                else set()
            )
            if hidden_serial_port_names:
                links_data[:] = [
                    link
                    for link in links_data
                    if link.get("_source") != "serial" or link.get("local_port") not in hidden_serial_port_names
                ]
        self._apply_termination_change_scope(
            links_data,
            preloaded_changeable_ids={
                ConsolePort: changeable_console_port_ids,
                ConsoleServerPort: changeable_console_server_port_ids,
            }
            if serial_remote_context is not None
            else None,
        )
        return links_data

    def get_table(self, data, obj):
        """Return the cable table for *data*; concrete subclasses choose the table class."""
        raise NotImplementedError

    def _prepare_context(self, request, obj, fetch_fresh=False, server_key=None):  # noqa: C901
        """Helper method to prepare the context data for cable sync views."""
        table = None
        cache_expiry = None
        # Scoped to the POST-resolved server when provided; else the degrading resolver.
        server_key = server_key or self._render_server_key()
        # For VC devices, cache under the sync device's key so SingleCableVerifyView reads the same entry.
        cache_device = self._viewable_sync_device(obj, server_key)
        if cache_device is None:
            return None
        cache_key = self.get_cache_key(cache_device, "links", server_key)
        incomplete_sources = []
        cached_before_refresh = cache.get(cache_key) if fetch_fresh else None

        if fetch_fresh:
            # Always fetch new data when requested
            links_data = self.get_links_data(obj, server_key=server_key, sync_device=cache_device)
            # Only a true fetch failure returns None. An empty list ([]) is a valid result
            # (device has no host links) and must flow through: get_links_data() may have
            # collected zero host links yet still set _oob_links_fetch_failed, and post()
            # surfaces that OOB warning only on the success path — `if not links_data`
            # would discard it and mislabel it "No links found".
            if links_data is None:
                cache.delete(cache_key)
                return None
            if getattr(self, "_librenms_id_unresolved", False):
                cache.delete(cache_key)
                return {
                    "table": None,
                    "object": obj,
                    "cache_expiry": None,
                    "server_key": server_key,
                    "refresh_incomplete": True,
                }
            snapshot_token = uuid4().hex
            if getattr(self, "_links_fetch_error", None) and getattr(self, "librenms_id", None) is not None:
                incomplete_sources.append("host")
            if getattr(self, "_oob_links_fetch_failed", False):
                incomplete_sources.append("OOB")
            if getattr(self, "_serial_links_fetch_failed", False):
                incomplete_sources.append("serial")
            if getattr(self, "_serial_source_skipped", False):
                prior_links = _extract_cached_links(cached_before_refresh) if cached_before_refresh else None
                links_data.extend(link for link in prior_links or [] if link.get("_source") == "serial")
                incomplete_sources.append("serial")
        else:
            # Try to use cached data
            cached_links_data = cache.get(cache_key)
            if cached_links_data:
                # Fail closed on a malformed/corrupt cache entry (non-dict, non-list "links", or a
                # non-dict link row) instead of crashing the cached render below on .items().
                links_data = _extract_cached_links(cached_links_data, cache_key)
                if links_data is None:
                    return None
                snapshot_token = cable_snapshot_token(cached_links_data)
                cached_incomplete = cached_links_data.get("incomplete_sources", [])
                if isinstance(cached_incomplete, list):
                    incomplete_sources = [source for source in cached_incomplete if isinstance(source, str)]
            else:
                return None

        # The shared snapshot stores only permission-independent LibreNMS source fields. NetBox
        # IDs, URLs, cable state, and user picks are derived below for this request only.
        raw_links = assign_cable_row_ids(
            [{k: v for k, v in link.items() if k in _RAW_LINK_KEYS} for link in links_data]
        )
        if raw_links is None:
            cache.delete(cache_key)
            return None
        if fetch_fresh:
            cache.set(
                cache_key,
                {
                    "links": raw_links,
                    "snapshot_token": snapshot_token,
                    "incomplete_sources": incomplete_sources,
                },
                timeout=self.librenms_api.cache_timeout,
            )

        user_id = getattr(getattr(request, "user", None), "pk", None)
        if user_id is not None:
            raw_links, _has_manual_picks = apply_cable_manual_picks(
                cache,
                cache_key,
                {"links": raw_links, "snapshot_token": snapshot_token},
                user_id,
                raw_links,
            )

        # Enrich a request-local copy against current permission-scoped NetBox state.
        links_data = self.enrich_links_data(
            raw_links,
            obj,
            server_key=server_key,
            sync_device=cache_device,
        )
        if isinstance(obj, Device):
            local_owner_ids = {
                owner_id
                for link in links_data
                if (owner_id := coerce_librenms_id(link.get("netbox_local_device_id"))) is not None
            }
            owner_devices = {device.pk: device for device in (obj, cache_device)}
            owner_devices.update(
                {
                    device.pk: device
                    for device in self._viewable_queryset(Device).filter(pk__in=local_owner_ids - owner_devices.keys())
                }
            )
            read_only_owner_ids = {
                device_id for device_id, device in owner_devices.items() if get_migrated_to_marker(device, server_key)
            }
            page_is_read_only = obj.pk in read_only_owner_ids or cache_device.pk in read_only_owner_ids
            for link in links_data:
                if (
                    not page_is_read_only
                    and coerce_librenms_id(link.get("netbox_local_device_id")) not in read_only_owner_ids
                ):
                    continue
                link["can_create_cable"] = False
                link.pop("picker_url", None)

        # Calculate cache expiry
        cache_ttl = cache_remaining_ttl(cache, cache_key)
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
            "incomplete_sources": incomplete_sources,
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
        if error := self.require_object_permissions("POST"):
            return error
        obj = self.get_object(pk)
        # Rebind the API to the POSTed server so live link/port fetches hit the same
        # Active server whose source snapshot supplies this multi-server tab. The strict helper,
        # not request.POST.get(): QueryDict.get() keeps the LAST of repeated values, so an
        # ambiguous payload would silently refresh one server's cache namespace from another's.
        server_key = self.rebind_api_for_posted_server(request.POST)
        if server_key is None:
            getlist = getattr(request.POST, "getlist", None)
            posted_values = getlist("server_key") if callable(getlist) else request.POST.get("server_key")
            if isinstance(posted_values, (list, tuple)) and len(posted_values) > 1:
                messages.error(request, "Select exactly one configured LibreNMS server.")
            else:
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
            SyncCacheConsistency(obj).mark_refresh_failure(
                SyncTab.CABLES,
                server_key,
                actor_id=request_actor_id(request),
            )
            return self.render_sync_partial(
                request,
                obj,
                server_key,
                {"cable_sync": {"object": obj, "table": None, "cache_expiry": None, "server_key": server_key}},
            )

        if context.get("refresh_incomplete"):
            messages.warning(
                request,
                "Cable refresh was incomplete. No cable rows were loaded. Refresh Cables to try again.",
            )
            SyncCacheConsistency(obj).mark_refresh_failure(
                SyncTab.CABLES,
                server_key,
                actor_id=request_actor_id(request),
            )
            return self.render_sync_partial(request, obj, server_key, {"cable_sync": context})

        # Decide the outcome before announcing it. cache.set() does not confirm that the
        # snapshot exists, and an eviction can remove it before this check.
        coordinator = SyncCacheConsistency(obj)
        snapshot_cached = coordinator.mark_refresh_outcome(
            SyncTab.CABLES,
            server_key,
            actor_id=request_actor_id(request),
        )
        if snapshot_cached:
            messages.success(request, "Cable data refreshed successfully.")
        else:
            messages.error(
                request,
                "Cable data could not be cached. The rows shown come from this refresh only "
                "and will not survive a reload. Refresh again before syncing; see server logs for details.",
            )
        # A host LLDP failure no longer aborts the refresh (OOB/serial rows can still surface it
        # as "successful"), so warn when the host fetch failed but we had a host id to query —
        # otherwise host-side cables are silently omitted under a success banner. Skip for an
        # OOB-only device (librenms_id is None), where a host fetch failure is expected.
        if getattr(self, "_links_fetch_error", None) and (
            getattr(self, "librenms_id", None) is not None or getattr(self, "_librenms_id_unresolved", False)
        ):
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
        if getattr(self, "_serial_links_fetch_failed", False):
            messages.warning(
                request,
                "Cables refreshed, but serial port sensor fetch failed; "
                "serial console rows may be missing. See server logs for details.",
            )
        return self.render_sync_partial(request, obj, server_key, {"cable_sync": context})


class SingleCableVerifyView(BaseCableTableView):
    """View to verify a single cable link between two devices."""

    # Read-only verify endpoint: require object-view permission (mirrors the interface/module
    # verify views). Without it any user with mere plugin-view rights could POST an arbitrary
    # device id and read back that device's rendered cable/topology rows.
    required_object_permissions = {"POST": [("view", Device)]}

    def post(self, request):  # noqa: C901
        data, err = parse_request_json(request)
        if err:
            return err
        # Gate BEFORE resolving the device / touching the LibreNMS client: an unauthorized caller
        # must not be able to probe device IDs or trigger work through this endpoint.
        if error := self.require_object_permissions_json("POST"):
            return error
        selected_device_id = data.get("device_id")
        row_id = data.get("row_id")
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
            "can_create_cable": False,
            "expected_local_id": None,
            "expected_local_device_id": None,
            "expected_remote_id": None,
            "expected_remote_device_id": None,
        }

        if selected_device_id:
            # Object-scope the lookup: the gate only checked model-level view_device, so an
            # out-of-scope pk must 404 rather than expose that device's cached cable row.
            selected_device = self.restrict_object_or_404(Device, pk=selected_device_id)
            origin_device = selected_device
            raw_origin_device_id = data.get("origin_device_id")
            if raw_origin_device_id is not None:
                origin_device_id = coerce_librenms_id(raw_origin_device_id)
                if origin_device_id is None:
                    return JsonResponse(
                        {"status": "error", "message": "A valid origin device ID is required."},
                        status=400,
                    )
                origin_device = self.restrict_object_or_404(Device, pk=origin_device_id)
                same_device = origin_device.pk == selected_device.pk
                same_chassis = (
                    origin_device.virtual_chassis_id is not None
                    and origin_device.virtual_chassis_id == selected_device.virtual_chassis_id
                )
                if not same_device and not same_chassis:
                    return JsonResponse(
                        {"status": "error", "message": "The cable page and selected device do not match."},
                        status=400,
                    )
            # Use the same sync-device resolution as the GET path so the cache
            # key matches what _prepare_context wrote. When the VC has no
            # resolvable sync device, return an empty row rather than crashing.
            if selected_device.virtual_chassis:
                primary_device = self._viewable_sync_device(selected_device, server_key)
                if primary_device is None:
                    return JsonResponse({"status": "success", "formatted_row": formatted_row})
            else:
                primary_device = selected_device

            read_only_origin = any(
                get_migrated_to_marker(device, server_key)
                for device in {origin_device, primary_device, selected_device}
            )

            links_cache_key = self.get_cache_key(primary_device, "links", server_key)
            cached_links = cache.get(links_cache_key)

            # Same fail-closed guard as the cached GET render: a malformed entry (non-dict, non-list
            # "links", or a non-dict link row) is purged and treated as no cache, so the verify path
            # returns the empty formatted_row instead of crashing on .get()/.items().
            valid_links = _extract_cached_links(cached_links, links_cache_key) if cached_links else None
            valid_links = assign_cable_row_ids(valid_links) if valid_links is not None else None
            if valid_links and request.user.pk is not None:
                valid_links, _has_manual_picks = apply_cable_manual_picks(
                    cache,
                    links_cache_key,
                    cached_links,
                    request.user.pk,
                    valid_links,
                )
            if valid_links:
                link_data = next(
                    (link for link in valid_links if link.get("row_id") == row_id),
                    None,
                )
                if link_data:
                    manual_remote_id = link_data.get("manual_remote_id")
                    # Strip derived fields from cached data to avoid stale
                    # IDs/URLs when NetBox objects are deleted after caching.
                    link_data = {k: v for k, v in link_data.items() if k in _RAW_LINK_KEYS}
                    if manual_remote_id is not None:
                        link_data["manual_remote_id"] = manual_remote_id

                    # Serial rows have a fixed ConsoleServerPort owner. Their owner selector is
                    # disabled, so they never need the member-change verify path.
                    if link_data.get("_source") == "serial":
                        return JsonResponse(
                            {"status": "error", "message": "Serial cable rows have a fixed device owner."},
                            status=400,
                        )

                    # The verify response returns formatted_row HTML directly (it does not pass
                    # through LibreNMSCableTable.render_local_port), so re-apply the OOB badge
                    # here to match the initial render — otherwise a verified OOB cable row loses
                    # the badge and looks like a plain host-port row. Same helper as the table
                    # render, so the two can't drift.
                    oob_badge = oob_badge_html(link_data, leading_space=True)

                    # Re-enrich remote side from current NetBox state
                    remote_hostname = link_data.get("remote_device", "")
                    if not self._apply_manual_interface_target(link_data) and remote_hostname:
                        link_data = self.process_remote_device(
                            link_data, remote_hostname, link_data.get("remote_device_id"), server_key=server_key
                        )

                    # `or ""` (not a .get default): the OOB-merge path stores local_port=None when
                    # the port name can't be resolved, and a present-but-None value would otherwise
                    # render the literal string "None" via escape() below.
                    local_port = link_data.get("local_port") or ""
                    formatted_row["local_port"] = local_port

                    # Resolve the local interface on the member selected by the user. The caller
                    # already proved that this Device is visible. Do not infer a different member
                    # from the port name after the dropdown selection.
                    interface = None
                    lookup_device = selected_device
                    # Merged OOB-controller rows are context-only: their local port lives on the
                    # CONTROLLER, so a shared name (or colliding stored librenms_id) must not bind
                    # a HOST interface here — mirrors enrich_local_port's guard on the initial
                    # render. Left unresolved, the row takes the labelled, badge-carrying
                    # unresolved branch below instead of linking the wrong interface.
                    if link_data.get("_source") != "oob":
                        # Shared id→dual-name resolution core (issue #88 fallback included), so
                        # this path can't drift from enrich_local_port's again.
                        name_candidates = [n for n in (local_port, link_data.get("local_port_alt")) if n]
                        interface = resolve_interface_on_device(
                            lookup_device,
                            server_key,
                            link_data.get("local_port_id"),
                            name_candidates,
                        )
                        if interface is not None and not self._object_is_viewable(interface):
                            interface = None

                    if interface:
                        link_data["netbox_local_interface_id"] = interface.pk
                        link_data["netbox_local_device_id"] = interface.device_id

                        # Check cable status if remote side was resolved
                        if link_data.get("netbox_remote_device_id"):
                            link_data = self.check_cable_status(link_data)

                        self._apply_termination_change_scope([link_data])
                        if read_only_origin:
                            link_data["can_create_cable"] = False
                        formatted_row["can_create_cable"] = bool(link_data.get("can_create_cable"))
                        formatted_row["expected_local_id"] = coerce_librenms_id(
                            link_data.get("netbox_local_interface_id")
                        )
                        formatted_row["expected_local_device_id"] = coerce_librenms_id(
                            link_data.get("netbox_local_device_id")
                        )
                        formatted_row["expected_remote_id"] = coerce_librenms_id(
                            link_data.get("netbox_remote_interface_id")
                        )
                        formatted_row["expected_remote_device_id"] = coerce_librenms_id(
                            link_data.get("netbox_remote_device_id")
                        )

                        # Escape LibreNMS-sourced labels to prevent XSS
                        safe_local_port = escape(local_port)
                        remote_port_name = link_data.get("remote_port_name") or link_data.get("remote_port") or ""
                        safe_remote_port = escape(remote_port_name)
                        remote_device_name = link_data.get("remote_device_display") or link_data.get(
                            "remote_device", ""
                        )
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
                            formatted_row["actions"] = f"""
                                <button type="submit"
                                        class="btn btn-sm btn-primary"
                                        name="sync_one"
                                        value="{escape(str(row_id))}">
                                    Sync Cable
                                </button>
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
                        remote_device_name = link_data.get("remote_device_display") or link_data.get(
                            "remote_device", ""
                        )
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

                    if not read_only_origin:
                        formatted_row["actions"] += self._remote_picker_action_html(
                            link_data,
                            selected_device,
                            server_key,
                        )

        return JsonResponse({"status": "success", "formatted_row": formatted_row})


class CableRemotePickerView(BaseCableTableView):
    """
    Modal picker for a cable row's remote endpoint.

    Remote matching is name-based (the serial label / LLDP port name) and often fails — names
    simply don't always match. This view lets the user pick the remote end by hand:

    - ``GET`` serves the picker modal, plus its HTMX fragments: ``action=search`` (device
      search results) and ``action=ports`` (the chosen device's port list — ConsolePorts for
      serial rows, Interfaces otherwise).
    - ``POST`` stores the pick in a user- and row-scoped cache entry outside the shared LibreNMS
      snapshot. It then re-renders the cable partial and closes the modal.

    The pick only needs to live until the sync runs. Its key includes the snapshot generation, so
    a full "Refresh Cables" invalidates unsynced picks without sharing them across users.
    """

    # Both methods enumerate devices and their ports, so both gate on dcim.view_device (mirroring
    # SingleCableVerifyView). Every device/port lookup below additionally resolves through a
    # restricted queryset: the gate only asks has_perm without an instance, which a CONSTRAINED
    # view_device grant clears.
    required_object_permissions = {"GET": [("view", Device)], "POST": [("view", Device)]}

    def _viewable_ports(self, request, model):
        """Return the permission-filtered port queryset, or None without model access."""
        permission = f"{model._meta.app_label}.view_{model._meta.model_name}"
        if not request.user.has_perm(permission):
            return None
        queryset = self.restricted_queryset(model, "view")
        if model is Interface:
            queryset = queryset.exclude(type__in=VIRTUAL_IFACE_TYPES)
        return queryset

    def _cache_state(self, obj, server_key):
        """Return ``(cache_key, cached_payload)`` for *obj*'s links snapshot (sync-device scoped)."""
        cache_obj = self._viewable_sync_device(obj, server_key)
        if cache_obj is None:
            return self.get_cache_key(obj, "links", server_key), None
        cache_key = self.get_cache_key(cache_obj, "links", server_key)
        return cache_key, cache.get(cache_key)

    @staticmethod
    def _snapshot_rows(cached, cache_key=None):
        """Return normalized rows, or None when the snapshot is absent or malformed."""
        links = _extract_cached_links(cached, cache_key)
        return assign_cable_row_ids(links) if links is not None else None

    def _find_row(self, cached, row_id, cache_key=None):
        """Return the cached links row matching *row_id*, or None."""
        rows = self._snapshot_rows(cached, cache_key)
        if rows is None:
            return None
        return next(
            (row for row in rows if row.get("row_id") == row_id),
            None,
        )

    def _row_is_viewable(self, request, row, obj, server_key):
        """Return whether the picker requester may view a cached row's local endpoint."""
        source = row.get("_source")
        if source == "oob":
            return False
        local_name = row.get("local_port")
        if not isinstance(local_name, str) or not local_name:
            return False
        if source != "serial":
            owner = get_virtual_chassis_member(obj, local_name)
            if owner is None or not self.restricted_queryset(Device).filter(pk=owner.pk).exists():
                return False
            interface = resolve_interface_on_device(
                owner,
                server_key,
                row.get("local_port_id"),
                [local_name, row.get("local_port_alt")],
            )
            return bool(interface is not None and self.restricted_queryset(Interface).filter(pk=interface.pk).exists())

        owner_id = coerce_librenms_id(row.get("device_id"))
        if owner_id is None:
            return False
        if not self.restricted_queryset(Device).filter(pk=owner_id).exists():
            return False
        viewable_ports = self._viewable_ports(request, ConsoleServerPort)
        return bool(viewable_ports is not None and viewable_ports.filter(device_id=owner_id, name=local_name).exists())

    def _refetch_snapshot(self, request, obj, server_key):
        """
        Rebuild the links snapshot fresh from LibreNMS after a cache expiry.

        A pick typically lands moments after a render, but the snapshot TTL (or a dev-server
        cache flush) can lapse in between — erroring out just to make the user click "Refresh
        Cables" first is needless friction, so do exactly what that button does and retry.

        Returns:
            tuple: The refreshed ``(cache_key, cached_payload, resolved_key)`` — payload None when
                the LibreNMS fetch itself failed. ``resolved_key`` is the key the snapshot was
                actually cached under; it differs from the passed ``server_key`` when a stale/forged
                key fell back to the active client key, so the caller reuses it for the rest of the
                render and the POST round-trip. Without that, the stale query-string key round-trips
                back on POST, misses ``_cache_state`` again, and triggers a SECOND live LibreNMS
                fetch that this first refetch already made unnecessary.
        """
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        view = DeviceCableTableView()
        view.setup(request, pk=obj.pk)
        # A key that names no configured server (stale tab / forged form) must not be reused: it
        # would cache the fresh snapshot under a bogus namespace, invisible to every real render.
        # Degrade to the session/default server's RESOLVED key, exactly like SyncCablesView.
        resolved_key = view.rebind_api_for_server_or_default(server_key)
        # Caches the fresh snapshot on success (same write "Refresh Cables" performs).
        view._prepare_context(request, obj, fetch_fresh=True, server_key=resolved_key)
        cache_key, cached = self._cache_state(obj, resolved_key)
        return cache_key, cached, resolved_key

    def get(self, request, pk):
        """Serve the picker modal or one of its search/ports HTMX fragments."""
        if denied := self.require_object_permissions("GET"):
            return denied
        obj = self.restrict_object_or_404(Device, pk=pk)
        server_key = self.rebind_api_for_server(request.GET.get("server_key"))
        if server_key is None:
            return HttpResponse("Selected LibreNMS server is no longer configured.", status=400)
        row_id = request.GET.get("row_id", "")
        action = request.GET.get("action")
        base_url = reverse("plugins:netbox_librenms_plugin:cable_remote_picker", args=[obj.pk])
        if action:
            source = request.GET.get("source") or ""
            base_query = f"row_id={quote_plus(str(row_id))}&server_key={quote_plus(server_key)}"
            if source:
                base_query += f"&source={quote_plus(source)}"
            if action == "search":
                q = (request.GET.get("q") or "").strip()
                devices = (
                    self.restricted_queryset(Device)
                    .filter(name__icontains=q)
                    .select_related("site")
                    .order_by("name")[:20]
                    if q
                    else []
                )
                return render(
                    request,
                    "netbox_librenms_plugin/htmx/_remote_picker_device_results.html",
                    {"devices": devices, "q": q, "ports_url": f"{base_url}?{base_query}&action=ports"},
                )
            if action == "ports":
                try:
                    target_pk = int(request.GET.get("device_id", ""))
                except (TypeError, ValueError):
                    return HttpResponse("Select a device.", status=400)
                target = self.restrict_object_or_404(Device, pk=target_pk)
                serial = source == "serial"
                port_model = ConsolePort if serial else Interface
                viewable_ports = self._viewable_ports(request, port_model)
                if viewable_ports is None:
                    return HttpResponse("You do not have permission to view these ports.", status=403)
                ports = viewable_ports.filter(device=target)
                cabled_ports = ports.none()
                if request.user.has_perm("dcim.view_cable"):
                    visible_cables = self.restricted_queryset(Cable, "view")
                    cabled_ports = ports.filter(cable__in=visible_cables).order_by("name")
                return render(
                    request,
                    "netbox_librenms_plugin/htmx/_remote_picker_ports.html",
                    {
                        "device": target,
                        "free_ports": ports.filter(cable__isnull=True).order_by("name"),
                        "cabled_ports": cabled_ports,
                        "port_noun": "console ports" if serial else "interfaces",
                    },
                )
            return HttpResponse("Unknown picker action.", status=400)

        # Only the modal render needs the row: the fragments above carry `source` in their URLs
        # and run per keystroke, so they never read the snapshot.
        cache_key, cached = self._cache_state(obj, server_key)
        rows = self._snapshot_rows(cached, cache_key)
        row = next((candidate for candidate in rows or [] if candidate.get("row_id") == row_id), None)
        # On a missing/malformed snapshot, rebuild it instead of dead-ending on an expired-cache
        # warning. A valid snapshot with an unknown row ID is authoritative and must not let a
        # forged ID trigger fresh LibreNMS requests.
        if rows is None:
            # Reuse the key the refetch actually cached under (it falls back to the active client
            # key on a stale/forged query-string key) for the rendered URLs and the modal's hidden
            # server_key input, so a stale key doesn't round-trip on POST and force a second fetch.
            cache_key, cached, server_key = self._refetch_snapshot(request, obj, server_key)
            row = self._find_row(cached, row_id, cache_key)

        if row is None or not self._row_is_viewable(request, row, obj, server_key):
            return HttpResponse("Cable row not found.", status=404)

        # Carry the row's _source in the fragment URLs so port-type selection does not depend on
        # the cache: with the snapshot expired mid-pick, a serial picker would otherwise silently
        # fall back to listing Interfaces instead of ConsolePorts. Row wins when the cache is
        # alive; the URL param is the fallback.
        source = (row.get("_source") if row else request.GET.get("source")) or ""
        base_query = f"row_id={quote_plus(str(row_id))}" + (
            f"&server_key={quote_plus(server_key)}" if server_key else ""
        )
        if source:
            base_query += f"&source={quote_plus(source)}"

        # Initial modal: prefill the device results with the label match when there is one, so
        # the common "right device, wrong port name" case is one click away from the port list.
        initial_devices = []
        label = (row or {}).get("remote_device")
        if row and label:
            device, found, _ = self.get_device_by_id_or_name(None, label)
            # The label match runs against the plain manager (it is shared with the render path),
            # so keep the prefill inside the user's own scope.
            if found and self.restricted_queryset(Device).filter(pk=device.pk).exists():
                initial_devices = [device]
        return render(
            request,
            "netbox_librenms_plugin/htmx/cable_remote_picker_modal.html",
            {
                "object": obj,
                "row": row,
                "server_key": server_key,
                "post_url": base_url,
                "search_url": f"{base_url}?{base_query}&action=search",
                "ports_url": f"{base_url}?{base_query}&action=ports",
                "initial_devices": initial_devices,
            },
        )

    def post(self, request, pk):
        """Validate the picked port and store it on the cached row, then re-render the partial."""
        denied = self.require_all_permissions("POST")
        if denied is not None:
            return denied
        obj = self.restrict_object_or_404(Device, pk=pk)
        server_key = self.rebind_api_for_server(request.POST.get("server_key"))
        if server_key is None:
            return HttpResponse("Selected LibreNMS server is no longer configured.", status=400)
        row_id = request.POST.get("row_id", "")
        cache_key, cached = self._cache_state(obj, server_key)
        rows = self._snapshot_rows(cached, cache_key)
        row = next((candidate for candidate in rows or [] if candidate.get("row_id") == row_id), None)
        if rows is None:
            # Snapshot expired between render and pick: rebuild it fresh (what "Refresh
            # Cables" does) and retry before giving up. The row identity is snapshot-stable, so
            # the row reappears unless LibreNMS itself dropped it.
            cache_key, cached, server_key = self._refetch_snapshot(request, obj, server_key)
            row = self._find_row(cached, row_id, cache_key)
        if row is None or not self._row_is_viewable(request, row, obj, server_key):
            return HttpResponse("Cable row not found.", status=404)

        serial = row.get("_source") == "serial"
        port_model = ConsolePort if serial else Interface
        try:
            remote_pk = int(request.POST.get("remote_interface_id", ""))
        except (TypeError, ValueError):
            return HttpResponse("Select a remote port.", status=400)
        # Scope the pick to a device the user may see — the same rule the ports fragment lists by,
        # so a forged remote_interface_id can't bind a termination on an out-of-scope device.
        viewable_ports = self._viewable_ports(request, port_model)
        port = (
            viewable_ports.filter(pk=remote_pk, device__in=self.restricted_queryset(Device))
            .select_related("device")
            .first()
            if viewable_ports is not None
            else None
        )
        if port is None:
            noun = "console port" if serial else "interface"
            return HttpResponse(f"Selected {noun} does not exist (or is the wrong kind of port).", status=400)

        # Keep unsaved UI state outside the shared LibreNMS snapshot. The key includes the
        # snapshot generation, user, and row, so users cannot see or overwrite each other's picks.
        remaining_ttl = cache_remaining_ttl(cache, cache_key)
        pick_key = cable_manual_pick_cache_key(
            cache_key,
            cable_snapshot_token(cached),
            request.user.pk,
            row["row_id"],
        )
        cache.set(
            pick_key,
            {"manual_remote_id": port.pk},
            timeout=remaining_ttl if remaining_ttl and remaining_ttl > 0 else 300,
        )

        # Re-render the cable partial (rows re-enrich against the updated cache) and close the
        # modal via the partial's close_modal OOB block. Mirrors SyncCablesView._sync_response.
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        view = DeviceCableTableView()
        view.setup(request, pk=obj.pk)
        # The row was resolved from this exact snapshot scope. A stale key reaches
        # _refetch_snapshot above, which replaces it with the resolved active key.
        resolved_key = server_key
        context = view._prepare_context(request, obj, fetch_fresh=False, server_key=resolved_key)
        if context is None:
            context = {"table": None, "object": obj, "cache_expiry": None, "server_key": resolved_key}
        elif context.get("table") is not None:
            tab_url = reverse("plugins:netbox_librenms_plugin:device_cable_sync", args=[obj.pk])
            context["table"].htmx_url = f"{tab_url}?tab=cables" + (
                f"&server_key={quote_plus(resolved_key)}" if resolved_key else ""
            )
        context["close_modal"] = True
        return view.render_sync_partial(request, obj, resolved_key, {"cable_sync": context})
