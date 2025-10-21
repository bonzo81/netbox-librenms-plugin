# Virtual Chassis Support

## Overview

The plugin automatically detects Virtual Chassis configurations and displays all VC interfaces on the LibreNMS Sync page of the designated sync device.

**LibreNMS Sync Device Selection Priority:**
1. Member with `librenms_id` custom field (highest priority)
2. Master device with primary IP
3. Any member with primary IP
4. Member with lowest VC position

> **Note:** LibreNMS treats a Virtual Chassis as a single logical device. Only one member (the sync device) should have the `librenms_id` custom field set.

## How It Works

### Member Selection

When viewing a device that is part of a virtual chassis, the plugin will:

1. Detects if the device is part of a virtual chassis and dispalys 'Virtual Chassis Member' column.
2. Automatically select the VC member by matching the device VC position to the first number in the interface name.
3. Allows selection of specific members if the auto select is not correct.

> Selecting a new member will trigger a new interface details comparison against the newly selected NEtbox VC member.

Interfaces data is then synced to the selected VC member in Netbox.

#### Virtual Chassis Member Select

![Virtual Chassis Member Selection](../img/Netbox-librenms-plugin-virtualchassis.gif)
