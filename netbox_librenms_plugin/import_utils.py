"""
Utilities for importing devices from LibreNMS to NetBox.

This module provides functions for:
- Validating LibreNMS devices for import
- Retrieving filtered LibreNMS devices
- Importing single and multiple devices
- Smart matching of NetBox objects
"""

import logging
from typing import List

from core.choices import JobStatusChoices
from dcim.models import Device, DeviceRole, DeviceType, Rack, Site, VirtualChassis
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .librenms_api import LibreNMSAPI
from .utils import (
    find_matching_platform,
    find_matching_site,
    match_librenms_hardware_to_device_type,
)

logger = logging.getLogger(__name__)


def get_validated_device_cache_key(
    server_key: str, filters: dict, device_id: int | str, vc_enabled: bool
) -> str:
    """
    Generate a consistent cache key for validated device data.

    This ensures both synchronous and background job processing use the same
    cache keys, avoiding duplicate validation work and cache entries.

    Args:
        server_key: LibreNMS server key
        filters: Filter dict with location, type, os, hostname, sysname keys
        device_id: LibreNMS device ID
        vc_enabled: Whether virtual chassis detection was enabled

    Returns:
        str: Cache key for the validated device

    Example:
        >>> key = get_validated_device_cache_key('default', {'location': 'NYC'}, 123, True)
        >>> key
        'validated_device_default_-1234567890_123_vc'
    """
    # Sort filters for consistent hashing
    filter_hash = hash(str(sorted(filters.items())))
    vc_part = "vc" if vc_enabled else "novc"
    return f"validated_device_{server_key}_{filter_hash}_{device_id}_{vc_part}"


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


def get_virtual_chassis_data(
    api: LibreNMSAPI, device_id: int | str, *, force_refresh: bool = False
) -> dict:
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

    cache_value = (
        _clone_virtual_chassis_data(detection_data)
        if detection_data
        else empty_virtual_chassis_data()
    )

    cache_timeout = getattr(api, "cache_timeout", 300) or 300
    cache.set(cache_key, cache_value, timeout=cache_timeout)
    return _clone_virtual_chassis_data(cache_value)


def prefetch_vc_data_for_devices(
    api: LibreNMSAPI, device_ids: List[int], *, force_refresh: bool = False
) -> None:
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
            logger.info(f"Connection error during VC prefetch at device {idx}: {e}")
            # Stop processing if connection is broken
            return
        except Exception as e:
            # Log but continue for other errors
            logger.warning(f"Error prefetching VC data for device {device_id}: {e}")

    logger.debug(f"VC cache warming complete for {len(device_ids)} devices")


def get_device_count_for_filters(
    api: LibreNMSAPI,
    filters: dict,
    clear_cache: bool = False,
    show_disabled: bool = True,
) -> int:
    """
    Get count of LibreNMS devices matching filters.

    This is a lightweight function to determine device count for background job
    decision making. Uses the same caching as get_librenms_devices_for_import().

    Args:
        api: LibreNMS API client instance
        filters: Filter dict with location, type, os, hostname, sysname keys
        clear_cache: Whether to force cache refresh
        show_disabled: Whether to include disabled devices

    Returns:
        int: Count of devices matching filters

    Example:
        >>> api = LibreNMSAPI()
        >>> count = get_device_count_for_filters(api, {'location': 'NYC'}, show_disabled=False)
        >>> print(f"Found {count} devices")
    """
    devices = get_librenms_devices_for_import(
        api, filters=filters, force_refresh=clear_cache
    )

    # Filter out disabled devices if requested
    if not show_disabled:
        devices = [d for d in devices if d.get("status") == 1]

    return len(devices)


