"""Virtual chassis detection, creation, and caching."""

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
        raw_position = member_copy.get("position", idx)
        try:
            member_copy["position"] = int(raw_position)
        except (TypeError, ValueError):
            member_copy["position"] = idx
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
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return _clone_virtual_chassis_data(cached)

    detection_data = detect_virtual_chassis_from_inventory(api, device_id)
    if detection_data and "detection_error" not in detection_data:
        detection_data["detection_error"] = None

    cache_value = _clone_virtual_chassis_data(detection_data) if detection_data else empty_virtual_chassis_data()

    cache_timeout = getattr(api, "cache_timeout", 300) or 300
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


def detect_virtual_chassis_from_inventory(api: LibreNMSAPI, device_id: int) -> dict:
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
        # Could be class="stack" or the main "chassis"
        parent_index = None
        for item in root_items:
            item_class = item.get("entPhysicalClass")
            if item_class in ["stack", "chassis"]:
                parent_index = item.get("entPhysicalIndex")
                logger.debug(f"VC detection: Found parent container at index {parent_index} for device {device_id}")
                break

        if not parent_index:
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
        members = []
        for idx, chassis in enumerate(chassis_items):
            raw_position = chassis.get("entPhysicalParentRelPos", idx)
            try:
                position = int(raw_position)
            except (TypeError, ValueError):
                position = idx
            member_data = {
                "serial": chassis.get("entPhysicalSerialNum", ""),
                "position": position,
                "model": chassis.get("entPhysicalModelName", ""),
                "name": chassis.get("entPhysicalName", ""),
                "index": chassis.get("entPhysicalIndex"),
                "description": chassis.get("entPhysicalDescr", ""),
            }

            # Generate suggested name if we have master name
            if master_name:
                member_data["suggested_name"] = _generate_vc_member_name(master_name, position + 1)
            else:
                member_data["suggested_name"] = f"Member-{position + 1}"

            members.append(member_data)

        # Sort by position
        members.sort(key=lambda m: m["position"])

        logger.info(f"Detected stack with {len(members)} members for device {device_id}")

        return {"is_stack": True, "member_count": len(members), "members": members}

    except Exception as e:
        logger.exception(f"Error detecting virtual chassis for device {device_id}: {e}")
        return None


def _generate_vc_member_name(master_name: str, position: int, serial: str = None) -> str:
    """
    Generate name for VC member device using configured pattern from settings.

    Args:
        master_name: Name of the master/primary device
        position: VC position number
        serial: Optional serial number of the member device

    Returns:
        Generated member device name

    Examples:
        pattern="-M{position}" -> "switch01-M2"
        pattern=" ({position})" -> "switch01 (2)"
        pattern="-SW{position}" -> "switch01-SW2"
        pattern=" [{serial}]" -> "switch01 [ABC123]"
    """
    # Import here to avoid circular dependency
    from ..models import LibreNMSSettings

    # Get pattern from settings with fallback to default
    try:
        settings = LibreNMSSettings.objects.first()
        pattern = settings.vc_member_name_pattern if settings else "-M{position}"
    except Exception as e:
        logger.warning(f"Could not load VC member name pattern from settings: {e}. Using default.")
        pattern = "-M{position}"

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
    except KeyError as e:
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

    for idx, member in enumerate(vc_data.get("members", [])):
        raw_position = member.get("position", idx)
        try:
            base_position = int(raw_position)
        except (TypeError, ValueError):
            base_position = idx
        position = base_position + 1  # Convert to 1-based position
        member["position"] = base_position
        member["suggested_name"] = _generate_vc_member_name(master_name, position, serial=member.get("serial"))

    return vc_data


def create_virtual_chassis_with_members(master_device: Device, members_info: list, libre_device: dict):
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

    # Store original master device state for rollback
    original_master_name = master_device.name
    original_vc = master_device.virtual_chassis
    original_vc_position = master_device.vc_position

    try:
        with transaction.atomic():
            # Rename master device to include position 1 pattern
            master_device_new_name = _generate_vc_member_name(original_master_name, 1, serial=master_device.serial)

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
            vc = VirtualChassis.objects.create(
                name=vc_name,
                master=master_device,
                domain=f"librenms-{libre_device['device_id']}",
            )

            # Update master device
            master_device.virtual_chassis = vc
            master_device.vc_position = 1  # Master is position 1
            master_device.save()

            # Create member devices for remaining positions
            position = 2  # Start at 2 (master is 1)
            members_created = 0

            for member in members_info:
                # Skip if this is the master's serial
                if member.get("serial") == master_device.serial:
                    continue

                serial = member.get("serial")

                member_rack = master_device.rack
                member_location = master_device.location or (
                    member_rack.location if member_rack and member_rack.location else None
                )

                # Check for duplicate serial
                if serial and Device.objects.filter(serial=serial).exists():
                    logger.warning(f"Device with serial '{serial}' already exists, skipping VC member creation")
                    continue

                member_name = _generate_vc_member_name(master_base_name, position, serial=serial)

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
                    vc_position=position,
                    comments=f"VC member (LibreNMS: {member.get('name', 'Unknown')})\n"
                    f"Auto-created from stack inventory",
                )
                members_created += 1
                position += 1

            # Validate member count
            expected_members = len([m for m in members_info if m.get("serial") != master_device.serial])
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
        # Rollback master device to original state
        logger.error(
            f"Virtual Chassis creation failed for device {master_device.name}: {e}. Rolling back master device changes."
        )
        master_device.name = original_master_name
        master_device.virtual_chassis = original_vc
        master_device.vc_position = original_vc_position
        master_device.save()
        raise
