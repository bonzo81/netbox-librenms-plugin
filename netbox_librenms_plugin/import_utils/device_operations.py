"""Device validation, import, and fetch operations."""

import logging
from types import SimpleNamespace

from dcim.models import Device, DeviceRole, DeviceType, Rack, Site
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from virtualization.models import Cluster  # noqa: F401 — used by test mock.patch targets

from ..ip_addressing import parse_host_address
from ..librenms_api import LibreNMSAPI
from ..utils import (
    AmbiguousLibreNMSIdError,
    cached_row_matches,
    coerce_librenms_id,
    find_by_librenms_id,
    find_matching_platform,
    find_matching_site,
    get_librenms_device_id,
    get_librenms_oob,
    is_legacy_librenms_id,
    match_librenms_hardware_to_device_type,
    normalize_serial,
    set_librenms_device_id,
)
from ..constants import normalize_oob_type
from ..import_validation_helpers import (
    apply_merge_candidates,
    apply_oob_detection_result,
    clear_match_derived_action_fields,
)
from .cache import get_import_device_cache_key
from .virtual_chassis import (
    _generate_vc_member_name,
    empty_virtual_chassis_data,
    get_virtual_chassis_data,
    update_vc_member_suggested_names,
)

logger = logging.getLogger(__name__)

# Prefix on the issue appended by validate_device_for_import()'s catch-all except branch when
# validation aborts on an exception and returns only a PARTIAL result. Downstream fail-closed
# guards (e.g. bulk_import.detect_collisions_for_device_ids) key on this to treat the row as
# not-reliably-checked. Keep it a shared constant so the producer and every consumer can't drift
# apart — a silent text change would otherwise defeat the fail-closed guarantee.
VALIDATION_ERROR_ISSUE_PREFIX = "Validation error:"


def _detect_oob_type_from_name(name):
    """
    Return the canonical OOB type token found in a device name.

    Routes through normalize_oob_type() so a vendor-specific token wins over the
    generic "oob" even when "oob" appears earlier in the name (e.g.
    "leaf01-oob-idrac9" -> "idrac", not "oob"). A bare re.search() returns the first
    token and would downgrade the hint.

    Args:
        name (str): The device name to inspect.

    Returns:
        str | None: The canonical OOB type token (idrac/ilo/ipmi/bmc/drac), or None
            if no token matches.
    """
    if not name:
        return None
    return normalize_oob_type(name, "")


def _describe_existing_librenms_link(obj, server_key):
    """
    Describe the current LibreNMS linkage on a NetBox object.

    Always returns a dict (with all-None values if nothing is linked) so callers can
    treat it as a plain status object. Tolerates legacy bare-int and dict-form custom
    field values.

    Args:
        obj: The NetBox object whose ``librenms_id`` custom field is inspected.
        server_key (str): The LibreNMS server key to read linkage for.

    Returns:
        dict: ``{"host_id": int|None, "oob_id": int|None, "oob_type": str|None}``
            summarising the ``librenms_id`` custom field for *server_key*.
    """
    info = {"host_id": None, "oob_id": None, "oob_type": None}
    # Host ID via the single canonical accessor (per coding guidelines) rather than touching the
    # custom field directly. auto_save=False: this is a read-only describe/badge path and must not
    # mutate custom_field_data. get_librenms_device_id handles legacy bare-int / string-digit and
    # the per-server dict's "id" key, mirroring find_by_librenms_id's coercion.
    host_id = get_librenms_device_id(obj, server_key, auto_save=False)
    if host_id is not None and host_id > 0:
        info["host_id"] = host_id
    # The OOB sub-object via the canonical accessor (mirrors the host-id read above): it returns
    # the raw oob dict ({"id": <int|str>, "type": <str>, ...}) or None, encapsulating the
    # dict-form navigation {"<server_key>": {"id": ..., "oob": {...}}}.
    oob = get_librenms_oob(obj, server_key)
    if oob is not None:
        oob_id = coerce_librenms_id(oob.get("id"))
        if oob_id is not None and oob_id > 0:
            info["oob_id"] = oob_id
        oob_type = oob.get("type")
        if isinstance(oob_type, str) and oob_type:
            info["oob_type"] = oob_type
    return info


def _describe_link_note(existing_link):
    """
    Return a human-readable phrase describing an existing LibreNMS link.

    Centralizes the host-id / OOB / unlinked wording that was copy-pasted — and had
    drifted ("already linked" vs "currently linked", "OOB already linked" vs "as an OOB
    controller") — across the VM-hostname, device-hostname, primary-IP and serial-match
    import branches, so the phrasing stays consistent and a future change lands in one place.

    Args:
        existing_link: A :func:`_describe_existing_librenms_link` dict, or None.

    Returns:
        str: One of "currently linked to LibreNMS device #N", "currently linked to LibreNMS
            as an OOB controller", or "not linked to LibreNMS".
    """
    link = existing_link or {}
    if link.get("host_id"):
        return f"currently linked to LibreNMS device #{link['host_id']}"
    if link.get("oob_id"):
        return "currently linked to LibreNMS as an OOB controller"
    return "not linked to LibreNMS"


def resolve_device_by_host_ip(primary_ip):
    """
    Resolve the unique NetBox device whose interface or oob_ip carries a host address.

    Scans EVERY ``IPAddress`` row sharing ``primary_ip`` as its host address (duplicate
    net_host rows are possible) across both the interface-assignment path and the ``oob_ip``
    direct-FK path, so a genuine collision fails closed instead of binding to whichever
    duplicate row sorts first. An IP can be a device's ``oob_ip`` while assigned to no
    interface, so the assigned_object scan alone would miss an OOB-only link.

    Shared by :func:`validate_device_for_import` and ``bulk_import._refresh_existing_device``
    so the two paths can't drift on which device a management IP resolves to.

    Args:
        primary_ip: The management IP (host form) to resolve.

    Returns:
        tuple: ``(device | None, ambiguous: bool, matching_ips: QuerySet)``.
            ``device`` is the single matching device, or ``None`` when none or more than one
            match; ``ambiguous`` is ``True`` only when >1 distinct device shares the address
            (the caller must block the import); ``matching_ips`` is the net_host queryset so
            callers can reuse it (e.g. for the ``oob_ip`` membership check).

    Raises:
        ValueError: When *primary_ip* is not a parseable host address. Callers catch it and
            fail closed for that device.
    """
    from dcim.models import Device
    from ipam.models import IPAddress

    canonical_host = str(parse_host_address(primary_ip))

    # prefetch_related on the assigned_object GenericForeignKey resolves every duplicate net_host
    # row's interface in one bulk pass instead of a per-row content-type lookup (small N, but free).
    matching_ips = IPAddress.objects.filter(address__net_host=canonical_host).prefetch_related("assigned_object")
    candidate_devices = {}
    matching_exists = False
    for existing_ip in matching_ips:
        matching_exists = True
        assigned = getattr(existing_ip, "assigned_object", None)
        dev = getattr(assigned, "device", None) if assigned else None
        if dev:
            candidate_devices[dev.pk] = dev
    if matching_exists:
        for oob_device in Device.objects.filter(oob_ip__in=matching_ips):
            candidate_devices[oob_device.pk] = oob_device
    if len(candidate_devices) > 1:
        return None, True, matching_ips
    if candidate_devices:
        return next(iter(candidate_devices.values())), False, matching_ips
    return None, False, matching_ips


def _try_chassis_device_type_match(api, device_id, preloaded_device_type_rules: dict | None = None):
    """
    Attempt device type matching using chassis inventory fields.

    When the LibreNMS hardware string doesn't match any NetBox device type,
    the chassis entity often contains a more standardized identifier
    (e.g., entPhysicalName 'CHAS-BP-MX480-S' or entPhysicalModelName '710-017414')
    that matches a DeviceType part_number or model.

    Tries entPhysicalName first (typically the chassis part number),
    then entPhysicalModelName as fallback.

    Args:
        api (LibreNMSAPI): API client used to fetch the chassis inventory.
        device_id (int): LibreNMS device ID whose chassis inventory to inspect.
        preloaded_device_type_rules (dict | None): Optional device_type NormalizationRule set from
            :func:`preload_normalization_rules`, threaded into the inner match so bulk imports don't
            reissue the rule query per device.

    Returns:
        dict | None: Dict with matched/device_type/match_type keys, or None on failure.
    """
    skip_values = {"", "-", "Unspecified", "BUILTIN", "None"}

    try:
        success, inventory = api.get_inventory_filtered(device_id, ent_physical_class="chassis")
        if not success or not inventory:
            return None

        for item in inventory:
            # Try entPhysicalName first (often the chassis part number like CHAS-BP-MX480-S)
            for field in ("entPhysicalName", "entPhysicalModelName"):
                value = item.get(field) or ""
                if value and value not in skip_values:
                    chassis_match = match_librenms_hardware_to_device_type(
                        value, preloaded_rules=preloaded_device_type_rules
                    )
                    if chassis_match is None:
                        continue
                    if chassis_match["matched"]:
                        chassis_match["match_type"] = "chassis"
                        chassis_match["chassis_model"] = value
                        return chassis_match
    except Exception:
        logger.debug(f"Chassis inventory fallback failed for device {device_id}", exc_info=True)

    return None


