# Sync Tabs

The **LibreNMS Sync** page compares LibreNMS data with the selected NetBox Device or Virtual Machine. Refresh a tab to load current LibreNMS data before applying changes.

The Interfaces and Modules tables always fit the window. When the cells cannot wrap enough, the table hides its least important columns first (for example, LibreNMS ID and Description) and names them above the table. Widen the window to show them again.

Each tab remembers the **Per Page** size that you select, for your user and for that tab only. Until you select a size on a tab, the tab uses your NetBox page size.

## Filters

The Interfaces, Cables, IP Addresses and VLANs tabs have a filter above the table. The server filters all rows of the device before it makes the pages, so a match on another page shows too, and the row count reads "Showing x-y of N (filtered from M)".

- **Search** finds the rows where any column shows the text: the cell text, the selected option of a dropdown, and the status and action labels. It does not search tooltips or options that are not selected. Upper and lower case and extra spaces do not matter.
- **Sync state** shows the matching, the mismatched, or the not-in-NetBox rows. Each tab below says what these states mean for its rows. A row in no state shows only under **All**.
- **Columns** opens one field for each column. Search, Sync state and the column fields must all match.

Select **Apply** or press Enter to filter. Enter in a filter field never starts a sync. **Clear filters** removes the filters of that tab.

The filters are part of the page URL. Paging, sorting, the page size, refreshing, syncing and the confirmation dialogs keep the filters of every tab. A refresh or a sync also keeps the page, or shows the last page when the page no longer exists. A new filter starts at the first page. These changes reset more:

- A switch to another LibreNMS server removes the filters and pages of every tab. It keeps the page sizes and the sorts.
- A change of the interface name field loads the page again with the same filters, at the first page of every tab.
- A page load (paging, a filter change or a name field change) loses the changes in the table that you did not submit.

A row that changes after the page loads, for example when you select another Virtual Chassis member, is marked **Changed since filtering**, because it can be outside the filter now. Select **Apply** to filter again. A row selection belongs to one filter, so a new filter starts with no rows selected. Selected rows that the page does not show, on another page or outside the filter, stay selected, and the notice above the table counts them.

A filter that is not valid, for example text longer than 256 characters, shows its errors in place of the table of that tab. The other tabs and the sync actions work as usual.

## Interfaces

Available for Devices and Virtual Machines. This tab creates or updates interfaces and can synchronize descriptions, status, type, speed, VLAN assignments, MAC addresses, and MTU values.

Interface names can come from different LibreNMS fields. Type mappings control how LibreNMS interface types become NetBox interface types. The tab also handles LAG and parent relationships, NetBox-only interfaces, and Virtual Chassis member assignment.

In the [filter](#filters), **Sync state** shows the interfaces that match NetBox, the interfaces with a different value in NetBox, or the interfaces that NetBox does not have. An ignored, ambiguous or incomplete row is in no state. The counts of ignored and NetBox-only interfaces describe the whole device, also under a filter.

A row cannot sync when its name is already used by a NetBox interface that is bound to a different LibreNMS port. The row shows that port. If the port is another row in the table, sync that row first, then sync this row. If LibreNMS no longer reports the port (for example, after the device was rediscovered), the row shows **Rebind**. Rebind moves only the LibreNMS binding of that interface to the row's port. The interface keeps its name, IP addresses, and cables. Rebind is not offered while the OOB controller data is incomplete.

## Cables

Available for Devices. This tab uses LibreNMS link data to create NetBox cable connections. Both devices and their interfaces should exist in NetBox first, so synchronize interfaces before cables.

The `librenms_id` custom field on interfaces improves matching when interface names differ between NetBox and LibreNMS.

In the [filter](#filters), **Sync state** shows the rows whose cable NetBox has, directly or through a patch path; the rows whose cable goes to another port, or still needs the LibreNMS cable tag; and the rows whose cable NetBox does not have. A row whose cable state you cannot view, whose far end is not in NetBox, or that a rule or the breakout check refuses is in no state. A remote-end pick or a remote-end create also comes back to the same filters and page.

## IP Addresses

Available for Devices and Virtual Machines. This tab creates IP addresses and assigns them to matching interfaces. It can also create a missing interface before assigning an address.

An [IP Mapping](../configuration/mappings.md#ip-mappings) keeps the addresses in a prefix out of the sync, for example the IPv6 link-local prefix `fe80::/10`. Such a row shows **Ignored** and cannot be synced.

**Set Primary IP** sets the NetBox Primary IP when the synchronized address matches the LibreNMS management address. Existing assignments or conflicting addresses require confirmation before they are changed.

When LibreNMS reports the address in a VRF, the row selects the NetBox VRF with the same route distinguisher, or else the same name. If no NetBox VRF has that route distinguisher or name, the row shows **+ VRF**. It creates the VRF with the LibreNMS name and route distinguisher, after a confirmation. It does not sync the address: every row in that VRF then selects the new VRF, and you sync them as usual.

In the [filter](#filters), **Sync state** shows the synced addresses, the addresses to update, or the addresses that NetBox does not have. An ignored address is in no state. **Set Primary IP** still selects the management address when the filter does not show it, and the notice above the table counts it.

## VLANs

Available for Devices. This tab creates VLANs reported by LibreNMS. Each VLAN can use a suitable VLAN group based on NetBox scope, or remain global when no group is selected.

Interface VLAN assignments are handled by the Interfaces tab rather than this tab.

In the [filter](#filters), **Sync state** shows the VLANs that NetBox has with the same name, with another name, or does not have. A VLAN with an invalid or duplicate VID is in no state. On the Interfaces tab, the VLAN modal option that applies the groups to all interfaces saves them for the whole device, on every page and in every filter.

## Modules

Available for Devices. This tab compares LibreNMS physical inventory with NetBox module bays and installed modules. Module matching has additional mapping and inventory rules, so it is covered in the [Module Sync guide](module_sync.md).
