# Background Jobs & Caching

The Device Import feature uses background job processing and intelligent caching for both searching and importing devices. Background jobs are enabled by default for both operations to handle large device sets efficiently.

## Background Jobs

Background jobs run asynchronously in NetBox's job system for both device searches and import operations.

### Background Job Processing

Both device searches and import operations can run as background jobs (default) or synchronously. Background jobs are recommended for:

- Large device sets (especially searches with more than 50 devices)
- Operations with Virtual Chassis detection enabled
- Import operations of any size

**Benefits of background jobs:**
- Avoid browser timeouts on long-running operations
- Cancel operations in progress if needed
- Continue using NetBox while the job runs
- Review detailed logs and results after completion


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

### Active Cached Searches

The import page displays all your recent searches at the top, showing which filter combinations, that are still found in the cache. Each cached search shows the filters used, device count, and time remaining before expiration.

Click any cached search to instantly reload those results without re-running filters or Virtual Chassis detection. This is particularly useful when switching between different filter combinations.

Cached searches expire after 5 minutes of inactivity (or what you set as the cache timeout). The countdown timer shows how long each search remains available.

The "Clear cache before search" option only clears the cache for the specific filter combination you're searching—other cached searches remain available.
