import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import NamedTuple, Optional

import django_tables2 as tables
import psycopg
from dcim.models import Device, Interface
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist, ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, Max
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from netbox.config import get_config
from netbox.plugins import get_plugin_config
from utilities.exceptions import AbortRequest
from utilities.paginator import EnhancedPaginator
from utilities.paginator import get_paginate_count as netbox_get_paginate_count

from netbox_librenms_plugin.constants import (
    DEFAULT_INTERFACE_NAME_FIELD,
    HOST_NAME_COLLISION_REASON,
    NAME_OWNER_LIVE,
    NAME_OWNER_STALE,
    NAME_OWNER_UNKNOWN,
    OOB_BADGE_HTML,
    OOB_INVENTORY_SOURCE,
    OOB_NAME_SUFFIX,
    PORT_ID_SOURCE_COLLISION_REASON,
    REPORTED_NAME_PORT_COLLISION_REASON,
    is_librenms_placeholder,
    is_supported_interface_name_field,
)
from netbox_librenms_plugin.ip_addressing import parse_address_with_prefix, parse_host_address
from netbox_librenms_plugin.librenms_ids import coerce_librenms_id, normalize_librenms_port_id
from netbox_librenms_plugin.server_mappings import (
    AmbiguousLibreNMSIdError,
    MappingRole,
    identity_q,
    is_server_key,
    name_match_may_be_port,
    read_mapping,
)
from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE, classify_conflict, nearest_database_error

logger = logging.getLogger(__name__)

_CABLE_ROW_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def cable_snapshot_token(cached_payload) -> str:
    """Return the stable generation token for one cached cable snapshot."""
    if isinstance(cached_payload, dict) and cached_payload.get("snapshot_token"):
        return str(cached_payload["snapshot_token"])
    serialized = json.dumps(
        cached_payload.get("links", []) if isinstance(cached_payload, dict) else [],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def assign_cable_row_ids(links):
    """Return copied cable rows with one stable action identity per snapshot row."""
    if not is_list_of_dicts(links):
        return None

    local_id_counts = {}
    for link in links:
        local_id = str(link.get("local_port_id", ""))
        local_id_counts[local_id] = local_id_counts.get(local_id, 0) + 1

    result = []
    seen = set()
    for link in links:
        copied = dict(link)
        local_id = str(copied.get("local_port_id", ""))
        if local_id_counts.get(local_id) == 1 and _CABLE_ROW_ID_RE.fullmatch(local_id):
            row_id = local_id
        else:
            source = str(copied.get("_source") or "main")
            link_id = coerce_librenms_id(copied.get("link_id"))
            if link_id is not None:
                row_id = f"{source}:link:{link_id}"
            else:
                identity = [
                    source,
                    copied.get("local_port_id"),
                    copied.get("protocol"),
                    copied.get("remote_device_id"),
                    copied.get("remote_port_id"),
                    copied.get("remote_device"),
                    copied.get("remote_port"),
                    copied.get("sensor_id"),
                ]
                digest = hashlib.sha256(json.dumps(identity, separators=(",", ":"), default=str).encode()).hexdigest()
                row_id = f"{source}:row:{digest[:32]}"
        if row_id in seen:
            return None
        seen.add(row_id)
        copied["row_id"] = row_id
        result.append(copied)
    return result


def cable_manual_pick_cache_key(snapshot_key, snapshot_token, user_id, row_id) -> str:
    """Return the user- and row-scoped cache key for one unsaved remote pick."""
    row_digest = hashlib.sha256(str(row_id).encode()).hexdigest()
    return f"{snapshot_key}:manual-remote:{snapshot_token}:{user_id}:{row_digest}"


def apply_cable_manual_picks(cache_backend, snapshot_key, cached_payload, user_id, links):
    """Copy *links* and overlay only the current user's manual remote picks."""
    snapshot_token = cable_snapshot_token(cached_payload)
    links = assign_cable_row_ids(links)
    if links is None:
        return [], False
    pick_keys = {
        link["row_id"]: cable_manual_pick_cache_key(snapshot_key, snapshot_token, user_id, link["row_id"])
        for link in links
    }
    picks = cache_backend.get_many(pick_keys.values())
    result = []
    applied = False
    for link in links:
        copied = dict(link)
        pick = picks.get(pick_keys[copied["row_id"]])
        if isinstance(pick, dict) and coerce_librenms_id(pick.get("manual_remote_id")):
            copied.update(
                {
                    "manual_remote_id": pick["manual_remote_id"],
                    "manual_remote": True,
                }
            )
            applied = True
        result.append(copied)
    return result, applied


def advisory_lock_key(lock_identity: str) -> int:
    """Return the signed 64-bit PostgreSQL advisory lock key for *lock_identity*."""
    return int.from_bytes(
        hashlib.blake2b(lock_identity.encode(), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )


def port_binding_lock_identity(port_id, server_key):
    """Return the advisory lock identity of the claim on LibreNMS port *port_id* of *server_key*."""
    return json.dumps(["librenms-port-binding", server_key, port_id], separators=(",", ":"))


def acquire_advisory_transaction_lock(lock_identity: str, *, using: str | None = None) -> None:
    """
    Acquire one stable PostgreSQL advisory lock for the current transaction.

    Args:
        lock_identity (str): Stable string hashed into the advisory lock key.
        using (str | None): Database alias that owns the transaction. Defaults to the default alias.

    Raises:
        RuntimeError: If the selected database connection has no open transaction.

    """
    # Resolve the alias the caller's transaction.atomic(using=...) opened; the default
    # connection can be in autocommit, where the lock would release immediately.
    from django.db import DEFAULT_DB_ALIAS, connections

    connection = connections[using or DEFAULT_DB_ALIAS]

    if not connection.in_atomic_block:
        raise RuntimeError("acquire_advisory_transaction_lock() requires an open transaction")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [advisory_lock_key(lock_identity)])


def is_list_of_dicts(value) -> bool:
    """
    Return True only when *value* is a list whose every element is a dict.

    Used to validate LibreNMS payloads at the API boundary: a ``success=True`` response can
    still carry a malformed-but-truthy body (a string, a list of scalars, etc.), and blindly
    dereferencing it turns a refresh into a 500 instead of the graceful error/fallback path.
    An empty list is considered valid (a device legitimately with no rows).

    Args:
        value: The candidate payload to validate.

    Returns:
        bool: True if *value* is a list of dicts (the empty list included), False otherwise.

    """
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def cache_remaining_ttl(cache, key):
    """
    Return the remaining TTL (seconds) for *key*, degrading to ``None`` off Redis.

    django-redis exposes ``cache.ttl(key)``; other backends (Django's ``LocMemCache`` in tests,
    or any non-Redis deployment) do not, so a bare ``cache.ttl(...)`` raises ``AttributeError``
    and 500s the cache-expiry render. Route every TTL read through here so the behaviour is
    uniform across backends and lives in one place instead of drifting per view.

    Args:
        cache: The Django cache backend instance.
        key: The cache key to inspect.

    Returns:
        int | None: The remaining TTL in seconds, ``None`` when the backend can't report it
            (no ``ttl`` method) or the key has no expiry.

    """
    ttl = getattr(cache, "ttl", None)
    if ttl is None:
        return None
    try:
        return ttl(key)
    except Exception:
        # A backend that HAS .ttl (django-redis) can still raise at call time — e.g. a transient
        # Redis connection error. Guarding only the method's ABSENCE would let that propagate into
        # the cache-expiry render (modules/cables/IP/VLAN/interfaces _build_context) and 500 the very
        # page this helper exists to protect. Degrade the cosmetic TTL to "unknown" instead.
        logger.warning("cache.ttl(%r) failed; treating remaining TTL as unknown", key, exc_info=True)
        return None


# The optional "-" admits Junos names such as xe-1/2/0, like get_virtual_chassis_member does.
_VC_MEMBER_INTERFACE_PATTERN = re.compile(r"^(?P<prefix>[A-Za-z][A-Za-z0-9]*-?)(?P<member>\d+)(?P<suffix>[/:].+)$")


def is_vc_position(value) -> bool:
    """Return whether *value* is a Virtual Chassis position: an int, not a bool, 0 or more."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def convert_speed_to_kbps(speed_bps: int | None) -> int | None:
    """
    Convert speed from bits per second to kilobits per second.

    Args:
        speed_bps (int | None): Speed in bits per second, or None.

    Returns:
        int | None: Speed in kilobits per second, or None if input is None.

    """
    if speed_bps is None:
        return None
    return speed_bps // 1000


def format_mac_address(mac_address: object) -> str:
    """
    Validate and format MAC address string for table display.

    Args:
        mac_address (object): The raw LibreNMS ifPhysAddress value to format.

    Returns:
        str: The MAC address formatted as XX:XX:XX:XX:XX:XX.

    """
    # A device can report ifPhysAddress as an int or a list, so a truthy value is not
    # necessarily a string. Treat any non-string as absent, like the interface view does.
    if not isinstance(mac_address, str) or not mac_address:
        return ""

    mac_address = mac_address.strip().replace(":", "").replace("-", "")

    if len(mac_address) != 12:
        return "Invalid MAC Address"  # Return a message if the address is not valid

    formatted_mac = ":".join(mac_address[i : i + 2] for i in range(0, len(mac_address), 2))
    return formatted_mac.upper()


def index_ip_port_records(ports_by_id):
    """Index cached records once by canonical ID, with None for an ambiguous ID."""
    index = {}
    if isinstance(ports_by_id, dict):
        for raw_id, port in ports_by_id.items():
            port_id = normalize_librenms_port_id(raw_id)
            if port_id is not None and isinstance(port, dict):
                index[port_id] = None if port_id in index else port
    return index


def ip_row_port_record(ports_by_id, port_id):
    """Read one port from the canonical uniqueness index, or None when it is absent or ambiguous."""
    normalized_id = normalize_librenms_port_id(port_id)
    if normalized_id is None or not isinstance(ports_by_id, dict):
        return None
    return ports_by_id.get(normalized_id)


def index_ip_source_interfaces(interfaces, server_key, obj_device_id=None):
    """Index one owner scope with identical ambiguity rules for reads and writes."""
    by_librenms_id = {}
    by_name = {}
    by_pk = {}
    for iface in interfaces:
        # Key by PK for the cached-interface_url fallback (rename-safe identity). Scoped to
        # this object's (and its VC members') interfaces, so a stale URL can never bind the
        # address to an unrelated device's interface — stricter than the old direct .get(id=).
        by_pk[str(iface.pk)] = iface
        lib_id = read_mapping(iface).own_id(server_key)
        if lib_id is not None:
            key = str(lib_id)
            if key in by_librenms_id:
                # Two current interfaces carry the same stored port id — we can't tell which
                # one the IP belongs to. Mark the id ambiguous (None) so _match_interface fails
                # the row safe instead of binding the address to an arbitrary interface.
                by_librenms_id[key] = None
            else:
                by_librenms_id[key] = iface
        # Name fallback: the VIEWED object's OWN interface always wins. NetBox enforces a unique
        # (device, name), so obj has at most one interface of a given name, and binding the IP to
        # it preserves the viewed device as the primary name scope.
        # A sibling VC member only fills a name obj doesn't own; a collision among siblings alone
        # (none on obj) stays ambiguous (None) so the address can't rebind to an arbitrary member.
        iface_is_obj = obj_device_id is not None and getattr(iface, "device_id", None) == obj_device_id
        if iface_is_obj:
            by_name[iface.name] = iface
        elif iface.name not in by_name:
            by_name[iface.name] = iface
        elif by_name[iface.name] is not None and getattr(by_name[iface.name], "device_id", None) != obj_device_id:
            by_name[iface.name] = None
    return by_librenms_id, by_name, by_pk


def resolve_ip_source_interface(row, by_librenms_id, by_name, by_pk=None, *, server_key):
    """Resolve a source interface without falling through an ambiguous or contradicted identity."""
    port_id = normalize_librenms_port_id(row.get("port_id"))
    if port_id is not None and str(port_id) in by_librenms_id:
        return by_librenms_id[str(port_id)]
    name = row.get("interface_name")
    interface_url = row.get("interface_url")
    if isinstance(name, str) and name in by_name:
        candidate = by_name[name]
    elif isinstance(interface_url, str) and by_pk:
        candidate = by_pk.get(interface_url.rstrip("/").rsplit("/", 1)[-1])
    else:
        return None
    # A name or a cached URL never wins over a binding to a different port on this server.
    if candidate is None or not name_match_may_be_port(candidate, server=server_key, port_id=row.get("port_id")):
        return None
    return candidate


def normalize_ip_sync_row_id(value):
    """Normalize a CIDR selection with an optional source-port discriminator."""
    if not isinstance(value, str):
        raise ValueError("IP row identity must be a string.")
    address, separator, raw_port = value.partition("@")
    address = str(parse_address_with_prefix(address))
    if not separator:
        return address
    port_id = normalize_librenms_port_id(raw_port)
    if port_id is None:
        raise ValueError("IP row identity has an invalid source port.")
    return f"{address}@{port_id}"


def identify_ip_sync_rows(rows):
    """Give duplicate source addresses separate keys without trusting cached keys."""
    addresses = []
    counts = {}
    for row in rows:
        address = None
        if isinstance(row, dict):
            try:
                address = str(parse_address_with_prefix(row.get("ip_with_mask")))
            except ValueError:
                pass
        addresses.append(address)
        if address is not None:
            counts[address] = counts.get(address, 0) + 1
    identified = []
    for row, address in zip(rows, addresses, strict=True):
        if not isinstance(row, dict):
            continue
        row_id = address
        if address is not None and counts[address] > 1:
            port_id = normalize_librenms_port_id(row.get("port_id"))
            row_id = f"{address}@{port_id}" if port_id is not None else None
        identified.append({**row, "row_id": row_id})
    return identified


def index_ip_sync_rows(rows):
    """Index current source rows and reject duplicate source identities."""
    index = {}
    duplicates = set()
    for row in identify_ip_sync_rows(rows):
        row_id = row["row_id"]
        if row_id is None:
            continue
        if row_id in index:
            duplicates.add(row_id)
        else:
            index[row_id] = row
    return index, duplicates


def get_interface_port_identity_sets(ports, interface_name_field) -> tuple[set[int], set[int]]:
    """Return unique host and OOB port IDs and the subset with a unique display name across all rows."""
    if not is_list_of_dicts(ports):
        return set(), set()

    port_id_counts = {}
    port_names = {}
    name_counts = {}
    for port in ports:
        # OOB rows count here too: they sync onto this device, and port_id being a LibreNMS
        # global primary key means an OOB id can never collide with a host id.
        port_id = normalize_librenms_port_id(port.get("port_id"))
        interface_name = port.get(interface_name_field)
        if port_id is None:
            continue
        port_id_counts[port_id] = port_id_counts.get(port_id, 0) + 1
        port_names[port_id] = interface_name
        if isinstance(interface_name, str) and interface_name.strip():
            # Host and OOB rows share the target NetBox device's interface namespace.
            name_counts[interface_name] = name_counts.get(interface_name, 0) + 1

    unique_port_ids = {port_id for port_id, count in port_id_counts.items() if count == 1}
    unambiguous_name_port_ids = {
        port_id
        for port_id in unique_port_ids
        if isinstance(port_names[port_id], str)
        and port_names[port_id].strip()
        and name_counts.get(port_names[port_id]) == 1
    }
    return unique_port_ids, unambiguous_name_port_ids


# re.compile raises OverflowError, not re.error, for a repeat count such as a{4294967295}.
REGEX_COMPILE_ERRORS = (re.error, OverflowError)


def validate_regex_field(value, field_name):
    """
    Compile ``value`` as a regex, raising a field-scoped ValidationError on failure.

    Centralizes the ``re.compile`` / ``re.error`` -> ``ValidationError`` validator that was
    copied across the plugin's pattern-bearing models (NormalizationRule,
    CarrierAutoInstallRule, InventoryIgnoreRule, ModuleBayMapping, PortStackLagPattern), so the
    wording stays consistent and any future hardening lives in one place.

    ReDoS note (low severity, accepted): these patterns are admin-supplied (change permission)
    and are later evaluated via ``re.search()`` / ``re.match()`` — including
    ``lag_name_pattern``, run per port on every interface refresh. Python's ``re`` has no
    per-evaluation timeout, so a catastrophic-backtracking pattern (e.g. ``^(a+)+$``) could
    block the worker. This is accepted as an admin-only risk bounded by the fields'
    ``max_length``; a real guard would need an out-of-process or timeout-capable regex engine
    the plugin doesn't depend on.

    Args:
        value (str): The regex source to validate.
        field_name (str): The model field name the error is attached to.

    Returns:
        re.Pattern: The compiled pattern (callers that need it for further checks reuse it).

    Raises:
        ValidationError: ``{field_name: "Invalid regex: <detail>"}`` when ``value`` won't compile.

    """
    try:
        return re.compile(value)
    except REGEX_COMPILE_ERRORS as exc:
        raise ValidationError({field_name: f"Invalid regex: {exc}"}) from exc


def normalize_relationship_maps(relationships) -> tuple[dict, dict, dict]:
    """
    Normalize a cached ``port_stack_relationships`` mapping into int-keyed relationship maps.

    Returns ``(lag_members, sub_interfaces, bridge_members)``. This is the single home for the corruption guard + key
    normalization shared by :func:`build_relationship_maps` and the bulk sync writer, so the
    readers and writer cannot drift.

    Fails soft against a corrupt / partial-write / format-migrated cache: a None or non-dict
    ``relationships`` (e.g. a list), or a present-but-None / non-dict nested ``lag_members`` /
    ``sub_interfaces`` / ``bridge_members``, collapses to ``{}`` so ``.items()`` never raises
    ``AttributeError``. Keys
    and values are normalized via :func:`normalize_librenms_port_id` so int-keyed lookups never miss
    stringified JSON values. An edge is dropped unless both endpoint IDs are valid.

    Args:
        relationships (object): Cached relationship mapping to normalize.

    Returns:
        tuple[dict[int, int], dict[int, int], dict[int, int]]: Normalized relationship maps.

    """
    if not isinstance(relationships, dict):
        relationships = {}
    lag_members_raw = relationships.get("lag_members")
    if not isinstance(lag_members_raw, dict):
        lag_members_raw = {}
    sub_interfaces_raw = relationships.get("sub_interfaces")
    if not isinstance(sub_interfaces_raw, dict):
        sub_interfaces_raw = {}
    bridge_members_raw = relationships.get("bridge_members")
    if not isinstance(bridge_members_raw, dict):
        bridge_members_raw = {}

    def _normalize_edges(raw_edges):
        normalized = {}
        conflicted_sources = set()
        for key, value in raw_edges.items():
            normalized_key = normalize_librenms_port_id(key)
            normalized_value = normalize_librenms_port_id(value)
            if normalized_key is None or normalized_value is None or normalized_key in conflicted_sources:
                continue
            existing_value = normalized.get(normalized_key)
            if existing_value is not None and existing_value != normalized_value:
                normalized.pop(normalized_key, None)
                conflicted_sources.add(normalized_key)
                continue
            normalized[normalized_key] = normalized_value
        return normalized

    lag_members = _normalize_edges(lag_members_raw)
    sub_interfaces = _normalize_edges(sub_interfaces_raw)
    bridge_members = _normalize_edges(bridge_members_raw)
    return lag_members, sub_interfaces, bridge_members


def normalize_stacked_ports(relationships) -> dict[int, list[int]]:
    """
    Normalize the cached ``stacked_ports`` adjacency into an int-keyed map.

    LibreNMS reports a port_stack pair without saying what kind of relationship it is or which
    side is the composite. A pair that matches no rule is carried here rather than dropped, so a
    row can still say which ports it is stacked with. The map is symmetric and states no
    direction, which is why the relationship writer never reads it: there is nothing to write.

    Fails soft against a corrupt or format-migrated cache, like
    :func:`normalize_relationship_maps`: a non-dict value, a non-list partner list, an unusable
    port id or a self-edge is dropped rather than raising.

    Args:
        relationships (object): Cached ``port_stack_relationships`` mapping.

    Returns:
        dict[int, list[int]]: Each port's partner port ids, deduplicated and sorted.

    """
    if not isinstance(relationships, dict):
        return {}
    raw_adjacency = relationships.get("stacked_ports")
    if not isinstance(raw_adjacency, dict):
        return {}
    normalized: dict[int, list[int]] = {}
    for raw_port_id, raw_partners in raw_adjacency.items():
        port_id = normalize_librenms_port_id(raw_port_id)
        if port_id is None or not isinstance(raw_partners, list):
            continue
        partners = {
            partner_id
            for raw_partner in raw_partners
            if (partner_id := normalize_librenms_port_id(raw_partner)) is not None and partner_id != port_id
        }
        if partners:
            normalized[port_id] = sorted(partners)
    return normalized


def invert_relationship_edges(edges) -> dict[int, list[int]]:
    """
    Invert one ``member -> aggregate`` edge map into ``aggregate -> [member, ...]``.

    The three maps :func:`normalize_relationship_maps` returns all point upward, so a row can
    name what it is attached to but an aggregate cannot name what is attached to it. This is the
    one place the direction flips, shared by the LAG, sub-interface and bridge views.

    Members are ordered by port id so two renders of one snapshot cannot disagree. A self-edge is
    dropped: a port is not its own member, so counting one would badge an aggregate that has none.
    The resolver cannot emit one, but a cached map can hold one, and the writer relies on NetBox's
    own ``clean()`` to refuse it rather than on the map being filtered.

    Args:
        edges (dict[int, int]): A normalized ``member -> aggregate`` map.

    Returns:
        dict[int, list[int]]: Members grouped under their aggregate, each list sorted.

    """
    inverted: dict[int, list[int]] = {}
    for member_id, aggregate_id in edges.items():
        if member_id == aggregate_id:
            continue
        inverted.setdefault(aggregate_id, []).append(member_id)
    for members in inverted.values():
        members.sort()
    return inverted


def get_cable_sync_settings(*, lock=False):
    """
    Return the settings row driving the cable-sync provenance stamp (tag name/color, description).

    These settings are DB/UI-managed (plugin Settings → Cable Sync), not PLUGINS_CONFIG. Operators
    can change their preferences without a NetBox restart. This split matches the
    SerialSensorTypePattern rows. The function falls back to an UNSAVED instance (pure field
    defaults) when no row exists yet, so read paths never write.

    Args:
        lock (bool): Whether to lock the settings row and create it when it does not exist.

    Returns:
        LibreNMSSettings: The saved settings row, or an unsaved instance with field defaults.

    """
    from netbox_librenms_plugin.models import LibreNMSSettings

    if lock:
        settings, _created = LibreNMSSettings.objects.select_for_update().get_or_create(pk=1)
        return settings
    return LibreNMSSettings.objects.first() or LibreNMSSettings()


def normalize_cable_tag_slug(name):
    """Return the tag slug, or reject a name that cannot produce one."""
    from django.utils.text import slugify

    slug = slugify(name)
    if not slug:
        raise ValidationError("The tag name must contain letters or numbers.")
    return slug


def _free_cable_tag_slug(slug):
    """Return the first tag slug derived from *slug* that no Tag currently uses."""
    from extras.models import Tag

    max_length = Tag._meta.get_field("slug").max_length
    candidate = slug[:max_length]
    suffix = 2
    while Tag.objects.filter(slug=candidate).exists():
        suffix_text = f"-{suffix}"
        candidate = f"{slug[: max_length - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate


def _create_cable_tag(name, slug, color):
    """Create (or adopt) the provenance tag, letting a taken slug surface."""
    from extras.models import Tag

    tag, _ = Tag.objects.get_or_create(name=name, defaults={"slug": _free_cable_tag_slug(slug), "color": color})
    return tag


def get_librenms_cable_tag(*, create=True, sync_settings=None):
    """
    Return (creating on first use) the Tag stamped on cables the LibreNMS cable sync creates.

    The tag name and color come from the settings row (see :func:`get_cable_sync_settings`).
    Its purpose is provenance: it marks cables that the plugin created or adopted. Destructive
    replacement still requires explicit confirmation for every cable. Mirrors the
    ``get_or_create(..., defaults=...)`` idiom used to bootstrap the ``librenms_id`` custom field.

    Returns:
        extras.models.Tag: The (existing or newly created) provenance tag.

    """
    from extras.models import Tag

    sync_settings = sync_settings or get_cable_sync_settings()
    name = sync_settings.cable_sync_tag
    if tag := Tag.objects.filter(name=name).first():
        return tag
    if not create:
        return None

    # Derive the slug only once a create is actually required: normalize_cable_tag_slug() raises
    # for a name that slugifies to nothing, and LibreNMSSettings.save() does not full_clean(), so a
    # direct ORM or API write can store one. Read paths such as the Cables tab render call this with
    # create=False and must report "no provenance tag" rather than raise.
    slug = normalize_cable_tag_slug(name)
    color = sync_settings.cable_sync_tag_color
    try:
        return _create_cable_tag(name, slug, color)
    except IntegrityError:
        # get_or_create only re-reads by name, so a concurrent insert that took the candidate slug
        # reaches here instead. Adopt the tag that name now points at, else search for a free slug once more.
        if tag := Tag.objects.filter(name=name).first():
            return tag
        return _create_cable_tag(name, slug, color)


def _cable_tag_ids(cable) -> set:
    """Return the set of tag primary keys currently on *cable*."""
    prefetched = getattr(cable, "_prefetched_objects_cache", {}).get("tags")
    if prefetched is not None:
        return {tag.pk for tag in prefetched}
    return set(cable.tags.values_list("pk", flat=True))


_PROVENANCE_TAG_UNSET = object()


def cable_has_librenms_tag(cable, provenance_tag=_PROVENANCE_TAG_UNSET) -> bool:
    """Return True when *cable* carries the librenms provenance tag (possibly among others)."""
    if provenance_tag is _PROVENANCE_TAG_UNSET:
        provenance_tag = get_librenms_cable_tag(create=False)
    return provenance_tag is not None and provenance_tag.pk in _cable_tag_ids(cable)


def cable_is_point_to_point(cable) -> bool:
    """Return whether *cable* has exactly one termination on each end."""
    prefetched = getattr(cable, "_prefetched_objects_cache", {}).get("terminations")
    if prefetched is not None:
        end_counts = {"A": 0, "B": 0}
        for termination in prefetched:
            if termination.cable_end in end_counts:
                end_counts[termination.cable_end] += 1
        return end_counts == {"A": 1, "B": 1}
    return len(cable.a_terminations) == 1 and len(cable.b_terminations) == 1


def classify_cable_action(local_term, remote_term, provenance_tag=_PROVENANCE_TAG_UNSET) -> dict:
    """
    Decide what syncing a cable between *local_term* and *remote_term* should do.

    The returned ``action`` is one of:

    - ``"create"``          -- neither endpoint is cabled; make a fresh (enriched) cable.
    - ``"noop"``            -- the desired cable already exists and already carries the librenms
      tag; nothing to do.
    - ``"tag_only"``        -- the desired cable already exists but is untagged; add the librenms
      tag (non-destructive, always allowed without force).
    - ``"needs_force"``     -- a different cable occupies an endpoint; require explicit
      confirmation of the exact current cable IDs before deletion.
    - ``"unsupported"``     -- an endpoint uses a multi-termination cable, which one row cannot
      safely replace or adopt.

    A cable "already connecting" the two ends means both terminate the SAME single NetBox cable; a
    multi-segment remodel (through patch panels) does NOT — its first segment lands on a panel port,
    so it correctly falls into the re-point branch and is protected.

    Args:
        local_term: The near-side termination (ConsoleServerPort / Interface).
        remote_term: The far-side termination (ConsolePort / Interface).
        provenance_tag: Optional tag used to identify cables managed by this plugin.

    Returns:
        dict: ``{"action": str, "to_remove": [Cable, ...], "cable": Cable | None}`` where
            ``to_remove`` lists the cables an overwrite would delete and ``cable`` is the existing
            desired-connection cable (for the noop / tag_only cases), else ``None``.

    """
    local_cable = local_term.cable
    remote_cable = remote_term.cable

    if local_cable is None and remote_cable is None:
        return {"action": "create", "to_remove": [], "cable": None}

    occupying_cables = {cable for cable in (local_cable, remote_cable) if cable is not None}
    if any(not cable_is_point_to_point(cable) for cable in occupying_cables):
        return {"action": "unsupported", "to_remove": [], "cable": None}

    # The exact desired connection already exists: both ends terminate the SAME single cable.
    if local_cable is not None and local_cable == remote_cable:
        if cable_has_librenms_tag(local_cable, provenance_tag):
            return {"action": "noop", "to_remove": [], "cable": local_cable}
        return {"action": "tag_only", "to_remove": [], "cable": local_cable}

    # A different cable occupies at least one endpoint -> a re-point. Every occupying cable must be
    # removed first (NetBox forbids a second cable on an already-terminated endpoint).
    to_remove = []
    for cable in (local_cable, remote_cable):
        if cable is not None and cable not in to_remove:
            to_remove.append(cable)
    return {"action": "needs_force", "to_remove": to_remove, "cable": None}


def cable_far_terminations(cable, near_termination) -> list:
    """
    Return the terminations on the opposite side of *cable* from *near_termination*.

    The direct far end of one physical cable — NOT the traced path end (see
    :func:`cable_path_reaches` for that). Used to tell whether an existing cable already lands
    on the LibreNMS-desired endpoint.

    Args:
        cable: The NetBox ``Cable`` to inspect.
        near_termination: The termination whose opposite side is wanted.

    Returns:
        list: The other side's terminations (``b_terminations`` when *near_termination* is on
            the A side, else ``a_terminations``).

    """
    from netbox_librenms_plugin.breakout_cables import breakout_far_end, is_channel

    # A channel is no termination: its far end is the one at its connector of the parent's cable.
    if is_channel(near_termination):
        far_end = breakout_far_end(near_termination, near_termination.parent, cable)
        return [] if far_end is None else [far_end]
    # Serial table enrichment prefetches the generic CableTermination rows and their concrete
    # objects for the whole page. Use that cache when present. The a_terminations/b_terminations
    # properties otherwise issue one concrete-object query per cable.
    prefetched = getattr(cable, "_prefetched_objects_cache", {}).get("terminations")
    if prefetched is not None:
        near_end = next(
            (termination.cable_end for termination in prefetched if termination.termination == near_termination),
            None,
        )
        if near_end is not None:
            far_end = "B" if near_end == "A" else "A"
            return [termination.termination for termination in prefetched if termination.cable_end == far_end]

    a_terminations = list(cable.a_terminations)
    if near_termination in a_terminations:
        return list(cable.b_terminations)
    return a_terminations


def cable_path_reaches(
    termination,
    remote_termination=None,
    remote_device=None,
    path=None,
    remote_termination_type=None,
) -> bool:
    """
    Return True when *termination*'s traced cable path ends at the given remote.

    Follows patch-panel front/rear pass-throughs via ``termination.trace()``, so a remodeled
    multi-segment path still counts as "reaching" its end device — the basis for treating a
    remodel as a better model of the same link rather than a mismatch to re-sync.

    Args:
        termination: The near-side termination (Interface / ConsoleServerPort) to trace from.
        remote_termination: Match when the path ends at exactly this termination.
        remote_device: Match when the path ends at ANY port on this device — the serial case,
            where the LibreNMS label only identifies the device, not the port.
        path: An already-computed ``termination.trace()`` result to reuse (avoids re-tracing).
        remote_termination_type: When set, a device match must also use this termination type.

    Returns:
        bool: True when the traced path's far end matches either criterion.

    """
    if path is None:
        path = termination.trace()
    if not path:
        return False
    for far_termination in path[-1][2] or []:
        if remote_termination is not None and far_termination == remote_termination:
            return True
        if (
            remote_device is not None
            and getattr(far_termination, "device_id", None) == remote_device.pk
            and (remote_termination_type is None or isinstance(far_termination, remote_termination_type))
        ):
            return True
    return False


def _object_is_visible(obj, user, cache=None) -> bool:
    """
    Return whether *user* may view one concrete NetBox object.

    Fails closed without a user: the production caller always supplies the request's user, so a
    missing one means the caller could not establish an identity, not that everything is public.

    Args:
        obj (models.Model): The NetBox object whose visibility is checked.
        user (object | None): The user the labels are rendered for.
        cache (dict | None): Optional dict memoising the answer per object for one render, so a
            long trace does not repeat the same restricted lookup at every hop.

    Returns:
        bool: Whether the user may view the object.

    """
    if user is None:
        return False
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    model = type(obj)
    key = (model._meta.label_lower, obj.pk)
    if cache is not None and key in cache:
        return cache[key]
    permission = f"{model._meta.app_label}.view_{model._meta.model_name}"
    if not user.has_perm(permission):
        visible = False
    else:
        visible = model.objects.restrict(user, "view").filter(pk=obj.pk).exists()
    if cache is not None:
        cache[key] = visible
    return visible


def _termination_label(terminations, user=None, cache=None) -> str:
    """Render a trace node's terminations as ``Device: Port`` (comma-joined), or a dash if empty."""
    labels = []
    for term in terminations or []:
        device = getattr(term, "device", None)
        if not _object_is_visible(term, user, cache) or (
            device is not None and not _object_is_visible(device, user, cache)
        ):
            labels.append("Restricted")
            continue
        device_name = getattr(device, "name", None)
        labels.append(f"{device_name}: {term}" if device_name else str(term))
    return ", ".join(labels) if labels else "—"


