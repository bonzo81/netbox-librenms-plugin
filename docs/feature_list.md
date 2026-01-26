### [Device Import](librenms_import/overview.md)

* Search and discover devices from LibreNMS using flexible filters
* Validate device prerequisites before import (Site, Device Type, Device Role)
* Import devices as physical Devices or Virtual Machines
* Smart matching for Sites, Device Types, and Platforms
* Bulk import support
* Automatic Virtual Chassis creation for stackable devices
* Background job processing for large device sets
* Duplicate detection to prevent re-importing existing devices

### Plugin Settings

* Multi-server LibreNMS configuration support
* Configurable device naming defaults (sysName vs hostname)
* Domain stripping options during import for cleaner device names
* Virtual Chassis member naming pattern customization during import

### Device

* LibreNMS device identification via:
  * [Custom field `librenms_id`](usage_tips/custom_field.md) _(recommended)_
  * Primary IP address
  * Primary IP DNS name
  * Hostname
* Add device to LibreNMS from netbox via SNMP v2c or v3

### [Virtual Chassis Support](usage_tips/virtual_chassis.md)

* Automatic VC member selection for each interface
* Member-specific interface synchronization
* Bulk member editing capabilities

### Interface Sync {#interface-sync}

* Create or Update interface in NetBox from LibreNMS interface data
  * Name
  * Description
  * Status (Enabled/Disabled)
  * Type (with custom mapping support)
  * Speed
  * MAC Address
  * MTU
* Sync all or specific fields

### Cable Sync {#cable-sync}

* Create Cable connection in NetBox from LibreNMS links data
* Best results when the [custom field](usage_tips/custom_field.md) `librenms_id` is populated on interfaces

### IP Address Sync {#ip-address-sync}

* Create IP address objects in Netbox from LibreNMS device IP data
* Best results when the [custom field](usage_tips/custom_field.md) `librenms_id` is populated on interfaces

### Location

* NetBox Site to LibreNMS location synchronization
* Sync location latitude and longitude values from NetBoxx to LibreNMS

### [Interface Mapping](usage_tips/interface_mappings.md)

* Customizable LibreNMS to NetBox interface type mappings
* Interface Speed-based mapping rules
* Bulk import support
