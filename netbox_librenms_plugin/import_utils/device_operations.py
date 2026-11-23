"""Device validation, import, and fetch operations."""

import logging
from collections.abc import Callable
from typing import NamedTuple

from dcim.models import Device, DeviceRole, DeviceType, Rack, Site, VirtualChassis
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from tenancy.models import Tenant
from virtualization.models import Cluster, VirtualMachine

from ..constants import is_librenms_placeholder, normalize_oob_type
from ..import_validation_helpers import (
    DEVICE_TYPE_ISSUES,
    ROLE_ISSUE,
    SITE_ISSUE_PREFIX,
    VM_PLACEMENT_ISSUE,
    apply_merge_candidates,
    apply_oob_detection_result,
    clear_match_derived_action_fields,
)
from ..ip_addressing import parse_host_address
from ..librenms_api import LibreNMSAPI, librenms_id_owned_message
from ..librenms_ids import coerce_librenms_id
from ..models import RuleConfigurationError
from ..serial_identity import (
    REFUSING,
    UNAVAILABLE_MASTER_LINK,
    Comparison,
    Resolution,
    ResolutionKind,
    SerialRules,
    assignment_lock,
    compare,
    display,
    is_identity,
    is_serial_value,
    keeps_stored_serial,
    other_device_holder,
    page_text,
    resolve_device,
    resolve_device_write,
    resolve_devices_write,
    stored_form,
    stored_form_refusal,
)
from ..server_selection import librenms_link_owner
from ..transactions import atomic_with_events
from ..platform_versions import (
    HIDDEN_PLATFORM,
    PlatformFacts,
    PlatformReason,
    PlatformState,
    assess_platform,
    import_platform,
    platform_label,
    platform_version_tracking_enabled,
    reason_text,
)
from ..utils import (
    cached_row_matches,
    row_identity_matches,
    exception_text_for,
    find_matching_location,
    find_matching_site,
    is_vc_position,
    librenms_text,
    match_librenms_hardware_to_device_type,
    parse_location_for_import,
    resolve_location_mapping,
)
from .cache import get_import_device_cache_key
from .naming import _name_candidates, _resolve_device_name
from .virtual_chassis import (
    VCNumberingError,
    _existing_member_name,
    _load_vc_member_name_pattern,
    empty_virtual_chassis_data,
    existing_member_name_refusal,
    get_virtual_chassis_data,
    identify_vc_master,
    member_serial_unavailable_message,
    members_to_create,
    numbering_refusal_text,
    prepare_virtual_chassis,
    unnamed_member_reason,
    unnamed_members,
    unnumbered_virtual_chassis,
    unreadable_member_serials,
)
from netbox_librenms_plugin.server_mappings import (
    IDENTITY_BUSY_MESSAGE,
    AmbiguousLibreNMSIdError,
    IdentityBusy,
    MappingRole,
    assign_own,
    find_mapping,
    lock_librenms_id_assignment,
    persist_mapping,
    read_mapping,
)

logger = logging.getLogger(__name__)

# Prefix on the issue appended by validate_device_for_import()'s catch-all except branch when
# validation aborts on an exception and returns only a PARTIAL result. Downstream fail-closed
# guards (e.g. bulk_import.detect_collisions_for_device_ids) key on this to treat the row as
# not-reliably-checked. Keep it a shared constant so the producer and every consumer can't drift
# apart — a silent text change would otherwise defeat the fail-closed guarantee.
VALIDATION_ERROR_ISSUE_PREFIX = "Validation error:"
# Refresh removes a stale duplicate serial blocker by this start.
SERIAL_DUPLICATE_ISSUE_MARKER = "Multiple NetBox devices share serial"
# The start and end of the blocker of a row whose serial no lookup can read; the serial text lies between them.
SERIAL_UNAVAILABLE_ISSUE_START = "Serial '"
SERIAL_UNAVAILABLE_ISSUE_END = f"' cannot be matched: {UNAVAILABLE_MASTER_LINK}"
STACK_DETECTION_FAILED_MESSAGE = "Stack detection failed. Refresh LibreNMS data and try again."


def _resolve_rack_for_import(site, rack_token):
    """Resolve an unambiguous parsed rack token within a NetBox site."""
    if not rack_token or rack_token == "-":
        return None

    rack_matches = list(
        Rack.objects.filter(Q(location__site=site) | Q(site=site), name__iexact=rack_token).select_related(
            "location", "site"
        )[:2]
    )
    if len(rack_matches) > 1:
        logger.warning(
            "Multiple racks named %r in site %s; skipping automatic rack assignment",
            rack_token,
            site,
        )
        return None
    if rack_matches:
        return rack_matches[0]
    return resolve_location_mapping("rack", rack_token, parent_site=site)


def _get_location_hierarchy(location):
    """Return a NetBox Location's ancestry from root to the resolved location."""
    hierarchy = []
    current = location
    while current is not None:
        hierarchy.append(current)
        current = getattr(current, "parent", None)
    return list(reversed(hierarchy))


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
    mapping = read_mapping(obj)
    info = {"host_id": mapping.own_id(server_key), "oob_id": None, "oob_type": None}
    entry = mapping.server(server_key)
    if entry is not None:
        info["oob_id"] = entry.oob_id
        if isinstance(entry.oob_type, str) and entry.oob_type:
            info["oob_type"] = entry.oob_type
    return info


# This is the canonical fixed-wording helper trusted by the import-disclosure rule.
# nosemgrep: import-disclosure-sanitizer-shadow
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


def _host_ip_rows(primary_ip):
    """
    Return every IPAddress row whose host address is *primary_ip*; duplicate net_host rows are possible.

    Raises:
        ValueError: When *primary_ip* is not a parseable host address.

    """
    from ipam.models import IPAddress

    return IPAddress.objects.filter(address__net_host=str(parse_host_address(primary_ip)))


def _answers_at_oob_ip(device, matching_ips) -> bool:
    """Return whether the OOB IP of the Device *device* is one of *matching_ips*, the rows of the LibreNMS address."""
    return device.oob_ip_id is not None and matching_ips.filter(pk=device.oob_ip_id).exists()


def resolve_device_by_host_ip(primary_ip):
    """
    Resolve the unique NetBox Device or VirtualMachine that carries a host address.

    Scans EVERY ``IPAddress`` row sharing ``primary_ip`` as its host address (duplicate
    net_host rows are possible) across Device interfaces, VM interfaces, and the Device
    ``oob_ip`` direct-FK path. A genuine collision fails closed instead of binding to
    whichever duplicate row sorts first. An IP can be a Device's ``oob_ip`` while assigned
    to no interface, so the assigned-object scan alone would miss an OOB-only link.

    Shared by :func:`validate_device_for_import` and ``bulk_import._refresh_existing_device``
    so the two paths can't drift on which device a management IP resolves to.

    Args:
        primary_ip: The management IP (host form) to resolve.

    Returns:
        tuple: ``(object | None, ambiguous: bool)``.
            ``object`` is the single matching Device or VirtualMachine, or ``None`` when none
            or more than one match. ``ambiguous`` is ``True`` only when more than one distinct
            object shares the address.

    Raises:
        ValueError: When *primary_ip* is not a parseable host address. Callers catch it and
            fail closed for that device.

    """
    from dcim.models import Device

    # prefetch_related on the assigned_object GenericForeignKey resolves every duplicate net_host
    # row's interface in one bulk pass instead of a per-row content-type lookup (small N, but free).
    matching_ips = _host_ip_rows(primary_ip).prefetch_related("assigned_object")
    candidates = {}
    matching_exists = False
    for existing_ip in matching_ips:
        matching_exists = True
        assigned = getattr(existing_ip, "assigned_object", None)
        candidate = None
        if assigned is not None:
            candidate = getattr(assigned, "device", None) or getattr(assigned, "virtual_machine", None)
        if candidate is not None:
            candidates[(candidate._meta.label_lower, candidate.pk)] = candidate
    if matching_exists:
        for oob_device in Device.objects.filter(oob_ip__in=matching_ips):
            candidates[(oob_device._meta.label_lower, oob_device.pk)] = oob_device
    if len(candidates) > 1:
        return None, True
    if candidates:
        return next(iter(candidates.values())), False
    return None, False


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
    try:
        success, inventory = api.get_inventory_filtered(device_id, ent_physical_class="chassis")
        if not success or not inventory:
            return None

        for item in inventory:
            # Try entPhysicalName first (often the chassis part number like CHAS-BP-MX480-S)
            for field in ("entPhysicalName", "entPhysicalModelName"):
                value = librenms_text(item.get(field))
                if not is_librenms_placeholder(value):
                    chassis_match = match_librenms_hardware_to_device_type(
                        value, preloaded_rules=preloaded_device_type_rules
                    )
                    if chassis_match is None:
                        continue
                    if chassis_match["matched"]:
                        chassis_match["match_type"] = "chassis"
                        chassis_match["chassis_model"] = value
                        return chassis_match
    except RuleConfigurationError:
        raise
    except Exception:
        logger.debug(f"Chassis inventory fallback failed for device {device_id}", exc_info=True)

    return None