def _cable_label(segment_cable, user=None, cache=None) -> Optional[str]:
    """Render a trace segment's cable (or list of cables) as ``#pk``; None for a passthrough gap."""
    if segment_cable is None:
        return None
    if isinstance(segment_cable, (list, tuple)):
        joined = ", ".join(
            f"#{c.pk}" if _object_is_visible(c, user, cache) else "Restricted" for c in segment_cable if c is not None
        )
        return joined or None
    return f"#{segment_cable.pk}" if _object_is_visible(segment_cable, user, cache) else "Restricted"


def render_cable_trace(cable, user=None) -> list:
    """
    Return *cable*'s full end-to-end physical path as an ordered list of hop dicts.

    Uses NetBox's ``termination.trace()`` (which follows patch-panel front/rear pass-throughs all
    the way to the end device). It starts from the first cable termination that supports tracing.
    The overwrite-confirm modal then shows every segment that a forced overwrite would affect.

    Args:
        cable: The NetBox ``Cable`` whose end-to-end path is rendered.
        user: Optional user whose object scope controls labels in the rendered trace.

    Returns:
        list[dict]: One ``{"near": str, "cable": str | None, "far": str}`` entry per traced
            segment, in order from the trace origin (labels read ``Device: Port``). Empty when no
            cable termination supports tracing.

    """
    terminations = [*cable.a_terminations, *cable.b_terminations]
    origin = next((termination for termination in terminations if callable(getattr(termination, "trace", None))), None)
    if origin is None:
        return []
    hops = []
    visibility_cache = {}
    for near, segment_cable, far in origin.trace():
        hops.append(
            {
                "near": _termination_label(near, user, visibility_cache),
                "cable": _cable_label(segment_cable, user, visibility_cache),
                "far": _termination_label(far, user, visibility_cache),
            }
        )
    return hops


def get_virtual_chassis_member(
    device: Device,
    port_name: str,
    members_by_position: dict | None = None,
    *,
    return_device_on_failure: bool = True,
) -> Device | None:
    """
    Determine the likely virtual chassis member from the device's vc_position and port name.

    Args:
        device (Device): The NetBox device instance.
        port_name (str): The name of the port (e.g., 'Ethernet1').
        members_by_position (dict | None): Optional prefetched ``{vc_position: member Device}``
            map. When provided, the member is resolved from it instead of issuing a per-call
            ``members.get(vc_position=...)`` query — pass it when resolving many rows of the same
            chassis (e.g. a VC interface table) to keep resolution O(1) per row instead of one
            query per row. An EMPTY map is treated like no map (falls back to the per-call
            query), so a caller forwarding a raw possibly-empty dict can't silently lose
            member resolution.
        return_device_on_failure: Return ``device`` when member matching fails. Callers
            resolving a remote VC endpoint can disable this fallback to avoid binding the
            advertised port against the wrong member.

    Returns:
        Device | None: The matching virtual chassis member. By default, returns the
            original device if matching fails; returns ``None`` instead when
            ``return_device_on_failure`` is false.

    """
    fallback = device if return_device_on_failure else None
    if not hasattr(device, "virtual_chassis") or not device.virtual_chassis:
        return fallback

    # A port row can lack the selected name field entirely (port.get(...) -> None) — or carry a
    # non-string value from a malformed payload. re.match() raises TypeError on those, which the
    # except tuple below deliberately doesn't cover (it must not mask real bugs), so guard here
    # and fall back to the viewed device like any other unmatchable name.
    if not isinstance(port_name, str):
        return fallback

    try:
        match = re.match(r"^[A-Za-z]+-?(\d+)", port_name)
        if not match:
            # Some stack operating systems use a slot/port form with no alphabetic prefix,
            # such as ``2/1/1``. Require the slash so a bare numeric label is not treated as a
            # chassis position.
            match = re.match(r"^(\d+)/", port_name)
        if not match:
            return fallback

        # Get the port number and use it
        vc_position = int(match.group(1))
        if members_by_position:
            return members_by_position.get(vc_position, fallback)
        return device.virtual_chassis.members.get(vc_position=vc_position)
    except (ValueError, ObjectDoesNotExist):
        return fallback


def resolve_interface_row_device(
    device: Device,
    port: dict,
    interface_name_field: str,
    *,
    interfaces_by_port_id: dict | None = None,
    members_by_position: dict | None = None,
    members_by_id: dict | None = None,
    return_device_on_failure: bool = True,
) -> Device | None:
    """
    Resolve a LibreNMS interface row to its likely Virtual Chassis member.

    Prefer a unique interface already bound to the stable LibreNMS port ID. For an unbound
    physical Ethernet row, use the member position encoded in the interface name. Do not apply
    that heuristic to logical rows or sub-interfaces because their number is not reliably a
    chassis position. Set ``return_device_on_failure`` to false when a writer must reject a row
    whose owner has no stable evidence.

    Args:
        device (Device): Device that LibreNMS represents as the logical chassis.
        port (dict): LibreNMS interface row to resolve.
        interface_name_field (str): Port field that contains the selected interface name.
        interfaces_by_port_id (dict | None): Interfaces indexed by stable LibreNMS port ID.
        members_by_position (dict | None): Virtual Chassis members indexed by position.
        members_by_id (dict | None): Virtual Chassis members indexed by primary key.
        return_device_on_failure (bool): Return ``device`` when no stable owner can be resolved.

    Returns:
        Device | None: Resolved Virtual Chassis member, the fallback device, or ``None``.

    """
    if not getattr(device, "virtual_chassis", None):
        return device
    fallback = device if return_device_on_failure else None
    if not isinstance(port, dict):
        return fallback

    port_id = normalize_librenms_port_id(port.get("port_id"))
    if port_id is not None and interfaces_by_port_id:
        matches = interfaces_by_port_id.get(port_id, [])
        if not isinstance(matches, (list, tuple, set)):
            matches = [matches]
        matches = [interface for interface in matches if interface is not None]
        if len(matches) == 1:
            owner_id = getattr(matches[0], "device_id", None)
            if owner_id is not None:
                if members_by_id and owner_id in members_by_id:
                    return members_by_id[owner_id]
                return matches[0].device

    raw_if_type = port.get("ifType")
    if_type = raw_if_type.lower() if isinstance(raw_if_type, str) else ""
    if "ethernet" not in if_type:
        return fallback

    candidate_names = []
    for field in (interface_name_field, "ifName", "ifDescr"):
        name = port.get(field)
        if isinstance(name, str) and name and name not in candidate_names:
            candidate_names.append(name)
    candidates = {
        member.pk: member
        for name in candidate_names
        if (
            member := get_virtual_chassis_member(
                device,
                name,
                members_by_position=members_by_position,
                return_device_on_failure=False,
            )
        )
        is not None
    }
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    return fallback


def get_virtual_chassis_members(device: Device) -> list:
    """
    Return all member Devices of a device's virtual chassis.

    Centralizes VC member expansion so callers don't hand-roll
    ``device.virtual_chassis.members.values_list(...)`` and can't drift on which
    members are considered. LibreNMS treats a virtual chassis as one logical device,
    so member-spanning lookups (e.g. resolving an interface/IP that may live on
    another member) must always consider the full member set this returns.

    Args:
        device (Device): The device whose virtual chassis members are expanded.

    Returns:
        list: All member Devices (including *device* itself), or ``[device]`` when
            it isn't in a virtual chassis.

    """
    vc = getattr(device, "virtual_chassis", None)
    members = getattr(vc, "members", None) if vc is not None else None
    if members is None or not hasattr(members, "all"):
        return [device]
    try:
        return list(members.all())
    except Exception:
        # Non-enumerable / broken membership: fall back to the documented [device] rather than
        # letting the enumeration error bubble out of this centralizing helper. Log it (don't
        # swallow silently) so a real DB/relation error isn't masked as "device has no VC
        # siblings", which would silently skip sibling-member interface/IP matching.
        logger.warning(
            "Failed to enumerate virtual chassis members for %s; treating as standalone.",
            getattr(device, "name", device),
            exc_info=True,
        )
        return [device]


def get_vc_member_positions(device: Device) -> set[int]:
    """Return known VC member positions for a device, including the device itself."""
    positions = set()

    own_position = getattr(device, "vc_position", None)
    if is_vc_position(own_position):
        positions.add(own_position)

    vc = getattr(device, "virtual_chassis", None)
    members = getattr(vc, "members", None) if vc is not None else None
    if members is None or not hasattr(members, "values_list"):
        return positions

    try:
        raw_positions = members.values_list("vc_position", flat=True)
    except Exception:
        return positions

    try:
        iterator = iter(raw_positions)
    except TypeError:
        return positions

    for raw_position in iterator:
        try:
            parsed = int(raw_position)
        except (TypeError, ValueError):
            continue
        if is_vc_position(parsed):
            positions.add(parsed)

    return positions


