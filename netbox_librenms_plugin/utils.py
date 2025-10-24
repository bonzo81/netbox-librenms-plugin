import re
from typing import Optional

from dcim.models import Device
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpRequest
from netbox.config import get_config
from netbox.plugins import get_plugin_config
from utilities.paginator import get_paginate_count as netbox_get_paginate_count


def convert_speed_to_kbps(speed_bps: int) -> int:
    """
    Convert speed from bits per second to kilobits per second.

    Args:
        speed_bps (int): Speed in bits per second.

    Returns:
        int: Speed in kilobits per second.
    """
    if speed_bps is None:
        return None
    return speed_bps // 1000


def format_mac_address(mac_address: str) -> str:
    """
    Validate and format MAC address string for table display.

    Args:
        mac_address (str): The MAC address string to format.

    Returns:
        str: The MAC address formatted as XX:XX:XX:XX:XX:XX.
    """
    if not mac_address:
        return ""

    mac_address = mac_address.strip().replace(":", "").replace("-", "")

    if len(mac_address) != 12:
        return "Invalid MAC Address"  # Return a message if the address is not valid

    formatted_mac = ":".join(
        mac_address[i : i + 2] for i in range(0, len(mac_address), 2)
    )
    return formatted_mac.upper()


def get_virtual_chassis_member(device: Device, port_name: str) -> Device:
    """
    Determines the likely virtual chassis member based on the device's vc_position and port name.

    Args:
        device (Device): The NetBox device instance.
        port_name (str): The name of the port (e.g., 'Ethernet1').

    Returns:
        Device: The virtual chassis member device corresponding to the port.
                Returns the original device if not part of a virtual chassis or if matching fails.
    """
    if not hasattr(device, "virtual_chassis") or not device.virtual_chassis:
        return device

    try:
        match = re.match(r"^[A-Za-z]+(\d+)", port_name)
        if not match:
            return device

        # Get the port number and use it
        vc_position = int(match.group(1))
        return device.virtual_chassis.members.get(vc_position=vc_position)
    except (re.error, ValueError, ObjectDoesNotExist):
        return device


def get_librenms_sync_device(device: Device) -> Optional[Device]:
    """
    Determine which Virtual Chassis member should handle LibreNMS sync operations.

    LibreNMS treats a Virtual Chassis as a single logical device, so only one member
    should have the librenms_id custom field set and be used for sync operations.

    Priority order for selecting the sync device:
    1. Any member with librenms_id custom field set (highest priority - already configured)
    2. Master device with primary IP (if master is designated)
    3. Any member with primary IP (fallback when no master or master lacks IP)
    4. Member with lowest vc_position (for error messages when no IPs configured)

    Args:
        device (Device): Any device in the virtual chassis.

    Returns:
        Optional[Device]: The device that should handle LibreNMS sync, or None if
                         the device is not in a virtual chassis.
    """
    if not hasattr(device, "virtual_chassis") or not device.virtual_chassis:
        return device

    vc = device.virtual_chassis
    all_members = vc.members.all()

    # Priority 1: Check if ANY member has librenms_id configured
    for member in all_members:
        if member.cf.get("librenms_id"):
            return member

    # Priority 2: Use master device if it has primary IP
    if vc.master and vc.master.primary_ip:
        return vc.master

    # Priority 3: Find any member with primary IP
    for member in all_members:
        if member.primary_ip:
            return member

    # Priority 4: Use member with lowest vc_position as fallback
    try:
        return min(all_members, key=lambda m: m.vc_position, default=None)
    except (ValueError, TypeError):
        return None


def get_table_paginate_count(request: HttpRequest, table_prefix: str) -> int:
    """
    Extends Netbox pagination to support multiple tables by using table-specific prefixes

    Args:
        request: HTTP request object
        table_prefix: Prefix for the table

    Returns:
        int: Number of items to display per page
    """
    config = get_config()
    if f"{table_prefix}per_page" in request.GET:
        try:
            per_page = int(request.GET.get(f"{table_prefix}per_page"))
            return min(per_page, config.MAX_PAGE_SIZE)
        except ValueError:
            pass

    return netbox_get_paginate_count(request)