def _determine_device_name(
    libre_device: dict,
    use_sysname: bool = True,
    strip_domain: bool = False,
    device_id: int | str = None,
) -> str:
    """
    Determine the device/VM name from LibreNMS data.

    Centralized logic for building device names with consistent handling of:
    - sysName vs hostname preference
    - Domain stripping (avoiding IP addresses)
    - Fallback to device_id when name is missing

    Args:
        libre_device: Device data from LibreNMS
        use_sysname: If True, prefer sysName; if False, use hostname
        strip_domain: If True, strip domain suffix (e.g., '.example.com')
        device_id: LibreNMS device ID for fallback name generation

    Returns:
        str: The determined device name

    Example:
        >>> _determine_device_name({'sysName': 'router.example.com', 'hostname': 'router'},
        ...                        use_sysname=True, strip_domain=True)
        'router'
    """
    # Determine base name based on use_sysname preference
    if use_sysname:
        name = libre_device.get("sysName") or libre_device.get("hostname")
    else:
        name = libre_device.get("hostname") or libre_device.get("sysName")

    # Fallback to device_id if no name found
    if not name:
        if device_id is not None:
            name = f"device-{device_id}"
        else:
            name = libre_device.get("device_id", "unknown")
            name = f"device-{name}"

    # Strip domain if requested (but not for IP addresses)
    if strip_domain and name and "." in name:
        try:
            parse_host_address(name)
            # It's a valid IP address, don't strip
        except ValueError:
            # Not an IP, safe to strip domain
            name = name.split(".")[0]

    return name


def _flag_ambiguous_librenms_id(result, librenms_id, exc):
    """
    Block import when a librenms_id resolves to more than one NetBox object.

    An ambiguous id is a data-integrity violation; treating it as "not found" would
    let the device import as new (or bind to an arbitrary row), so fail closed
    instead.

    The message is appended to ``issues`` (not just ``warnings``) because the
    readiness step recomputes ``can_import`` from ``issues`` — a warning alone would
    be silently overridden back to importable when no other issue is present.

    Args:
        result (dict): The validation result dict, mutated in place.
        librenms_id: The ambiguous LibreNMS id (for the message text).
        exc: The :class:`AmbiguousLibreNMSIdError` raised during resolution.

    Returns:
        None
    """
    logger.warning("Import validation blocked — ambiguous librenms_id %r: %s", librenms_id, exc)
    result["ambiguous_librenms_id"] = True
    result["can_import"] = False
    if result.get("existing_match_type") != "ambiguous_librenms_id":
        result["existing_match_type"] = "ambiguous_librenms_id"
        message = (
            f"LibreNMS ID {librenms_id} matches more than one existing NetBox record; import "
            "blocked to avoid binding to the wrong object. Resolve the duplicate librenms_id "
            "assignment, then retry."
        )
        result["warnings"].append(message)
        result["issues"].append(message)


def _detect_serial_match_role(existing_by_serial, existing_link, hostname, serial, libre_device, server_key):
    """Decide the role an incoming LibreNMS device plays against a NetBox device matched by serial.

    Pure decision step for the serial-match branch of :func:`validate_device_for_import`:
    reads NetBox/LibreNMS state but does **not** mutate ``result``. Computes whether the
    incoming device is an OOB-controller candidate, a host-promotion, a plain link, or a
    hostname-differs case, and returns the keyword arguments for
    :func:`apply_oob_detection_result` (``serial_action``, ``oob_candidate``,
    ``promote_to_host``, ``serial_role_choice_available``, ``warnings``).

    Args:
        existing_by_serial: the NetBox Device matched by serial.
        existing_link: ``_describe_existing_librenms_link`` dict for *existing_by_serial*.
        hostname: the incoming LibreNMS hostname (already resolved).
        serial: the incoming serial (for warning text only).
        libre_device: the raw LibreNMS device payload.
        server_key: active LibreNMS server key.
    """
    # Compute both possible roles for the incoming LibreNMS device against
    # the existing NetBox device, then pick a heuristic default. The UI
    # offers a manual toggle whenever both roles are feasible so the user
    # can override the heuristic (e.g. mark a "linux"-OS device as OOB or
    # demote an apparent host into the OOB slot).
    oob_type_from_libre = normalize_oob_type(
        libre_device.get("os", ""),
        libre_device.get("hardware", ""),
    )
    existing_oob = get_librenms_oob(existing_by_serial, server_key=server_key)

    # Only treat this as a possible host/OOB chassis-pair situation when
    # there is a real ambiguity: either the existing NetBox device's name
    # differs from the incoming LibreNMS hostname (so they likely represent
    # two sides of one physical box), or the existing device is already
    # linked to a different LibreNMS id. When names match exactly and the
    # existing has no link, the user almost certainly just wants to link.
    names_match = bool(existing_by_serial.name and existing_by_serial.name.lower() == hostname.lower())
    # Normalize to int so that a string device_id from the API
    # (e.g. "17") doesn't cause a false "linked elsewhere" result
    # when compared to the int host_id from coerce_librenms_id.
    normalized_device_id = coerce_librenms_id(libre_device.get("device_id"))
    # Only a real (non-None) incoming id can establish "linked to a DIFFERENT id". A missing or
    # zero device_id normalizes to None, which is unknown — not a mismatch — so it must NOT trip
    # the chassis-pair / host-promotion heuristic (host_id != None is always True and would offer
    # a spurious OOB/Host toggle).
    linked_to_other_id = bool(
        existing_link
        and existing_link["host_id"]
        and normalized_device_id is not None
        and existing_link["host_id"] != normalized_device_id
    )
    already_linked_elsewhere = linked_to_other_id
    # A bare hostname mismatch is NOT enough to treat this as a host/OOB chassis pair: a device
    # reinstalled with a new hostname keeps its chassis serial, so "same serial, new hostname, no
    # link, neither side OOB-flavoured" is a REINSTALL, not a pair — offering "Add as OOB
    # controller" there mis-pairs a reinstalled host with its own stale record. Require a real OOB
    # signal (incoming os/hardware or hostname looks OOB, or the existing device's name looks OOB)
    # or an existing link to a DIFFERENT id before treating a name mismatch as a chassis pair.
    existing_oob_from_name = _detect_oob_type_from_name(existing_by_serial.name)
    incoming_oob_signal = bool(
        oob_type_from_libre
        or _detect_oob_type_from_name(libre_device.get("hostname") or libre_device.get("sysName") or "")
    )
    has_oob_signal = incoming_oob_signal or bool(existing_oob_from_name)
    # A name match normally means "just link" — but not when LibreNMS itself reports the incoming
    # device as an OOB controller (os/hardware → oob_type_from_libre). An iDRAC/iLO/IPMI sharing the
    # host's chassis serial often also shares (or mirrors) its hostname, so gating purely on
    # ``not names_match`` would drop a same-name OOB row into the legacy link path and attach the
    # controller's LibreNMS id as the HOST id. A definitive incoming OOB type is enough on its own to
    # treat this as a chassis pair, regardless of name; the reinstall guard above stays intact
    # because a reinstalled host has no incoming OOB type.
    chassis_pair_likely = (
        already_linked_elsewhere or bool(oob_type_from_libre) or ((not names_match) and has_oob_signal)
    )

    oob_possible = chassis_pair_likely and existing_oob is None
    host_possible = chassis_pair_likely and bool(linked_to_other_id and not existing_link.get("oob_id"))

    # --- Compute all values before mutating result ---
    oob_candidate_data = None
    if oob_possible:
        inferred_oob_type = (
            oob_type_from_libre
            or _detect_oob_type_from_name(libre_device.get("hostname") or libre_device.get("sysName") or "")
            or "oob"
        )
        oob_candidate_data = {
            "device": existing_by_serial,
            "type": inferred_oob_type,
            "version": libre_device.get("version") or None,
            "ip": libre_device.get("ip") or None,
        }

    promote_to_host_data = None
    if host_possible:
        promote_to_host_data = {
            "existing_libre_id": existing_link["host_id"],
            "existing_oob_type": existing_oob_from_name or "oob",
            # Included for bulk-collision detection: lets
            # detect_bulk_collisions identify which NetBox device
            # would be modified without an extra DB round-trip.
            "existing_device": existing_by_serial,
        }

    # Heuristic default: incoming-OS clearly OOB -> oob; otherwise if the
    # existing device's NAME suggests it is the OOB and a host link can be
    # demoted, offer promote; otherwise fall back to whichever is feasible.
    if oob_type_from_libre and oob_possible:
        serial_action_value = "oob_candidate"
    elif host_possible and existing_oob_from_name:
        serial_action_value = "promote_to_host"
    elif oob_possible and host_possible:
        # Both feasible but neither heuristic matches strongly --
        # default to oob_candidate (least-destructive), let the user flip.
        serial_action_value = "oob_candidate"
    elif oob_possible:
        serial_action_value = "oob_candidate"
    elif host_possible:
        serial_action_value = "promote_to_host"
    else:
        serial_action_value = None

    block_warnings: list = []
    if oob_type_from_libre and existing_oob is not None:
        # OOB-typed incoming but existing already has an OOB linked -- inform without blocking.
        # Use a dedicated non-actionable value: "link" would render the generic host-link form
        # ("Link to LibreNMS" button) in device_validation_details.html, posting an
        # indistinguishable host-link request instead of leaving this branch informational.
        serial_action_value = "oob_already_linked"
        block_warnings.append(
            f"Device '{existing_by_serial.name}' already has an OOB controller linked. "
            f"Re-import will update the existing OOB entry."
        )
    elif not oob_possible and not host_possible:
        # Neither role is feasible -- fall back to legacy hostname/serial
        # warning behaviour so the user still sees a useful message.
        if existing_by_serial.name and existing_by_serial.name.lower() == hostname.lower():
            block_warnings.append(
                f"Device with same serial and hostname exists as '{existing_by_serial.name}' "
                f"({_describe_link_note(existing_link)})"
            )
            serial_action_value = "link"
        else:
            block_warnings.append(
                f"Device with same serial ({serial}) exists as '{existing_by_serial.name}' "
                f"but hostname differs (LibreNMS: '{hostname}'). Device may have been reinstalled."
            )
            serial_action_value = "hostname_differs"

    return {
        "serial_action": serial_action_value,
        "oob_candidate": oob_candidate_data,
        "promote_to_host": promote_to_host_data,
        "serial_role_choice_available": oob_possible and host_possible,
        "warnings": block_warnings,
    }