def match_import_device_type(libre_device: dict, api=None, preloaded_device_type_rules: dict | None = None):
    """
    Return the DeviceType match for a LibreNMS row: mapping or hardware first, then the chassis model.

    Validation and the Virtual Chassis details dialog both read the matched DeviceType from here.

    Returns:
        dict | None: ``{matched, device_type, match_type}``, or None when several DeviceTypes match.

    """
    dt_match = match_librenms_hardware_to_device_type(
        libre_device.get("hardware", ""), preloaded_rules=preloaded_device_type_rules
    )
    # When the hardware string matches nothing, the chassis model is a second lookup source.
    if dt_match is not None and not dt_match["matched"] and api and libre_device.get("device_id"):
        chassis_match = _try_chassis_device_type_match(
            api, libre_device["device_id"], preloaded_device_type_rules=preloaded_device_type_rules
        )
        if chassis_match and chassis_match["matched"]:
            dt_match = chassis_match
    return dt_match


DEVICE_TYPE_OVERRIDE_UNAVAILABLE = "Selected device type is unavailable"


class DeviceTypeOverrideError(ValueError):
    """A manual DeviceType override names no DeviceType."""


def effective_import_device_type(matched_device_type, manual_mappings: dict | None = None):
    """
    Return the DeviceType an import row creates its device with.

    A manual ``device_type_id`` override wins and must name a DeviceType; otherwise the matched
    DeviceType from :func:`match_import_device_type` applies. The stack master match, the bulk
    import precheck and the device write all read this one result.

    Raises:
        DeviceTypeOverrideError: The override is present but names no DeviceType.

    """
    if not manual_mappings or "device_type_id" not in manual_mappings:
        return matched_device_type
    try:
        device_type = DeviceType.objects.filter(pk=int(manual_mappings["device_type_id"])).first()
    except (TypeError, ValueError):
        device_type = None
    if device_type is None:
        raise DeviceTypeOverrideError(DEVICE_TYPE_OVERRIDE_UNAVAILABLE)
    return device_type


def _determine_device_name(
    libre_device: dict,
    use_sysname: bool = True,
    strip_domain: bool = False,
    device_id: int | str = None,
) -> str:
    """
    Determine the device/VM name from LibreNMS data.

    Thin wrapper over :func:`_resolve_device_name` for callers that need only the name.

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
    return _resolve_device_name(libre_device, use_sysname=use_sysname, strip_domain=strip_domain, device_id=device_id)[
        0
    ]


def _write_conflict(existing_device, incoming_serial, serial_rules) -> Resolution | None:
    """Return the refusing Resolution of the Devices that a serial write of *incoming_serial* on *existing_device* would duplicate."""
    # A chassis member gets a link only, and a link writes no serial.
    if existing_device.virtual_chassis_id is not None:
        return None
    other = resolve_device_write(
        incoming_serial, serial_rules.for_device(existing_device), rules=serial_rules, exclude_pks=[existing_device.pk]
    )
    return other if other.kind in REFUSING else None


def _flag_serial_conflict(result, other, incoming_serial, phase) -> bool:
    """
    Flag the serial conflict of a matched device when *other*, the refusing Resolution of its action, is set.

    Returns:
        bool: True when another Device holds the incoming serial, several do, or the lookup cannot tell.

    """
    if other is None:
        return False
    result["serial_action"] = "conflict"
    result["serial_duplicate"] = True
    # The lookup is unrestricted, so only the display gate may name the holder (see disclosure.py).
    result["serial_conflict"] = {
        "pk": other.object.pk if other.object is not None else None,
        "serial": display(incoming_serial),
        "phase": phase,
        "ambiguous": other.kind is ResolutionKind.AMBIGUOUS,
        "unavailable": other.reason,
    }
    return True


LEGACY_HOLDERS_UNCHECKED_PREFIX = "The other holders of the serial cannot be checked:"


def check_legacy_id_serial(result, device, incoming_serial, serial_rules) -> None:
    """Set the ``legacy_id_serial_*`` flags of a legacy-linked *device*; validation and the warm refresh share it."""
    # Unavailable until the lookup returns, so a failed lookup never reads as a shared serial.
    result["legacy_id_serial_confirmed"] = False
    result["legacy_id_serial_unavailable"] = True
    result["legacy_id_serial_shared"] = False
    warnings = result.setdefault("warnings", [])
    # A cached row can carry the warning of an earlier check.
    warnings[:] = [w for w in warnings if not (isinstance(w, str) and w.startswith(LEGACY_HOLDERS_UNCHECKED_PREFIX))]
    try:
        holders = resolve_device(incoming_serial, rules=serial_rules)
    except RuleConfigurationError as exc:
        # The legacy ID names the Device, so another maker's broken rule hides only the other holders.
        warnings.append(f"{LEGACY_HOLDERS_UNCHECKED_PREFIX} {exception_text_for(exc, Device, None)}")
        return
    holder = holders.object
    confirmed = holder is not None and holder.pk == device.pk
    unavailable = holders.kind is ResolutionKind.UNAVAILABLE
    result["legacy_id_serial_confirmed"] = confirmed
    result["legacy_id_serial_unavailable"] = unavailable
    result["legacy_id_serial_shared"] = not confirmed and not unavailable


def serial_unavailable_issue(incoming_serial) -> str:
    """Return the blocker of a row whose serial lookup is Unavailable; validation and the refresh both add it."""
    # The constant, not the lookup's reason: the issue text must carry nothing that an unscoped lookup returned.
    return f"{SERIAL_UNAVAILABLE_ISSUE_START}{page_text(incoming_serial)}{SERIAL_UNAVAILABLE_ISSUE_END}"


def is_serial_unavailable_issue(issue) -> bool:
    """Return whether *issue* is a ``serial_unavailable_issue()`` text; other issues can quote its words."""
    return (
        isinstance(issue, str)
        and issue.startswith(SERIAL_UNAVAILABLE_ISSUE_START)
        and issue.endswith(SERIAL_UNAVAILABLE_ISSUE_END)
    )


def _serial_taken_message(device_id, incoming_serial, taken) -> str:
    """Return why the import of *device_id* stops: a NetBox device already holds its serial, or the lookup cannot tell."""
    if taken.kind is ResolutionKind.UNAVAILABLE:
        return f"Cannot import device {device_id}: its serial cannot be matched. {taken.reason}"
    return (
        f"Cannot import device {device_id}: a NetBox device already has serial '{page_text(incoming_serial)}'. "
        "Refresh the import list."
    )


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


# The match types of a match by a name: the resolved name, the LibreNMS hostname or the sysName.
NAME_MATCH_TYPES = ("resolved_name", "hostname", "sysname")

# The warning of a controller row whose host already has an OOB link.
OOB_ALREADY_LINKED_WARNING = (
    "The matched NetBox device already has an OOB controller linked, so no OOB action is offered."
)


def _reported_oob_type(libre_device) -> str | None:
    """Return the OOB type that the row's OS or hardware text names; a value that is not text names none."""
    os_text, hardware_text = (
        value if isinstance(value, str) else "" for value in (libre_device.get("os"), libre_device.get("hardware"))
    )
    return normalize_oob_type(os_text, hardware_text)


def _short_name(name: str) -> str:
    """Return *name* in lower case without its domain; an IP address stays whole."""
    try:
        parse_host_address(name)
    except ValueError:
        return name.split(".")[0].lower()
    return name.lower()


def _row_name_oob_type(libre_device, device) -> str | None:
    """
    Return the OOB type that a name of the row names, when that name is not the NetBox *device*'s own name.

    The names are the current sysName and hostname of the row; a value that is not text is no name. A name
    that is the device's own name tells nothing about the row: the same full name, or one full name that is the
    short form, without the domain, of the other. Two FQDNs with one short name are two names.
    """
    own_full = (device.name or "").lower()
    own_short = _short_name(device.name or "")
    for name in _name_candidates(libre_device):
        if not name or own_full in (name.lower(), _short_name(name)) or name.lower() == own_short:
            continue
        if oob_type := _detect_oob_type_from_name(name):
            return oob_type
    return None


def _oob_candidate(matched, libre_device, matched_by, server_key) -> dict:
    """
    Return the attach offer of the LibreNMS row as the OOB controller of the *matched* Device.

    The offer's ``device`` is its OOB target: the owner of the LibreNMS links of *matched*, whose one OOB slot
    Add as OOB writes. The type comes from the row's OS or hardware, then its name, then "oob". *matched_by*
    ("oob_ip", "primary_ip", "serial" or "hostname") names the match that Add as OOB checks again under its locks.
    """
    target = librenms_link_owner(matched, server_key)
    return {
        "device": target,
        # The host link of the target, which the dialog shows as the attach's unchanged host slot.
        "target_host_id": read_mapping(target).own_id(server_key),
        "type": _reported_oob_type(libre_device) or _row_name_oob_type(libre_device, matched) or "oob",
        "version": libre_device.get("version") or None,
        "ip": libre_device.get("ip") or None,
        "matched_by": matched_by,
    }


