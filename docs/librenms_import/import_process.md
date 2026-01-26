## Import Settings

Both single and bulk imports support these configuration options:

### Use sysName

**When enabled** (default): Uses the SNMP sysName as the device name in NetBox. Falls back to LibreNMS hostname if sysName is not available.

**When disabled**: Uses the LibreNMS hostname field as the device name.

**Default**: Controlled by Plugin Settings. Can be overridden per-import in the bulk import confirmation modal.

### Strip Domain

**When enabled**: Removes domain suffixes from device names. For example, "router.example.com" becomes "router". The plugin avoids stripping IP addresses.

**When disabled**: Keeps the full name as-is.

**Default**: Controlled by Plugin Settings. Can be overridden per-import in the bulk import confirmation modal.

### Device Name Examples

```
LibreNMS sysName: router-core-01.example.com
LibreNMS hostname: 10.0.0.1

Use sysName + Strip domain → "router-core-01"
Use sysName + Keep domain → "router-core-01.example.com"
Use hostname + Strip domain → "10.0.0.1" (IP preserved)
Use hostname + Keep domain → "10.0.0.1"
```

If neither sysName nor hostname exists, the plugin generates a name as `device-{librenms_id}`.

## What Gets Created During Import

When you import a device, the plugin creates several objects in NetBox:

### Device or VirtualMachine Object

Created with these attributes:

**Required fields**
: Name, Site (devices only), Device Type (devices only), Device Role/VM Role, Cluster (VMs only)

**Optional fields**
: Platform, Serial number, Rack (devices only), Location (devices only)

> **Note:** When a rack is assigned during import, the device is placed in the rack but without a specific rack unit (U) position. After import, these devices appear in the "Non racked" section of the rack in NetBox. You'll need to manually assign U positions to organize devices within the rack. The [NetBox Reorder Rack plugin](https://github.com/minitriga/netbox-reorder-rack) can simplify this task.

**Status field**
: Set to "active" if LibreNMS shows device as up (status=1), "offline" otherwise

**Comments**
: Automatically includes import timestamp and source information

**Custom fields**
: `librenms_id` is set to the LibreNMS device ID for linking

### Virtual Chassis (if detected)

For stackable devices with detected Virtual Chassis:

**Virtual Chassis object**
: Created with a name based on the master device and domain from VC data

**Master device assignment**
: The imported device is set as the master

**Member positions**
: Detected members are recorded with their positions, serials, and models for reference

After import, you can use the standard Virtual Chassis features to add member devices and configure the stack fully.

### LibreNMS Integration

The `librenms_id` custom field is automatically set, which enables:
- [Interface synchronization](../feature_list.md#interface-sync)
- [Cable synchronization](../feature_list.md#cable-sync)
- [IP address synchronization](../feature_list.md#ip-address-sync)
- Device field updates (serial, device type, platform)

No additional configuration is needed—these features become immediately available after import.

## Post-Import Workflow

After importing devices, typical next steps include:

1. **Review imported devices** - Click the links in the success message to verify device details
2. **Assign rack positions** - If devices were imported to a rack, assign specific U positions. The [NetBox Reorder Rack plugin](https://github.com/minitriga/netbox-reorder-rack) makes this process a lot easier.
3. **Sync interfaces** - Navigate to the device in NetBox and use the LibreNMS sync button to pull interface data
4. **Sync cables** - Once interfaces exist, sync cable connections from LibreNMS link data
5. **Sync IP addresses** - Pull IP address assignments from LibreNMS to NetBox

These sync operations use the `librenms_id` custom field that was automatically set during import.
