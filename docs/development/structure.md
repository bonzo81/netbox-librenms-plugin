# Project Structure

This document provides an overview of the NetBox LibreNMS Plugin's codebase organization.

## Main Directories

- `netbox_librenms_plugin/` — Main plugin code
  - `views/` — Custom views for devices, mappings, VMs, etc.
    - `base/` — Abstract base views for shared logic
    - `sync/` — Views for synchronization logic
  - `models.py` — Database models
  - `forms.py` — Custom forms
  - `tables/` — Table definitions for UI
  - `templates/` — Custom templates
    - `netbox_librenms_plugin/` — Main template directory
      - `inc/` — Shared template fragments (e.g., paginator)
  - `api/` — API serializers, views, and URLs
  - `migrations/` — Django migrations
  - `utils.py` — Utility functions
  - `navigation.py` — Menu/navigation integration
  - `static/` — Static assets (JS, CSS)
    - `netbox_librenms_plugin/` — Plugin-specific static files
      - `js/` — JavaScript files
- `tests/` — Test suite
- `docs/` — Documentation


