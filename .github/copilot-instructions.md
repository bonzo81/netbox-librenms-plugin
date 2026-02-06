# NetBox LibreNMS Plugin – AI Assistant Guide

> **Note:** Additional context-specific instructions are in `.github/instructions/`:
> - [testing.instructions.md](instructions/testing.instructions.md) – applies to `tests/**`
> - [frontend.instructions.md](instructions/frontend.instructions.md) – applies to templates and static files
> - [background-jobs.instructions.md](instructions/background-jobs.instructions.md) – applies to `jobs.py` and import views

## Architecture & Key Modules
- Plugin hooks into NetBox (Django 5) under `netbox_librenms_plugin/`; respect NetBox plugin APIs (`navigation.py`, `urls.py`, `api/`).
- LibreNMS communication lives in `librenms_api.py`; reuse this client instead of new `requests` calls. It handles multi-server configs via `LibreNMSSettings` model and the `servers` plugin config, plus caching via Django cache + custom fields.
- Views follow layered structure: resource views in `views/`, shared logic in `views/base/` and `views/mixins.py`. New views should extend the closest base class and compose mixins (e.g., `LibreNMSAPIMixin`, `CacheMixin`).
- Tables drive most UIs (`tables/*.py`). They emit HTMX-enabled columns and buttons, so prefer updating the table renderer rather than templates when changing row actions.
- Templates live in `templates/netbox_librenms_plugin/`; reuse/includes under `inc/`. Sync pages extend `librenms_sync_base.html`.

## Data & Sync Conventions
- Devices/VMs map to LibreNMS via the `librenms_id` custom field, then cached if absent. Always call `LibreNMSAPI.get_librenms_id` instead of touching the field directly.
- Matching is intentionally **exact-only** for site, platform, device type, and role. See `utils.py` (`find_matching_site`, `match_librenms_hardware_to_device_type`, `find_matching_platform`). Do not add fuzzy matching.
- Sync pipelines generally fetch LibreNMS data (`librenms_api.py`), cache it (`CacheMixin`), build comparison tables (`tables/`), and render HTMX fragments (`templates/netbox_librenms_plugin/htmx/`). Follow that flow for new resources.

## Developer Workflow
- Prefer the devcontainer commands (`netbox-run`, `netbox-run-bg`, `netbox-reload`, `netbox-logs`) described in `.devcontainer/README.md`. They manage NetBox + plugin reloading.
- Static assets belong in `static/netbox_librenms_plugin/`; run NetBox's `collectstatic` when bundling, but the devcontainer handles this automatically.

## Integration Touchpoints
- REST endpoints for imports live in `views/imports/actions.py` (with the list view in `views/imports/list.py`) and surface via `urls.py`. They also emit HTMX fragments (`templates/netbox_librenms_plugin/htmx/device_import_row.html`, etc.). Keep server responses and HTMX targets in sync.
- API serializers (`api/serializers.py`) mirror models for external consumption. Update serializers and `api/views.py` together to avoid contract drift.
- Navigation and menu items are registered in `navigation.py`; extend there for new sections so NetBox renders links correctly.

## Permission System
- Uses two-tier permissions via `LibreNMSSettings` model: `view_librenmssettings` (read) and `change_librenmssettings` (write). See `docs/development/permissions.md`.
- All views inherit `LibreNMSPermissionMixin` from `views/mixins.py`. Permission constants live in `constants.py`.
- **Sync POST handlers** must call `require_write_permission()` at the start and return early if it returns a response.
- `require_write_permission()` handles HTMX requests with `HX-Redirect` header; regular requests get standard redirect.
- API endpoints use `LibreNMSPluginPermission` class in `api/views.py` (GET=view, others=change).
- Navigation menu permissions are set in `navigation.py` using permission constants.
- **Background job polling requires superuser** (NetBox core restriction on `/api/core/background-tasks/`). Non-superusers automatically fall back to synchronous mode—see `should_use_background_job()` methods.

## When in Doubt
- Check docs in `docs/development/` for structure, view inheritance, mixins, and template conventions before introducing new patterns.
- Review the existing sync views (e.g., `views/sync/interfaces.py`) as reference implementations for data flow and caching patterns.
- Coordinate any schema changes through Django migrations in `migrations/` and update `models.py` + admin/pydantic representations accordingly.
