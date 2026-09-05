import hashlib
import logging
import re
import threading
from copy import deepcopy
from typing import Optional

import netaddr
from dcim.models import Device, Interface
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Count, Max, Q
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject
from django.utils.html import escape
from django.utils.safestring import mark_safe
from netbox.config import get_config
from netbox.plugins import get_plugin_config
from utilities.paginator import get_paginate_count as netbox_get_paginate_count

from netbox_librenms_plugin.constants import (
    DEFAULT_INTERFACE_NAME_FIELD,
    OOB_BADGE_HTML,
    is_supported_interface_name_field,
)
from netbox_librenms_plugin.ip_addressing import parse_host_address

logger = logging.getLogger(__name__)

# Bounded at 19 digits, the width of a PostgreSQL bigint. Without the bound an oversized string is
# rejected only by CPython's int_max_str_digits limit, which a host may raise or disable.
_ASCII_POSITIVE_INTEGER_RE = re.compile(r"^[ \t\r\n\f\v]*\+?[0-9]{1,19}[ \t\r\n\f\v]*$")


def acquire_advisory_transaction_lock(lock_identity: str, *, using: str | None = None) -> None:
    """Acquire one stable PostgreSQL advisory lock for the current transaction.

    Args:
        lock_identity: Stable string hashed into the advisory lock key.
        using: Database alias that owns the transaction. Defaults to the default alias.
    """
    # Resolve the alias the caller's transaction.atomic(using=...) opened; the default
    # connection can be in autocommit, where the lock would release immediately.
    from django.db import DEFAULT_DB_ALIAS, connections

    connection = connections[using or DEFAULT_DB_ALIAS]

    if not connection.in_atomic_block:
        raise RuntimeError("acquire_advisory_transaction_lock() requires an open transaction")
    lock_key = int.from_bytes(
        hashlib.blake2b(lock_identity.encode(), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])


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


_VC_MEMBER_INTERFACE_PATTERN = re.compile(r"^(?P<prefix>[A-Za-z][A-Za-z0-9]*)(?P<member>\d+)(?P<suffix>[/:].+)$")


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


def normalize_librenms_port_id(value) -> int | None:
    """Normalize a LibreNMS port_id to a positive integer, or None."""
    return coerce_librenms_id(value)


def interface_name_fallback_matches_port(interface, port_id, server_key) -> bool:
    """Return true when a same-name interface is unbound or owns the requested port ID."""
    requested_id = normalize_librenms_port_id(port_id)
    if requested_id is None:
        return False

    raw_mapping = interface.custom_field_data.get("librenms_id")
    # Only the "no binding recorded" rules stay local; get_librenms_device_id owns every
    # stored shape, so the two readers cannot drift on which ones resolve.
    if raw_mapping is None:
        return True
    if isinstance(raw_mapping, dict) and server_key not in raw_mapping:
        return True

    return get_librenms_device_id(interface, server_key, auto_save=False) == requested_id


def get_interface_port_identity_sets(ports, interface_name_field) -> tuple[set[int], set[int]]:
    """Return host IDs that are unique and the subset with a unique selected display name."""
    if not is_list_of_dicts(ports):
        return set(), set()

    port_id_counts = {}
    port_names = {}
    name_counts = {}
    for port in ports:
        if port.get("_source") == "oob":
            continue
        port_id = normalize_librenms_port_id(port.get("port_id"))
        interface_name = port.get(interface_name_field)
        if port_id is None:
            continue
        port_id_counts[port_id] = port_id_counts.get(port_id, 0) + 1
        port_names[port_id] = interface_name
        if isinstance(interface_name, str) and interface_name.strip():
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
    except re.error as exc:
        raise ValidationError({field_name: f"Invalid regex: {exc}"}) from exc


def normalize_relationship_maps(relationships) -> tuple[dict, dict]:
    """
    Normalize a cached ``port_stack_relationships`` mapping into int-keyed relationship maps.

    Returns ``(lag_members, sub_interfaces)``. The single home for the corruption guard + key
    normalization shared by :func:`build_relationship_maps` and the bulk sync writer, so the
    readers and writer cannot drift.

    Fails soft against a corrupt / partial-write / format-migrated cache: a None or non-dict
    ``relationships`` (e.g. a list) — or a present-but-None / non-dict nested ``lag_members`` /
    ``sub_interfaces`` — collapses to ``{}`` so ``.items()`` never raises ``AttributeError``. Keys
    and values are normalized via :func:`normalize_librenms_port_id` so int-keyed lookups never miss
    stringified JSON values. An edge is dropped unless both endpoint IDs are valid.
    """
    if not isinstance(relationships, dict):
        relationships = {}
    lag_members_raw = relationships.get("lag_members")
    if not isinstance(lag_members_raw, dict):
        lag_members_raw = {}
    sub_interfaces_raw = relationships.get("sub_interfaces")
    if not isinstance(sub_interfaces_raw, dict):
        sub_interfaces_raw = {}

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
    return lag_members, sub_interfaces


def get_virtual_chassis_member(
    device: Device,
    port_name: str,
    members_by_position: dict | None = None,
    *,
    return_device_on_failure: bool = True,
) -> Device | None:
    """
    Determines the likely virtual chassis member based on the device's vc_position and port name.

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
    except (re.error, ValueError, ObjectDoesNotExist):
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
    """Resolve a LibreNMS interface row to its likely Virtual Chassis member.

    Prefer a unique interface already bound to the stable LibreNMS port ID. For an unbound
    physical Ethernet row, use the member position encoded in the interface name. Do not apply
    that heuristic to logical rows or sub-interfaces because their number is not reliably a
    chassis position. Set ``return_device_on_failure`` to false when a writer must reject a row
    whose owner has no stable evidence.
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
    if isinstance(own_position, int) and own_position > 0:
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
        if parsed > 0:
            positions.add(parsed)

    return positions


def rewrite_interface_name_for_vc_member(
    interface_name: str, vc_position: int, member_positions: set[int] | None = None
) -> str | None:
    """Rewrite a template/interface name to the selected VC member position when appropriate."""
    if not interface_name or not isinstance(vc_position, int) or vc_position < 1:
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


