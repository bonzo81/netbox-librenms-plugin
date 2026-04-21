# NetBox LibreNMS Plugin – AI Assistant Guide

> **Note:** Additional context-specific instructions are in `.github/instructions/`:
> - [testing.instructions.md](instructions/testing.instructions.md) – applies to `tests/**`
> - [frontend.instructions.md](instructions/frontend.instructions.md) – applies to templates and static files
> - [background-jobs.instructions.md](instructions/background-jobs.instructions.md) – applies to `jobs.py`, import views, and import utilities
> - [sync.instructions.md](instructions/sync.instructions.md) – applies to sync views, base views, tables, and sync JS
> - [release.instructions.md](instructions/release.instructions.md) – applies to changelog, pyproject.toml, and `__init__.py` version bumps

## Architecture & Key Modules
- Plugin hooks into NetBox (Django 5) under `netbox_librenms_plugin/`; respect NetBox plugin APIs (`navigation.py`, `urls.py`, `api/`).
- LibreNMS communication lives in `librenms_api.py`; reuse this client instead of new `requests` calls. It handles multi-server configs via `LibreNMSSettings` model and the `servers` plugin config, plus caching via Django cache + custom fields.
- Views follow a three-layer structure:
  - **Base views** (`views/base/`) — abstract views for each sync resource (`BaseInterfaceTableView`, `BaseCableTableView`, `BaseIPAddressTableView`, `BaseVLANTableView`).
  - **Object sync views** (`views/object_sync/`) — concrete per-model views registered as tabs on NetBox's Device/VM detail pages via `@register_model_view(Device, ...)`. These wire base views to models.
  - **Sync action views** (`views/sync/`) — POST-only views that apply changes (add/change/delete NetBox objects). Includes `interfaces.py`, `cables.py`, `ip_addresses.py`, `vlans.py`, `devices.py`, `device_fields.py`, `locations.py`.
  - **Shared mixins** (`views/mixins.py`) — `LibreNMSPermissionMixin`, `NetBoxObjectPermissionMixin`, `LibreNMSAPIMixin`, `CacheMixin`, `VlanAssignmentMixin`.
- All four sync resources (interfaces, cables, IP addresses, VLANs) follow the same three-layer pattern. VLAN sync additionally uses `VlanAssignmentMixin` for VLAN group scope resolution (Rack → Location → Site → SiteGroup → Region → Global).
- New views should extend the closest base class and compose mixins.
- Tables (`tables/*.py`) and templates (`templates/netbox_librenms_plugin/`) drive the UI. See `frontend.instructions.md` for HTMX, template, and styling conventions.
- Forms (`forms.py`) include dynamic LibreNMS API-populated choices (location dropdowns, poller groups) and a split-form pattern for settings (server config form + import settings form).
- `import_validation_helpers.py` centralizes validation state mutation during import (role/cluster/rack assignment, issue removal, status recalculation).

## Data & Sync Conventions
- Devices/VMs map to LibreNMS via the `librenms_id` custom field, then cached if absent. Always call `LibreNMSAPI.get_librenms_id` instead of touching the field directly.
- Matching is intentionally **exact-only** for site, platform, device type, and role. See `utils.py` (`find_matching_site`, `match_librenms_hardware_to_device_type`, `find_matching_platform`). Do not add fuzzy matching.
- Sync pipelines generally fetch LibreNMS data (`librenms_api.py`), cache it (`CacheMixin`), build comparison tables (`tables/`), and render HTMX fragments (`templates/netbox_librenms_plugin/htmx/`). Follow that flow for new resources.
- Virtual chassis support uses `get_virtual_chassis_member()` for port-to-member mapping and `get_librenms_sync_device()` for VC priority-based device selection.

## Developer Workflow
- Prefer the devcontainer commands (`netbox-run`, `netbox-run-bg`, `netbox-reload`, `netbox-logs`) described in `.devcontainer/README.md`. They manage NetBox + plugin reloading.
- Static assets belong in `static/netbox_librenms_plugin/`; run NetBox's `collectstatic` when bundling, but the devcontainer handles this automatically.

