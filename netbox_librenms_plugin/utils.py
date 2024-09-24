def convert_speed_to_kbps(speed_bps):
    if speed_bps is None:
        return None
    return speed_bps / 1000


LIBRENMS_TO_NETBOX_MAPPING = {
    'ifName': 'name',
    'ifType': 'type',
    'ifSpeed': 'speed',
    'ifAlias': 'description',
}
