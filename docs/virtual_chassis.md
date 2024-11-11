# Virtual Chassis Support

## Overview
On the LibreNMS Sync page it is possible to synchronize interfaces to the specific virtual chassis members.

All virtual chassis interfaces are dispalyed on the LibreNMS Sync tab on either the Virtual Chassis master, or the first member with a Primary IP.

## How It Works
### Member Selection
When viewing a device that is part of a virtual chassis, the plugin will:

1. Detects if the device is part of a virtual chassis and dispalys 'Virtual Chassis Member' column.
2. Automatically select the VC member by matching the device VC position to the first number in the interface name.
3. Allows selection of specific members if the auto select is not correct.

>  Selecting a new member will trigger a new interface details comparison against the newly selected NEtbox VC member.

Interfaces data is then synced to the selected VC member in Netbox.


#### Virtual Chassis Member Select
![Virtual Chassis Member Selection](img/Netbox-librenms-plugin-virtualchassis.gif)