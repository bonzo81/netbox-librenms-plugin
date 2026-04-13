"""Virtual chassis detection, creation, and management."""

import logging
from typing import List

from dcim.models import Device, VirtualChassis
from django.core.cache import cache
from django.db import transaction

from ..librenms_api import LibreNMSAPI

logger = logging.getLogger(__name__)


def empty_virtual_chassis_data() -> dict:
    """Public helper for callers that need a blank VC payload."""

    return {
        "is_stack": False,
        "member_count": 0,
        "members": [],
        "detection_error": None,
    }


def _clone_virtual_chassis_data(data: dict | None) -> dict:
    """Return a defensive copy of cached VC data to avoid shared references."""

    if not data:
        return empty_virtual_chassis_data()

    members = []
    for idx, member in enumerate(data.get("members", [])):
        member_copy = member.copy()
        raw_position = member_copy.get("position", idx + 1)
        try:
            pos = int(raw_position)
            member_copy["position"] = pos if pos > 0 else idx + 1
        except (TypeError, ValueError):
            member_copy["position"] = idx + 1  # 1-based fallback; position 0 is invalid
        members.append(member_copy)

    member_count = data.get("member_count") or len(members)

    return {
        "is_stack": bool(data.get("is_stack")),
        "member_count": member_count,
        "members": members,
        "detection_error": data.get("detection_error"),
    }


_VC_CACHE_VERSION = "v1"


def _vc_cache_key(api: LibreNMSAPI, device_id: int | str) -> str:
    server_key = getattr(api, "server_key", "default")
    return f"librenms_vc_detection_{_VC_CACHE_VERSION}_{server_key}_{device_id}"


def get_virtual_chassis_data(api: LibreNMSAPI, device_id: int | str, *, force_refresh: bool = False) -> dict:
    """Fetch (and cache) virtual chassis data for a LibreNMS device."""

    if not api or device_id is None:
        return empty_virtual_chassis_data()

    cache_key = _vc_cache_key(api, device_id)
    _cache_timeout = getattr(api, "cache_timeout", None)
    cache_timeout = 300 if _cache_timeout is None else _cache_timeout
    if not force_refresh and cache_timeout != 0:
        cached = cache.get(cache_key)
        if cached is not None:
            return _clone_virtual_chassis_data(cached)

    detection_data = detect_virtual_chassis_from_inventory(api, device_id)
    if detection_data is None:
        # Non-stack device or transient API failure — cache the negative result so
        # prefetch_vc_data_for_devices() can skip these on subsequent renders.
        # Use force_refresh=True to bypass the cache if needed.
        empty = empty_virtual_chassis_data()
        if cache_timeout != 0:
            cache.set(cache_key, empty, timeout=cache_timeout)
        return _clone_virtual_chassis_data(empty)

    if "detection_error" not in detection_data:
        detection_data["detection_error"] = None

    cache_value = _clone_virtual_chassis_data(detection_data)
    if cache_timeout != 0:
        cache.set(cache_key, cache_value, timeout=cache_timeout)
    return _clone_virtual_chassis_data(cache_value)


def prefetch_vc_data_for_devices(api: LibreNMSAPI, device_ids: List[int], *, force_refresh: bool = False) -> None:
    """
    Pre-warm the virtual chassis cache for multiple devices.

    This eliminates the 0.5-1s delay when rendering the import table
    by proactively fetching VC data before validation.

    Args:
        api: LibreNMSAPI instance
        device_ids: List of LibreNMS device IDs to prefetch VC data for
        force_refresh: When True, bypass cache and fetch fresh data

    Example:
        >>> # Before rendering import table
        >>> prefetch_vc_data_for_devices(api, [123, 124, 125])
        >>> # Now all validate_device_for_import() calls hit cache instantly
    """
    if not api or not device_ids:
        return

    logger.debug(f"Pre-warming VC cache for {len(device_ids)} devices")

    for idx, device_id in enumerate(device_ids):
        # This populates the cache if empty, or skips if already cached
        try:
            get_virtual_chassis_data(api, device_id, force_refresh=force_refresh)
        except (BrokenPipeError, ConnectionError, IOError, OSError) as e:
            logger.warning(f"Connection error during VC prefetch at device {idx}: {e}")
            # Stop processing if connection is broken
            return
        except Exception as e:
            # Log but continue for other errors
            logger.warning(f"Error prefetching VC data for device {device_id}: {e}")

    logger.debug(f"VC cache warming complete for {len(device_ids)} devices")


