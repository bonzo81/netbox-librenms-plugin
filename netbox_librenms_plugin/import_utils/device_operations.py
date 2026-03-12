"""Device validation, import, and fetch operations."""

import logging

from dcim.models import Device, DeviceRole, DeviceType, Rack, Site
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from virtualization.models import Cluster  # noqa: F401 — used by test mock.patch targets

from ..librenms_api import LibreNMSAPI
from ..utils import (
    find_matching_platform,
    find_matching_site,
    match_librenms_hardware_to_device_type,
    set_librenms_device_id,
)
from .cache import get_import_device_cache_key
from .virtual_chassis import (
    _generate_vc_member_name,
    empty_virtual_chassis_data,
    get_virtual_chassis_data,
    update_vc_member_suggested_names,
)

logger = logging.getLogger(__name__)


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


def validate_device_for_import(
    libre_device: dict,
    import_as_vm: bool = False,
    api: "LibreNMSAPI" = None,
    *,
    include_vc_detection: bool = True,
    force_vc_refresh: bool = False,
    use_sysname: bool = True,
    strip_domain: bool = False,
    server_key: str = "default",
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
        "serial_action": None,  # None, "link", "conflict", "update_serial", "hostname_differs"
        "serial_confirmed": False,  # True when librenms_id match and serial matches
        "serial_duplicate": False,  # True when incoming serial is already on a different device
        "librenms_id_needs_migration": False,  # True when existing device has legacy bare-int ID
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

        # Check for existing VM first (by librenms_id custom field)
        try:
            from netbox_librenms_plugin.utils import find_by_librenms_id

            existing_vm = find_by_librenms_id(VirtualMachine, librenms_id, server_key)
        except (ValueError, TypeError):
            # librenms_id is not convertible to int; no match will be found
            existing_vm = None

        if existing_vm:
            logger.info(f"Found existing VM: {existing_vm.name} (matched by librenms_id={librenms_id})")
            result["existing_device"] = existing_vm
            result["existing_match_type"] = "librenms_id"
            result["import_as_vm"] = True  # Force VM mode since VM exists
            result["can_import"] = False

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
            # Note: name_sync_available/suggested_name are intentionally not set for VMs
            # because UpdateDeviceNameView only supports Device objects; VM name-sync
            # would require a separate implementation.
            if hostname and existing_vm.name == hostname:
                result["name_matches"] = True

        # Check for existing Device (by librenms_id custom field)
        if not result["existing_device"]:
            try:
                from netbox_librenms_plugin.utils import find_by_librenms_id

                existing_device = find_by_librenms_id(Device, librenms_id, server_key)
            except (ValueError, TypeError):
                # librenms_id is not convertible to int; no match will be found
                existing_device = None

            if existing_device:
                logger.info(f"Found existing device: {existing_device.name} (matched by librenms_id={librenms_id})")
                result["existing_device"] = existing_device
                result["existing_match_type"] = "librenms_id"
                result["can_import"] = False

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

                # Check for serial drift on the linked device
                incoming_serial = libre_device.get("serial") or ""
                if incoming_serial and incoming_serial != "-":
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

        # Only check hostname/serial/IP if not already matched by librenms_id
        if not result["existing_device"]:
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
            elif existing_device:
                logger.info(f"Found existing device by hostname: {existing_device.name}")
                result["existing_device"] = existing_device
                result["existing_match_type"] = "hostname"

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
                    result["warnings"].append(
                        f"Device with same hostname exists in NetBox as '{existing_device.name}' (not linked to LibreNMS)"
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

                        if existing_by_serial.name and existing_by_serial.name.lower() == hostname.lower():
                            result["warnings"].append(
                                f"Device with same serial and hostname exists as '{existing_by_serial.name}' "
                                f"(not linked to LibreNMS)"
                            )
                            result["serial_action"] = "link"
                        else:
                            result["warnings"].append(
                                f"Device with same serial ({serial}) exists as '{existing_by_serial.name}' "
                                f"but hostname differs (LibreNMS: '{hostname}'). Device may have been reinstalled."
                            )
                            result["serial_action"] = "hostname_differs"

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
                            result["existing_device"] = device
                            result["existing_match_type"] = "primary_ip"
                            result["warnings"].append(
                                f"IP address {primary_ip} already assigned to device '{device.name}' (not linked to LibreNMS)"
                            )
                            result["can_import"] = False

        # Refresh local variable to reflect any VM-mode adjustments made during detection
        # (e.g. existing VM found by hostname sets result["import_as_vm"] = True)
        import_as_vm = result["import_as_vm"]

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

            # Check for device type mismatch between existing device and LibreNMS
            if hasattr(existing, "device_type") and existing.device_type:
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
                }
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

            device_data = {
                "name": device_name,
                "site": site,
                "device_type": device_type,
                "role": device_role,
                "status": "active" if libre_device.get("status") == 1 else "offline",
                "comments": f"Imported from LibreNMS by netbox-librenms-plugin on {import_time}",
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
            set_librenms_device_id(device, device_id, api.server_key)
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