def rewrite_interface_name_for_vc_member(
    interface_name: str, vc_position: int, member_positions: set[int] | None = None
) -> str | None:
    """Rewrite a template/interface name to the selected VC member position when appropriate."""
    if not interface_name or not is_vc_position(vc_position):
        return None
    match = _VC_MEMBER_INTERFACE_PATTERN.match(interface_name)
    if not match:
        return None

    try:
        current_position = int(match.group("member"))
    except (TypeError, ValueError):
        return None

    if member_positions is not None and current_position not in member_positions:
        return None

    if current_position == vc_position:
        return interface_name

    return f"{match.group('prefix')}{vc_position}{match.group('suffix')}"


def _instantiate_module_template_interface_specs(device: Device, module) -> list[tuple[str, str]]:
    """Return instantiated template names and types after VC rewriting."""
    if device is None:
        return []

    template_manager = getattr(getattr(module, "module_type", None), "interfacetemplates", None)
    if template_manager is None or not hasattr(template_manager, "all"):
        return []

    vc_position = getattr(device, "vc_position", None)
    vc_id = getattr(device, "virtual_chassis_id", None)
    member_positions = None
    if is_vc_position(vc_position) and isinstance(vc_id, int):
        member_positions = get_vc_member_positions(device)

    template_specs = []
    name_indexes = {}
    for template in template_manager.all():
        try:
            instance = template.instantiate(device=device, module=module)
        except Exception:
            logger.debug(
                "instantiate() failed for template %r; skipping",
                getattr(template, "name", None),
                exc_info=True,
            )
            continue

        name = (getattr(instance, "name", "") or "").strip()
        if not name:
            continue

        if member_positions is not None:
            rewritten_name = rewrite_interface_name_for_vc_member(
                name,
                vc_position,
                member_positions=member_positions,
            )
            if rewritten_name:
                name = rewritten_name

        if name in name_indexes:
            template_specs[name_indexes[name]] = (name, "")
        else:
            name_indexes[name] = len(template_specs)
            template_specs.append((name, getattr(template, "type", "") or ""))

    return template_specs


def get_module_template_interface_specs(device: Device, module) -> list[tuple[str, str]]:
    """Return unique instantiated interface-template names and their attributable types."""
    template_specs = _instantiate_module_template_interface_specs(device, module)
    names = [name for name, _template_type in template_specs]
    # One send answers the common case. Only a receiver that actually renames costs one send per
    # template, because a batch answer cannot prove which input each returned name came from.
    if predict_module_interface_rename(device, module, names) == names:
        return template_specs

    predicted_specs = []
    name_indexes = {}
    for name, template_type in template_specs:
        predicted_name = predict_module_interface_rename(device, module, [name])[0]
        if predicted_name in name_indexes:
            predicted_specs[name_indexes[predicted_name]] = (predicted_name, "")
        else:
            name_indexes[predicted_name] = len(predicted_specs)
            predicted_specs.append((predicted_name, template_type))

    return predicted_specs


class ModuleInterfaceNames(NamedTuple):
    """The interface names of a module that this plugin may move into the module."""

    names: list[str]
    # True when the prediction expands the templates into a family: the naming plugin owns its other rows.
    family: bool

    def may_move(self, name: str) -> bool:
        """Return whether this plugin may set the module on the interface row named *name*."""
        return not self.family or name in self.names


def get_module_adoptable_interface_names(device: Device, module) -> ModuleInterfaceNames:
    """Return the predicted template names, or only the raw ones when the prediction expands them into a family."""
    template_names = [name for name, _template_type in _instantiate_module_template_interface_specs(device, module)]
    predicted_names = _predict_template_interface_names(device, module, template_names)
    if len(predicted_names) > len(template_names):
        return ModuleInterfaceNames(template_names, True)
    return ModuleInterfaceNames(predicted_names, False)


def _predict_template_interface_names(device: Device, module, template_names: list[str]) -> list[str]:
    """Return the names the prediction receivers give for the instantiated template names."""
    from netbox_librenms_plugin.signals import predict_module_interface_names

    # Adoption accepts a receiver's whole-list answer, while typed specs require one result per template.
    # send_robust (not send): this is a public extension point, so a buggy third-party
    # receiver must not break the module-adoption flow. send_robust isolates each receiver
    # and returns the Exception in place of its result; we log and skip those, preserving
    # the documented "last non-None return wins" ordering for the receivers that succeed.
    for _receiver, returned in predict_module_interface_names.send_robust(
        sender=type(module), device=device, module=module, names=list(template_names)
    ):
        if isinstance(returned, Exception):
            logger.warning("predict_module_interface_names receiver failed: %s", returned)
            continue
        if returned is None:
            continue
        # Only accept a non-string sequence of strings. A bare str would be split into
        # characters by list(); a dict would collapse to its keys — either silently
        # corrupts the name list, so reject and keep the prior receiver's result.
        if isinstance(returned, (str, bytes)) or not isinstance(returned, (list, tuple)):
            logger.warning(
                "predict_module_interface_names receiver returned %s, expected a list/tuple of names; ignoring",
                type(returned).__name__,
            )
            continue
        if not all(isinstance(name, str) for name in returned):
            logger.warning("predict_module_interface_names receiver returned non-string element(s); ignoring")
            continue
        # De-dup while preserving order: the caller built template_names as a
        # unique list, so a receiver returning repeats must not reintroduce dupes.
        template_names = list(dict.fromkeys(returned))

    return template_names


def predict_module_interface_rename(device: Device, module, names) -> list[str]:
    """
    Return the INR-predicted post-rename names for an explicit list of *names*.

    Same receiver semantics as :func:`get_module_adoptable_interface_names`
    (``send_robust`` isolates a failing receiver and the last non-None list return
    wins), but the input is a caller-supplied list of names rather than template
    instantiations. Used to recognise when a standalone interface is the renamed
    twin of a module's raw interface (the rename a naming plugin would apply but
    skipped because the target name was already taken).

    Args:
        device (Device): Device that owns the module interfaces.
        module (object): Module supplied to interface-name prediction receivers.
        names (list[str]): Interface names to send to prediction receivers.

    Returns:
        list[str]: Predicted names in the same order as the input names.

    """
    from netbox_librenms_plugin.signals import predict_module_interface_names

    predicted = list(names)
    for _receiver, returned in predict_module_interface_names.send_robust(
        sender=type(module), device=device, module=module, names=list(names)
    ):
        if isinstance(returned, Exception):
            logger.warning("predict_module_interface_names receiver failed: %s", returned)
            continue
        if returned is None:
            continue
        # send_robust only isolates a receiver that *raises*; a receiver that successfully returns a
        # scalar still reaches here. A str/bytes is iterable but list("Gi0/0") would explode it into
        # characters and mispair the rename, and a non-iterable (e.g. 1) would TypeError-500 the
        # module sync path. Reject both and keep the previous prediction.
        if isinstance(returned, (str, bytes)):
            logger.warning(
                "predict_module_interface_names receiver returned a scalar string result; "
                "ignoring it to avoid mispairing a module interface rename"
            )
            continue
        try:
            returned = list(returned)
        except TypeError:
            logger.warning(
                "predict_module_interface_names receiver returned a non-iterable result; "
                "ignoring it to avoid mispairing a module interface rename"
            )
            continue
        # Enforce the 1:1, order-preserving contract at the boundary: a receiver that returns a
        # shorter/longer (or filtered) list would otherwise misalign the caller's
        # ``zip(raw_ifaces, predicted)`` and fold a raw duplicate into the WRONG adopted interface —
        # a silent wrong-object write of a LibreNMS binding. Reject a misaligned result and keep the
        # previous prediction rather than trust it.
        if len(returned) != len(names):
            logger.warning(
                "predict_module_interface_names receiver returned %d names for %d inputs; "
                "ignoring its misaligned result to avoid mispairing a module interface rename",
                len(returned),
                len(names),
            )
            continue
        predicted = returned

    return predicted


def detect_vc_normalization_noop(device: Device, module) -> Optional[dict]:
    """
    Return a diagnostic dict when VC member-name rewriting would no-op for this module.

    "No-op" here means: the device is a VC member but none of the module's
    instantiated template names match the VC member-position regex, so
    rewrite_interface_name_for_vc_member can't transform them. This is the
    signature of an unsupported vendor naming convention and is what a user
    would want to report so support can be added.

    Returns None when:
      - The device isn't a VC member
      - The module has no instantiatable templates
      - At least one instantiated name matches the regex (rewriting is working
        or unnecessary)

    Args:
        device (Device): Device that owns the module.
        module (object): Module whose interface templates are checked.

    Returns:
        Optional[dict]: Diagnostic details, or ``None`` when normalization is not a no-op.

    """
    vc_position = getattr(device, "vc_position", None)
    vc_id = getattr(device, "virtual_chassis_id", None)
    # bool is a subclass of int; reject explicitly so True/False can't masquerade.
    if isinstance(vc_id, bool):
        return None
    if not (is_vc_position(vc_position) and isinstance(vc_id, int)):
        return None

    template_manager = getattr(getattr(module, "module_type", None), "interfacetemplates", None)
    if template_manager is None or not hasattr(template_manager, "all"):
        return None

    template_pairs = []
    any_regex_match = False
    for template in template_manager.all():
        raw_name = (getattr(template, "name", "") or "").strip()
        try:
            instance = template.instantiate(device=device, module=module)
        except Exception:
            logger.debug(
                "instantiate() failed for template %r; skipping",
                raw_name,
                exc_info=True,
            )
            continue
        instantiated_name = (getattr(instance, "name", "") or "").strip()
        if not instantiated_name:
            continue
        if _VC_MEMBER_INTERFACE_PATTERN.match(instantiated_name):
            any_regex_match = True
        template_pairs.append((raw_name, instantiated_name))

    if not template_pairs or any_regex_match:
        return None

    module_type = getattr(module, "module_type", None)
    manufacturer = getattr(module_type, "manufacturer", None)
    device_type = getattr(device, "device_type", None)
    module_bay = getattr(module, "module_bay", None)

    return {
        "manufacturer_slug": getattr(manufacturer, "slug", None),
        "device_type_model": getattr(device_type, "model", None),
        "module_type_model": getattr(module_type, "model", None),
        "module_bay_name": getattr(module_bay, "name", None),
        "vc_position": vc_position,
        "vc_member_positions": sorted(get_vc_member_positions(device)),
        "template_pairs": template_pairs,
        "regex": _VC_MEMBER_INTERFACE_PATTERN.pattern,
    }


_OPTIONAL_SUFFIX = " _(optional, you can remove this line)_"


def build_vc_normalization_report(diagnostic: dict) -> str:
    """
    Render a VC-normalization no-op diagnostic as a markdown blob for a GitHub issue.

    Catalog identifiers (manufacturer/device type/module type/bay name) get an
    "(optional, you can remove this line)" suffix so users who treat their HW
    inventory as confidential can strip them before pasting.

    Args:
        diagnostic (dict): VC-normalization diagnostic details.

    Returns:
        str: Markdown report for a GitHub issue.

    """
    import sys

    from netbox_librenms_plugin import __version__ as plugin_version

    try:
        from django.conf import settings

        netbox_version = getattr(settings, "RELEASE", None) or getattr(settings, "VERSION", None) or "?"
    except Exception:
        netbox_version = "?"
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    def _line(label, value, optional=False):
        if value in (None, ""):
            value = "_(unknown)_"
        else:
            value = f"`{value}`"
        suffix = _OPTIONAL_SUFFIX if optional else ""
        return f"- {label}: {value}{suffix}"

    pairs_block = "\n".join(
        f"  - `{raw or '_(no raw template name)_'}` → `{instantiated}`"
        for raw, instantiated in diagnostic.get("template_pairs", [])
    )

    lines = [
        "**VC interface normalization — no match**",
        "",
        _line("Manufacturer", diagnostic.get("manufacturer_slug"), optional=True),
        _line("Device type", diagnostic.get("device_type_model"), optional=True),
        _line("Module type", diagnostic.get("module_type_model"), optional=True),
        _line("Module bay", diagnostic.get("module_bay_name"), optional=True),
        f"- VC position (target): {diagnostic.get('vc_position')}",
        f"- VC member positions: {list(diagnostic.get('vc_member_positions') or [])}",
        "- Template names (raw → instantiated):",
        pairs_block or "  - _(no templates)_",
        f"- Regex tried: `{diagnostic.get('regex', '')}`",
        f"- Plugin: {plugin_version} / NetBox: {netbox_version} / Python: {python_version}",
    ]
    return "\n".join(lines)


SYNC_TAB_PAGE_SIZE_PREFERENCE = "plugins.netbox_librenms_plugin.per_page"


def get_table_paginate_count(request: HttpRequest, table_prefix: str) -> int:
    """
    Return the page size of one sync tab table, and remember a requested size for that tab.

    The order is: a valid ``<prefix>per_page`` request value, the size saved for the tab, then
    NetBox's own page size. A request value is saved when it differs from the size the tab
    shows without it. Every size is clamped to MAX_PAGE_SIZE.

    Args:
        request: HTTP request object
        table_prefix: Prefix for the table

    Returns:
        int: Number of items to display per page

    """
    # An HTMX refresh sends the new page size in POST while the URL can retain an old value.
    param_key = f"{table_prefix}per_page"
    param_value = request.POST.get(param_key) if param_key in request.POST else request.GET.get(param_key)
    preference = f"{SYNC_TAB_PAGE_SIZE_PREFERENCE}.{table_prefix.rstrip('_')}"
    max_page_size = get_config().MAX_PAGE_SIZE

    def clamp(per_page):
        # MAX_PAGE_SIZE 0/None disables the NetBox ceiling; don't clamp to it (min() with 0
        # would zero the page size, and with None it TypeErrors).
        return min(per_page, max_page_size) if max_page_size else per_page

    saved = coerce_positive_int(get_user_pref(request, preference))
    current = clamp(saved) if saved is not None else netbox_get_paginate_count(request)
    requested = coerce_positive_int(param_value)
    if requested is None:
        return current
    per_page = clamp(requested)
    # Page links and the sync form carry the rendered size, so only a different size is a choice.
    if per_page != current:
        save_user_pref(request, preference, per_page)
    return per_page


def paginate_sync_table(table, request, page=None):
    """
    Order and paginate a sync tab table, with the page size from get_table_paginate_count().

    RequestConfig would take a raw ``<prefix>per_page`` from GET past the size checks, so it only orders the table.
    """
    tables.RequestConfig(request, paginate=False).configure(table)
    if page is None:
        page = request.GET.get(table.prefixed_page_field, 1)
    try:
        table.paginate(
            paginator_class=EnhancedPaginator, per_page=get_table_paginate_count(request, table.prefix), page=page
        )
    except PageNotAnInteger:
        table.page = table.paginator.page(1)
    except EmptyPage:
        table.page = table.paginator.page(table.paginator.num_pages)


def get_user_pref(request, path, default=None):
    """Get a user preference value via request.user.config."""
    if hasattr(request, "user") and hasattr(request.user, "config"):
        return request.user.config.get(path, default)
    return default


# By preference path: a save counter, and the newest sequence number and save count of each recent page.
PREFERENCE_ORDER = "plugins.netbox_librenms_plugin.preference_order"
# Pages that saved a preference recently; an older page's late request is then accepted again.
PREFERENCE_ORDER_PAGES = 20


def save_user_pref(request, path, value, order=None):
    """
    Save a user preference value in the user's NetBox config.

    The write reads the user's config again under a row lock, so a request that saves another
    preference at the same time keeps its value. The fresh row also keeps the write away from
    NetBox's shared DEFAULT_USER_PREFERENCES dict, which a new UserConfig holds by reference.

    Args:
        request (HttpRequest): Request for the user whose preference is saved.
        path (str): Preference path to set.
        value (object): Preference value to save.
        order (tuple[str, int] | None): The page that made the choice and its count of choices.
            A choice older than the stored choice of the same page is ignored, so a request that
            arrives late cannot undo a newer choice. Pages count on their own, so no clock is compared.

    """
    from users.models import UserConfig

    user = getattr(request, "user", None)
    config = getattr(user, "config", None) if user is not None else None
    if config is None or config.pk is None:
        return
    with transaction.atomic():
        user_config = UserConfig.objects.select_for_update().get(pk=config.pk)
        orders = user_config.get(PREFERENCE_ORDER)
        orders = dict(orders) if isinstance(orders, dict) else {}
        if order is not None:
            client_id, sequence = order
            record = orders.get(path)
            record = record if isinstance(record, dict) else {}
            pages = {
                page: entry
                for page, entry in (record.get("pages") or {}).items()
                if isinstance(entry, list) and len(entry) == 2 and all(type(number) is int for number in entry)
            }
            stored = pages.get(client_id)
            if stored is not None and stored[0] > sequence:
                return
            # jsonb reorders object keys, so a save counter, not key order, tells which page saved last.
            serial = record.get("serial") if type(record.get("serial")) is int else 0
            serial += 1
            pages[client_id] = [sequence, serial]
            while len(pages) > PREFERENCE_ORDER_PAGES:
                del pages[min(pages, key=lambda page: pages[page][1])]
            orders[path] = {"serial": serial, "pages": pages}
        try:
            user_config.set(path, value)
            if order is not None:
                user_config.set(PREFERENCE_ORDER, orders)
        except (TypeError, ValueError):
            return
        user_config.save()
    user.config = user_config


IMPORT_CONTEXT_COLUMNS = ("location", "hardware", "hostname", "sysname")
IMPORT_CONTEXT_COLUMNS_DEFAULT = ("location", "hardware")
IMPORT_CONTEXT_COLUMNS_PREFERENCE = "plugins.netbox_librenms_plugin.import_columns"


def validate_import_context_columns(value):
    """
    Validate and order an import context-column preference.

    Args:
        value (object): The stored or submitted preference value.

    Returns:
        tuple[str, ...] | None: Ordered columns, or None when the value is invalid.

    """
    if not isinstance(value, list) or any(not isinstance(column, str) for column in value):
        return None
    if len(value) != len(set(value)):
        return None
    if any(column not in IMPORT_CONTEXT_COLUMNS for column in value):
        return None
    return tuple(column for column in IMPORT_CONTEXT_COLUMNS if column in value)


def resolve_import_context_columns(request):
    """
    Resolve the user's visible import context columns.

    Args:
        request (HttpRequest): Request for the user who owns the preference.

    Returns:
        tuple[str, ...]: Valid visible columns or the focused-table defaults.

    """
    stored = get_user_pref(request, IMPORT_CONTEXT_COLUMNS_PREFERENCE, list(IMPORT_CONTEXT_COLUMNS_DEFAULT))
    validated = validate_import_context_columns(stored)
    return IMPORT_CONTEXT_COLUMNS_DEFAULT if validated is None else validated


# The interface sync POST reads these values from its ``exclude_columns`` field; the dict order is the menu order.
INTERFACE_SYNC_EXCLUDE_COLUMNS = {
    "name": "Name",
    "type": "Type",
    "speed": "Speed",
    "vlans": "VLANs",
    "mac_address": "MAC",
    "mtu": "MTU",
    "enabled": "Enabled",
    "description": "Description",
}
INTERFACE_SYNC_OPTIONS_DEFAULT = {"auto_select_lag_members": True, "exclude_columns": ()}
INTERFACE_SYNC_OPTIONS_PREFERENCE = "plugins.netbox_librenms_plugin.interface_sync_options"


def validate_interface_sync_options(value):
    """
    Validate and normalize an interface "Sync options" menu preference.

    Args:
        value (object): The stored or submitted preference value.

    Returns:
        dict | None: The value with its columns in menu order, or None when the value is invalid.

    """
    if not isinstance(value, dict) or value.keys() != INTERFACE_SYNC_OPTIONS_DEFAULT.keys():
        return None
    auto_select = value["auto_select_lag_members"]
    columns = value["exclude_columns"]
    if not isinstance(auto_select, bool) or not isinstance(columns, list):
        return None
    if any(not isinstance(column, str) or column not in INTERFACE_SYNC_EXCLUDE_COLUMNS for column in columns):
        return None
    if len(columns) != len(set(columns)):
        return None
    return {
        "auto_select_lag_members": auto_select,
        "exclude_columns": [column for column in INTERFACE_SYNC_EXCLUDE_COLUMNS if column in columns],
    }


def interface_sync_options_menu(request):
    """
    Build the interface "Sync options" menu state from the user's choice and the factory defaults.

    A stored value that does not validate counts as absent, so the menu shows the factory defaults.

    Args:
        request (HttpRequest): Request for the user who owns the preference.

    Returns:
        dict: The switch state and one entry per exclude column, each with its factory default.

    """
    options = validate_interface_sync_options(get_user_pref(request, INTERFACE_SYNC_OPTIONS_PREFERENCE))
    if options is None:
        options = INTERFACE_SYNC_OPTIONS_DEFAULT
    return {
        "auto_select_lag_members": options["auto_select_lag_members"],
        "auto_select_lag_members_default": INTERFACE_SYNC_OPTIONS_DEFAULT["auto_select_lag_members"],
        "exclude_columns": [
            {
                "value": column,
                "label": label,
                "checked": column in options["exclude_columns"],
                "default_checked": column in INTERFACE_SYNC_OPTIONS_DEFAULT["exclude_columns"],
            }
            for column, label in INTERFACE_SYNC_EXCLUDE_COLUMNS.items()
        ],
    }


