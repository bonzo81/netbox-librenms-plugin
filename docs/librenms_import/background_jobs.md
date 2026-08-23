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

The import table caches data for 5 minutes to reduce load times and minimize API calls to LibreNMS. Cache keys are unique per LibreNMS server and filter combination.

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

The import page displays active searches from every configured LibreNMS server at the top. Each cached search shows its server, filters, device count, and time remaining before expiration. Searches with identical filters remain separate when they belong to different servers.

Click a cached search to activate its server and reload its results without re-running filters or Virtual Chassis detection. The link restores the filters, naming options, and Virtual Chassis detection state that created the search.

Cached searches expire after the configured cache timeout. The countdown timer shows how long each search remains available. Searches for removed servers do not appear.

The **Clear** action removes the current filters and results. It keeps the active server and does not remove cached searches. The **Clear cache before search** option refreshes only the active server and filter combination. Cached searches from other servers remain available.
