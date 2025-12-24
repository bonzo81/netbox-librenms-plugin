# Advanced Topics

## Best Practices

### Start with Focused Filters

Use Location or Type as your primary filter to narrow the device set, then refine with additional filters. This approach:
- Provides faster search results
- Makes validation more manageable
- Reduces the chance of accidentally importing incorrect devices

Example workflow:
1. Filter by Location: "Data Center A"
2. Refine by Type: "network"
3. Further filter by OS: "junos"

This gives you a focused set of Juniper network devices in Data Center A.

### Enable Virtual Chassis Detection Strategically

Only enable Virtual Chassis detection when importing stackable switches or chassis. This feature:
- Adds processing time to searches
- Is unnecessary for standalone devices
- Provides critical information for multi-member devices

**When to enable**: Importing switches known to be in stacks (Cisco, Juniper EX, Arista, etc.)

**When to skip**: Importing routers, servers, firewalls, or other standalone devices

### Configure Default Import Settings

Set your preferred "Use sysName" and "Strip domain" defaults in Plugin Settings to match your naming conventions. Benefits:
- Consistent device naming across all imports
- Reduces repetitive configuration per import
- Can still override on a per-import basis when needed

Access settings: **Plugins → LibreNMS Plugin → Settings → Plugin Settings**

### Use Bulk Import for Consistency

When importing multiple devices of the same type, use bulk import to apply consistent settings:
- Same Device Role for all devices of a type (e.g., all switches get "Switch" role)
- Same naming convention (use sysName, strip domain)
- Same optional configurations (rack assignments, platforms)

This ensures uniformity in your NetBox data and reduces manual effort.

### Create Missing Device Types Proactively

If you notice many devices with missing Device Types during validation, create the common types in NetBox before importing. This:
- Enables automatic matching for future imports
- Reduces manual selection during validation
- Ensures consistent device type usage

Match Device Type model names to LibreNMS hardware strings for automatic matching.

## Performance Optimization

### Background Job Control

The import filter form includes a "Run as background job" checkbox (enabled by default) that controls whether searches run asynchronously:

**Background job enabled** (recommended)
: Search runs asynchronously with progress tracking, cancellation capability, and browser responsiveness. Best for most use cases, especially with Virtual Chassis detection or large device counts.

**Background job disabled**
: Search runs synchronously. Browser enters "page loading" state until complete. Use for small searches when immediate results are preferred and job overhead isn't needed.

### Caching Strategy

The plugin caches LibreNMS data for 5 minutes by default. To optimize:

**For repeated imports**: Don't clear cache between searches. Use cached data for faster results.

**For fresh data**: Check "Clear cache before search" to force retrieval of the latest LibreNMS data.

**After LibreNMS changes**: Clear cache if you've just added/updated devices in LibreNMS and need to see them immediately.

## Multi-Server Configuration

When working with multiple LibreNMS servers:

### Server Selection

The import feature automatically uses the currently selected server from Plugin Settings. To switch servers:

1. Navigate to **Plugins → LibreNMS Plugin → Settings → Plugin Settings**
2. Change the "LibreNMS Server" dropdown
3. Click "Save"
4. Return to the import page

All imports use the selected server, and imported devices are linked to that server.

### Server-Specific Considerations

**Devices are server-specific**: The `librenms_id` is only unique within a single LibreNMS instance. If the same device exists in multiple LibreNMS servers, import it only once.

**Cache is server-specific**: Changing servers doesn't clear the cache for the previous server. Each server has its own cache keys.

**Configuration consistency**: Ensure NetBox objects (Sites, Device Types) are consistent across servers if you're importing similar devices from multiple instances.

## Special Cases

### Hostname Conflicts

If both a Device and VM exist in NetBox with the same hostname:

**Problem**: The plugin cannot determine which object matches the LibreNMS device.

**Solution**: Set the `librenms_id` custom field on the correct existing object in NetBox. The plugin will then match correctly.

**Workaround**: If you want to import as a new object despite the conflict, proceed with import. The plugin allows it but shows a warning.

