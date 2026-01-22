---
applyTo: "netbox_librenms_plugin/templates/**,netbox_librenms_plugin/static/**"
description: Frontend patterns for templates, HTMX, and static assets
---

# Frontend Patterns

## HTMX Conventions
- HTMX 2.x is the primary async layer. Table row updates return `<tr hx-swap-oob="true">`.
- Avoid `outerHTML` swaps; use OOB or targeted `innerHTML` swaps to keep table layout intact.
- HTMX fragments live in `templates/netbox_librenms_plugin/htmx/`. Keep server responses and HTMX targets in sync.

## Modal Implementation
- Modals use Tabler (Bootstrap-like) but **without** `bootstrap.Modal` helpers.
- Buttons target the `htmx-modal-content` element and JavaScript in `librenms_import.html` toggles the wrapper.
- Do not reintroduce `data-bs-toggle` or duplicate modal IDs.

## Form Controls
- Device import dropdowns rely on TomSelect decorators set up elsewhere.
- Keep `<select class="device-role-select">` markup stable to preserve JS hook-up.

## Styling
- Styling assumes Tabler defaults.
- Removing `table-responsive` wrappers was deliberate to prevent dropdown clipping—do not re-add them.

## Template Structure
- Templates live in `templates/netbox_librenms_plugin/`; reuse/includes under `inc/`.
- Sync pages extend `librenms_sync_base.html`.
- Tables drive most UIs (`tables/*.py`). They emit HTMX-enabled columns and buttons, so prefer updating the table renderer in Python rather than templates when changing row actions.
