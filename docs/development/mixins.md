# Mixins

Mixins in `views/mixins.py` provide reusable logic to keep views clean and DRY (Don't Repeat Yourself). They are designed to be combined with Django or NetBox views to add specific behaviors or shared functionality. When adding new views, consider using or extending these mixins to maintain consistency and reduce code duplication.

### Key Mixins

**LibreNMSPermissionMixin**

  - Provides the plugin-level read permission required by ordinary plugin views: `view_librenmssettings`.
  - Provides `has_write_permission()` and the `require_write_permission()` / `require_write_permission_json()` helpers for actions that also need `change_librenmssettings`.
  - Redirects ordinary and HTMX requests with an error message when a permission check fails. JSON endpoints receive a 403 response instead.

**LibreNMSWritePermissionMixin**

  - A variant of `LibreNMSPermissionMixin` for views that are write-only at the page level.
  - Sets the required plugin permission to `change_librenmssettings`.

**LibreNMSGenericPermissionMixin** and **LibreNMSGenericWritePermissionMixin**

  - Add plugin permissions to NetBox generic views through `additional_permissions`.
  - The read variant adds `view_librenmssettings`; the write variant adds both `view_librenmssettings` and `change_librenmssettings`.
  - These mixins deliberately do not inherit Django's `PermissionRequiredMixin`. NetBox generic views use `ObjectPermissionRequiredMixin`, and inheriting both would shadow NetBox's permission handling and prevent constrained querysets from being applied.

**NetBoxObjectPermissionMixin**

  - Checks the NetBox permissions needed for a view's object operations. Views declare them in `required_object_permissions`, keyed by HTTP method, as `(action, model)` pairs such as `("add", Interface)` or `("change", Interface)`.
  - Provides `require_object_permissions()` for normal or HTMX responses and `require_object_permissions_json()` for JSON responses.
  - `require_all_permissions()` and `require_all_permissions_json()` combine the plugin write check with the NetBox object-permission check. Sync POST handlers should use one of these combined helpers.
  - `restricted_queryset()` and `restrict_object_or_404()` resolve objects through NetBox's object-permission constraints, so a model-level grant cannot expose an out-of-scope object by primary key.

The plugin therefore retains a two-tier permission model: plugin permissions control access to LibreNMS integration features, while NetBox object permissions control which NetBox objects a user may view or change. Both tiers are required for operations that modify NetBox objects. See the [permissions guide](../usage_tips/permissions.md) for the user-facing permission model.

**LibreNMSAPIMixin**

  - Provides a `librenms_api` property for accessing the LibreNMS API from any view.
  - Ensures a single instance of the API client is reused per view instance.
  - Example usage: Add to views that need to fetch or sync data with LibreNMS.

**CacheMixin**

  - Supplies helper methods for generating cache keys related to objects and data types (e.g., ports, links, vlans).
  - Useful for views that cache data fetched from LibreNMS to improve performance.
  - Methods:
    - `get_cache_key(obj, data_type="ports")`: Returns a unique cache key for the object and data type.
    - `get_last_fetched_key(obj, data_type="ports")`: Returns a cache key for tracking when data was last fetched.
    - `get_vlan_overrides_key(obj)`: Returns a cache key for storing user VLAN group override selections.

**VlanAssignmentMixin**

  - Provides VLAN group resolution and assignment logic used by both the Interfaces tab (per-interface VLAN assignments) and the VLANs tab (VLAN object sync).
  - Resolves which VLAN groups are relevant to a device based on a scope hierarchy: Rack → Location → Site → SiteGroup → Region → Global.
  - Methods:
    - `get_vlan_groups_for_device(device)`: Returns all VLAN groups relevant to the device based on scope hierarchy.
    - `_build_vlan_lookup_maps(vlan_groups)`: Builds lookup dictionaries mapping VIDs to groups, VLANs, and names.
    - `_select_most_specific_group(groups, device)`: Resolves ambiguity when a VID exists in multiple groups by selecting the most specific scope.
    - `_find_vlan_in_group(vid, vlan_group_id, lookup_maps)`: Finds a VLAN by VID, preferring the specified group.
    - `_update_interface_vlan_assignment(interface, vlan_data, vlan_group_map, lookup_maps)`: Updates interface mode, untagged VLAN, and tagged VLANs in NetBox.

### How to Use Mixins

To use a mixin, simply add it to the inheritance list of your view class. For example:

```python
from .mixins import LibreNMSAPIMixin, CacheMixin

class MyCustomView(LibreNMSAPIMixin, CacheMixin, SomeBaseView):
    # ... your view logic ...
```

Mixins can be combined as needed. For ordinary plugin views, place the mixins before the base view so their methods and properties are available. For NetBox generic views, use the generic permission mixins described above and preserve NetBox's `ObjectPermissionRequiredMixin` permission handling.
