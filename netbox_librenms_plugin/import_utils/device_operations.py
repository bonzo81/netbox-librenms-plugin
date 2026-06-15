"""Device validation, import, and fetch operations."""

import logging
from types import SimpleNamespace

from dcim.models import Device, DeviceRole, DeviceType, Rack, Site
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from virtualization.models import Cluster  # noqa: F401 — used by test mock.patch targets

from ..librenms_api import LibreNMSAPI
from ..utils import (
    AmbiguousLibreNMSIdError,
    coerce_librenms_id,
    find_by_librenms_id,
    find_matching_platform,
    find_matching_site,
    get_librenms_oob,
    match_librenms_hardware_to_device_type,
    set_librenms_device_id,
)
from ..constants import normalize_oob_type
from ..import_validation_helpers import apply_merge_candidates, apply_oob_detection_result
from .cache import get_import_device_cache_key
from .virtual_chassis import (
    _generate_vc_member_name,
    empty_virtual_chassis_data,
    get_virtual_chassis_data,
    update_vc_member_suggested_names,
)

logger = logging.getLogger(__name__)


def _detect_oob_type_from_name(name):
    """Return canonical OOB type token (idrac/ilo/ipmi/bmc/drac) found in *name*, or None.

    Routes through normalize_oob_type() so a vendor-specific token wins over the generic
    "oob" even when "oob" appears earlier in the name (e.g. "leaf01-oob-idrac9" -> "idrac",
    not "oob"). A bare re.search() returns the first token and would downgrade the hint.
    """
    if not name:
        return None
    return normalize_oob_type(name, "")


def _describe_existing_librenms_link(obj, server_key):
    """
    Describe the current LibreNMS linkage on a NetBox object.

    Returns a dict ``{"host_id": int|None, "oob_id": int|None, "oob_type": str|None}``
    summarising the ``librenms_id`` custom field for *server_key*.  Always returns a
    dict (with all-None values if nothing is linked) so callers can treat it as a
    plain status object.  Tolerates legacy bare-int and dict-form custom field values.
    """
    info = {"host_id": None, "oob_id": None, "oob_type": None}
    cf_value = obj.cf.get("librenms_id") if hasattr(obj, "cf") else None
    # Legacy bare-int OR string-digit (pre-JSON format).
    if not isinstance(cf_value, dict):
        info["host_id"] = coerce_librenms_id(cf_value)
        return info
    entry = cf_value.get(server_key)
    # Per-server simple form: legacy bare-int or string-digit under the server key.
    if not isinstance(entry, dict):
        info["host_id"] = coerce_librenms_id(entry)
        return info
    # New dict-form: {"id": <int|str>, "oob": {"id": <int|str>, "type": <str>, ...}}.
    # Use coerce_librenms_id so string-digit values (e.g. "42") stored by older
    # plugin versions are still recognized, matching the behaviour of
    # find_by_librenms_id() and get_librenms_device_id().
    host_id = coerce_librenms_id(entry.get("id"))
    if host_id is not None and host_id > 0:
        info["host_id"] = host_id
    oob = entry.get("oob")
    if isinstance(oob, dict):
        oob_id = coerce_librenms_id(oob.get("id"))
        if oob_id is not None and oob_id > 0:
            info["oob_id"] = oob_id
        oob_type = oob.get("type")
        if isinstance(oob_type, str) and oob_type:
            info["oob_type"] = oob_type
    return info


def _try_chassis_device_type_match(api, device_id):
    """
    Attempt device type matching using chassis inventory fields.

    When the LibreNMS hardware string doesn't match any NetBox device type,
    the chassis entity often contains a more standardized identifier
    (e.g., entPhysicalName 'CHAS-BP-MX480-S' or entPhysicalModelName '710-017414')
    that matches a DeviceType part_number or model.

    Tries entPhysicalName first (typically the chassis part number),
    then entPhysicalModelName as fallback.

    Returns:
        dict with matched/device_type/match_type keys, or None on failure.
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
                    chassis_match = match_librenms_hardware_to_device_type(value)
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
            from ipaddress import ip_address

            ip_address(name)
            # It's a valid IP address, don't strip
        except ValueError:
            # Not an IP, safe to strip domain
            name = name.split(".")[0]

    return name


def _flag_ambiguous_librenms_id(result, librenms_id, exc):
    """Block import when a librenms_id resolves to more than one NetBox object.

    An ambiguous id is a data-integrity violation; treating it as "not found" would let
    the device import as new (or bind to an arbitrary row), so fail closed instead.

    The message is appended to ``issues`` (not just ``warnings``) because the readiness
    step recomputes ``can_import`` from ``issues`` — a warning alone would be silently
    overridden back to importable when no other issue is present.
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


