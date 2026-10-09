"""Virtual chassis detection, creation, and management."""

import logging
from typing import List

from dcim.models import Device, VirtualChassis
from django.core.cache import cache
from django.db import transaction

from ..constants import JUNOS_FPC_DESCR_PREFIX, JUNOS_FPC_MEMBER_DESCR_RE, JUNOS_VC_ROOT_DESCR_MARKER
from ..librenms_api import LibreNMSAPI
from ..utils import (
    exception_text_for,
    find_devices_by_serial,
    is_vc_position,
    normalize_inventory_serial,
    normalize_stack_serial,
    preload_normalization_rules,
)

logger = logging.getLogger(__name__)


def empty_virtual_chassis_data() -> dict:
    """Public helper for callers that need a blank VC payload."""
    return {
        "is_stack": False,
        "member_count": 0,
        "members": [],
        "detection_failed": False,
        "detection_error": None,
    }


def _clone_virtual_chassis_data(data: dict | None) -> dict:
    """Return a defensive copy of cached VC data to avoid shared references."""
    if not data:
        return empty_virtual_chassis_data()

    members = []
    for idx, member in enumerate(data.get("members", [])):
        member_copy = member.copy()
        position = _as_int(member_copy.get("position"))
        member_copy["position"] = position if is_vc_position(position) else idx + 1
        members.append(member_copy)

    member_count = data.get("member_count") or len(members)

    return {
        "is_stack": bool(data.get("is_stack")),
        "member_count": member_count,
        "members": members,
        "detection_failed": bool(data.get("detection_failed")),
        "detection_error": data.get("detection_error"),
    }


def _failed_virtual_chassis_data(error: str) -> dict:
    """Return a VC payload that distinguishes a failed read from a non-stack device."""
    data = empty_virtual_chassis_data()
    data["detection_failed"] = True
    data["detection_error"] = error
    return data


_VC_CACHE_VERSION = "v3"


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
        # Cache a confirmed non-stack result so subsequent renders skip repeated reads.
        empty = empty_virtual_chassis_data()
        if cache_timeout != 0:
            cache.set(cache_key, empty, timeout=cache_timeout)
        return _clone_virtual_chassis_data(empty)

    if detection_data.get("detection_failed"):
        return _clone_virtual_chassis_data(detection_data)

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
            logger.warning("Connection error during VC prefetch at device %s: %s", idx, e)
            # Stop processing if connection is broken
            return
        except Exception as e:
            # Log but continue for other errors
            logger.warning("Error prefetching VC data for device %s: %s", device_id, e)

    logger.debug(f"VC cache warming complete for {len(device_ids)} devices")


def select_vc_parent_index(root_rows: list) -> int | None:
    """Return the index of the root that holds the stack members: a stack root first, else a chassis root."""
    for wanted in ("stack", "chassis"):
        for row in root_rows:
            if isinstance(row, dict) and row.get("entPhysicalClass") == wanted:
                index = _as_int(row.get("entPhysicalIndex"))
                if index is not None:
                    return index
    return None


def is_vc_root(row: dict, rows) -> bool:
    """Return whether *row* is the root that holds the stack members, as :func:`extract_vc_members` picks it."""
    if row.get("entPhysicalClass") not in ("stack", "chassis") or _as_int(row.get("entPhysicalContainedIn")) != 0:
        return False
    roots = [item for item in rows if isinstance(item, dict) and _as_int(item.get("entPhysicalContainedIn")) == 0]
    index = select_vc_parent_index(roots)
    return index is not None and _as_int(row.get("entPhysicalIndex")) == index


def _is_junos_vc_root(row: dict) -> bool:
    """Return whether *row* is a chassis root whose description names a Junos Virtual Chassis."""
    descr = row.get("entPhysicalDescr")
    return (
        row.get("entPhysicalClass") == "chassis"
        and _as_int(row.get("entPhysicalContainedIn")) == 0
        and isinstance(descr, str)
        and JUNOS_VC_ROOT_DESCR_MARKER.casefold() in descr.casefold()
    )


def _is_junos_fpc(row: dict) -> bool:
    """Return whether *row* is a Junos FPC container row."""
    descr = row.get("entPhysicalDescr")
    return (
        row.get("entPhysicalClass") == "container"
        and isinstance(descr, str)
        and descr.startswith(JUNOS_FPC_DESCR_PREFIX)
    )


def _junos_fpc_rows(root: dict, children: list) -> list:
    """Return the FPC member rows of a Junos Virtual Chassis root, or [] when any condition fails."""
    if not _is_junos_vc_root(root):
        return []
    fpcs = [row for row in children if _is_junos_fpc(row)]
    serials = [normalize_stack_serial(row.get("entPhysicalSerialNum")) for row in fpcs]
    root_serial = normalize_stack_serial(root.get("entPhysicalSerialNum"))
    # A false stack creates bogus member devices on import, so every condition must hold.
    if len(fpcs) < 2 or not all(serials) or len(set(serials)) != len(serials) or serials.count(root_serial) != 1:
        return []
    return fpcs


