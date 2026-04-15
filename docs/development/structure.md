# Project Structure

This document provides an overview of the NetBox LibreNMS Plugin's codebase organization.

## Main Directories

- `netbox_librenms_plugin/` — Main plugin code
  - `views/` — Custom views for devices, mappings, VMs, etc.
    - `base/` — Abstract base views for shared logic (interfaces, cables, IP addresses, VLANs)
    - `object_sync/` — Per-model sync views registered as tabs on Device/VM detail pages
    - `sync/` — POST-only views that apply sync changes (interfaces, cables, IP addresses, VLANs, devices)
  - `models.py` — Database models
  - `forms.py` — Custom forms
  - `tables/` — Table definitions for UI
  - `templates/` — Custom templates
    - `netbox_librenms_plugin/` — Main template directory
      - `inc/` — Shared template fragments (e.g., paginator)
  - `api/` — API serializers, views, and URLs
  - `import_utils/` — Import pipeline logic, split into focused modules
    - `device_operations.py` — Device validation, single-device import, filtered fetch
    - `vm_operations.py` — VM creation and import logic
    - `bulk_import.py` — Multi-device / bulk import orchestration
    - `filters.py` — LibreNMS device filtering and retrieval
    - `permissions.py` — User permission checking helpers
    - `cache.py` — Cache key generation
    - `virtual_chassis.py` — Virtual chassis data helpers
  - `import_validation_helpers.py` — Validation state mutation during import (role/cluster/rack assignment, issue removal, status recalculation)
  - `migrations/` — Django migrations
  - `utils.py` — Utility functions
  - `navigation.py` — Menu/navigation integration
  - `static/` — Static assets (JS, CSS)
    - `netbox_librenms_plugin/` — Plugin-specific static files
      - `js/` — JavaScript files
- `tests/` — Test suite
- `docs/` — Documentation
