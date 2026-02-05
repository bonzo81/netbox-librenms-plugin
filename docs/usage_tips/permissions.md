# Permissions & Access Control

## Overview

The plugin uses a two-tier permission system that works with NetBox's built-in permissions. Both tiers must be satisfied for users to perform actions:

1. **Plugin permissions** control access to plugin pages and features
2. **NetBox object permissions** control what objects users can create or modify

This design ensures the plugin respects your existing NetBox permission structure. A user might have full plugin access but still be restricted to creating certain objects based on their NetBox permissions.


> Superusers have full access to all plugin features and NetBox objects by default. So the following applies only to regular users who can be granted specific permissions as needed.


## Two-Tier Permission Model

### How the Tiers Work Together?

A user needs both tiers of permissions to complete an action. For example, to view the Librenms Import page AND import a device:

1. **Tier 1: Plugin permission**: User needs View AND Change permission on **LibreNMS Settings**

    - View: allows access to the plugin pages and pulling data from LibreNMS.
    - Change: allows performing actions that modify Netbox or Librenms data

The Plugin also enforces Netbox object permissions so the following permission would also be required:

2. **Tier 2: Object permission**: User needs `dcim.add_device` (to create the device in NetBox)

If either permission is missing, the operation fails with an appropriate error message.

## Creating Permissions

All permissions are created using Netbox's standard Object permissions UI.

For details on how NetBox permissions work, see the [NetBox Permissions documentation](https://netboxlabs.com/docs/netbox/administration/permissions/).

### Plugin Permissions

To grant a user or group access to the plugin:

1. Go to **Admin → Permissions**
2. Click **Add**
3. For **Object types**, select "NetBox Librenms Plugin | LibreNMS Settings"
4. Under **Actions**, check the permissions to grant:
      - ☑ **Can view** — for read-only access
      - ☑ **Can change** — for write access (requires View as well)
5. Assign to specific **Users** or **Groups**
6. Click **Save**

### NetBox Object Permissions

NetBox object permissions are created similarly but for different object types (DCIM, IPAM, VIRTUALIZATION, etc.).

### Interface Type Mapping Permissions

The Interface Type Mapping feature uses its own object permissions in addition to the plugin permissions. To manage interface mappings, users need:

- **Plugin permission**: View permission on LibreNMS Settings (to access the page)
- **Object permissions**: `netbox_librenms_plugin.add_interfacetypemapping`, `change_interfacetypemapping`, or `delete_interfacetypemapping` as needed

These permissions are enforced automatically by NetBox's generic views.


## Example Scenarios

### Read-Only Access

- **Plugin permissions**: Librenms Setting View only (Can view LibreNMS Plugin pages)
- **NetBox permissions**: View permissions for devices, interfaces, etc.

Users can access all plugin pages, refresh data from LibreNMS, and review comparison tables, but cannot import devices or sync data.

### Full Plugin Access

- **Plugin permissions**: View + Change (Can view LibreNMS Plugin Pages and Import and sync devices data)
- **NetBox permissions**: Add/change permissions for devices, interfaces, cables, IP addresses

Users have full access to all plugin features and can import devices, sync interfaces, and create cables.


## Further Details
### Tier 1: Plugin Permissions

Plugins permissions use the **LibreNMS Settings** model permissions:

| Permission | NetBox UI Selection | Grants |
|------------|---------------------|--------|
| `view_librenmssettings` | LibreNMS Settings → ☑ Can view | Access all plugin pages, view LibreNMS data |
| `change_librenmssettings` | LibreNMS Settings → ☑ Can change | Import devices, sync data, save settings |

Users without View permission won't see the LibreNMS menu or the LibreNMS Sync tab. Users with **View** but not **Change** can browse all plugin pages but cannot perform import or sync actions that modify Netbox data and Librenms data like Locatons and Adding devices.

### Tier 2: NetBox Object Permissions

When the plugin creates or modifies NetBox objects (devices, interfaces, cables, IP addresses), NetBox enforces its standard object permissions. The plugin checks these permissions and will block operations if the user lacks the required access.

| Plugin Action | Required Object Permissions |
|---------------|----------------------------|
| Import device | `dcim.add_device`, `dcim.add_interface` |
| Import device with VC | Above + `dcim.add_virtualchassis` |
| Import VM | `virtualization.add_virtualmachine` |
| Sync interfaces | `dcim.add_interface`, `dcim.change_interface` |
| Delete interfaces | `dcim.delete_interface` |
| Sync VM interfaces | `virtualization.add_vminterface`, `virtualization.change_vminterface` |
| Delete VM interfaces | `virtualization.delete_vminterface` |
| Sync cables | `dcim.add_cable`, `dcim.change_cable` |
| Sync IP addresses | `ipam.add_ipaddress`, `ipam.change_ipaddress` |
| Sync device fields | `dcim.change_device` |
| Create platform | `dcim.add_platform` |


### Why LibreNMS Settings Permissions?

NetBox's permission system is object-based—permissions are tied to specific models like Device, Interface, or Cable. However, the plugin's Import and Sync pages are feature pages that don't have their own dedicated models. They work with LibreNMS data and create or modify existing NetBox objects.

To control access to these pages, the plugin uses the **LibreNMS Settings** model permissions as a gate for all plugin features:

- **No dedicated models for pages** — The Import and Sync pages aren't objects, so we need an existing model to attach permissions to
- **No custom migrations required** — Uses Django's built-in model permissions that NetBox already understands
- **Standard NetBox workflow** — Administrators assign permissions the same way they do for any other NetBox object
- **Single permission per access level** — One "View" permission for read access, one "Change" permission for write access

While using a settings model for access control may seem unconventional, it provides a simple and maintainable way to gate plugin access without introducing custom permission infrastructure.


## Special note: Background Jobs and Superuser Access

The device import page can use background jobs to help support large device sets, and virtual chassis detection. However, NetBox restricts access to background job status APIs to superusers. There is no permission in NetBox for this.  This is a core design decision, not a plugin limitation.

| User Type | Background Jobs |
|-----------|-----------------|
| Superuser | Full access to background jobs with real-time status updates |
| Non-superuser | Automatic fallback to synchronous processing |

The plugin automatically detects whether the current user is a superuser and adjusts behavior accordingly. Non-superuser users don't need to change any settings—the plugin simply processes requests synchronously instead of as background jobs. All import and filter operations work correctly regardless of superuser status.

## Troubleshooting

**User can't see the LibreNMS menu**
: The user doesn't have View permission for the plugin. Add an Object Permission for "LibreNMS Settings" with "Can view" checked.

**User sees pages but can't import or sync**
: The user has View permission but not Change permission. Edit their Object Permission to also include "Can change".

**User gets "permission denied" when importing devices**
: The user has plugin permissions but may be missing NetBox object permissions. Check that they have `dcim.add_device` and related permissions.

**Background jobs show 403 errors in console**
: This is expected for non-superuser users. The plugin automatically falls back to synchronous mode, so functionality is not affected. The console errors can be ignored.