def _device_type_mismatch_warning(librenms_device_type) -> str:
    """Return the warning of a matched device whose type differs from the type that LibreNMS reports."""
    return (
        f"Device type mismatch: LibreNMS reports '{librenms_device_type}', which differs from the "
        f"NetBox record. This may indicate the wrong device was matched."
    )


def _ip_assigned_warning(primary_ip, existing_link) -> str:
    """Return the warning of a row whose IP address a NetBox Device already holds."""
    return f"IP address {primary_ip} is already assigned to a device in NetBox ({_describe_link_note(existing_link)})"


def _row_at_oob_ip(device, libre_device) -> bool:
    """Return whether the LibreNMS row's address is the OOB IP of the Device *device*."""
    row_ip = libre_device.get("ip")
    if device.oob_ip_id is None or not row_ip:
        return False
    try:
        return _answers_at_oob_ip(device, _host_ip_rows(row_ip))
    except ValueError:
        return False


def row_at_oob_target_ip(matched, libre_device, server_key) -> bool:
    """
    Return whether the row's address is the OOB IP of the *matched* Device or of the offer's OOB target.

    The OOB target is the owner of the LibreNMS links of *matched* (``librenms_link_owner``); either address is
    controller evidence, and Add as OOB checks the same two addresses again under its locks.
    """
    owner = librenms_link_owner(matched, server_key)
    return _row_at_oob_ip(matched, libre_device) or (owner.pk != matched.pk and _row_at_oob_ip(owner, libre_device))


def _oob_controller_evidence(result, libre_device, server_key) -> bool:
    """
    Return whether the evidence shows that the LibreNMS row is the OOB controller of the matched object.

    One rule for every match type: evidence is the row's OOB-ID link, or, on a name, serial or IP match of a
    Device, the row at the Device's OOB IP or an OOB signal of the row itself (its OS or hardware, or a name
    that is not the Device's own name). A cached action, an own-ID link, the serial role's ambiguous default
    and a Device whose own name looks like a controller's are no evidence.
    """
    matched = result.get("existing_device")
    match_type = result.get("existing_match_type")
    if matched is None:
        return False
    if match_type == "librenms_oob":
        return True
    if not isinstance(matched, Device) or match_type not in (*NAME_MATCH_TYPES, "serial", "primary_ip"):
        return False
    if row_at_oob_target_ip(matched, libre_device, server_key):
        return True
    return bool(_reported_oob_type(libre_device) or _row_name_oob_type(libre_device, matched))


def _current_resolved_name(result, libre_device) -> str:
    """Return the name of the current row under the naming rules that the validation of *result* used."""
    criteria = result.get("naming_criteria") or {}
    name, _source = _resolve_device_name(
        libre_device,
        use_sysname=criteria.get("use_sysname", True),
        strip_domain=criteria.get("strip_domain", False),
        device_id=libre_device.get("device_id"),
    )
    return name


def _withdraw_attach_offer(result) -> None:
    """Remove the attach offer of the row and the action that names it."""
    result["oob_candidate"] = None
    if result.get("serial_action") == "oob_candidate":
        result["serial_action"] = None


def _withdraw_controller_state(result, libre_device, server_key) -> None:
    """Remove the controller state of a row without controller evidence; a verified serial match gets its role again."""
    matched = result.get("existing_device")
    if (
        result.get("existing_match_type") == "serial"
        and result.get("match_verified", True)
        and isinstance(matched, Device)
        and result.get("serial_action") != "merge_netbox_devices"
    ):
        _apply_serial_role(result, matched, libre_device, server_key)
    elif result.get("oob_candidate"):
        _withdraw_attach_offer(result)
    if result.get("serial_action") == "oob_already_linked":
        result["serial_action"] = None
    if OOB_ALREADY_LINKED_WARNING in result.get("warnings", []):
        result["warnings"] = [warning for warning in result["warnings"] if warning != OOB_ALREADY_LINKED_WARNING]


def _apply_serial_role(result, matched, libre_device, server_key) -> None:
    """Decide the serial role of the row again from current facts, as a fresh validation does."""
    decision = _detect_serial_match_role(
        matched,
        result.get("existing_librenms_link"),
        _current_resolved_name(result, libre_device),
        display(libre_device.get("serial")),
        libre_device,
        server_key,
    )
    # The role's warnings name no NetBox object; the ones that an earlier decision already gave are skipped.
    known = set(result.setdefault("warnings", []))
    role_messages = decision.pop("warnings")
    apply_oob_detection_result(result, **decision, warnings=[text for text in role_messages if text not in known])


def _set_attach_offer(result, matched, libre_device, server_key) -> None:
    """Build the attach offer of a controller row from current facts, or withdraw it when the target has an OOB link."""
    if read_mapping(librenms_link_owner(matched, server_key)).has_oob(server_key):
        _withdraw_attach_offer(result)
        if OOB_ALREADY_LINKED_WARNING not in result["warnings"]:
            result["warnings"].append(OOB_ALREADY_LINKED_WARNING)
        return
    if row_at_oob_target_ip(matched, libre_device, server_key):
        matched_by = "oob_ip"
    elif result["existing_match_type"] == "primary_ip":
        matched_by = "primary_ip"
    elif result["existing_match_type"] in NAME_MATCH_TYPES:
        matched_by = "hostname"
    else:
        matched_by = "serial"
    result["serial_action"] = "oob_candidate"
    result["oob_candidate"] = _oob_candidate(matched, libre_device, matched_by, server_key)


def classify_oob_controller(result, libre_device, server_key) -> None:
    """
    Decide once whether the LibreNMS row is the OOB controller of the matched object, and set the state of that decision.

    It sets ``row_is_oob_controller`` from the evidence alone. A controller row gets no host-side name sync,
    serial update, host promotion, type mismatch or host warnings. A merge suggestion stays as it is. Else a
    Device host gets the attach offer, built again from current facts, when the owner of its LibreNMS links has
    no OOB link, and the already-linked warning when it has one. A match that the validation did not verify
    (``match_verified`` False) gets no offer. Validation and the cached-row refresh both call it, so the rule
    has one definition.
    """
    libre_device = libre_device or {}
    result["row_is_oob_controller"] = _oob_controller_evidence(result, libre_device, server_key)
    if not result["row_is_oob_controller"]:
        _withdraw_controller_state(result, libre_device, server_key)
        return
    matched = result["existing_device"]
    result["name_sync_available"] = False
    result["suggested_name"] = None
    result.pop("promote_to_host", None)
    result["serial_role_choice_available"] = False
    result["device_type_mismatch"] = False
    if result["serial_action"] in ("update_serial", "promote_to_host"):
        result["serial_action"] = None
    host_warnings = {
        _serial_differs_warning(libre_device.get("serial")),
        _ip_assigned_warning(libre_device.get("ip"), result["existing_librenms_link"]),
        _device_type_mismatch_warning(result["device_type"].get("device_type")),
    }
    result["warnings"] = [warning for warning in result["warnings"] if warning not in host_warnings]
    if result["serial_action"] == "merge_netbox_devices":
        return
    if result["existing_match_type"] == "librenms_oob" or not isinstance(matched, Device):
        return
    if not result.get("match_verified", True):
        _withdraw_attach_offer(result)
        return
    _set_attach_offer(result, matched, libre_device, server_key)


def _serial_differs_warning(incoming_serial) -> str:
    """Return the warning of a hostname match whose LibreNMS serial differs from the stored one."""
    return (
        f"Hostname matches but the serial differs from the NetBox record "
        f"(LibreNMS: '{page_text(incoming_serial)}'). Hardware may have been replaced."
    )


class _SerialRoleFacts(NamedTuple):
    """The evidence of the serial role: what LibreNMS and NetBox report about a row and its serial match."""

    oob_type_from_libre: str | None
    existing_oob_recorded: bool
    existing_oob_from_name: str | None
    oob_possible: bool
    host_possible: bool


def _serial_role_facts(existing_by_serial, existing_link, hostname, libre_device, server_key) -> _SerialRoleFacts:
    """Return the serial-role evidence of the row against *existing_by_serial*; see :func:`_detect_serial_match_role`."""
    # Compute both possible roles for the incoming LibreNMS device against
    # the existing NetBox device, then pick a heuristic default. The UI
    # offers a manual toggle whenever both roles are feasible so the user
    # can override the heuristic (e.g. mark a "linux"-OS device as OOB or
    # demote an apparent host into the OOB slot).
    oob_type_from_libre = _reported_oob_type(libre_device)
    existing_oob_recorded = read_mapping(librenms_link_owner(existing_by_serial, server_key)).has_oob(server_key)

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
    incoming_oob_signal = bool(oob_type_from_libre or _row_name_oob_type(libre_device, existing_by_serial))
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

    oob_possible = chassis_pair_likely and not existing_oob_recorded
    host_possible = chassis_pair_likely and bool(linked_to_other_id and not existing_link.get("oob_id"))
    return _SerialRoleFacts(
        oob_type_from_libre, existing_oob_recorded, existing_oob_from_name, oob_possible, host_possible
    )