_TRUTHY_PARAMETER_VALUES = frozenset({"on", "true", "1"})


def is_truthy_parameter(value) -> bool:
    """Return True when a request parameter carries one of the accepted truthy spellings."""
    return value.lower() in _TRUTHY_PARAMETER_VALUES if isinstance(value, str) else False


def read_request_toggle(request, keys):
    """
    Return the first key's POST value, else its GET value, else None.

    One reader for every POST-then-GET toggle in this module, so the accepted vocabulary
    cannot drift between them.

    Args:
        request (HttpRequest): Request that contains the toggle values.
        keys (Iterable[str]): Toggle keys in precedence order.

    Returns:
        str | None: First matching toggle value, or ``None`` when no key is present.

    """
    post_value = next((request.POST.get(key) for key in keys if key in request.POST), None)
    if post_value is not None:
        return post_value
    return next((request.GET.get(key) for key in keys if key in request.GET), None)


def resolve_naming_preferences(request) -> tuple[bool, bool]:
    """
    Resolve use_sysname/strip_domain: POST/GET toggle → user pref → plugin settings.

    This is the single source of truth for naming preference resolution,
    used by the import page, sync page, and sync action views.

    Args:
        request (HttpRequest): Request used to resolve toggle and user preference values.

    Returns:
        tuple[bool, bool]: The ``use_sysname`` and ``strip_domain`` values.

    """
    from netbox_librenms_plugin.models import LibreNMSSettings

    settings = None
    _USE_SYSNAME_KEYS = ("use-sysname-toggle", "use_sysname-toggle", "use_sysname")
    _STRIP_DOMAIN_KEYS = ("strip-domain-toggle", "strip_domain-toggle", "strip_domain")

    # POST first (import form submissions), then GET (HTMX hx-include)
    _use_sysname_value = read_request_toggle(request, _USE_SYSNAME_KEYS)
    if _use_sysname_value is not None:
        use_sysname = is_truthy_parameter(_use_sysname_value)
    else:
        pref = get_user_pref(request, "plugins.netbox_librenms_plugin.use_sysname")
        if pref is not None:
            use_sysname = pref
        else:
            settings = LibreNMSSettings.objects.first()
            use_sysname = getattr(settings, "use_sysname_default", True) if settings else True

    _strip_domain_value = read_request_toggle(request, _STRIP_DOMAIN_KEYS)
    if _strip_domain_value is not None:
        strip_domain = is_truthy_parameter(_strip_domain_value)
    else:
        pref = get_user_pref(request, "plugins.netbox_librenms_plugin.strip_domain")
        if pref is not None:
            strip_domain = pref
        else:
            if settings is None:
                settings = LibreNMSSettings.objects.first()
            strip_domain = getattr(settings, "strip_domain_default", False) if settings else False

    return use_sysname, strip_domain


def same_host(a, b) -> bool:
    """Return whether two address strings refer to the same host IP."""
    try:
        return parse_host_address(a) == parse_host_address(b)
    except ValueError:
        return False


SET_PRIMARY_IP_PREFERENCE = "plugins.netbox_librenms_plugin.set_primary_ip"
CREATE_MISSING_INTERFACES_PREFERENCE = "plugins.netbox_librenms_plugin.create_missing_interfaces"


def _resolve_sync_toggle(request, keys, preference):
    """Return the request toggle, else the user's saved boolean, else ``False``; a non-boolean saved value counts as absent."""
    value = read_request_toggle(request, keys)
    if value is not None:
        return is_truthy_parameter(value)
    pref = get_user_pref(request, preference)
    return pref if isinstance(pref, bool) else False


def resolve_create_missing_interfaces(request) -> bool:
    """
    Resolve the "create a missing NetBox interface before assigning the IP" flag.

    POST wins, then GET, then the user's saved preference
    ``plugins.netbox_librenms_plugin.create_missing_interfaces``, then ``False`` (opt-in).
    The IP-sync template renders the toggle from this value, so a table refresh restores
    what the user selected instead of silently reverting to off.

    Args:
        request (HttpRequest): Request used to resolve the toggle and user preference values.

    Returns:
        bool: Whether missing NetBox interfaces can be created before IP assignment.

    """
    return _resolve_sync_toggle(
        request,
        ("create-missing-interfaces-toggle", "create_missing_interfaces"),
        CREATE_MISSING_INTERFACES_PREFERENCE,
    )


def resolve_set_primary_ip(request) -> bool:
    """
    Resolve the "set Primary IP from the LibreNMS management IP" flag.

    Cascade (mirrors :func:`resolve_naming_preferences`, minus a settings
    default since this is a per-sync action choice):

    1. POST/GET ``set-primary-ip-toggle`` (or ``set_primary_ip``) wins
       -- set by the IP-sync tab toggle.
    2. Otherwise the user's saved boolean preference
       ``plugins.netbox_librenms_plugin.set_primary_ip``.
    3. Otherwise ``False`` (opt-in).

    When enabled, :class:`SyncIPAddressesView` sets ``primary_ip4``/``primary_ip6``
    on the device/VM for the synced IP that matches the LibreNMS management IP,
    provided that IP ends up assigned to one of the object's interfaces.

    Args:
        request (HttpRequest): Request used to resolve the toggle and user preference values.

    Returns:
        bool: Whether the matching synced management IP can become the primary IP.

    """
    return _resolve_sync_toggle(
        request, ("set-primary-ip-toggle", "set_primary_ip-toggle", "set_primary_ip"), SET_PRIMARY_IP_PREFERENCE
    )


INTERFACE_NAME_PREFERENCE_PATH = "plugins.netbox_librenms_plugin.interface_name_field"
INTERFACE_NAME_PLATFORM_PREFERENCES_PATH = "plugins.netbox_librenms_plugin.interface_name_fields_by_platform"
_POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807


def _remember_interface_name_per_platform(request: HttpRequest) -> bool:
    """Return the installation setting, cached for the lifetime of this request."""
    cache_attribute = "_librenms_remember_interface_name_per_platform"
    request_state = request.__dict__
    if cache_attribute in request_state:
        return request_state[cache_attribute]

    from netbox_librenms_plugin.models import LibreNMSSettings

    configured = (
        LibreNMSSettings.objects.filter(pk=1).values_list("remember_interface_name_per_platform", flat=True).first()
    )
    enabled = bool(configured)
    setattr(request, cache_attribute, enabled)
    return enabled


def interface_field_limit(field_name, model=None):
    """
    Return the column length NetBox declares for one interface field.

    Read from the model's own field so the bound cannot drift, the same way
    :func:`coerce_interface_mtu` reads ``INTERFACE_MTU_MIN``/``MAX``. Pass the concrete
    writer model rather than trusting Interface and VMInterface to stay equal.

    Args:
        field_name (str): Interface model field to inspect.
        model (type | None): Concrete interface model. Defaults to ``Interface``.

    Returns:
        int | None: Maximum field length, or ``None`` when the field has no length limit.

    """
    return (model or Interface)._meta.get_field(field_name).max_length


def syncable_interface_name(port, interface_name_field, model=None):
    """
    Return the LibreNMS name usable as a NetBox interface name, or ``None``.

    ``save()`` runs no validators and Django never truncates a CharField, so an over-long name
    reaches Postgres as a ``DataError``. Truncating instead would collide on the
    ``(device, name)`` unique constraint, so an unstorable name makes the row unsyncable.

    Args:
        port (dict): LibreNMS port row.
        interface_name_field (str): Port field that contains the selected interface name.
        model (type | None): Concrete interface model. Defaults to ``Interface``.

    Returns:
        str | None: Valid interface name, or ``None`` when NetBox cannot store it.

    """
    name = port.get(interface_name_field)
    if not isinstance(name, str) or not name.strip():
        return None
    if len(name) > interface_field_limit("name", model):
        return None
    return name


def interface_name_rejection_reason(port, interface_name_field, model=None):
    """
    Return why the LibreNMS name cannot be synced, or ``None`` when it can.

    The two cases read very differently to a user, so the caller reports them apart rather
    than telling someone their 80-character interface name is blank.

    Args:
        port (dict): LibreNMS port row.
        interface_name_field (str): Port field that contains the selected interface name.
        model (type | None): Concrete interface model. Defaults to ``Interface``.

    Returns:
        str | None: Rejection reason, or ``None`` when the name can be synced.

    """
    name = port.get(interface_name_field)
    if not isinstance(name, str) or not name.strip():
        return "interface name is blank"
    limit = interface_field_limit("name", model)
    if len(name) > limit:
        return f"interface name is longer than the {limit} characters NetBox stores"
    return None


def synced_interface_name(
    port,
    interface_name_field,
    host_owned_names,
    reserved_name_port_ids=None,
    model=None,
):
    """
    Return the name one LibreNMS port will use in NetBox, or ``None``.

    Args:
        port (dict): LibreNMS port row.
        interface_name_field (str): Port field that contains the selected interface name.
        host_owned_names (set[str]): Names owned by host rows in the same snapshot.
        reserved_name_port_ids (dict[str, set[int]] | None): Active-server port IDs bound to
            each existing NetBox interface name on the target device.
        model (type | None): Concrete interface model. Defaults to ``Interface``.

    Returns:
        str | None: The name to sync, or ``None`` when NetBox cannot store it.

    """
    name = syncable_interface_name(port, interface_name_field, model)
    port_id = normalize_librenms_port_id(port.get("port_id"))
    reserved_name_port_ids = reserved_name_port_ids or {}
    name_is_reserved = bool(reserved_name_port_ids.get(name)) and port_id not in reserved_name_port_ids[name]
    if (
        name is None
        or port.get("_source") != OOB_INVENTORY_SOURCE
        or (name not in host_owned_names and not name_is_reserved)
    ):
        if name_is_reserved:
            return None
        return name
    derived_name = f"{name}{OOB_NAME_SUFFIX}"
    if len(derived_name) > interface_field_limit("name", model):
        return None
    if reserved_name_port_ids.get(derived_name) and port_id not in reserved_name_port_ids[derived_name]:
        return None
    return derived_name


def _cross_source_interface_port_ids(ports):
    """Return normalized port IDs claimed by both host and OOB rows."""
    port_sources = {}
    for port in ports:
        port_id = normalize_librenms_port_id(port.get("port_id"))
        if port_id is not None:
            port_sources.setdefault(port_id, set()).add(port.get("_source") == OOB_INVENTORY_SOURCE)
    return {port_id for port_id, sources in port_sources.items() if len(sources) > 1}


def _host_interface_names_by_device(ports, interface_name_field, target_device_ids, model):
    """Return valid host-row names grouped by target device ID."""
    host_names = {}
    for port in ports:
        if port.get("_source") == OOB_INVENTORY_SOURCE:
            continue
        port_id = normalize_librenms_port_id(port.get("port_id"))
        name = syncable_interface_name(port, interface_name_field, model)
        if port_id is not None and name is not None:
            host_names.setdefault(target_device_ids.get(port_id), set()).add(name)
    return host_names


def _synced_interface_name_rejection_reason(port, interface_name_field, model):
    """Return why the resolved sync name is unavailable."""
    reason = interface_name_rejection_reason(port, interface_name_field, model)
    if reason is not None:
        return reason
    if port.get("_source") == OOB_INVENTORY_SOURCE:
        raw_name = syncable_interface_name(port, interface_name_field, model)
        derived_name = f"{raw_name}{OOB_NAME_SUFFIX}"
        limit = interface_field_limit("name", model)
        if len(derived_name) > limit:
            return f"derived interface name is longer than the {limit} characters NetBox stores"
    return HOST_NAME_COLLISION_REASON


def _oob_name_collision_rejections(
    ports,
    names,
    host_names_by_device,
    target_device_ids,
    interface_name_field,
    model,
):
    """Return OOB port IDs whose candidate names conflict with other snapshot rows."""
    rejected = {}
    oob_claims = {}
    for port in ports:
        if port.get("_source") != OOB_INVENTORY_SOURCE:
            continue
        port_id = normalize_librenms_port_id(port.get("port_id"))
        device_id = target_device_ids.get(port_id)
        synced_name = names.get(port_id)
        raw_name = syncable_interface_name(port, interface_name_field, model)
        if synced_name is not None and synced_name != raw_name:
            if synced_name in host_names_by_device.get(device_id, set()):
                rejected[port_id] = HOST_NAME_COLLISION_REASON
        if synced_name is not None and not port.get("_dedup_conflict"):
            oob_claims.setdefault((device_id, synced_name), []).append(port_id)
    for port_ids in oob_claims.values():
        if len(port_ids) > 1:
            rejected.update(dict.fromkeys(port_ids, HOST_NAME_COLLISION_REASON))
    return rejected


def synced_interface_names(
    ports,
    interface_name_field,
    model=None,
    *,
    target_device_ids=None,
    reserved_name_port_ids_by_device=None,
):
    """
    Resolve snapshot interface names by normalized LibreNMS port ID.

    Args:
        ports (list): The merged host and OOB port rows.
        interface_name_field (str): Port field that contains the selected interface name.
        model (type | None): Concrete interface model. Defaults to ``Interface``.
        target_device_ids (dict[int, int] | None): Target device ID by normalized port ID.
        reserved_name_port_ids_by_device (dict[int, dict[str, set[int]]] | None): Active-server
            port IDs bound to each existing name, grouped by target device ID.

    Returns:
        tuple[dict[int, str], dict[int, str]]: Candidate names and rejection reasons by port ID.

    """
    if not is_list_of_dicts(ports):
        return {}, {}
    target_device_ids = target_device_ids or {}
    reserved_name_port_ids_by_device = reserved_name_port_ids_by_device or {}
    cross_source_port_ids = _cross_source_interface_port_ids(ports)
    host_owned_names_by_device = _host_interface_names_by_device(
        ports,
        interface_name_field,
        target_device_ids,
        model,
    )
    names = {}
    rejected = {}
    for port in ports:
        port_id = normalize_librenms_port_id(port.get("port_id"))
        if port_id is None:
            continue
        if port_id in cross_source_port_ids:
            names.pop(port_id, None)
            rejected[port_id] = PORT_ID_SOURCE_COLLISION_REASON
            continue
        device_id = target_device_ids.get(port_id)
        host_owned_names = host_owned_names_by_device.get(device_id, set())
        reserved_name_port_ids = reserved_name_port_ids_by_device.get(device_id, {})
        name = synced_interface_name(
            port,
            interface_name_field,
            host_owned_names,
            reserved_name_port_ids,
            model,
        )
        if name is not None:
            names[port_id] = name
            continue
        reported_name = syncable_interface_name(port, interface_name_field, model)
        reserved_port_ids = reserved_name_port_ids.get(reported_name, set())
        if port.get("_source") != OOB_INVENTORY_SOURCE and reserved_port_ids and port_id not in reserved_port_ids:
            rejected[port_id] = REPORTED_NAME_PORT_COLLISION_REASON
        else:
            rejected[port_id] = _synced_interface_name_rejection_reason(port, interface_name_field, model)
    rejected.update(
        _oob_name_collision_rejections(
            ports,
            names,
            host_owned_names_by_device,
            target_device_ids,
            interface_name_field,
            model,
        )
    )
    return names, rejected


@dataclass(frozen=True)
class ReportedNameOwner:
    """The LibreNMS port bound to the NetBox interface that holds a host row's reported name."""

    name: str
    port_id: int
    status: str
    owner_row_name: str | None = None
    owner_row_is_oob: bool = False
    owner_synced_name: str | None = None

    def explanation(self):
        """Return the operator-facing text for this owner."""
        if self.status == NAME_OWNER_LIVE:
            source = "OOB" if self.owner_row_is_oob else "host"
            held = (
                f"NetBox interface '{self.name}' is bound to LibreNMS port {self.port_id}, "
                f"the {source} row '{self.owner_row_name}'."
            )
            if self.owner_synced_name is None:
                return f"{held} That row cannot sync either, so it cannot release the name."
            return f"{held} Sync that row first; it syncs as '{self.owner_synced_name}'. Then sync this row."
        if self.status == NAME_OWNER_STALE:
            return (
                f"NetBox interface '{self.name}' is bound to LibreNMS port {self.port_id}, which this device "
                "no longer reports. Rebind keeps the interface, its IP addresses and cables, and moves only "
                "the LibreNMS binding to this row's port."
            )
        return (
            f"NetBox interface '{self.name}' is bound to LibreNMS port {self.port_id}. The OOB inventory "
            "is incomplete, so that port can still exist. Refresh the data."
        )


def reported_name_owners(
    ports,
    interface_name_field,
    synced_names,
    rejected_names,
    *,
    target_device_ids,
    reserved_name_port_ids_by_device,
    snapshot_complete,
    model=None,
):
    """
    Classify who holds the reported name of each host row rejected for a name bound to another port.

    Args:
        ports (list): The merged host and OOB port rows.
        interface_name_field (str): Port field that contains the selected interface name.
        synced_names (dict[int, str]): Candidate names by port ID, from ``synced_interface_names``.
        rejected_names (dict[int, str]): Rejection reasons by port ID, from ``synced_interface_names``.
        target_device_ids (dict[int, int]): Target device ID by normalized port ID.
        reserved_name_port_ids_by_device (dict[int, dict[str, set[int]]]): Active-server port IDs
            bound to each existing name, grouped by target device ID.
        snapshot_complete (bool): False when the snapshot is tagged ``oob_incomplete``.
        model (type | None): Concrete interface model. Defaults to ``Interface``.

    Returns:
        dict[int, ReportedNameOwner]: The owner by the rejected row's normalized port ID.

    """
    if not is_list_of_dicts(ports):
        return {}
    rows_by_port_id = {}
    for port in ports:
        port_id = normalize_librenms_port_id(port.get("port_id"))
        if port_id is not None:
            rows_by_port_id.setdefault(port_id, port)
    owners = {}
    for port_id, port in rows_by_port_id.items():
        if rejected_names.get(port_id) != REPORTED_NAME_PORT_COLLISION_REASON:
            continue
        name = syncable_interface_name(port, interface_name_field, model)
        reserved = reserved_name_port_ids_by_device.get(target_device_ids.get(port_id), {}).get(name, set())
        holders = set(reserved) - {port_id}
        if len(holders) != 1:
            continue
        holder = holders.pop()
        owner_row = rows_by_port_id.get(holder)
        if owner_row is None:
            status = NAME_OWNER_STALE if snapshot_complete else NAME_OWNER_UNKNOWN
            owners[port_id] = ReportedNameOwner(name=name, port_id=holder, status=status)
            continue
        owner_can_sync = holder not in rejected_names and not owner_row.get("_dedup_conflict")
        owners[port_id] = ReportedNameOwner(
            name=name,
            port_id=holder,
            status=NAME_OWNER_LIVE,
            owner_row_name=owner_row.get(interface_name_field),
            owner_row_is_oob=owner_row.get("_source") == OOB_INVENTORY_SOURCE,
            owner_synced_name=synced_names.get(holder) if owner_can_sync else None,
        )
    return owners


def bounded_interface_text(field_name, value, model=None):
    """
    Return *value* clipped to the column NetBox declares for *field_name*.

    Refuses ``name``: an over-long name has to make the row unsyncable, because a truncated
    one collides with its siblings on the ``(device, name)`` unique constraint.

    Args:
        field_name (str): Interface field that will store the value.
        value (str): Text to clip to the field limit.
        model (type | None): Concrete interface model. Defaults to ``Interface``.

    Returns:
        str: Original or clipped field value.

    Raises:
        ValueError: If ``field_name`` is ``name``.

    """
    if field_name == "name":
        raise ValueError("An interface name must not be truncated; use syncable_interface_name().")
    limit = interface_field_limit(field_name, model)
    return value[:limit] if limit is not None and len(value) > limit else value


def coerce_interface_mtu(value) -> int | None:
    """
    Return an MTU inside NetBox's accepted range, or None.

    ``save()`` does not run field validators, so an out-of-range or non-numeric ``ifMtu`` from
    LibreNMS would otherwise reach the column unchecked. NetBox pins the bounds in
    ``dcim.constants``; they are read from there so the two cannot drift.

    Args:
        value (object): Candidate LibreNMS MTU value.

    Returns:
        int | None: Valid NetBox MTU, or ``None`` when the value is invalid or outside the accepted range.

    """
    from dcim.constants import INTERFACE_MTU_MAX, INTERFACE_MTU_MIN

    mtu = coerce_positive_int(value)
    return mtu if mtu is not None and INTERFACE_MTU_MIN <= mtu <= INTERFACE_MTU_MAX else None


def coerce_model_pk(value) -> int | None:
    """Normalize a NetBox primary key without allowing PostgreSQL bigint overflow."""
    model_pk = coerce_positive_int(value)
    if model_pk is None or model_pk > _POSTGRES_BIGINT_MAX:
        return None
    return model_pk


