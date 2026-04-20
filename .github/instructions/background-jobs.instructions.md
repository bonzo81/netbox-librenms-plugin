---
applyTo: "**/jobs.py,**/views/imports/**,**/import_utils/**,**/import_validation_helpers.py"
description: Background job architecture, import workflow, and task management patterns
---

# Background Jobs & Import Workflow

## Job Architecture
- Background jobs use NetBox's `JobRunner` base class (`netbox.jobs.JobRunner`) for long-running operations like device filtering with VC detection.
- Jobs run via Redis Queue (RQ) in Redis, separate from the database Job model. Real-time status must be checked via RQ, not the database.

## Critical Job Architecture Points
- Job UUID (`job.job_id`) is used for RQ API endpoints: `/api/core/background-tasks/{uuid}/`
- Job PK (`job.pk`) is used for database endpoints and result loading
- RQ status values: `queued`, `started`, `finished`, `stopped`, `failed` (NOT `completed`)
- Database Job status values: `pending`, `scheduled`, `running`, `completed`, `failed`, `errored` (NO `cancelled` status exists)
- Check `rq_job.is_stopped` or `rq_job.is_failed` flags in Redis for cancellation detection, not database status

## Job Cancellation Flow
1. Call `/api/core/background-tasks/{uuid}/stop/` to stop RQ job
2. Call plugin's sync endpoint `/api/plugins/librenms_plugin/jobs/{pk}/sync-status/` to update database
3. Frontend polling detects status changes and redirects appropriately

## Polling Implementation
- Poll `/api/core/background-tasks/{uuid}/` for real-time RQ status
- Update modal messages based on status: "Job queued...", "Processing...", "Job completed!"
- Handle all RQ status values explicitly to avoid infinite polling
- Use `cancelInProgress` flag to prevent polling interference during cancellation

## Superuser Requirement for Background Jobs
- NetBox's `/api/core/background-tasks/` endpoint requires **superuser** (`IsSuperuser` in `BaseRQViewSet`).
- Non-superuser users cannot poll job status; they get 403 Forbidden.
- The plugin automatically falls back to synchronous mode for non-superusers—see `should_use_background_job()` in `list.py` and `actions.py`.
- This is a NetBox core design decision, not a plugin limitation. No amount of permissions (including `core.view_job`) bypasses it.

## Import Jobs
- **`FilterDevicesJob`** — background device filtering with VC detection. `job.data` keys: `device_ids`, `total_processed`, `filters`, `server_key`, `vc_detection_enabled`, `cache_timeout`, `cached_at`, `completed`. Devices are cached individually via shared cache keys from `get_validated_device_cache_key()`.
- **`ImportDevicesJob`** — background device/VM import. Calls `bulk_import_devices_shared()` for devices and `bulk_import_vms()` for VMs. `job.data` keys: `imported_device_pks`, `imported_vm_pks`, `imported_libre_device_ids`, `imported_libre_vm_ids`, `server_key`, `total`, `success_count`, `failed_count`, `skipped_count`, `virtual_chassis_created`, `errors`, `completed`.

## Shared Cache Key Pattern
- Both synchronous and background modes use `get_validated_device_cache_key()` from `import_utils.py` to generate cache keys. This ensures `_load_job_results()` in the list view can retrieve devices regardless of which mode produced them.
- `get_active_cached_searches()` manages multi-search cache to let users run and switch between searches.
- Never hardcode cache key formats; always use the helper functions.

## Permission Checks in Jobs
- Background jobs run outside view context, so they cannot use view mixins.
- Use standalone helpers from `import_utils.py` for permission checks inside job code:
  - `check_user_permissions(user, permissions)` → `(bool, missing_list)`
  - `require_permissions(user, permissions, action_description)` — raises `PermissionDenied`.

## Custom Sync Endpoint
`api/views.py::sync_job_status()` syncs database Job status with RQ job status, needed because NetBox worker doesn't always update DB when jobs stop before processing starts.

## Import Page Flow
The import page (`LibreNMSImportView` in `views/imports/list.py`) supports two modes:

1. **Synchronous** — calls `process_device_filters()` directly, renders results inline.
2. **Background** — enqueues `FilterDevicesJob`, returns `JsonResponse` with `job_id`/`job_pk`/`poll_url`. Frontend polls and redirects to `?job_id={pk}` on completion.

Result loading: `_load_job_results(job_id)` reads `job.data["device_ids"]`, reconstructs devices from per-device cache using `get_validated_device_cache_key()`.

Filter fields: `librenms_location`, `librenms_type`, `librenms_os`, `librenms_hostname`, `librenms_sysname`, `librenms_hardware`, `enable_vc_detection`, `show_disabled`, `exclude_existing`.

## Import Action Views (`views/imports/actions.py`)
- **`DeviceImportHelperMixin`** — provides `get_validated_device_with_selections()` and `render_device_row()` for HTMX row rendering. Shared by update views.
- **`BulkImportConfirmView`** (POST) — renders confirmation modal with selected device list. Returns `htmx/bulk_import_confirm.html`.
- **`BulkImportDevicesView`** (POST) — executes import. Background mode enqueues `ImportDevicesJob`; sync mode calls `bulk_import_devices()` + `bulk_import_vms()` and returns OOB row swaps with `HX-Trigger: closeModal`.
- **`DeviceValidationDetailsView`** (GET) — renders expandable validation details via `htmx/device_validation_details.html`.
- **`DeviceVCDetailsView`** (GET) — renders VC member details via `htmx/device_vc_details.html`.
- **`DeviceRoleUpdateView`**, **`DeviceClusterUpdateView`**, **`DeviceRackUpdateView`** (POST) — per-device dropdown updates. Apply selection to validation state and return re-rendered row via `render_device_row()`.

## Key Import Utilities (`import_utils/` package)
`import_utils/` is a package; the `__init__.py` re-exports key functions so callers can still use `from import_utils import ...`.

- `filters.py` — `process_device_filters(filters, ...)`, `fetch_device_with_cache(device_id, ...)`.
- `device_operations.py` — `validate_device_for_import(device, ...)`, `bulk_import_devices_shared(devices, user, ...)`.
- `vm_operations.py` — `bulk_import_vms(vm_imports, user, ...)`.
- `cache.py` — `get_validated_device_cache_key()`, `get_cache_metadata_key()`, `get_active_cached_searches()`, `get_import_device_cache_key()`.
- `permissions.py` — `check_user_permissions(user, permissions)`, `require_permissions(user, permissions, action_description)`.
- `virtual_chassis.py` — `create_virtual_chassis_with_members()`, `_sync_module_bay_counter()`.

## Validation Helpers (`import_validation_helpers.py`)
Centralizes validation state mutation used by the role/cluster/rack update views:
- `apply_role_to_validation()`, `apply_cluster_to_validation()`, `apply_rack_to_validation()` — update validation state when user selects a role/cluster/rack.
- `remove_validation_issue()`, `recalculate_validation_status()` — maintain issue list and overall status.
- `fetch_model_by_id()`, `extract_device_selections()` — helpers for reading form data.
