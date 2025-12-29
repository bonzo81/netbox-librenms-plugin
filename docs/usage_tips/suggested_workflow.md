# Suggested Workflow

This guide provides a recommended workflow for using the plugin after installation. Following this order helps ensure smooth operation and avoids common issues.

## 1. Configure Plugin Settings

Navigate to **Plugins → LibreNMS Plugin → Settings** and configure:

- **LibreNMS Server**: Select which server to use (if multi-server setup)
- **Device Naming**: Set your preferred defaults for "Use sysName" and "Strip Domain" - see [Import Settings](../librenms_import/import_settings.md)
- **Virtual Chassis Naming**: Configure the member naming pattern if you plan to import stackable devices

**Why first**: These defaults apply to all imports and save time by reducing per-import configuration.

## 2. Create Custom Field

Create the `librenms_id` custom field in NetBox following the [Custom Field Setup](custom_field.md) guide.

**Why early**: This field enables the most reliable device matching and is required for interface, cable, and IP address synchronization features. Creating it before importing prevents issues with duplicate device detection.

## 3. Prepare NetBox Data

Ensure NetBox has the basic objects needed for device imports:

- **Sites**: Create Sites that match your LibreNMS locations (exact name matching works best)
- **Device Types**: Add Device Types for your common hardware models
- **Device Roles**: Create appropriate roles (Switch, Router, Firewall, etc.)
- **Platforms**: Add Platforms matching your LibreNMS OS names (optional but helpful)

**Why before importing**: The plugin auto-matches these objects during import. Pre-creating them reduces manual configuration during the import process.

## 4. Configure Interface Mappings

If you have specific interface type mapping requirements, configure them via **Plugins → LibreNMS Plugin → Interface Type Mappings** - see [Interface Mappings](interface_mappings.md).

**Why**: Ensure specific NetBox interface types are used for your LibreNMS interface data.

## 5. Import Devices

Use the [Device Import](../librenms_import/overview.md) feature to bring devices into NetBox:

1. Navigate to **Plugins → LibreNMS Plugin → Import → LibreNMS Import**
2. Apply filters to find devices (start with Location or Type)
3. Review validation status and configure missing fields
4. Import devices individually or in bulk

**Tips**:

- Start with a small set (single location or device type) to verify your setup
- Enable Virtual Chassis detection only when importing stackable switches

## 6. Sync Interfaces

After devices are imported, sync their interfaces:

1. Navigate to a device in NetBox
2. Use the LibreNMS sync button to pull interface data
3. Review and adjust [Interface Mappings](interface_mappings.md) if needed

**Why after import**: Interfaces require the device to exist in NetBox first. The `librenms_id` field set during import enables accurate synchronization.

## 7. Sync Cables and IP Addresses

Complete your device data by syncing:

- **Cables**: Pull link data from LibreNMS to create cable connections
- **IP Addresses**: Import IP assignments to populate NetBox's IPAM

**Why last**: Both features require that interfaces already exist in NetBox and ideally with the `librenms_id` field set. The `librenms_id` field on interfaces ensures accurate matching.

## 8. Sync Locations (Optional)

If you want to synchronize location latitude/longitude data between NetBox Sites and LibreNMS locations, use the location sync feature.

**Why optional**: Only needed if you maintain geographic coordinates and want bidirectional sync.

## Next Steps

After completing the initial workflow:

- Regular imports: Use the same import process for new devices as they're added to LibreNMS
- Interface updates: Re-sync interfaces periodically to capture configuration changes
- Virtual Chassis: See [Virtual Chassis](virtual_chassis.md) for managing multi-member devices
- Background Jobs: Understand [Background Jobs & Caching](../librenms_import/background_jobs.md) for performance optimization