def _detect_serial_match_role(existing_by_serial, existing_link, hostname, serial, libre_device, server_key):
    """
    Decide the role an incoming LibreNMS device plays against a NetBox device matched by serial.

    Pure decision step for the serial-match branch of :func:`validate_device_for_import`:
    reads NetBox/LibreNMS state but does **not** mutate ``result``. Computes whether the
    incoming device is an OOB-controller candidate, a host-promotion, a plain link, or a
    hostname-differs case, and returns the keyword arguments for
    :func:`apply_oob_detection_result` (``serial_action``, ``oob_candidate``,
    ``promote_to_host``, ``serial_role_choice_available``, ``warnings``).

    Args:
        existing_by_serial (Device): The NetBox Device matched by serial.
        existing_link (dict | None): The ``_describe_existing_librenms_link`` result for
            *existing_by_serial*.
        hostname (str): The incoming LibreNMS hostname (already resolved).
        serial (str): The incoming serial (for warning text only). The matched device's own
            identity never reaches a warning: the serial lookup is unrestricted, so only the
            display gate may name it (see ``import_utils/disclosure.py``).
        libre_device (dict): The raw LibreNMS device payload.
        server_key (str): The active LibreNMS server key.

    Returns:
        dict: The keyword arguments for :func:`apply_oob_detection_result`.

    """
    facts = _serial_role_facts(existing_by_serial, existing_link, hostname, libre_device, server_key)
    oob_type_from_libre, existing_oob_recorded, existing_oob_from_name, oob_possible, host_possible = facts

    # --- Compute all values before mutating result ---
    oob_candidate_data = (
        _oob_candidate(existing_by_serial, libre_device, "serial", server_key) if oob_possible else None
    )

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
    if oob_type_from_libre and existing_oob_recorded:
        # OOB-typed incoming but existing already has an OOB linked -- inform without blocking.
        # Use a dedicated non-actionable value: "link" would render the generic host-link form
        # ("Link to LibreNMS" button) in device_validation_details.html, posting an
        # indistinguishable host-link request instead of leaving this branch informational.
        serial_action_value = "oob_already_linked"
        block_warnings.append(OOB_ALREADY_LINKED_WARNING)
    elif not oob_possible and not host_possible:
        # Neither role is feasible -- fall back to legacy hostname/serial
        # warning behaviour so the user still sees a useful message.
        if existing_by_serial.name and existing_by_serial.name.lower() == hostname.lower():
            block_warnings.append(
                f"A device with this serial and hostname already exists in NetBox "
                f"({_describe_link_note(existing_link)})"
            )
            serial_action_value = "link"
        else:
            block_warnings.append(
                f"A device with serial {page_text(serial)} already exists in NetBox but its hostname differs "
                f"(LibreNMS: '{hostname}'). Device may have been reinstalled."
            )
            serial_action_value = "hostname_differs"

    return {
        "serial_action": serial_action_value,
        "oob_candidate": oob_candidate_data,
        "promote_to_host": promote_to_host_data,
        "serial_role_choice_available": oob_possible and host_possible,
        "warnings": block_warnings,
    }


def _stack_member_label(position, serial) -> str:
    """Return how a stack member issue names a member: its position, or its serial in a stack without numbers."""
    return f"Stack member {position}" if position is not None else f"Stack member with serial '{page_text(serial)}'"


def _stack_member_serial_refusals(result, incoming_serial, serial_rules) -> list:
    """Return an issue for each stack member serial that the chassis create of a new device refuses."""
    vc = result.get("virtual_chassis") or {}
    if not vc.get("is_stack"):
        return []
    device_type = effective_import_device_type(result["device_type"].get("device_type"))
    context = serial_rules.for_device_type(device_type)
    master = identify_vc_master(vc["members"], incoming_serial, context)
    # A stack without an identified master imports as one device and creates no member.
    if master is None:
        return []
    # The member writer names every member, the master too, and refuses the stack when one gets no name.
    pattern = _load_vc_member_name_pattern()
    unnamed_ids = {id(member) for member, _position in unnamed_members(vc["members"], pattern, context)}
    unnamed = [
        f"{_stack_member_label(member.get('position'), member.get('serial'))}: "
        f"{unnamed_member_reason(member.get('serial'), pattern)}"
        for member in vc["members"]
        if id(member) in unnamed_ids
    ]
    members = [(member, member.get("position")) for member in vc["members"] if member is not master]
    # The text comes from the LibreNMS members only: the lookup is unrestricted, so it selects and names nothing.
    if unreadable := unreadable_member_serials(members):
        return unnamed + [
            f"{_stack_member_label(position, member.get('serial'))}: {refusal}"
            for member, position, refusal in unreadable
        ]
    members = [(member, position, display(member.get("serial"))) for member, position in members]
    taken = resolve_devices_write([serial for _member, _position, serial in members], context, rules=serial_rules)
    # The member writer refuses the whole stack for an Unavailable member, so validation blocks it too.
    unavailable = [
        f"{_stack_member_label(position, serial)}: {member_serial_unavailable_message(serial)}"
        for _member, position, serial in members
        if taken[serial].kind is ResolutionKind.UNAVAILABLE
    ]
    if unavailable:
        return unnamed + unavailable
    created = {
        id(member)
        for member, *_rest in members_to_create(
            members, taken, context, lambda member, _position, _serial: member.get("suggested_name")
        )
    }
    return unnamed + [
        f"{_stack_member_label(position, serial)}: {refusal}"
        for member, position, serial in members
        if id(member) in created and (refusal := stored_form_refusal(serial, Device))
    ]


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
    serial_rules: SerialRules | None = None,
    serial_discoveries: Callable[[], dict[str, Resolution]] | None = None,
    numbering_rules: tuple | None = None,
) -> dict:
    """
    Validate if a LibreNMS device can be imported to NetBox.

    Performs comprehensive validation:
    - Checks if device already exists in NetBox
    - Validates required prerequisites (Site, DeviceType, DeviceRole for devices)
      or placement (matched Site, selected Cluster, or selected host Device) for VMs
    - Provides smart matching for missing objects
    - Detects virtual chassis/stack configuration (if API provided)
    - Returns detailed validation status

    Args:
        libre_device: Device data from LibreNMS.
        import_as_vm: If True, validate for VM import instead of Device import.
        api: Optional LibreNMSAPI instance for virtual chassis detection.
        server_key: LibreNMS server key for stored identifiers and API data.
        include_vc_detection: Skip VC detection when False to speed up bulk operations.
        collision_only: Return after existing-object and collision-candidate matching. This skips
            site, device type, role, platform, rack, and virtual-chassis import prerequisites.
        force_vc_refresh: When True, bypass cached VC data and re-query LibreNMS.
        use_sysname: If True, prefer sysName over hostname (matches import behavior).
        strip_domain: If True, strip domain suffix from device name.
        preloaded_device_type_rules: Optional device-type normalization rules keyed for lookup.
        serial_rules: The batch's :class:`SerialRules`; None reads them once for this device.
        serial_discoveries: Optional lazy batch of serial lookup results for search fallback.
        numbering_rules: The batch's VC member numbering rules; None reads them for a stack.

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
                'legacy_id_serial_confirmed': bool,  # Legacy-linked device is the one holder of the serial
                'legacy_id_serial_unavailable': bool,  # The holder lookup of that serial cannot read every row
                'legacy_id_serial_shared': bool,  # The holder lookup found another Device with that serial
                'serial_duplicate': bool,  # Incoming serial already on a different device
                'serial_role_choice_available': bool,  # Both oob_candidate and promote_to_host valid
                'oob_candidate': dict or None,  # {device (the OOB target), target_host_id, type, version, ip, matched_by}
                'row_is_oob_controller': bool,  # The evidence shows the row is the matched object's OOB controller
                'match_verified': bool,  # False when an error stopped validation before it verified the match
                # 'promote_to_host': dict — CONDITIONAL, present only when host promotion is available
                'merge_candidates': dict or None,  # {host_named: {...}, oob_named: {...}} when two
                    # NetBox devices look like the same physical box (devices only)
                'librenms_id_needs_migration': bool,  # Existing object carries a legacy bare-int id
                'name_matches': bool,  # Existing object's name matches the resolved name
                'name_sync_available': bool,  # Existing object's name differs (sync offered)
                'suggested_name': str or None,  # Name to suggest when name_sync_available
                'member_name_refusal': str,  # Why a linked stack member gets no name, or ""
                'device_type_mismatch': bool,  # Existing device's type differs from LibreNMS (devices only)
                'naming_criteria': dict or None,  # How resolved_name was derived (use_sysname/strip_domain)
                'virtual_chassis': dict,  # VC state, prepare_virtual_chassis() for the matched DeviceType
                'issues': List[str],  # Blocking issues
                'warnings': List[str],  # Non-blocking warnings
                'site': {  # Device site or VM matched-site placement
                    'found': bool,
                    'site': Site or None,
                    'match_type': str,  # 'exact' or None
                    'suggestions': List[Site]  # Alternative suggestions
                },
                'region': {
                    'found': bool,
                    'region': Region or None
                },
                'device_type': {  # Only for devices
                    'found': bool,
                    'device_type': DeviceType or None,
                    'match_type': str,  # 'exact' or None
                    'suggestions': List[dict]  # Device types for user selection
                },
                'device_role': {
                    'found': bool,  # False until manually selected for a new Device
                    'role': DeviceRole or None,
                    'available_roles': List[DeviceRole]  # All roles for user selection
                },
                'cluster': {  # Selected or host-derived VM cluster
                    'found': bool,
                    'cluster': Cluster or None,
                    'available_clusters': List[Cluster]  # All clusters for user selection
                },
                'vm_placement': {
                    'method': str,  # 'site', 'cluster', or 'host'
                    'found': bool,
                    'host_device': Device or None
                },
                'platform': {
                    'platform': Platform or None,  # assigned without a create: the target or the OS platform
                    'assessment': PlatformAssessment or None,
                    'reason_text': str,  # the words of the assessment reason
                },
                'rack': {  # Only for devices
                    'found': bool,
                    'rack': Rack or None,
                    'available_racks': List[Rack]
                },
                'location': {
                    'found': bool,
                    'location': Location or None,
                    'match_type': str,  # 'exact', 'mapping', or None
                    'token': str or None,
                    'hierarchy': List[Location]
                },
                'tenant': {
                    'found': bool,
                    'tenant': Tenant or None,
                    'match_type': str,  # 'exact', 'mapping', or None
                    'token': str or None
                }
            }

    Example:
        >>> validation = validate_device_for_import(libre_device)
        >>> if validation['is_ready']:
        ...     import_single_device(libre_device['device_id'])

    """
    # The matcher and the classifier read the mappings of the same server.
    server_key = api.server_key if api is not None else server_key
    result = _match_and_validate(
        libre_device,
        import_as_vm,
        api,
        server_key=server_key,
        include_vc_detection=include_vc_detection,
        collision_only=collision_only,
        force_vc_refresh=force_vc_refresh,
        use_sysname=use_sysname,
        strip_domain=strip_domain,
        preloaded_device_type_rules=preloaded_device_type_rules,
        serial_rules=serial_rules,
        serial_discoveries=serial_discoveries,
        numbering_rules=numbering_rules,
    )
    # Every return path of the matcher, an early return or an error, reaches this one decision.
    classify_oob_controller(result, libre_device, server_key)
    return result


