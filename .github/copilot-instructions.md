# NetBox LibreNMS Plugin – AI Assistant Guide

## Architecture & Key Modules
- Plugin hooks into NetBox (Django 5) under `netbox_librenms_plugin/`; respect NetBox plugin APIs (`navigation.py`, `urls.py`, `api/`).
- LibreNMS communication lives in `librenms_api.py`; reuse this client instead of new `requests` calls. It handles multi-server configs (`LibreNMSSettings`, `PLUGINS_CONFIG['servers']`) and caching via Django cache + custom fields.
- Views follow layered structure: resource views in `views/`, shared logic in `views/base/` and `views/mixins.py`. New views should extend the closest base class and compose mixins (e.g., `LibreNMSAPIMixin`, `CacheMixin`).
- Tables drive most UIs (`tables/*.py`). They emit HTMX-enabled columns and buttons, so prefer updating the table renderer rather than templates when changing row actions.
- Templates live in `templates/netbox_librenms_plugin/`; reuse/includes under `inc/`. Sync pages extend `librenms_sync_base.html`.

## Frontend Patterns
- HTMX 2.x is the primary async layer. Table row updates return `<tr hx-swap-oob="true">`. Avoid `outerHTML` swaps; use OOB or targeted `innerHTML` swaps to keep table layout intact.
- Modals use Tabler (Bootstrap-like) but without `bootstrap.Modal` helpers. Buttons target the `htmx-modal-content` element and JavaScript in `librenms_import.html` toggles the wrapper. Do not reintroduce `data-bs-toggle` or duplicate modal IDs.
- Device import dropdowns rely on TomSelect decorators set up elsewhere; keep `<select class="device-role-select">` markup stable to preserve JS hook-up.
- Styling assumes Tabler defaults. Removing `table-responsive` wrappers was deliberate to prevent dropdown clipping.

## Data & Sync Conventions
- Devices/VMs map to LibreNMS via the `librenms_id` custom field, then cached if absent. Always call `LibreNMSAPI.get_librenms_id` instead of touching the field directly.
- Matching is intentionally **exact-only** for site, platform, device type, and role. See `import_utils.py` and `tables/device_status.py`. Do not add fuzzy matching back.
- Sync pipelines generally fetch LibreNMS data (`librenms_api.py`), cache it (`CacheMixin`), build comparison tables (`tables/`), and render HTMX fragments (`templates/netbox_librenms_plugin/htmx/`). Follow that flow for new resources.

## Background Jobs & Task Management
- Background jobs use NetBox's `JobRunner` base class (`netbox.jobs.JobRunner`) for long-running operations like device filtering with VC detection.
- Jobs run via Redis Queue (RQ) in Redis, separate from the database Job model. Real-time status must be checked via RQ, not the database.
- **Critical job architecture points:**
  - Job UUID (`job.job_id`) is used for RQ API endpoints: `/api/core/background-tasks/{uuid}/`
  - Job PK (`job.pk`) is used for database endpoints and result loading
  - RQ status values: `queued`, `started`, `finished`, `stopped`, `failed` (NOT `completed`)
  - Database Job status values: `pending`, `scheduled`, `running`, `completed`, `failed`, `errored` (NO `cancelled` status exists)
  - Check `rq_job.is_stopped` or `rq_job.is_failed` flags in Redis for cancellation detection, not database status
- **Job cancellation flow:**
  1. Call `/api/core/background-tasks/{uuid}/stop/` to stop RQ job
  2. Call plugin's sync endpoint `/api/plugins/librenms_plugin/jobs/{pk}/sync-status/` to update database
  3. Frontend polling detects status changes and redirects appropriately
- **Polling implementation:**
  - Poll `/api/core/background-tasks/{uuid}/` for real-time RQ status
  - Update modal messages based on status: "Job queued...", "Processing...", "Job completed!"
  - Handle all RQ status values explicitly to avoid infinite polling
  - Use `cancelInProgress` flag to prevent polling interference during cancellation
- **Custom sync endpoint:** `api/views.py::sync_job_status()` syncs database Job status with RQ job status, needed because NetBox worker doesn't always update DB when jobs stop before processing starts.

## Developer Workflow
- Prefer the devcontainer commands (`netbox-run`, `netbox-run-bg`, `netbox-reload`, `netbox-logs`) described in `.devcontainer/README.md`. They manage NetBox + plugin reloading.
- Formatting/linting: `make format` (isort + black) and `make lint` (flake8). No Ruff yet—align with Makefile targets.
- Tests are minimal (`tests/test_netbox_librenms_plugin.py`). When adding tests, leverage NetBox's plugin test helpers and keep them under `tests/`.
- Static assets belong in `static/netbox_librenms_plugin/`; run NetBox's `collectstatic` when bundling, but the devcontainer handles this automatically.

## Integration Touchpoints
- REST endpoints for imports live in `views/imports/actions.py` (with the list view in `views/imports/list.py`) and surface via `urls.py`. They also emit HTMX fragments (`templates/netbox_librenms_plugin/htmx/device_import_row.html`, etc.). Keep server responses and HTMX targets in sync.
- API serializers (`api/serializers.py`) mirror models for external consumption. Update serializers and `api/views.py` together to avoid contract drift.
- Navigation and menu items are registered in `navigation.py`; extend there for new sections so NetBox renders links correctly.

## When in Doubt
- Check docs in `docs/development/` for structure, view inheritance, mixins, and template conventions before introducing new patterns.
- Review the existing sync views (e.g., `views/sync/interfaces.py`) as reference implementations for data flow and caching patterns.
- Coordinate any schema changes through Django migrations in `migrations/` and update `models.py` + admin/pydantic representations accordingly.