def validate_device_for_import(
    libre_device: dict,
    import_as_vm: bool = False,
    api: "LibreNMSAPI" = None,
    *,
    server_key: str = "default",
    include_vc_detection: bool = True,
    collision_only: bool = False,
    force_vc_refresh: bool = False,
    use_sysname: bool = True,
    strip_domain: bool = False,
    preloaded_device_type_rules: dict | None = None,
) -> dict:
    """
    Validate if a LibreNMS device can be imported to NetBox.

    Performs comprehensive validation:
    - Checks if device already exists in NetBox
    - Validates required prerequisites (Site, DeviceType, DeviceRole for devices)
      OR (Cluster for VMs)
    - Provides smart matching for missing objects
    - Detects virtual chassis/stack configuration (if API provided)
    - Returns detailed validation status

    Args:
        libre_device: Device data from LibreNMS
        import_as_vm: If True, validate for VM import instead of device import
        api: Optional LibreNMSAPI instance for virtual chassis detection
        include_vc_detection: Skip VC detection when False to speed up bulk operations
        collision_only: Return after existing-object and collision-candidate matching. This skips
            site, device type, role, platform, rack, and virtual-chassis import prerequisites.
        force_vc_refresh: When True, bypass cached VC data and re-query LibreNMS
        use_sysname: If True, prefer sysName over hostname (matches import behaviour)
        strip_domain: If True, strip domain suffix from device name

    Returns:
        dict: Validation result with structure (the key set is pinned by
        ``test_validation_result_key_contract``; extend both together):
            {
                'is_ready': bool,  # Can import without user intervention
                'can_import': bool,  # Can import (possibly after configuration)
                'import_as_vm': bool,  # Whether importing as VM (may be flipped True by a VM hostname match)
                'resolved_name': str or None,  # Final device name after applying naming preferences
                'existing_device': Device or VirtualMachine or None,
                'existing_match_type': str or None,  # How it matched: 'librenms_id', 'librenms_oob',
                    # 'hostname', 'serial', 'primary_ip', or the terminal blockers
                    # 'ambiguous_librenms_id' / 'ambiguous_hostname_or_serial'
                'ambiguous_librenms_id': bool,  # librenms_id matches >1 NetBox object (import blocked)
                'existing_librenms_link': dict or None,  # {host_id, oob_id, oob_type} current linkage
                    # of the matched object (devices; VMs get a host_id-only variant)
                'serial_action': str or None,  # None, 'link', 'conflict', 'update_serial',
                    # 'hostname_differs', 'oob_candidate', 'promote_to_host', 'merge_netbox_devices'
                'serial_confirmed': bool,  # librenms_id match and serial matches
                'serial_duplicate': bool,  # Incoming serial already on a different device
                'serial_role_choice_available': bool,  # Both oob_candidate and promote_to_host valid
                'oob_candidate': dict or None,  # {device, type, version, ip} when detected (devices only)
                # 'promote_to_host': dict — CONDITIONAL, present only when host promotion is available
                'merge_candidates': dict or None,  # {host_named: {...}, oob_named: {...}} when two
                    # NetBox devices look like the same physical box (devices only)
                'librenms_id_needs_migration': bool,  # Existing object carries a legacy bare-int id
                'name_matches': bool,  # Existing object's name matches the resolved name
                'name_sync_available': bool,  # Existing object's name differs (sync offered)
                'suggested_name': str or None,  # Name to suggest when name_sync_available
                'device_type_mismatch': bool,  # Existing device's type differs from LibreNMS (devices only)
                'naming_criteria': dict or None,  # How resolved_name was derived (use_sysname/strip_domain)
                'virtual_chassis': dict,  # VC detection state, empty_virtual_chassis_data() shape
                'issues': List[str],  # Blocking issues
                'warnings': List[str],  # Non-blocking warnings
                'site': {  # Only for devices
                    'found': bool,
                    'site': Site or None,
                    'match_type': str,  # 'exact' or None
                    'suggestions': List[Site]  # Alternative suggestions
                },
                'device_type': {  # Only for devices
                    'found': bool,
                    'device_type': DeviceType or None,
                    'match_type': str,  # 'exact' or None
                    'suggestions': List[dict]  # Device types for user selection
                },
                'device_role': {  # Only for devices
                    'found': bool,  # Always False - requires manual selection
                    'role': DeviceRole or None,
                    'available_roles': List[DeviceRole]  # All roles for user selection
                },
                'cluster': {  # Only for VMs
                    'found': bool,  # Always False - requires manual selection
                    'cluster': Cluster or None,
                    'available_clusters': List[Cluster]  # All clusters for user selection
                },
                'platform': {
                    'found': bool,
                    'platform': Platform or None,
                    'match_type': str  # 'exact' or None
                },
                'rack': {  # Only for devices
                    'found': bool,
                    'rack': Rack or None,
                    'available_racks': List[Rack]
                }
            }

    Example:
        >>> validation = validate_device_for_import(libre_device)
        >>> if validation['is_ready']:
        ...     import_single_device(libre_device['device_id'])
    """
    result = {
        "is_ready": False,
        "can_import": False,
        "import_as_vm": import_as_vm,
        "resolved_name": None,  # Final device name after applying user preferences
        "existing_device": None,
        "existing_match_type": None,  # Track how existing device was matched
        "ambiguous_librenms_id": False,  # True when the librenms_id matches >1 NetBox object (import blocked)
        "serial_action": None,  # None, "link", "conflict", "update_serial", "hostname_differs", "oob_candidate", "promote_to_host", "merge_netbox_devices"
        "serial_confirmed": False,  # True when librenms_id match and serial matches
        "serial_duplicate": False,  # True when incoming serial is already on a different device
        "serial_role_choice_available": False,  # True when both oob_candidate and promote_to_host are valid choices
        "librenms_id_needs_migration": False,  # True when existing device has legacy bare-int ID
        "oob_candidate": None,  # dict {device, type, version, ip} when oob_candidate detected
        # promote_to_host is only set when the host-promotion path is available; absent otherwise.
        "existing_librenms_link": None,  # dict {host_id, oob_id, oob_type} describing existing device's current LibreNMS linkage
        "merge_candidates": None,  # dict {host_named: {pk,name,librenms_link}, oob_named: {pk,name,librenms_link}} when two NB devices look like the same physical box
        "name_matches": False,  # True when existing device name matches LibreNMS sysName
        "name_sync_available": False,  # True when existing device name differs from sysName
        "suggested_name": None,  # sysName to suggest when name_sync_available is True
        "device_type_mismatch": False,  # True when existing device's type differs from LibreNMS
        "issues": [],
        "warnings": [],
        "virtual_chassis": empty_virtual_chassis_data(),
        "site": {
            "found": False,
            "site": None,
            "match_type": None,
            "suggestions": [],
        },
        "device_type": {
            "found": False,
            "device_type": None,
            "match_type": None,
            "suggestions": [],
        },
        "device_role": {
            "found": False,
            "role": None,
            "available_roles": [],
        },
        "cluster": {
            "found": False,
            "cluster": None,
            "available_clusters": [],
        },
        "platform": {"found": False, "platform": None, "match_type": None},
        "rack": {
            "found": False,
            "rack": None,
            "available_racks": [],
        },
        "naming_criteria": None,  # Populated after resolved_name is set
    }

    try:
        # 1. Check if device/VM already exists in NetBox
        # Always check both Devices AND VMs to properly detect existing objects
        librenms_id = libre_device.get("device_id")
        hostname = _determine_device_name(
            libre_device,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
            device_id=librenms_id,
        )
        result["resolved_name"] = hostname
        _raw_sysname = libre_device.get("sysName") or ""
        _raw_hostname = libre_device.get("hostname") or ""
        if not _raw_sysname and not _raw_hostname:
            _source = f"device-{librenms_id}"
        elif use_sysname:
            _source = "sysname" if _raw_sysname else "hostname"
        else:
            _source = "hostname" if _raw_hostname else "sysname"
        result["naming_criteria"] = {
            "use_sysname": use_sysname,
            "strip_domain": strip_domain,
            "raw_sysname": _raw_sysname,
            "raw_hostname": _raw_hostname,
            "source": _source,
        }
        logger.debug(
            f"Checking for existing device/VM: "
            f"librenms_id={librenms_id} (type={type(librenms_id).__name__}), "
            f"hostname={hostname}"
        )

        from virtualization.models import VirtualMachine

        server_key = api.server_key if api is not None else server_key

        # Check for existing VM first (by librenms_id custom field).
        # find_by_librenms_id() covers both the new per-server JSON format
        # and legacy bare-integer values so neither is missed. An ambiguous id
        # (matching multiple records) blocks the import — see _flag_ambiguous_librenms_id.
        try:
            existing_vm = find_by_librenms_id(VirtualMachine, librenms_id, server_key)
        except AmbiguousLibreNMSIdError as exc:
            existing_vm = None
            _flag_ambiguous_librenms_id(result, librenms_id, exc)

        # Cross-model collision: the same librenms_id on both a VM and a Device is ambiguous.
        # Without this, the VM lookup wins and the Device lookup below is skipped, silently
        # binding to the VM. Detect it and fail closed (the device block is gated on the flag).
        if existing_vm is not None:
            try:
                _device_collision = find_by_librenms_id(Device, librenms_id, server_key)
            except AmbiguousLibreNMSIdError as exc:
                # An ambiguous device lookup is itself a fail-closed condition: drop the
                # VM binding too so the block below cannot rebind it as a definitive
                # "librenms_id" match (the import is already flagged ambiguous).
                _device_collision = None
                _flag_ambiguous_librenms_id(result, librenms_id, exc)
                existing_vm = None
            if _device_collision is not None:
                _flag_ambiguous_librenms_id(result, librenms_id, "matches both a VirtualMachine and a Device")
                existing_vm = None

        if existing_vm:
            logger.info(f"Found existing VM: {existing_vm.name} (matched by librenms_id={librenms_id})")
            result["existing_device"] = existing_vm
            result["existing_match_type"] = "librenms_id"
            result["import_as_vm"] = True  # Force VM mode since VM exists
            result["can_import"] = False
            # Surface the host/OOB linkage so a librenms_id-matched VM renders as linked
            # (mirrors the device path); otherwise the UI shows the VM as unlinked.
            result["existing_librenms_link"] = _describe_existing_librenms_link(existing_vm, server_key)

            # Detect legacy bare-integer or string-digit format so UI can offer a migration action.
            # Direct access needed to detect legacy format for migration prompt:
            # LibreNMSAPI.get_librenms_id() returns an int in both formats, so only the
            # raw type check on custom_field_data reveals whether migration is needed.
            _vm_cf_id = existing_vm.custom_field_data.get("librenms_id")
            if is_legacy_librenms_id(_vm_cf_id):
                result["librenms_id_needs_migration"] = True

            # Check if name matches resolved name (accounts for use_sysname/strip_domain)
            if hostname and existing_vm.name == hostname:
                result["name_matches"] = True
            elif hostname and existing_vm.name != hostname:
                result["name_sync_available"] = True
                result["suggested_name"] = hostname

        # Check for existing Device (by librenms_id custom field).
        # find_by_librenms_id() covers both the new per-server JSON format
        # and legacy bare-integer values so neither is missed. Skip when an ambiguity
        # (intra-model or cross-model) was already flagged — binding must fail closed.
        if not result["existing_device"] and not result["ambiguous_librenms_id"]:
            try:
                existing_device = find_by_librenms_id(Device, librenms_id, server_key)
            except AmbiguousLibreNMSIdError as exc:
                existing_device = None
                _flag_ambiguous_librenms_id(result, librenms_id, exc)

            if existing_device:
                logger.info(f"Found existing device: {existing_device.name} (matched by librenms_id={librenms_id})")
                result["existing_device"] = existing_device
                result["existing_match_type"] = "librenms_id"
                # A Device matched: force Device mode so a user-selected VM mode doesn't carry a
                # Device-mapped row into the VM validation/UI path (the VM branches above set
                # import_as_vm=True; the Device branches must symmetrically set it False).
                result["import_as_vm"] = False
                result["can_import"] = False

                # If the match was via the OOB sub-key, mark it so the UI shows no duplicate warning.
                _existing_oob = get_librenms_oob(existing_device, server_key=server_key)
                if _existing_oob and coerce_librenms_id(_existing_oob.get("id")) == coerce_librenms_id(librenms_id):
                    result["existing_match_type"] = "librenms_oob"

                # Surface the full host/OOB linkage so the import table can render
                # both halves of an existing pair with consistent paired styling.
                result["existing_librenms_link"] = _describe_existing_librenms_link(existing_device, server_key)

                # Detect legacy bare-integer or string-digit format so UI can offer a migration action.
                # Direct access needed to detect legacy format for migration prompt:
                # LibreNMSAPI.get_librenms_id() returns an int in both formats, so only the
                # raw type check on custom_field_data reveals whether migration is needed.
                _dev_cf_id = existing_device.custom_field_data.get("librenms_id")
                if is_legacy_librenms_id(_dev_cf_id):
                    result["librenms_id_needs_migration"] = True

                # Check if name matches resolved name (VC-aware: compare against VC member name)
                if hostname and existing_device.virtual_chassis and existing_device.vc_position:
                    incoming_serial = normalize_serial(libre_device.get("serial"))
                    if incoming_serial == "-":
                        incoming_serial = ""
                    vc_expected_name = _generate_vc_member_name(
                        hostname,
                        existing_device.vc_position,
                        # Trim the stored fallback too, or a padded serial mints a bogus name.
                        serial=incoming_serial or normalize_serial(existing_device.serial),
                    )
                    if existing_device.name == vc_expected_name:
                        result["name_matches"] = True
                    else:
                        result["name_sync_available"] = True
                        result["suggested_name"] = vc_expected_name
                elif hostname and existing_device.name == hostname:
                    result["name_matches"] = True
                elif hostname and existing_device.name != hostname:
                    result["name_sync_available"] = True
                    result["suggested_name"] = hostname

                # Check for serial drift on the linked device. Skip when the match was via the
                # OOB sub-key (existing_match_type == "librenms_oob"): the incoming payload is the
                # OOB controller's, so comparing it against the host record's serial would surface
                # bogus replacement/conflict warnings on a row that is already correctly linked.
                incoming_serial = normalize_serial(libre_device.get("serial"))
                # Normalize the stored side too: a legacy padded serial equals the incoming one.
                stored_serial = normalize_serial(existing_device.serial)
                if result["existing_match_type"] != "librenms_oob" and incoming_serial and incoming_serial != "-":
                    if stored_serial and stored_serial == incoming_serial:
                        result["serial_confirmed"] = True
                    elif stored_serial and stored_serial != incoming_serial:
                        serial_conflict = (
                            Device.objects.filter(serial=incoming_serial).exclude(pk=existing_device.pk).first()
                        )
                        if serial_conflict:
                            result["serial_action"] = "conflict"
                            result["serial_duplicate"] = True
                            result["warnings"].append(
                                f"Serial conflict: incoming serial '{incoming_serial}' is already assigned to "
                                f"device '{serial_conflict.name}' (ID: {serial_conflict.pk}) in NetBox. "
                                f"Investigate which device should own this serial before updating."
                            )
                        else:
                            result["serial_action"] = "update_serial"
                            result["warnings"].append(
                                f"Serial number differs (NetBox: '{existing_device.serial}', "
                                f"LibreNMS: '{incoming_serial}'). Hardware may have been replaced."
                            )

        # Only check hostname/serial/IP if not already matched by librenms_id.
        # Skip when an ambiguous librenms_id was flagged — hostname/serial/IP matching
        # would otherwise rebind existing_device/existing_match_type and defeat the
        # fail-closed ambiguity contract (mirrors the librenms_id block guard above).
        if not result["existing_device"] and not result["ambiguous_librenms_id"]:
            # Check by hostname/name - Check both VMs and Devices for conflicts
            existing_vm = VirtualMachine.objects.filter(name__iexact=hostname).first()
            existing_device = Device.objects.filter(name__iexact=hostname).first()

            # If BOTH exist with same hostname, it's ambiguous - don't match either
            if existing_vm and existing_device:
                logger.warning(
                    f"Hostname conflict: Both VM '{existing_vm.name}' and Device "
                    f"'{existing_device.name}' exist with hostname '{hostname}'"
                )
                result["warnings"].append(
                    f"Both a VM and Device exist with hostname '{hostname}' in NetBox. "
                    f"Cannot determine which to match. Please set the librenms_id custom field on the correct object."
                )
                # Fail closed when EITHER side ALSO has same-model duplicates. Nothing binds in
                # this branch, so the Stage-1 duplicate guard below (keyed on a bound
                # existing_device) never sees them — without this, two Devices plus one VM on a
                # hostname would sail through as a new import, i.e. the VM's presence would
                # RELAX the terminal protection the 2-Devices-no-VM case gets. Mirrors that
                # guard's terminal state (message keeps the "hostname/serial" substring the
                # refresh-path blocker cleanup keys on), including its early return: a
                # hostname-bound duplicate never falls through to serial/IP re-binding either.
                _device_peers = list(Device.objects.filter(name__iexact=hostname)[:2])
                _vm_peers = list(VirtualMachine.objects.filter(name__iexact=hostname)[:2])
                if len(_device_peers) > 1 or len(_vm_peers) > 1:
                    _dup_kind = "devices" if len(_device_peers) > 1 else "virtual machines"
                    _dup_msg = (
                        f"Multiple NetBox {_dup_kind} share this device's hostname/serial; resolve the "
                        "duplicate before importing or linking."
                    )
                    if _dup_msg not in result.setdefault("issues", []):
                        result["issues"].append(_dup_msg)
                    result["existing_match_type"] = "ambiguous_hostname_or_serial"
                    result["can_import"] = False
                    result["is_ready"] = False
                    return result
                # Don't set existing_device, don't block import - let user proceed as new
                # This allows them to import and then resolve the conflict manually
            elif existing_vm:
                logger.info(f"Found existing VM by hostname: {existing_vm.name}")
                result["existing_device"] = existing_vm
                result["existing_match_type"] = "hostname"
                result["import_as_vm"] = True  # Force VM mode since VM exists
                # Describe the VM's current LibreNMS linkage and word the warning to match it —
                # a hostname-matched VM can already carry a librenms_id (to a different id/server),
                # so a flat "not linked" would contradict the badge. Mirrors the primary-IP path.
                existing_link = _describe_existing_librenms_link(existing_vm, server_key)
                result["existing_librenms_link"] = existing_link
                link_note = _describe_link_note(existing_link)
                result["warnings"].append(
                    f"VM with same hostname exists in NetBox as '{existing_vm.name}' ({link_note})"
                )
                result["can_import"] = False
            elif existing_device:
                logger.info(f"Found existing device by hostname: {existing_device.name}")
                result["existing_device"] = existing_device
                result["existing_match_type"] = "hostname"
                # Force Device mode (see the librenms_id branch above): a hostname match to a
                # Device must not leave a user-selected VM mode active.
                result["import_as_vm"] = False
                # Surface the current host/OOB linkage so a hostname-matched device that
                # is already linked to LibreNMS isn't mislabelled as "not linked".
                result["existing_librenms_link"] = _describe_existing_librenms_link(existing_device, server_key)

                # Check for serial conflict on hostname-matched device. Both sides are
                # normalized so a legacy padded stored serial isn't read as drift.
                incoming_serial = normalize_serial(libre_device.get("serial"))
                stored_serial = normalize_serial(existing_device.serial)
                if incoming_serial and incoming_serial != "-" and stored_serial != incoming_serial:
                    serial_conflict = (
                        Device.objects.filter(serial=incoming_serial).exclude(pk=existing_device.pk).first()
                    )
                    if serial_conflict:
                        result["serial_action"] = "conflict"
                        result["serial_duplicate"] = True
                        result["warnings"].append(
                            f"Serial conflict: incoming serial '{incoming_serial}' is already assigned to "
                            f"device '{serial_conflict.name}' (ID: {serial_conflict.pk}) in NetBox. "
                            f"Investigate which device should own this serial before importing."
                        )
                    else:
                        result["serial_action"] = "update_serial"
                        result["warnings"].append(
                            f"Hostname matches but serial differs (NetBox: '{existing_device.serial}', "
                            f"LibreNMS: '{incoming_serial}'). Hardware may have been replaced."
                        )
                else:
                    link_note = _describe_link_note(result["existing_librenms_link"])
                    result["warnings"].append(
                        f"Device with same hostname exists in NetBox as '{existing_device.name}' ({link_note})"
                    )

                result["can_import"] = False

            # Check by serial number (strong physical match - hardware identity)
            if not result["existing_device"]:
                # Normalize the incoming serial before the exact indexed lookup. Migration 0012
                # canonicalizes legacy Device rows, and every plugin write path stores this same
                # normalized form, so matching and persistence share one representation.
                serial = normalize_serial(libre_device.get("serial"))
                if serial and serial != "-" and not import_as_vm:
                    # Serial is not unique in NetBox, so .first() would bind an arbitrary row
                    # and the downstream serial/OOB/merge flow would derive its guidance from a
                    # random device. Require a unique match before binding, mirroring the
                    # merge-peer [:2] guard (issue #101).
                    serial_matches = list(Device.objects.filter(serial=serial)[:2])
                    if len(serial_matches) > 1:
                        # Device.serial is not unique in NetBox, so several rows already share it.
                        # Binding to an arbitrary one is wrong, and importing anyway would mint YET
                        # ANOTHER same-serial device. Make it a blocking issue (issue -> can_import
                        # False) and flag the duplicate, rather than only warning and letting the row
                        # import. Not serial_action="conflict": there is no single peer to resolve
                        # against, so the sync-serial UI (which needs one existing_device) can't apply.
                        result["serial_duplicate"] = True
                        result["issues"].append(
                            f"Multiple NetBox devices share serial '{serial}'; resolve the duplicate "
                            "serial before importing."
                        )
                        existing_by_serial = None
                    else:
                        existing_by_serial = serial_matches[0] if serial_matches else None
                    if existing_by_serial:
                        logger.info(f"Found existing device by serial: {existing_by_serial.name} (serial={serial})")
                        result["existing_device"] = existing_by_serial
                        result["existing_match_type"] = "serial"
                        result["can_import"] = False

                        # Capture existing device's current LibreNMS linkage so the UI can
                        # present accurate state (NOT just "not linked to LibreNMS").
                        existing_link = _describe_existing_librenms_link(existing_by_serial, server_key)
                        result["existing_librenms_link"] = existing_link

                        # Decide OOB-candidate / promote-to-host / link / hostname-differs role
                        # for the incoming device (pure: reads state, doesn't mutate result),
                        # then apply the decision.
                        apply_oob_detection_result(
                            result,
                            **_detect_serial_match_role(
                                existing_by_serial, existing_link, hostname, serial, libre_device, server_key
                            ),
                        )

            # Refresh local variable to reflect any VM-mode adjustments made during detection
            # (e.g. existing VM found by hostname sets result["import_as_vm"] = True).
            # Must happen before the merge-candidates block below so a VM hostname-match
            # doesn't fall through to Device-only merge logic.
            import_as_vm = result["import_as_vm"]

            # Fail closed against an ARBITRARY duplicate match for the current side, independent of
            # whether a usable serial is available. The hostname/serial match above used .first(),
            # so with duplicate NetBox names/serials result["existing_device"] may be an arbitrary
            # row and acting on it (link/promote/import) would target the wrong device. This must
            # run even for a hostname match with no serial — the merge-candidate block below is
            # gated on a serial (it pairs host+OOB) and would otherwise skip the check entirely.
            # Compute the duplicate-detection peer lists ONCE: the Stage-1 duplicate guard here and
            # the Stage-2 merge-candidate detection below both run the identical UNIQUE [:2] query
            # for the matched type, so share the result instead of issuing it twice per device.
            _match_type = result.get("existing_match_type")
            _serial_now = normalize_serial(libre_device.get("serial"))
            _dup_eligible = result.get("existing_device") is not None and _match_type in ("hostname", "serial")
            # VM matches are just as vulnerable: NetBox only enforces VM-name uniqueness per
            # cluster, so the VM hostname match above binds .first() among cross-cluster
            # duplicates. Pick the peer model from the object that ACTUALLY matched, not from
            # import_as_vm: the hostname fallback can bind a Device even when the caller requested
            # a VM import (import_as_vm=True passed by vm_operations), so keying on import_as_vm
            # would query the wrong table and miss same-name duplicates on the matched side. The
            # serial path is device-only.
            _matched_is_vm = isinstance(result.get("existing_device"), VirtualMachine)
            _PeerModel = VirtualMachine if _matched_is_vm else Device
            _hostname_peers = (
                list(_PeerModel.objects.filter(name__iexact=hostname)[:2])
                if _dup_eligible and _match_type == "hostname" and hostname
                else []
            )
            _serial_peers = (
                # Reuse the Stage-1 serial [:2] result (issue #101 guard, computed above) instead of
                # re-issuing the identical UNIQUE query — keeps the single-query invariant while the
                # fail-closed guard stays in place. Only defined when existing_match_type == "serial".
                serial_matches
                if _dup_eligible and _match_type == "serial" and _serial_now and _serial_now != "-"
                else []
            )
            if _dup_eligible:
                _dup_current = False
                if _match_type == "hostname" and hostname:
                    _dup_current = len(_hostname_peers) > 1
                elif _match_type == "serial":
                    _dup_current = bool(_serial_now) and _serial_now != "-" and len(_serial_peers) > 1
                if _dup_current:
                    # Arbitrary .first() match among duplicates: block link/promote/import and
                    # surface a blocking issue. The match is left for display only.
                    # Drop the arbitrary existing_device and all match-derived linkage/name state:
                    # this is a terminal ambiguity, so retaining the .first() row would let
                    # bulk_import treat it as a real existing match — `_refresh_existing_device`
                    # short-circuits on a set existing_device (skipping the ambiguity re-check) and
                    # the exclude_existing / collision paths key off it — pinning the row to the
                    # wrong device. Only existing_match_type carries the ambiguity forward.
                    result["existing_device"] = None
                    result["existing_librenms_link"] = None
                    clear_match_derived_action_fields(result)
                    # Demote the match_type off "hostname"/"serial" so neither the device_status
                    # table (has_actions) nor device_validation_details.html renders a "Link to
                    # LibreNMS" action — otherwise the arbitrary row could be linked to the wrong
                    # NetBox device. Mirrors the "ambiguous_librenms_id" terminal-state pattern.
                    result["existing_match_type"] = "ambiguous_hostname_or_serial"
                    result["can_import"] = False
                    result["is_ready"] = False
                    # Both wordings keep the "hostname/serial" substring the refresh-path
                    # blocker cleanup keys on (_AMBIGUOUS_SERIAL_IP_MARKERS in bulk_import).
                    _dup_kind = "virtual machines" if _matched_is_vm else "devices"
                    _dup_msg = (
                        f"Multiple NetBox {_dup_kind} share this device's hostname/serial; resolve the "
                        "duplicate before importing or linking."
                    )
                    if _dup_msg not in result.setdefault("issues", []):
                        result["issues"].append(_dup_msg)
                    # Terminal, like the ambiguous_librenms_id and primary-IP-ambiguity guards:
                    # return now. We cleared existing_device above, so without this the
                    # primary-IP fallback pass (and the new-import validation) below would run
                    # and re-bind existing_device + demote match_type to "primary_ip" — silently
                    # re-homing this duplicate-hostname/serial row onto an arbitrary IP-matched
                    # device and dropping the terminal blocker the cleanup keys on.
                    return result

            # Stage 2 — merge-candidates detection.
            # When the hostname-matched device and the serial-matched device are
            # DIFFERENT NetBox objects, the two probably represent the same
            # physical box (host + OOB) imported as separate entries. Surface
            # this as a merge action instead of silently picking one.
            try:
                _serial_for_pair = normalize_serial(libre_device.get("serial"))
                if (
                    _serial_for_pair
                    and _serial_for_pair != "-"
                    and not import_as_vm
                    and result.get("existing_device") is not None
                    and result.get("existing_match_type") in ("hostname", "serial")
                ):
                    # The CURRENT side comes from result["existing_device"], which an earlier
                    # hostname/serial match set via .first() — so with duplicate NetBox names or
                    # serials it could be an arbitrary row, pairing the user with the wrong merge
                    # target. Re-validate the current side with the same UNIQUE [:2] guard used for
                    # the peer below: keep it as a candidate only when exactly one row matches,
                    # otherwise skip the merge suggestion and warn.
                    _hostname_match = None
                    _serial_match = None
                    # A non-unique current side (duplicate name/serial) is already failed closed
                    # above; here it just means we can't pick a single peer to pair, so skip the
                    # merge suggestion and warn.
                    if result.get("existing_match_type") == "hostname" and hostname:
                        # Reuse the Stage-1 peer list (identical name__iexact[:2] query).
                        if len(_hostname_peers) == 1:
                            _hostname_match = _hostname_peers[0]
                        elif len(_hostname_peers) > 1:
                            result["warnings"].append(
                                f"Multiple NetBox devices share hostname '{hostname}'; merge suggestion skipped."
                            )
                    elif result.get("existing_match_type") == "serial":
                        # Reuse the Stage-1 peer list (identical serial[:2] query).
                        if len(_serial_peers) == 1:
                            _serial_match = _serial_peers[0]
                        elif len(_serial_peers) > 1:
                            result["warnings"].append(
                                f"Multiple NetBox devices share serial '{_serial_for_pair}'; merge suggestion skipped."
                            )
                    # Whichever path landed first, look the other one up too. Require a UNIQUE
                    # peer: serial isn't unique in NetBox (and names are only unique per site),
                    # so a bare .first() could pair the matched device with an arbitrary row and
                    # surface the wrong merge target. Fetch up to 2; only pair on exactly one,
                    # otherwise skip the suggestion and warn.
                    if _hostname_match and not _serial_match:
                        _serial_peers = list(
                            Device.objects.filter(serial=_serial_for_pair).exclude(pk=_hostname_match.pk)[:2]
                        )
                        if len(_serial_peers) == 1:
                            _serial_match = _serial_peers[0]
                        elif len(_serial_peers) > 1:
                            result["warnings"].append(
                                f"Multiple NetBox devices share serial '{_serial_for_pair}'; merge suggestion skipped."
                            )
                    elif _serial_match and not _hostname_match and hostname:
                        _hostname_peers = list(
                            Device.objects.filter(name__iexact=hostname).exclude(pk=_serial_match.pk)[:2]
                        )
                        if len(_hostname_peers) == 1:
                            _hostname_match = _hostname_peers[0]
                        elif len(_hostname_peers) > 1:
                            result["warnings"].append(
                                f"Multiple NetBox devices share hostname '{hostname}'; merge suggestion skipped."
                            )

                    if _hostname_match and _serial_match and _hostname_match.pk != _serial_match.pk:
                        host_link = _describe_existing_librenms_link(_hostname_match, server_key)
                        oob_link = _describe_existing_librenms_link(_serial_match, server_key)
                        # Conservative guard: at least one side must already be linked,
                        # otherwise this is more likely two unrelated devices that share
                        # serial data by coincidence (test fixtures, mis-keyed assets).
                        # A LibreNMS link counts whether it's a host link or an OOB link.
                        if any(link and (link.get("host_id") or link.get("oob_id")) for link in (host_link, oob_link)):
                            apply_merge_candidates(
                                result,
                                host_named={
                                    "pk": _hostname_match.pk,
                                    "name": _hostname_match.name,
                                    "librenms_link": host_link,
                                    # Carry the concrete model so bulk-collision detection keys the
                                    # right bucket. Stage-2 merge is Device-only today (gated on
                                    # not import_as_vm), but reading it from the object future-proofs
                                    # a VM-side merge instead of assuming "device".
                                    "model_name": _hostname_match._meta.model_name,
                                },
                                oob_named={
                                    "pk": _serial_match.pk,
                                    "name": _serial_match.name,
                                    "librenms_link": oob_link,
                                    "model_name": _serial_match._meta.model_name,
                                },
                                warning=(
                                    f"Two NetBox devices appear to represent this physical box: "
                                    f"'{_hostname_match.name}' (matches LibreNMS hostname) and "
                                    f"'{_serial_match.name}' (matches chassis serial). "
                                    f"Choose which one to keep and merge the other into it."
                                ),
                            )

            except Exception:  # pragma: no cover - defensive: never break validation
                logger.exception("merge-candidate detection failed")

            # Check by primary IP (weaker match, IP could be reassigned) - only for devices
            if not result["existing_device"]:
                primary_ip = libre_device.get("ip")
                if primary_ip and not import_as_vm:
                    # Shared resolver: scans every net_host row across the interface-assignment and
                    # oob_ip-FK paths and fails closed on >1 distinct device (mirrors
                    # _refresh_existing_device in bulk_import.py via the same helper).
                    device, ip_ambiguous, matching_ips = resolve_device_by_host_ip(primary_ip)
                    if ip_ambiguous:
                        # Duplicate net_host rows point at >1 distinct NetBox device — binding to
                        # an arbitrary one could re-home the import to the wrong device, so fail
                        # closed (mirrors the librenms_id cross-model ambiguity guard above).
                        # Put this collision into the same terminal ambiguity state the
                        # hostname/serial guard uses, so a cached primary-IP collision can be
                        # cleared by _refresh_existing_device() once the duplicate IP assignment is
                        # resolved. The message carries the shared "serial or management IP" marker
                        # the refresh cleanup strips on; without the match_type + marker the stale
                        # blocker would survive refresh and keep the row blocked until cache expiry.
                        result["existing_match_type"] = "ambiguous_hostname_or_serial"
                        result["issues"].append(
                            f"Multiple NetBox devices match this device's serial or management IP "
                            f"(IP address {primary_ip}); resolve the duplicate assignment before importing."
                        )
                        result["can_import"] = False
                        result["is_ready"] = False
                        # Terminal, like the ambiguous_librenms_id guard: return now so the
                        # new-import validation below doesn't append unrelated site/role/device-type
                        # blockers to a row that's already blocked on the duplicate-IP ambiguity.
                        return result
                    elif device:
                        # Surface any existing host/OOB linkage so the import UI renders the
                        # correct row state (the librenms_id / serial branches do the same;
                        # without this an already-linked device shows as "not linked" here).
                        result["existing_librenms_link"] = _describe_existing_librenms_link(device, server_key)
                        # Check if this is an OOB candidate via the IP path.
                        # The OOB controller's IP may already be the device's oob_ip, or the
                        # LibreNMS device may identify itself as an OOB type (iDRAC/iLO/etc.).
                        oob_type = normalize_oob_type(
                            libre_device.get("os", ""),
                            libre_device.get("hardware", ""),
                        )
                        # Check the device's oob_ip against EVERY row sharing this host address,
                        # not just existing_ip (.first()): with duplicate net_host rows the device's
                        # oob_ip may be a different matching row, and comparing only the first would
                        # wrongly read is_oob_ip as False.
                        is_oob_ip = device.oob_ip_id is not None and matching_ips.filter(pk=device.oob_ip_id).exists()
                        has_primary_ip = bool(device.primary_ip4_id or device.primary_ip6_id)
                        # When the incoming IP already IS the device's oob_ip this is an OOB
                        # candidate regardless of whether the LibreNMS os/hardware tokens let us
                        # classify a type. Requiring oob_type here silently downgrades such a row
                        # to a plain primary-IP match and loses the OOB action flow, so infer a
                        # type from the hostname (or a generic "oob" fallback), mirroring the
                        # serial-match branch above.
                        inferred_oob_type = (
                            oob_type
                            or _detect_oob_type_from_name(
                                libre_device.get("hostname") or libre_device.get("sysName") or ""
                            )
                            or "oob"
                        )
                        if is_oob_ip or (oob_type and not has_primary_ip):
                            existing_oob = get_librenms_oob(device, server_key=server_key)
                            if existing_oob is None:
                                result["existing_device"] = device
                                result["existing_match_type"] = "primary_ip"
                                result["serial_action"] = "oob_candidate"
                                result["oob_candidate"] = {
                                    "device": device,
                                    "type": inferred_oob_type,
                                    "version": libre_device.get("version") or None,
                                    "ip": libre_device.get("ip") or None,
                                }
                                result["can_import"] = False
                            else:
                                result["existing_device"] = device
                                result["existing_match_type"] = "primary_ip"
                                result["warnings"].append(
                                    f"IP address {primary_ip} already assigned to device '{device.name}' "
                                    f"(OOB already linked)"
                                )
                                result["can_import"] = False
                        else:
                            result["existing_device"] = device
                            result["existing_match_type"] = "primary_ip"
                            # Line 728 may already have populated a host/OOB
                            # linkage; describe it accurately instead of always
                            # claiming "not linked to LibreNMS".
                            link_note = _describe_link_note(result.get("existing_librenms_link"))
                            result["warnings"].append(
                                f"IP address {primary_ip} already assigned to device '{device.name}' ({link_note})"
                            )
                            result["can_import"] = False

        # Refresh local mode after ALL detection branches. The refresh at the top of the
        # unmatched-device block only runs when nothing matched by librenms_id; an existing
        # VM matched directly by librenms_id (above) sets result["import_as_vm"]=True but
        # skips that block, so without this a linked VM would wrongly take the Device path
        # (missing cluster["available_clusters"], running device-only validation/VC detection).
        import_as_vm = result["import_as_vm"]

        # An ambiguous librenms_id (matches >1 NetBox record) is the terminal blocker for this
        # row — the user must resolve the duplicate id. Don't run the new-import site/device_type/
        # role/cluster validation below: with existing_device fail-closed to None, it would pile
        # unrelated "must select ..." blockers onto a row whose real problem is the ambiguous id
        # (mirrors bulk_import.py treating ambiguity as the terminal state). existing_match_type
        # is already "ambiguous_librenms_id" (set by _flag_ambiguous_librenms_id).
        if result["ambiguous_librenms_id"]:
            result["can_import"] = False
            result["is_ready"] = False
            return result

        if collision_only:
            return result

        # Validate based on import type (Device or VM)
        if import_as_vm:
            # Always populate available clusters for all VMs (new or existing) so
            # the cluster dropdown has options whether creating or updating a VM.
            cache_key = "librenms_import_all_clusters"
            all_clusters = cache.get(cache_key)
            if all_clusters is None:
                all_clusters = list(Cluster.objects.all())
                # Use API cache timeout if available, otherwise use default 5 minutes
                cache_timeout = api.cache_timeout if api else 300
                cache.set(cache_key, all_clusters, cache_timeout)
            result["cluster"]["available_clusters"] = all_clusters

        if import_as_vm:
            if not result.get("existing_device"):
                # 2. For NEW VMs: Validate Cluster (required) - Must be manually selected
                result["cluster"]["found"] = False
                result["issues"].append("Cluster must be manually selected before importing as VM")

            # Skip device-specific validations for all VMs (new and existing)
            result["site"]["found"] = True  # Not required for VMs
            result["device_type"]["found"] = True  # Not required for VMs
            result["device_role"]["found"] = True  # Not required for VMs

        else:
            # 2. For Devices: Validate Site (required)
            location = libre_device.get("location", "")
            site_match = find_matching_site(location)
            result["site"] = site_match

            if not site_match["found"]:
                if not result.get("existing_device"):
                    result["issues"].append(f"No matching site found for location: '{location}'")
                # Get alternative suggestions
                if location:
                    all_sites = Site.objects.all()[:10]  # Limit for performance
                    result["site"]["suggestions"] = list(all_sites)

            # 3. Validate DeviceType (required)
            hardware = libre_device.get("hardware", "")
            dt_match = match_librenms_hardware_to_device_type(hardware, preloaded_rules=preloaded_device_type_rules)

            if dt_match is None:
                result["device_type"]["found"] = False
                result["device_type"]["device_type"] = None
                result["device_type"]["match_type"] = "ambiguous"
                if not result.get("existing_device"):
                    result["issues"].append(
                        f"Multiple device types match hardware '{hardware}' — resolve the ambiguity in NetBox."
                    )
            else:
                # Chassis inventory fallback: when hardware doesn't match,
                # try the chassis entPhysicalModelName as an additional lookup source
                if not dt_match["matched"] and api:
                    device_id = libre_device.get("device_id")
                    if device_id:
                        chassis_match = _try_chassis_device_type_match(
                            api, device_id, preloaded_device_type_rules=preloaded_device_type_rules
                        )
                        if chassis_match and chassis_match["matched"]:
                            dt_match = chassis_match

                # Update result keys individually to preserve the existing schema (especially "found")
                result["device_type"]["found"] = dt_match["matched"]
                result["device_type"]["device_type"] = dt_match.get("device_type")
                result["device_type"]["match_type"] = dt_match.get("match_type")

            if not result["device_type"]["found"] and result["device_type"].get("match_type") != "ambiguous":
                result["device_type"]["found"] = False
                if not result.get("existing_device"):
                    result["issues"].append(f"No matching device type found for hardware: '{hardware}'")
                # Get some device types for user to choose from
                all_device_types = DeviceType.objects.all()[:10]
                result["device_type"]["suggestions"] = [
                    {
                        "device_type": dt,
                        "similarity": 0.0,  # No fuzzy matching, just showing options
                        "match_field": None,
                    }
                    for dt in all_device_types
                ]

            if not result.get("existing_device"):
                # 4. DeviceRole (required for new devices) - Must be manually selected
                logger.debug(f"[{hostname}] Issues BEFORE adding role issue: {result['issues']}")
                result["device_role"]["found"] = False
                result["issues"].append("Device role must be manually selected before import")
                logger.debug(f"[{hostname}] Issues AFTER adding role issue: {result['issues']}")
            # Provide list of available roles for user selection (cached)
            cache_key = "librenms_import_all_roles"
            all_roles = cache.get(cache_key)
            if all_roles is None:
                all_roles = list(DeviceRole.objects.all())
                # Use API cache timeout if available, otherwise use default 5 minutes
                cache_timeout = api.cache_timeout if api else 300
                cache.set(cache_key, all_roles, cache_timeout)
            result["device_role"]["available_roles"] = all_roles

            # 4b. Rack (optional) - Provide available racks for the matched site
            if site_match["found"] and site_match["site"]:
                site = site_match["site"]
                # Use cache to optimize rack lookups per site
                cache_key = f"librenms_import_racks_site_{site.pk}"
                available_racks = cache.get(cache_key)

                if available_racks is None:
                    # Query racks for this site - include both:
                    # 1. Racks assigned to locations within the site
                    # 2. Racks directly assigned to the site (without location)
                    available_racks = list(
                        Rack.objects.filter(Q(location__site=site) | Q(site=site))
                        .select_related("location", "site")
                        .order_by("location__name", "name")
                    )
                    # Use API cache timeout if available, otherwise use default 5 minutes
                    cache_timeout = api.cache_timeout if api else 300
                    cache.set(cache_key, available_racks, cache_timeout)

                result["rack"]["available_racks"] = available_racks
                # Rack is optional, don't add to issues
                result["rack"]["found"] = True  # Mark as "found" even if None (optional field)

            # Skip VM-specific validations for devices
            result["cluster"]["found"] = True  # Not required for devices

        # 5. Match Platform (optional - same for both devices and VMs)
        os = libre_device.get("os", "")
        platform_match = find_matching_platform(os)
        result["platform"] = platform_match

        if not platform_match["found"] and os:
            if platform_match.get("match_type") == "ambiguous":
                ambiguity_source = platform_match.get("ambiguity_source", "mapping")
                if ambiguity_source == "platform":
                    result["warnings"].append(
                        f"Multiple Platforms match OS: '{os}' — resolve the duplicate Platform names in NetBox"
                    )
                else:
                    result["warnings"].append(
                        f"Multiple platform mappings found for OS: '{os}' — resolve the conflict in Platform Mappings"
                    )
            else:
                result["warnings"].append(f"No matching platform found for OS: '{os}'")

        # 6. Additional validations
        if not hostname:
            result["issues"].append("Device has no hostname")

        # 7. Virtual chassis detection (only for devices, not VMs)
        if include_vc_detection and not import_as_vm and api is not None:
            device_id = libre_device.get("device_id")
            if device_id:
                try:
                    logger.debug(f"Calling get_virtual_chassis_data for device {device_id}")
                    vc_detection = get_virtual_chassis_data(api, device_id, force_refresh=force_vc_refresh)
                    logger.debug(
                        f"VC detection result: is_stack={vc_detection.get('is_stack')}, "
                        f"member_count={vc_detection.get('member_count')}, "
                        f"members={len(vc_detection.get('members', []))}"
                    )
                    if vc_detection:
                        result["virtual_chassis"] = vc_detection
                        if vc_detection["is_stack"]:
                            logger.debug(
                                f"Virtual chassis CONFIRMED for device {hostname}: "
                                f"{vc_detection['member_count']} members"
                            )
                            result["virtual_chassis"] = update_vc_member_suggested_names(vc_detection, hostname)
                except Exception as e:
                    logger.exception(f"Exception during VC detection for device {hostname}: {e}")
                    result["virtual_chassis"]["detection_error"] = str(e)
            else:
                logger.debug(f"No device_id found for {hostname}")

        # 8. Determine if device/VM is ready to import
        if result["existing_device"]:
            # Already matched - can_import was already set to False
            result["is_ready"] = False
            # Populate role from existing device so the modal shows it
            existing = result["existing_device"]
            if hasattr(existing, "role") and existing.role:
                result["device_role"]["found"] = True
                result["device_role"]["role"] = existing.role

            # Check for device type mismatch between existing device and LibreNMS. Skip for an
            # OOB-sub-key match (existing_match_type == "librenms_oob"): the LibreNMS payload is
            # the OOB controller's, so a host-vs-OOB device-type compare is a bogus "wrong device"
            # warning on a correctly-linked row.
            if (
                result.get("existing_match_type") != "librenms_oob"
                and hasattr(existing, "device_type")
                and existing.device_type
            ):
                librenms_dt = result["device_type"].get("device_type")
                if librenms_dt and existing.device_type.pk != librenms_dt.pk:
                    result["device_type_mismatch"] = True
                    result["warnings"].append(
                        f"Device type mismatch: NetBox has '{existing.device_type}' "
                        f"but LibreNMS reports '{librenms_dt}'. "
                        f"This may indicate the wrong device was matched."
                    )
        else:
            result["can_import"] = len(result["issues"]) == 0

            if import_as_vm:
                # For VMs: only cluster is required
                result["is_ready"] = result["can_import"] and result["cluster"]["found"]
            else:
                # For Devices: site, device_type, and device_role are required
                result["is_ready"] = (
                    result["can_import"]
                    and result["site"]["found"]
                    and result["device_type"]["found"]
                    and result["device_role"]["found"]
                )

        logger.debug(
            f"Validation for {libre_device.get('hostname')} ({'VM' if import_as_vm else 'Device'}): "
            f"issues={len(result['issues'])}, can_import={result['can_import']}, "
            f"issues_list={result['issues']}"
        )

        return result

    except Exception as e:
        logger.exception(f"Error validating device for import: {libre_device.get('hostname', 'unknown')}")
        result["issues"].append(f"{VALIDATION_ERROR_ISSUE_PREFIX} {str(e)}")
        return result


