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

Click the validation details button to review what is missing. The validation status updates immediately when you select a Device Role or a Rack in the import table. The details dialog previews the Region, Location, Rack, and Tenant values that will be used during import; a manually selected rack takes precedence over the parsed rack. The Site row displays the LibreNMS sysLocation; the other placement rows display `-` in the LibreNMS value column.

Region is inherited from the resolved Site because NetBox does not assign a region directly to a device. When a Location resolves, its table value displays the parent-to-child NetBox Location hierarchy. A **Mapped** badge identifies Location and Tenant values resolved through a Location Mapping instead of a direct name match. Tenant is shown as a separate value because NetBox assigns it directly to the device rather than placing it within the physical-location hierarchy. Location, Rack, and Tenant are optional, so an unresolved value is displayed as `-` and does not prevent import.

### Import as Device

- **Site** (required) - Auto-matched from LibreNMS location. See [Location Parsing](import_settings.md#location-parsing) for splitting the location string into site, location, rack, and tenant.
- **Device Type** (required) - Auto-matched from LibreNMS hardware string, or via [Device Type Mapping](../usage_tips/mapping_rules.md#device-type-mappings)
- **Device Role** (required) - Must be selected manually
- **Platform** (optional) - Auto-matched from LibreNMS OS via [Platform Mapping](../usage_tips/mapping_rules.md#platform-mappings). If no mapping exists and the platform is not found, a **Create Platform** button opens a modal to create a new NetBox Platform and mapping in one step.
- **Region, Location, Rack, and Tenant** (optional) - Previewed in the validation comparison table. Region is inherited from the matched Site; Location, Rack, and Tenant are applied from parsed location tokens when they resolve uniquely. Location displays its NetBox hierarchy when it has parent locations. Rack selection is available when the site has racks, and an explicit selection takes precedence over the parsed rack. See [Location Parsing](import_settings.md#location-parsing) and [Location Mappings](../usage_tips/mapping_rules.md#location-mappings).

### Import as Virtual Machine

- **Cluster** (required) - Must be selected manually
- **Platform** (optional) - Auto-matched from LibreNMS OS via [Platform Mapping](../usage_tips/mapping_rules.md#platform-mappings). The same **Create Platform** modal is available if needed.

## Virtual Chassis Detection

When Virtual Chassis Detection is enabled during search, the validation details show detected stack members with their positions, serials, and suggested names. The plugin automatically creates the Virtual Chassis object during import. See [Virtual Chassis](../usage_tips/virtual_chassis.md) for details.

## Duplicate Detection

The plugin checks for existing devices using:

1. **LibreNMS ID custom field** (most reliable) - If set, device is marked "Already Exists"
2. **Hostname match** - Exact name match against Devices and VMs
3. **Primary IP address** (weak match) - If IP is already assigned to a device

If both a VM and Device with the same hostname exist, the plugin cannot determine which to match and allows import. Set the `librenms_id` custom field on the correct existing object to clarify the match.

## Out-of-Band (OOB) Detection

When an incoming LibreNMS device looks like an out-of-band controller (iDRAC, iLO, BMC, …) and matches an existing NetBox device, the validation details show an **OOB Detected** panel instead of a plain import button. Rather than creating a duplicate device, the plugin offers the appropriate reconciliation action — **Add as OOB**, **Promote to host**, or **Merge NetBox devices**. See [Out-of-Band (OOB) Management](../usage_tips/oob_management.md) for the full flow.

## Next Steps

- [Import Settings](import_settings.md) - Configure device naming and import options
- [Out-of-Band Management](../usage_tips/oob_management.md) - Reconcile OOB controllers with their host devices
