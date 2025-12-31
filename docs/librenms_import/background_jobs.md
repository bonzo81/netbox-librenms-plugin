# Background Jobs & Caching

The Device Import feature uses background job processing and intelligent caching for both searching and importing devices. Background jobs are enabled by default for both operations to handle large device sets efficiently.

## Background Jobs

Background jobs run asynchronously in NetBox's job system for both device searches and import operations.

### Search Jobs

When searching for devices, you can choose to run as a background job (default) or synchronously. Background jobs are recommended for large device sets or when Virtual Chassis detection is enabled. They allow you to cancel operations and avoid browser timeouts.

For small, quick searches (under 50 devices), you can disable background jobs for immediate results.

### Import Jobs

Device import operations can run as background jobs (default) or synchronously. Background jobs are recommended as they allow you to continue using NetBox while devices are imported, handle errors properly, and let you review results after completion.

For small imports, you can disable background jobs if you prefer to wait for immediate completion.

### Viewing Job Status

All background jobs appear in NetBox's **Jobs** interface, where you can view status, start time, duration, and results.

## Caching

The import table caches data for 5 minutes to reduce load times and minimize API calls to LibreNMS. Cache keys are unique per filter combination.

### What Gets Cached

The cache includes both LibreNMS device data AND NetBox reference data used in the import table:

**From LibreNMS:**
- Device lists matching your search filters
- Device details (hostname, sysName, location, hardware, etc.)
- Virtual chassis detection results

**From NetBox:**
- Available device roles (for the role dropdown in each row)
- Available VM clusters (for VM imports)
- Available racks for each site (filtered by the device's matched site)

This means if you add a new role, create a new rack, or add a new cluster in NetBox, those changes won't appear in the import table dropdowns until you clear the cache or wait for it to expire (5 minutes).

### Controlling Cache

The search form includes a "Clear cache before search" checkbox:

| Setting | Behavior |
|---------|----------|
| Unchecked (default) | Uses cached data if available. Fastest results. |
| Checked | Forces fresh data retrieval from both LibreNMS and NetBox. |

**When to clear cache:**
- After adding or updating devices in LibreNMS
- After adding new roles, racks, or clusters in NetBox that should appear in import dropdowns
- When troubleshooting import issues
- When you need to verify current state

**When to keep cache enabled:**
- Normal operations and when refining search filters
- When repeatedly working with the same set of devices
- When NetBox reference data hasn't changed