### Serial Number Duplicates

If a device's serial number already exists in NetBox:

**Behavior**: The plugin shows a warning but allows import.

**Reason**: Serial numbers can be duplicated in some cases (VMs, testing equipment, manufacturer reuse).

**Recommendation**: Review the warning and verify the device isn't already in NetBox under a different name.

### Missing Hostnames

If a LibreNMS device has no hostname or sysName:

**Behavior**: The plugin generates a name as `device-{librenms_id}`.

**Recommendation**: Update the hostname in LibreNMS first, then re-import, or manually rename the device in NetBox after import.

### Platform Matching

Platform matching is case-insensitive and matches against both the Platform name and slug:

**LibreNMS OS**: "ios-xe"
**NetBox Platform name**: "Cisco IOS XE" (slug: "ios-xe")
**Result**: Matched

If platforms aren't matching automatically:
- Check the NetBox Platform slug matches the LibreNMS OS string
- Consider creating platform mappings that match your LibreNMS OS values
- Platform is optional and can be set manually after import

## Troubleshooting

### No Devices Found After Search

**Check filter values**: Ensure filter values match exactly what's in LibreNMS for exact-match filters (Location, Type).

**Try broader filters**: Use fewer filters or switch to partial-match filters (hostname, OS with other filters).

**Check "Include Disabled Devices"**: If searching for disabled devices, ensure this option is checked.

**Verify LibreNMS connection**: Navigate to Plugin Settings and test the LibreNMS connection.

### Search Takes a Long Time

**Cause**: Large LibreNMS installations or Virtual Chassis detection enabled.

**Solutions**:
- Use background job mode (threshold or always) in Plugin Settings
- Narrow your search with more specific filters
- Disable Virtual Chassis detection if not needed
- Check LibreNMS API performance

### Device Shows "Cannot Import"

**Check validation details**: Click the badge to see specific blocking issues.

**Common causes**:
- Device has no hostname in LibreNMS
- Critical data missing from LibreNMS
- Device data corrupted or incomplete

**Resolution**: Fix the data in LibreNMS first, then search again.

### Background Job Appears Stuck

**Check job status**: Navigate to **System → Background Tasks** in NetBox to see job status and any error messages.

**Cancellation**: Jobs can be cancelled before they start processing. Once processing begins, they run to completion.

**Results retrieval**: If a job completes while you're on another page, return to the import page and use the job_id parameter to load results.

### Import Fails After Validation Shows "Ready"

**Most common cause**: Validation selections were not properly saved.

**Solution**:
1. Refresh the page
2. Re-search for the devices
3. Re-apply your validation selections
4. Verify the badge shows "Ready to Import"
5. Try importing again

**Prevention**: Wait for validation status to update after each selection before proceeding to import.

## Integration with Other Features

After importing devices, leverage other plugin features:

**[Interface Sync](../feature_list.md#interface-sync)**
: Navigate to the device in NetBox and click the LibreNMS sync button. Select which interface fields to sync (name, description, type, speed, MAC, MTU, status).

**[Cable Sync](../feature_list.md#cable-sync)**
: From the device page, sync cable connections based on LibreNMS link/FDB data. Requires interfaces to exist first.

**[IP Address Sync](../feature_list.md#ip-address-sync)**
: Pull IP address assignments from LibreNMS to NetBox. Creates IP objects and assigns them to interfaces.

**[Virtual Chassis Management](../usage_tips/virtual_chassis.md)**
: For imported stacks, add member devices and configure interface mappings across members.

All these features use the `librenms_id` custom field that was automatically set during import.

## Getting Help

If you encounter issues not covered here:

**Check logs**: NetBox logs may contain detailed error messages from the plugin.

**GitHub Issues**: Report bugs or request help at the [plugin repository](https://github.com/bonzo81/netbox-librenms-plugin/issues).

**Discussions**: Ask questions or share ideas in [GitHub Discussions](https://github.com/bonzo81/netbox-librenms-plugin/discussions).