def get_module_template_interface_names(device: Device, module) -> list[str]:
    """Return unique instantiated interface-template names, rewritten for VC members when needed."""
    if device is None:
        return []

    template_manager = getattr(getattr(module, "module_type", None), "interfacetemplates", None)
    if template_manager is None or not hasattr(template_manager, "all"):
        return []

    vc_position = getattr(device, "vc_position", None)
    vc_id = getattr(device, "virtual_chassis_id", None)
    member_positions = None
    if isinstance(vc_position, int) and vc_position > 0 and isinstance(vc_id, int):
        member_positions = get_vc_member_positions(device)

    template_names = []
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

        if name not in template_names:
            template_names.append(name)

    from netbox_librenms_plugin.signals import predict_module_interface_names

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

    Same receiver semantics as :func:`get_module_template_interface_names`
    (``send_robust`` isolates a failing receiver and the last non-None list return
    wins), but the input is a caller-supplied list of names rather than template
    instantiations. Used to recognise when a standalone interface is the renamed
    twin of a module's raw interface (the rename a naming plugin would apply but
    skipped because the target name was already taken).
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
    """Return a diagnostic dict when VC member-name rewriting would no-op for this module.

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
    """
    vc_position = getattr(device, "vc_position", None)
    vc_id = getattr(device, "virtual_chassis_id", None)
    # bool is a subclass of int; reject explicitly so True/False can't masquerade.
    if isinstance(vc_position, bool) or isinstance(vc_id, bool):
        return None
    if not (isinstance(vc_position, int) and vc_position > 0 and isinstance(vc_id, int)):
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
    """Render a VC-normalization no-op diagnostic as a markdown blob for a GitHub issue.

    Catalog identifiers (manufacturer/device type/module type/bay name) get an
    "(optional, you can remove this line)" suffix so users who treat their HW
    inventory as confidential can strip them before pasting.
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


def get_librenms_sync_device(device: Device, server_key: str = None) -> Optional[Device]:
    """
    Determine which Virtual Chassis member should handle LibreNMS sync operations.

    LibreNMS treats a Virtual Chassis as a single logical device, so only one member
    should have the librenms_id custom field set and be used for sync operations.

    Priority order for selecting the sync device:
    1. Any member with librenms_id custom field set for *server_key* (highest priority).
       When *server_key* is None, matches any member that has any librenms_id set.
    2. Master device with primary IP (if master is designated)
    3. Any member with primary IP (fallback when no master or master lacks IP)
    4. Member with lowest vc_position (for error messages when no IPs configured)

    Args:
        device (Device): Any device in the virtual chassis.
        server_key: LibreNMS server key used to resolve the correct librenms_id mapping.
                    Pass None to match any member that has any librenms_id (e.g. in
                    contexts where the active server is not known, such as table columns).

    Returns:
        Optional[Device]: The device that should handle LibreNMS sync, or None if
                         the device is not in a virtual chassis.
    """
    if not hasattr(device, "virtual_chassis") or not device.virtual_chassis:
        return device

    vc = device.virtual_chassis
    all_members = vc.members.all()

    def _host_id_valid(val):
        # A real host-side LibreNMS id: a bare int/string-digit, or the "id" of a per-server
        # dict ({"id": 42, ...}). coerce_librenms_id mirrors the int/string rules (rejects
        # bools and floats like 1.0).
        if val is None or isinstance(val, bool):
            return False
        if isinstance(val, dict):
            return coerce_librenms_id(val.get("id")) is not None
        return coerce_librenms_id(val) is not None

    def _oob_id_valid(val):
        # An OOB-only linkage: a per-server dict carrying {"oob": {"id": 7, ...}}. set_librenms_oob()
        # can persist this before a host id exists; it's a real linkage but weaker than a host id.
        if not isinstance(val, dict):
            return False
        oob = val.get("oob")
        return isinstance(oob, dict) and coerce_librenms_id(oob.get("id")) is not None

    if server_key is not None:
        # Priority 1: a member with a real host id for server_key (a migrated dict mapping is
        # preferred over a legacy bare-int below).
        for member in all_members:
            raw_cf = member.cf.get("librenms_id")
            if isinstance(raw_cf, dict) and _host_id_valid(raw_cf.get(server_key)):
                return member

        # Priority 1b (legacy fallback): any member whose host id resolves for this server
        # (includes bare-int legacy IDs that are a universal fallback).
        for member in all_members:
            if get_librenms_device_id(member, server_key, auto_save=False):
                return member

        # Priority 1c: OOB-only mapping for this server — a member linked only as an OOB
        # controller. Evaluated LAST so a member holding the real host id always wins; without
        # this pass an OOB-only member would fall through to the master/primary-IP fallback.
        for member in all_members:
            raw_cf = member.cf.get("librenms_id")
            if isinstance(raw_cf, dict) and _oob_id_valid(raw_cf.get(server_key)):
                return member
    else:
        # server_key is None (e.g. table columns without an active server): prefer a member
        # with any host id on any server, then fall back to any OOB-only linkage.
        for member in all_members:
            raw_cf = member.cf.get("librenms_id")
            if isinstance(raw_cf, dict):
                if any(_host_id_valid(v) for v in raw_cf.values()):
                    return member
            elif _host_id_valid(raw_cf):
                return member
        for member in all_members:
            raw_cf = member.cf.get("librenms_id")
            if isinstance(raw_cf, dict) and any(_oob_id_valid(v) for v in raw_cf.values()):
                return member

    # Priority 2: Use master device if it has primary IP
    if vc.master and vc.master.primary_ip:
        return vc.master

    # Priority 3: Find any member with primary IP
    for member in all_members:
        if member.primary_ip:
            return member

    # Priority 4: Use member with lowest vc_position as fallback
    try:
        return min(all_members, key=lambda m: m.vc_position, default=None)
    except (ValueError, TypeError):
        return None


def get_table_paginate_count(request: HttpRequest, table_prefix: str) -> int:
    """
    Extends Netbox pagination to support multiple tables by using table-specific prefixes

    Args:
        request: HTTP request object
        table_prefix: Prefix for the table

    Returns:
        int: Number of items to display per page
    """
    config = get_config()
    # Check GET first, then POST (HTMX refresh requests send pagination via POST body)
    param_key = f"{table_prefix}per_page"
    param_value = request.GET.get(param_key) or request.POST.get(param_key)
    if param_value:
        try:
            per_page = int(param_value)
            # Guard against 0/negative input, which would break the paginator.
            if per_page < 1:
                return netbox_get_paginate_count(request)
            max_page_size = config.MAX_PAGE_SIZE
            # MAX_PAGE_SIZE 0/None disables the NetBox ceiling; don't clamp to it (min() with 0
            # would zero the page size, and with None it TypeErrors).
            return min(per_page, max_page_size) if max_page_size else per_page
        except ValueError:
            pass

    return netbox_get_paginate_count(request)


def get_user_pref(request, path, default=None):
    """Get a user preference value via request.user.config."""
    if hasattr(request, "user") and hasattr(request.user, "config"):
        return request.user.config.get(path, default)
    return default


def save_user_pref(request, path, value):
    """
    Save a user preference value via request.user.config.

    NetBox seeds a new UserConfig with ``settings.DEFAULT_USER_PREFERENCES`` **by reference**
    (users/signals.py) and ``UserConfig.set()`` edits ``data`` in place, so writing straight
    through would mutate that process-wide dict: this user's choice would then become the
    default every later-created user inherits, and the fallback every preference-less user
    reads. Copy the row's data first so the write stays with this user.
    """
    if hasattr(request, "user") and hasattr(request.user, "config"):
        user_config = request.user.config
        user_config.data = deepcopy(user_config.data)
        try:
            user_config.set(path, value, commit=True)
        except (TypeError, ValueError):
            pass


_TRUTHY_PARAMETER_VALUES = frozenset({"on", "true", "1"})


def is_truthy_parameter(value) -> bool:
    """Return True when a request parameter carries one of the accepted truthy spellings."""
    return value.lower() in _TRUTHY_PARAMETER_VALUES if isinstance(value, str) else False


def read_request_toggle(request, keys):
    """Return the first key's POST value, else its GET value, else None.

    One reader for every POST-then-GET toggle in this module, so the accepted vocabulary
    cannot drift between them.
    """
    post_value = next((request.POST.get(key) for key in keys if key in request.POST), None)
    if post_value is not None:
        return post_value
    return next((request.GET.get(key) for key in keys if key in request.GET), None)


def resolve_naming_preferences(request) -> tuple[bool, bool]:
    """Resolve use_sysname/strip_domain: POST/GET toggle → user pref → plugin settings.

    This is the single source of truth for naming preference resolution,
    used by the import page, sync page, and sync action views.

    Returns:
        (use_sysname, strip_domain) booleans.
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
    """True if two address strings refer to the same host IP (version-aware)."""
    try:
        return parse_host_address(a) == parse_host_address(b)
    except ValueError:
        return False


def resolve_create_missing_interfaces(request) -> bool:
    """Resolve the "create a missing NetBox interface before assigning the IP" flag.

    POST wins, then GET, then ``False`` (opt-in). The IP-sync template renders the toggle
    from this value, so a table refresh restores what the user selected instead of
    silently reverting to off.
    """
    value = read_request_toggle(request, ("create-missing-interfaces-toggle", "create_missing_interfaces"))
    return is_truthy_parameter(value) if value is not None else False


def resolve_set_primary_ip(request) -> bool:
    """Resolve the "set Primary IP from the LibreNMS management IP" flag.

    Cascade (mirrors :func:`resolve_naming_preferences`, minus a settings
    default since this is a per-sync action choice):

    1. POST/GET ``set-primary-ip-toggle`` (or ``set_primary_ip``) wins
       -- set by the IP-sync tab toggle.
    2. Otherwise the user's saved preference
       ``plugins.netbox_librenms_plugin.set_primary_ip``.
    3. Otherwise ``False`` (opt-in).

    When enabled, :class:`SyncIPAddressesView` sets ``primary_ip4``/``primary_ip6``
    on the device/VM for the synced IP that matches the LibreNMS management IP,
    provided that IP ends up assigned to one of the object's interfaces.
    """
    value = read_request_toggle(request, ("set-primary-ip-toggle", "set_primary_ip-toggle", "set_primary_ip"))
    if value is not None:
        return is_truthy_parameter(value)

    pref = get_user_pref(request, "plugins.netbox_librenms_plugin.set_primary_ip")
    if pref is not None:
        return is_truthy_parameter(pref) if isinstance(pref, str) else bool(pref)

    return False


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
    """Return the column length NetBox declares for one interface field.

    Read from the model's own field so the bound cannot drift, the same way
    :func:`coerce_interface_mtu` reads ``INTERFACE_MTU_MIN``/``MAX``. Pass the concrete
    writer model rather than trusting Interface and VMInterface to stay equal.
    """
    return (model or Interface)._meta.get_field(field_name).max_length


def syncable_interface_name(port, interface_name_field, model=None):
    """Return the LibreNMS name usable as a NetBox interface name, or ``None``.

    ``save()`` runs no validators and Django never truncates a CharField, so an over-long name
    reaches Postgres as a ``DataError``. Truncating instead would collide on the
    ``(device, name)`` unique constraint, so an unstorable name makes the row unsyncable.
    """
    name = port.get(interface_name_field)
    if not isinstance(name, str) or not name.strip():
        return None
    if len(name) > interface_field_limit("name", model):
        return None
    return name


def interface_name_rejection_reason(port, interface_name_field, model=None):
    """Return why the LibreNMS name cannot be synced, or ``None`` when it can.

    The two cases read very differently to a user, so the caller reports them apart rather
    than telling someone their 80-character interface name is blank.
    """
    name = port.get(interface_name_field)
    if not isinstance(name, str) or not name.strip():
        return "interface name is blank"
    limit = interface_field_limit("name", model)
    if len(name) > limit:
        return f"interface name is longer than the {limit} characters NetBox stores"
    return None


def bounded_interface_text(field_name, value, model=None):
    """Return *value* clipped to the column NetBox declares for *field_name*.

    Refuses ``name``: an over-long name has to make the row unsyncable, because a truncated
    one collides with its siblings on the ``(device, name)`` unique constraint.
    """
    if field_name == "name":
        raise ValueError("An interface name must not be truncated; use syncable_interface_name().")
    limit = interface_field_limit(field_name, model)
    return value[:limit] if limit is not None and len(value) > limit else value


def coerce_interface_mtu(value) -> int | None:
    """Return an MTU inside NetBox's accepted range, or None.

    ``save()`` does not run field validators, so an out-of-range or non-numeric ``ifMtu`` from
    LibreNMS would otherwise reach the column unchecked. NetBox pins the bounds in
    ``dcim.constants``; they are read from there so the two cannot drift.
    """
    from dcim.constants import INTERFACE_MTU_MAX, INTERFACE_MTU_MIN

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        mtu = value
    elif isinstance(value, str):
        if not _ASCII_POSITIVE_INTEGER_RE.fullmatch(value):
            return None
        try:
            mtu = int(value)
        except ValueError:
            return None
    else:
        return None
    return mtu if INTERFACE_MTU_MIN <= mtu <= INTERFACE_MTU_MAX else None


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
    # NormalizationRule scope before the DeviceTypeMapping lookup (docs/usage_tips/
    # mapping_rules.md: "normalizes LibreNMS hardware string before DeviceTypeMapping
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


def find_matching_site(librenms_location: str) -> dict:
    """
    Find exact matching NetBox site for a LibreNMS location.

    Only performs exact name matching (case-insensitive).

    Args:
        librenms_location (str): Location string from LibreNMS

    Returns:
        dict: Dictionary containing:
            - found (bool): Whether a match was found
            - site (Site|None): The matched Site object
            - match_type (str|None): Always 'exact' if found, None otherwise
            - confidence (float): Always 1.0 if found, 0.0 otherwise
    """
    from dcim.models import Site

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

    return {"found": False, "site": None, "match_type": None, "confidence": 0.0}


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