def get_librenms_devices_for_import(
    api: LibreNMSAPI = None,
    filters: dict = None,
    server_key: str = None,
    *,
    force_refresh: bool = False,
    return_cache_status: bool = False,
) -> List[dict] | tuple[List[dict], bool]:
    """
    Retrieve LibreNMS devices based on filters.

    Args:
        api: LibreNMSAPI instance (if not provided, creates one with server_key)
        filters: Dict containing filter parameters:
            - location: LibreNMS location/site filter
            - type: Device type filter
            - os: Operating system filter
            - hostname: Hostname filter (partial match)
            - sysname: System name filter (partial match)
            - status: Device status filter (1=up, 0=down)
            - disabled: Include disabled devices (0=active only, 1=all)
        server_key: Key for specific server configuration (used if api not provided)
        force_refresh: When True, bypass the cache and fetch fresh data
        return_cache_status: When True, returns (devices, from_cache) tuple

    Returns:
        List of device dictionaries from LibreNMS, or tuple of (devices, from_cache)
        if return_cache_status is True. from_cache=True means data was loaded from
        existing cache; from_cache=False means data was just fetched from LibreNMS.

    Example:
        >>> api = LibreNMSAPI()
        >>> devices = get_librenms_devices_for_import(api, {'location': 'NYC'})
        >>> for device in devices:
        ...     print(device['hostname'])
        >>> # With cache status
        >>> devices, from_cache = get_librenms_devices_for_import(api, {'location': 'NYC'}, return_cache_status=True)
    """
    try:
        # Use provided API instance or create a new one
        if api is None:
            api = LibreNMSAPI(server_key=server_key)

        # Build LibreNMS API filters using the type/query format
        # LibreNMS API v0 expects ?type=X&query=Y format, not direct parameters
        # NOTE: API only supports ONE type/query pair, so we'll use the most
        # specific filter for the API and apply others client-side
        api_filters = {}
        client_filters = {}  # Filters to apply after fetching from API

        if filters:
            # Check for status filter first - it has special handling
            if filters.get("status") is not None:
                # Status filter uses special types that don't need query param
                if filters["status"] == 1:
                    api_filters["type"] = "up"
                elif filters["status"] == 0:
                    api_filters["type"] = "down"

                # Save ALL other filters for client-side filtering when status is used
                if filters.get("location"):
                    client_filters["location"] = filters["location"]
                if filters.get("type"):
                    client_filters["type"] = filters["type"]
                if filters.get("os"):
                    client_filters["os"] = filters["os"]
                if filters.get("hostname"):
                    client_filters["hostname"] = filters["hostname"]
                if filters.get("sysname"):
                    client_filters["sysname"] = filters["sysname"]
            else:
                # Priority order for type/query filters: location > type > os > hostname > sysname
                # Note: When sysname is combined with other filters, it's applied client-side for partial matching
                # When sysname is alone, it uses API exact match (type=sysName)
                # Use first available for API, save others for client-side filtering
                if filters.get("location"):
                    api_filters["type"] = "location_id"
                    api_filters["query"] = filters["location"]
                    # Save remaining filters for client-side
                    if filters.get("type"):
                        client_filters["type"] = filters["type"]
                    if filters.get("os"):
                        client_filters["os"] = filters["os"]
                    if filters.get("hostname"):
                        client_filters["hostname"] = filters["hostname"]
                    if filters.get("sysname"):
                        client_filters["sysname"] = filters["sysname"]
                elif filters.get("type"):
                    api_filters["type"] = "type"
                    api_filters["query"] = filters["type"]
                    # Save remaining filters for client-side
                    if filters.get("os"):
                        client_filters["os"] = filters["os"]
                    if filters.get("hostname"):
                        client_filters["hostname"] = filters["hostname"]
                    if filters.get("sysname"):
                        client_filters["sysname"] = filters["sysname"]
                elif filters.get("os"):
                    api_filters["type"] = "os"
                    api_filters["query"] = filters["os"]
                    # Save remaining filters for client-side
                    if filters.get("hostname"):
                        client_filters["hostname"] = filters["hostname"]
                    if filters.get("sysname"):
                        client_filters["sysname"] = filters["sysname"]
                elif filters.get("hostname"):
                    api_filters["type"] = "hostname"
                    api_filters["query"] = filters["hostname"]
                    # Save sysname for client-side
                    if filters.get("sysname"):
                        client_filters["sysname"] = filters["sysname"]
                elif filters.get("sysname"):
                    # sysname-only filter: Use API exact match (type=sysName&query=<value>)
                    # This is safe - returns empty if no exact match found
                    api_filters["type"] = "sysName"
                    api_filters["query"] = filters["sysname"]

            # Note: disabled filter isn't directly supported by LibreNMS API
            # We'll filter client-side if needed

        # Use caching to avoid repeated API calls
        # Include both API and client filters in cache key
        cache_key = f"librenms_devices_import_{server_key}_{hash(str(api_filters))}_{hash(str(client_filters))}"
        from_cache = False

        if force_refresh:
            cache.delete(cache_key)
        else:
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                # No need to deepcopy - cached data isn't mutated
                devices = cached_result
                from_cache = True
                if return_cache_status:
                    return devices, from_cache
                return devices

        success, devices = api.list_devices(api_filters if api_filters else None)

        if not success:
            logger.error(f"Failed to retrieve devices from LibreNMS: {devices}")
            if return_cache_status:
                return [], False
            return []

        # Apply client-side filters if any
        if client_filters:
            devices = _apply_client_filters(devices, client_filters)

        # Cache using configured timeout (default 300s)
        # No need to deepcopy - Django's cache backend handles serialization
        cache.set(cache_key, devices, timeout=api.cache_timeout)

        if return_cache_status:
            return devices, from_cache
        return devices

    except Exception:
        logger.exception("Error retrieving LibreNMS devices for import")
        if return_cache_status:
            return [], False
        return []


def _apply_client_filters(devices: List[dict], filters: dict) -> List[dict]:
    """
    Apply client-side filters to device list.

    Args:
        devices: List of device dicts from LibreNMS
        filters: Dict of filters to apply (location, type, os, hostname, sysname)

    Returns:
        Filtered list of devices
    """
    filtered = devices

    if filters.get("location"):
        location_id = str(filters["location"])
        filtered = [d for d in filtered if str(d.get("location_id", "")) == location_id]

    if filters.get("type"):
        device_type = filters["type"].lower()
        filtered = [d for d in filtered if d.get("type", "").lower() == device_type]

    if filters.get("os"):
        os_filter = filters["os"].lower()
        filtered = [d for d in filtered if os_filter in d.get("os", "").lower()]

    if filters.get("hostname"):
        hostname_filter = filters["hostname"].lower()
        filtered = [
            d for d in filtered if hostname_filter in d.get("hostname", "").lower()
        ]

    if filters.get("sysname"):
        sysname_filter = filters["sysname"].lower()
        filtered = [
            d for d in filtered if sysname_filter in d.get("sysName", "").lower()
        ]

    return filtered


