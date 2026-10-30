# Virtual Chassis Support

## Overview

The plugin automatically detects Virtual Chassis configurations and displays all VC interfaces on the LibreNMS Sync page of the designated sync device.

**LibreNMS Sync Device Selection Priority:**
1. Member with `librenms_id` custom field (highest priority)
2. Master device with primary IP
3. Any member with primary IP
4. Member with lowest VC position

> **Note:** LibreNMS treats a Virtual Chassis as a single logical device. Only one member (the sync device) should have the `librenms_id` custom field set.

## Stack Detection

On import, the plugin reads the ENTITY-MIB inventory root and its direct children. It detects a stack from one of two shapes:

- **Chassis members:** two or more `chassis` rows directly under the stack (or chassis) root, for example Cisco StackWise. The member whose serial matches the LibreNMS device serial is the master.
- **Junos Virtual Chassis:** one `chassis` root whose description contains "Virtual Chassis", with two or more `container` rows directly under it whose description starts with "FPC". Each FPC must have its own serial, and the root serial must match exactly one FPC. Detection compares these serials with the global serial normalization rules, and again with the rules of each manufacturer that has serial rules. If no set of rules meets every condition, the inventory is not a Junos Virtual Chassis: the device is imported as a single device, and no warning shows. The member model is the FPC name (for example `EX4400-24X-S`).

The master is the one member whose serial matches the device serial. Both serials go through the serial normalization rules first: the global rules and the rules of the manufacturer of the DeviceType that the device imports as. That DeviceType comes from a DeviceType mapping or the hardware string, then from the chassis model in the inventory. So a decorated serial such as Juniper's `S/N BCFB9793` still matches. A Junos Virtual Chassis must also meet the FPC serial conditions again under these rules. If the plugin detects a stack, but no member or more than one member matches, the import creates the device without a virtual chassis and shows a warning. The Virtual Chassis details dialog and the import confirmation say so before the import. If the inventory matches neither shape, the device is imported as a single device. The **VC Serials** dialog on the LibreNMS Sync page lists the same members.

## Member Positions

On import, each member gets the position number that the device reports for it. Junos numbers its members from 0 (`ge-0/0/0` is on member 0), so a Junos Virtual Chassis gets positions 0, 1, and so on. Cisco StackWise numbers its members from 1. If any member reports no position, a negative position, or the same position as another member, the plugin numbers all members in inventory order, starting at 1, so that no two members share a position.

## Member Serials

Each member stores its serial as LibreNMS reports it, with the spaces at the start and end removed. A Juniper member shows `S/N BCFB9751`, for example. A placeholder such as `-`, `N/A` or `unknown` is not stored. A numeric `0` is a real serial and is stored.

The plugin does not store the output of the serial normalization rules. It applies the rules to both serials each time it compares them: the stored serial and the LibreNMS serial. The rules of the manufacturer of the virtual chassis master apply, and the global rules. So a rule change applies to both sides at once, and a member name with `{serial}` is the same on import, on the LibreNMS Sync page and after a name sync. A serial that is a placeholder before or after the rules matches nothing.

The **VC Serials** dialog shows each member serial after the rules, and **Sync All** stores the serial as LibreNMS reports it. If the dialog comes from a page that an earlier plugin version rendered, **Sync All** writes nothing and asks you to reload the page.

On import, the plugin does not create a member when a device with the same serial exists. It looks for a device that stores the LibreNMS serial, or the serial after the rules. When the found device stores the serial after the rules, but the rules give a different value for that stored serial, the two serials can be the same member or two different members. The plugin cannot tell them apart, so it skips the member and logs a warning that names the device. Check the two serials, then assign the serial manually.

Members that an earlier plugin version created store the serial after the rules. They keep their match while the rules give the same value for a value that they already changed, which is true for the seeded Juniper `S/N` rule. If a rule changes its own output again, use **Sync All** in the **VC Serials** dialog to store the LibreNMS serials.

## How It Works

### Member Selection

When viewing a device that is part of a virtual chassis, the plugin will:

1. Detects if the device is part of a virtual chassis and displays 'Virtual Chassis Member' column.
2. Automatically select the VC member by matching the device VC position to the first number in the interface name.
3. Allows selection of specific members if the auto select is not correct.

> Selecting a new member will trigger a new interface details comparison against the newly selected NetBox VC member.

Interfaces data is then synced to the selected VC member in Netbox.

#### Virtual Chassis Member Select

![Virtual Chassis Member Selection](../img/Netbox-librenms-plugin-virtualchassis.gif)