def _member_entries(rows: list, *, model_field: str, master_serial: str) -> list[dict]:
    """Return one member entry per inventory row, in position order."""
    positions = _member_positions([row.get("entPhysicalParentRelPos") for row in rows])
    members = [
        {
            "serial": row.get("entPhysicalSerialNum", ""),
            "position": position,
            "model": row.get(model_field, ""),
            "name": row.get("entPhysicalName", ""),
            "index": row.get("entPhysicalIndex"),
            "description": row.get("entPhysicalDescr", ""),
            "is_master": bool(
                master_serial and normalize_stack_serial(row.get("entPhysicalSerialNum")) == master_serial
            ),
        }
        for row, position in zip(rows, positions, strict=True)
    ]
    members.sort(key=lambda member: member["position"])
    return members


def junos_vc_member_number(row: dict, rows_by_index: dict) -> int | None:
    """
    Return the Junos Virtual Chassis member that an inventory row belongs to, or None.

    Only rows below a Junos Virtual Chassis root have a member. An FPC container directly under
    the root is its own member (its parent-relative position, as in :func:`extract_vc_members`),
    a row whose description starts with "FPC <n> " names member n, and any other row takes the
    member of its nearest such ancestor. A Routing Engine number is not a member id, so a
    Routing Engine row has no member here.

    Args:
        row: One inventory row.
        rows_by_index: Inventory rows keyed by their raw ``entPhysicalIndex`` value.

    """
    chain = [row]
    seen = {id(row)}
    while _as_int(chain[-1].get("entPhysicalContainedIn")):
        parent = rows_by_index.get(chain[-1].get("entPhysicalContainedIn"))
        if not isinstance(parent, dict) or id(parent) in seen:
            return None
        chain.append(parent)
        seen.add(id(parent))
    root = chain[-1]
    if not _is_junos_vc_root(root):
        return None
    root_index = _as_int(root.get("entPhysicalIndex"))
    for item in chain[:-1]:
        descr = item.get("entPhysicalDescr")
        named = JUNOS_FPC_MEMBER_DESCR_RE.match(descr) if isinstance(descr, str) else None
        if named:
            return int(named.group("member"))
        if _is_junos_fpc(item) and _as_int(item.get("entPhysicalContainedIn")) == root_index:
            position = _as_int(item.get("entPhysicalParentRelPos"))
            return position if is_vc_position(position) else None
    return None


def extract_vc_members(rows: list, device_serial=None) -> list[dict]:
    """
    Return the Virtual Chassis members that ENTITY-MIB inventory rows describe.

    This is the one definition of a stack member, for import detection, the sync-tab serials
    modal and the data-shape signature. Two shapes describe members:

    * Two or more ``chassis`` children of the stack (or chassis) root. The member whose serial
      matches *device_serial* is the master.
    * Junos: a ``chassis`` root whose description names the Virtual Chassis, with two or more
      ``container`` children whose description starts with "FPC". Each FPC needs its own real
      serial, and the root serial must match exactly one FPC, which is the master. The model is
      the FPC name, and the position is the FPC's parent-relative position (the member id).

    Args:
        rows: Inventory rows. They must hold the root rows and at least their direct children;
            deeper rows are ignored.
        device_serial: The LibreNMS device serial, used to find the master of a chassis stack.

    Returns:
        list[dict]: The members in position order, or [] when the rows describe no stack.

    """
    rows = [row for row in rows if isinstance(row, dict)]
    roots = [row for row in rows if _as_int(row.get("entPhysicalContainedIn")) == 0]
    parent_index = select_vc_parent_index(roots)
    if parent_index is None:
        return []
    root = next(row for row in roots if _as_int(row.get("entPhysicalIndex")) == parent_index)
    children = [row for row in rows if _as_int(row.get("entPhysicalContainedIn")) == parent_index]

    chassis = [row for row in children if row.get("entPhysicalClass") == "chassis"]
    if len(chassis) >= 2:
        return _member_entries(
            chassis, model_field="entPhysicalModelName", master_serial=normalize_stack_serial(device_serial)
        )
    fpcs = _junos_fpc_rows(root, children)
    if fpcs:
        return _member_entries(
            fpcs, model_field="entPhysicalName", master_serial=normalize_stack_serial(root.get("entPhysicalSerialNum"))
        )
    return []