def get_interface_name_field(request: Optional[HttpRequest] = None) -> str:
    """
    Get interface name field with request override support.

    Args:
        request: Optional HTTP request object that may contain override

    Returns:
        str: Interface name field to use
    """
    if request:
        if request.GET.get("interface_name_field"):
            return request.GET.get("interface_name_field")
        if request.POST.get("interface_name_field"):
            return request.POST.get("interface_name_field")

    # Fall back to plugin config
    return get_plugin_config("netbox_librenms_plugin", "interface_name_field")


def match_librenms_hardware_to_device_type(hardware_name: str) -> dict:
    """
    Match LibreNMS hardware string to a NetBox DeviceType.
    
    This function implements a prioritized matching strategy to find the best
    DeviceType match for a given LibreNMS hardware value.
    
    Matching Priority:
    1. Part number exact match (case-insensitive) - Most specific identifier
    2. Model name contains hardware value (case-insensitive) - Handles vendor prefixes
    3. Model name exact match (case-insensitive) - Direct model name match
    4. Slug match - Fallback using slugified hardware name

    Args:
        hardware_name (str): Hardware string from LibreNMS API (e.g., 'C9200L-48P-4X')

    Returns:
        dict: Dictionary containing:
            - matched (bool): Whether a match was found
            - device_type (DeviceType|None): The matched DeviceType object
            - match_type (str|None): How the match was found
              ('part_number', 'contains', 'exact', 'slug', or None)
            
    Examples:
        >>> result = match_librenms_hardware_to_device_type('C9200L-48P-4X')
        >>> if result['matched']:
        ...     print(f"Matched via {result['match_type']}: {result['device_type']}")
        Matched via part_number: Catalyst 9200L-48P-4X
        
        >>> result = match_librenms_hardware_to_device_type('NonExistent')
        >>> result['matched']
        False
    """
    from dcim.models import DeviceType
    from django.utils.text import slugify

    if not hardware_name or hardware_name == "-":
        return {"matched": False, "device_type": None, "match_type": None}

    # 1. Try part number exact match (highest priority)
    try:
        device_type = DeviceType.objects.get(part_number__iexact=hardware_name)
        return {"matched": True, "device_type": device_type, "match_type": "part_number"}
    except DeviceType.DoesNotExist:
        pass
    except DeviceType.MultipleObjectsReturned:
        # If multiple matches, return the first one
        device_type = DeviceType.objects.filter(part_number__iexact=hardware_name).first()
        return {"matched": True, "device_type": device_type, "match_type": "part_number"}

    # 2. Try model name contains hardware value
    # (for cases like 'C9200L-48P-4X' in 'Catalyst 9200L-48P-4X')
    try:
        device_types = DeviceType.objects.filter(model__icontains=hardware_name)
        if device_types.count() == 1:
            return {"matched": True, "device_type": device_types.first(), "match_type": "contains"}
        # If multiple matches, prefer exact word match or shorter model name
        elif device_types.count() > 1:
            # Try to find one where hardware_name is a complete word in the model
            for dt in device_types:
                # Check if hardware_name appears as a standalone part (separated by spaces or hyphens)
                pattern = r'\b' + re.escape(hardware_name) + r'\b'
                if re.search(pattern, dt.model, re.IGNORECASE):
                    return {"matched": True, "device_type": dt, "match_type": "contains"}
            # If no exact word match, return the shortest model name (likely most specific)
            device_type = min(device_types, key=lambda x: len(x.model))
            return {"matched": True, "device_type": device_type, "match_type": "contains"}
    except DeviceType.DoesNotExist:
        pass

    # 3. Try exact model match (case-insensitive)
    try:
        device_type = DeviceType.objects.get(model__iexact=hardware_name)
        return {"matched": True, "device_type": device_type, "match_type": "exact"}
    except DeviceType.DoesNotExist:
        pass
    except DeviceType.MultipleObjectsReturned:
        device_type = DeviceType.objects.filter(model__iexact=hardware_name).first()
        return {"matched": True, "device_type": device_type, "match_type": "exact"}

    # 4. Try slug match (lowest priority)
    hardware_slug = slugify(hardware_name)
    try:
        device_type = DeviceType.objects.get(slug=hardware_slug)
        return {"matched": True, "device_type": device_type, "match_type": "slug"}
    except DeviceType.DoesNotExist:
        pass
    except DeviceType.MultipleObjectsReturned:
        device_type = DeviceType.objects.filter(slug=hardware_slug).first()
        return {"matched": True, "device_type": device_type, "match_type": "slug"}

    return {"matched": False, "device_type": None, "match_type": None}
