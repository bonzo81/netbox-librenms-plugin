# ruleid: metadata-ports-skip-vlan-associations
api.get_ports(device_id)
# ruleid: metadata-ports-skip-vlan-associations
api.get_ports(device_id, with_vlans=True)
# ok: metadata-ports-skip-vlan-associations
api.get_ports(device_id, with_vlans=False)
