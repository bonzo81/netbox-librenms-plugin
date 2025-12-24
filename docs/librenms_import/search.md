# Searching for Devices

The import feature requires at least one filter to search for devices. This prevents accidentally loading thousands of devices and helps you work with focused device sets.

## Available Filters

**LibreNMS Location**
: Exact match by LibreNMS location ID. The dropdown shows all locations from your LibreNMS instance with their names and IDs.

**LibreNMS Type**
: Exact match by device type (network, server, storage, wireless, firewall, power, appliance, printer, loadbalancer, other).

**Operating System**
: Partial match by OS name. For example, "ios" matches "cisco-ios", "ios-xe", "cisco-ios-xr".

**LibreNMS Hostname**
: Partial match by the hostname or IP address used to add the device to LibreNMS.

**LibreNMS System Name**
: When used alone, performs an exact match on the SNMP sysName. When combined with other filters, performs a partial match.

## Additional Search Options

**Include Disabled Devices**
: When checked, includes devices marked as disabled in LibreNMS. By default, only active devices are shown.

**Include Virtual Chassis Detection**
: When checked, analyzes device inventory to detect stackable switches and chassis. This adds processing time but provides helpful information about multi-member devices. See [Virtual Chassis](../usage_tips/virtual_chassis.md) for details.

**Clear cache before search**
: Forces the plugin to fetch fresh data from LibreNMS instead of using cached results. LibreNMS data is normally cached for 5 minutes to improve performance.

**Exclude Existing Devices**
: When checked, hides devices that already exist in NetBox. By default, all devices are shown including those already imported. This helps focus on new devices that need to be imported.

## Filter Matching Rules

Understanding how filters work helps you get the right results:

**Exact Match Filters**
: Location, Type, and OS (when used alone) must match exactly as shown in LibreNMS.

**Partial Match Filters**
: Hostname, OS (with other filters), and System Name (with other filters) find devices containing your search text.

**Multiple Filters**
: All filters must match for a device to appear in results. Start with Location or Type to narrow results, then refine with additional filters.

### Filter Examples

**Find all network devices in New York**
```
Location: New York (ID: 5)
Type: network
```

**Find Cisco switches**
```
Type: network
OS: ios
```

**Find a specific device by name**
```
System Name: router-core-01.example.com
```

**Find devices with "router" in hostname**
```
Hostname: router
Type: network
```

## Background Job Processing

For searches that return many devices or include Virtual Chassis detection, you can choose to run your search as a background job. This keeps your browser responsive and allows you to cancel long-running operations.

### Controlling Background Jobs

The filter form includes a "Run as background job" checkbox (enabled by default):

**Enabled** (recommended)
: Search runs asynchronously as a background job. Provides cancellation capability, keeps browser responsive, and tracks progress in NetBox's Jobs interface.

**Disabled**
: Search runs synchronously. Browser enters "page loading" state until complete. Use for small searches when immediate results are preferred.

### Background Job Experience

When you enable background job processing, you see a modal showing:

1. **Initial Status** - "Job queued, waiting to start..."
2. **Processing** - "Processing your filter request..."
3. **Completion** - "Job completed! Redirecting to results..."

The page automatically redirects to show your results when processing completes.

**Cancellation**: You can cancel the job at any time before processing starts by clicking "Cancel". Once processing begins, it will complete even if you navigate away—results are cached and accessible later through the NetBox Jobs interface.

## Search Performance

**Initial searches** on large LibreNMS datasets may take a moment to process, especially with Virtual Chassis detection enabled. Once data is cached, subsequent searches within the 5-minute cache window are nearly instant.

**Smart caching** accounts for your specific filter combination, so changing filters triggers a fresh search even if individual filter values were previously cached.

**Tip**: Start with Location or Type filters to narrow the dataset, then refine with additional filters. This provides faster results and makes validation more manageable.

## Next Steps

After searching, proceed to:
- [Validation & Configuration](validation.md) - Review and configure devices for import
