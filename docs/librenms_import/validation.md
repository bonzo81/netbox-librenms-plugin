# Validation & Configuration

After searching, the import table displays devices with color-coded validation status badges indicating their import readiness.

## Validation Status Badges

The plugin validates each device and displays a status badge:

**Green - Ready to Import**
: Device has all prerequisites and can be imported immediately. All required NetBox objects are matched or configured.

**Yellow - Needs Review**
: Device requires manual configuration before import. One or more required fields need to be selected.

**Blue - Already Exists**
: Device already exists in NetBox. Cannot be imported again to prevent duplicates.

**Red - Cannot Import**
: Device has blocking issues preventing import (typically missing hostname or other critical data).

Click any validation badge to see detailed information about what's missing and how to resolve it.

## Validation Requirements

The plugin validates different requirements based on whether you're importing as a Device or Virtual Machine:

### For Physical Devices

**Site** (required)
: Automatically matched by comparing LibreNMS location to NetBox Site names (exact match). If no match is found, you must select a Site manually.

**Device Type** (required)
: Automatically matched by comparing LibreNMS hardware string to NetBox Device Type model names. If no match is found, you must select a Device Type manually.

**Device Role** (required)
: Cannot be auto-matched. You must select a Device Role from the dropdown. The plugin remembers your selection for consistency across similar devices.

**Platform** (optional)
: Automatically matched by comparing LibreNMS OS to NetBox Platform names or slugs. Not required for import.

**Rack** (optional)
: If the matched Site has Racks available, you can optionally assign the device to a specific Rack. Shows only racks within the matched Site.

### For Virtual Machines

**Cluster** (required)
: Cannot be auto-matched. You must select a Cluster from the dropdown.

**Platform** (optional)
: Automatically matched by comparing LibreNMS OS to NetBox Platform names or slugs. Not required for import.

## Resolving Validation Issues

When a device needs review, the validation details panel shows actionable resolution steps:

### Missing Site

The plugin couldn't find a NetBox Site matching the LibreNMS location name. To resolve:

1. Click the validation badge to expand details
2. Review the LibreNMS location value shown
3. Select the correct Site from the dropdown
4. The validation status updates immediately

**Tip**: If you're importing many devices from the same location, create a NetBox Site with a name matching the LibreNMS location first. This enables automatic matching for all devices.

### Missing Device Type

The LibreNMS hardware string didn't match any NetBox Device Type. To resolve:

1. Click the validation badge to expand details
2. Review the LibreNMS hardware value shown
3. Select the correct Device Type from the dropdown, or
4. Use the "Create Platform and Assign" quick action if you have appropriate permissions

**Tip**: Common hardware strings from your vendors should have corresponding Device Types in NetBox for automatic matching. Consider creating these proactively.

### Missing Device Role

All devices require a Device Role, which cannot be auto-matched. To resolve:

1. Click the validation badge to expand details
2. Select the appropriate role from the dropdown (e.g., "Switch", "Router", "Firewall")
3. The validation status updates immediately

**Bulk tip**: If importing multiple devices of the same type, you can select the role for all of them in the bulk import confirmation modal.

### Optional Rack Assignment

If the matched Site has Racks available, you can assign the device to a specific Rack:

1. The Rack dropdown shows all racks within the matched Site
2. Select a rack if the device's physical location is known
3. Leave unselected if rack assignment isn't needed yet

## Import as Device or Virtual Machine

Each device in the list can be imported as either a physical Device or a Virtual Machine. Use the **"Import As"** dropdown in each row to select:

**Device**
: Creates a NetBox Device object. Requires Site, Device Type, and Device Role. Optionally supports Rack assignment.

**Virtual Machine**
: Creates a NetBox VirtualMachine object. Requires Cluster. Platform is optional.

The validation requirements change based on your selection. Switching between Device and VM updates the validation panel to show the appropriate requirements.

## Virtual Chassis Detection

When Virtual Chassis Detection is enabled during search, the plugin analyzes device inventory data to identify stackable switches and chassis. If detected, the validation details show:

- Number of stack members detected
- Member position/slot numbers
- Serial numbers for each member
- Model/hardware information
- Suggested member names based on the master device

If a Virtual Chassis is detected, the plugin will automatically create the Virtual Chassis object and assign members during import. See the [Virtual Chassis](../usage_tips/virtual_chassis.md) documentation for more details about member naming patterns and management.

## Real-Time Validation Updates

The validation status badge updates in real-time as you make selections:

1. Select a missing Site → "Site" issue resolved
2. Select a Device Type → "Device Type" issue resolved
3. Select a Device Role → "Device Role" issue resolved
4. Badge changes from "Needs Review" to "Ready to Import"

You can now proceed with importing the device.

## Duplicate Detection

The plugin prevents duplicate device imports by checking for existing devices in NetBox using multiple strategies:

**LibreNMS ID Custom Field** (primary)
: Most reliable method. If a Device or VM already has the `librenms_id` custom field set to this device's ID, it's marked as "Already Exists".

**Hostname Match**
: Checks for exact name match against both Devices and VirtualMachines. If found, marked as "Already Exists".

**Primary IP Address** (weak match, devices only)
: If the primary IP is already assigned to a NetBox device, it's marked as potentially existing.

### Handling Hostname Conflicts

If both a VM and Device with the same hostname exist in NetBox, the plugin cannot determine which to match:
- The device is not marked as existing
- You can proceed with import as a new device
- A warning recommends setting the `librenms_id` custom field on the correct existing object to clarify the match

## Next Steps

Once devices are validated and ready:
- [Import Devices](import_process.md) - Single and bulk import with settings control
