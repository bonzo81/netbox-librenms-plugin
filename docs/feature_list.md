# Features List

### Device 

- LibreNMS device identification via:
    - [Custom field `librenms_id`](custom_field.md) *(recommended)*
    - Primary IP address
    - Primary IP DNS name
    - Hostname
- Add device to LibreNMS from netbox via SNMP v2c or v3

### [Virtual Chassis Support](virtual_chassis.md)
- Automatic VC member selection for each interface
- Member-specific interface synchronization
- Bulk member editing capabilities

### Interface 
- Create or Update interface in NetBox from LibreNMS interface data
    - Name
    - Description
    - Status (Enabled/Disabled)
    - Type (with custom mapping support)
    - Speed
    - MAC Address
    - MTU
- Sync all or specific fields

### Cable 
- Create Cable connection in NetBox from LibreNMS links data
- Best results when the [custom field](custom_field.md) `librenms_id` is populated on interfaces

### Location 
- NetBox Site to LibreNMS location synchronization
- Sync location latitude and longitude values from NetBoxx to LibreNMS

### [Interface Mapping](interface_mappings.md) 
- Customizable LibreNMS to NetBox interface type mappings
- Interface Speed-based mapping rules