def save_interface_name_preference(
    request: HttpRequest,
    value: str,
    platform_id: int | None = None,
) -> bool:
    """Persist an interface-name choice at the configured user preference scope."""
    if not is_supported_interface_name_field(value):
        return False

    normalized_platform_id = coerce_model_pk(platform_id)
    if normalized_platform_id is not None and _remember_interface_name_per_platform(request):
        stored = get_user_pref(request, INTERFACE_NAME_PLATFORM_PREFERENCES_PATH, {})
        stored = stored if isinstance(stored, dict) else {}
        preferences = {}
        for key, candidate in stored.items():
            stored_platform_id = coerce_model_pk(key)
            if stored_platform_id is not None and is_supported_interface_name_field(candidate):
                preferences[str(stored_platform_id)] = candidate
        preferences[str(normalized_platform_id)] = value
        save_user_pref(request, INTERFACE_NAME_PLATFORM_PREFERENCES_PATH, preferences)
        return True

    save_user_pref(request, INTERFACE_NAME_PREFERENCE_PATH, value)
    return True


def get_interface_name_field(request: Optional[HttpRequest] = None, obj=None) -> str:
    """
    Get interface name field with request override support.

    Checks in order: GET/POST params, platform-specific user preference, global
    user preference, plugin config default. Platform-specific storage is used
    only when the installation setting is enabled and the object has a platform.

    Args:
        request: Optional HTTP request object that may contain an override.
        obj: Optional NetBox object whose platform scopes the preference.

    Returns:
        str: Interface name field to use

    """
    platform_id = coerce_model_pk(getattr(obj, "platform_id", None))
    if request:
        # Explicit override from request params
        param_val = request.GET.get("interface_name_field") or request.POST.get("interface_name_field")
        if is_supported_interface_name_field(param_val):
            # Read only. The selector posts to the save_user_pref endpoint, so persisting here
            # made every GET render mutate stored user state and add a write per render.
            return param_val

        if platform_id is not None and _remember_interface_name_per_platform(request):
            platform_preferences = get_user_pref(request, INTERFACE_NAME_PLATFORM_PREFERENCES_PATH, {})
            if isinstance(platform_preferences, dict):
                platform_value = platform_preferences.get(str(platform_id))
                if is_supported_interface_name_field(platform_value):
                    return platform_value

        # Check user preference
        pref_val = get_user_pref(request, INTERFACE_NAME_PREFERENCE_PATH)
        if is_supported_interface_name_field(pref_val):
            return pref_val

    # Fall back to plugin config. An unsupported configured value would be written into every
    # tab snapshot and then rejected by the readers, so refuse it here instead.
    configured = get_plugin_config("netbox_librenms_plugin", "interface_name_field")
    if is_supported_interface_name_field(configured):
        return configured
    logger.warning(
        "Unsupported interface_name_field %r in PLUGINS_CONFIG; using %r.",
        configured,
        DEFAULT_INTERFACE_NAME_FIELD,
    )
    return DEFAULT_INTERFACE_NAME_FIELD


def match_librenms_hardware_to_device_type(hardware_name: str, *, preloaded_rules: dict | None = None) -> dict | None:
    """
    Match LibreNMS hardware string to a NetBox DeviceType.

    Checks DeviceTypeMapping table first, then falls back to exact matching
    on part_number and model fields (case-insensitive).

    Args:
        hardware_name (str): Hardware string from LibreNMS API (e.g., 'C9200L-48P-4X')
        preloaded_rules: Optional dict from :func:`preload_normalization_rules` for the
            ``device_type`` scope. When matching many devices in a loop (bulk import), pass it
            so the device_type NormalizationRule set is fetched once instead of per device.

    Returns:
        dict | None: Dictionary containing:
            - matched (bool): Whether a match was found
            - device_type (DeviceType|None): The matched DeviceType object
            - match_type (str|None): 'mapping' if via DeviceTypeMapping, 'exact' if via
              part_number/model, None otherwise
        Returns ``None`` when ``MultipleObjectsReturned`` is raised on any lookup
        (DeviceTypeMapping, part_number, or model) — i.e., the function fails closed
        on all ambiguity cases.  Callers must guard with ``if result is None:``
        before inspecting the dict.

    """
    from dcim.models import DeviceType

    try:
        from netbox_librenms_plugin.models import DeviceTypeMapping

        _has_device_type_mapping = True
    except ImportError:
        _has_device_type_mapping = False

    if not hardware_name or hardware_name == "-":
        return {"matched": False, "device_type": None, "match_type": None}

    # Normalize the raw LibreNMS hardware string per the documented ``device_type``
    # NormalizationRule scope before the DeviceTypeMapping lookup (docs/configuration/
    # mappings.md: "normalizes LibreNMS hardware string before DeviceTypeMapping
    # lookup"). With no device_type rules configured this returns the input unchanged.
    # .strip() to match how DeviceTypeMapping stores the key: its save() does .strip().lower(), so
    # an unstripped search_name (e.g. rule output or raw hardware carrying surrounding whitespace)
    # would never satisfy librenms_hardware__iexact (case-, but not whitespace-, insensitive).
    raw_name = hardware_name.strip()
    search_name = apply_normalization_rules(
        value=hardware_name, scope="device_type", preloaded_rules=preloaded_rules
    ).strip()

    # Check DeviceTypeMapping table first (when available). The normalized key is the
    # canonical form; the raw key is a fallback for rows the standard mapping CRUD/CSV
    # write paths stored un-normalized (their model clean() only does .strip().lower()).
    # Blank candidates are skipped: librenms_hardware can never be blank, and a rule
    # chain that empties the string must not query iexact="".
    if _has_device_type_mapping:
        for candidate in dict.fromkeys(name for name in (search_name, raw_name) if name):
            try:
                mapping = DeviceTypeMapping.objects.get(librenms_hardware__iexact=candidate)
                return {
                    "matched": True,
                    "device_type": mapping.netbox_device_type,
                    "match_type": "mapping",
                }
            except DeviceTypeMapping.DoesNotExist:
                continue
            except DeviceTypeMapping.MultipleObjectsReturned:
                logger.warning(
                    "Multiple DeviceTypeMapping entries match hardware %r — skipping mapping lookup; "
                    "resolve the ambiguity by removing duplicate mappings.",
                    candidate,
                )
                return None

    # The part_number/model exact matches use the RAW string: the documented rule scope is
    # the DeviceTypeMapping lookup only, and DeviceTypes whose part_number/model literally
    # equal the LibreNMS hardware string must keep matching after a rule is added. A blank
    # raw string (whitespace-only hardware) must not run the lookups at all — part_number
    # defaults to "" so part_number__iexact="" would match an arbitrary unrelated DeviceType.
    if not raw_name:
        return {"matched": False, "device_type": None, "match_type": None}

    # Try part number exact match
    try:
        device_type = DeviceType.objects.get(part_number__iexact=raw_name)
        return {
            "matched": True,
            "device_type": device_type,
            "match_type": "exact",
        }
    except DeviceType.DoesNotExist:
        pass
    except DeviceType.MultipleObjectsReturned:
        logger.warning(
            "Multiple DeviceType entries match part_number %r — cannot auto-select; "
            "resolve the ambiguity by ensuring part numbers are unique across manufacturers.",
            raw_name,
        )
        return None

    # Try exact model match (case-insensitive)
    try:
        device_type = DeviceType.objects.get(model__iexact=raw_name)
        return {"matched": True, "device_type": device_type, "match_type": "exact"}
    except DeviceType.DoesNotExist:
        pass
    except DeviceType.MultipleObjectsReturned:
        logger.warning(
            "Multiple DeviceType entries match model %r — cannot auto-select; "
            "resolve the ambiguity by ensuring model names are unique across manufacturers.",
            raw_name,
        )
        return None

    return {"matched": False, "device_type": None, "match_type": None}


def find_matching_location(site, librenms_location: str):
    """Find a site-scoped NetBox Location, including parent/ancestor locations."""
    if not site or not librenms_location or librenms_location == "-":
        return None

    from dcim.models import Location

    normalized = _normalise_location_value(librenms_location).strip()
    if not normalized:
        return None

    # Prefer the most specific direct child within the site.
    direct_match = Location.objects.filter(site=site, name__iexact=normalized).first()
    if direct_match:
        return direct_match

    # Fall back to any location in the site's hierarchy whose name or ancestor name matches.
    for location in Location.objects.filter(site=site).select_related("parent"):
        current = location
        while current is not None:
            if current.name and current.name.strip().casefold() == normalized.casefold():
                return location
            current = getattr(current, "parent", None)
    return None


def find_matching_site(librenms_location: str) -> dict:
    """
    Find exact matching NetBox site for a LibreNMS location.

    Tries an exact, case-insensitive name match first, then falls back to a
    ``LocationMapping`` (field_type='site') for when the LibreNMS value differs
    from the NetBox Site name.

    Args:
        librenms_location (str): Location string from LibreNMS

    Returns:
        dict: Dictionary containing:
            - found (bool): Whether a match was found
            - site (Site|None): The matched Site object
            - match_type (str|None): 'exact', 'mapping', or None
            - confidence (float): 1.0 if found, 0.0 otherwise

    """
    from dcim.models import Site

    librenms_location = _normalise_location_value(librenms_location)
    if not librenms_location or librenms_location == "-":
        return {"found": False, "site": None, "match_type": None, "confidence": 0.0}

    # Try case-insensitive exact match
    try:
        site = Site.objects.get(name__iexact=librenms_location)
        return {"found": True, "site": site, "match_type": "exact", "confidence": 1.0}
    except Site.DoesNotExist:
        pass
    except Site.MultipleObjectsReturned:
        site = Site.objects.filter(name__iexact=librenms_location).first()
        return {"found": True, "site": site, "match_type": "exact", "confidence": 1.0}

    # Fall back to LocationMapping for when the LibreNMS value differs from the Site name
    mapped_site = resolve_location_mapping("site", librenms_location)
    if mapped_site is not None:
        return {"found": True, "site": mapped_site, "match_type": "mapping", "confidence": 1.0}

    return {"found": False, "site": None, "match_type": None, "confidence": 0.0}


def resolve_location_mapping(field_type: str, librenms_value: str, parent_site=None):
    """
    Resolve a parsed LibreNMS location value to a NetBox object via LocationMapping.

    Args:
        field_type (str): One of 'region', 'site', 'location', 'rack', 'tenant'.
        librenms_value (str): The value parsed from the LibreNMS location string.
        parent_site (Site|None): For 'location'/'rack', restrict the match to
            objects belonging to this site (NetBox scopes these to a parent site).

    Returns:
        The matched NetBox object, or None if no mapping applies or if several
        mappings resolve to different objects (ambiguous — see below).

    """
    if not librenms_value:
        return None

    try:
        from netbox_librenms_plugin.models import LocationMapping
    except ImportError:
        return None

    mappings = LocationMapping.objects.filter(
        field_type=field_type,
        librenms_value__iexact=librenms_value,
    ).select_related("content_type")

    candidates = []
    for mapping in mappings:
        obj = mapping.netbox_object
        if obj is None:
            continue
        if parent_site is not None and field_type in ("location", "rack"):
            if get_object_site_id(obj) != parent_site.pk:
                continue
        candidates.append(obj)

    if not candidates:
        return None

    # LocationMapping.clean() rejects colliding aliases, but data predating that
    # check (or created concurrently) could still yield several candidates.
    # Picking one arbitrarily would assign a different object run to run, so
    # refuse to guess and leave the field unset.
    distinct = {(type(obj), obj.pk) for obj in candidates}
    if len(distinct) > 1:
        logger.warning(
            "Ambiguous LocationMapping for %s '%s': %d candidates; skipping",
            field_type,
            librenms_value,
            len(distinct),
        )
        return None

    return candidates[0]


def get_object_site_id(obj):
    """Return the site id an object belongs to (directly or via its location)."""
    site_id = getattr(obj, "site_id", None)
    if site_id is None:
        location = getattr(obj, "location", None)
        site_id = getattr(location, "site_id", None)
    return site_id


# Placeholder tokens supported by the LibreNMS location parse pattern.
LOCATION_PARSE_TOKENS = ("region", "site", "location", "rack", "tenant")
_LOCATION_TOKEN_RE = re.compile(r"\{(region|site|location|rack|tenant)\}")

# Hard cap on the location string length fed to regex matching, to bound
# worst-case backtracking on user-provided patterns (regex DoS mitigation).
LOCATION_PARSE_MAX_INPUT_LEN = 512


def _normalise_location_value(location):
    """
    Return the location name as a string.

    LibreNMS 26.5.0 returns the location as a relationship object
    (e.g. ``{"id": 1, "location": "Site A", "lat": ..., "lng": ...}``)
    rather than a flat name string. This coerces either shape to the
    plain name so all downstream matching/parsing works consistently,
    including for any device data cached before the API change was handled.
    """
    if isinstance(location, dict):
        return location.get("location") or ""
    return location or ""


def _placeholder_pattern_to_regex(pattern: str) -> str:
    """
    Convert a placeholder pattern (e.g. '{site} - {rack}') into an anchored regex.

    Literal text between placeholders is escaped and treated as a separator.
    Each placeholder becomes a non-greedy capture group. Repeated placeholders are
    renamed internally to keep the regex valid while still mapping back to the
    canonical token name (e.g. {location} and {location} both resolve to the
    'location' field in the result dict).
    """
    regex = ""
    last_end = 0
    placeholder_counts = {}
    for match in _LOCATION_TOKEN_RE.finditer(pattern):
        regex += re.escape(pattern[last_end : match.start()])
        token = match.group(1)
        count = placeholder_counts.get(token, 0)
        placeholder_counts[token] = count + 1
        group_name = token if count == 0 else f"{token}_{count}"
        regex += f"(?P<{group_name}>.+?)"
        last_end = match.end()
    regex += re.escape(pattern[last_end:])
    return f"^{regex}$"


def parse_librenms_location(location_string: str, pattern: str, is_regex: bool = False) -> dict:
    """
    Parse a LibreNMS location string into region/site/location/rack/tenant tokens.

    Args:
        location_string (str): The raw LibreNMS location value.
        pattern (str): Placeholder pattern (e.g. '{site} - {rack}') or, when
            ``is_regex`` is True, a raw regex with named groups.
        is_regex (bool): Treat ``pattern`` as a raw regular expression.

    Returns:
        dict: Mapping of each supported token to its parsed value (or None).

    """
    result = {name: None for name in LOCATION_PARSE_TOKENS}
    if not location_string or not pattern:
        return result

    if len(location_string) > LOCATION_PARSE_MAX_INPUT_LEN:
        location_string = location_string[:LOCATION_PARSE_MAX_INPUT_LEN]

    try:
        if is_regex:
            compiled = re.compile(pattern)
            match = compiled.search(location_string)
        else:
            compiled = re.compile(_placeholder_pattern_to_regex(pattern))
            match = compiled.match(location_string)
    except REGEX_COMPILE_ERRORS:
        logger.warning("Invalid LibreNMS location parse pattern: %r", pattern)
        return result

    if not match:
        return result

    for name, value in match.groupdict().items():
        token = name
        if "_" in name:
            token = name.rsplit("_", 1)[0]
        if token in result and value is not None:
            result[token] = value.strip() or None
    return result


def get_location_parse_settings():
    """
    Return the configured (pattern, is_regex) for parsing LibreNMS locations.

    Falls back to ("", False) — i.e. whole-string matching — if the settings
    row cannot be read, so import behaviour degrades gracefully rather than
    raising.
    """
    try:
        from netbox_librenms_plugin.models import LibreNMSSettings

        settings = LibreNMSSettings.objects.order_by("pk").first()
    # Optional config read: fall back to the default on any failure.
    except Exception:
        logger.debug("Could not read LibreNMS location parse settings; using defaults", exc_info=True)
        return "", False

    if settings is None:
        return "", False
    return settings.location_parse_pattern or "", bool(settings.location_parse_is_regex)


def parse_location_for_import(location_string: str) -> dict:
    """
    Parse a LibreNMS location string using the configured plugin settings.

    When no parse pattern is configured, the whole string is used for both the
    site and location tokens (preserving the plugin's original behaviour).

    Args:
        location_string (str): The raw LibreNMS location value.

    Returns:
        dict: Mapping of each supported token to its parsed value (or None).

    """
    location_string = _normalise_location_value(location_string)
    pattern, is_regex = get_location_parse_settings()
    whole = (location_string or "").strip() or None
    if not pattern:
        return {
            "region": None,
            "site": whole,
            "location": whole,
            "rack": None,
            "tenant": None,
        }
    parsed = parse_librenms_location(location_string, pattern, is_regex)
    # A global parse pattern is best-effort: if this particular location does
    # not match the pattern (no site token resolved), fall back to whole-string
    # matching so locations that don't fit the pattern still resolve a site.
    if not parsed.get("site"):
        parsed["site"] = whole
        if not parsed.get("location"):
            parsed["location"] = whole
    return parsed


def find_matching_platform(librenms_os: str) -> dict:
    """
    Find matching NetBox platform for a LibreNMS OS.

    Tries exact case-insensitive name match first, then falls back to
    PlatformMapping for when the platform name differs from the LibreNMS OS string.

    Args:
        librenms_os (str): OS string from LibreNMS (e.g., 'ios', 'linux', 'junos')

    Returns:
        dict: Dictionary containing:
            - found (bool): Whether a match was found
            - platform (Platform|None): The matched Platform object
            - match_type (str|None): 'mapping', 'exact', 'ambiguous', or None.
              'ambiguous' means multiple PlatformMapping entries matched, or
              multiple Platform objects share the same name, and a single
              Platform could not be determined.

    """
    from dcim.models import Platform

    if not librenms_os or librenms_os == "-":
        return {"found": False, "platform": None, "match_type": None}

    # Try case-insensitive exact name match first.  We defer the "ambiguous"
    # short-circuit so that an explicit PlatformMapping can still disambiguate
    # the LibreNMS OS in exactly the case where multiple Platform rows share
    # the name and the user added a mapping to break the tie.
    platform_ambiguous = False
    try:
        platform = Platform.objects.get(name__iexact=librenms_os)
        return {"found": True, "platform": platform, "match_type": "exact"}
    except Platform.DoesNotExist:
        pass
    except Platform.MultipleObjectsReturned:
        platform_ambiguous = True

    # Fall back to PlatformMapping for when the platform name differs from the LibreNMS OS string
    try:
        from netbox_librenms_plugin.models import PlatformMapping as _PlatformMapping

        _has_platform_mapping = True
    except ImportError:
        _has_platform_mapping = False

    if _has_platform_mapping:
        try:
            mapping = _PlatformMapping.objects.get(librenms_os__iexact=librenms_os)
            return {"found": True, "platform": mapping.netbox_platform, "match_type": "mapping"}
        except _PlatformMapping.DoesNotExist:
            pass
        except _PlatformMapping.MultipleObjectsReturned:
            return {"found": False, "platform": None, "match_type": "ambiguous", "ambiguity_source": "mapping"}

    if platform_ambiguous:
        return {"found": False, "platform": None, "match_type": "ambiguous", "ambiguity_source": "platform"}

    return {"found": False, "platform": None, "match_type": None}


def get_vlan_sync_css_class(exists_in_netbox: bool, name_matches: bool = True) -> str:
    """
    Determine CSS class for a VLAN row on the VLAN sync tab.

    Used by both the server-side table renderer (LibreNMSVLANTable)
    and the client-facing verify endpoint (VerifyVlanSyncGroupView)
    to keep color logic consistent.

    Args:
        exists_in_netbox: Whether the VLAN exists in NetBox (in the selected group or globally).
        name_matches: Whether the VLAN name in NetBox matches the LibreNMS name.

    Returns:
        CSS class string: 'text-success', 'text-warning', or 'text-danger'.

    """
    if not exists_in_netbox:
        return "text-danger"
    if name_matches:
        return "text-success"
    return "text-warning"


def render_vlan_sync_action(vid, exists_in_netbox, name_matches, *, actions_enabled=True):
    """
    Return the VLAN row's status cell: the button that syncs this one row, or its state.

    Shared by the table renderer and the verify endpoint so the row action always matches
    the current VLAN synchronization state.
    """
    canonical_vid = normalize_vlan_vid(vid)
    can_submit = actions_enabled and canonical_vid is not None
    css_class = get_vlan_sync_css_class(exists_in_netbox, name_matches)
    if not exists_in_netbox:
        if not can_submit:
            return format_html('<span class="{}">Not in NetBox</span>', css_class)
        return format_html(
            '<button type="submit" class="btn btn-sm btn-primary" name="sync_one" value="{}"'
            ' title="Create this VLAN in NetBox"><i class="mdi mdi-plus-thick" aria-hidden="true"></i>'
            " Create</button>",
            canonical_vid,
        )
    if not name_matches:
        if not can_submit:
            return format_html('<span class="{}">Name differs</span>', css_class)
        return format_html(
            '<button type="submit" class="btn btn-sm btn-warning" name="sync_one" value="{}"'
            ' title="Update this VLAN\'s name in NetBox"><i class="mdi mdi-pencil" aria-hidden="true"></i>'
            " Update</button>",
            canonical_vid,
        )
    return format_html(
        '<span class="{}"><i class="mdi mdi-check-circle"></i> Synced</span>',
        css_class,
    )


# ============================================
# Interface VLAN CSS helpers
# ============================================
# Shared by LibreNMSInterfaceTable (tables/interfaces.py) and
# SingleVlanGroupVerifyView (views/object_sync/devices.py).


