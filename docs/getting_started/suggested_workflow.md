# Suggested Workflow

This guide provides separate workflows for importing devices and synchronizing existing NetBox objects with LibreNMS.

## Import Workflow

### 1. Configure Plugin Settings

Navigate to **LibreNMS → Settings → Plugin Settings** and configure:

- **LibreNMS Server**: Select which server to use (if multi-server setup)
- **Device Naming**: Set your preferred defaults for "Use sysName" and "Strip Domain" - see [Import Settings](../configuration/import_settings.md)
- **Location Parsing**: Split a LibreNMS location into Site, Location, Rack, and Tenant values when needed
- **Virtual Chassis Naming**: Configure the member naming pattern if you plan to import stackable devices

**Why first**: These defaults apply to all imports and save time by reducing per-import configuration.

### 2. Verify Custom Field

As of version 0.4.4, the plugin **automatically creates** the `librenms_id` custom field when migrations are run. No manual setup is required. See the [Custom Field Setup](../configuration/custom_field.md) guide for details on how the field works and optional manual configuration.

**Why early**: This field enables the most reliable device matching and is required for interface, cable, and IP address synchronization features. It is created automatically during `manage.py migrate`, so just verify it exists before importing.

### 3. Prepare NetBox Data

Ensure NetBox has the basic objects needed for device imports:

- **Sites**: Create Sites that match your LibreNMS locations (exact name matching works best)
- **Device Types**: Add Device Types for your common hardware models
- **Device Roles**: Create appropriate roles (Switch, Router, Firewall, etc.)
- **Platforms**: Add Platforms matching your LibreNMS OS names (optional but helpful)

**Why before importing**: The plugin auto-matches these objects during import. Pre-creating them reduces manual configuration during the import process.

### 4. Configure Import Mappings

Configure mappings when LibreNMS values do not match NetBox names:

- [Device Type Mappings](../configuration/mappings.md#device-type-mappings) match LibreNMS hardware strings to NetBox Device Types.
- [Platform Mappings](../configuration/mappings.md#platform-mappings) match LibreNMS operating systems to NetBox Platforms.
- [Location Mappings](../configuration/mappings.md#location-mappings) match parsed location values to Sites, Locations, Racks, or Tenants.

Normalization Rules can adjust hardware values before matching. See [Rules & Patterns](../configuration/rules_and_patterns.md#normalization-rules).

### 5. Import Devices

Use the [Device Import](../device_import/overview.md) feature to bring devices into NetBox:

1. Navigate to **LibreNMS → Import → LibreNMS Import**
2. Apply filters to find devices (start with Location or Type)
3. Review validation status and configure missing fields
4. Import devices individually or in bulk

**Tips**:

- Start with a small set (single location or device type) to verify your setup
- Enable Virtual Chassis detection only when importing stackable switches. See [Virtual Chassis](../device_sync/virtual_chassis.md).
- Review the [Out-of-Band Management](../device_import/oob_management.md) options when LibreNMS reports a management controller as a separate device.
- Large searches and imports can use [Background Jobs & Caching](../device_import/background_jobs_and_caching.md).

## Device Sync Workflow

Use this workflow for imported devices or existing NetBox Devices and Virtual Machines. Open the **LibreNMS Sync** page on the object and confirm that it is matched to the correct LibreNMS device. See the [Device Sync overview](../device_sync/overview.md).

### 1. Configure Sync Mappings and Rules

Review the configuration that applies to the data you plan to synchronize:

- [Interface Type Mappings](../configuration/mappings.md#interface-type-mappings) choose the NetBox interface type from the LibreNMS type and speed.
- [Platform Mappings](../configuration/mappings.md#platform-mappings) support Device Information Sync.
- [Module Type and Module Bay Mappings](../configuration/mappings.md#module-type-mappings) support Module Sync.
- [Port Stack LAG Patterns](../configuration/rules_and_patterns.md#port-stack-lag-patterns) identify LAGs and ignore service access point relationships on platforms that need custom patterns.
- [Module rules](../configuration/rules_and_patterns.md) normalize or filter physical inventory and suggest carrier modules.

### 2. Sync VLANs

Create required VLANs before synchronizing interface VLAN assignments. The plugin can suggest a VLAN group based on its NetBox scope, or create a global VLAN. VLAN Sync is available for Devices.

### 3. Sync Interfaces

Synchronize interfaces before cables and IP addresses because those objects depend on NetBox interfaces.

Review the interface naming source and type mappings before applying changes. Interface Sync can also handle LAG and parent relationships, VLAN assignments, and Virtual Chassis member selection.

See [Sync Tabs](../device_sync/sync_tabs.md#interfaces) for the key Interface Sync behavior.

### 4. Sync Cables and IP Addresses

Complete your device data by syncing:

- **Cables**: Pull link data from LibreNMS to create cable connections
- **IP Addresses**: Import IP assignments, optionally create missing interfaces, and optionally set the Primary IP

Cable Sync is available for Devices. IP Address Sync is available for Devices and Virtual Machines. See [Sync Tabs](../device_sync/sync_tabs.md) for details.

### 5. Sync Modules (Optional)

For Devices with physical inventory, compare LibreNMS inventory with NetBox module bays and installed modules. Module Sync may require Module Type Mappings, Module Bay Mappings, and inventory rules.

See the [Module Sync guide](../device_sync/module_sync.md) for the full workflow.

### 6. Sync Locations (Optional)

If you want to synchronize location latitude/longitude data between NetBox Sites and LibreNMS locations, use the location sync feature.

**Why optional**: Only needed if you maintain geographic coordinates and want bidirectional sync.

## Next Steps

After completing the initial workflow:

- Regular imports: Use the same import process for new devices as they're added to LibreNMS
- Interface updates: Re-sync interfaces periodically to capture configuration changes
- Virtual Chassis: See [Virtual Chassis](../device_sync/virtual_chassis.md) for managing multi-member devices
- Configuration: Review [Mappings](../configuration/mappings.md) and [Rules & Patterns](../configuration/rules_and_patterns.md) as new hardware and platforms are added
