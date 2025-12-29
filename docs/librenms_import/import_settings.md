# Import Settings

Configure how devices are named and what data is imported from LibreNMS to NetBox.

## Setting Defaults

To configure global defaults for all imports:

1. Navigate to **Plugins → LibreNMS Plugin → Settings**
2. Click **Plugin Settings**
3. Configure Use sysName and Strip Domain to your preferred defaults
4. Save changes

These defaults apply to all future imports unless overridden during the import process.

## Device Naming Options

The plugin provides two settings that control how device names are created in NetBox. Both are configured in Plugin Settings under **Plugins → LibreNMS Plugin → Settings → Plugin Settings** and can be overridden on the LibreNMS import page.

### Use sysName

Controls which field from LibreNMS becomes the device name in NetBox.

- **Enabled** (default): Uses the SNMP sysName, falling back to LibreNMS hostname if sysName is not available
- **Disabled**: Uses the LibreNMS hostname field

### Strip Domain

Removes domain suffixes from device names to create shorter, cleaner names.

- **Enabled**: Removes domain suffixes (e.g., "router.example.com" becomes "router"). IP addresses are preserved without modification
- **Disabled**: Keeps the full name as-is

### Naming Examples

```
LibreNMS sysName: router-core-01.example.com
LibreNMS hostname: 10.0.0.1

Use sysName + Strip domain → "router-core-01"
Use sysName + Keep domain → "router-core-01.example.com"
Use hostname + Strip domain → "10.0.0.1" (IP preserved)
Use hostname + Keep domain → "10.0.0.1"
```

If neither sysName nor hostname exists, the plugin generates a name as `device-{librenms_id}`.



## Per-Import Overrides

When using bulk import, you can override the default settings in the confirmation modal before importing. This allows you to:

- Import some devices with sysName and others with hostname
- Apply domain stripping selectively based on device type or location
- Test different naming conventions before changing global defaults

The override only affects the current import operation and doesn't change your saved defaults.

## Next Steps

After configuring import settings:
- [Background Jobs & Caching](background_jobs.md) - Understand how import operations are processed
