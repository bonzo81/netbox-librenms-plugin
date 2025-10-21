# Features List

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

### Interface

* Create or Update interface in NetBox from LibreNMS interface data
  * Name
  * Description
  * Status (Enabled/Disabled)
  * Type (with custom mapping support)
  * Speed
  * MAC Address
  * MTU
* Sync all or specific fields

### Cable

* Create Cable connection in NetBox from LibreNMS links data
* Best results when the [custom field](usage_tips/custom_field.md) `librenms_id` is populated on interfaces

### IP Address

* Create IP address objects in Netbox from LibreNMS device IP data
* Best results when the [custom field](usage_tips/custom_field.md) `librenms_id` is populated on interfaces

### Location

* NetBox Site to LibreNMS location synchronization
* Sync location latitude and longitude values from NetBoxx to LibreNMS

### [Interface Mapping](usage_tips/interface_mappings.md)

* Customizable LibreNMS to NetBox interface type mappings
* Interface Speed-based mapping rules
* Bulk import support

