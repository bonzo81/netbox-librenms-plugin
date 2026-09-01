---
applyTo: "**/views/base/**,**/views/object_sync/**,**/views/sync/**,**/tables/**,**/librenms_sync.js"
description: Sync page architecture, base views, and sync action patterns
---

# Sync Pages

## Three-Layer View Architecture
All four sync resources (interfaces, cables, IP addresses, VLANs) follow the same pattern:

1. **Base views** (`views/base/`) — abstract classes that define the data pipeline:
   - `BaseLibreNMSSyncView` — tabbed sync page, orchestrates all tabs via abstract `get_*_context()` methods.
   - `BaseInterfaceTableView` — fetch ports → enrich with VLANs → cache → compare with NetBox interfaces → render table.
   - `BaseCableTableView` — fetch links → match remote devices → check cable status → render table.
   - `BaseIPAddressTableView` — fetch IPs → resolve interfaces → detect existing/update/new → render table.
   - `BaseVLANTableView` — fetch VLANs → compare with NetBox VLANs → auto-select groups → render table.

2. **Object sync views** (`views/object_sync/`) — wire base views to NetBox models:
   - Use `@register_model_view(Device, name="librenms_sync", path="librenms-sync")` to inject as a tab on Device/VM detail pages.
   - Each `get_*_context()` method creates an instance of the concrete table view, copies `request`, and calls `get_context_data()`.
   - VMs skip cables and VLANs (return `None`).

3. **Sync action views** (`views/sync/`) — POST-only views that create/update/delete NetBox objects:
   - Follow a consistent pattern: check permissions → read selected rows from POST → load cached data → apply changes in `transaction.atomic()` → return the action's HTMX response, or redirect to the sync tab for a non-HTMX request.
   - Cable sync returns `_cable_sync_content.html` for HTMX so the response matches `#cable-sync-content`. Other actions can use `HX-Redirect` when they require full navigation.

## Data Pipeline (Base Views)
Every base table view follows: **fetch → cache → compare → render**.

- **Fetch:** Call LibreNMS API (e.g., `get_ports()`, `get_device_ips()`, `get_device_vlans()`).
- **Cache:** Store results via `CacheMixin` keys: `librenms_{data_type}_{model_name}_{pk}`. Also store fetch timestamp at `librenms_{data_type}_last_fetched_{model_name}_{pk}`.
- **Compare:** Match LibreNMS data against NetBox objects. Each resource implements its own comparison (interface matching by name, IP matching by address/mask, VLAN matching by VID+group).
- **Render:** Build a django-tables2 table, return a partial template (`_*_sync_content.html`).

## Sync Action View Pattern
```python
class SyncSomeResourceView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, CacheMixin, View):
    required_object_permissions = {"POST": [("add", Model), ("change", Model)]}

    def post(self, request, object_type, object_id):
        if error := self.require_all_permissions("POST"):
            return error
        # 1. Resolve object (Device or VM)
        # 2. Read selected items from request.POST.getlist("select")
        # 3. Load cached data from cache.get(self.get_cache_key(obj, "..."))
        # 4. Apply changes inside transaction.atomic()
        # 5. Return the action's HTMX response, or redirect to ?tab=<resource>
```

## Table Conventions (`tables/*.py`)
- Tables define HTMX-enabled columns and checkboxes. Selection uses `ToggleColumn(attrs={"input": {"name": "select"}})`.
- Constructor takes contextual params (e.g., `device`, `interface_name_field`, `vlan_groups`) to customize rendering.
- Tables set `self.tab` and `self.prefix` for multi-table pagination via `get_table_paginate_count()`.
- Row attrs include `data-*` attributes for JavaScript filtering and identification.
- VLAN columns use `render_vlans()` with hidden inputs for per-row group selection and JSON data for modals.

## Key Mixins Used by Sync Views
- **`LibreNMSAPIMixin`** — lazy-creates `LibreNMSAPI` instance via `self.librenms_api` property. Also provides `get_server_info()` for template context.
- **`CacheMixin`** — generates consistent cache keys via `get_cache_key(obj, data_type)` and `get_last_fetched_key(obj, data_type)`. Also provides `get_vlan_overrides_key(obj)` for VLAN group override persistence.
- **`VlanAssignmentMixin`** — VLAN group scope resolution: Rack → Location → Site → SiteGroup → Region → Global. Used by interface and VLAN sync for auto-selecting the most-specific VLAN group and building lookup maps.

## JavaScript (`librenms_sync.js`)
- Not wrapped in an IIFE — functions are global. Master initializer `initializeScripts()` runs on `DOMContentLoaded` and `htmx:afterSwap`.
- **Key function groups:**
  - Checkbox management: `initializeTableCheckboxes()`, `updateBulkActionButton()`.
  - TomSelect dropdowns: `initializeVCMemberSelect()`, `initializeVRFSelects()`, `initializeVlanGroupSelects()`, `initializeVlanSyncGroupSelects()`. Uses `TOMSELECT_INIT_DELAY_MS = 100` for delayed initialization after HTMX swaps.
  - Verification: `handleInterfaceChange()`, `handleCableChange()`, `handleVRFChange()` — POST to single-item verify endpoints.
  - VLAN modals: `openVlanDetailModal()`, `verifyVlanInGroup()`, `verifyVlanSyncGroup()` — per-interface VLAN detail editing.
  - Bulk operations: `initializeBulkEditApply()`, `deleteSelectedInterfaces()`.
  - Table filtering: `initializeTableFilters()`, `filterTable()` — client-side row filtering.
  - URL/tab state: `initializeTabs()`, `getDeviceIdFromUrl()`, `setInterfaceNameFieldFromURL()`.
  - Cache countdowns: `initializeCountdown()`, `initializeCountdowns()`.
- CSRF token extracted via `document.querySelector('[name=csrfmiddlewaretoken]').value`.