def _match_and_validate(  # noqa: C901
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
    serial_rules: SerialRules | None = None,
    serial_discoveries: Callable[[], dict[str, Resolution]] | None = None,
    numbering_rules: tuple | None = None,
) -> dict:
    """Return the validation of :func:`validate_device_for_import` before the OOB-controller decision."""
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
        "legacy_id_serial_confirmed": False,  # True when the legacy-linked device is the one holder of the serial
        "legacy_id_serial_unavailable": False,  # True when the holder lookup of that serial cannot read every row
        "legacy_id_serial_shared": False,  # True when the holder lookup found another Device with that serial
        "serial_duplicate": False,  # True when incoming serial is already on a different device
        # {"pk", "serial", "phase"} for the device already holding the incoming serial. The
        # lookup is unrestricted, so only the display gate may name it (see disclosure.py).
        "serial_conflict": None,
        "serial_role_choice_available": False,  # True when both oob_candidate and promote_to_host are valid choices
        "librenms_id_needs_migration": False,  # True when existing device has legacy bare-int ID
        "oob_candidate": None,  # dict {device, type, version, ip} when oob_candidate detected
        # True when the row is the OOB controller of the matched object: linked, detected or already linked.
        "row_is_oob_controller": False,
        "match_verified": True,  # False when an error stopped validation before it verified the match
        # promote_to_host is only set when the host-promotion path is available; absent otherwise.
        "existing_librenms_link": None,  # dict {host_id, oob_id, oob_type} describing existing device's current LibreNMS linkage
        "merge_candidates": None,  # dict {host_named: {pk,name,librenms_link}, oob_named: {pk,name,librenms_link}} when two NB devices look like the same physical box
        "name_matches": False,  # True when existing device name matches LibreNMS sysName
        "name_sync_available": False,  # True when existing device name differs from sysName
        "suggested_name": None,  # sysName to suggest when name_sync_available is True
        "member_name_refusal": "",  # Why the member name pattern gives a linked stack member no name
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
        "region": {
            "found": False,
            "region": None,
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
        "vm_placement": {
            "method": "site",
            "found": False,
            "host_device": None,
        },
        "platform": {"platform": None, "assessment": None, "reason_text": ""},
        "rack": {
            "found": False,
            "rack": None,
            "available_racks": [],
        },
        "location": {
            "found": False,
            "location": None,
            "match_type": None,
            "token": None,
            "hierarchy": [],
        },
        "tenant": {
            "found": False,
            "tenant": None,
            "match_type": None,
            "token": None,
        },
        "naming_criteria": None,  # Populated after resolved_name is set
    }

    try:
        serial_rules = serial_rules or SerialRules.load()
        incoming_serial = libre_device.get("serial")
        # 1. Check if device/VM already exists in NetBox
        # Always check both Devices AND VMs to properly detect existing objects
        librenms_id = libre_device.get("device_id")
        hostname, name_source = _resolve_device_name(
            libre_device,
            use_sysname=use_sysname,
            strip_domain=strip_domain,
            device_id=librenms_id,
        )
        result["resolved_name"] = hostname
        _raw_sysname, _raw_hostname = _name_candidates(libre_device)
        result["naming_criteria"] = {
            "use_sysname": use_sysname,
            "strip_domain": strip_domain,
            "raw_sysname": _raw_sysname or "",
            "raw_hostname": _raw_hostname or "",
            "source": name_source,
        }
        logger.debug(
            f"Checking for existing device/VM: "
            f"librenms_id={librenms_id} (type={type(librenms_id).__name__}), "
            f"hostname={hostname}"
        )

        # Check for existing VM first (by librenms_id custom field).
        # find_mapping() covers both the new per-server JSON format
        # and legacy bare-integer values so neither is missed. An ambiguous id
        # (matching multiple records) blocks the import — see _flag_ambiguous_librenms_id.
        try:
            existing_vm = find_mapping(
                VirtualMachine.objects.all(),
                server=server_key,
                identity=librenms_id,
                roles=(MappingRole.OWN, MappingRole.OOB),
            )
        except AmbiguousLibreNMSIdError as exc:
            existing_vm = None
            _flag_ambiguous_librenms_id(result, librenms_id, exc)

        # Cross-model collision: the same librenms_id on both a VM and a Device is ambiguous.
        # Without this, the VM lookup wins and the Device lookup below is skipped, silently
        # binding to the VM. Detect it and fail closed (the device block is gated on the flag).
        if existing_vm is not None:
            try:
                _device_collision = find_mapping(
                    Device.objects.all(),
                    server=server_key,
                    identity=librenms_id,
                    roles=(MappingRole.OWN, MappingRole.OOB),
                )
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

            # A legacy bare value resolves like a scoped one, so only the snapshot shows it needs migration.
            if read_mapping(existing_vm).legacy.is_legacy:
                result["librenms_id_needs_migration"] = True

            # A match via the OOB sub-key is labeled like the Device case.
            existing_vm_oob_id = read_mapping(existing_vm).oob_id(server_key)
            if existing_vm_oob_id is not None and existing_vm_oob_id == coerce_librenms_id(librenms_id):
                result["existing_match_type"] = "librenms_oob"

            # Check if name matches resolved name (accounts for use_sysname/strip_domain)
            if hostname and existing_vm.name == hostname:
                result["name_matches"] = True
            elif hostname and existing_vm.name != hostname:
                result["name_sync_available"] = True
                result["suggested_name"] = hostname

        # Check for existing Device (by librenms_id custom field).
        # find_mapping() covers both the new per-server JSON format
        # and legacy bare-integer values so neither is missed. Skip when an ambiguity
        # (intra-model or cross-model) was already flagged — binding must fail closed.
        if not result["existing_device"] and not result["ambiguous_librenms_id"]:
            try:
                existing_device = find_mapping(
                    Device.objects.all(),
                    server=server_key,
                    identity=librenms_id,
                    roles=(MappingRole.OWN, MappingRole.OOB),
                )
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
                existing_oob_id = read_mapping(existing_device).oob_id(server_key)
                if existing_oob_id is not None and existing_oob_id == coerce_librenms_id(librenms_id):
                    result["existing_match_type"] = "librenms_oob"

                # Surface the full host/OOB linkage so the import table can render
                # both halves of an existing pair with consistent paired styling.
                result["existing_librenms_link"] = _describe_existing_librenms_link(existing_device, server_key)

                # A legacy bare value resolves like a scoped one, so only the snapshot shows it needs migration.
                if read_mapping(existing_device).legacy.is_legacy:
                    result["librenms_id_needs_migration"] = True

                # Check if name matches resolved name (VC-aware: compare against VC member name)
                if hostname and existing_device.virtual_chassis and is_vc_position(existing_device.vc_position):
                    vc_expected_name = _existing_member_name(hostname, existing_device, serial_rules)
                    if vc_expected_name and existing_device.name == vc_expected_name:
                        result["name_matches"] = True
                    elif vc_expected_name:
                        result["name_sync_available"] = True
                        result["suggested_name"] = vc_expected_name
                    else:
                        result["member_name_refusal"] = existing_member_name_refusal(existing_device, serial_rules)
                elif hostname and existing_device.name == hostname:
                    result["name_matches"] = True
                elif hostname and existing_device.name != hostname:
                    result["name_sync_available"] = True
                    result["suggested_name"] = hostname

                # Check for serial drift on the linked device. Skip when the match was via the
                # OOB sub-key (existing_match_type == "librenms_oob"): the incoming payload is the
                # OOB controller's, so comparing it against the host record's serial would surface
                # bogus replacement/conflict warnings on a row that is already correctly linked.
                # No LibreNMS serial text, no comparison: the context reads the device type and chassis.
                drift = None
                if result["existing_match_type"] != "librenms_oob" and display(incoming_serial):
                    context = serial_rules.for_device(existing_device)
                    drift = compare(existing_device.serial, incoming_serial, context)
                if drift is None:
                    pass
                elif drift is Comparison.SAME:
                    result["serial_confirmed"] = True
                    # The legacy ID converts on the serial only for its one holder; the POST checks it again under lock.
                    if result["librenms_id_needs_migration"]:
                        check_legacy_id_serial(result, existing_device, incoming_serial, serial_rules)
                elif drift is Comparison.UNAVAILABLE:
                    result["warnings"].append(f"The serial cannot be compared: {UNAVAILABLE_MASTER_LINK}")
                elif is_identity(incoming_serial, context) and display(existing_device.serial):
                    if not _flag_serial_conflict(
                        result,
                        _write_conflict(existing_device, incoming_serial, serial_rules),
                        incoming_serial,
                        "updating",
                    ):
                        result["serial_action"] = "update_serial"
                        result["warnings"].append(
                            f"Serial number differs from the NetBox record "
                            f"(LibreNMS: '{page_text(incoming_serial)}'). Hardware may have been replaced."
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
                result["warnings"].append(f"A VM with this hostname already exists in NetBox ({link_note})")
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

                # Check for serial conflict on hostname-matched device.
                drift = None
                if display(incoming_serial):
                    context = serial_rules.for_device(existing_device)
                    drift = compare(existing_device.serial, incoming_serial, context)
                # The link refuses another holder of the same serial, so validation offers no link either.
                same_serial_held = drift is Comparison.SAME and _flag_serial_conflict(
                    result,
                    other_device_holder(existing_device, incoming_serial, rules=serial_rules),
                    incoming_serial,
                    "importing",
                )
                if drift is Comparison.UNAVAILABLE:
                    # The link refuses a serial that it cannot compare, so validation offers no link either.
                    unavailable = Resolution(ResolutionKind.UNAVAILABLE, reason=context.unavailable)
                    _flag_serial_conflict(result, unavailable, incoming_serial, "importing")
                elif drift is not None and is_identity(incoming_serial, context) and drift is not Comparison.SAME:
                    if not _flag_serial_conflict(
                        result,
                        _write_conflict(existing_device, incoming_serial, serial_rules),
                        incoming_serial,
                        "importing",
                    ):
                        result["serial_action"] = "update_serial"
                        result["warnings"].append(_serial_differs_warning(incoming_serial))
                elif not same_serial_held:
                    link_note = _describe_link_note(result["existing_librenms_link"])
                    result["warnings"].append(f"A device with this hostname already exists in NetBox ({link_note})")

                result["can_import"] = False

            # Check by serial number (strong physical match - hardware identity)
            serial = display(incoming_serial)
            serial_matches = []
            if not result["existing_device"]:
                if not import_as_vm:
                    # Serial is not unique in NetBox, so .first() would bind an arbitrary row
                    # and the downstream serial/OOB/merge flow would derive its guidance from a
                    # random device. Require a unique match before binding, mirroring the
                    # merge-peer [:2] guard (issue #101). The owner counts equivalent rows under
                    # the serial rules, also rows that the user cannot see.
                    # The batch keys text, and the text of a boolean or a list can equal a stored serial.
                    if serial and is_serial_value(incoming_serial) and serial_discoveries is not None:
                        discovery = serial_discoveries()[serial]
                    else:
                        discovery = resolve_device(incoming_serial, rules=serial_rules)
                    serial_matches = list(discovery.matches)
                    if discovery.kind is ResolutionKind.UNAVAILABLE:
                        # The row stays unresolved: a weaker IP match must not bind it.
                        result["issues"].append(serial_unavailable_issue(incoming_serial))
                        result["can_import"] = False
                        result["is_ready"] = False
                        return result
                    if discovery.kind is ResolutionKind.AMBIGUOUS:
                        # Leave the row unbound before the weaker IP lookup.
                        result["existing_match_type"] = "ambiguous_hostname_or_serial"
                        result["serial_duplicate"] = True
                        result["issues"].append(
                            f"{SERIAL_DUPLICATE_ISSUE_MARKER} '{page_text(serial)}'; resolve the duplicate "
                            "serial before importing."
                        )
                        result["can_import"] = False
                        result["is_ready"] = False
                        return result
                    existing_by_serial = discovery.object
                    if existing_by_serial:
                        # Capture existing device's current LibreNMS linkage so the UI can
                        # present accurate state (NOT just "not linked to LibreNMS").
                        existing_link = _describe_existing_librenms_link(existing_by_serial, server_key)
                        # Decide OOB-candidate / promote-to-host / link / hostname-differs role
                        # for the incoming device (pure: reads state, doesn't mutate result).
                        role = _detect_serial_match_role(
                            existing_by_serial, existing_link, hostname, serial, libre_device, server_key
                        )
                        # Update & Link writes the serial, so a row that the write would duplicate blocks the match.
                        if (
                            role["serial_action"] == "hostname_differs"
                            and not keeps_stored_serial(existing_by_serial.serial, incoming_serial, Device)
                            and _write_conflict(existing_by_serial, incoming_serial, serial_rules)
                        ):
                            result["serial_duplicate"] = True
                            result["issues"].append(
                                f"Another NetBox device holds serial '{page_text(serial)}' under the serial rules of the "
                                "matched device; resolve the duplicate serial before importing."
                            )
                            existing_by_serial = None
                    if existing_by_serial:
                        logger.info(f"Found existing device by serial: {existing_by_serial.name} (serial={serial})")
                        result["existing_device"] = existing_by_serial
                        result["existing_match_type"] = "serial"
                        result["can_import"] = False
                        result["existing_librenms_link"] = existing_link
                        apply_oob_detection_result(result, **role)

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
                serial_matches if _dup_eligible and _match_type == "serial" else []
            )
            if _dup_eligible:
                _dup_current = False
                if _match_type == "hostname" and hostname:
                    _dup_current = len(_hostname_peers) > 1
                elif _match_type == "serial":
                    _dup_current = len(_serial_peers) > 1
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
                _serial_for_pair = serial
                if (
                    _serial_for_pair
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
                        _peer = resolve_device(incoming_serial, rules=serial_rules, exclude_pks=[_hostname_match.pk])
                        if _peer.kind is ResolutionKind.UNIQUE:
                            _serial_match = _peer.object
                        elif _peer.kind is ResolutionKind.AMBIGUOUS:
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
                                    "Two NetBox devices appear to represent this physical box: one "
                                    "matches the LibreNMS hostname, the other matches the chassis "
                                    "serial. Choose which one to keep and merge the other into it."
                                ),
                            )

            except Exception:  # pragma: no cover - defensive: never break validation
                logger.exception("merge-candidate detection failed")

            # Check by primary IP (weaker match, IP could be reassigned).
            # A serial match that a duplicate serial refused ends the lookup: the issue blocks the row.
            if not result["existing_device"] and not result["serial_duplicate"]:
                primary_ip = libre_device.get("ip")
                if primary_ip:
                    # Shared resolver: scans every net_host row across the interface-assignment and
                    # oob_ip-FK paths and fails closed on >1 distinct device (mirrors
                    # _refresh_existing_device in bulk_import.py via the same helper).
                    matched_object, ip_ambiguous = resolve_device_by_host_ip(primary_ip)
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
                    elif isinstance(matched_object, VirtualMachine):
                        result["existing_device"] = matched_object
                        result["existing_match_type"] = "primary_ip"
                        result["import_as_vm"] = True
                        result["existing_librenms_link"] = _describe_existing_librenms_link(matched_object, server_key)
                        link_note = _describe_link_note(result["existing_librenms_link"])
                        result["warnings"].append(
                            f"IP address {primary_ip} is already assigned to a VM in NetBox ({link_note})"
                        )
                        result["can_import"] = False
                    elif matched_object:
                        result["import_as_vm"] = False
                        result["existing_device"] = matched_object
                        result["existing_match_type"] = "primary_ip"
                        # Surface any existing host/OOB linkage so the import UI renders the correct row state.
                        result["existing_librenms_link"] = _describe_existing_librenms_link(matched_object, server_key)
                        result["warnings"].append(_ip_assigned_warning(primary_ip, result["existing_librenms_link"]))
                        result["can_import"] = False

        # Refresh local mode after ALL detection branches. The refresh at the top of the
        # unmatched-device block only runs when nothing matched by librenms_id; an existing
        # VM matched directly by librenms_id (above) sets result["import_as_vm"]=True but
        # skips that block, so without this a linked VM would wrongly take the Device path
        # (missing cluster["available_clusters"], running device-only validation/VC detection).
        import_as_vm = result["import_as_vm"]

        # An ambiguous librenms_id (matches >1 NetBox record) is the terminal blocker for this
        # row — the user must resolve the duplicate id. Don't run the new-import site/device_type/
        # role/placement validation below: with existing_device fail-closed to None, it would pile
        # unrelated "must select ..." blockers onto a row whose real problem is the ambiguous id
        # (mirrors bulk_import.py treating ambiguity as the terminal state). existing_match_type
        # is already "ambiguous_librenms_id" (set by _flag_ambiguous_librenms_id).
        if result["ambiguous_librenms_id"]:
            result["can_import"] = False
            result["is_ready"] = False
            return result

        if collision_only:
            return result

        # Site placement is valid for both Devices and virtual machines. Resolve it before the
        # type-specific branches so a VM can use its LibreNMS location without a cluster.
        location = libre_device.get("location", "")
        parsed_location = parse_location_for_import(location)
        site_token = parsed_location.get("site") or ""
        site_match = find_matching_site(site_token)
        result["site"] = site_match

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
                result["vm_placement"].update(method="site", found=site_match["found"])
                if not site_match["found"]:
                    result["issues"].append(VM_PLACEMENT_ISSUE)

            # Skip device-specific validations for all VMs (new and existing)
            result["device_type"]["found"] = True  # Not required for VMs
            result["device_role"]["found"] = True  # Not required for VMs

        else:
            # 2. For Devices: Validate Site (required)
            if not site_match["found"]:
                if not result.get("existing_device"):
                    result["issues"].append(f"{SITE_ISSUE_PREFIX} '{location}'")
                # Get alternative suggestions
                if location:
                    all_sites = Site.objects.all()[:10]  # Limit for performance
                    result["site"]["suggestions"] = list(all_sites)

            # 3. Validate DeviceType (required)
            hardware = libre_device.get("hardware", "")
            dt_match = match_import_device_type(
                libre_device, api, preloaded_device_type_rules=preloaded_device_type_rules
            )

            if dt_match is None:
                result["device_type"]["found"] = False
                result["device_type"]["device_type"] = None
                result["device_type"]["match_type"] = "ambiguous"
                if not result.get("existing_device"):
                    result["issues"].append(f"{DEVICE_TYPE_ISSUES[1]} '{hardware}' — resolve the ambiguity in NetBox.")
            else:
                # Update result keys individually to preserve the existing schema (especially "found")
                result["device_type"]["found"] = dt_match["matched"]
                result["device_type"]["device_type"] = dt_match.get("device_type")
                result["device_type"]["match_type"] = dt_match.get("match_type")

            if not result["device_type"]["found"] and result["device_type"].get("match_type") != "ambiguous":
                result["device_type"]["found"] = False
                if not result.get("existing_device"):
                    result["issues"].append(f"{DEVICE_TYPE_ISSUES[0]} '{hardware}'")
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
                result["issues"].append(ROLE_ISSUE)
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
                region = getattr(site, "region", None)
                result["region"].update(found=region is not None, region=region)
                location_token = parsed_location.get("location")
                result["location"]["token"] = location_token
                if location_token and location_token != "-":
                    matched_location = find_matching_location(site, location_token)
                    if matched_location is not None:
                        result["location"].update(
                            found=True,
                            location=matched_location,
                            match_type="exact",
                            hierarchy=_get_location_hierarchy(matched_location),
                        )
                    else:
                        mapped_location = resolve_location_mapping("location", location_token, parent_site=site)
                        if mapped_location is not None:
                            result["location"].update(
                                found=True,
                                location=mapped_location,
                                match_type="mapping",
                                hierarchy=_get_location_hierarchy(mapped_location),
                            )

                tenant_token = parsed_location.get("tenant")
                result["tenant"]["token"] = tenant_token
                if tenant_token and tenant_token != "-":
                    tenant = Tenant.objects.filter(name__iexact=tenant_token).first()
                    if tenant is not None:
                        result["tenant"].update(
                            found=True,
                            tenant=tenant,
                            match_type="exact",
                        )
                    else:
                        mapped_tenant = resolve_location_mapping("tenant", tenant_token)
                        if mapped_tenant is not None:
                            result["tenant"].update(
                                found=True,
                                tenant=mapped_tenant,
                                match_type="mapping",
                            )

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
                result["rack"]["rack"] = _resolve_rack_for_import(site, parsed_location.get("rack"))
                # Rack is optional, don't add to issues
                result["rack"]["found"] = True  # Mark as "found" even if None (optional field)

            # Skip VM-specific validations for devices
            result["cluster"]["found"] = True  # Not required for devices

        # 5. Match Platform (optional - same for both devices and VMs)
        platform_assessment = assess_platform(
            None,
            PlatformFacts.from_librenms(libre_device.get("os"), libre_device.get("version")),
            tracking=platform_version_tracking_enabled(),
        )
        result["platform"] = {
            "platform": platform_assessment.target or platform_assessment.os_platform,
            "assessment": platform_assessment,
            # The validation is cached and shown to any import user, so its text names no platform.
            "reason_text": reason_text(platform_assessment.reason, platform_assessment, None),
        }
        if platform_assessment.state is PlatformState.BLOCKED or platform_assessment.os_platform is None:
            if platform_assessment.reason is not PlatformReason.NO_OS:
                result["warnings"].append(result["platform"]["reason_text"])

        # 6. Additional validations
        if not hostname:
            result["issues"].append("Device has no hostname")

        # 7. Virtual chassis detection (only for devices, not VMs)
        if include_vc_detection and not import_as_vm and api is not None:
            device_id = libre_device.get("device_id")
            if device_id:
                try:
                    logger.debug(f"Calling get_virtual_chassis_data for device {device_id}")
                    vc_detection = get_virtual_chassis_data(
                        api, device_id, force_refresh=force_vc_refresh, serial_rules=serial_rules
                    )
                    logger.debug(
                        f"VC detection result: is_stack={vc_detection.get('is_stack')}, "
                        f"member_count={vc_detection.get('member_count')}, "
                        f"members={len(vc_detection.get('members', []))}"
                    )
                    if vc_detection:
                        result["virtual_chassis"] = vc_detection
                        if vc_detection.get("detection_failed"):
                            result["issues"].append(STACK_DETECTION_FAILED_MESSAGE)
                        elif vc_detection["is_stack"]:
                            logger.debug(
                                f"Virtual chassis CONFIRMED for device {hostname}: "
                                f"{vc_detection['member_count']} members"
                            )
                            try:
                                result["virtual_chassis"] = prepare_virtual_chassis(
                                    vc_detection,
                                    device_type=effective_import_device_type(result["device_type"].get("device_type")),
                                    master_name=hostname,
                                    device_serial=incoming_serial,
                                    serial_rules=serial_rules,
                                    numbering_rules=numbering_rules,
                                )
                            except (VCNumberingError, RuleConfigurationError) as exc:
                                # The import numbers the stack again for its final DeviceType.
                                refusal = numbering_refusal_text(exc)
                                result["virtual_chassis"] = unnumbered_virtual_chassis(vc_detection, refusal)
                                result["issues"].append(refusal)
                except Exception as e:
                    logger.exception("Exception during VC detection for device %s: %s", hostname, e)
                    # The validation result has no viewer: it is cached and shown to any import user.
                    result["virtual_chassis"]["detection_error"] = exception_text_for(e, VirtualChassis, None)
                    result["virtual_chassis"]["detection_failed"] = True
                    result["issues"].append(STACK_DETECTION_FAILED_MESSAGE)
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

            # Check for device type mismatch between existing device and LibreNMS.
            if hasattr(existing, "device_type") and existing.device_type:
                librenms_dt = result["device_type"].get("device_type")
                if librenms_dt and existing.device_type.pk != librenms_dt.pk:
                    result["device_type_mismatch"] = True
                    result["warnings"].append(_device_type_mismatch_warning(librenms_dt))
        else:
            # The import stores the LibreNMS serial, and the writer refuses one that does not fit the field.
            if not import_as_vm and (refusal := stored_form_refusal(incoming_serial, Device)):
                result["issues"].append(refusal)
            if not import_as_vm:
                result["issues"].extend(_stack_member_serial_refusals(result, incoming_serial, serial_rules))
            result["can_import"] = len(result["issues"]) == 0

            if import_as_vm:
                result["is_ready"] = result["can_import"] and result["vm_placement"]["found"]
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
        # The validation result has no viewer: it is cached and shown to any import user.
        detail = exception_text_for(e, VirtualMachine if import_as_vm else Device, None)
        result["issues"].append(f"{VALIDATION_ERROR_ISSUE_PREFIX} {detail}")
        # The error can stop validation after a provisional match; a LibreNMS ID link proves its match itself.
        if result["existing_match_type"] not in ("librenms_id", "librenms_oob"):
            result["match_verified"] = False
        return result