def detect_virtual_chassis_from_inventory(api: LibreNMSAPI, device_id: int) -> dict | None:
    """
    Detect if device is a stack/Virtual Chassis by analyzing ENTITY-MIB inventory.
    Vendor-agnostic using standard hierarchical structure.

    Args:
        api: LibreNMSAPI instance
        device_id: LibreNMS device ID

    Returns:
        dict with structure:
        {
            'is_stack': bool,
            'member_count': int,
            'members': [
                {
                    'serial': str,
                    'position': int,
                    'model': str,
                    'name': str,
                    'index': int,
                    'description': str,
                    'suggested_name': str  # Generated using master device name
                }
            ]
        }
        Returns None if not a stack or detection fails.

    Detection Logic:
        1. Check root level (entPhysicalContainedIn=0) for parent container
        2. Find parent index (entPhysicalClass='stack' or 'chassis')
        3. Get children chassis at that parent's index
        4. If multiple chassis found -> Stack detected
    """
    try:
        # Get the master device info to use for naming
        success, device_info = api.get_device_info(device_id)
        master_name = None
        if success and device_info:
            master_name = device_info.get("sysName") or device_info.get("hostname")

        # Step 1: Get root level items
        success, root_items = api.get_inventory_filtered(device_id, ent_physical_contained_in=0)

        if not success or not root_items:
            logger.debug(f"No root inventory items found for device {device_id}")
            return None

        # Step 2: Find parent container index
        # Prefer "stack" over "chassis" for deterministic VC detection
        parent_index = None
        stack_index = None
        chassis_index = None
        for item in root_items:
            item_class = item.get("entPhysicalClass")
            if item_class == "stack" and stack_index is None:
                stack_index = item.get("entPhysicalIndex")
            elif item_class == "chassis" and chassis_index is None:
                chassis_index = item.get("entPhysicalIndex")
        parent_index = stack_index if stack_index is not None else chassis_index
        if parent_index is not None:
            logger.debug(f"VC detection: Found parent container at index {parent_index} for device {device_id}")

        if parent_index is None:
            return None

        # Step 3: Get children chassis at next level
        success, child_items = api.get_inventory_filtered(
            device_id,
            ent_physical_class="chassis",
            ent_physical_contained_in=parent_index,
        )

        if not success:
            return None

        # Filter for chassis only (in case API filter didn't work)
        chassis_items = [item for item in (child_items or []) if item.get("entPhysicalClass") == "chassis"]

        # Step 4: Multiple chassis = stack
        if len(chassis_items) <= 1:
            return None

        # Step 5: Extract member info
        # First pass: collect raw entPhysicalParentRelPos values to detect 0-based
        # indexing.  Some vendors use 0-based positions (0,1,2,3,4) instead of the
        # RFC 2737 standard 1-based (1,2,3,4,5).  If any raw position is 0, shift
        # all valid positions up by 1 so the resulting set is always 1-based.
        raw_positions = []
        for chassis in chassis_items:
            raw = chassis.get("entPhysicalParentRelPos")
            try:
                raw_positions.append(int(raw))
            except (TypeError, ValueError):
                raw_positions.append(None)

        valid_positions = [p for p in raw_positions if p is not None]
        zero_based = bool(valid_positions) and min(valid_positions) == 0

        # Identify the master member by matching the LibreNMS device serial
        # against the ENTITY-MIB serials.  The device-level serial reported by
        # LibreNMS corresponds to the active/master switch in the stack.
        device_serial = ""
        if device_info:
            device_serial = _norm_serial(device_info.get("serial"))

        # Load naming pattern once to avoid a DB query per member.
        vc_name_pattern = _load_vc_member_name_pattern() if master_name else None
        members = []
        for idx, chassis in enumerate(chassis_items):
            raw_pos = raw_positions[idx]
            if raw_pos is not None:
                position = raw_pos + 1 if zero_based else raw_pos
                # Guard against negative or zero after shift
                if position <= 0:
                    position = idx + 1
            else:
                position = idx + 1

            serial = chassis.get("entPhysicalSerialNum", "")
            is_master = bool(device_serial and _norm_serial(serial) == device_serial)

            member_data = {
                "serial": serial,
                "position": position,
                "model": chassis.get("entPhysicalModelName", ""),
                "name": chassis.get("entPhysicalName", ""),
                "index": chassis.get("entPhysicalIndex"),
                "description": chassis.get("entPhysicalDescr", ""),
                "is_master": is_master,
            }

            # Generate suggested name if we have master name.
            # position is already 1-based, so pass it directly (no +1).
            if master_name:
                member_data["suggested_name"] = _generate_vc_member_name(
                    master_name, position, serial=_norm_serial(serial), pattern=vc_name_pattern
                )
            else:
                member_data["suggested_name"] = f"Member-{position}"

            members.append(member_data)

        # Sort by position
        members.sort(key=lambda m: m["position"])

        if zero_based:
            logger.debug(
                f"VC detection: corrected 0-based entPhysicalParentRelPos for device {device_id} "
                f"(raw min={min(valid_positions)})"
            )

        master_member = next((m for m in members if m["is_master"]), None)
        if master_member:
            logger.info(
                f"Detected stack with {len(members)} members for device {device_id}; "
                f"master at position {master_member['position']} (serial {device_serial})"
            )
        else:
            logger.info(
                f"Detected stack with {len(members)} members for device {device_id}; "
                f"master could not be identified by serial"
            )

        return {"is_stack": True, "member_count": len(members), "members": members}

    except Exception as e:
        logger.exception(f"Error detecting virtual chassis for device {device_id}: {e}")
        return None