def get_untagged_vlan_css_class(librenms_vid, netbox_vid, exists_in_netbox, missing_vlans, group_matches=True):
    """
    Get CSS class for an untagged VLAN comparison.

    Color logic:
    - Red (text-danger) + warning icon: VLAN not in any NetBox group (cannot sync)
    - Red (text-danger): Interface missing from NetBox, or no untagged VLAN in NetBox
    - Orange (text-warning): Different untagged VLAN assigned, or same VID but different group
    - Green (text-success): Same untagged VLAN assigned in same group (match)

    Args:
        librenms_vid: VLAN ID from LibreNMS.
        netbox_vid: VLAN ID currently assigned in NetBox (int or None).
        exists_in_netbox: Whether the interface exists in NetBox.
        missing_vlans: List of VIDs not found in any NetBox VLAN group.
        group_matches: Whether the selected VLAN group matches the NetBox VLAN's group.
                       Only meaningful when VIDs match; defaults to True.

    Returns:
        CSS class string: text-danger, text-warning, or text-success.

    """
    if not exists_in_netbox:
        return "text-danger"
    if librenms_vid in missing_vlans:
        return "text-danger"
    if librenms_vid == netbox_vid:
        if not group_matches:
            return "text-warning"
        return "text-success"
    if netbox_vid is None:
        return "text-danger"
    return "text-warning"


def get_tagged_vlan_css_class(vid, netbox_tagged_vids, exists_in_netbox, missing_vlans, group_matches=True):
    """
    Get CSS class for a tagged VLAN comparison.

    Color logic:
    - Red (text-danger) + warning icon: VLAN not in any NetBox group (cannot sync)
    - Red (text-danger): Interface missing from NetBox, or VLAN not tagged on this interface
    - Orange (text-warning): Same VID tagged but in different VLAN group
    - Green (text-success): VLAN is tagged on this interface in same group

    Args:
        vid: VLAN ID to check.
        netbox_tagged_vids: Set of VIDs currently tagged on the NetBox interface.
        exists_in_netbox: Whether the interface exists in NetBox.
        missing_vlans: List of VIDs not found in any NetBox VLAN group.
        group_matches: Whether the selected VLAN group matches the NetBox VLAN's group.
                       Only meaningful when VIDs match; defaults to True.

    Returns:
        CSS class string: text-danger, text-warning, or text-success.

    """
    if not exists_in_netbox:
        return "text-danger"
    if vid in missing_vlans:
        return "text-danger"
    if vid in netbox_tagged_vids:
        if not group_matches:
            return "text-warning"
        return "text-success"
    return "text-danger"


def get_missing_vlan_warning(vid, missing_vlans):
    """Return warning icon HTML if VLAN is not found in any NetBox VLAN group."""
    if vid in missing_vlans:
        return (
            ' <i class="mdi mdi-alert text-danger" '
            'title="VLAN not in NetBox\u2014use VLAN Sync first to create it"></i>'
        )
    return ""


def check_vlan_group_matches(
    vlan_type,
    vid,
    selected_group_id,
    netbox_untagged_group_id,
    netbox_tagged_group_ids,
    netbox_untagged_vid,
    netbox_tagged_vids,
):
    """
    Check whether the selected VLAN group matches the NetBox VLAN's group.

    Only relevant when VIDs match — if VIDs differ, the CSS is already
    warning/danger regardless of group.

    Args:
        vlan_type: "U" or "T".
        vid: VLAN ID.
        selected_group_id: Group ID (int or None) the user selected.
        netbox_untagged_group_id: group_id of netbox untagged VLAN (int or None).
        netbox_tagged_group_ids: {vid: group_id} of netbox tagged VLANs.
        netbox_untagged_vid: VID of netbox untagged VLAN (int or None).
        netbox_tagged_vids: set of VIDs tagged in netbox.

    Returns:
        bool: True if groups match (or comparison not applicable).

    """
    if vlan_type == "U":
        if netbox_untagged_vid == vid:
            return netbox_untagged_group_id == selected_group_id
    else:
        if vid in netbox_tagged_vids:
            netbox_gid = netbox_tagged_group_ids.get(vid)
            return netbox_gid == selected_group_id
    return True


def normalize_serial(value) -> str:
    """
    Return the trimmed str form of a LibreNMS serial; only None means missing.

    LibreNMS returns all-digit serials as JSON numbers, so the value must be
    coerced before stripping — and ``str(value or "")`` would silently drop the
    real-but-falsey serial ``0``. Call sites keep their own ``"-"`` placeholder
    guards.

    A vendor marker such as Juniper's leading "S/N " is removed by a NormalizationRule
    in the ``serial`` scope, so the operator can see and edit the transformation.

    Args:
        value: The raw serial as returned by LibreNMS (str, number, or None).

    """
    return "" if value is None else str(value).strip()


def normalize_stack_serial(value) -> str:
    """Return a serial usable as stack identity evidence, preserving numeric zero."""
    serial = normalize_serial(value)
    return "" if is_librenms_placeholder(serial) else serial


def find_devices_by_serial(serial: str, limit: int = 2) -> list:
    """
    Return up to *limit* Devices whose stored serial matches an already-normalized *serial*.

    The exact lookup uses the serial index that migration 0012 creates. That migration also
    canonicalizes existing rows, but a row written afterwards by another tool can hold padding
    again, so a miss re-checks the trimmed column.

    Args:
        serial: The incoming serial, already passed through ``normalize_serial``.
        limit: How many rows to read, enough to tell a unique match from a duplicate.

    """
    from dcim.models import Device
    from django.db.models import CharField, F, Func, Value

    matches = list(Device.objects.filter(serial=serial)[:limit])
    if len(matches) >= limit:
        return matches
    # An exact hit does not mean a unique one: a row written with padding normalizes to the same
    # serial, and the caller uses len(matches) > 1 to refuse a duplicate. Returning early on the
    # first exact match would hide that twin and let the import bind an arbitrary row.
    # Django's Trim() strips spaces only; normalize_serial() uses str.strip(), so name the same set.
    trimmed = Func(F("serial"), Value(" \t\n\r\v\f"), function="BTRIM", output_field=CharField())
    remaining = limit - len(matches)
    matches.extend(
        Device.objects.annotate(trimmed_serial=trimmed)
        .filter(trimmed_serial=serial)
        .exclude(pk__in=[device.pk for device in matches])[:remaining]
    )
    return matches


def normalize_inventory_serial(value, manufacturer=None, preloaded_rules=None) -> str:
    """
    Trim a LibreNMS serial, then apply the serial-scope NormalizationRule chain.

    Vendors decorate the ENTITY-MIB serial: Juniper reports "S/N BCFB9793". Keeping that
    transformation in a rule shows the operator why a stored serial differs from the raw
    inventory, and lets them add the next vendor without a release. Callers keep their own
    placeholder guards, as ``normalize_serial`` does.
    """
    from netbox_librenms_plugin.models import NormalizationRule

    serial = normalize_serial(value)
    if not serial:
        return serial
    # Strip again: a rule that drops a prefix without its trailing space leaves the stored
    # serial padded, which reads as a mismatch against the same serial normalized elsewhere.
    return apply_normalization_rules(
        serial, NormalizationRule.SCOPE_SERIAL, manufacturer=manufacturer, preloaded_rules=preloaded_rules
    ).strip()


def normalize_vlan_vid(value) -> int | None:
    """Return one valid NetBox VLAN VID, or None when the source value is unusable."""
    from ipam.models import VLAN

    vid = coerce_librenms_id(value)
    if vid is None:
        return None
    try:
        return VLAN._meta.get_field("vid").clean(vid, None)
    except ValidationError:
        return None


def index_vlan_source_rows(rows):
    """Index unique source rows by canonical VID and return every unusable row separately."""
    candidate_counts = {}
    for row in rows:
        vid = normalize_vlan_vid(row.get("vlan_vlan"))
        if vid is not None:
            candidate_counts[vid] = candidate_counts.get(vid, 0) + 1

    rows_by_vid = {}
    invalid_rows = []
    for row in rows:
        vid = normalize_vlan_vid(row.get("vlan_vlan"))
        if vid is not None and candidate_counts[vid] == 1:
            rows_by_vid[vid] = row
        else:
            invalid_rows.append(row)
    return rows_by_vid, invalid_rows


# Header attributes for the sync tables: a header may wrap, and librenms_sync.js finds a column by its data-col.
SYNC_TABLE_HEADER_ATTRS = {"class": "text-wrap", "data-col": lambda bound_column: bound_column.name}


def render_vc_member_options(members, selected_id):
    """
    Build the ``<option>`` list for a Virtual Chassis member dropdown.

    Shared by the VC interface/cable/module tables' ``render_device_selection`` so the
    member-name escaping and selected-flag logic cannot drift between per-table copies
    (the XSS escaping was previously added to each copy separately). Ids are compared as
    strings because the module table's selected id can arrive as a string from cached
    row data, while the other tables pass ints — ``str()`` makes both compare correctly.

    Args:
        members: Iterable of VC member devices (anything with ``.id`` and ``.name``).
        selected_id: The member id to mark selected (int or numeric string).

    Returns:
        SafeString: The concatenated ``<option>`` elements, member names escaped.

    """
    # Names are escaped above and the ids are model pks, so the markup is trusted.
    return mark_safe(
        "".join(
            f'<option value="{member.id}"{" selected" if str(member.id) == str(selected_id) else ""}>'
            f"{escape(member.name)}</option>"
            for member in members
        )
    )


def oob_badge_html(record, leading_space=False):
    """
    Return the OOB-source badge for a row merged from the linked OOB controller.

    Shared by the interface/module/cable tables and the cable-verify formatter so the
    ``_source == "oob"`` check and the badge markup cannot drift between per-site copies
    (each previously carried its own conditional).

    Args:
        record: A row dict from the merged host+OOB data.
        leading_space: Prepend a space when the badge follows inline text (the cable
            port-name column).

    Returns:
        SafeString: The badge markup, or ``""`` when the row is not OOB-sourced.

    """
    if record.get("_source") != OOB_INVENTORY_SOURCE:
        return ""
    # Static trusted markup — mark_safe, not format_html (which requires interpolation
    # args and raises TypeError when given a bare string).
    # Static trusted markup, no interpolation.
    return mark_safe((" " if leading_space else "") + OOB_BADGE_HTML)


def rule_block_html(rule_block):
    """
    Return the pill that says why the interface rules refuse a row's write.

    Shared by the IP and cable tables and the cable-verify formatter. It uses the badge language
    of the interfaces table's rule pill.

    Args:
        rule_block (dict): The row data from ``interface_rules.row_rule_block``.

    Returns:
        SafeString: The pill markup.

    """
    from netbox_librenms_plugin.interface_rules import RuleDecisionKind  # interface_rules imports utils

    if rule_block["kind"] == RuleDecisionKind.IGNORE.value:
        color, icon, label = "secondary", "mdi-eye-off", "Ignored"
    else:
        color, icon, label = "warning", "mdi-refresh", "Refresh needed"
    return format_html(
        '<span class="badge bg-{}-lt fw-normal d-inline-flex align-items-center gap-1" title="Not synced: {}">'
        '<i class="mdi {}"></i>{}</span>',
        color,
        rule_block["reason"],
        icon,
        label,
    )


def remote_port_html(value, record):
    """
    Return one cable row's remote-port cell: the port name, its link, and its badges.

    Shared by the cable table column and the cable-verify formatter so an inline verify cannot
    drop a badge the full render draws. Escaped in every branch, including the bare one: the
    verify response is injected into the page as HTML.

    Args:
        value: The remote port name, or None when the row resolved no name.
        record: A cable row dict.

    Returns:
        SafeString: The cell markup.

    """
    # Static trusted markup, mirrors the Serial badge idiom.
    manual_badge = (
        mark_safe(' <i class="mdi mdi-gesture-tap-button text-muted" title="Remote end picked manually"></i>')
        if record.get("manual_remote")
        else ""
    )
    # One adjacency reported over both CDP and LLDP renders once; name the protocol whose
    # row was collapsed into this one so the evidence is not silently dropped.
    also = record.get("also_reported_by")
    protocol_badge = (
        format_html(
            ' <i class="mdi mdi-lan-connect text-muted" title="Also reported over {}"></i>',
            ", ".join(str(protocol).upper() for protocol in also),
        )
        if also
        else ""
    )
    # Normalize None to "" like the local-port and remote-device cells — an unset remote port
    # name would otherwise render the literal "None".
    display_value = value or ""
    if url := record.get("remote_port_url"):
        return format_html('<a href="{}">{}</a>{}{}', url, display_value, manual_badge, protocol_badge)
    return format_html("{}{}{}", display_value, manual_badge, protocol_badge)


def is_valid_ports_payload(payload) -> bool:
    """
    Return True only for a well-formed LibreNMS ports payload.

    ``LibreNMSAPI.get_ports`` is an external boundary: a truthy success does not guarantee the
    expected shape, so every caller that indexes ``payload["ports"]`` or enriches the rows must
    gate on this and fail closed on a malformed 200 rather than raising on ``.get()`` / iteration.

    Args:
        payload: The raw value returned for a ports fetch (host, OOB, or a cached snapshot).

    Returns:
        bool: True when *payload* is a dict whose ``"ports"`` is a list of dict rows.

    """
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("ports"), list)
        and all(isinstance(port, dict) for port in payload["ports"])
    )


def coerce_positive_int(value) -> int | None:
    """
    Coerce a value to a strictly-positive ``int``, or ``None`` when invalid.

    A named alias for :func:`coerce_librenms_id` at the NetBox-pk coercion sites —
    bulk-import collision detection and module-inventory port identity — so the call
    reads as pk handling while the strictly-positive-int rule genuinely lives in ONE
    place and can't drift. The two apply the identical rule: only an ``int`` (excluding
    ``bool``, an ``int`` subclass, so ``int(True)`` can't silently become ``1``) or an
    integer-parseable ``str``, kept only when ``> 0``; non-integer types such as ``float``
    are rejected outright (``int(1.9)`` would truncate to a valid-looking ``1`` and resolve
    a malformed id to the wrong object).

    Args:
        value: The raw value to coerce (a NetBox pk or similar identifier).

    Returns:
        int | None: The positive integer, or None if it can't be coerced.

    """
    return coerce_librenms_id(value)


def cached_row_matches(cached_row, device_id) -> bool:
    """
    Decide whether a cached LibreNMS device row may be served for *device_id*.

    A non-dictionary row or a row whose own ``device_id`` contradicts the requested id
    must not be served as this device. A dictionary without a readable ``device_id`` stays
    trusted, and ``None`` never matches. An uncoercible requested id never matches either.
    Shared by the single-import and bulk-import cache reads so the acceptance rule cannot drift.

    Args:
        cached_row: The cached payload for *device_id* (usually a dict, possibly None).
        device_id: The LibreNMS device id the caller asked for.

    Returns:
        bool: True when the cached row may be used for *device_id*.

    """
    if not isinstance(cached_row, dict):
        return False
    requested_id = coerce_librenms_id(device_id)
    if requested_id is None:
        return False
    cached_row_id = cached_row.get("device_id")
    return cached_row_id is None or coerce_librenms_id(cached_row_id) == requested_id


def row_identity_matches(row, device_id) -> bool:
    """
    Decide whether a LibreNMS device payload verifiably describes *device_id*.

    Stricter than :func:`cached_row_matches`: a real LibreNMS device read always returns
    a dict carrying its own ``device_id``, so anything else — a non-dict payload, a
    missing or contradicting id, an un-coercible requested id — fails closed. Shared by
    the bulk-import collision pre-check and the single-row live-fetch fallback so a
    payload that flunks this check is neither imported nor written into the shared cache.

    Args:
        row: The fetched (or cached) payload to identity-check.
        device_id: The LibreNMS device id the caller asked for.

    Returns:
        bool: True when the payload's own device_id equals the requested id.

    """
    requested_id = coerce_librenms_id(device_id)
    if requested_id is None:
        return False
    row_id = coerce_librenms_id(row.get("device_id")) if isinstance(row, dict) else None
    return row_id == requested_id


class PortDisclosure:
    """
    The one rule that decides whether a refusal or a pill may name a LibreNMS port.

    A port's id, name and refusing rule may be shown only when the port's owner is in the user's
    view scope. When an interface is bound to the port on this server, that interface and its own
    owner (read from the binding) must be in view scope, and the owner a caller passes does not
    count. For an unbound port the caller's owner counts; None is never visible. An ambiguous
    binding is never visible.

    A table calls :meth:`preload` once with every ``(port_id, owner)`` it may ask about, so the
    rule costs a fixed number of queries per render. A writer may ask one port at a time. The
    cable and IP tables and writers all use it.
    """

    def __init__(self, user, server_key: str):
        """Bind the rule to one user and one LibreNMS server; nothing is read yet."""
        self._user = user
        self._server_key = server_key
        self._bindings = {}
        self._visible = {}

    def preload(self, ports) -> None:
        """
        Read the bindings and the view scope for these ``(port_id, owner)`` pairs.

        It costs one query per interface model for the bindings, and one per model for the view
        scope, whatever the number of ports.
        """
        from virtualization.models import VMInterface

        ports = list(ports)
        new_ids = {port_id for port_id, _owner in ports if port_id not in self._bindings}
        if new_ids:
            for model in (Interface, VMInterface):
                match = identity_q(
                    model,
                    server=self._server_key,
                    identities=new_ids,
                    roles=(MappingRole.OWN, MappingRole.OOB),
                )
                for interface in model.objects.filter(match):
                    port_id = read_mapping(interface).own_id(self._server_key)
                    if port_id in new_ids:
                        self._bindings.setdefault(port_id, []).append(interface)
            for port_id in new_ids:
                self._bindings.setdefault(port_id, [])
        wanted = {}
        for port_id, owner in ports:
            for model, pk in self._evidence(port_id, owner):
                wanted.setdefault(model, set()).add(pk)
        for model, pks in wanted.items():
            pks -= {pk for (known_model, pk) in self._visible if known_model is model}
            if not pks:
                continue
            visible = self._visible_pks(model, pks)
            self._visible.update({(model, pk): pk in visible for pk in pks})

    def __call__(self, port_id, owner) -> bool:
        """Return whether a refusal may name *port_id*; reads what :meth:`preload` did not."""
        self.preload([(port_id, owner)])
        evidence = self._evidence(port_id, owner)
        return bool(evidence) and all(self._visible[key] for key in evidence)

    def _evidence(self, port_id, owner) -> list:
        """Return the ``(model, pk)`` objects that must be visible, or ``[]`` when none can be."""
        from virtualization.models import VirtualMachine, VMInterface

        bound = self._bindings.get(port_id, [])
        if len(bound) > 1:
            return []
        if bound:
            interface = bound[0]
            if isinstance(interface, VMInterface):
                return [(VMInterface, interface.pk), (VirtualMachine, interface.virtual_machine_id)]
            return [(Interface, interface.pk), (Device, interface.device_id)]
        return [] if owner is None else [(type(owner), owner.pk)]

    def _visible_pks(self, model, pks) -> set:
        """Return the subset of *pks* the user may view, with the same checks as :func:`_object_is_visible`."""
        user = self._user
        if user is None or not getattr(user, "is_authenticated", False):
            return set()
        if getattr(user, "is_superuser", False):
            return set(pks)
        if not user.has_perm(f"{model._meta.app_label}.view_{model._meta.model_name}"):
            return set()
        return set(model.objects.restrict(user, "view").filter(pk__in=pks).values_list("pk", flat=True))


_MODULE_TOKEN_LEAF_FIX_VERSION = (4, 5, 6)
_PARENT_CHASSIS_CLEAN_BUG_VERSION = (4, 4, 0)
_STANDALONE_VM_HOST_VERSION = (4, 6, 0)
_MODULE_RELOCATION_VERSION = (4, 7, 0)
_NETBOX_VERSION_PREFIX_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _get_netbox_version_tuple():
    """
    Return the running NetBox version as a (major, minor, patch) int tuple, or ``None`` when it cannot be determined.

    Tolerates trailing build metadata (``-dev``, ``-Docker-3.2.0``, ``+local``,
    ``.dev1``, etc.) by extracting the leading three numeric components and
    ignoring anything past them.

    Returns:
        tuple[int, int, int] | None: Parsed NetBox version, or ``None`` when it cannot be determined.

    """
    try:
        from netbox.settings import RELEASE

        version = getattr(RELEASE, "version", "") or ""
    except (ImportError, ModuleNotFoundError, AttributeError):
        return None
    match = _NETBOX_VERSION_PREFIX_RE.match(version)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def netbox_clean_reads_parent_virtual_chassis():
    """
    Return whether NetBox raises ``AttributeError`` for a cross-member parent interface.

    NetBox 4.4.0 (issue #20197) dereferences ``self.parent.virtual_chassis`` in
    ``Interface.clean()``; 4.4.1 fixed it. When the version cannot be detected we keep the
    tolerance, so an undetectable release cannot turn a known core defect into a 500.

    Returns:
        bool: Whether the running NetBox needs tolerance for the parent validation defect.

    """
    version = _get_netbox_version_tuple()
    if version is None:
        return True
    return version == _PARENT_CHASSIS_CLEAN_BUG_VERSION


