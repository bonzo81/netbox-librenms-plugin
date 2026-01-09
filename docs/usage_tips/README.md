# Usage Tips

## Initial Setup

1. [Configure Custom Field](custom_field.md)
    - Set up the `librenms_id` custom field for optimal device matching
    - This ensures reliable device identification between NetBox and LibreNMS

2. [Configure Interface Mappings](interface_mappings.md)
    - Review and set up interface type mappings before synchronization
    - Create specific mappings for your network equipment types
    - Pay attention to speed-based mappings for accurate interface types

3. [Multi Server Configuration](multi_server_configuration.md)
    - Configure multiple LibreNMS instances in your NetBox configuration
    - Switch between different LibreNMS servers through the web interface
    - Maintain backward compatibility with single-server configurations

## Device Import

[Device Import Guide](../librenms_import/overview.md) - Import devices from LibreNMS into NetBox

1. Search for devices using flexible filters (location, type, OS, hostname, sysname)
2. Validate import prerequisites (Site, Device Type, Device Role)
3. Configure missing mappings or select from suggestions
4. Import devices individually or in bulk
5. Automatic Virtual Chassis creation for stackable switches

The Device Import feature automatically sets the `librenms_id` custom field, enabling all other plugin features.

> **Rack Position Assignment:** Imported devices can be assigned to racks without specific rack unit (U) positions. After import, assign U positions through the "Non racked" section of each rack. The [NetBox Reorder Rack plugin](https://github.com/minitriga/netbox-reorder-rack) simplifies this workflow.

## Device Synchronization

### Devices

> **Note:** If you imported devices using the [Device Import feature](../librenms_import/overview.md), the `librenms_id` is already set and will be used automatically. The steps below apply to devices added to NetBox manually.

1. Ensure devices have either:
    - Primary IP configured
    - Valid DNS name (set on the Primary IP)
    - hostname (that matches LibreNMS hostname)
2. The plugin will populate the `librenms_id` custom field if the device is found in LibreNMS

### Virtual Chassis

LibreNMS treats a Virtual Chassis as one logical device. The plugin selects a single "sync device" from your chassis to communicate with LibreNMS using this priority:

1. **Member with `librenms_id` set** (if already configured)
2. **Master device with primary IP** (most common)
3. **Any member with primary IP** (fallback)
4. **Member with lowest position number** (last resort)

Only the selected sync device should have the `librenms_id` custom field populated—leave it empty on all other members.

For best results, align chassis member positions with interface naming patterns. For example, if switch 1 has interfaces like `eth1/0/1` and switch 2 has `eth2/0/1`, the plugin can auto-detect the correct member for each interface. Always verify the member selection before running bulk synchronization.

## Interface Management

1.  Verify Before Sync
    - Review interface mappings indicated by the icons (🔗 shows a mapping is configured)
    - Check speed and type matches
    - Confirm member assignments for virtual chassis
2. Exlude columns to exclude from interface sync
    - Sync only the values you want to sync

## Cable Management

1. Preparation
    - Ensure devices are properly identified in both systems
    - Open LibreNMS Sync on all devices to populate librenms_id custom field
    - Remote Device and Remote interface need to be found in NetBox for cable creation to work
    - Check Device and Interface naming

## Best Practices

1. Regular Maintenance
    - Periodically review and update interface mappings
    - Keep custom fields current


## Optimization
    - DNS lookup time can slow response of the API call to LibreNMS