def normalize_inventory_serial(value, manufacturer=None, preloaded_rules=None) -> str:
    """Trim a LibreNMS serial, then apply the serial-scope NormalizationRule chain.

    Vendors decorate the ENTITY-MIB serial: Juniper reports "S/N BCFB9793". Keeping that
    transformation in a rule shows the operator why a stored serial differs from the raw
    inventory, and lets them add the next vendor without a release. Callers keep their own
    placeholder guards, as ``normalize_serial`` does.
    """
    from netbox_librenms_plugin.models import NormalizationRule

    serial = normalize_serial(value)
    if not serial:
        return serial
    return apply_normalization_rules(
        serial, NormalizationRule.SCOPE_SERIAL, manufacturer=manufacturer, preloaded_rules=preloaded_rules
    )


def coerce_librenms_id(value) -> int | None:
    """
    Coerce a raw LibreNMS ID value (int or string-digit) to int, or None.

    Accepts only ``int`` and ``str`` — other types (None, dicts, MagicMocks, etc.)
    return None. Booleans are rejected because ``bool`` is a subclass of ``int`` in
    Python, so ``int(True)`` silently becomes ``1`` — a valid-looking device ID. Zero
    and negative values are also rejected since LibreNMS IDs are strictly positive
    integers.

    Args:
        value: The raw LibreNMS id value to coerce.

    Returns:
        int | None: The positive integer id, or None if it can't be coerced.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        if not _ASCII_POSITIVE_INTEGER_RE.fullmatch(value):
            return None
        try:
            coerced = int(value)
        except ValueError:
            return None
        return coerced if coerced > 0 else None
    return None


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
    return mark_safe(  # noqa: S308 — names escaped above; ids are model pks
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
    if record.get("_source") != "oob":
        return ""
    # Static trusted markup — mark_safe, not format_html (which requires interpolation
    # args and raises TypeError when given a bare string).
    return mark_safe((" " if leading_space else "") + OOB_BADGE_HTML)  # noqa: S308


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


def resolve_server_mapping_display_id(entry) -> tuple[int | None, bool]:
    """
    Resolve the display LibreNMS id for one per-server ``librenms_id`` custom-field entry.

    *entry* is the value stored for a single server key: either a scalar (a legacy bare id) or
    the migrated dict form ``{"id": N, "oob": {"id": M}}``. The host id wins; when it is
    absent/invalid the nested OOB controller id is used instead, because an OOB-only linkage is
    still a real link to that server (the user must be able to see and remove it). All coercion
    goes through :func:`coerce_librenms_id`, so booleans, non-numeric strings and non-positive
    ids are rejected uniformly.

    Args:
        entry: The per-server value from the ``librenms_id`` custom field.

    Returns:
        tuple[int | None, bool]: ``(display_id, is_oob_only)`` — the coerced id to display (or
            None when neither a host nor an OOB id is valid), and whether it came from the OOB
            fallback (host id absent/invalid but ``oob.id`` valid).
    """
    if isinstance(entry, dict):
        host_id = coerce_librenms_id(entry.get("id"))
        if host_id is not None:
            return host_id, False
        oob = entry.get("oob")
        if isinstance(oob, dict):
            oob_id = coerce_librenms_id(oob.get("id"))
            if oob_id is not None:
                return oob_id, True
        return None, False
    return coerce_librenms_id(entry), False


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


def get_librenms_device_id(obj, server_key: str = "default", *, auto_save: bool = True):
    """
    Get the LibreNMS device/port ID for a specific server from the JSON custom field.

    Supports both the legacy integer format and the new multi-server JSON format::

        Legacy:  librenms_id = 42          → returns 42 for any server_key (universal fallback)
        New:     librenms_id = {"primary": 42}  → returns 42 only for server_key="primary"

    If the stored value (or the dict entry for server_key) is a string it is
    normalised to ``int``.  When *auto_save* is ``True`` (the default) the
    normalised value is written back so that subsequent DB queries can use a
    plain integer without defensive ``str()`` casting.  Pass ``auto_save=False``
    in read-only contexts (e.g. table renderers) to avoid triggering unintended
    DB writes or signals.

    Args:
        obj: NetBox object with a ``librenms_id`` custom field.
        server_key: LibreNMS server key (from plugin ``servers`` config).
        auto_save: When True (default), persist any normalised value back to the DB.

    Returns:
        int or None
    """
    cf_value = obj.cf.get("librenms_id")
    if cf_value is None:
        return None
    if isinstance(cf_value, int) and not isinstance(cf_value, bool):
        # Legacy bare integer — universal fallback for any server to ensure
        # devices imported before multi-server support remain discoverable.
        return cf_value if cf_value > 0 else None
    if isinstance(cf_value, str):
        # Someone stored a bare string (e.g., via NetBox UI/API) — normalise to int.
        # Treat as a legacy universal fallback.
        try:
            int_id = int(cf_value)
        except (ValueError, TypeError):
            return None
        if int_id <= 0:
            return None
        if auto_save:
            obj.custom_field_data["librenms_id"] = int_id
            obj.save(update_fields=["custom_field_data"])
        return int_id
    if isinstance(cf_value, dict):
        value = cf_value.get(server_key)
        if isinstance(value, dict):
            # New form: {"id": 42, "oob": {...}} — extract the main device id.
            # coerce_librenms_id centralizes the bool/int/str/positive checks.
            inner = value.get("id")
            int_id = coerce_librenms_id(inner)
            if int_id is None:
                return None
            # Normalise a string-stored id ("42" → 42) back to the DB.
            if auto_save and isinstance(inner, str):
                value["id"] = int_id
                obj.custom_field_data["librenms_id"] = cf_value
                obj.save(update_fields=["custom_field_data"])
            return int_id
        # Bare scalar entry ({"primary": 42} / {"primary": "42"}): coerce_librenms_id
        # rejects bools, non-positive, and non-numeric strings in one place.
        int_id = coerce_librenms_id(value)
        if int_id is None:
            return None
        # Normalise a string-stored id back to the DB so later queries use a plain int.
        if auto_save and isinstance(value, str):
            cf_value[server_key] = int_id
            obj.custom_field_data["librenms_id"] = cf_value
            obj.save(update_fields=["custom_field_data"])
        return int_id
    return None


def set_librenms_device_id(obj, device_id, server_key: str = "default"):
    """
    Set the LibreNMS device/port ID for a specific server on the JSON custom field.

    Does NOT silently migrate legacy bare-integer values to the dict format.
    If the field contains a legacy bare integer (or a string that parses as an integer),
    a warning is logged and the write is skipped; use the migration workflow instead.

    Args:
        obj: NetBox object with a ``librenms_id`` custom field.
        device_id: LibreNMS device ID (integer).
        server_key: LibreNMS server key (from plugin ``servers`` config).
    """
    if isinstance(device_id, bool):
        logger.warning(
            "librenms_id device_id is a boolean (%r) on %r; not storing.",
            device_id,
            obj,
        )
        return
    cf_value = obj.custom_field_data.get("librenms_id") or {}
    if is_legacy_librenms_id(cf_value):
        # Legacy bare int OR its numeric-string form — skip the write so we don't silently migrate.
        logger.warning(
            "librenms_id on %r has legacy bare integer %r; skipping write to prevent "
            "silent migration. Use the migration workflow to convert.",
            obj,
            cf_value,
        )
        return
    elif isinstance(cf_value, str):
        # A non-numeric string is corrupt (is_legacy_librenms_id already excluded numeric ones).
        logger.warning(
            "librenms_id custom field has unexpected string %r on %r; resetting to empty dict.",
            cf_value,
            obj,
        )
        cf_value = {}
    elif not isinstance(cf_value, dict):
        logger.warning(
            "librenms_id custom field has unexpected type %s on %r; resetting to empty dict.",
            type(cf_value).__name__,
            obj,
        )
        cf_value = {}
    # coerce_librenms_id rejects bools, floats (1.9 would otherwise truncate to 1),
    # and non-positive values — only positive ints / numeric strings are stored.
    int_id = coerce_librenms_id(device_id)
    if int_id is None:
        logger.warning(
            "librenms_id device_id %r is not a valid positive integer on %r; not storing.",
            device_id,
            obj,
        )
        return  # Don't persist an invalid entry
    # Preserve any existing OOB sub-object when rewriting the main device id.
    existing_entry = cf_value.get(server_key)
    if isinstance(existing_entry, dict) and "oob" in existing_entry:
        cf_value[server_key] = {"id": int_id, "oob": existing_entry["oob"]}
    else:
        cf_value[server_key] = int_id
    obj.custom_field_data["librenms_id"] = cf_value


class AmbiguousLibreNMSIdError(LookupError):
    """
    Raised when a librenms_id resolves to more than one NetBox object.

    Distinguishes a genuine ambiguity (a data-integrity violation — e.g. two devices
    sharing the same host id, or a host id and a *different* OOB id) from a clean
    miss. Returning ``None`` for both would let callers treat an ambiguous link as
    "not found" and proceed (importing/binding), so :func:`find_by_librenms_id` raises
    this instead and callers fail closed.
    """


def build_librenms_id_qs(server_key, value):
    """
    Build ``(host_q, oob_q)`` Q objects matching every stored form of a librenms_id under server_key.

    Single source of truth for the librenms_id JSON-path coverage shared by
    :func:`find_by_librenms_id` and ``cables_view._librenms_id_q``, so the two can't drift on
    which stored shapes resolve. Matches the namespaced scalar (``{server_key: 42}``), the
    dict-with-id form (``{server_key: {"id": 42}}``), the legacy bare int/str (pre multi-server),
    and the OOB sub-key (``{server_key: {"oob": {"id": 42}}}``), across the value's int and string
    representations (so ``"042"`` / ``" 42 "`` match JSON ``42``).

    Fails closed on an invalid *value* (bool / None / zero / negative / non-numeric string): it
    returns match-nothing predicates rather than building a lookup that could hit a corrupt legacy
    row. Callers may still pre-validate for their own control flow, but no longer have to for safety.

    Args:
        server_key (str): The LibreNMS server key whose JSON sub-key is matched.
        value (int | str): The already-validated LibreNMS id.

    Returns:
        tuple[Q, Q]: ``(host_q, oob_q)`` — host-identity predicates (scalar / ``__id`` / legacy
            bare) and the OOB-controller predicate (``__oob__id``), kept separate so callers can
            fail closed on a host-vs-OOB cross-row collision.
    """
    # Fail closed centrally so every caller is safe: a value that isn't a valid librenms_id
    # (bool / None / zero / negative / non-numeric string like "abc") must never build a predicate
    # that could match a corrupt legacy row (e.g. ``custom_field_data__librenms_id="abc"``). Callers
    # still validate for their own reasons, but this makes the shared builder the last line of
    # defence. coerce_librenms_id() only gates validity here — the variant list below keeps its full
    # match breadth (incl. zero-padded string forms) for accepted values.
    normalized_value = coerce_librenms_id(value)
    if normalized_value is None:
        match_none = Q(pk__in=[])
        return match_none, match_none
    variants = [value, str(value)]
    if isinstance(value, str):
        try:
            int_value = int(value.strip())
        except ValueError:
            int_value = None
        if int_value is not None and int_value > 0:
            variants += [str(int_value), int_value]
    seen = []
    for v in variants:
        if v not in seen:
            seen.append(v)

    host_q = Q()
    oob_q = Q()
    for v in seen:
        # Namespaced scalar, the dict-with-id form ({"id": .., "oob": {..}}), and the legacy bare
        # integer/string (pre multi-server) all identify the HOST device.
        host_q |= Q(**{f"custom_field_data__librenms_id__{server_key}": v})
        host_q |= Q(**{f"custom_field_data__librenms_id__{server_key}__id": v})
        host_q |= Q(custom_field_data__librenms_id=v)
        # The OOB controller's own device id — so a re-import recognises the merged device.
        oob_q |= Q(**{f"custom_field_data__librenms_id__{server_key}__oob__id": v})

    # Every reader accepts numeric strings with leading zeroes, an optional plus sign, and
    # surrounding whitespace. Exact JSON comparisons cannot find those forms when the caller
    # supplies the canonical integer. Match the text extracted from each supported JSON shape so
    # indexed candidate lookups and full in-memory scans enforce the same normalized-ID contract.
    numeric_pattern = rf"^[ \t\r\n\f\v]*\+?0*{normalized_value}[ \t\r\n\f\v]*$"
    host_q |= Q(**{f"custom_field_data__librenms_id__{server_key}__regex": numeric_pattern})
    host_q |= Q(**{f"custom_field_data__librenms_id__{server_key}__id__regex": numeric_pattern})
    host_q |= Q(custom_field_data__librenms_id__regex=numeric_pattern)
    oob_q |= Q(**{f"custom_field_data__librenms_id__{server_key}__oob__id__regex": numeric_pattern})
    return host_q, oob_q


def find_by_librenms_id(model, librenms_id, server_key: str = "default", *, select_for_update: bool = False):
    """
    Return the first object of *model* whose ``librenms_id`` JSON field contains
    *librenms_id* under *server_key*.

    Raises :class:`AmbiguousLibreNMSIdError` when the id resolves to more than one
    distinct object (duplicate host-only, duplicate OOB-only, or host vs. a different
    OOB match); callers must fail closed rather than treating it as a miss.

    Also matches legacy records stored as a bare ``librenms_id`` integer or string
    in ``custom_field_data``—these predate multi-server support and act as a
    universal fallback for any *server_key*.

    Args:
        model: A Django model class (Device, VirtualMachine, Interface, …).
        librenms_id: The LibreNMS device/port ID to look up.
        server_key: LibreNMS server key (from plugin ``servers`` config).
        select_for_update (bool): When True, lock the matched row(s) with
            ``SELECT … FOR UPDATE`` so a concurrent conflict check serializes against
            an existing owner. Must be called inside a transaction; best-effort like
            the serial guard (a row that does not yet exist cannot be locked).

    Returns:
        Model instance or None
    """
    if librenms_id is None:
        return None
    if isinstance(librenms_id, bool):
        return None
    # Reject floats and arbitrary non-scalar objects before they reach _id_variants()
    # and the ORM predicates: only int/str representations honour the int-only contract
    # enforced by coerce_librenms_id() (which a positive float would otherwise bypass).
    if not isinstance(librenms_id, (int, str)):
        return None
    if isinstance(librenms_id, int) and librenms_id <= 0:
        return None
    if isinstance(librenms_id, str):
        cleaned = librenms_id.strip()
        if cleaned == "":
            return None
        try:
            if int(cleaned) <= 0:
                return None
        except ValueError:
            return None

    # Build host-identity and OOB-identity predicates SEPARATELY (via the shared path builder, so
    # this and cables_view._librenms_id_q can't drift on the stored shapes they match). Folding
    # both into one OR + .first() can silently bind to the wrong NetBox object when one row matches
    # by host id and a *different* row matches by OOB id; query each set and fail closed on a
    # cross-row collision rather than trusting model ordering to pick "the" row.
    host_q, oob_q = build_librenms_id_qs(server_key, librenms_id)

    # Lock the matched rows when asked so a concurrent conflict check serializes against an
    # existing owner (best-effort: a not-yet-created row can't be locked). Caller must hold a txn.
    manager = model.objects.select_for_update() if select_for_update else model.objects

    # Fast path: a single combined query covers the common case (0 or 1 match). One matching row is
    # unambiguous by definition — it can't collide host-vs-OOB or duplicate within a set — so return
    # it without a second query (this runs per-port during sync). Only when ≥2 rows match do we
    # re-run the separate host/OOB predicates (two rows per side) to classify and fail closed on the
    # precise ambiguity.
    combined = list(manager.filter(host_q | oob_q)[:2])
    if not combined:
        return None
    if len(combined) == 1:
        return combined[0]

    host_matches = list(manager.filter(host_q)[:2])
    oob_matches = list(manager.filter(oob_q)[:2])

    # Fail closed on intra-set ambiguity: two distinct rows sharing the same host (or
    # OOB) librenms_id is a data-integrity violation — binding to whichever sorts first
    # would silently attach sync/migration work to the wrong object.
    if len(host_matches) > 1:
        logger.warning(
            "Ambiguous librenms_id %r for %s on server %r: multiple host matches (pk=%s, pk=%s) "
            "— refusing to bind (fail closed).",
            librenms_id,
            model.__name__,
            server_key,
            host_matches[0].pk,
            host_matches[1].pk,
        )
        raise AmbiguousLibreNMSIdError(
            f"librenms_id {librenms_id!r} matches multiple {model.__name__} host records "
            f"(pk={host_matches[0].pk}, pk={host_matches[1].pk}) on server {server_key!r}"
        )
    if len(oob_matches) > 1:
        logger.warning(
            "Ambiguous librenms_id %r for %s on server %r: multiple OOB matches (pk=%s, pk=%s) "
            "— refusing to bind (fail closed).",
            librenms_id,
            model.__name__,
            server_key,
            oob_matches[0].pk,
            oob_matches[1].pk,
        )
        raise AmbiguousLibreNMSIdError(
            f"librenms_id {librenms_id!r} matches multiple {model.__name__} OOB records "
            f"(pk={oob_matches[0].pk}, pk={oob_matches[1].pk}) on server {server_key!r}"
        )

    host_match = host_matches[0] if host_matches else None
    oob_match = oob_matches[0] if oob_matches else None
    if host_match is not None and oob_match is not None and host_match.pk != oob_match.pk:
        logger.warning(
            "Ambiguous librenms_id %r for %s on server %r: host match pk=%s but OOB match "
            "pk=%s — refusing to bind to either (fail closed).",
            librenms_id,
            model.__name__,
            server_key,
            host_match.pk,
            oob_match.pk,
        )
        raise AmbiguousLibreNMSIdError(
            f"librenms_id {librenms_id!r} matches {model.__name__} host pk={host_match.pk} but a "
            f"different OOB pk={oob_match.pk} on server {server_key!r}"
        )
    # Host identity wins when both resolve to the same row (or only one matched).
    return host_match or oob_match


def get_librenms_oob(obj, server_key: str = "default") -> dict | None:
    """
    Return the OOB sub-object from the ``librenms_id`` JSON custom field, or ``None``.

    Read-only — never triggers a DB write.  Returns the raw ``oob`` dict verbatim so
    callers can inspect ``id``, ``type``, ``version``, and ``ip`` without additional helpers.

    Returns ``None`` when:
    - the field is absent, a legacy bare integer, or not a dict;
    - the server-key entry is a bare integer (no OOB attached);
    - the ``oob`` key is missing or not a dict.

    Args:
        obj: NetBox object with a ``librenms_id`` custom field.
        server_key: LibreNMS server key (from plugin ``servers`` config).

    Returns:
        dict or None
    """
    cf_value = obj.cf.get("librenms_id")
    if not isinstance(cf_value, dict):
        return None
    entry = cf_value.get(server_key)
    if not isinstance(entry, dict):
        return None
    oob = entry.get("oob")
    return oob if isinstance(oob, dict) else None


def set_librenms_oob(
    obj,
    oob_device_id: int,
    server_key: str = "default",
    *,
    oob_type: str,
) -> None:
    """
    Attach an OOB management controller to a device under *server_key*.

    Promotes the server-key value to the ``{"id": N, "oob": {...}}`` dict form if it is
    currently a bare integer.  Validates *oob_type* against ``OOB_TYPE_PATTERN`` or accepts
    the generic sentinel ``"oob"`` (used when no specific type keyword can be detected).

    Stores only the identity-mapping essentials — the OOB controller's LibreNMS device
    ``id`` and a static ``type`` label.  Mutable LibreNMS state (the controller's IP and
    firmware version) is deliberately NOT persisted here: the IP's source of truth is the
    device's interface-assigned ``oob_ip`` IPAddress, and the version belongs in LibreNMS.

    Does **not** call ``obj.save()`` — the caller is responsible for persisting the change.

    Args:
        obj: NetBox object with a ``librenms_id`` custom field.
        oob_device_id: LibreNMS device ID of the OOB controller.
        server_key: LibreNMS server key (from plugin ``servers`` config).
        oob_type: Raw type string (e.g. ``"iDRAC9"``, ``"ilo"``, or the generic ``"oob"``).
            Will be normalized to lowercase.

    Raises:
        ValueError: if *oob_type* does not match any known OOB type and is not the
            generic ``"oob"`` sentinel, or if the stored host-side ``librenms_id`` for
            *server_key* is a non-empty, unparseable string (fail closed rather than
            silently dropping the corrupted host link).
    """
    from netbox_librenms_plugin.constants import OOB_TYPE_PATTERN, OOB_TYPES

    _type_normalized = (oob_type or "").strip().lower()
    if _type_normalized == "oob":
        # Generic sentinel: OOB relationship confirmed but specific controller type unknown.
        normalized_type = "oob"
    elif not (match := OOB_TYPE_PATTERN.search(_type_normalized)):
        raise ValueError(f"oob_type {oob_type!r} does not match any known OOB type {OOB_TYPES}")
    else:
        normalized_type = match.group(1).lower()

    # Validate the OOB device ID: reject booleans (int subclass), zero, and negatives.
    if isinstance(oob_device_id, bool) or not isinstance(oob_device_id, (int, str)):
        raise ValueError(f"oob_device_id must be a positive integer, got {oob_device_id!r}")
    _oob_id = coerce_librenms_id(oob_device_id)
    if _oob_id is None:
        raise ValueError(f"oob_device_id must be a positive integer, got {oob_device_id!r}")

    cf_value = obj.custom_field_data.get("librenms_id") or {}
    if not isinstance(cf_value, dict):
        # Legacy single-server format: a bare int/str librenms_id. The rest of the module
        # still supports this shape and this PR avoids a mandatory migration, so promote it
        # to the per-server dict here instead of no-opping the OOB attach. The legacy id is
        # this server's host id; fail closed on a non-blank unparseable value.
        legacy_id = coerce_librenms_id(cf_value)
        if legacy_id is None and str(cf_value).strip():
            raise ValueError(f"Cannot attach OOB: legacy librenms_id on {obj!r} is not a valid id: {cf_value!r}")
        cf_value = {server_key: legacy_id} if legacy_id is not None else {}

    entry = cf_value.get(server_key)
    if isinstance(entry, int) and not isinstance(entry, bool):
        # Promote bare int to dict form, preserving the main device id. Pass it through
        # coerce_librenms_id() like the string branch so a stored 0/negative host id fails
        # closed instead of being wrapped into a bogus {"id": 0} that reads back as missing.
        coerced = coerce_librenms_id(entry)
        if coerced is None:
            raise ValueError(f"Cannot attach OOB: stored librenms_id for {server_key!r} is not a valid id: {entry!r}")
        entry = {"id": coerced}
    elif isinstance(entry, str):
        coerced = coerce_librenms_id(entry)
        if coerced:
            entry = {"id": coerced}
        elif entry.strip():
            # A non-empty but unparseable host id: fail closed instead of collapsing to
            # {} and silently dropping the existing host-side link. Mirrors
            # merge_librenms_links(), which already rejects this corrupted state.
            raise ValueError(f"Cannot attach OOB: stored librenms_id for {server_key!r} is not a valid id: {entry!r}")
        else:
            entry = {}
    elif isinstance(entry, dict):
        entry = dict(entry)  # shallow copy so we don't mutate the stored dict in-place
        # Fail closed on a corrupt dict-form host id too (e.g. {"id": "abc"}): otherwise
        # the OOB block is attached over a broken host mapping that get_librenms_device_id()
        # then reads as missing. Absent/empty id stays lenient (mirrors the string branch).
        host_id = entry.get("id")
        if host_id is not None and coerce_librenms_id(host_id) is None and str(host_id).strip():
            raise ValueError(
                f"Cannot attach OOB: stored librenms_id host id for {server_key!r} is not a valid id: {host_id!r}"
            )
    else:
        entry = {}

    oob: dict = {"id": _oob_id, "type": normalized_type}
    entry["oob"] = oob
    cf_value[server_key] = entry
    obj.custom_field_data["librenms_id"] = cf_value


def clear_librenms_oob(obj, server_key: str = "default") -> None:
    """
    Remove the OOB sub-object from the server-key entry of ``librenms_id``.

    The entry is left in ``{"id": N}`` object form — it is NOT demoted back to a bare
    integer (either form is valid; keeping object form avoids an extra save).

    Does **not** call ``obj.save()`` — the caller is responsible for persisting the change.
    Is a no-op when the server-key entry has no ``oob`` sub-key.

    Args:
        obj: NetBox object with a ``librenms_id`` custom field.
        server_key: LibreNMS server key (from plugin ``servers`` config).
    """
    cf_value = obj.custom_field_data.get("librenms_id")
    if not isinstance(cf_value, dict):
        return
    entry = cf_value.get(server_key)
    if not isinstance(entry, dict):
        return
    entry.pop("oob", None)
    cf_value[server_key] = entry
    obj.custom_field_data["librenms_id"] = cf_value


def is_legacy_librenms_id(value) -> bool:
    """
    Return ``True`` when a ``librenms_id`` custom-field value is the legacy bare-integer form.

    Legacy = a bare integer (created before the multi-server JSON refactor) or a string that
    parses as an integer, i.e. NOT the per-server dict form and not absent. Uses ``int()``
    coercion (so a whitespace-padded ``" 42 "`` is legacy too), matching
    :func:`coerce_librenms_id` / :func:`get_librenms_device_id` rather than a stricter
    ``str.isdigit()`` check that would hide a valid legacy link.

    Args:
        value: The raw ``custom_field_data["librenms_id"]`` value.

    Returns:
        bool: ``True`` for a *positive* bare int (non-bool) or a positive int-parseable string;
            ``False`` for ``None``, ``0``/negative, the dict form, a bool, or a non-numeric string.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        try:
            return int(value) > 0
        except (TypeError, ValueError):
            return False
    return False


