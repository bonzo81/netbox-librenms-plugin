The import feature requires at least one filter to search for devices. This prevents accidentally loading thousands of devices and helps you work with focused device sets.

## Available Filters

LibreNMS Location
: Exact match by LibreNMS location ID. The dropdown shows all locations from your LibreNMS instance with their names and IDs.

LibreNMS Type
: Exact match by device type (network, server, storage, wireless, firewall, power, appliance, printer, loadbalancer, other).

Operating System
: Partial match by OS name. For example, "ios" matches "cisco-ios", "ios-xe", "cisco-ios-xr".

LibreNMS Hostname
: Partial match by the hostname or IP address used to add the device to LibreNMS.

LibreNMS System Name
: When used alone, performs an exact match on the SNMP sysName. When combined with other filters, performs a partial match.

Hardware
: Partial match by LibreNMS hardware model. For example, "C9300" matches devices with hardware like "Cisco C9300-48P".

## Additional Search Options

Include Disabled Devices
: When checked, includes devices marked as disabled in LibreNMS. By default, only active devices are shown.

Include Virtual Chassis Detection
: When checked, analyzes device inventory to detect stackable switches and chassis. This adds processing time but provides helpful information about multi-member devices. See [Virtual Chassis](../usage_tips/virtual_chassis.md) for details.

Clear cache before search
: Forces the plugin to fetch fresh data from LibreNMS instead of using cached results. LibreNMS data is normally cached for 5 minutes to improve performance.

Exclude Existing Devices
: When checked, hides devices that already exist in NetBox. By default, all devices are shown including those already imported.

## Filter Matching Rules

Understanding how filters work helps you get the right results:

Exact Match Filters
: Location, Type, and OS (when used alone) must match exactly as shown in LibreNMS.

Partial Match Filters
: Hostname, OS (with other filters), and System Name (with other filters) find devices containing your search text.

Multiple Filters
: All filters must match for a device to appear in results. Start with Location or Type to narrow results, then refine with additional filters.

### Filter Examples

**Find all network devices in New York**
```
Location: New York
Type: network
```

**Find Cisco devices**
```
Type: network
OS: ios
```

**Find specific hardware model at New York**
```
Location: New York
Hardware: C9300
Type: network
```

**Find a specific device by name**
```
System Name: router-core-01.example.com
```

**Find devices with "F" in hostname at New York with device type firewall**
```
Hostname: F
Location: New York
Type: firewall
```

## Search Options

Run as background job
: Enabled by default. Runs searches asynchronously, allowing you to track progress and cancel operations. Recommended for most use cases, especially with Virtual Chassis detection or large device sets. See [Background Jobs & Caching](background_jobs.md) for details.

Clear cache before search
: Forces fresh data from LibreNMS instead of using cached results. LibreNMS data is normally cached for 5 minutes to improve performance. See [Background Jobs & Caching](background_jobs.md) for caching details.

## Saved Cached Searches
The import page displays all your recent searches at the top, showing which filter combinations, that are still found in the cache. Each cached search shows the filters used, device count, and time remaining before expiration.  Click any cached search to instantly reload those results without re-running filters. This is particularly useful when switching between different filter combinations.

## Next Steps

After searching, proceed to:
- [Validation & Configuration](validation.md) - Review and configure devices for import
- [Background Jobs & Caching](background_jobs.md) - Understand job processing and performance optimization