def _load_vc_member_name_pattern() -> str:
    """Load the VC member name pattern from settings, with fallback to default."""
    from ..models import LibreNMSSettings

    default = "-M{position}"
    try:
        settings = LibreNMSSettings.objects.order_by("pk").first()
        if not settings:
            return default
        pattern = settings.vc_member_name_pattern
        return pattern if isinstance(pattern, str) and pattern.strip() else default
    except Exception as e:
        logger.warning(f"Could not load VC member name pattern from settings: {e}. Using default.")
        return default


def _generate_vc_member_name(master_name: str, position: int, serial: str = None, pattern: str = None) -> str:
    """
    Generate name for VC member device using configured pattern from settings.

    Args:
        master_name: Name of the master/primary device
        position: VC position number
        serial: Optional serial number of the member device
        pattern: Optional pre-loaded name pattern; if None, loaded from settings.
                 Pass a pre-loaded pattern when calling inside a loop to avoid
                 repeated DB queries.

    Returns:
        Generated member device name

    Examples:
        pattern="-M{position}" -> "switch01-M2"
        pattern=" ({position})" -> "switch01 (2)"
        pattern="-SW{position}" -> "switch01-SW2"
        pattern=" [{serial}]" -> "switch01 [ABC123]"
    """
    if pattern is None:
        pattern = _load_vc_member_name_pattern()

    # Prepare format variables
    format_vars = {
        "master_name": master_name,
        "position": position,
        "serial": serial or "",
    }

    # Apply pattern - pattern should be suffix/prefix, not full name
    try:
        formatted_suffix = pattern.format(**format_vars)
        return f"{master_name}{formatted_suffix}"
    except (KeyError, ValueError, IndexError) as e:
        logger.error(f"Invalid placeholder in VC naming pattern '{pattern}': {e}. Using default.")
        return f"{master_name}-M{position}"