def migrate_legacy_librenms_id(obj, server_key: str = "default") -> bool:
    """
    Migrate a legacy bare-integer ``librenms_id`` custom field to the JSON dict format,
    scoped to *server_key*.

    Only performs the migration when the current value is a bare integer, i.e. a record
    created before the multi-server JSON refactor.  The integer is assumed to belong to
    the server identified by *server_key* (the caller must verify this, e.g. by confirming
    that the LibreNMS device ID and serial number both match).

    Does **not** call ``obj.save()`` — the caller is responsible for persisting the change.

    Args:
        obj: NetBox object with a ``librenms_id`` custom field.
        server_key: LibreNMS server key the legacy integer should be scoped to.

    Returns:
        True if the value was migrated, False if it was already in the correct format.
    """
    cf_value = obj.custom_field_data.get("librenms_id")
    if isinstance(cf_value, bool):
        return False
    if isinstance(cf_value, int):
        int_value = cf_value
    elif isinstance(cf_value, str):
        # Coerce with int() (not str.isdigit()) so this writer accepts exactly what the
        # is_legacy_librenms_id() gate — and coerce_librenms_id / get_librenms_device_id — accept:
        # a whitespace-padded " 42 " or signed "+42" is legacy everywhere else, so gating the
        # migration on the stricter isdigit() would offer the Convert-ID button, pass every
        # precondition, then dead-end here with "could not be converted" (issue #99).
        try:
            int_value = int(cf_value)
        except (TypeError, ValueError):
            return False
    else:
        return False
    if int_value <= 0:
        # Keep the migration aligned with the positive-ID invariant (is_legacy_librenms_id /
        # get_librenms_device_id treat <= 0 as no valid ID): never canonicalise 0 or a negative
        # into the per-server JSON form.
        return False
    obj.custom_field_data["librenms_id"] = {server_key: int_value}
    logger.info(
        "Migrated legacy librenms_id %r → {%r: %d} on %r",
        cf_value,
        server_key,
        int_value,
        obj,
    )
    return True


