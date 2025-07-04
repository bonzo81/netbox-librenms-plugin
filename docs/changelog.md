# Changelog

## 0.3.13 (2025-06=27)

### New Feature
* Add support for IPv6 handling in IP address synchronization

### Documentation
* Add basic development documentation

## 0.3.12 (2025-04-11)

### Improvements
* Add VRF selection support to IP address table and sync
* Implement single IP address verification and VRF assignment
* Extend single IP verification to support Virtual Machines

### Under the hood
* Refactor cable and IP address synchronization methods for improved transaction handling
* Refactor IP address enrichment for improved performance

## 0.3.11 (2025-03-31)

### Improvements
* Enhance remote port enrichment for virtual chassis devices

## 0.3.10 (2025-03-17)

### Fixes
* Fix URL error when no interfaces are selected during sync
* Add hidden SNMP version field to forms and update sync logic

## 0.3.9 (2025-03-14)

### Fixes
* Fix missing add_device_modal.html template and form handling
* Fix missing interfacetypemapping template


## 0.3.8 (2025-03-06)

### Fixes
* Fix cable table error when more than one remote device returned 
* Fix cable table checkboxes controls for virtual chassis devices

### Improvements
* Add slug check to Site and Location Sync 


## 0.3.7 (2025-01-22)

### Fixes
* Fix issue with empty queryset to stop fielderror
### Improvements
* Enhance filtering options for devices and virtual machines
### Under the hood
* Review and refactor docstrings across all files


## 0.3.6 (2025-01-21)

###  NOTE
***Netbox v4.2+ required for this release***

### New Feature
* New dedicated plugin menu item 
* Add device and VM status pages

### Fixes
* Add description to interface mapping page  

### Under the hood
* Update to use new Mac Address object for Netbox v4.2  

## 0.3.5 (2025-01-13)

### Fixes

* Fix IP Address table not displaying for Virutal Machines

## 0.3.4 (2025-01-08)

### Fixes
* Fix VM Interface table not dispalying

## 0.3.3 (2025-01-03)

### New Feature
* Add IP address synchronization

### Fixes
* Refactor librenms_id handling in SyncInterfacesView

### Under the hood
* Refactor table.py into separate modules for better maintainability
* Enhance interface data retrieval efficiency


## 0.3.2 (2024-12-16)

### Fixes
* Refactor tab handling for interface and cable views 
* Fix Duplicate ID in SNMP forms 
* Refactor cable link processing and fix CSRF token error. 
* Generate unique base ID for TomSelect components in VCInterfaceTable
* Add countdown interval variable to initializeCountdown function
## 0.3.1 (2024-12-13)

### Fixes
* Fix issue with tab selection not working after sync task
* Updated interface name field tooltip

## 0.3.0 (2024-12-13)

### New Setting
* Add `interface_name_feild` optional setting to allow choice of interface name field used when syncing interface data.
* Add `interface_name_field` override in GUI for per device control and flexibility.

### Improvements
* Add `librenms_id` to interface sync table and data sync
* Use of `librenms_id` custom field on interface lookup for improved matching in the cables table.
* Add Pagination support to the cables table.


### Fixes
* Fix issue with case sensitive hostname matching

### Under the hood
* Refactor views into seperate modules for better maintainability


## 0.2.9 (2024-11-30)

## Fix pypi release
Add static include in MANIFEST.in for pypi release

## 0.2.8 (2024-11-29)
### Use of Custom Field
This release introduces the option of using a custom field `librenms_id` (integer) to device and virtual machine objects in NetBox. The plugin will work without it but it is recommended for LibreNMS API lookups especially if no primary IP or FQDN available. 

**Note: New static javascript file requires running collectstatic after update**

```
(venv) $ python manage.py collectstatic --no-input
```


### New Features
* Add device to LibreNMS using SNMPv3
* Create cable connection from LIbreNMS links data31
* Plugin can now use primary IP, hostname or Primary IP DNS Name to identify device in LibreNMS 
* Exclude specific columns when syncing data 
* Filter interface and cable tables 
* Bulk edit Virtual Chassis members



### Improvements
* Add pagination to SiteLocationSyncTable
* Add site location filtering functionality and update template for search 
* Refactor LibreNMSAPI to enhance device ID retrieval logic and include DNS name handling 
* Enhance cable sync with device ID handling and user guidance modal
* Add device mismatch check and user feedback 
* Add check for empty MAC address in format_mac_address function
* Increase API request timeout to 20 seconds 
* Fix dropdown menu size issue on click 


### Under the hood

* Refactor interface enabled status logic 
* Fix handling of data-enabled attribute in interface table
* Improve interface mapping logic for speed matchingpull/24
* Refactor cable context handling and improve data rendering in cable tables
* Refactor Javascript into single file. Add cable sync filters and countdown timer 
* Refactor device addition and enhance SNMP v3 support


## 0.2.7 (2024-11-11)
### What's Changed
* Add new interface table logic to handle virtual chassis member selection 
* Update LibreNMS plugin configuration to allow disabling of SSL verification

### Interface name change
*The LibreNMS Sync interface names now use the ifDescr from Librenms. This displays the full interface name to better align with the device type library convention. e.g GigabitEthernet1/0/1 instead of Gig1/0/1.*

## 0.2.6 (2024-10-25)
### New Feature

* Sync Virtual Machine interfaces

### Bug fix
* Pagination bug where page contents would duplicate now fixed.

### Under the hood
* Refactoring of views into separate files for better maintainability.
* Code formatting improvements
* Remove unused elements

##  0.2.5 (2024-10-21)
Bug fix release:
* Missing commas in LibreNMS api module

## 0.2.4 (2024-10-21)
### Enhancements

* Add mac_address, MTU to interface sync
* Enable select all and shift click on interface sync page rows, and other improvements
* Interface mapping now accounts for speed of interface
    > Update to Interface mapping modal may require recreation of existing mapping. 
* Updated LibreNMS Sync page layout to prepare for new features
### Under the hood
* Refactor all views to be class-based
* Big refactor of device LibreNMS sync views to make way for new features
## 0.2.3 (2024-09-30)

* Fix bug where wrong template is used when editing interface mappings
* Remove unused templates from view

## 0.2.2 (2024-09-27)

* Fix too many arguments to add_device error

## 0.2.1 (2024-09-27)

* Fix LibreNMS hardware variable not found
* Add update_device_field to LibreNMS API
* Add device location Sync button to device plugin tab
* Change SNMP community from 'text' to 'password' for privacy

## 0.2.0 (2024-09-25)

* Update to v0.2.0 of the plugin

## 0.1.1 (2024-09-24)

* First release on PyPI.
