def convert_speed_to_kbps(speed_bps):
    if speed_bps is None:
        return None
    return speed_bps / 1000


LIBRENMS_TO_NETBOX_MAPPING = {
    'ifName': 'name',
    'ifType': 'type',
    'ifSpeed': 'speed',
    'ifAlias': 'description',
    'ifPhysAddress': 'mac_address',
    'ifMtu': 'mtu',
}

def format_mac_address(mac_address):
    """
    Validate and format MAC address string for table display.

    Args:
        mac_address (str): The MAC address string to format.

    Returns:
        str: The MAC address formatted as XX:XX:XX:XX:XX:XX. 
    """
    mac_address = mac_address.strip().replace(':', '').replace('-', '')

    if len(mac_address) != 12:
        return "Invalid MAC Address"  # Return a message if the address is not valid
    
    formatted_mac = ':'.join(mac_address[i:i+2] for i in range(0, len(mac_address), 2))
    return formatted_mac.upper()