_MODULE_TOKEN_LEAF_FIX_VERSION = (4, 5, 6)
_PARENT_CHASSIS_CLEAN_BUG_VERSION = (4, 4, 0)
_NETBOX_VERSION_PREFIX_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _get_netbox_version_tuple():
    """Return the running NetBox version as a (major, minor, patch) int tuple,
    or ``None`` when it cannot be determined.

    Tolerates trailing build metadata (``-dev``, ``-Docker-3.2.0``, ``+local``,
    ``.dev1``, etc.) by extracting the leading three numeric components and
    ignoring anything past them.
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
    """Return True when the running NetBox raises ``AttributeError`` while validating a
    parent interface on another virtual chassis member.

    NetBox 4.4.0 (issue #20197) dereferences ``self.parent.virtual_chassis`` in
    ``Interface.clean()``; 4.4.1 fixed it. When the version cannot be detected we keep the
    tolerance, so an undetectable release cannot turn a known core defect into a 500.
    """
    version = _get_netbox_version_tuple()
    if version is None:
        return True
    return version == _PARENT_CHASSIS_CLEAN_BUG_VERSION


def netbox_resolves_module_token_per_leaf():
    """Return True when the running NetBox resolves a single ``{module}`` token
    in a modular component template to the leaf module bay's position.

    NetBox 4.5.6 (issue #20467) changed single-token resolution to use the
    leaf bay's position instead of the root ancestor's. With that fix in
    place, sibling bays under the same parent always produce unique
    interface names, so the plugin's nested-name-conflict check no longer
    applies.

    When the version cannot be detected we assume the fix is present
    (permissive default) — the plugin pins NetBox >= 4.4 and we don't want
    to surface false positives on releases newer than this code knows about.
    """
    version = _get_netbox_version_tuple()
    if version is None:
        return True
    return version >= _MODULE_TOKEN_LEAF_FIX_VERSION


def ip_family(ip):
    """
    Return the IP family (4 or 6) of an IPAddress, or None when it has no address.

    Mirrors NetBox >= 4.5's ``IPAddress.family``. NetBox 4.4's property lacks the
    str-tolerant branch, and a freshly constructed ``IPAddress(address="...")``
    keeps the plain str in memory even after ``save()`` — so reading ``.family``
    on 4.4 raises AttributeError for exactly the objects the sync flows create.

    Args:
        ip: The IPAddress whose family to read.
    """
    address = ip.address
    if not address:
        return None
    if isinstance(address, str):
        return netaddr.IPNetwork(address).version
    return address.version


def _normalize_merge_entry(entry, *, owner_label, owner_name, server_key, copy_dict):
    """
    Coerce one device's per-server ``librenms_id`` entry to a dict, failing closed on corrupt shapes.

    Shared by :func:`merge_librenms_links` for the winner and donor entries so their per-server
    shape validation can't drift. A bare int (or numeric string) becomes ``{"id": N}``; a blank/None
    entry becomes ``{}``; a *non-blank* unparseable string, or an unsupported type (bool/float/list),
    is corrupted state and raises ``ValueError`` rather than silently collapsing to "no link".

    ``copy_dict`` controls whether a dict entry is returned as a shallow copy (the winner entry is
    mutated downstream and must not alias the stored dict) or as-is (the donor entry is read-only).
    ``mark_librenms_migrated`` deliberately does NOT use this helper: it adds nested id/oob validation
    and emits action-specific "migrate it first" guidance.

    Args:
        entry: The raw ``custom_field_data['librenms_id'][server_key]`` value.
        owner_label (str): "winner" or "donor", for the error message.
        owner_name (str): The device name, for the error message.
        server_key (str): The LibreNMS server key the entry is namespaced under.
        copy_dict (bool): Shallow-copy a dict entry (True) or return it as-is (False).

    Returns:
        dict: The normalized entry (``{"id": N}``, ``{}``, or the original/copied dict).
    """
    if isinstance(entry, int) and not isinstance(entry, bool):
        return {"id": entry}
    if isinstance(entry, str):
        coerced = coerce_librenms_id(entry)
        if coerced is None and entry.strip():
            raise ValueError(
                f"{owner_label} '{owner_name}' has an unparseable librenms_id[{server_key!r}] "
                f"{entry!r} — expected a positive integer or numeric string."
            )
        return {"id": coerced} if coerced else {}
    if isinstance(entry, dict):
        return dict(entry) if copy_dict else entry
    if entry is None:
        return {}
    raise ValueError(
        f"{owner_label} '{owner_name}' has an unsupported librenms_id[{server_key!r}] of type "
        f"{type(entry).__name__} — expected a positive integer, numeric string, or mapping."
    )


def _coerce_link_id_or_raise(raw, *, owner, name, server_key, kind):
    """
    Coerce a librenms_id host/oob id field to a positive int, or raise on a non-blank unparseable value.

    Treats a blank/whitespace string as absent (returns None) — lenient, matching the top-level string
    branch that collapses empty strings to "no id". A non-blank value that :func:`coerce_librenms_id`
    can't turn into a positive int (``"abc"``, ``0``, ``True``) is corrupt-but-present link state: fail
    closed with a message naming the owner/kind rather than silently dropping the link. Shared by the
    four id checks in :func:`merge_librenms_links` (winner/donor host id, winner/donor oob id) so the
    blank-strip / coerce / fail-closed rule can't drift between the copies.

    Args:
        raw: The raw id read from the entry (``entry["id"]`` or ``entry["oob"]["id"]``).
        owner: ``"winner"`` or ``"donor"`` — names the side in the error message.
        name: The device name, for the error message.
        server_key: The server key the entry is namespaced under.
        kind: ``"id"`` or ``"oob id"`` — names the field in the error message.

    Returns:
        int | None: The positive integer id, or None when the value is blank/absent.
    """
    if isinstance(raw, str) and not raw.strip():
        raw = None
    coerced = coerce_librenms_id(raw) if raw is not None else None
    if raw is not None and coerced is None:
        raise ValueError(
            f"{owner} '{name}' has an unparseable librenms_id[{server_key!r}] {kind} "
            f"{raw!r} — expected a positive integer or numeric string."
        )
    return coerced


def merge_librenms_links(winner, donor, server_key: str = "default") -> dict:
    """
    Merge donor's ``librenms_id[server_key]`` link state into winner's.

    Used by the Stage-2 "two NetBox devices represent the same physical box"
    flow.  This function **only mutates the winner's** ``custom_field_data``.
    The donor is not modified here — callers must call
    :func:`mark_librenms_migrated` separately (and save both objects
    themselves) to clear the donor's active link and stamp the
    ``_migrated_to`` marker.

    Conflict policy (winner-wins for already-populated fields):

    * If winner already has ``id`` set, donor's ``id`` is moved into the
      ``oob`` slot (only when winner has no ``oob`` yet, with type derived
      from the donor's name when possible).
    * If winner has no ``id`` and donor does, winner inherits ``id``.
    * If donor has an ``oob`` sub-block and winner has none, winner
      inherits it verbatim.
    * Winner's existing ``oob`` is never overwritten.

    Args:
        winner: NetBox Device that will hold the merged link state.
        donor: NetBox Device whose link state will be absorbed.
        server_key: LibreNMS server key to scope the merge to.

    Returns:
        A dict describing what was actually merged: keys ``host_id_from_donor``,
        ``oob_from_donor`` (None or dict), ``donor_id_demoted_to_oob``
        (None or dict).  Useful for audit logging and tests.
    """
    from netbox_librenms_plugin.constants import normalize_oob_type

    summary = {
        "host_id_from_donor": None,
        "oob_from_donor": None,
        "donor_id_demoted_to_oob": None,
    }

    # Read with an explicit None check rather than ``or {}``: a falsy-but-corrupt value
    # (``False`` from a bad bool write, or ``0``) must NOT collapse to ``{}`` and be merged as
    # "no mapping" — it has to reach the dict guard below and fail closed. Only a genuinely
    # absent field (None) is treated as "no link".
    winner_cf = winner.custom_field_data.get("librenms_id")
    donor_cf = donor.custom_field_data.get("librenms_id")
    if winner_cf is None:
        winner_cf = {}
    if donor_cf is None:
        donor_cf = {}
    if not isinstance(winner_cf, dict) or not isinstance(donor_cf, dict):
        # Legacy bare-int forms (and corrupt non-mapping values such as a bool/0) must be
        # repaired/migrated by the caller before merging — never silently treated as "no link".
        raise ValueError("Cannot merge: one or both devices have a legacy bare-integer or corrupt librenms_id.")

    # The winner entry is copied (it is mutated below); the donor entry is read-only. Both share the
    # same fail-closed shape validation via _normalize_merge_entry so they can't drift apart.
    winner_entry = _normalize_merge_entry(
        winner_cf.get(server_key),
        owner_label="winner",
        owner_name=winner.name,
        server_key=server_key,
        copy_dict=True,
    )
    donor_entry = _normalize_merge_entry(
        donor_cf.get(server_key),
        owner_label="donor",
        owner_name=donor.name,
        server_key=server_key,
        copy_dict=False,
    )

    def _extract_oob_entry(owner_label, owner_name, entry):
        # Validate the nested oob shape the same way the top-level/per-server shapes are
        # validated above: a non-dict, non-null oob is corrupted state, not "no OOB link".
        # Silently treating it as None could let donor data overwrite it, or drop it when the
        # donor is later marked migrated — so fail closed instead.
        raw_oob = entry.get("oob")
        if raw_oob is None:
            return None
        if isinstance(raw_oob, dict):
            return raw_oob
        raise ValueError(
            f"{owner_label} '{owner_name}' has an unsupported librenms_id[{server_key!r}] oob shape "
            f"{type(raw_oob).__name__} — expected a mapping or null."
        )

    donor_id = donor_entry.get("id")
    donor_oob = _extract_oob_entry("donor", donor.name, donor_entry)

    # Coerce both IDs before branching so that a malformed but truthy winner_id (e.g. "abc") does
    # not incorrectly trigger the "demote donor" path. A blank/whitespace string id is treated as
    # "no id" (lenient, matching the top-level string branch above which collapses empty strings to
    # {}); only a non-blank unparseable value fails closed. winner is checked before donor so a
    # corrupt winner id surfaces first, as before.
    winner_id = _coerce_link_id_or_raise(
        winner_entry.get("id"), owner="winner", name=winner.name, server_key=server_key, kind="id"
    )
    donor_id = _coerce_link_id_or_raise(donor_id, owner="donor", name=donor.name, server_key=server_key, kind="id")
    winner_oob = _extract_oob_entry("winner", winner.name, winner_entry)
    # _extract_oob_entry only validates the oob *shape* (dict/null), not its id. A corrupt
    # non-blank winner oob id (e.g. "abc", 0) would otherwise make the slot look "occupied"
    # (winner_oob is not None) below, skipping donor OOB inheritance/demotion and silently
    # losing the donor's real controller link once the donor is marked migrated — while the
    # winner keeps an unusable oob. Fail closed, mirroring the winner_id/donor_id checks above;
    # a blank/whitespace id is treated leniently as "no id".
    if winner_oob is not None:
        _coerce_link_id_or_raise(
            winner_oob.get("id"), owner="winner", name=winner.name, server_key=server_key, kind="oob id"
        )

    # Validate the donor's oob id up-front (mirroring the winner_oob check above) so a corrupt
    # non-blank id fails closed regardless of which branch fires below, and so a metadata-only
    # donor oob (a type but no usable id) is NOT mistaken for a real controller link. Treating
    # any truthy donor_oob as "occupied" would skip demoting the donor's host id into the empty
    # oob slot, silently losing that host link once the donor is marked migrated. Only meaningful
    # when the winner has no oob of its own (otherwise donor_oob is ignored); a blank/whitespace
    # id is treated leniently as "no id".
    donor_oob_has_valid_id = False
    _donor_oob_id = None
    if winner_oob is None and donor_oob is not None:
        _donor_oob_id = _coerce_link_id_or_raise(
            donor_oob.get("id"), owner="donor", name=donor.name, server_key=server_key, kind="oob id"
        )
        donor_oob_has_valid_id = _donor_oob_id is not None

    # The winner has only one free link slot (oob), while the donor carries two distinct links
    # (its host id and a real oob id). Choosing either donor link would silently orphan the other
    # once mark_librenms_migrated() clears the donor entry, so require the operator to unlink one
    # side first rather than applying a lossy preference.
    if winner_id is not None and donor_id is not None and donor_id != winner_id and donor_oob_has_valid_id:
        raise ValueError(
            f"Cannot merge: donor '{donor.name}' has two distinct LibreNMS links but winner "
            f"'{winner.name}' has only one free link slot. Unlink one donor link first."
        )

    # Fail closed when the donor's host id has nowhere to go: the winner already occupies BOTH
    # its host-id slot (winner_id) AND its oob slot (winner_oob), so neither the inherit-id branch
    # (needs an empty winner host slot) nor the demote branch (needs an empty winner oob slot) can
    # place a distinct donor id. Proceeding would silently drop the donor's LibreNMS host link and
    # orphan that device once the donor is marked migrated — the same data loss every other branch
    # here fails closed on, and exactly what the merge modal's "Moved to winner" promise forbids.
    # A donor id EQUAL to the winner's is the same host linkage (a duplicate mapping), not an
    # orphan, so it is allowed through; a donor that carries its own real oob is inherited by the
    # branch below when the winner's oob slot is free (winner_oob is None), so this only fires when
    # the winner's oob is genuinely occupied.
    if winner_id is not None and donor_id is not None and donor_id != winner_id and winner_oob is not None:
        raise ValueError(
            f"Cannot merge: winner '{winner.name}' already holds both a LibreNMS host id and an "
            f"OOB link, so donor '{donor.name}' host id {donor_id} has nowhere to move. "
            "Unlink one side first."
        )

    if winner_id is None and donor_id is not None:
        winner_entry["id"] = donor_id
        summary["host_id_from_donor"] = donor_id
    elif (
        winner_id is not None
        and donor_id is not None
        and donor_id != winner_id
        and winner_oob is None
        and not donor_oob_has_valid_id
    ):
        # Demote donor's host id into winner's oob slot — but ONLY when the donor has no real OOB
        # controller (no usable oob id) and the donor host id actually differs from the winner's.
        # Equal ids are the same host linkage (a duplicate mapping), not an OOB controller, so
        # demoting them would fabricate a fake OOB link on the merged device.
        # A donor shaped {"id": ..., "oob": {<valid id>}} has a REAL controller, which the
        # donor_oob inheritance path below takes precedence over — so it is excluded here. But a
        # metadata-only donor oob ({"type": ...} with no usable id) is NOT a real link: fold its
        # metadata (type) into the demoted block so it survives, then preserve the donor host id.
        demoted = dict(donor_oob) if donor_oob else {}
        demoted.pop("id", None)
        if not demoted.get("type"):
            # Resolve the type via normalize_oob_type (vendor token wins over the generic 'oob')
            # for parity with the import-path OOB detection (device_operations._detect_oob_type_from_name),
            # instead of a raw first-match search that would pick 'oob' from a name like
            # 'leaf01-oob-idrac9'. Fall back to the generic 'oob' when no vendor token matches.
            demoted["type"] = normalize_oob_type(donor.name or "", "") or "oob"
        demoted["id"] = donor_id
        winner_entry["oob"] = demoted
        summary["donor_id_demoted_to_oob"] = demoted
        winner_oob = demoted

    if donor_oob and winner_oob is None:
        # Inherit the donor's oob. Its id was already validated/coerced up-front (a non-blank
        # unparseable id fails closed there); a blank/absent id is dropped so the winner inherits
        # the oob metadata (type, etc.) without carrying a bogus id string.
        inherited_oob = dict(donor_oob)
        if _donor_oob_id is not None:
            inherited_oob["id"] = _donor_oob_id
        else:
            inherited_oob.pop("id", None)
        # Don't persist an empty {} oob: a donor oob carrying only a blank/absent id (no type or
        # other metadata) collapses to {} once the bogus id is dropped. A later merge reads that
        # empty dict as an OCCUPIED oob slot (winner_oob is not None) and refuses to demote a
        # subsequent donor host id into it — silently losing that mapping. Only inherit when
        # something meaningful (a valid id or other oob metadata) actually remains.
        if inherited_oob:
            winner_entry["oob"] = dict(inherited_oob)
            summary["oob_from_donor"] = dict(inherited_oob)

    winner_cf[server_key] = winner_entry
    winner.custom_field_data["librenms_id"] = winner_cf
    return summary


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
        # ip_family(), not ip.family: NetBox 4.4's property raises on in-memory str addresses,
        # and getattr's default would turn that crash into a bogus refusal of a valid address.
        family = ip_family(ip)
        if field == "primary_ip4" and family != 4:
            raise ValueError(f"set_device_ip_fk: refusing to set primary_ip4 to non-IPv4 address {ip}")
        if field == "primary_ip6" and family != 6:
            raise ValueError(f"set_device_ip_fk: refusing to set primary_ip6 to non-IPv6 address {ip}")
    setattr(device, field, ip)
    if save:
        device.save(update_fields=[field])
    return field


def mark_librenms_migrated(donor, winner_pk: int, server_key: str = "default", at: str | None = None) -> None:
    """
    Mark *donor* as migrated to the device with primary key *winner_pk*.

    Removes any active ``id`` / ``oob`` keys from ``donor.custom_field_data
    ['librenms_id'][server_key]`` (so the device is no longer matched by
    ``find_by_librenms_id``) and writes a ``_migrated_to`` sub-key with the
    target device pk, server key, and ISO-8601 UTC timestamp.

    Does **not** call ``donor.save()`` — caller is responsible for persisting.

    Args:
        donor: NetBox Device being absorbed by the winner.
        winner_pk: Primary key of the winning device.
        server_key: LibreNMS server key whose link state is being cleared.
        at: ISO timestamp string. When None, a timezone-aware UTC timestamp
            (``datetime.now(timezone.utc)`` formatted as ``YYYY-MM-DDTHH:MM:SSZ``)
            is used.
    """
    from datetime import datetime, timezone

    # bool is a subclass of int (int(True) == 1); reject it and non-positive
    # ids so a malformed marker can never target the wrong device.
    if isinstance(winner_pk, bool) or not isinstance(winner_pk, int) or winner_pk <= 0:
        raise ValueError(f"winner_pk must be a positive integer, got {winner_pk!r}")
    # Keep the writer's key normalization paired with get_migrated_to_marker()'s.
    server_key = server_key or "default"

    cf_value = donor.custom_field_data.get("librenms_id")
    if cf_value is None:
        cf_value = {}
    elif not isinstance(cf_value, dict):
        # Fail closed on a legacy bare-int/bare-string or corrupt top-level librenms_id rather
        # than silently collapsing it to {} and stamping the marker: that drops the donor's
        # still-resolvable mapping (find_by_librenms_id can no longer locate the old owner),
        # converting recoverable state into data loss. The caller must migrate the legacy form
        # first — mirrors merge_librenms_links(), which rejects the same shapes.
        raise ValueError(
            f"Cannot mark '{getattr(donor, 'name', donor)}' migrated: librenms_id is a legacy "
            f"bare-integer or corrupt value ({cf_value!r}); migrate it to the dict form first."
        )
    entry = cf_value.get(server_key)
    if isinstance(entry, int) and not isinstance(entry, bool):
        entry = {"id": entry}
    elif isinstance(entry, str):
        # Mirror merge_librenms_links()'s per-entry validation: a non-empty string that can't be
        # parsed to a positive id is corrupt donor state, not "no link" — fail closed instead of
        # collapsing it to {} and stamping the donor migrated (which would hide the malformed value
        # behind _migrated_to and drop any recoverable mapping).
        coerced = coerce_librenms_id(entry)
        if coerced is None and entry.strip():
            raise ValueError(
                f"Cannot mark '{getattr(donor, 'name', donor)}' migrated: "
                f"librenms_id[{server_key!r}] is unparseable ({entry!r}); migrate it first."
            )
        entry = {"id": coerced} if coerced else {}
    elif isinstance(entry, dict):
        entry = dict(entry)
        # Validate the host id itself before it is popped below, the same way the bare-int and
        # bare-string branches above do: a non-blank but unparseable id ({"id": "abc"}, {"id": 0},
        # {"id": True}) is corrupt-but-recoverable state, not "no link". Without this the dict
        # branch would silently pop it and stamp _migrated_to, erasing the recoverable mapping. A
        # blank/None id ({}, {"id": None}) is a genuine "no active link" and proceeds.
        raw_id = entry.get("id")
        if isinstance(raw_id, str) and not raw_id.strip():
            raw_id = None
        if raw_id is not None and coerce_librenms_id(raw_id) is None:
            raise ValueError(
                f"Cannot mark '{getattr(donor, 'name', donor)}' migrated: "
                f"librenms_id[{server_key!r}] has an unparseable id ({raw_id!r}); migrate it first."
            )
        # Validate the nested oob before it is popped below, the same way
        # merge_librenms_links() does: a non-dict oob, or an oob carrying a
        # non-blank unparseable id, is corrupt-but-recoverable state. Failing
        # closed here stops a donor like {"oob": "garbage"} or
        # {"oob": {"id": "abc"}} from being silently converted to marker-only
        # state (erasing the link data) instead of being migrated first.
        raw_oob = entry.get("oob")
        if raw_oob is not None:
            if not isinstance(raw_oob, dict):
                raise ValueError(
                    f"Cannot mark '{getattr(donor, 'name', donor)}' migrated: "
                    f"librenms_id[{server_key!r}] has unsupported oob type {type(raw_oob).__name__}."
                )
            raw_oob_id = raw_oob.get("id")
            if isinstance(raw_oob_id, str) and not raw_oob_id.strip():
                raw_oob_id = None
            if raw_oob_id is not None and coerce_librenms_id(raw_oob_id) is None:
                raise ValueError(
                    f"Cannot mark '{getattr(donor, 'name', donor)}' migrated: "
                    f"librenms_id[{server_key!r}] has an unparseable oob id ({raw_oob_id!r}); migrate it first."
                )
    elif entry is None:
        entry = {}
    else:
        # An unsupported shape (bool/float/list/…) is corrupt state, not "no mapping". Fail closed
        # like the top-level guard rather than collapsing to {} and marking the donor migrated.
        raise ValueError(
            f"Cannot mark '{getattr(donor, 'name', donor)}' migrated: "
            f"librenms_id[{server_key!r}] has unsupported type {type(entry).__name__}."
        )
    entry.pop("id", None)
    entry.pop("oob", None)
    entry["_migrated_to"] = {
        "device_id": int(winner_pk),
        "server_key": server_key,
        "at": at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    cf_value[server_key] = entry
    donor.custom_field_data["librenms_id"] = cf_value


def get_migrated_to_marker(device, server_key: str = "default") -> dict | None:
    """
    Read the ``_migrated_to`` marker (Stage 2b) from a device's librenms_id block.

    Used by the librenms-sync UI to switch a donor device into "migrated mode":
    disable sync actions and surface per-row "Move to winner" buttons. A live host or
    OOB link takes precedence over a stale marker.

    Args:
        device: The donor device whose ``librenms_id[server_key]`` sub-block is read.
        server_key (str): The LibreNMS server key the marker is namespaced under.

    Returns:
        dict | None: The marker dict ``{device_id, server_key, at}`` when the donor
            was previously merged via :func:`mark_librenms_migrated`, or None when no
            valid marker is present (missing, malformed, or superseded by a live link).
    """
    if device is None:
        return None
    # Blank/None server_key means the default server everywhere else in this plugin; normalize
    # here so every marker reader agrees, whatever key hygiene its call site has.
    server_key = server_key or "default"
    cf_value = device.cf.get("librenms_id") if hasattr(device, "cf") else None
    if not isinstance(cf_value, dict):
        return None
    entry = cf_value.get(server_key)
    if not isinstance(entry, dict):
        return None
    # A live host or OOB link takes precedence over a stale _migrated_to marker: if the
    # same entry still resolves via find_by_librenms_id(), leaving the donor in migrated
    # mode is a contradictory state. Treat the marker as obsolete when an active id/oob
    # link exists on the entry.
    if coerce_librenms_id(entry.get("id")) is not None:
        return None
    oob = entry.get("oob")
    if isinstance(oob, dict) and coerce_librenms_id(oob.get("id")) is not None:
        return None
    marker = entry.get("_migrated_to")
    if not isinstance(marker, dict):
        return None
    # The marker is namespaced under cf[server_key] and mark_librenms_migrated() always
    # stamps its own server_key. Reject a marker whose stamped server_key doesn't match the
    # entry it lives under (malformed or copied across server sub-blocks) so a stray marker
    # can't force the donor into migrated mode for the wrong server.
    if marker.get("server_key") != server_key:
        return None
    device_id = marker.get("device_id")
    # bool is a subclass of int; reject it and non-positive ids so migrated-mode
    # logic never targets a bogus device from a malformed marker.
    if isinstance(device_id, bool) or not isinstance(device_id, int) or device_id <= 0:
        return None
    return marker


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
        dict: ``{migrated_to_marker, migrated_to_winner}``. Both None when not in migrated mode (no
            marker, or a corrupt self-pointing marker). ``migrated_to_winner`` is a lazy ``Device``
            proxy (truthiness/attribute access resolves it; it proxies None if the winner row was
            since deleted) — test it via truthiness in templates, not ``is None``.
    """
    marker = get_migrated_to_marker(obj, server_key)
    # A self-pointing marker (winner == this donor) is corrupt: it would flip the donor's own sync
    # page into migrated mode resolving the "winner" to itself, hiding the ordinary sync controls.
    # Suppress it in memory (no Device fetch) — the donor always exists, so resolving device_id would
    # only ever return obj. The marker is deliberately NOT rejected in get_migrated_to_marker(): the
    # move views read it via _resolve_winner_for_donor() to report it as "stale/corrupt".
    if not marker or marker.get("device_id") == getattr(obj, "pk", None):
        return {"migrated_to_marker": None, "migrated_to_winner": None}

    # device_id is guaranteed a positive int by get_migrated_to_marker(). Defer the row fetch until a
    # template reads the winner (cable/module/VLAN never do), saving a query per HTMX refresh.
    return {
        "migrated_to_marker": marker,
        "migrated_to_winner": SimpleLazyObject(lambda: Device.objects.filter(pk=marker["device_id"]).first()),
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
    """Dict mapping LibreNMS keys → ``ModuleType``, plus manufacturer-scoped overrides.

    Behaves exactly like a plain ``dict`` for the global key space (model/part_number
    plus any global ``ModuleTypeMapping`` rows), but additionally exposes
    ``mfr_mappings`` — a dict keyed by ``(manufacturer_pk, librenms_model)`` populated
    from manufacturer-scoped ``ModuleTypeMapping`` rows. ``resolve_module_type`` probes
    the manufacturer-scoped key first so vendor-specific overrides win for matching
    devices without leaking into other vendors' lookups.
    """

    __slots__ = ("mfr_mappings",)

    def __init__(self, *args, mfr_mappings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mfr_mappings: dict = mfr_mappings or {}


# (version, index) held as a single tuple so a lock-free reader always gets a CONSISTENT pair from
# one atomic reference read — never a freshly-bumped version paired with a stale index, or vice
# versa. Rebuilds are serialised by the lock so concurrent renders don't each rebuild.
_MODULE_TYPES_INDEX_CACHE: tuple = (None, None)
_MODULE_TYPES_INDEX_LOCK = threading.Lock()


def _module_types_index_version():
    """
    Return a cheap fingerprint of the module-type index inputs.

    Changes on any create/delete/edit (via ``save()``) of a ModuleType, a module-type
    InterfaceTemplate, or a ModuleTypeMapping, so the cache below self-invalidates
    (including across test transactions) with no signal wiring.

    Limitation: a count-preserving bulk ``QuerySet.update()`` bypasses ``save()`` and so does
    not bump ``last_updated`` (``auto_now``); such an edit leaves the fingerprint unchanged and
    the cached index stale until the row count changes or the worker restarts. The plugin only
    ever mutates these models via ``save()``, so this affects external bulk-update tooling only.
    """
    from dcim.models import InterfaceTemplate, ModuleType

    from netbox_librenms_plugin.models import ModuleTypeMapping

    def _fp(qs):
        agg = qs.aggregate(n=Count("pk"), latest=Max("last_updated"))
        return (agg["n"] or 0, agg["latest"].isoformat() if agg["latest"] else "")

    return (
        _fp(ModuleType.objects.all()),
        _fp(InterfaceTemplate.objects.filter(module_type__isnull=False)),
        _fp(ModuleTypeMapping.objects.all()),
    )


def get_module_types_indexed() -> dict:
    """
    Return the module-type index, rebuilt only when its inputs change.

    The underlying build loads every ModuleType (with interface templates) and
    ModuleTypeMapping — per-render work that dominates the module-sync render once
    the rule-lookup cost is removed. Cache it keyed on a cheap fingerprint of those
    tables so repeat renders reuse it.

    Thread-safe: the (version, index) pair is read as one atomic tuple reference on the fast
    path, and rebuilds happen under a lock so concurrent renders neither rebuild redundantly nor
    observe a torn version/index swap.
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
    index — they live under ``mfr_mappings`` keyed by ``(manufacturer_pk,
    librenms_model)`` so they apply only to devices of that vendor.
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
    return _ModuleTypeIndex(result, mfr_mappings=mfr_mappings)


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
    the note in ``get_module_types_indexed`` — ``ModuleTypeMapping``'s
    conditional UniqueConstraint pair (``unique_module_type_mapping`` and
    ``unique_module_type_mapping_global``) makes mapping-vs-mapping
    collisions structurally impossible.
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
            except (re.error, IndexError):
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


def resolve_module_type(
    model_name: str,
    module_types: dict,
    manufacturer=None,
    *,
    norm_rules: dict | None = None,
    generic_fallback: dict | None = None,
):
    """
    Resolve a LibreNMS model name to a NetBox ModuleType via direct lookup then normalization.

    Pass ``norm_rules`` (from :func:`preload_normalization_rules`) to avoid
    repeated DB queries when called in a loop.

    Pass ``generic_fallback`` (from :func:`get_generic_module_types_indexed`) to enable
    a secondary look-up in the 'Generic' manufacturer when the primary index has no match
    (e.g. because the same model name is used by multiple manufacturers, making it
    ambiguous in the primary index).

    When *manufacturer* is provided **and** *module_types* is the ``_ModuleTypeIndex``
    returned by :func:`get_module_types_indexed`, manufacturer-scoped
    ``ModuleTypeMapping`` rows are probed first (raw and normalized model name)
    before falling back to the global index. This lets vendor-specific overrides
    win without polluting lookups for other vendors.

    Returns the matched ModuleType or None.
    """
    if not model_name:
        return None

    mfr_mappings = getattr(module_types, "mfr_mappings", None)
    mfr_pk = getattr(manufacturer, "pk", None)

    def _lookup_mfr(name):
        if mfr_mappings and mfr_pk is not None and name:
            return mfr_mappings.get((mfr_pk, name))
        return None

    matched = _lookup_mfr(model_name)
    if not matched:
        matched = module_types.get(model_name)
    normalized = None
    if not matched:
        normalized = apply_normalization_rules(
            model_name, "module_type", manufacturer=manufacturer, preloaded_rules=norm_rules
        )
        if normalized != model_name:
            matched = _lookup_mfr(normalized) or module_types.get(normalized)
    if not matched and generic_fallback:
        matched = generic_fallback.get(model_name)
        if not matched:
            if normalized is None:
                normalized = apply_normalization_rules(
                    model_name, "module_type", manufacturer=manufacturer, preloaded_rules=norm_rules
                )
            if normalized != model_name:
                matched = generic_fallback.get(normalized)
    return matched


def get_enabled_ignore_rules() -> list:
    """Return all enabled InventoryIgnoreRule instances as a list."""
    from netbox_librenms_plugin.models import InventoryIgnoreRule

    return list(InventoryIgnoreRule.objects.filter(enabled=True).order_by("pk"))


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
