# Project Structure

This document provides an overview of the NetBox LibreNMS Plugin's codebase organization.

## Main Directories

- `netbox_librenms_plugin/` — Main plugin code
  - `views/` — User-facing pages and actions
    - `base/` — Shared sync-page logic for interfaces, cables, IP addresses, VLANs, and modules
    - `object_sync/` — Device and Virtual Machine views registered as NetBox object tabs
    - `sync/` — Actions that apply interface, cable, IP address, VLAN, module, device-field, location, and migration changes
    - `imports/` — Device import list and action endpoints
    - `mapping_views.py` — Generic views for Mappings and Rules & Patterns
    - `mixins.py` — Shared permissions, LibreNMS API access, caching, and server selection
    - `settings_views.py` — Plugin settings and connection testing
    - `status_check.py` — Device and Virtual Machine status pages
  - `api/` — REST API serializers, views, and URLs
  - `import_utils/` — Import pipeline logic, split into focused modules
    - `device_operations.py` — Device validation and creation
    - `vm_operations.py` — VM creation and import logic
    - `bulk_import.py` — Multi-device import orchestration
    - `filters.py` — LibreNMS device filtering and retrieval
    - `permissions.py` — Permission helpers for background jobs
    - `cache.py` — Import cache helpers
    - `collisions.py` — Duplicate and identity-collision handling
    - `virtual_chassis.py` — Virtual Chassis import helpers
  - `import_validation_helpers.py` — Validation state mutation during import (role/cluster/rack assignment, issue removal, status recalculation)
  - `librenms_api.py` — LibreNMS API client, server selection, and device lookup
  - `interface_sync.py` — Shared interface creation and update logic
  - `interface_relationships.py` — Parent, child, and LAG relationship resolution
  - `ip_addressing.py` — Shared IP address handling
  - `sync_cache.py` and `cache_signals.py` — Sync-tab cache coordination and invalidation
  - `jobs.py` — Background import jobs
  - `signals.py` — Public extension signals
  - `models.py`, `forms.py`, `filters.py`, and `filtersets.py` — Data models and user input/query definitions
  - `tables/` — NetBox table definitions for sync, status, mapping, and rule pages
  - `templates/netbox_librenms_plugin/` — Page templates
    - `inc/` — Shared template fragments
    - `htmx/` — HTMX response fragments
    - `_<resource>_sync.html` and `_<resource>_sync_content.html` — Sync-tab wrappers and refreshable content
  - `static/netbox_librenms_plugin/` — Plugin JavaScript and CSS
  - `tests/` — Primary Django and plugin test suite
  - `migrations/` — Django migrations
  - `navigation.py`, `urls.py`, and `constants.py` — Plugin registration, routing, and shared constants
  - `utils.py` — Shared matching, normalization, and data-conversion helpers
- `docs/` — User and developer documentation plus screenshots
- `contrib/` — Example mapping, normalization, and rule files for bulk import
- `tests/e2e/` — End-to-end tests outside the primary Django test suite
- `tools/` — Repository-specific lint and maintenance utilities
- `.devcontainer/` — Local NetBox development environment and helper scripts
