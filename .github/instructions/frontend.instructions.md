---
applyTo: "netbox_librenms_plugin/templates/**,netbox_librenms_plugin/static/**"
description: Frontend patterns for templates, HTMX, and static assets
---

# Frontend Patterns

## HTMX Conventions
- HTMX 2.x is the primary async layer. Table row updates return `<tr hx-swap-oob="true">`.
- Avoid `outerHTML` swaps; use OOB or targeted `innerHTML` swaps to keep table layout intact.
- All HTMX requests and `fetch()` calls must include a CSRF token. The standard pattern is `document.querySelector('[name=csrfmiddlewaretoken]').value` (from a hidden form input). The import JS also uses `getCookie('csrftoken')` as a fallback — prefer the hidden input approach for consistency.

## Modal Implementation
- Modals try Bootstrap 5 native (`bootstrap.Modal`) first, falling back to manual DOM manipulation if unavailable. Both `librenms_sync.js` and `librenms_import.js` follow this pattern via `showModal()`/`hideModal()` helpers.
- Buttons target the `htmx-modal-content` element and JavaScript in `librenms_import.html` toggles the wrapper.
- Do not reintroduce `data-bs-toggle` or duplicate modal IDs.
- The import page uses `ModalManager` class and `filterModalManager` instance—always use this reference in fetch callbacks, not undefined `modalInstance` variables.
- Dismiss handlers (backdrop click, `data-bs-dismiss` buttons) are bound once per element to prevent stacking on repeated `showModal()` calls.

## JavaScript Fetch Patterns
- Always check `response.ok` before processing fetch responses to catch HTTP errors.
- In catch blocks, show `error.message` for debugging rather than generic messages.
- The import filter form uses fetch with `Accept: application/json, text/html`—JSON for background jobs, HTML for synchronous mode.

## Form Controls
- Device import dropdowns rely on TomSelect decorators set up elsewhere.
- Keep `<select class="device-role-select">` markup stable to preserve JS hook-up.

## Styling
- Styling assumes Tabler defaults.
- Removing `table-responsive` wrappers was deliberate to prevent dropdown clipping—do not re-add them.

## Template Structure
- Templates live in `templates/netbox_librenms_plugin/`; reuse/includes under `inc/`.
- Sync pages extend `librenms_sync_base.html`.
- Tables emit HTMX-enabled columns and buttons (`tables/*.py`), so prefer updating the table renderer in Python rather than templates when changing row actions.

## Sync Tab Template Pattern
- Each sync resource has two templates following a naming convention:
  - `_<resource>_sync.html` — the tab wrapper, loaded once when the tab is selected.
  - `_<resource>_sync_content.html` — the HTMX-swappable inner fragment, refreshed on data changes without a full page reload.
- Current resources: `_interface_sync`, `_cable_sync`, `_ipaddress_sync`, `_vlan_sync`.
- When adding a new sync resource, create both the wrapper and content templates following this pattern.

## HTMX Fragments
- HTMX fragments live in `templates/netbox_librenms_plugin/htmx/` and include:
  - `device_import_row.html` — individual import row updates.
  - `device_validation_details.html` — expandable validation details.
  - `device_vc_details.html` — virtual chassis member details.
  - `bulk_import_confirm.html` — import confirmation modal content.
- Keep server responses and HTMX targets in sync when modifying these fragments.

## Settings Page
- `settings.html` uses a split-form pattern: two separate Django forms (`ServerConfigForm` + `ImportSettingsForm`) sharing one page, differentiated by a hidden `form_type` field (`"server_config"` or `"import_settings"`).
- The test-connection button is an HTMX POST to `TestLibreNMSConnectionView`, returning an inline alert fragment.

## Paginator
- `inc/paginator.html` is a custom paginator that preserves tab state and `interface_name_field` in pagination URLs. Used across all sync tables.

## Import Page JavaScript (`librenms_import.js`)
- Wrapped in an IIFE with `window.LibreNMSImportInitialized` guard to prevent re-initialization during HTMX swaps.
- **`ModalManager`** class wraps Bootstrap 5 modal show/hide with fallback.
- **`pollJobStatus()`** — polls `/api/core/background-tasks/{jobId}/` every 2s, updates progress messages, handles cancel button, redirects on completion.
- **`captureSelectionState()` / `restoreSelectionState()`** — preserves checkbox state across HTMX content swaps.
- **`createCacheCountdown()`** — generic countdown timer for cache expiration display.
- **`initializeFilterForm()`** — intercepts form submit, detects JSON response (background job), starts polling.
- CSRF token extracted via `getCookie('csrftoken')` (cookie-based).