def update_vc_member_suggested_names(vc_data: dict, master_name: str) -> dict:
    """
    Regenerate suggested VC member names using the actual master device name.

    This ensures preview shows accurate names after use_sysname and strip_domain
    are applied to the master device name.

    Args:
        vc_data: Virtual chassis detection data dict
        master_name: The actual name that will be used for master device in NetBox

    Returns:
        Updated vc_data dict with corrected suggested_name for each member
    """
    if not vc_data or not vc_data.get("is_stack"):
        return vc_data

    # Load naming pattern once to avoid a DB query per member
    vc_pattern = _load_vc_member_name_pattern()
    for idx, member in enumerate(vc_data.get("members", [])):
        # Positions are stored as 1-based (from entPhysicalParentRelPos or idx+1 fallback).
        # Use them directly for name generation; only replace 0/negative with 1-based fallback.
        raw_position = member.get("position", idx + 1)
        try:
            position = int(raw_position)
            if position <= 0:
                position = idx + 1
        except (TypeError, ValueError):
            position = idx + 1
        member["position"] = position
        member["suggested_name"] = _generate_vc_member_name(
            master_name, position, serial=_norm_serial(member.get("serial")), pattern=vc_pattern
        )

    return vc_data


def _safe_pos(value) -> int | None:
    """Return int position or None if not parseable."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _norm_serial(s) -> str:
    """Normalize serial: strip whitespace; treat '-' as absent."""
    s = str(s or "").strip()
    return "" if s == "-" else s


def create_virtual_chassis_with_members(
    master_device: Device, members_info: list, libre_device: dict, server_key: str | None = None
) -> VirtualChassis:
    """
    Create Virtual Chassis and member devices from detection info.

    This function creates a NetBox VirtualChassis with the master device
    and all detected member devices, wrapped in a transaction for safety.

    Args:
        master_device: The imported device (becomes VC master)
        members_info: List of member dicts from VC detection
        libre_device: Original LibreNMS device data

    Returns:
        VirtualChassis: The created virtual chassis instance

    Raises:
        ValidationError: If member count validation fails
        IntegrityError: If duplicate serials/names are detected
        Exception: For other creation errors

    Example members_info:
        [
            {'serial': 'ABC123', 'position': 0, 'model': 'C9300-48U', 'name': 'Switch 1'},
            {'serial': 'ABC124', 'position': 1, 'model': 'C9300-48U', 'name': 'Switch 2'}
        ]
    """

    # Save originals for in-memory rollback — transaction.atomic() rolls back DB but
    # not in-memory model fields.
    original_master_name = master_device.name
    original_vc = master_device.virtual_chassis
    original_vc_position = master_device.vc_position

    # Find master's actual VC position from members_info.
    # Priority: is_master flag (set during detection) → serial match → default 1.
    _master_pos = 1
    _master_member = next((m for m in members_info if m.get("is_master")), None)
    if _master_member:
        _found_pos = _safe_pos(_master_member.get("position"))
        if _found_pos and _found_pos >= 1:
            _master_pos = _found_pos
    elif _norm_serial(master_device.serial):
        for _m in members_info:
            if _norm_serial(_m.get("serial")) == _norm_serial(master_device.serial):
                _found_pos = _safe_pos(_m.get("position"))
                if _found_pos and _found_pos >= 1:
                    _master_pos = _found_pos
                break

    try:
        with transaction.atomic():
            # Load naming pattern once to avoid a DB query per member
            vc_pattern = _load_vc_member_name_pattern()
            # Rename master device to include position 1 pattern
            master_device_new_name = _generate_vc_member_name(
                original_master_name, _master_pos, serial=_norm_serial(master_device.serial), pattern=vc_pattern
            )

            # Check if renamed master conflicts with existing device
            if Device.objects.filter(name=master_device_new_name).exclude(pk=master_device.pk).exists():
                logger.warning(
                    f"Cannot rename master to '{master_device_new_name}' - name already exists. "
                    f"Keeping original name '{original_master_name}'"
                )
                master_base_name = original_master_name
            else:
                master_device.name = master_device_new_name
                master_base_name = original_master_name

            # Create VC using original base name
            vc_name = master_base_name
            _device_id = libre_device.get("device_id") or master_device.pk
            _domain_prefix = f"librenms-{server_key}" if server_key else "librenms"
            vc = VirtualChassis.objects.create(
                name=vc_name,
                master=master_device,
                domain=f"{_domain_prefix}-{_device_id}",
            )

            # Update master device
            master_device.virtual_chassis = vc
            master_device.vc_position = _master_pos
            master_device.save()

            # Create member devices for remaining positions
            position = _master_pos + 1  # Start after master position
            used_positions = {_master_pos}  # Master occupies its actual position
            members_created = 0

            for member in members_info:
                # Normalize serial and position up front so all skip-checks and
                # downstream logic use consistent values (strips whitespace and
                # treats the sentinel "-" as "no serial").
                serial = str(member.get("serial") or "").strip()
                if serial == "-":
                    serial = ""
                member_pos = _safe_pos(member.get("position"))

                # Skip the master member — identified by is_master flag, serial match,
                # or position match.
                if member.get("is_master"):
                    continue
                # Skip if this is the master's serial (only when both serials are non-empty)
                if serial and serial == _norm_serial(master_device.serial):
                    continue
                # Skip blank-serial entries that represent the master slot by position
                if (
                    not serial
                    and member_pos is not None
                    and master_device.vc_position is not None
                    and member_pos == master_device.vc_position
                ):
                    continue

                member_rack = master_device.rack
                member_location = master_device.location or (
                    member_rack.location if member_rack and member_rack.location else None
                )

                # Check for duplicate serial
                if serial and Device.objects.filter(serial=serial).exists():
                    logger.warning(f"Device with serial '{serial}' already exists, skipping VC member creation")
                    continue

                # Prefer the discovered SNMP position; fall back to sequential counter.
                # member_pos was normalized via _safe_pos() above; 0 is not a valid vc_position.
                discovered_pos = member_pos if (member_pos is not None and member_pos >= 1) else None
                # If discovered_pos is already taken by another member, treat as absent.
                if discovered_pos is not None and discovered_pos in used_positions:
                    discovered_pos = None
                # Consume next free sequential slot when no valid discovered_pos.
                if discovered_pos is None:
                    while position in used_positions:
                        position += 1
                    chosen_pos = position
                    position += 1
                else:
                    chosen_pos = discovered_pos
                    # Advance sequential counter past chosen position.
                    position = max(position, chosen_pos + 1)
                used_positions.add(chosen_pos)

                member_name = _generate_vc_member_name(master_base_name, chosen_pos, serial=serial, pattern=vc_pattern)

                # Check for duplicate name
                if Device.objects.filter(name=member_name).exists():
                    logger.warning(f"Device with name '{member_name}' already exists, skipping VC member creation")
                    continue

                Device.objects.create(
                    name=member_name,
                    device_type=master_device.device_type,
                    role=master_device.role,
                    site=master_device.site,
                    location=member_location,
                    rack=member_rack,
                    platform=master_device.platform,
                    serial=serial,
                    virtual_chassis=vc,
                    vc_position=chosen_pos,
                    comments=f"VC member (LibreNMS: {member.get('name', 'Unknown')})\n"
                    f"Auto-created from stack inventory",
                )
                members_created += 1

            # Validate member count
            # Validate member count — exclude master-slot entries with blank serials
            expected_members = len(
                [
                    m
                    for m in members_info
                    if not (
                        _norm_serial(m.get("serial"))
                        and _norm_serial(m.get("serial")) == _norm_serial(master_device.serial)
                    )
                    and not (
                        not _norm_serial(m.get("serial"))
                        and m.get("position") is not None
                        and master_device.vc_position is not None
                        and _safe_pos(m["position"]) == master_device.vc_position
                    )
                ]
            )
            if members_created < expected_members:
                logger.warning(
                    f"Created {members_created} members but expected {expected_members}. "
                    "Some members may have been skipped due to duplicates."
                )

            logger.info(
                f"Created Virtual Chassis '{vc.name}' with {vc.members.count()} total members "
                f"(1 master + {members_created} additional)"
            )

            return vc

    except Exception as e:
        master_device.name = original_master_name
        master_device.virtual_chassis = original_vc
        master_device.vc_position = original_vc_position
        logger.error(
            f"Virtual Chassis creation failed for device {original_master_name}: {e}",
            exc_info=True,
        )
        raise