def netbox_interface_clean(interface):
    """
    Run NetBox's ``clean()`` on an interface, with the 4.4.0 parent-chassis defect handled.

    NetBox 4.4.0 reads ``self.parent.virtual_chassis`` for a parent on another device, so the
    chassis check it means to run raises AttributeError. Its type rule for a parent runs before
    that check. On the same chassis the parent is valid, so validation runs again without it and
    the later rules still apply. On another chassis (or none), the error NetBox means is raised.

    Args:
        interface: The Interface or VMInterface to validate.

    Raises:
        ValidationError: NetBox refuses the interface.
        AttributeError: Any failure that is not the 4.4.0 parent-chassis defect.

    """
    try:
        interface.clean()
    except AttributeError as exc:
        parent = getattr(interface, "parent", None)
        source_chassis = getattr(getattr(interface, "device", None), "virtual_chassis_id", None)
        if not (
            # exc.name is the attribute the failed access asked for, so this matches the one dereference.
            getattr(exc, "name", None) == "virtual_chassis"
            and parent is not None
            and getattr(interface, "device_id", None) != getattr(parent, "device_id", None)
            and source_chassis is not None
            and netbox_clean_reads_parent_virtual_chassis()
        ):
            raise
        if parent.device.virtual_chassis_id != source_chassis:
            raise ValidationError(
                {
                    "parent": (
                        f"The selected parent interface ({parent}) belongs to {parent.device}, which is not part "
                        f"of virtual chassis {interface.device.virtual_chassis}."
                    )
                }
            ) from exc
        interface.parent_id = None
        try:
            interface.clean()
        finally:
            interface.parent = parent
        logger.debug(
            "Interface %s: this NetBox cannot validate its parent on another chassis member; both interfaces "
            "belong to virtual chassis %s, so the parent is accepted.",
            interface.name,
            source_chassis,
        )


def netbox_allows_standalone_vm_host():
    """
    Return whether NetBox allows a VM to use a host without a cluster.

    NetBox 4.6 added direct VM assignment to standalone devices. Older releases require the
    selected host and VM to share a cluster. An undetectable version fails closed because an
    optimistic result would let the import reach ``VirtualMachine.full_clean()`` with an invalid
    placement.

    Returns:
        bool: Whether standalone host placement is supported.

    """
    version = _get_netbox_version_tuple()
    return version is not None and version >= _STANDALONE_VM_HOST_VERSION


def netbox_constrains_module_bay_types():
    """Return True when NetBox only installs a module into a bay whose module bay types fit (NetBox 4.7)."""
    from dcim.models import ModuleBay

    return any(field.name == "module_bay_types" for field in ModuleBay._meta.get_fields())


def load_module_type_bay_types():
    """Return the module bay types of every module type in one query, keyed by module type pk."""
    if not netbox_constrains_module_bay_types():
        return {}
    from dcim.models import ModuleType

    pairs = {}
    for type_pk, bay_type_pk in ModuleType.module_bay_types.through.objects.values_list(
        "moduletype_id", "modulebaytype_id"
    ):
        pairs.setdefault(type_pk, set()).add(bay_type_pk)
    return {type_pk: frozenset(bay_type_pks) for type_pk, bay_type_pks in pairs.items()}


def module_type_bay_type_ids(module_type, preloaded=None):
    """Return the module bay types *module_type* installs into; an empty set fits any bay.

    *preloaded* is the result of ``load_module_type_bay_types``; without it, this reads one query.
    """
    if module_type is None or not netbox_constrains_module_bay_types():
        return frozenset()
    if preloaded is not None:
        return preloaded.get(module_type.pk, frozenset())
    return frozenset(module_type.module_bay_types.values_list("pk", flat=True))


def prefetch_module_bay_types(bays):
    """Prefetch each bay's module bay types where NetBox has them, so a fit check adds no query per bay."""
    return bays.prefetch_related("module_bay_types") if netbox_constrains_module_bay_types() else bays


def module_bay_type_fit(bay, accepted_bay_type_ids):
    """
    Return whether a bay and a module type fit by their module bay types.

    True when they share a type, False when NetBox refuses the install (both typed, no type shared),
    and None when either side is untyped.
    """
    if not accepted_bay_type_ids:
        return None
    bay_type_ids = {bay_type.pk for bay_type in bay.module_bay_types.all()}
    if not bay_type_ids:
        return None
    return not bay_type_ids.isdisjoint(accepted_bay_type_ids)


def module_bay_fits(bay, accepted_bay_type_ids):
    """Apply NetBox's install rule: a typed bay refuses a typed module only when no type is shared."""
    return module_bay_type_fit(bay, accepted_bay_type_ids) is not False


def netbox_relocates_module_subtree():
    """
    Return True when the running NetBox moves a module's whole subtree with it.

    NetBox 4.7 (issue #15289) relocates the module's components, its own module bays and any
    child modules installed in them, and re-resolves template-derived names for the destination
    bay. Below 4.7 the same assignment is accepted with no error but moves only the module row,
    leaving its interfaces, its nested bays and its child modules on the source device.

    Unlike the other version predicates here, an undetectable version returns False. The two
    above tolerate a wrong guess; this one would write a half-moved subtree.
    """
    version = _get_netbox_version_tuple()
    if version is None:
        return False
    return version >= _MODULE_RELOCATION_VERSION


def netbox_resolves_module_token_per_leaf():
    """
    Return whether NetBox resolves one ``{module}`` token to the leaf bay position.

    NetBox 4.5.6 (issue #20467) changed single-token resolution to use the
    leaf bay's position instead of the root ancestor's. With that fix in
    place, sibling bays under the same parent always produce unique
    interface names, so the plugin's nested-name-conflict check no longer
    applies.

    When the version cannot be detected we assume the fix is present
    (permissive default). The plugin pins NetBox >= 4.4 and we don't want
    to surface false positives on releases newer than this code knows about.

    Returns:
        bool: Whether NetBox resolves the token to the leaf module bay's position.

    """
    version = _get_netbox_version_tuple()
    if version is None:
        return True
    return version >= _MODULE_TOKEN_LEAF_FIX_VERSION


# This is the formatter that caught-error-text trusts in a log call.
# nosemgrep: caught-error-text-shadow  # noqa: ERA001
def validation_error_detail(exc: ValidationError) -> str:
    """
    Flatten a ValidationError into a single human-readable string for a JSON error body.

    Shared by the interface-sync and migrate views so the two sync flows format
    validation errors identically.

    Args:
        exc: The ValidationError to flatten.

    Returns:
        str: ``field: message`` pairs joined with ``"; "`` (or the plain messages/str form).

    """
    if hasattr(exc, "message_dict"):
        return "; ".join(f"{field}: {' '.join(str(m) for m in msgs)}" for field, msgs in exc.message_dict.items())
    return "; ".join(str(m) for m in exc.messages) if hasattr(exc, "messages") else str(exc)


def is_active_superuser(user) -> bool:
    """Return whether *user* is an authenticated, active superuser: the only viewer who may view every object."""
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "is_superuser", False)
    )


def refused_model_field(model, key) -> str | None:
    """Return the concrete *model* field that a ValidationError key names, or None for any other key."""
    # A validator or a post_clean receiver can key an error by any text, such as an object's name.
    try:
        field = model._meta.get_field(key)
    except FieldDoesNotExist:
        return None
    return field.name if field.concrete else None


def hidden_refusal_text(model, fields) -> str:
    """
    Return the text that tells a viewer which *model* fields NetBox refuses, without NetBox's message.

    Args:
        model (type[Model]): The model that NetBox validated.
        fields (list[str]): The concrete *model* fields that NetBox refuses (from ``refused_model_field``).

    Returns:
        str: For example "NetBox refuses the serial field (only a superuser sees the message)".

    """
    if not fields:
        subject = f"the {model._meta.verbose_name}"
    elif len(fields) == 1:
        subject = f"the {fields[0]} field"
    else:
        subject = f"the {', '.join(fields[:-1])} and {fields[-1]} fields"
    return f"NetBox refuses {subject} (only a superuser sees the message)"


# The one text of a database error: PostgreSQL's own text can name rows and values outside the viewer's scope.
DATABASE_ERROR_MESSAGE = "The database refused the operation. Refresh the data and try again."


# This is the rule that caught-error-text trusts.
# nosemgrep: caught-error-text-shadow  # noqa: ERA001
def exception_text_for(exc: Exception, model, user) -> str:
    """
    Return the text of a caught *exc* that *user* may read.

    NetBox's ``clean()`` messages can name related objects, and admin ``CUSTOM_VALIDATORS`` or
    ``post_clean`` and ``pre_save`` receivers can add any text under any key. So only a superuser
    gets the message of a ValidationError. Every other viewer gets the concrete *model* fields
    that the error keys name, or the model. Identity conflicts and database errors use generic
    text because their details can identify objects outside the viewer's scope. A database error
    is a ``DatabaseError``, a psycopg ``Error``, or an ``AbortRequest`` that NetBox raised from one.
    A lock conflict (``classify_conflict``) gets the "try again" text for every viewer.

    Args:
        exc (Exception): The caught error.
        model (type[Model]): The model that NetBox validated.
        user (User | None): The viewer.

    Returns:
        str: The "try again" text, safe identity or database error text, scoped validation text,
            or the other exception's text.

    """
    if classify_conflict(exc):
        return TRY_AGAIN_MESSAGE
    if isinstance(exc, AmbiguousLibreNMSIdError):
        return "Multiple records use this LibreNMS ID. Ask an administrator to correct the mappings."
    if isinstance(exc, (DatabaseError, psycopg.Error)) or (
        isinstance(exc, AbortRequest) and nearest_database_error(exc) is not None
    ):
        logger.warning("The database rejected %s: %s", model.__name__, exc)
        return DATABASE_ERROR_MESSAGE
    if not isinstance(exc, ValidationError):
        return str(exc)
    if is_active_superuser(user):
        return validation_error_detail(exc)
    keys = exc.error_dict if hasattr(exc, "error_dict") else ()
    fields = [name for name in dict.fromkeys(refused_model_field(model, key) for key in keys) if name]
    return hidden_refusal_text(model, fields)


# The device-level IP foreign keys this plugin re-homes during OOB linking, merges, and the
# Stage-2b "move to winner" actions. NetBox requires each to reference an address assigned to
# one of THAT device's own interfaces.
DEVICE_IP_FK_FIELDS = ("primary_ip4", "primary_ip6", "oob_ip")

# Human-readable labels for the device IP FK fields, used in user-facing transfer/reconcile messages.
# Single source so the per-field reconcile notes and the TransferDeviceIPView buttons can't disagree.
DEVICE_IP_FK_LABELS = {"primary_ip4": "primary IPv4", "primary_ip6": "primary IPv6", "oob_ip": "OOB IP"}


def set_device_ip_fk(device, field, ip, *, save=True):
    """
    Assign a device IP FK with the NetBox ownership invariant enforced.

    Sets the device's ``primary_ip4`` / ``primary_ip6`` / ``oob_ip`` FK, then (by
    default) persists ONLY that column. NetBox requires those fields to reference an
    address assigned to one of *that* device's own interfaces. The OOB/merge/move
    flows persist these via ``save(update_fields=[...])`` to avoid ``full_clean()``
    rejecting the write over unrelated pre-existing inconsistencies (e.g. ``face`` set
    without ``rack``) — but ``update_fields`` also skips the ownership check, so a
    careless call site could silently store an FK pointing at an address on *another*
    device's interface. This is the single guarded chokepoint for those writes:
    clearing (``ip is None``) is always allowed.

    Re-homing an FK between two devices must still order the writes in the caller —
    release the donor (set ``None``) BEFORE the winner claims it, because
    ``primary_ip*``/``oob_ip`` are UNIQUE per address. Run inside the caller's
    transaction with the relevant rows locked.

    Args:
        device: The NetBox device whose FK is being assigned.
        field (str): One of ``primary_ip4`` / ``primary_ip6`` / ``oob_ip``.
        ip: The IPAddress to assign, or None to clear the FK.
        save (bool): When True (default) persist only this column; pass False to
            validate + assign only and let the caller batch *field* into its own
            ``update_fields``.

    Returns:
        str: The assigned *field* (handy for
            ``update_fields.append(set_device_ip_fk(...))``).

    Raises:
        ValueError: If *field* is unsupported, or a non-``None`` *ip* is not assigned
            to an interface on *device*, or its family doesn't match the field.

    """
    from dcim.models import Interface

    if field not in DEVICE_IP_FK_FIELDS:
        raise ValueError(f"set_device_ip_fk: unsupported field {field!r} (expected one of {DEVICE_IP_FK_FIELDS})")
    if ip is not None:
        assigned = getattr(ip, "assigned_object", None)
        if not isinstance(assigned, Interface) or assigned.device_id != device.pk:
            raise ValueError(
                f"set_device_ip_fk: refusing to set {field} on device pk={device.pk} — "
                f"address {ip} is not assigned to an interface on that device"
            )
        # NetBox's Device.clean() requires primary_ip4 to be IPv4 and primary_ip6 to be IPv6;
        # update_fields skips full_clean(), so enforce the family here too (oob_ip is family-
        # agnostic). Otherwise an IPv6 address could be silently stored as primary_ip4.
        family = ip.family
        if field == "primary_ip4" and family != 4:
            raise ValueError(f"set_device_ip_fk: refusing to set primary_ip4 to non-IPv4 address {ip}")
        if field == "primary_ip6" and family != 6:
            raise ValueError(f"set_device_ip_fk: refusing to set primary_ip6 to non-IPv6 address {ip}")
    if save:
        device.snapshot()
    setattr(device, field, ip)
    if save:
        device.save(update_fields=[field, "last_updated"])
    return field


def build_migrated_context(obj, server_key: str = "default") -> dict:
    """
    Build the donor "migrated mode" template context.

    Shared by the full sync page and the HTMX tab partials so a merged donor shows the
    migration UI — and hides ordinary sync actions — consistently on both the initial
    render and after a tab refresh (the partial-render views build their own context
    and would otherwise drop the marker).

    ``migrated_to_winner`` is a lazy proxy: the winner ``Device`` row is fetched only when a template
    actually reads it (the interface/IP partials, which render the per-row "Move to winner" controls).
    The cable/module/VLAN partials render only the marker banner and never touch it, so they avoid the
    lookup on every HTMX refresh. The self-pointing suppression is done in memory (``device_id ==
    obj.pk``) so it never forces that fetch — the donor always exists, so the old ``Device`` lookup
    could only ever have returned ``obj`` itself.

    Args:
        obj: The donor device to build migrated-mode context for.
        server_key (str): The LibreNMS server key the marker is namespaced under.

    Returns:
        dict: ``{migrated_to_marker, migrated_to_winner}``. Both None when not in migrated mode (a
            rejected server key, no marker, or a corrupt self-pointing marker). ``migrated_to_winner``
            is a lazy ``Device`` proxy (truthiness/attribute access resolves it; it proxies None if
            the winner row was since deleted) — test it via truthiness in templates, not ``is None``.

    """
    # Callers pass a request-supplied scope. Only mark_migrated() writes markers, and it
    # validates the key first, so a rejected key can hold none — report migrated mode off instead
    # of letting the marker reader raise on it.
    if not is_server_key(server_key or "default"):
        return {"migrated_to_marker": None, "migrated_to_winner": None}
    marker = read_mapping(obj).migrated_to(server_key or "default") if obj is not None else None
    # A self-pointing marker (winner == this donor) is corrupt: it would flip the donor's own sync
    # page into migrated mode resolving the "winner" to itself, hiding the ordinary sync controls.
    # Suppress it in memory (no Device fetch) — the donor always exists, so resolving device_id would
    # only ever return obj. The marker reader deliberately does NOT reject it: the move views read
    # it via _resolve_winner_for_donor() to report it as "stale/corrupt".
    if marker is None or marker.device_id == getattr(obj, "pk", None):
        return {"migrated_to_marker": None, "migrated_to_winner": None}

    # Defer the row fetch until a template reads the winner (cable/module/VLAN never do), saving a
    # query per HTMX refresh.
    return {
        "migrated_to_marker": marker,
        "migrated_to_winner": SimpleLazyObject(lambda: Device.objects.filter(pk=marker.device_id).first()),
    }


def has_nested_name_conflict(module_type, module_bay, sibling_counts=None):
    """
    Check if installing this module type in a nested bay would cause a name conflict.

    Returns a non-empty reason string when ALL of the following are true:
    - The running NetBox version is older than 4.5.6 (issue #20467 fix)
    - The module type has interface templates using ``{module}``
    - The bay is nested (its parent is owned by an installed module)
    - There is at least one sibling bay under the same parent

    On NetBox < 4.5.6, ``resolve_name()`` replaces a single ``{module}`` token
    with the root ancestor's bay position, producing the same interface name
    for every sibling at this nesting level. NetBox 4.5.6+ resolves the token
    to the leaf bay's position, so siblings get unique names and no conflict
    can arise — in that case this function always returns an empty string.

    Returns an empty string (falsy) when no conflict is detected.

    Args:
        module_type: The ModuleType to install.
        module_bay: The ModuleBay target.
        sibling_counts: Optional precomputed dict mapping module_id → bay count.
            When provided, avoids a per-call DB query.  Pass
            ``{mid: len(bays) for mid, bays in module_scoped_bays.items()}`` from
            the caller that already has the bay maps loaded.

    """
    from dcim.constants import MODULE_TOKEN

    if netbox_resolves_module_token_per_leaf():
        return ""  # NetBox 4.5.6+ resolves {module} to the leaf bay's position

    if not module_bay or not module_bay.module_id:
        return ""  # Top-level bay — no conflict

    templates = list(module_type.interfacetemplates.all())
    if not templates:
        return ""  # No interface templates

    if not any(MODULE_TOKEN in t.name for t in templates):
        return ""  # Template doesn't use {module}

    if sibling_counts is not None:
        sibling_count = sibling_counts.get(module_bay.module_id, 0)
    else:
        from dcim.models import ModuleBay as ModuleBayModel

        sibling_count = ModuleBayModel.objects.filter(
            device=module_bay.device,
            module_id=module_bay.module_id,
        ).count()

    if sibling_count <= 1:
        return ""

    return (
        f"Interface templates for '{module_type.model}' use {{module}}, which resolves to "
        f"the same root bay name for all {sibling_count} sibling bays on this NetBox "
        "version — installing here would create duplicate interface names. "
        "Upgrade NetBox to 4.5.6 or later (issue #20467) to resolve this — the "
        "{module} token will then resolve to each leaf bay's position."
    )