def validate_device_for_import(
    libre_device: dict,
    import_as_vm: bool = False,
    api: "LibreNMSAPI" = None,
    *,
    include_vc_detection: bool = True,
    force_vc_refresh: bool = False,
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
        "existing_device": None,
        "existing_match_type": None,  # Track how existing device was matched
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
    }

    try:
        # 1. Check if device/VM already exists in NetBox
        # Always check both Devices AND VMs to properly detect existing objects
        librenms_id = libre_device.get("device_id")
        hostname = libre_device.get("hostname", "")
        logger.debug(
            f"Checking for existing device/VM: "
            f"librenms_id={librenms_id} (type={type(librenms_id).__name__}), "
            f"hostname={hostname}"
        )

        from virtualization.models import VirtualMachine

        # Check for existing VM first (by librenms_id custom field)
        existing_vm = VirtualMachine.objects.filter(
            custom_field_data__librenms_id=str(librenms_id)
        ).first()
        if not existing_vm and isinstance(librenms_id, str):
            try:
                existing_vm = VirtualMachine.objects.filter(
                    custom_field_data__librenms_id=int(librenms_id)
                ).first()
            except (ValueError, TypeError):
                # librenms_id is not convertible to int; no match will be found
                pass

        if existing_vm:
            logger.info(
                f"Found existing VM: {existing_vm.name} (matched by librenms_id={librenms_id})"
            )
            result["existing_device"] = existing_vm
            result["existing_match_type"] = "librenms_id"
            result["import_as_vm"] = True  # Force VM mode since VM exists
            result["warnings"].append(
                f"VM already imported to NetBox as '{existing_vm.name}'"
            )
            result["can_import"] = False
            return result

        # Check for existing Device (by librenms_id custom field)
        existing_device = Device.objects.filter(
            custom_field_data__librenms_id=str(librenms_id)
        ).first()
        if not existing_device and isinstance(librenms_id, str):
            try:
                existing_device = Device.objects.filter(
                    custom_field_data__librenms_id=int(librenms_id)
                ).first()
            except (ValueError, TypeError):
                # librenms_id is not convertible to int; no match will be found
                pass

        if existing_device:
            logger.info(
                f"Found existing device: {existing_device.name} (matched by librenms_id={librenms_id})"
            )
            result["existing_device"] = existing_device
            result["existing_match_type"] = "librenms_id"
            result["warnings"].append(
                f"Device already imported to NetBox as '{existing_device.name}'"
            )
            result["can_import"] = False
            return result

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
            result["warnings"].append(
                f"VM with same hostname exists in NetBox as '{existing_vm.name}' (not linked to LibreNMS)"
            )
            result["can_import"] = False
            return result
        elif existing_device:
            logger.info(f"Found existing device by hostname: {existing_device.name}")
            result["existing_device"] = existing_device
            result["existing_match_type"] = "hostname"
            result["warnings"].append(
                f"Device with same hostname exists in NetBox as '{existing_device.name}' (not linked to LibreNMS)"
            )
            result["can_import"] = False
            return result

        # Check by primary IP (weaker match, IP could be reassigned) - only for devices
        primary_ip = libre_device.get("ip")
        if primary_ip and not import_as_vm:
            from ipam.models import IPAddress

            existing_ip = IPAddress.objects.filter(
                address__startswith=primary_ip
            ).first()
            if existing_ip and existing_ip.assigned_object:
                device = (
                    existing_ip.assigned_object.device
                    if hasattr(existing_ip.assigned_object, "device")
                    else None
                )
                if device:
                    result["existing_device"] = device
                    result["existing_match_type"] = "primary_ip"
                    result["warnings"].append(
                        f"IP address {primary_ip} already assigned to device '{device.name}' (not linked to LibreNMS)"
                    )
                    result["can_import"] = False
                    return result

        # Validate based on import type (Device or VM)
        if import_as_vm:
            # 2. For VMs: Validate Cluster (required) - Must be manually selected
            from virtualization.models import Cluster

            result["cluster"]["found"] = False
            result["issues"].append(
                "Cluster must be manually selected before importing as VM"
            )
            # Provide list of available clusters for user selection
            all_clusters = Cluster.objects.all()
            result["cluster"]["available_clusters"] = list(all_clusters)

            # Skip device-specific validations for VMs
            result["site"]["found"] = True  # Not required for VMs
            result["device_type"]["found"] = True  # Not required for VMs
            result["device_role"]["found"] = True  # Not required for VMs

        else:
            # 2. For Devices: Validate Site (required)
            location = libre_device.get("location", "")
            site_match = find_matching_site(location)
            result["site"] = site_match

            if not site_match["found"]:
                result["issues"].append(
                    f"No matching site found for location: '{location}'"
                )
                # Get alternative suggestions
                if location:
                    all_sites = Site.objects.all()[:10]  # Limit for performance
                    result["site"]["suggestions"] = list(all_sites)

            # 3. Validate DeviceType (required)
            hardware = libre_device.get("hardware", "")
            dt_match = match_librenms_hardware_to_device_type(hardware)
            result["device_type"] = dt_match

            if not dt_match["matched"]:
                result["issues"].append(
                    f"No matching device type found for hardware: '{hardware}'"
                )
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
            else:
                # Rename 'matched' to 'found' for consistency
                result["device_type"]["found"] = dt_match["matched"]
                result["device_type"]["device_type"] = dt_match["device_type"]
                result["device_type"]["match_type"] = dt_match["match_type"]

            # 4. DeviceRole (required) - Must be manually selected by user
            logger.debug(
                f"[{hostname}] Issues BEFORE adding role issue: {result['issues']}"
            )
            result["device_role"]["found"] = False
            result["issues"].append(
                "Device role must be manually selected before import"
            )
            logger.debug(
                f"[{hostname}] Issues AFTER adding role issue: {result['issues']}"
            )
            # Provide list of available roles for user selection
            all_roles = DeviceRole.objects.all()
            result["device_role"]["available_roles"] = list(all_roles)

            # 4b. Rack (optional) - Provide available racks for the matched site
            if site_match["found"] and site_match["site"]:
                site = site_match["site"]
                # Use cache to optimize rack lookups per site
                cache_key = f"librenms_import_racks_site_{site.pk}"
                available_racks = cache.get(cache_key)

                if available_racks is None:
                    from dcim.models import Rack
                    from django.db.models import Q

                    # Query racks for this site - include both:
                    # 1. Racks assigned to locations within the site
                    # 2. Racks directly assigned to the site (without location)
                    available_racks = list(
                        Rack.objects.filter(Q(location__site=site) | Q(site=site))
                        .select_related("location", "site")
                        .order_by("location__name", "name")
                    )
                    # Cache for 5 minutes
                    cache.set(cache_key, available_racks, 300)

                result["rack"]["available_racks"] = available_racks
                # Rack is optional, don't add to issues
                result["rack"]["found"] = (
                    True  # Mark as "found" even if None (optional field)
                )

            # Skip VM-specific validations for devices
            result["cluster"]["found"] = True  # Not required for devices

        # 5. Match Platform (optional - same for both devices and VMs)
        os = libre_device.get("os", "")
        platform_match = find_matching_platform(os)
        result["platform"] = platform_match

        if not platform_match["found"] and os:
            result["warnings"].append(f"No matching platform found for OS: '{os}'")

        # 6. Additional validations
        if not hostname:
            result["issues"].append("Device has no hostname")

        # Serial number check
        serial = libre_device.get("serial", "")
        if serial and serial != "-":
            existing_serial = Device.objects.filter(serial=serial).first()
            if existing_serial:
                result["warnings"].append(
                    f"Serial number {serial} already exists on device: {existing_serial.name}"
                )

        # 7. Virtual chassis detection (only for devices, not VMs)
        if include_vc_detection and not import_as_vm and api is not None:
            device_id = libre_device.get("device_id")
            if device_id:
                try:
                    logger.debug(
                        f"Calling get_virtual_chassis_data for device {device_id}"
                    )
                    vc_detection = get_virtual_chassis_data(
                        api, device_id, force_refresh=force_vc_refresh
                    )
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
                except Exception as e:
                    logger.exception(
                        f"Exception during VC detection for device {hostname}: {e}"
                    )
                    result["virtual_chassis"]["detection_error"] = str(e)
            else:
                logger.debug(f"No device_id found for {hostname}")

        # 8. Determine if device/VM is ready to import
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

        # Debug logging
        logger.debug(
            f"Validation for {libre_device.get('hostname')} ({'VM' if import_as_vm else 'Device'}): "
            f"issues={len(result['issues'])}, can_import={result['can_import']}, "
            f"issues_list={result['issues']}"
        )

        return result

    except Exception as e:
        logger.exception(
            f"Error validating device for import: {libre_device.get('hostname', 'unknown')}"
        )
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
                }
            }

    Example:
        >>> result = import_single_device(123, manual_mappings={'site_id': 1})
        >>> if result['success']:
        ...     print(f"Imported: {result['device'].name}")
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
            validation = validate_device_for_import(libre_device)

        # Check if device already exists
        if validation.get("existing_device"):
            return {
                "success": False,
                "device": validation["existing_device"],
                "message": "",
                "error": f"Device already exists: {validation['existing_device'].name}",
                "synced": {},
            }

        # Use validation-derived matches, allow manual mappings to override specific fields
        site = validation["site"].get("site")
        device_type = validation["device_type"].get("device_type")
        device_role = validation["device_role"].get("role")
        platform = validation["platform"].get("platform")
        rack = validation.get("rack", {}).get("rack")

        if manual_mappings:
            site = (
                Site.objects.filter(id=manual_mappings.get("site_id")).first() or site
            )
            device_type = (
                DeviceType.objects.filter(
                    id=manual_mappings.get("device_type_id")
                ).first()
                or device_type
            )
            device_role = (
                DeviceRole.objects.filter(
                    id=manual_mappings.get("device_role_id")
                ).first()
                or device_role
            )

            platform_id = manual_mappings.get("platform_id")
            if platform_id:
                from dcim.models import Platform

                platform = Platform.objects.filter(id=platform_id).first() or platform

            rack_id = manual_mappings.get("rack_id")
            if rack_id:
                rack = (
                    Rack.objects.select_related("location", "site")
                    .filter(id=rack_id)
                    .first()
                    or rack
                )

        rack = rack or validation.get("rack", {}).get("rack")

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
            # Determine device name based on sync options
            use_sysname = (
                sync_options.get("use_sysname", True) if sync_options else True
            )
            strip_domain = (
                sync_options.get("strip_domain", False) if sync_options else False
            )

            device_name = _determine_device_name(
                libre_device,
                use_sysname=use_sysname,
                strip_domain=strip_domain,
                device_id=device_id,
            )

            device_data = {
                "name": device_name,
                "site": site,
                "device_type": device_type,
                "role": device_role,
                "status": "active" if libre_device.get("status") == 1 else "offline",
                "custom_field_data": {"librenms_id": str(device_id)},
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
                location = Location.objects.filter(
                    site=site, name__iexact=location_name
                ).first()
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
                logger.info(
                    f"Interface sync should be performed for device {device.name}"
                )

            # Sync cables
            if sync_options.get("sync_cables", True):
                logger.info(f"Cable sync should be performed for device {device.name}")

            # Sync IP addresses
            if sync_options.get("sync_ips", True):
                logger.info(
                    f"IP address sync should be performed for device {device.name}"
                )

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


def bulk_import_devices(
    device_ids: List[int],
    server_key: str = None,
    sync_options: dict = None,
    manual_mappings_per_device: dict = None,
    libre_devices_cache: dict = None,
) -> dict:
    """
    Import multiple LibreNMS devices to NetBox.

    Args:
        device_ids: List of LibreNMS device IDs to import
        server_key: LibreNMS server configuration key
        sync_options: Sync options to apply to all devices
        manual_mappings_per_device: Dict mapping device_id to manual_mappings dict
            Example: {1179: {'device_role_id': 5}, 1180: {'device_role_id': 3}}
        libre_devices_cache: Optional dict mapping device_id to pre-fetched device data
            to avoid redundant API calls. Example: {123: {...device_data...}}

    Returns:
        dict: Bulk import result with structure:
            {
                'total': int,
                'success': List[dict],  # Successfully imported devices
                'failed': List[dict],   # Failed imports with errors
                'skipped': List[dict],  # Skipped devices (already exist, etc.)
                'virtual_chassis_created': int  # Number of VCs created
            }

    Example:
        >>> result = bulk_import_devices([1, 2, 3, 4, 5])
        >>> print(f"Imported {len(result['success'])} of {result['total']} devices")
    """
    total = len(device_ids)
    success_list = []
    failed_list = []
    skipped_list = []
    vc_created_count = 0
    processed_vc_domains = set()  # Track VCs already created by domain

    # Initialize API client once for all devices to avoid repeated config parsing
    api = LibreNMSAPI(server_key=server_key)

    for device_id in device_ids:
        try:
            # Use cached device data if available to avoid redundant API calls
            if libre_devices_cache and device_id in libre_devices_cache:
                libre_device = libre_devices_cache[device_id]
                success = True
            else:
                success, libre_device = api.get_device_info(device_id)

            if not success or not libre_device:
                failed_list.append(
                    {
                        "device_id": device_id,
                        "error": f"Failed to retrieve device {device_id} from LibreNMS",
                    }
                )
                continue

            validation = validate_device_for_import(libre_device, api=api)

            # Build manual mappings from validation + any provided overrides
            device_mappings = {}

            # Get site and device_type from validation
            if validation["site"].get("found") and validation["site"].get("site"):
                device_mappings["site_id"] = validation["site"]["site"].id
            if validation["device_type"].get("found") and validation["device_type"].get(
                "device_type"
            ):
                device_mappings["device_type_id"] = validation["device_type"][
                    "device_type"
                ].id
            if validation["platform"].get("found") and validation["platform"].get(
                "platform"
            ):
                device_mappings["platform_id"] = validation["platform"]["platform"].id

            # Override with any manual mappings provided for this device
            if manual_mappings_per_device and device_id in manual_mappings_per_device:
                device_mappings.update(manual_mappings_per_device[device_id])

            result = import_single_device(
                device_id,
                server_key=server_key,
                sync_options=sync_options,
                manual_mappings=device_mappings if device_mappings else None,
                libre_device=libre_device,
            )

            if result["success"]:
                success_list.append(
                    {
                        "device_id": device_id,
                        "device": result["device"],
                        "message": result["message"],
                    }
                )

                # Handle virtual chassis creation for stacks
                vc_data = validation.get("virtual_chassis", {})
                if vc_data.get("is_stack", False):
                    vc_domain = f"librenms-{device_id}"

                    # Only create VC if we haven't processed this stack yet
                    # Add to set BEFORE attempting creation to prevent race condition
                    if vc_domain not in processed_vc_domains:
                        processed_vc_domains.add(vc_domain)
                        try:
                            vc = create_virtual_chassis_with_members(
                                result["device"],
                                vc_data["members"],
                                libre_device,
                            )
                            vc_created_count += 1
                            logger.info(
                                f"Created VC '{vc.name}' during bulk import for device {device_id}"
                            )
                        except Exception as vc_error:
                            # Remove from set on failure so retry is possible
                            processed_vc_domains.discard(vc_domain)
                            logger.warning(
                                f"Failed to create VC for device {device_id}: {vc_error}"
                            )
                            # Don't fail the import, just log the warning

            elif result.get("device"):  # Device exists
                skipped_list.append({"device_id": device_id, "reason": result["error"]})
            else:  # Failed to import
                failed_list.append({"device_id": device_id, "error": result["error"]})

        except Exception as e:
            logger.exception(f"Unexpected error importing device {device_id}")
            failed_list.append({"device_id": device_id, "error": str(e)})

    return {
        "total": total,
        "success": success_list,
        "failed": failed_list,
        "skipped": skipped_list,
        "virtual_chassis_created": vc_created_count,
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


def create_device_from_librenms(
    libre_device: dict, validation: dict, use_sysname: bool = True
) -> Device:
    """
    Create a NetBox device from LibreNMS device data.

    Args:
        libre_device: Device data from LibreNMS
        validation: Validation result from validate_device_for_import
        use_sysname: If True, prefer sysName; if False, use hostname

    Returns:
        Created Device instance

    Raises:
        Exception if device cannot be created
    """
    if not validation["can_import"]:
        raise ValueError(
            f"Device cannot be imported: {', '.join(validation['issues'])}"
        )

    # Extract matched objects from validation
    site = validation["site"]["site"]
    device_type = validation["device_type"]["device_type"]
    device_role = validation["device_role"]["role"]
    platform = validation["platform"].get("platform")
    rack = validation.get("rack", {}).get("rack")  # Optional rack assignment

    # Determine device name based on use_sysname setting
    device_name = _determine_device_name(
        libre_device,
        use_sysname=use_sysname,
        strip_domain=False,
        device_id=libre_device.get("device_id"),
    )

    # Generate import timestamp comment
    import_time = timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")

    # Create the device with librenms_id custom field
    device = Device.objects.create(
        name=device_name,
        device_type=device_type,
        role=device_role,
        site=site,
        rack=rack,  # Assign rack if selected
        platform=platform,
        serial=libre_device.get("serial", ""),
        comments=f"Imported from LibreNMS by netbox-librenms-plugin on {import_time}",
        custom_field_data={"librenms_id": str(libre_device["device_id"])},
    )

    logger.info(
        f"Created device {device.name} (ID: {device.pk}) from LibreNMS device {libre_device['device_id']}"
    )
    return device


def create_vm_from_librenms(
    libre_device: dict, validation: dict, use_sysname: bool = True, role=None
):
    """
    Create a NetBox VirtualMachine from LibreNMS device data.

    Args:
        libre_device: Device data from LibreNMS
        validation: Validation result from validate_device_for_import with import_as_vm=True
        use_sysname: If True, prefer sysName; if False, use hostname
        role: Optional DeviceRole to assign to the VM

    Returns:
        Created VirtualMachine instance

    Raises:
        Exception if VM cannot be created
    """
    from virtualization.models import VirtualMachine

    if not validation["can_import"]:
        raise ValueError(f"VM cannot be imported: {', '.join(validation['issues'])}")

    # Extract matched objects from validation
    cluster = validation["cluster"]["cluster"]
    platform = validation["platform"].get("platform")

    # Determine VM name - use pre-computed name if available (handles strip_domain)
    vm_name = libre_device.get("_computed_name")
    if not vm_name:
        vm_name = _determine_device_name(
            libre_device,
            use_sysname=use_sysname,
            strip_domain=False,
            device_id=libre_device.get("device_id"),
        )

    # Generate import timestamp comment
    import_time = timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")

    # Create the VM with librenms_id custom field
    vm = VirtualMachine.objects.create(
        name=vm_name,
        cluster=cluster,
        role=role,  # Optional VM role
        platform=platform,
        comments=f"Imported from LibreNMS by netbox-librenms-plugin on {import_time}",
        custom_field_data={"librenms_id": str(libre_device["device_id"])},
    )

    logger.info(
        f"Created VM {vm.name} (ID: {vm.pk}) from LibreNMS device {libre_device['device_id']}"
    )
    return vm


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
        4. If multiple chassis found → Stack detected
    """
    try:
        # Get the master device info to use for naming
        success, device_info = api.get_device_info(device_id)
        master_name = None
        if success and device_info:
            master_name = device_info.get("sysName") or device_info.get("hostname")

        # Step 1: Get root level items
        success, root_items = api.get_inventory_filtered(
            device_id, ent_physical_contained_in=0
        )

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
                logger.debug(
                    f"VC detection: Found parent container at index {parent_index} for device {device_id}"
                )
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
        chassis_items = [
            item
            for item in (child_items or [])
            if item.get("entPhysicalClass") == "chassis"
        ]

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
                member_data["suggested_name"] = _generate_vc_member_name(
                    master_name, position + 1
                )
            else:
                member_data["suggested_name"] = f"Member-{position + 1}"

            members.append(member_data)

        # Sort by position
        members.sort(key=lambda m: m["position"])

        logger.info(
            f"Detected stack with {len(members)} members for device {device_id}"
        )

        return {"is_stack": True, "member_count": len(members), "members": members}

    except Exception as e:
        logger.exception(f"Error detecting virtual chassis for device {device_id}: {e}")
        return None


def _generate_vc_member_name(
    master_name: str, position: int, serial: str = None
) -> str:
    """
    Generate name for VC member device using configured pattern from settings.

    Args:
        master_name: Name of the master/primary device
        position: VC position number
        serial: Optional serial number of the member device

    Returns:
        Generated member device name

    Examples:
        pattern="-M{position}" → "switch01-M2"
        pattern=" ({position})" → "switch01 (2)"
        pattern="-SW{position}" → "switch01-SW2"
        pattern=" [{serial}]" → "switch01 [ABC123]"
    """
    # Import here to avoid circular dependency
    from .models import LibreNMSSettings

    # Get pattern from settings with fallback to default
    try:
        settings = LibreNMSSettings.objects.first()
        pattern = settings.vc_member_name_pattern if settings else "-M{position}"
    except Exception as e:
        logger.warning(
            f"Could not load VC member name pattern from settings: {e}. Using default."
        )
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
        logger.error(
            f"Invalid placeholder in VC naming pattern '{pattern}': {e}. Using default."
        )
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
        member["suggested_name"] = _generate_vc_member_name(
            master_name, position, serial=member.get("serial")
        )

    return vc_data


def create_virtual_chassis_with_members(
    master_device: Device, members_info: list, libre_device: dict
):
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
            master_device_new_name = _generate_vc_member_name(
                original_master_name, 1, serial=master_device.serial
            )

            # Check if renamed master conflicts with existing device
            if (
                Device.objects.filter(name=master_device_new_name)
                .exclude(pk=master_device.pk)
                .exists()
            ):
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
                    member_rack.location
                    if member_rack and member_rack.location
                    else None
                )

                # Check for duplicate serial
                if serial and Device.objects.filter(serial=serial).exists():
                    logger.warning(
                        f"Device with serial '{serial}' already exists, skipping VC member creation"
                    )
                    continue

                member_name = _generate_vc_member_name(
                    master_base_name, position, serial=serial
                )

                # Check for duplicate name
                if Device.objects.filter(name=member_name).exists():
                    logger.warning(
                        f"Device with name '{member_name}' already exists, skipping VC member creation"
                    )
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
            expected_members = len(
                [m for m in members_info if m.get("serial") != master_device.serial]
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
        # Rollback master device to original state
        logger.error(
            f"Virtual Chassis creation failed for device {master_device.name}: {e}. "
            f"Rolling back master device changes."
        )
        master_device.name = original_master_name
        master_device.virtual_chassis = original_vc
        master_device.vc_position = original_vc_position
        master_device.save()
        raise


def process_device_filters(
    api: LibreNMSAPI,
    filters: dict,
    vc_detection_enabled: bool,
    clear_cache: bool,
    show_disabled: bool,
    exclude_existing: bool = False,
    job=None,
    request=None,
    return_cache_status: bool = False,
) -> List[dict] | tuple[List[dict], bool]:
    """
    Process LibreNMS device filters and return validated devices.

    Shared function used by both synchronous view and background job processing.
    Fetches devices, optionally pre-warms VC cache, validates each device, and
    caches results for HTMX row updates.

    Args:
        api: LibreNMS API client instance
        filters: Filter dict with location, type, os, hostname, sysname keys
        vc_detection_enabled: Whether to detect virtual chassis
        clear_cache: Whether to force cache refresh
        show_disabled: Whether to include disabled devices
        exclude_existing: Whether to exclude devices that already exist in NetBox
        job: Optional JobRunner instance for logging job events
        request: Optional Django request for client disconnect detection (synchronous only)
        return_cache_status: When True, returns (devices, from_cache) tuple

    Returns:
        List[dict]: Validated devices with _validation key, or tuple of (devices, from_cache)
        if return_cache_status is True. from_cache=True means data was loaded from existing
        cache; from_cache=False means data was just fetched from LibreNMS.
    """
    # Fetch devices from LibreNMS
    if job:
        job.logger.info(f"Fetching devices with filters: {filters}")
    else:
        logger.info(f"Fetching devices with filters: {filters}")

    if return_cache_status:
        libre_devices, from_cache = get_librenms_devices_for_import(
            api,
            filters=filters,
            force_refresh=clear_cache,
            return_cache_status=True,
        )
    else:
        libre_devices = get_librenms_devices_for_import(
            api,
            filters=filters,
            force_refresh=clear_cache,
        )

    # Filter out disabled devices if requested
    if not show_disabled:
        libre_devices = [d for d in libre_devices if d.get("status") == 1]

    if job:
        job.logger.info(f"Found {len(libre_devices)} devices to process")
    else:
        logger.info(f"Found {len(libre_devices)} devices")

    # Pre-warm VC cache if needed
    if vc_detection_enabled and libre_devices:
        device_ids = [d["device_id"] for d in libre_devices]
        if job:
            job.logger.info(
                f"Pre-fetching virtual chassis data for {len(device_ids)} devices. "
                "This may take some time..."
            )
        else:
            logger.info(f"Pre-fetching VC data for {len(device_ids)} devices")

        try:
            prefetch_vc_data_for_devices(api, device_ids, force_refresh=clear_cache)
            if job:
                job.logger.info("Virtual chassis data pre-fetch completed")
        except (BrokenPipeError, ConnectionError, IOError) as e:
            if request:
                logger.info(f"Client disconnected during VC prefetch: {e}")
                return []
            raise

    # Validate each device
    validated_devices = []
    total = len(libre_devices)
    api_for_validation = api if vc_detection_enabled else None

    if job:
        job.logger.info(f"Starting validation of {total} devices")
        # Initial check if job was already terminated before we even started
        try:
            from django_rq import get_queue
            from rq.job import Job as RQJob

            queue = get_queue("default")
            rq_job = RQJob.fetch(str(job.job.job_id), connection=queue.connection)

            if rq_job.is_failed or rq_job.is_stopped:
                job.logger.warning("Job was already stopped before validation started")
                return []
        except Exception:
            # Fall back to DB check if RQ check fails
            job.job.refresh_from_db()
            if job.job.status == JobStatusChoices.STATUS_FAILED:
                job.logger.warning("Job was stopped before validation started")
                return []
    else:
        logger.info(f"Validating {total} devices")

    for idx, device in enumerate(libre_devices, 1):
        # Check for job termination or client disconnect periodically
        if (
            idx % 5 == 0 or idx == 1
        ):  # Check more frequently (every 5 devices + first device)
            if job:
                # Check if job was terminated via stop API
                # CRITICAL: Check the RQ job status in Redis, not just the DB model
                # NetBox's stop endpoint marks the RQ job as failed in Redis
                try:
                    from django_rq import get_queue
                    from rq.job import Job as RQJob

                    queue = get_queue("default")
                    rq_job = RQJob.fetch(
                        str(job.job.job_id), connection=queue.connection
                    )

                    # Check if RQ job is in a stopped state
                    if rq_job.is_failed or rq_job.is_stopped:
                        job.logger.info(
                            f"Job stopped at device {idx}/{total} (RQ status: {rq_job.get_status()}). Exiting gracefully."
                        )
                        return []
                except Exception:
                    # If we can't check RQ status, fall back to DB status check
                    job.job.refresh_from_db()
                    if job.job.status == JobStatusChoices.STATUS_FAILED:
                        job.logger.info(
                            f"Job stopped at device {idx}/{total}. Exiting gracefully."
                        )
                        return []
            elif request:
                # Check for client disconnect
                try:
                    if hasattr(request, "META") and request.META.get("wsgi.input"):
                        pass
                except (BrokenPipeError, ConnectionError, IOError):
                    logger.info(
                        f"Client disconnected during validation at device {idx}"
                    )
                    return []

        # Drop any cached validation/meta keys before recomputing
        device.pop("_validation", None)

        # Generate shared cache key for this validated device
        device_id = device["device_id"]
        cache_key = get_validated_device_cache_key(
            server_key=api.server_key,
            filters=filters,
            device_id=device_id,
            vc_enabled=vc_detection_enabled,
        )

        # Check if we already have cached validation for this device
        # (only if not forcing refresh)
        if not clear_cache:
            cached_device = cache.get(cache_key)
            if cached_device:
                # Use cached validation
                device["_validation"] = cached_device["_validation"]

                # Apply exclude_existing filter if enabled
                if exclude_existing:
                    validation = device["_validation"]
                    if validation["existing_device"]:
                        continue

                validated_devices.append(device)
                continue

        # Not in cache or forcing refresh - validate now
        try:
            validation = validate_device_for_import(
                device,
                api=api_for_validation,
                include_vc_detection=vc_detection_enabled,
                force_vc_refresh=clear_cache,
            )
        except (BrokenPipeError, ConnectionError, IOError) as e:
            if request:
                logger.info(f"Client disconnected during device validation: {e}")
                return []
            raise

        # Set VC detection metadata
        if not vc_detection_enabled:
            validation["virtual_chassis"] = empty_virtual_chassis_data()
            validation["_vc_detection_skipped"] = True
        else:
            validation["_vc_detection_skipped"] = False

        # Apply exclude_existing filter if enabled
        if exclude_existing and validation["existing_device"]:
            continue

        device["_validation"] = validation
        validated_devices.append(device)

        # Cache validated device data (shared between sync and async processing)
        cache.set(cache_key, device, timeout=api.cache_timeout)

    if job:
        if validation_status_filter:
            filtered_count = total - len(validated_devices)
            job.logger.info(
                f"Validation complete: {len(validated_devices)} devices passed filter, "
                f"{filtered_count} filtered out (status: {validation_status_filter})"
            )
        else:
            job.logger.info(
                f"Validation complete: {len(validated_devices)} devices ready for import"
            )
    else:
        logger.info(f"Processed {len(validated_devices)} validated devices")

    if return_cache_status:
        return validated_devices, from_cache
    return validated_devices
