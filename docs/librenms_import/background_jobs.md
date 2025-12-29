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

LibreNMS API responses are cached for 5 minutes to reduce load times and minimize API calls. Cache keys are unique per filter combination.

### Controlling Cache

The search form includes a "Clear cache before search" checkbox:

| Setting | Behavior |
|---------|----------|
| Unchecked (default) | Uses cached data if available. Fastest results. |
| Checked | Forces fresh data retrieval from LibreNMS. |

Clear cache when you need current data after adding or updating devices in LibreNMS, or when troubleshooting. Keep cache enabled for normal operations and when refining filters.
