"""Virtual chassis detection, creation, and management."""

import logging
from typing import List, NamedTuple

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
        "master_identified": False,
        "detection_failed": False,
        "detection_error": None,
    }


def _clone_virtual_chassis_data(data: dict | None) -> dict:
    """Return a defensive copy of cached VC data to avoid shared references."""
    if not data:
        return empty_virtual_chassis_data()

    members = [member.copy() for member in data.get("members", [])]
    for member, position in zip(members, _member_positions([m.get("position") for m in members]), strict=True):
        member["position"] = position

    member_count = data.get("member_count") or len(members)

    return {
        "is_stack": bool(data.get("is_stack")),
        "member_count": member_count,
        "members": members,
        # Import creates the chassis only when detection names the master; the UI says so too.
        "master_identified": vc_master_member(members) is not None,
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


def vc_master_member(members: list) -> dict | None:
    """Return the detected member that is the stack master, or None when detection named none."""
    return next((member for member in members if member.get("is_master")), None)


def _member_entries(rows: list, *, model_field: str, master_serial: str, serial_key) -> list[dict]:
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
            "is_master": bool(master_serial and serial_key(row.get("entPhysicalSerialNum")) == master_serial),
        }
        for row, position in zip(rows, positions, strict=True)
    ]
    members.sort(key=lambda member: member["position"])
    return members


def extract_vc_members(rows: list, device_serial=None, *, serial_key=normalize_stack_serial) -> list[dict]:
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
        serial_key: Turns a serial into the form that master matching compares.

    Returns:
        list[dict]: The members in position order, or [] when the rows describe no stack.

    """
    return _extract(rows, device_serial, serial_key)[2]


def _extract(rows, device_serial, serial_key=normalize_stack_serial) -> tuple:
    """Return the stack root, its member rows and the members for :func:`extract_vc_members`; (None, [], []) for no stack."""
    rows = [row for row in rows if isinstance(row, dict)]
    roots = [row for row in rows if _as_int(row.get("entPhysicalContainedIn")) == 0]
    parent_index = select_vc_parent_index(roots)
    if parent_index is None:
        return None, [], []
    root = next(row for row in roots if _as_int(row.get("entPhysicalIndex")) == parent_index)
    children = [row for row in rows if _as_int(row.get("entPhysicalContainedIn")) == parent_index]

    chassis = [row for row in children if row.get("entPhysicalClass") == "chassis"]
    if len(chassis) >= 2:
        master_serial = serial_key(device_serial)
        members = _member_entries(
            chassis, model_field="entPhysicalModelName", master_serial=master_serial, serial_key=serial_key
        )
        return root, chassis, members
    fpcs = _junos_fpc_rows(root, children)
    if fpcs:
        master_serial = serial_key(root.get("entPhysicalSerialNum"))
        members = _member_entries(
            fpcs, model_field="entPhysicalName", master_serial=master_serial, serial_key=serial_key
        )
        return root, fpcs, members
    return None, [], []


class VCMemberRows(NamedTuple):
    """The inventory rows that :func:`extract_vc_members` reads as a stack, keyed by raw index."""

    root_index: object
    positions: dict
    # Junos only: {FPC slot: FPC row index} for slots that exactly one FPC reports.
    fpc_slots: dict

    def fpc_named_by(self, row: dict):
        """
        Return the raw index of the FPC row that a Junos "FPC <n> ..." row names, or None.

        Only a row directly under the Junos root that is no member row (a PSU or fan tray) names
        its FPC this way, and it belongs to whatever member that FPC row resolves to. A slot that
        no FPC or more than one FPC reports names nothing.
        """
        if row.get("entPhysicalContainedIn") != self.root_index or row.get("entPhysicalIndex") in self.positions:
            return None
        descr = row.get("entPhysicalDescr")
        named = JUNOS_FPC_MEMBER_DESCR_RE.match(descr) if isinstance(descr, str) else None
        return self.fpc_slots.get(int(named.group("member"))) if named else None


def vc_member_rows(rows) -> VCMemberRows:
    """
    Return the stack root and the member rows of an inventory, from :func:`extract_vc_members`.

    Only a member row's position is a member number. Module sync reads it to attribute rows,
    so it shares one stack definition with import detection and the serials modal. With no
    stack, ``root_index`` is None and ``positions`` is empty.

    Args:
        rows: Inventory rows that hold the roots and at least their direct children.

    Returns:
        VCMemberRows: The stack root's raw ``entPhysicalIndex``, ``{entPhysicalIndex: member position}``
            and, for a Junos stack, ``{FPC slot: FPC row index}`` for the slots that one FPC reports.

    """
    root, member_rows, members = _extract(rows, None)
    if root is None:
        return VCMemberRows(None, {}, {})
    fpc_slots = {}
    if all(_is_junos_fpc(row) for row in member_rows):
        slots = [_as_int(row.get("entPhysicalParentRelPos")) for row in member_rows]
        fpc_slots = {
            slot: row.get("entPhysicalIndex")
            for row, slot in zip(member_rows, slots, strict=True)
            if is_vc_position(slot) and slots.count(slot) == 1
        }
    return VCMemberRows(
        root.get("entPhysicalIndex"), {member["index"]: member["position"] for member in members}, fpc_slots
    )


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

        # The seeded serial rules strip vendor marks such as Juniper's "S/N " before master matching.
        serial_rules = preload_normalization_rules("serial")

        def serial_key(value):
            return normalize_stack_serial(normalize_inventory_serial(value, preloaded_rules=serial_rules))

        members = extract_vc_members(
            [*root_items, *(child_items or [])], device_serial=device_serial, serial_key=serial_key
        )
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
    members = vc_data.get("members", [])
    for member, position in zip(members, _member_positions([m.get("position") for m in members]), strict=True):
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
    """Return the reported member positions, or row order from 1 when any is missing, negative or shared."""
    parsed = [_as_int(raw) for raw in raw_positions]
    if all(is_vc_position(position) for position in parsed) and len(set(parsed)) == len(parsed):
        return parsed
    # Repairing one member at a time could reuse a slot that another member reports.
    return list(range(1, len(parsed) + 1))


def _sync_module_bay_counter(device: Device) -> None:
    """Reconcile device module_bay_count with actual ModuleBay rows in the DB; a failure fails the caller's transaction."""
    actual_count = device.modulebays.count()
    if getattr(device, "module_bay_count", None) != actual_count:
        Device.objects.filter(pk=device.pk).update(module_bay_count=actual_count)
        device.module_bay_count = actual_count


