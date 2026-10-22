# Sync Tabs

The **LibreNMS Sync** page compares LibreNMS data with the selected NetBox Device or Virtual Machine. Refresh a tab to load current LibreNMS data before applying changes.

The Interfaces and Modules tables always fit the window. When the cells cannot wrap enough, the table hides its least important columns first (for example, LibreNMS ID and Description) and names them above the table. Widen the window to show them again.

Each tab remembers the **Per Page** size that you select, for your user and for that tab only. Until you select a size on a tab, the tab uses your NetBox page size.

## Interfaces

Available for Devices and Virtual Machines. This tab creates or updates interfaces and can synchronize descriptions, status, type, speed, VLAN assignments, MAC addresses, and MTU values.

Interface names can come from different LibreNMS fields. Type mappings control how LibreNMS interface types become NetBox interface types. The tab also handles LAG and parent relationships, NetBox-only interfaces, and Virtual Chassis member assignment.

The filter above the table searches the whole device, not only the visible page. **Search** finds the rows where any column shows the text, **Sync state** shows the matching, mismatched or not-in-NetBox rows, and **Columns** opens one field per column. All of them must match. The page and the row count follow the filtered rows; the counts of ignored and NetBox-only interfaces still describe the whole device. The filters are part of the page URL, so paging, sorting, refreshing and syncing keep them. A refresh or a sync also keeps the page, or shows the last page when the page no longer exists. A row that changes after the page loads, for example when you select another Virtual Chassis member, is marked **Changed since filtering**: select **Apply** to filter again. A row selection belongs to one filter, so a new filter starts with no rows selected. When you change the interface name field, the page loads again at the first page with the same filters, and changes that you did not submit are lost.

A row cannot sync when its name is already used by a NetBox interface that is bound to a different LibreNMS port. The row shows that port. If the port is another row in the table, sync that row first, then sync this row. If LibreNMS no longer reports the port (for example, after the device was rediscovered), the row shows **Rebind**. Rebind moves only the LibreNMS binding of that interface to the row's port. The interface keeps its name, IP addresses, and cables. Rebind is not offered while the OOB controller data is incomplete.

## Cables

Available for Devices. This tab uses LibreNMS link data to create NetBox cable connections. Both devices and their interfaces should exist in NetBox first, so synchronize interfaces before cables.

The `librenms_id` custom field on interfaces improves matching when interface names differ between NetBox and LibreNMS.

## IP Addresses

Available for Devices and Virtual Machines. This tab creates IP addresses and assigns them to matching interfaces. It can also create a missing interface before assigning an address.

An [IP Mapping](../configuration/mappings.md#ip-mappings) keeps the addresses in a prefix out of the sync, for example the IPv6 link-local prefix `fe80::/10`. Such a row shows **Ignored** and cannot be synced.

**Set Primary IP** sets the NetBox Primary IP when the synchronized address matches the LibreNMS management address. Existing assignments or conflicting addresses require confirmation before they are changed.

When LibreNMS reports the address in a VRF, the row selects the NetBox VRF with the same route distinguisher, or else the same name. If no NetBox VRF has that route distinguisher or name, the row shows **+ VRF**. It creates the VRF with the LibreNMS name and route distinguisher, after a confirmation. It does not sync the address: every row in that VRF then selects the new VRF, and you sync them as usual.

The filter above the table works as on the Interfaces tab: it searches every address of the device, and a sync, a VRF create or a confirmation comes back to the same filters and page. **Sync state** shows the synced addresses, the addresses to update, or the addresses that NetBox does not have; an ignored address is in no state. **Set Primary IP** still selects the management address when the filter does not show it, and the notice above the table counts it. A row whose VRF you change is marked **Changed since filtering**.

## VLANs

Available for Devices. This tab creates VLANs reported by LibreNMS. Each VLAN can use a suitable VLAN group based on NetBox scope, or remain global when no group is selected.

Interface VLAN assignments are handled by the Interfaces tab rather than this tab.

The filter above the table works as on the Interfaces tab and searches every VLAN of the device. **Sync state** shows the VLANs that NetBox has with the same name, with another name, or does not have; a VLAN with an invalid or duplicate VID is in no state. A row whose VLAN group you change is marked **Changed since filtering**. On the Interfaces tab, the VLAN modal option that applies the groups to all interfaces saves them for the whole device, on every page and in every filter.

## Modules

Available for Devices. This tab compares LibreNMS physical inventory with NetBox module bays and installed modules. Module matching has additional mapping and inventory rules, so it is covered in the [Module Sync guide](module_sync.md).
