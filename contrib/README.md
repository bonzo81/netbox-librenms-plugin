# Contrib: Example Mapping Files

This directory contains example YAML mapping files for bulk import into the
NetBox LibreNMS Plugin. Each file can be imported via the plugin's bulk import
feature in the NetBox UI.

## How to Import

1. Navigate to the mapping page (e.g., **LibreNMS → Device Type Mappings**)
2. Click the **Import** button (upload icon) in the top right
3. Select **YAML** format
4. Paste the contents of the relevant YAML file
5. Click **Submit**

## Available Mappings

| File | Description |
|------|-------------|
| `interface_type_mappings.yaml` | Maps LibreNMS interface types + speeds to NetBox interface types |
| `device_type_mappings.yaml` | Maps LibreNMS hardware strings to NetBox device types |
| `module_type_mappings.yaml` | Maps LibreNMS inventory model names to NetBox module types (incl. transceivers) |
| `module_bay_mappings.yaml` | Maps LibreNMS inventory container names to NetBox module bay names |
| `normalization_rules.yaml` | Regex-based string normalization applied before module type/bay lookups |
| `inventory_ignore_rules.yaml` | Suppresses phantom ENTITY-MIB entries (e.g. Cisco IOS-XR IDPROM artefacts) |
| `platform_mappings.yaml` | Maps LibreNMS platform strings to NetBox device platforms |

## Customisation

These files are **examples** — adjust values to match the device types, module
types, and interface types defined in your NetBox instance.  The `netbox_*`
fields must reference objects that already exist in your NetBox.