# Import creates no chassis for a stack whose master detection could not name: guessing it splits
# the stack into more devices than switches.
VC_MASTER_UNKNOWN_REASON = "the stack master could not be identified by serial"
VC_MASTER_UNKNOWN_WARNING = "Imported device {device_id} without a virtual chassis: " + VC_MASTER_UNKNOWN_REASON + "."


class VirtualChassisMasterUnknownError(ValueError):
    """The detected stack names no master, so no chassis can be built around the imported device."""


def create_virtual_chassis_with_members(
    master_device: Device, members_info: list, libre_device: dict, server_key: str | None = None
) -> VirtualChassis:
    """
    Create Virtual Chassis and member devices from detection info.

    This function creates a NetBox VirtualChassis with the master device
    and all detected member devices, wrapped in a transaction for safety.

    Args:
        master_device: The imported device (becomes VC master)
        members_info: List of member dicts from VC detection, the master included and marked
            ``is_master``
        libre_device: Original LibreNMS device data
        server_key: LibreNMS server key stored with the created members.

    Returns:
        VirtualChassis: The created virtual chassis instance

    Raises:
        VirtualChassisMasterUnknownError: No member is marked as the master. Nothing is written.
        IntegrityError: If duplicate serials/names are detected
        Exception: For other creation errors

    Example members_info:
        [
            {'serial': 'ABC123', 'position': 0, 'model': 'C9300-48U', 'name': 'Switch 1', 'is_master': True},
            {'serial': 'ABC124', 'position': 1, 'model': 'C9300-48U', 'name': 'Switch 2'}
        ]

    """
    master_member = vc_master_member(members_info)
    if master_member is None:
        raise VirtualChassisMasterUnknownError(VC_MASTER_UNKNOWN_REASON)
    # Caller-supplied members can lack positions; the stack rule then numbers them in row order.
    positions = _member_positions([member.get("position") for member in members_info])
    master_pos = next(pos for member, pos in zip(members_info, positions, strict=True) if member is master_member)

    # Save originals for in-memory rollback — transaction.atomic() rolls back DB but
    # not in-memory model fields.
    original_master_name = master_device.name
    original_vc = master_device.virtual_chassis
    original_vc_position = master_device.vc_position

    # The ENTITY-MIB serial carries the vendor's decoration ("S/N BCFB9793" on Juniper) while the
    # stored device serial does not, so member serials go through the same rule chain.
    member_manufacturer = getattr(getattr(master_device, "device_type", None), "manufacturer", None)
    serial_rules = preload_normalization_rules("serial", manufacturer=member_manufacturer)

    def _member_serial(value):
        """Return a member's serial normalized the way the stored device serial was written."""
        return normalize_stack_serial(
            normalize_inventory_serial(value, manufacturer=member_manufacturer, preloaded_rules=serial_rules)
        )

    master_serial = normalize_stack_serial(master_device.serial)

    try:
        with transaction.atomic():
            master_device.snapshot()
            # Load naming pattern once to avoid a DB query per member
            vc_pattern = _load_vc_member_name_pattern()
            # Rename the master device with its own position
            master_device_new_name = _generate_vc_member_name(
                original_master_name, master_pos, serial=master_serial, pattern=vc_pattern
            )

            # Check if renamed master conflicts with existing device
            rename_master = not Device.objects.filter(name=master_device_new_name).exclude(pk=master_device.pk).exists()
            if rename_master:
                master_device.name = master_device_new_name
            else:
                logger.warning(
                    f"Cannot rename master to '{master_device_new_name}' - name already exists. "
                    f"Keeping original name '{original_master_name}'"
                )

            # Create VC using original base name
            _device_id = libre_device.get("device_id") or master_device.pk
            _domain_prefix = f"librenms-{server_key}" if server_key else "librenms"
            vc = VirtualChassis.objects.create(
                name=original_master_name,
                domain=f"{_domain_prefix}-{_device_id}",
            )

            # Update master device
            master_device.virtual_chassis = vc
            master_device.vc_position = master_pos
            save_fields = ["virtual_chassis", "vc_position", "last_updated"]
            if rename_master:
                save_fields.append("name")
            master_device.save(update_fields=save_fields)

            members_created = _create_member_devices(
                master_device,
                original_master_name,
                members_info,
                positions,
                master_member,
                vc,
                vc_pattern,
                _member_serial,
            )
            expected_members = len(members_info) - 1
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


def _create_member_devices(
    master_device, base_name, members_info, positions, master_member, vc, vc_pattern, member_serial
) -> int:
    """Create a device for each non-master member at its stack position; return how many were created."""
    member_rack = master_device.rack
    member_location = master_device.location or (member_rack.location if member_rack and member_rack.location else None)
    created = 0
    for member, position in zip(members_info, positions, strict=True):
        if member is master_member:
            continue
        serial = member_serial(member.get("serial"))
        if serial and find_devices_by_serial(serial, limit=1):
            logger.warning(f"Device with serial '{serial}' already exists, skipping VC member creation")
            continue
        member_name = _generate_vc_member_name(base_name, position, serial=serial, pattern=vc_pattern)
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
            vc_position=position,
            comments=f"VC member (LibreNMS: {member.get('name', 'Unknown')})\nAuto-created from stack inventory",
        )
        created += 1
    return created