def import_single_device(
    device_id: int,
    server_key: str = None,
    validation: dict = None,
    manual_mappings: dict = None,
    sync_options: dict = None,
    libre_device: dict = None,
) -> dict:
    """
    Import a single LibreNMS device to NetBox.

    Args:
        device_id: LibreNMS device ID
        server_key: LibreNMS server configuration key
        validation: Pre-computed validation dict (optional)
        manual_mappings: Manual object mappings (optional):
            - site_id: NetBox Site ID
            - device_type_id: NetBox DeviceType ID
            - device_role_id: NetBox DeviceRole ID
            - platform_id: NetBox Platform ID (optional)
            - rack_id: NetBox Rack ID (optional)
        sync_options: Sync options (optional):
            - sync_interfaces: bool (default True)
            - sync_cables: bool (default True)
            - sync_fields: bool (default True)
        libre_device: Pre-fetched LibreNMS device data (optional).
            If provided, skips API call to fetch device info.

    Returns:
        dict: Import result with structure:
            {
                'success': bool,
                'device': Device object or None,
                'message': str,
                'error': str or None,
                'synced': {
                    'interfaces': int,
                    'cables': int,
                    'ip_addresses': int
                },
            }
    """
    try:
        api = LibreNMSAPI(server_key=server_key)

        # Use pre-fetched device data if provided, otherwise fetch from API. Read LIVE
        # (use_cache=False): this is the device-CREATION path — name/serial/status/hardware are
        # derived from libre_device — so it must not build a NetBox device from the 60s get_device_info
        # snapshot a sync-tab render may have seeded, mirroring the import flow's live-read policy.
        if libre_device is None:
            success, libre_device = api.get_device_info(device_id, use_cache=False)
            if not success or not libre_device:
                return {
                    "success": False,
                    "device": None,
                    "message": "",
                    "error": f"Failed to retrieve device {device_id} from LibreNMS",
                    "synced": {},
                }

        # Validate device if validation not provided
        if validation is None:
            use_sysname_opt = sync_options.get("use_sysname", True) if sync_options else True
            strip_domain_opt = sync_options.get("strip_domain", False) if sync_options else False
            validation = validate_device_for_import(
                libre_device,
                api=api,
                use_sysname=use_sysname_opt,
                strip_domain=strip_domain_opt,
                server_key=api.server_key,
            )

        # Check if device already exists
        if validation.get("existing_device"):
            return {
                "success": False,
                "device": validation["existing_device"],
                "message": "",
                "error": f"Device already exists: {validation['existing_device'].name}",
                "synced": {},
            }

        # Hard fail-closed guard: an ambiguous librenms_id (matches >1 NetBox record) is the
        # terminal blocker — validate_device_for_import() sets existing_device=None for it, so the
        # check above doesn't catch it, and a manual_mappings import would otherwise create a
        # duplicate Device under the ambiguous id. Block the create outright.
        if validation.get("ambiguous_librenms_id"):
            return {
                "success": False,
                "device": None,
                "message": "",
                "error": "Import blocked: ambiguous LibreNMS ID matches multiple NetBox records.",
                "synced": {},
            }

        # Parallel terminal-ambiguity guard: a duplicate hostname/serial match is also a terminal
        # blocker — validate_device_for_import() sets existing_device=None AND
        # existing_match_type="ambiguous_hostname_or_serial" for it, so neither the existing_device
        # check above nor the ambiguous_librenms_id guard catches it. Without this, a manual_mappings
        # import (which supplies site/type/role and so skips the `if not site` fail-closed below)
        # would create a duplicate Device under the unresolved ambiguity — the same fail-open the
        # ambiguous_librenms_id guard exists to prevent.
        if validation.get("existing_match_type") == "ambiguous_hostname_or_serial":
            return {
                "success": False,
                "device": None,
                "message": "",
                "error": "Import blocked: this device's hostname, serial, or management IP matches multiple NetBox devices; resolve the duplicate first.",
                "synced": {},
            }

        # Use validation-derived matches, allow manual mappings to override specific fields
        site = validation["site"].get("site")
        device_type = validation["device_type"].get("device_type")
        device_role = validation["device_role"].get("role")
        platform = validation["platform"].get("platform")
        rack = validation.get("rack", {}).get("rack")

        if manual_mappings:
            site = Site.objects.filter(id=manual_mappings.get("site_id")).first() or site
            device_type = DeviceType.objects.filter(id=manual_mappings.get("device_type_id")).first() or device_type
            device_role = DeviceRole.objects.filter(id=manual_mappings.get("device_role_id")).first() or device_role

            platform_id = manual_mappings.get("platform_id")
            if platform_id:
                from dcim.models import Platform

                platform = Platform.objects.filter(id=platform_id).first() or platform

            rack_id = manual_mappings.get("rack_id")
            if rack_id:
                rack = Rack.objects.select_related("location", "site").filter(id=rack_id).first() or rack

        # Validate required fields
        if not site:
            return {
                "success": False,
                "device": None,
                "message": "",
                "error": "Site is required but not provided",
                "synced": {},
            }
        if not device_type:
            return {
                "success": False,
                "device": None,
                "message": "",
                "error": "Device type is required but not provided",
                "synced": {},
            }
        if not device_role:
            return {
                "success": False,
                "device": None,
                "message": "",
                "error": "Device role is required but not provided",
                "synced": {},
            }

        # Create device in NetBox
        with transaction.atomic():
            # Use pre-computed resolved_name from validation when available so the
            # created device name matches exactly what was displayed in the import UI.
            # Only fall back to recomputing from sync_options when no validation exists.
            if validation and validation.get("resolved_name"):
                device_name = validation["resolved_name"]
            else:
                use_sysname = sync_options.get("use_sysname", True) if sync_options else True
                strip_domain = sync_options.get("strip_domain", False) if sync_options else False
                device_name = _determine_device_name(
                    libre_device,
                    use_sysname=use_sysname,
                    strip_domain=strip_domain,
                    device_id=device_id,
                )

            # Generate import timestamp comment
            import_time = timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")

            _cf_proxy = SimpleNamespace(custom_field_data={})
            set_librenms_device_id(_cf_proxy, device_id, api.server_key)
            device_data = {
                "name": device_name,
                "site": site,
                "device_type": device_type,
                "role": device_role,
                "status": "active" if libre_device.get("status") == 1 else "offline",
                "comments": f"Imported from LibreNMS by netbox-librenms-plugin on {import_time}",
                "custom_field_data": _cf_proxy.custom_field_data,
            }

            # Add optional fields
            if platform:
                device_data["platform"] = platform

            if rack:
                device_data["rack"] = rack

            # Persist the canonical serial used by the exact indexed match lookups.
            serial = normalize_serial(libre_device.get("serial"))
            if serial and serial != "-":
                device_data["serial"] = serial

            location_name = libre_device.get("location", "")
            if location_name and location_name != "-":
                from dcim.models import Location

                # Try to find matching location within the site
                location = Location.objects.filter(site=site, name__iexact=location_name).first()
                if location:
                    device_data["location"] = location

            # Create the device
            device = Device(**device_data)
            device.full_clean()
            device.save()

        # Sync additional data based on options
        sync_options = sync_options or {}
        synced = {"interfaces": 0, "cables": 0, "ip_addresses": 0}

        try:
            # Sync interfaces
            if sync_options.get("sync_interfaces", True):
                # This is simplified - would need proper request context
                # For now, just log that it should be done
                logger.info(f"Interface sync should be performed for device {device.name}")

            # Sync cables
            if sync_options.get("sync_cables", True):
                logger.info(f"Cable sync should be performed for device {device.name}")

        except Exception as e:
            logger.warning(f"Error during post-import sync: {str(e)}")
            # Don't fail the import if sync fails

        return {
            "success": True,
            "device": device,
            "message": f"Successfully imported device: {device.name}",
            "error": None,
            "synced": synced,
        }

    except Exception as e:
        logger.exception(f"Error importing device {device_id}")
        return {
            "success": False,
            "device": None,
            "message": "",
            "error": str(e),
            "synced": {},
        }


