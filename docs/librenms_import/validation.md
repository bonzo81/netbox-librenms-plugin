# Validation & Configuration

After searching, the import table displays devices with action buttons that reflect their validation status.

## Validation States

Import Button (Green)
: Device is ready to import. All required fields are matched or configured.

Disabled Import Button + Details Button (Gray/Red)
: Device has missing required fields. Click Details to configure.

Link to Existing Device
: Device already exists in NetBox. Link navigates to the existing device.

## Required Fields

NetBox requires three fields before importing a device: **Site**, **Device Type**, and **Device Role**. The plugin attempts to match Site and Device Type automatically by comparing LibreNMS data to existing NetBox objects. Device Role must always be selected manually.

Click the validation details button to review what's missing and select values from the dropdowns. The validation status updates immediately.

### Import as Device

- **Site** (required) - Auto-matched from LibreNMS location
- **Device Type** (required) - Auto-matched from LibreNMS hardware string
- **Device Role** (required) - Must be selected manually
- **Platform** (optional) - Auto-matched from LibreNMS OS
- **Rack** (optional) - Available if Site has racks

### Import as Virtual Machine

- **Cluster** (required) - Must be selected manually
- **Platform** (optional) - Auto-matched from LibreNMS OS

## Virtual Chassis Detection

When Virtual Chassis Detection is enabled during search, the validation details show detected stack members with their positions, serials, and suggested names. The plugin automatically creates the Virtual Chassis object during import. See [Virtual Chassis](../usage_tips/virtual_chassis.md) for details.

## Duplicate Detection

The plugin checks for existing devices using:

1. **LibreNMS ID custom field** (most reliable) - If set, device is marked "Already Exists"
2. **Hostname match** - Exact name match against Devices and VMs
3. **Primary IP address** (weak match) - If IP is already assigned to a device

If both a VM and Device with the same hostname exist, the plugin cannot determine which to match and allows import. Set the `librenms_id` custom field on the correct existing object to clarify the match.

## Next Steps

- [Import Settings](import_settings.md) - Configure device naming and import options
