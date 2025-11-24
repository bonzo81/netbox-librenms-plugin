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

from dcim.models import Device, DeviceRole, DeviceType, Site
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


def get_librenms_devices_for_import(
    api: LibreNMSAPI = None, filters: dict = None, server_key: str = None
) -> List[dict]:
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

    Returns:
        List of device dictionaries from LibreNMS

    Example:
        >>> api = LibreNMSAPI()
        >>> devices = get_librenms_devices_for_import(api, {'location': 'NYC'})
        >>> for device in devices:
        ...     print(device['hostname'])
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
        cached_result = cache.get(cache_key)
        if cached_result:
            return cached_result

        success, devices = api.list_devices(api_filters if api_filters else None)

        if not success:
            logger.error(f"Failed to retrieve devices from LibreNMS: {devices}")
            return []

        # Apply client-side filters if any
        if client_filters:
            devices = _apply_client_filters(devices, client_filters)

        # Cache for 5 minutes
        cache.set(cache_key, devices, timeout=300)
        return devices

    except Exception:
        logger.exception("Error retrieving LibreNMS devices for import")
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


def validate_device_for_import(libre_device: dict, import_as_vm: bool = False) -> dict:
    """
    Validate if a LibreNMS device can be imported to NetBox.

    Performs comprehensive validation:
    - Checks if device already exists in NetBox
    - Validates required prerequisites (Site, DeviceType, DeviceRole for devices)
      OR (Cluster for VMs)
    - Provides smart matching for missing objects
    - Returns detailed validation status

    Args:
        libre_device: Device data from LibreNMS
        import_as_vm: If True, validate for VM import instead of device import

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
    }

    try:
        # 1. Check if device/VM already exists in NetBox
        librenms_id = libre_device.get("device_id")
        hostname = libre_device.get("hostname", "")
        logger.debug(
            f"Checking for existing {'VM' if import_as_vm else 'device'}: "
            f"librenms_id={librenms_id} (type={type(librenms_id).__name__}), "
            f"hostname={hostname}"
        )

        if import_as_vm:
            # Check for existing VM
            from virtualization.models import VirtualMachine

            # Check by librenms_id custom field (most reliable match)
            existing = VirtualMachine.objects.filter(
                custom_field_data__librenms_id=str(librenms_id)
            ).first()
            if not existing and isinstance(librenms_id, str):
                try:
                    existing = VirtualMachine.objects.filter(
                        custom_field_data__librenms_id=int(librenms_id)
                    ).first()
                except (ValueError, TypeError):
                    pass

            if existing:
                logger.info(
                    f"Found existing VM: {existing.name} (matched by librenms_id={librenms_id})"
                )
                result["existing_device"] = existing
                result["existing_match_type"] = "librenms_id"
                result["warnings"].append(
                    f"VM already imported to NetBox as '{existing.name}'"
                )
                result["can_import"] = False
                return result

            # Check by hostname/name
            existing = VirtualMachine.objects.filter(name__iexact=hostname).first()
            if existing:
                result["existing_device"] = existing
                result["existing_match_type"] = "hostname"
                result["warnings"].append(
                    f"VM with same hostname exists in NetBox as '{existing.name}' (not linked to LibreNMS)"
                )
                result["can_import"] = False
                return result
        else:
            # Check for existing Device
            # Check by librenms_id custom field (most reliable match)
            # Note: Custom field data is stored as strings in NetBox's JSON field
            # LibreNMS device_id might be int or str, so check both
            existing = Device.objects.filter(
                custom_field_data__librenms_id=str(librenms_id)
            ).first()
            if not existing and isinstance(librenms_id, str):
                # Try as int if it was passed as string
                try:
                    existing = Device.objects.filter(
                        custom_field_data__librenms_id=int(librenms_id)
                    ).first()
                except (ValueError, TypeError):
                    pass

            if existing:
                logger.info(
                    f"Found existing device: {existing.name} (matched by librenms_id={librenms_id})"
                )
                result["existing_device"] = existing
                result["existing_match_type"] = "librenms_id"
                result["warnings"].append(
                    f"Device already imported to NetBox as '{existing.name}'"
                )
                result["can_import"] = False
                return result
            else:
                logger.debug(f"No existing device found by librenms_id={librenms_id}")

            # Check by hostname/name (strong match, but could be coincidence)
            existing = Device.objects.filter(name__iexact=hostname).first()
            if existing:
                result["existing_device"] = existing
                result["existing_match_type"] = "hostname"
                result["warnings"].append(
                    f"Device with same hostname exists in NetBox as '{existing.name}' (not linked to LibreNMS)"
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

        # 7. Determine if device/VM is ready to import
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
        sync_options: Sync options (optional):
            - sync_interfaces: bool (default True)
            - sync_cables: bool (default True)
            - sync_ips: bool (default True)
            - sync_fields: bool (default True)

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

        # Get device info from LibreNMS
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

        # Use manual mappings if provided, otherwise use validation matches
        if manual_mappings:
            site = Site.objects.filter(id=manual_mappings.get("site_id")).first()
            device_type = DeviceType.objects.filter(
                id=manual_mappings.get("device_type_id")
            ).first()
            device_role = DeviceRole.objects.filter(
                id=manual_mappings.get("device_role_id")
            ).first()
            platform_id = manual_mappings.get("platform_id")
            platform = None
            if platform_id:
                from dcim.models import Platform

                platform = Platform.objects.filter(id=platform_id).first()
        else:
            site = validation["site"].get("site")
            device_type = validation["device_type"].get("device_type")
            device_role = validation["device_role"].get("role")
            platform = validation["platform"].get("platform")

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

            if use_sysname:
                device_name = libre_device.get("sysName") or libre_device.get(
                    "hostname", f"device-{device_id}"
                )
            else:
                device_name = libre_device.get("hostname", f"device-{device_id}")

            # Strip domain if requested (but not for IP addresses)
            if strip_domain and device_name and "." in device_name:
                # Check if it's an IP address - if so, don't strip
                try:
                    from ipaddress import ip_address

                    ip_address(device_name)
                    # It's a valid IP, don't strip
                except ValueError:
                    # Not an IP, safe to strip domain
                    device_name = device_name.split(".")[0]

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
) -> dict:
    """
    Import multiple LibreNMS devices to NetBox.

    Args:
        device_ids: List of LibreNMS device IDs to import
        server_key: LibreNMS server configuration key
        sync_options: Sync options to apply to all devices
        manual_mappings_per_device: Dict mapping device_id to manual_mappings dict
            Example: {1179: {'device_role_id': 5}, 1180: {'device_role_id': 3}}

    Returns:
        dict: Bulk import result with structure:
            {
                'total': int,
                'success': List[dict],  # Successfully imported devices
                'failed': List[dict],   # Failed imports with errors
                'skipped': List[dict]   # Skipped devices (already exist, etc.)
            }

    Example:
        >>> result = bulk_import_devices([1, 2, 3, 4, 5])
        >>> print(f"Imported {len(result['success'])} of {result['total']} devices")
    """
    total = len(device_ids)
    success_list = []
    failed_list = []
    skipped_list = []

    for device_id in device_ids:
        try:
            # Get device info and validate first
            api = LibreNMSAPI(server_key=server_key)
            success, libre_device = api.get_device_info(device_id)

            if not success or not libre_device:
                failed_list.append(
                    {
                        "device_id": device_id,
                        "error": f"Failed to retrieve device {device_id} from LibreNMS",
                    }
                )
                continue

            validation = validate_device_for_import(libre_device)

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
            )

            if result["success"]:
                success_list.append(
                    {
                        "device_id": device_id,
                        "device": result["device"],
                        "message": result["message"],
                    }
                )
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

    # Determine device name based on use_sysname setting
    if use_sysname:
        device_name = libre_device.get("sysName") or libre_device.get("hostname")
    else:
        device_name = libre_device.get("hostname")

    # Generate import timestamp comment
    import_time = timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")

    # Create the device with librenms_id custom field
    device = Device.objects.create(
        name=device_name,
        device_type=device_type,
        role=device_role,
        site=site,
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

    # Determine VM name based on use_sysname setting
    if use_sysname:
        vm_name = libre_device.get("sysName") or libre_device.get("hostname")
    else:
        vm_name = libre_device.get("hostname")

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