class _ModuleTypeIndex(dict):
    """
    Map LibreNMS keys to ``ModuleType`` objects and store manufacturer-scoped overrides.

    Behaves exactly like a plain ``dict`` for the global key space (model/part_number
    plus any global ``ModuleTypeMapping`` rows), but additionally exposes
    ``mfr_mappings``, a dict keyed by ``(manufacturer_pk, librenms_model)`` populated
    from manufacturer-scoped ``ModuleTypeMapping`` rows. ``resolve_module_type`` probes
    the manufacturer-scoped key first so vendor-specific overrides win for matching
    devices without leaking into other vendors' lookups. ``generic`` holds the
    :func:`get_generic_module_types_indexed` index, so every caller of the resolver gets
    the same Generic fallback as the table.
    """

    __slots__ = ("generic", "mfr_mappings")

    def __init__(self, *args, mfr_mappings=None, generic=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mfr_mappings: dict = mfr_mappings or {}
        self.generic: dict = generic or {}


# (version, index) held as a single tuple so a lock-free reader always gets a CONSISTENT pair from
# one atomic reference read — never a freshly-bumped version paired with a stale index, or vice
# versa. Rebuilds are serialised by the lock so concurrent renders don't each rebuild.
_MODULE_TYPES_INDEX_CACHE: tuple = (None, None)
_MODULE_TYPES_INDEX_LOCK = threading.Lock()


def _module_types_index_version():
    """
    Return a cheap fingerprint of the module-type index inputs.

    Changes on any create/delete/edit (via ``save()``) of a ModuleType, a module-type
    InterfaceTemplate, a ModuleTypeMapping, or a Manufacturer (the Generic index keys on its name), so the cache below self-invalidates
    (including across test transactions) with no signal wiring.

    Limitation: a count-preserving bulk ``QuerySet.update()`` bypasses ``save()`` and so does
    not bump ``last_updated`` (``auto_now``); such an edit leaves the fingerprint unchanged and
    the cached index stale until the row count changes or the worker restarts. The plugin only
    ever mutates these models via ``save()``, so this affects external bulk-update tooling only.

    Returns:
        tuple[tuple[int, str], ...]: Fingerprints for module types, interface templates,
            module type mappings, and manufacturers.

    """
    from dcim.models import InterfaceTemplate, Manufacturer, ModuleType

    from netbox_librenms_plugin.models import ModuleTypeMapping

    def _fp(qs):
        agg = qs.aggregate(n=Count("pk"), latest=Max("last_updated"))
        return (agg["n"] or 0, agg["latest"].isoformat() if agg["latest"] else "")

    return (
        _fp(ModuleType.objects.all()),
        _fp(InterfaceTemplate.objects.filter(module_type__isnull=False)),
        _fp(ModuleTypeMapping.objects.all()),
        _fp(Manufacturer.objects.all()),
    )


def get_module_types_indexed() -> dict:
    """
    Return the module-type index, rebuilt only when its inputs change.

    The underlying build loads every ModuleType (with interface templates) and
    ModuleTypeMapping. This per-render work dominates the module-sync render once
    the rule-lookup cost is removed. The cache uses a cheap fingerprint of those
    tables so repeat renders reuse it.

    Thread-safe: the (version, index) pair is read as one atomic tuple reference on the fast
    path, and rebuilds happen under a lock so concurrent renders neither rebuild redundantly nor
    observe a torn version/index swap.

    Returns:
        dict: Module types indexed by LibreNMS lookup keys.

    """
    global _MODULE_TYPES_INDEX_CACHE

    version = _module_types_index_version()
    cached_version, cached_index = _MODULE_TYPES_INDEX_CACHE  # single atomic read of the pair
    if cached_index is not None and cached_version == version:
        return cached_index

    with _MODULE_TYPES_INDEX_LOCK:
        # Re-check inside the lock: another thread may have rebuilt while we waited.
        cached_version, cached_index = _MODULE_TYPES_INDEX_CACHE
        if cached_index is None or cached_version != version:
            cached_index = _build_module_types_index()
            _MODULE_TYPES_INDEX_CACHE = (version, cached_index)
        return cached_index


def _build_module_types_index() -> dict:
    """
    Return all NetBox module types indexed by model (and part_number), with ModuleTypeMapping applied.

    Global (manufacturer=NULL) ``ModuleTypeMapping`` rows are merged into the
    main index and take priority over the base model/part_number keys, so that
    explicit overrides win when the same string appears in both.

    Manufacturer-scoped ``ModuleTypeMapping`` rows are kept out of the main
    index. They live under ``mfr_mappings`` keyed by ``(manufacturer_pk,
    librenms_model)`` so they apply only to devices of that vendor.

    Returns:
        dict: Module types indexed by global keys and manufacturer-scoped overrides.

    """
    from dcim.models import ModuleType

    from netbox_librenms_plugin.models import ModuleTypeMapping

    result: dict = {}
    ambiguous: set = set()
    for mt in ModuleType.objects.all().select_related("manufacturer").prefetch_related("interfacetemplates"):
        seen_this_entry: set = set()
        for key in (mt.model, mt.part_number):
            if not key or key in seen_this_entry:
                continue
            seen_this_entry.add(key)
            if key in ambiguous:
                continue
            if key in result:
                ambiguous.add(key)
                del result[key]
            else:
                result[key] = mt
    # Mapping ambiguity is tracked separately so that a unique mapping always
    # wins over a base ModuleType entry (explicit overrides take priority).
    #
    # Precedence: a manufacturer-scoped mapping (mfg_pk, librenms_model) wins
    # over a global one for matching devices; both win over base ModuleType
    # entries. Manufacturer-scoped rows live in ``mfr_mappings`` and are
    # NOT merged into the main dict — keeping them isolated avoids polluting
    # lookups for other vendors. The DB conditional UniqueConstraint pair
    # (see ``ModuleTypeMapping.Meta.constraints``) ensures (a) no two global
    # rows share a librenms_model, and (b) no two rows share the same
    # (librenms_model, manufacturer) pair, so the ``mapping_ambiguous``
    # branches below are defensive-only — they cannot fire under the current
    # schema. ``get_module_type_ambiguities`` therefore has no companion
    # mapping-collision helper.
    mapping_seen: set = set()
    mapping_ambiguous: set = set()
    mfr_mappings: dict = {}
    mfr_seen: set = set()
    for mapping in ModuleTypeMapping.objects.select_related(
        "netbox_module_type__manufacturer", "manufacturer"
    ).prefetch_related("netbox_module_type__interfacetemplates"):
        if mapping.manufacturer_id is not None:
            mfr_key = (mapping.manufacturer_id, mapping.librenms_model)
            if mfr_key in mfr_seen:
                # Defensive: blocked by conditional UniqueConstraint.
                continue
            mfr_seen.add(mfr_key)
            mfr_mappings[mfr_key] = mapping.netbox_module_type
            continue
        key = mapping.librenms_model
        if key in mapping_ambiguous:
            continue
        if key in mapping_seen:
            mapping_ambiguous.add(key)
            del result[key]
        else:
            mapping_seen.add(key)
            result[key] = mapping.netbox_module_type
    return _ModuleTypeIndex(result, mfr_mappings=mfr_mappings, generic=get_generic_module_types_indexed())


def get_module_type_ambiguities() -> dict:
    """
    Return part_number/model strings that map to multiple NetBox ModuleTypes.

    Returns a dict mapping ``key`` (a model or part_number string that collides
    across two or more ModuleType rows) → list of ``ModuleType`` instances that
    share that key.  ``get_module_types_indexed`` deliberately drops these keys
    from its lookup index (fail-closed safety), so callers wanting to *report*
    why a LibreNMS string can't be matched to a single NetBox ModuleType use
    this helper to surface the conflicting candidates to the user.

    Manufacturer is intentionally **not** restricted: a key that collides
    across vendors is just as ambiguous as one that collides within a vendor,
    and the underlying index ignores manufacturer too.

    There is no companion ``get_module_type_mapping_ambiguities`` helper: see
    the note in ``get_module_types_indexed``. ``ModuleTypeMapping``'s
    conditional UniqueConstraint pair (``unique_module_type_mapping`` and
    ``unique_module_type_mapping_global``) makes mapping-vs-mapping
    collisions structurally impossible.

    Returns:
        dict[str, list]: Ambiguous lookup keys mapped to their candidate ModuleTypes.

    """
    from dcim.models import ModuleType

    candidates: dict = {}
    for mt in ModuleType.objects.all().select_related("manufacturer"):
        seen_this_entry: set = set()
        for key in (mt.model, mt.part_number):
            if not key or key in seen_this_entry:
                continue
            seen_this_entry.add(key)
            candidates.setdefault(key, []).append(mt)
    return {key: mts for key, mts in candidates.items() if len(mts) > 1}


def get_generic_module_types_indexed() -> dict:
    """
    Return NetBox module types from the 'Generic' manufacturer, indexed by model and part_number.

    Used as a secondary fallback in :func:`resolve_module_type` when the primary look-up
    (which excludes ambiguous names) fails.  This lets common SFP/optic models that exist
    under both a vendor-specific and a Generic entry still be matched via the Generic type.

    Mirrors the fail-closed behaviour of :func:`get_module_types_indexed`: if two Generic
    ModuleTypes share the same ``model``/``part_number`` value the key is dropped from the
    index so callers cannot auto-pick an arbitrary row. The user must disambiguate via an
    explicit ``ModuleTypeMapping``.

    Returns:
        dict: Generic module types indexed by unambiguous model and part number values.

    """
    from dcim.models import ModuleType

    result: dict = {}
    ambiguous: set = set()
    for mt in (
        ModuleType.objects.filter(manufacturer__name__iexact="Generic")
        .select_related("manufacturer")
        .prefetch_related("interfacetemplates")
    ):
        seen_this_entry: set = set()
        for key in (mt.model, mt.part_number):
            if not key or key in seen_this_entry or key in ambiguous:
                continue
            seen_this_entry.add(key)
            if key in result:
                ambiguous.add(key)
                del result[key]
            else:
                result[key] = mt
    return result


def preload_normalization_rules(scope: str, manufacturer=None) -> dict:
    """
    Preload NormalizationRule rows for a (scope, manufacturer) combination.

    Returns a dict mapping ``(scope, manufacturer_pk_or_None)`` → list of rules.
    Pass this dict as ``preloaded_rules`` to :func:`apply_normalization_rules` and
    :func:`resolve_module_type` to avoid repeated DB queries inside loops.

    Args:
        scope (str): Normalization rule scope to preload.
        manufacturer (Manufacturer | None): Optional manufacturer whose scoped rules are loaded first.

    Returns:
        dict: Rule lists indexed by scope and optional manufacturer primary key.

    """
    from netbox_librenms_plugin.models import NormalizationRule

    cache: dict = {}
    if manufacturer and manufacturer.pk is not None:
        mfg_pk = manufacturer.pk
        cache[(scope, mfg_pk)] = list(
            NormalizationRule.objects.filter(scope=scope, manufacturer=manufacturer).order_by("priority", "pk")
        )
    cache[(scope, None)] = list(
        NormalizationRule.objects.filter(scope=scope, manufacturer__isnull=True).order_by("priority", "pk")
    )
    return cache


def apply_normalization_rules(value: str, scope: str, manufacturer=None, *, preloaded_rules: dict | None = None) -> str:
    """
    Apply NormalizationRule chain to transform a string before matching.

    Rules for the given scope are applied in priority order.  Each rule's
    regex substitution transforms the output of the previous rule, forming
    a pipeline.  If no rules match, the original value is returned unchanged.

    When *manufacturer* is given, manufacturer-scoped rules run first,
    followed by unscoped (``manufacturer__isnull=True``) rules.  When
    *manufacturer* is ``None``, only unscoped rules are applied.

    Args:
        value:  The raw string to normalize (e.g. '3HE16474AARA01').
        scope:  One of NormalizationRule.SCOPE_* constants.
        manufacturer:  Optional Manufacturer instance to scope rules.
        preloaded_rules:  Optional dict from :func:`preload_normalization_rules`.
            When provided, DB queries are skipped and preloaded lists are used
            instead, eliminating repeated queries inside loops.

    Returns:
        The normalized string after all matching rules have been applied.

    """
    from netbox_librenms_plugin.models import NormalizationRule

    if not value:
        return value

    def _apply_rules(val, rules_qs):
        for rule in rules_qs:
            try:
                val = re.sub(rule.match_pattern, rule.replacement, val)
            except (*REGEX_COMPILE_ERRORS, IndexError):
                logger.error(
                    "Invalid regex in NormalizationRule pk=%s pattern=%r — skipping", rule.pk, rule.match_pattern
                )
        return val

    if preloaded_rules is not None:
        if manufacturer and manufacturer.pk is not None:
            mfg_pk = manufacturer.pk
            if (scope, mfg_pk) in preloaded_rules:
                value = _apply_rules(value, preloaded_rules[(scope, mfg_pk)])
            else:
                # Manufacturer key missing from preloaded dict — fall back to DB and cache result
                rules = list(
                    NormalizationRule.objects.filter(scope=scope, manufacturer=manufacturer).order_by("priority", "pk")
                )
                preloaded_rules[(scope, mfg_pk)] = rules
                value = _apply_rules(value, rules)
        if (scope, None) in preloaded_rules:
            value = _apply_rules(value, preloaded_rules[(scope, None)])
        else:
            # Unscoped rules not preloaded — fall back to DB and cache result
            unscoped_rules = list(
                NormalizationRule.objects.filter(scope=scope, manufacturer__isnull=True).order_by("priority", "pk")
            )
            preloaded_rules[(scope, None)] = unscoped_rules
            value = _apply_rules(value, unscoped_rules)
    elif manufacturer and manufacturer.pk is not None:
        # Manufacturer-specific rules first, then unscoped rules
        for mfg_filter in [{"manufacturer": manufacturer}, {"manufacturer__isnull": True}]:
            rules = NormalizationRule.objects.filter(scope=scope, **mfg_filter).order_by("priority", "pk")
            value = _apply_rules(value, rules)
    else:
        rules = NormalizationRule.objects.filter(scope=scope, manufacturer__isnull=True).order_by("priority", "pk")
        value = _apply_rules(value, rules)
    return value


def _probe_module_type_name(name, module_types, *, manufacturer, norm_rules):
    """Run one candidate name through the scoped, global, normalized and generic look-ups."""
    mfr_mappings = getattr(module_types, "mfr_mappings", None)
    generic_fallback = getattr(module_types, "generic", None)
    mfr_pk = getattr(manufacturer, "pk", None)

    def _lookup_mfr(candidate):
        if mfr_mappings and mfr_pk is not None and candidate:
            return mfr_mappings.get((mfr_pk, candidate))
        return None

    def _normalized():
        return apply_normalization_rules(name, "module_type", manufacturer=manufacturer, preloaded_rules=norm_rules)

    matched = _lookup_mfr(name) or module_types.get(name)
    normalized = None
    if not matched:
        normalized = _normalized()
        if normalized != name:
            matched = _lookup_mfr(normalized) or module_types.get(normalized)
    if not matched and generic_fallback:
        matched = generic_fallback.get(name)
        if not matched:
            if normalized is None:
                normalized = _normalized()
            if normalized != name:
                matched = generic_fallback.get(normalized)
    return matched


def module_type_lookup_candidates(item):
    """
    Return the ordered names one inventory row may be matched by, placeholders removed.

    The model leads. ``entPhysicalDescr`` follows because it carries the real part number when
    the vendor reports a placeholder model, and because ``_merge_transceiver_data`` stores the
    transceiver API's type there on a synthetic row. Every call site reads this one definition,
    so the fallback order cannot differ between the table and the install path.

    Args:
        item (dict): One LibreNMS inventory row.

    Returns:
        list[str]: Usable lookup names, most specific first, without duplicates.

    """
    candidates = []
    for key in ("entPhysicalModelName", "entPhysicalDescr"):
        value = normalize_serial(item.get(key))
        if not is_librenms_placeholder(value) and value not in candidates:
            candidates.append(value)
    return candidates


def resolve_module_type(
    model_name: str,
    module_types: dict,
    manufacturer=None,
    *,
    norm_rules: dict | None = None,
    fallback_names: tuple | list = (),
):
    """
    Resolve a LibreNMS model name to a NetBox ModuleType via direct lookup then normalization.

    Pass ``norm_rules`` (from :func:`preload_normalization_rules`) to avoid
    repeated DB queries when called in a loop.

    When *module_types* is the ``_ModuleTypeIndex`` from :func:`get_module_types_indexed`, its
    ``generic`` index gives a secondary look-up in the 'Generic' manufacturer when the primary
    index has no match (e.g. because the same model name is used by multiple manufacturers,
    making it ambiguous in the primary index).

    When *manufacturer* is provided **and** *module_types* is the ``_ModuleTypeIndex``
    returned by :func:`get_module_types_indexed`, manufacturer-scoped
    ``ModuleTypeMapping`` rows are probed first (raw and normalized model name)
    before falling back to the global index. This lets vendor-specific overrides
    win without polluting lookups for other vendors.

    Args:
        model_name (str): LibreNMS model name to resolve.
        module_types (dict): Primary module type lookup index.
        manufacturer (Manufacturer | None): Optional manufacturer for scoped rules and mappings.
        norm_rules (dict | None): Preloaded normalization rules that avoid repeated database queries.
        fallback_names (tuple | list): Names to try, in order, when *model_name* is a placeholder.
            They are ignored when it is a real model. :func:`module_type_lookup_candidates` builds
            these from a row so every call site tries the same names in the same order.

    Returns:
        ModuleType | None: Matched ModuleType, or ``None`` when no lookup path matches.

    """
    # A placeholder is absent data, not a key: matching on it would let one mapping row answer
    # for every SFP the vendor declined to identify. The fallbacks apply only then. A real model
    # that resolves to nothing stays unresolved, because matching it on its own description
    # would be a guess, and a ModuleTypeMapping row is the supported way to teach that name.
    if is_librenms_placeholder(model_name):
        candidates = []
        for name in fallback_names:
            if not is_librenms_placeholder(name) and name not in candidates:
                candidates.append(name)
    else:
        candidates = [model_name]

    for name in candidates:
        matched = _probe_module_type_name(
            name,
            module_types,
            manufacturer=manufacturer,
            norm_rules=norm_rules,
        )
        if matched:
            return matched
    return None


def slashless_route_aliases(patterns):
    """
    Return an alias for each ``path()`` route in *patterns*, without its trailing slash.

    NetBox runs with ``APPEND_SLASH``, so a request that reaches Django with its trailing slash
    already removed is answered with a 301 back to the slashed form. Anything in front of NetBox
    that normalises the slash away strips it again on the retry, and the browser stops at
    ERR_TOO_MANY_REDIRECTS. An XHR shows that as a control that does nothing, with no error to
    read, and a redirected POST loses its body.

    Serving both forms answers such a request instead of bouncing it. Aliases carry no name, so
    ``reverse()`` keeps returning the canonical slashed URL that templates and tables render.
    Regex routes are skipped: a DRF router builds those, and its clients follow redirects.
    """
    from django.urls import URLPattern, path
    from django.urls.resolvers import RoutePattern

    routes = {str(entry.pattern) for entry in patterns if isinstance(entry, URLPattern)}
    aliases = []
    for entry in patterns:
        if not isinstance(entry, URLPattern) or not isinstance(entry.pattern, RoutePattern):
            continue
        route = str(entry.pattern)
        if not route.endswith("/") or route[:-1] in routes:
            continue
        aliases.append(path(route[:-1], entry.callback, kwargs=entry.default_args))
    return aliases


def load_bay_mappings() -> tuple:
    """
    Load all ModuleBayMapping rows, split into exact and regex lists.

    Returns:
        (exact_mappings, regex_mappings) tuple of lists.

    """
    from netbox_librenms_plugin.models import ModuleBayMapping

    # Regex precedence is first-match-wins, so it must NOT depend on the
    # lexicographic sort of the pattern text — that would let a pattern edit
    # silently reorder vendor-specific vs fallback regexes.  Preserve insertion
    # order within each (exact, regex) bucket by ordering on the PK only.
    all_mappings = list(ModuleBayMapping.objects.order_by("is_regex", "id"))
    exact = [m for m in all_mappings if not m.is_regex]
    regex = [m for m in all_mappings if m.is_regex]
    return exact, regex


def effective_vlan_mode(vlan_data):
    """Return the NetBox mode implied by the reported mode and VLAN assignment."""
    if vlan_data.get("tagged_vlans"):
        return "tagged"
    # The reported mode is only authoritative for a port that also carries VLAN data.
    if not vlan_data.get("untagged_vlan"):
        return None
    return "tagged" if vlan_data.get("mode") == "tagged" else "access"


def _row_vlan_shape(port):
    """Return one row's VLAN assignment as a hashable, comparable ``(untagged, tagged)`` pair."""
    return port.get("untagged_vlan"), tuple(sorted(port.get("tagged_vlans") or []))


def _copy_row_vlan_data(source, target, interface_name_field):
    """Copy one row's VLAN assignment onto another and record where it came from."""
    untagged, tagged = _row_vlan_shape(source)
    target["mode"] = source.get("mode")
    target["untagged_vlan"] = untagged
    target["tagged_vlans"] = list(tagged)
    target["vlan_inherited_from"] = source.get(interface_name_field) or source.get("ifName") or ""


def apply_lag_vlan_fill(ports, lag_members, *, interface_name_field="ifName"):
    """
    Fill missing VLAN data between a LAG aggregate and its members, in place.

    LibreNMS reports the VLANs on whichever side of the aggregation the platform exposes:
    Juniper on the aggregate, several switch platforms on the members. Filling only the rows
    that have none keeps the rule vendor-neutral, because it can complete a picture but never
    correct one. A filled row carries ``vlan_inherited_from`` naming the row it copied.

    Args:
        ports: The enriched LibreNMS port rows, mutated in place.
        lag_members (dict): ``{member_port_id: aggregate_port_id}``.
        interface_name_field (str): The row field naming the interface ('ifName' or 'ifDescr').

    Returns:
        None

    """
    if not lag_members:
        return

    rows_by_port_id = {}
    for port in ports:
        port_id = normalize_librenms_port_id(port.get("port_id"))
        if port_id is not None:
            rows_by_port_id.setdefault(port_id, port)

    # Snapshot before writing anything: both directions read the original state, so a row this
    # call fills can never become the source of a second fill.
    carries_own = {port_id: _row_vlan_shape(port) != (None, ()) for port_id, port in rows_by_port_id.items()}

    members_by_aggregate = {}
    for raw_member_id, raw_aggregate_id in lag_members.items():
        member_id = normalize_librenms_port_id(raw_member_id)
        aggregate_id = normalize_librenms_port_id(raw_aggregate_id)
        if member_id is None or aggregate_id is None or member_id == aggregate_id:
            continue
        members_by_aggregate.setdefault(aggregate_id, []).append(member_id)

    for aggregate_id, member_ids in members_by_aggregate.items():
        aggregate = rows_by_port_id.get(aggregate_id)
        if aggregate is None:
            continue
        member_ids = sorted(member_ids)
        if carries_own.get(aggregate_id):
            for member_id in member_ids:
                member = rows_by_port_id.get(member_id)
                if member is not None and not carries_own.get(member_id):
                    _copy_row_vlan_data(aggregate, member, interface_name_field)
            continue
        member_rows = [rows_by_port_id.get(member_id) for member_id in member_ids]
        # Rolling up the subset that happens to have data would invent an assignment for the
        # members that have none, so every member must agree on one set.
        if any(row is None for row in member_rows) or not all(carries_own.get(mid) for mid in member_ids):
            continue
        if len({(_row_vlan_shape(row), effective_vlan_mode(row)) for row in member_rows}) != 1:
            continue
        _copy_row_vlan_data(member_rows[0], aggregate, interface_name_field)