def detect_virtual_chassis_from_inventory(api: LibreNMSAPI, device_id: int) -> dict | None:
    """
    Detect a stack or Virtual Chassis from ENTITY-MIB inventory.

    Reads the root rows and the direct children of the stack (or chassis) root, then lets
    :func:`extract_vc_members` decide which children are members.

    Args:
        api: LibreNMSAPI instance
        device_id: LibreNMS device ID

    Returns:
        dict | None: ``{is_stack, member_count, members, detection_failed, detection_error}``.
            Each member carries serial, position, model, name, index, description, is_master
            and suggested_name. None when the inventory confirms that the device is not a stack.
            A failed inventory read returns an empty payload with detection_failed=True so callers
            can fail closed without caching the failure as a negative result.

    """
    try:
        device_found, device_info = api.get_device_info(device_id)
        master_name = None
        device_serial = None
        if device_found and device_info:
            master_name = device_info.get("sysName") or device_info.get("hostname")
            # The LibreNMS device serial is the serial of the master member.
            device_serial = device_info.get("serial")

        # A confirmed device with no inventory rows is a non-stack. For a missing device,
        # an inventory 404 is a failed read and must not be cached as a non-stack.
        success, root_items = api.get_inventory_filtered(
            device_id, ent_physical_contained_in=0, missing_is_empty=device_found
        )
        if not success:
            logger.warning(f"Could not read root inventory items for device {device_id}")
            return _failed_virtual_chassis_data("LibreNMS root inventory request failed")

        root_items = root_items or []
        parent_index = select_vc_parent_index(root_items)
        if parent_index is None:
            return None

        success, child_items = api.get_inventory_filtered(
            device_id, ent_physical_contained_in=parent_index, missing_is_empty=True
        )
        if not success:
            logger.warning(f"Could not read child inventory for device {device_id}")
            return _failed_virtual_chassis_data("LibreNMS child inventory request failed")

        members = extract_vc_members([*root_items, *(child_items or [])], device_serial=device_serial)
        if not members:
            return None

        # Load naming pattern once to avoid a DB query per member.
        vc_name_pattern = _load_vc_member_name_pattern() if master_name else None
        for member in members:
            if master_name:
                member["suggested_name"] = _generate_vc_member_name(
                    master_name,
                    member["position"],
                    serial=normalize_stack_serial(member["serial"]),
                    pattern=vc_name_pattern,
                )
            else:
                member["suggested_name"] = f"Member-{member['position']}"

        master_member = next((m for m in members if m["is_master"]), None)
        if master_member:
            logger.info(
                f"Detected stack with {len(members)} members for device {device_id}; "
                f"master at position {master_member['position']}"
            )
        else:
            logger.info(
                f"Detected stack with {len(members)} members for device {device_id}; "
                f"master could not be identified by serial"
            )

        return {
            "is_stack": True,
            "member_count": len(members),
            "members": members,
            "detection_failed": False,
            "detection_error": None,
        }

    except Exception as e:
        logger.exception("Error detecting virtual chassis for device %s: %s", device_id, e)
        # The detection result has no viewer: it is cached and shown to any import user.
        return _failed_virtual_chassis_data(exception_text_for(e, VirtualChassis, None) or type(e).__name__)


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
        logger.warning("Could not load VC member name pattern from settings: %s. Using default.", e)
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
        position = _as_int(member.get("position"))
        if not is_vc_position(position):
            position = idx + 1
        member["position"] = position
        member["suggested_name"] = _generate_vc_member_name(
            master_name, position, serial=normalize_stack_serial(member.get("serial")), pattern=vc_pattern
        )

    return vc_data


def _as_int(value) -> int | None:
    """Return an ENTITY-MIB number (position, index, containment) as an int, or None."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _member_positions(raw_positions: list) -> list[int]:
    """Return the reported member positions; a missing, negative or shared one becomes the row order from 1."""
    parsed = [_as_int(raw) for raw in raw_positions]
    return [
        position if is_vc_position(position) and parsed.count(position) == 1 else idx + 1
        for idx, position in enumerate(parsed)
    ]


def _sync_module_bay_counter(device: Device) -> None:
    """Reconcile device module_bay_count with actual ModuleBay rows in the DB; a failure fails the caller's transaction."""
    actual_count = device.modulebays.count()
    if getattr(device, "module_bay_count", None) != actual_count:
        Device.objects.filter(pk=device.pk).update(module_bay_count=actual_count)
        device.module_bay_count = actual_count