def validate_device_for_import(
    libre_device: dict,
    import_as_vm: bool = False,
    api: "LibreNMSAPI" = None,
    *,
    server_key: str = "default",
    include_vc_detection: bool = True,
    force_vc_refresh: bool = False,
    use_sysname: bool = True,
    strip_domain: bool = False,
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
        force_vc_refresh: When True, bypass cached VC data and re-query LibreNMS
        use_sysname: If True, prefer sysName over hostname (matches import behaviour)
        strip_domain: If True, strip domain suffix from device name

    Returns:
        dict: Validation result with structure:
            {
                'is_ready': bool,  # Can import without user intervention
                'can_import': bool,  # Can import (possibly after configuration)
                'import_as_vm': bool,  # Whether importing as VM
                'existing_device': Device or VirtualMachine or None,
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
            if (isinstance(_vm_cf_id, int) and not isinstance(_vm_cf_id, bool)) or (
                isinstance(_vm_cf_id, str) and _vm_cf_id.isdigit()
            ):
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
                if (isinstance(_dev_cf_id, int) and not isinstance(_dev_cf_id, bool)) or (
                    isinstance(_dev_cf_id, str) and _dev_cf_id.isdigit()
                ):
                    result["librenms_id_needs_migration"] = True

                # Check if name matches resolved name (VC-aware: compare against VC member name)
                if hostname and existing_device.virtual_chassis and existing_device.vc_position:
                    incoming_serial = libre_device.get("serial") or ""
                    if incoming_serial == "-":
                        incoming_serial = ""
                    vc_expected_name = _generate_vc_member_name(
                        hostname,
                        existing_device.vc_position,
                        serial=incoming_serial or existing_device.serial or "",
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
                incoming_serial = libre_device.get("serial") or ""
                if result["existing_match_type"] != "librenms_oob" and incoming_serial and incoming_serial != "-":
                    if existing_device.serial and existing_device.serial == incoming_serial:
                        result["serial_confirmed"] = True
                    elif existing_device.serial and existing_device.serial != incoming_serial:
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
                if existing_link.get("host_id"):
                    link_note = f"already linked to LibreNMS device #{existing_link['host_id']}"
                elif existing_link.get("oob_id"):
                    link_note = "already linked to LibreNMS as an OOB controller"
                else:
                    link_note = "not linked to LibreNMS"
                result["warnings"].append(
                    f"VM with same hostname exists in NetBox as '{existing_vm.name}' ({link_note})"
                )
                result["can_import"] = False
            elif existing_device:
                logger.info(f"Found existing device by hostname: {existing_device.name}")
                result["existing_device"] = existing_device
                result["existing_match_type"] = "hostname"
                # Surface the current host/OOB linkage so a hostname-matched device that
                # is already linked to LibreNMS isn't mislabelled as "not linked".
                result["existing_librenms_link"] = _describe_existing_librenms_link(existing_device, server_key)

                # Check for serial conflict on hostname-matched device
                incoming_serial = libre_device.get("serial") or ""
                if incoming_serial and incoming_serial != "-" and existing_device.serial != incoming_serial:
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
                    existing_link = result["existing_librenms_link"] or {}
                    if existing_link.get("host_id"):
                        link_note = f"currently linked to LibreNMS device #{existing_link['host_id']}"
                    elif existing_link.get("oob_id"):
                        link_note = "OOB already linked"
                    else:
                        link_note = "not linked to LibreNMS"
                    result["warnings"].append(
                        f"Device with same hostname exists in NetBox as '{existing_device.name}' ({link_note})"
                    )

                result["can_import"] = False

            # Check by serial number (strong physical match - hardware identity)
            if not result["existing_device"]:
                serial = libre_device.get("serial") or ""
                if serial and serial != "-" and not import_as_vm:
                    existing_by_serial = Device.objects.filter(serial=serial).first()
                    if existing_by_serial:
                        logger.info(f"Found existing device by serial: {existing_by_serial.name} (serial={serial})")
                        result["existing_device"] = existing_by_serial
                        result["existing_match_type"] = "serial"
                        result["can_import"] = False

                        # Capture existing device's current LibreNMS linkage so the UI can
                        # present accurate state (NOT just "not linked to LibreNMS").
                        existing_link = _describe_existing_librenms_link(existing_by_serial, server_key)
                        result["existing_librenms_link"] = existing_link

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
                        names_match = bool(
                            existing_by_serial.name and existing_by_serial.name.lower() == hostname.lower()
                        )
                        # Normalize to int so that a string device_id from the API
                        # (e.g. "17") doesn't cause a false "linked elsewhere" result
                        # when compared to the int host_id from coerce_librenms_id.
                        normalized_device_id = coerce_librenms_id(libre_device.get("device_id"))
                        already_linked_elsewhere = bool(
                            existing_link
                            and existing_link["host_id"]
                            and existing_link["host_id"] != normalized_device_id
                        )
                        chassis_pair_likely = (not names_match) or already_linked_elsewhere

                        oob_possible = chassis_pair_likely and existing_oob is None
                        host_possible = chassis_pair_likely and bool(
                            existing_link
                            and existing_link["host_id"]
                            and existing_link["host_id"] != normalized_device_id
                            and not existing_link.get("oob_id")
                        )
                        existing_oob_from_name = _detect_oob_type_from_name(existing_by_serial.name)

                        # --- Compute all values before mutating result ---
                        oob_candidate_data = None
                        if oob_possible:
                            inferred_oob_type = (
                                oob_type_from_libre
                                or _detect_oob_type_from_name(
                                    libre_device.get("hostname") or libre_device.get("sysName") or ""
                                )
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
                            # OOB-typed incoming but existing already has an OOB linked --
                            # inform without blocking. No actionable button in this branch.
                            serial_action_value = "link"
                            block_warnings.append(
                                f"Device '{existing_by_serial.name}' already has an OOB controller linked. "
                                f"Re-import will update the existing OOB entry."
                            )
                        elif not oob_possible and not host_possible:
                            # Neither role is feasible -- fall back to legacy hostname/serial
                            # warning behaviour so the user still sees a useful message.
                            if existing_by_serial.name and existing_by_serial.name.lower() == hostname.lower():
                                if existing_link and existing_link["host_id"]:
                                    block_warnings.append(
                                        f"Device with same serial and hostname exists as '{existing_by_serial.name}' "
                                        f"(currently linked to LibreNMS device #{existing_link['host_id']})"
                                    )
                                else:
                                    block_warnings.append(
                                        f"Device with same serial and hostname exists as '{existing_by_serial.name}' "
                                        f"(not linked to LibreNMS)"
                                    )
                                serial_action_value = "link"
                            else:
                                block_warnings.append(
                                    f"Device with same serial ({serial}) exists as '{existing_by_serial.name}' "
                                    f"but hostname differs (LibreNMS: '{hostname}'). Device may have been reinstalled."
                                )
                                serial_action_value = "hostname_differs"

                        apply_oob_detection_result(
                            result,
                            serial_action=serial_action_value,
                            oob_candidate=oob_candidate_data,
                            promote_to_host=promote_to_host_data,
                            serial_role_choice_available=oob_possible and host_possible,
                            warnings=block_warnings,
                        )

            # Refresh local variable to reflect any VM-mode adjustments made during detection
            # (e.g. existing VM found by hostname sets result["import_as_vm"] = True).
            # Must happen before the merge-candidates block below so a VM hostname-match
            # doesn't fall through to Device-only merge logic.
            import_as_vm = result["import_as_vm"]

            # Stage 2 — merge-candidates detection.
            # When the hostname-matched device and the serial-matched device are
            # DIFFERENT NetBox objects, the two probably represent the same
            # physical box (host + OOB) imported as separate entries. Surface
            # this as a merge action instead of silently picking one.
            try:
                _serial_for_pair = (libre_device.get("serial") or "").strip()
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
                    if result.get("existing_match_type") == "hostname" and hostname:
                        _hostname_peers = list(Device.objects.filter(name__iexact=hostname)[:2])
                        if len(_hostname_peers) == 1:
                            _hostname_match = _hostname_peers[0]
                        elif len(_hostname_peers) > 1:
                            result["warnings"].append(
                                f"Multiple NetBox devices share hostname '{hostname}'; merge suggestion skipped."
                            )
                    elif result.get("existing_match_type") == "serial":
                        _serial_peers = list(Device.objects.filter(serial=_serial_for_pair)[:2])
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
                                },
                                oob_named={
                                    "pk": _serial_match.pk,
                                    "name": _serial_match.name,
                                    "librenms_link": oob_link,
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
                    from ipam.models import IPAddress

                    existing_ip = IPAddress.objects.filter(address__net_host=primary_ip).first()
                    if existing_ip and existing_ip.assigned_object:
                        device = (
                            existing_ip.assigned_object.device
                            if hasattr(existing_ip.assigned_object, "device")
                            else None
                        )
                        if device:
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
                            is_oob_ip = device.oob_ip_id is not None and existing_ip.pk == device.oob_ip_id
                            has_primary_ip = bool(device.primary_ip4_id or device.primary_ip6_id)
                            if oob_type and (is_oob_ip or not has_primary_ip):
                                existing_oob = get_librenms_oob(device, server_key=server_key)
                                if existing_oob is None:
                                    result["existing_device"] = device
                                    result["existing_match_type"] = "primary_ip"
                                    result["serial_action"] = "oob_candidate"
                                    result["oob_candidate"] = {
                                        "device": device,
                                        "type": oob_type,
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
                                existing_link = result.get("existing_librenms_link") or {}
                                if existing_link.get("host_id"):
                                    link_note = f"currently linked to LibreNMS device #{existing_link['host_id']}"
                                elif existing_link.get("oob_id"):
                                    link_note = "OOB already linked"
                                else:
                                    link_note = "not linked to LibreNMS"
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
            dt_match = match_librenms_hardware_to_device_type(hardware)

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
                        chassis_match = _try_chassis_device_type_match(api, device_id)
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
        result["issues"].append(f"Validation error: {str(e)}")
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
            - sync_ips: bool (default True)
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

        # Use pre-fetched device data if provided, otherwise fetch from API
        if libre_device is None:
            success, libre_device = api.get_device_info(device_id)
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

            serial = libre_device.get("serial", "")
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

            # Sync IP addresses
            if sync_options.get("sync_ips", True):
                logger.info(f"IP address sync should be performed for device {device.name}")

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


def get_librenms_device_by_id(api: LibreNMSAPI, device_id: int) -> dict:
    """
    Retrieve a single device from LibreNMS by ID.

    Args:
        api: LibreNMSAPI instance
        device_id: LibreNMS device ID

    Returns:
        Device dictionary or None if not found
    """
    try:
        # Use the dedicated API endpoint to get device by ID
        success, device = api.get_device_info(device_id)
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
    # Check pre-fetched cache dict first (fastest)
    if libre_devices_cache and device_id in libre_devices_cache:
        return libre_devices_cache[device_id]

    # Check Django cache
    cache_key = get_import_device_cache_key(device_id, server_key or api.server_key)
    libre_device = cache.get(cache_key)

    if not libre_device:
        # Fallback to API fetch
        libre_device = get_librenms_device_by_id(api, device_id)
        if libre_device:
            # Cache for future use
            cache.set(cache_key, libre_device, timeout=api.cache_timeout)

    return libre_device


def __getattr__(name):
    """Lazily re-export ``bulk_import_devices_shared`` to satisfy the
    ``import_utils/device_operations.py`` export contract.

    A top-level ``from .bulk_import import bulk_import_devices_shared`` would be a
    circular import (``bulk_import`` imports ``validate_device_for_import`` from this
    module at load time), so PEP 562 defers the import until the attribute is accessed.
    """
    if name == "bulk_import_devices_shared":
        from .bulk_import import bulk_import_devices_shared

        return bulk_import_devices_shared
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