## Integration Touchpoints
- REST endpoints for imports live in `views/imports/actions.py` (with the list view in `views/imports/list.py`) and surface via `urls.py`. They also emit HTMX fragments (`templates/netbox_librenms_plugin/htmx/device_import_row.html`, etc.). Keep server responses and HTMX targets in sync.
- API serializers (`api/serializers.py`) mirror models for external consumption. Update serializers and `api/views.py` together to avoid contract drift.
- Navigation and menu items are registered in `navigation.py`; extend there for new sections so NetBox renders links correctly.

## Permission System
- Uses two-tier permissions via `LibreNMSSettings` model: `view_librenmssettings` (read) and `change_librenmssettings` (write). See `docs/development/permissions.md`.
- Permission constants in `constants.py`: `PERM_VIEW_PLUGIN` and `PERM_CHANGE_PLUGIN`.

### Plugin-Level Permissions
- All views inherit `LibreNMSPermissionMixin` from `views/mixins.py`, which sets `permission_required = PERM_VIEW_PLUGIN` and provides:
  - `has_write_permission()` — checks `PERM_CHANGE_PLUGIN`.
  - `require_write_permission()` — returns error response (HTMX `HX-Redirect` or standard redirect) if denied.
  - `require_write_permission_json()` — returns `JsonResponse(403)` if denied (for AJAX endpoints).

### Object-Level Permissions
- `NetBoxObjectPermissionMixin` adds a **second layer** of permission checking for NetBox model operations (add/change/delete on Device, Interface, VLAN, etc.).
- Views declare `required_object_permissions` dict mapping HTTP methods to `[(action, Model)]` tuples, e.g.:
  ```python
  required_object_permissions = {"POST": [("add", VLAN), ("change", VLAN)]}
  ```
- Some views set `required_object_permissions` dynamically per-request (e.g., `SyncInterfacesView` switches between `Interface` and `VMInterface` based on object type).
- Provides:
  - `check_object_permissions(method)` → `(bool, missing_perms_list)`
  - `require_object_permissions(method)` — redirect/HTMX on failure.
  - `require_object_permissions_json(method)` — JSON 403 on failure.
  - `require_all_permissions(method)` — combined plugin write + object perms check (redirect/HTMX).
  - `require_all_permissions_json(method)` — combined check, JSON variant.
- **Sync POST handlers** must call `require_all_permissions("POST")` (not just `require_write_permission()`) and return early if it returns a response. AJAX/JSON endpoints use `require_all_permissions_json("POST")`.
- `_get_safe_redirect_url(request)` validates referrer URLs to prevent open-redirect attacks.

### Permission Helpers for Background Jobs
- Background jobs run outside view context and cannot use view mixins. Use standalone helpers from `import_utils/permissions.py` (`check_user_permissions`, `require_permissions`). See `background-jobs.instructions.md` for details.

### API & Navigation Permissions
- API endpoints use `LibreNMSPluginPermission` class in `api/views.py` (GET=view, others=change).
- Navigation menu (`navigation.py`) has 3 groups: **Settings** (Plugin Settings, Interface Mappings), **Import** (LibreNMS Import), **Status Check** (Site & Location Sync, Device Status, VM Status). All items use `permissions=[PERM_VIEW_PLUGIN]`.
- **Background job polling requires superuser** — non-superusers fall back to synchronous mode. See `background-jobs.instructions.md` for details.

## When in Doubt
- Check docs in `docs/development/` for structure, view inheritance, mixins, and template conventions before introducing new patterns.
- Review the existing sync views (e.g., `views/sync/interfaces.py`) as reference implementations for data flow and caching patterns.
- Coordinate any schema changes through Django migrations in `migrations/` and update `models.py` + admin/pydantic representations accordingly.
