# Sync Tabs

The **LibreNMS Sync** page compares LibreNMS data with the selected NetBox Device or Virtual Machine. Refresh a tab to load current LibreNMS data before applying changes.

## Interfaces

Available for Devices and Virtual Machines. This tab creates or updates interfaces and can synchronize descriptions, status, type, speed, VLAN assignments, MAC addresses, and MTU values.

Interface names can come from different LibreNMS fields. Type mappings control how LibreNMS interface types become NetBox interface types. The tab also handles LAG and parent relationships, NetBox-only interfaces, and Virtual Chassis member assignment.

## Cables

Available for Devices. This tab uses LibreNMS link data to create NetBox cable connections. Both devices and their interfaces should exist in NetBox first, so synchronize interfaces before cables.

The `librenms_id` custom field on interfaces improves matching when interface names differ between NetBox and LibreNMS.

## IP Addresses

Available for Devices and Virtual Machines. This tab creates IP addresses and assigns them to matching interfaces. It can also create a missing interface before assigning an address.

**Set Primary IP** sets the NetBox Primary IP when the synchronized address matches the LibreNMS management address. Existing assignments or conflicting addresses require confirmation before they are changed.

## VLANs

Available for Devices. This tab creates VLANs reported by LibreNMS. Each VLAN can use a suitable VLAN group based on NetBox scope, or remain global when no group is selected.

Interface VLAN assignments are handled by the Interfaces tab rather than this tab.

## Modules

Available for Devices. This tab compares LibreNMS physical inventory with NetBox module bays and installed modules. Module matching has additional mapping and inventory rules, so it is covered in the [Module Sync guide](module_sync.md).