def create_virtual_chassis_with_members(  # noqa: C901
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
        server_key: LibreNMS server key stored with the created members.

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
    # Priority: is_master flag (set during detection) → serial match → first slot (0 on a 0-based stack, else 1).
    # The ENTITY-MIB serial carries the vendor's decoration ("S/N BCFB9793" on Juniper) while the
    # stored device serial does not. Resolve the rule chain once here, before the first comparison,
    # so master matching, the member loop and the member-count check all read the same value.
    member_manufacturer = getattr(getattr(master_device, "device_type", None), "manufacturer", None)
    serial_rules = preload_normalization_rules("serial", manufacturer=member_manufacturer)

    def _member_serial(value):
        """Return a member's serial normalized the way the stored device serial was written."""
        return normalize_stack_serial(
            normalize_inventory_serial(value, manufacturer=member_manufacturer, preloaded_rules=serial_rules)
        )

    _master_serial = normalize_stack_serial(master_device.serial)
    # Callers can omit the master's own row, so a 1-based stack keeps slot 1 for the master.
    _reported = [pos for m in members_info if is_vc_position(pos := _as_int(m.get("position")))]
    _master_pos = min([*_reported, 1])
    _master_member = next((m for m in members_info if m.get("is_master")), None)
    if _master_member:
        _found_pos = _as_int(_master_member.get("position"))
        if is_vc_position(_found_pos):
            _master_pos = _found_pos
    elif _master_serial:
        for _m in members_info:
            if _member_serial(_m.get("serial")) == _master_serial:
                _found_pos = _as_int(_m.get("position"))
                if is_vc_position(_found_pos):
                    _master_pos = _found_pos
                break

    try:
        with transaction.atomic():
            master_device.snapshot()
            # Load naming pattern once to avoid a DB query per member
            vc_pattern = _load_vc_member_name_pattern()
            # Rename the master device with its own position
            master_device_new_name = _generate_vc_member_name(
                original_master_name, _master_pos, serial=_master_serial, pattern=vc_pattern
            )

            # Check if renamed master conflicts with existing device
            if Device.objects.filter(name=master_device_new_name).exclude(pk=master_device.pk).exists():
                logger.warning(
                    f"Cannot rename master to '{master_device_new_name}' - name already exists. "
                    f"Keeping original name '{original_master_name}'"
                )
                master_base_name = original_master_name
                rename_master = False
            else:
                master_device.name = master_device_new_name
                master_base_name = original_master_name
                rename_master = True

            # Create VC using original base name
            vc_name = master_base_name
            _device_id = libre_device.get("device_id") or master_device.pk
            _domain_prefix = f"librenms-{server_key}" if server_key else "librenms"
            vc = VirtualChassis.objects.create(
                name=vc_name,
                domain=f"{_domain_prefix}-{_device_id}",
            )

            # Update master device
            master_device.virtual_chassis = vc
            master_device.vc_position = _master_pos
            save_fields = ["virtual_chassis", "vc_position", "last_updated"]
            if rename_master:
                save_fields.append("name")
            master_device.save(update_fields=save_fields)

            # Create member devices for remaining positions
            position = _master_pos + 1  # Start after master position
            used_positions = {_master_pos}  # Master occupies its actual position
            members_created = 0

            for member in members_info:
                # Normalize serial and position up front so all skip-checks and
                # downstream logic use consistent values (strips whitespace and
                # treats the sentinel "-" as "no serial").
                serial = _member_serial(member.get("serial"))
                member_pos = _as_int(member.get("position"))

                # Skip the master member — identified by is_master flag, serial match,
                # or position match.
                if member.get("is_master"):
                    continue
                # Skip if this is the master's serial (only when both serials are non-empty)
                if serial and serial == _master_serial:
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
                if serial and find_devices_by_serial(serial, limit=1):
                    logger.warning(f"Device with serial '{serial}' already exists, skipping VC member creation")
                    continue

                # Prefer the discovered SNMP position; fall back to sequential counter.
                discovered_pos = member_pos if is_vc_position(member_pos) else None
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

            # Validate the member count after excluding entries that identify the master.
            def is_expected_member(member):
                if member.get("is_master"):
                    return False
                serial = _member_serial(member.get("serial"))
                if serial and serial == _master_serial:
                    return False
                return not (
                    not serial
                    and member.get("position") is not None
                    and master_device.vc_position is not None
                    and _as_int(member["position"]) == master_device.vc_position
                )

            expected_members = sum(is_expected_member(member) for member in members_info)
            if members_created < expected_members:
                logger.warning(
                    f"Created {members_created} members but expected {expected_members}. "
                    "Some members may have been skipped due to duplicates."
                )

            # Assign VC master only after all members are attached to avoid
            # NetBox's create-time auto-master signal changing order/state.
            vc.snapshot()
            vc.master = master_device
            vc.save(update_fields=["master", "last_updated"])
            _sync_module_bay_counter(master_device)

            logger.info(
                f"Created Virtual Chassis '{vc.name}' with {vc.members.count()} total members "
                f"(1 master + {members_created} additional)"
            )

            return vc

    except Exception as e:
        master_device.name = original_master_name
        master_device.virtual_chassis = original_vc
        master_device.vc_position = original_vc_position
        logger.error("Virtual Chassis creation failed for device %s: %s", original_master_name, e, exc_info=True)
        raise
