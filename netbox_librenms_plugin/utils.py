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
    except (re.error, ValueError, ObjectDoesNotExist) as e:
        return device


def get_table_paginate_count(request: HttpRequest, table_prefix: str) -> int:
    """
    Extends Netbox pagination to support multiple tables by using table-specific prefixes
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