def _validate_and_insert(row, _mapping_fields=frozenset()):
    """Validate the whole new row and insert it once, with its mapping already on it."""
    row.full_clean()
    row.save()
    return row


def import_platform_warnings(label, plan, text_viewer):
    """
    Return the warning for an imported object that did not get its version platform.

    Args:
        label (str): Names the imported object, e.g. ``device 12``.
        plan (ImportPlatform): The platform the import assigned.
        text_viewer: The user who reads the text, or None for saved text.

    Returns:
        list[str]: One warning, or none when the object got the platform the rule calls for.

    """
    if plan.reason is None:
        return []
    from dcim.models import Platform

    detail = reason_text(plan.reason, plan.assessment, text_viewer)
    if plan.error is not None:
        detail = f"{detail} {exception_text_for(plan.error, Platform, text_viewer)}"
    platform = platform_label(plan.platform, text_viewer)
    assigned = "an OS platform you cannot view" if platform == HIDDEN_PLATFORM else f"OS platform {platform}"
    return [f"Imported {label} with {assigned}: {detail}"]


def import_single_device(  # noqa: C901
    device_id: int,
    server_key: str = None,
    validation: dict = None,
    manual_mappings: dict = None,
    sync_options: dict = None,
    libre_device: dict = None,
    user=None,
    text_viewer=None,
    serial_rules: SerialRules | None = None,
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
            - rack_id: NetBox Rack ID (optional)
        sync_options: Sync options (optional):
            - sync_interfaces: bool (default True)
            - sync_cables: bool (default True)
            - sync_fields: bool (default True)
        libre_device: Pre-fetched LibreNMS device data (optional).
            If provided, skips API call to fetch device info.
        user: User whose view scope authorizes explicit object selections.
        text_viewer: User who reads the ``error`` text. None (the default) for text that is saved,
            such as job output, so that only the hidden text of a NetBox refusal is kept.
        serial_rules: The batch's :class:`SerialRules`; None reads them in the import transaction.

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
                'warnings': List[str],  # only on success: the version platform the device did not get
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

        if validation.get("virtual_chassis", {}).get("detection_failed"):
            return {
                "success": False,
                "device": None,
                "message": "",
                "error": STACK_DETECTION_FAILED_MESSAGE,
                "synced": {},
            }

        # Check if device already exists
        if validation.get("existing_device"):
            return {
                "success": False,
                "device": validation["existing_device"],
                "message": "",
                # No name: the match came from an unrestricted search and this string reaches
                # job data and job logs, which any holder of core.view_job can read.
                "error": f"Device {device_id} already exists in NetBox",
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
        rack = None
        rack_explicitly_selected = False

        try:
            device_type = effective_import_device_type(device_type, manual_mappings)
        except DeviceTypeOverrideError as exc:
            return {"success": False, "device": None, "message": "", "error": str(exc), "synced": {}}
        if manual_mappings:
            site = Site.objects.filter(id=manual_mappings.get("site_id")).first() or site
            device_role = DeviceRole.objects.filter(id=manual_mappings.get("device_role_id")).first() or device_role

            rack_id = manual_mappings.get("rack_id")
            if rack_id:
                rack = (
                    Rack.objects.restrict(user, "view").select_related("location", "site").filter(id=rack_id).first()
                    if user is not None
                    else None
                )
                if rack is None:
                    return {
                        "success": False,
                        "device": None,
                        "message": "",
                        "error": "Selected rack is unavailable",
                        "synced": {},
                    }
                rack_explicitly_selected = True

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
        with atomic_with_events():
            # The serial identity locks come first; then the lookup sees a concurrent import's committed device.
            serial_rules = serial_rules or SerialRules.load()
            # The LibreNMS value, not its text: the text of a boolean or a list is no serial.
            incoming_serial = stored_form(libre_device.get("serial"), Device)
            assignment_lock([incoming_serial], model=Device, rules=serial_rules)
            # The insert writes the type row's device count anyway; lock it now, so the context is its manufacturer.
            device_type = DeviceType.objects.select_for_update(no_key=True).get(pk=device_type.pk)
            if incoming_serial:
                taken = resolve_device_write(
                    incoming_serial, serial_rules.for_device_type(device_type), rules=serial_rules
                )
                if taken.kind in REFUSING:
                    return {
                        "success": False,
                        "device": None,
                        "message": "",
                        # No name: the lookup is unrestricted and this string reaches job data and logs.
                        "error": _serial_taken_message(device_id, incoming_serial, taken),
                        "synced": {},
                    }
            _locked_owner, conflict = lock_librenms_id_assignment(device_id, api.server_key)
            if conflict is not None:
                return {
                    "success": False,
                    "device": None,
                    "message": "",
                    # Unrestricted claim search and no user here, so the owner stays unnamed.
                    "error": librenms_id_owned_message(device_id),
                    "synced": {},
                }

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

            device_data = {
                "name": device_name,
                "site": site,
                "device_type": device_type,
                "role": device_role,
                "status": "active" if libre_device.get("status") == 1 else "offline",
                "comments": f"Imported from LibreNMS by netbox-librenms-plugin on {import_time}",
            }

            # A version platform created here rolls back with a failed import.
            platform_plan = import_platform(
                PlatformFacts.from_librenms(libre_device.get("os"), libre_device.get("version")),
                tracking=platform_version_tracking_enabled(),
                actor=user,
            )
            if platform_plan.platform is not None:
                device_data["platform"] = platform_plan.platform

            # Persist the trimmed LibreNMS text; serial_identity keys it when it compares.
            if incoming_serial:
                device_data["serial"] = incoming_serial

            location_name = libre_device.get("location", "")
            parsed_location = parse_location_for_import(location_name)
            location_token = parsed_location.get("location")
            if location_token and location_token != "-":
                # Try to find matching location within the site, including stacked parent/ancestor
                # locations, then fall back to a mapping alias.
                location = find_matching_location(site, location_token)
                if location is None:
                    location = resolve_location_mapping("location", location_token, parent_site=site)
                if location:
                    device_data["location"] = location

            # Resolve the rack from the parsed location token after applying any
            # site override. An explicit table selection takes precedence.
            if not rack_explicitly_selected:
                rack_token = parsed_location.get("rack")
                rack = _resolve_rack_for_import(site, rack_token)
            if rack:
                device_data["rack"] = rack

            # Resolve the tenant from the parsed location token. Tenants are
            # globally unique in NetBox, so no site scoping is required.
            tenant_token = parsed_location.get("tenant")
            if tenant_token and tenant_token != "-":
                from tenancy.models import Tenant

                tenant = Tenant.objects.filter(name__iexact=tenant_token).first()
                if tenant is None:
                    tenant = resolve_location_mapping("tenant", tenant_token)
                if tenant:
                    device_data["tenant"] = tenant

            # Create the device with its mapping: the claim and the owner check come before the insert.
            device = Device(**device_data)
            persist_mapping(device, assign_own(device, api.server_key, device_id), write=_validate_and_insert)
            warnings = import_platform_warnings(f"device {device_id}", platform_plan, text_viewer)

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
            logger.warning("Error during post-import sync: %s", e)
            # Don't fail the import if sync fails

        return {
            "success": True,
            "device": device,
            "message": f"Successfully imported device: {device.name}",
            "error": None,
            "synced": synced,
            "warnings": warnings,
        }

    except Exception as e:
        logger.exception(f"Error importing device {device_id}")
        # The import names a busy identity claim; every other lock conflict gets the try-again text.
        detail = IDENTITY_BUSY_MESSAGE if isinstance(e, IdentityBusy) else exception_text_for(e, Device, text_viewer)
        return {
            "success": False,
            "device": None,
            "message": "",
            "error": detail,
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
        logger.exception("Failed to get device %s from LibreNMS: %s", device_id, e)
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

    # The live read checks the device ID of its row, so a cached row of another device counts as a miss.
    if not row_identity_matches(libre_device, device_id):
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