def get_librenms_device_by_id(api: LibreNMSAPI, device_id: int, use_cache: bool = True) -> dict:
    """
    Retrieve a single device from LibreNMS by ID.

    Args:
        api: LibreNMSAPI instance
        device_id: LibreNMS device ID
        use_cache: Passed through to ``get_device_info``. The import fallback passes ``False`` so
            that when ``fetch_device_with_cache``'s own caches miss, the API fallback returns live
            data rather than the 60s get_device_info snapshot a sync-tab render may have seeded.

    Returns:
        Device dictionary or None if not found
    """
    try:
        # Use the dedicated API endpoint to get device by ID
        success, device = api.get_device_info(device_id, use_cache=use_cache)
        if success and device:
            return device

        logger.warning(f"Device {device_id} not found in LibreNMS")
        return None
    except Exception as e:
        logger.exception(f"Failed to get device {device_id} from LibreNMS: {e}")
        return None


def fetch_device_with_cache(
    device_id: int,
    api: LibreNMSAPI,
    server_key: str = None,
    libre_devices_cache: dict = None,
) -> dict | None:
    """
    Fetch LibreNMS device from cache or API with automatic caching.

    Checks three sources in order:
    1. Pre-fetched cache dict (if provided)
    2. Django cache (Redis/memory)
    3. LibreNMS API (caches result for future use)

    This function consolidates the device fetching pattern used throughout
    the import workflow, eliminating code duplication.

    Args:
        device_id: LibreNMS device ID to fetch
        api: LibreNMSAPI instance for fallback API calls
        server_key: Optional server key for multi-server setups (defaults to api.server_key)
        libre_devices_cache: Optional pre-fetched device cache dict

    Returns:
        Device dict from LibreNMS, or None if not found

    Example:
        >>> # Simple usage
        >>> libre_device = fetch_device_with_cache(123, api)
        >>> if libre_device:
        ...     print(libre_device['hostname'])
        >>>
        >>> # With pre-fetched cache dict
        >>> cache_dict = {123: {...}, 456: {...}}
        >>> libre_device = fetch_device_with_cache(123, api, libre_devices_cache=cache_dict)
    """
    # Check pre-fetched cache dict first (fastest) — but only when the cached row's OWN device_id
    # doesn't contradict the requested id (cached_row_matches), so a mis-keyed/stale entry isn't
    # served AS this device. A contradiction falls through to the Django cache / API fetch below.
    cached_row = libre_devices_cache.get(device_id) if libre_devices_cache else None
    if cached_row_matches(cached_row, device_id):
        return cached_row

    # Check Django cache
    cache_key = get_import_device_cache_key(device_id, server_key or api.server_key)
    libre_device = cache.get(cache_key)

    if not libre_device:
        # Fallback to API fetch. Read LIVE: both this function's caches (the pre-fetched dict and the
        # import Django cache) have already missed, so the fallback must reflect current LibreNMS
        # state — not the separate 60s get_device_info snapshot a sync-tab render may have populated —
        # or the confirm modal / re-rendered row shows metadata that disagrees with what import uses.
        libre_device = get_librenms_device_by_id(api, device_id, use_cache=False)
        if libre_device:
            # Cache for future use
            cache.set(cache_key, libre_device, timeout=api.cache_timeout)

    return libre_device


def __getattr__(name):
    """
    Lazily re-export ``bulk_import_devices_shared`` (PEP 562 module ``__getattr__``).

    A top-level ``from .bulk_import import bulk_import_devices_shared`` would be a
    circular import (``bulk_import`` imports ``validate_device_for_import`` from this
    module at load time), so PEP 562 defers the import until the attribute is
    accessed.

    Args:
        name (str): The attribute name being looked up on the module.

    Returns:
        The ``bulk_import_devices_shared`` callable when requested.

    Raises:
        AttributeError: If *name* is any other attribute.
    """
    if name == "bulk_import_devices_shared":
        from .bulk_import import bulk_import_devices_shared

        return bulk_import_devices_shared
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
