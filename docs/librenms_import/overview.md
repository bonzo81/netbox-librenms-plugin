# Device Import Overview

The Device Import feature allows you to discover and import devices from LibreNMS into NetBox. This streamlines the process of populating NetBox with devices that are already monitored in LibreNMS, while giving you full control over how devices are imported.


The import page should be clear and intuitive to use, but this overview provides additional context and details.

## How It Works

The import workflow consists of three main steps:

1. **[Search & Filter](search.md)** - Find devices in LibreNMS using flexible filter criteria
2. **[Review & Validate](validation.md)** - Validate import readiness and configure missing NetBox objects
3. **[Import](import_settings.md)** - Configure import settings and create devices in NetBox

The plugin validates all required NetBox objects (Site, Device Type, Device Role) before allowing import.

## Key Features

**Flexible Filtering**
: Search by location, type, operating system, hostname, or system name. Combine filters for precise device selection.

**Smart Validation**
: Automatic matching for Sites, Device Types, and Platforms based on LibreNMS data. Clear indicators for what's missing.

**Device or VM**
: Import as physical Devices (requires Site, Device Type, Role) or Virtual Machines (requires Cluster).

**Virtual Chassis Support**
: Automatic detection and creation of Virtual Chassis objects for stackable switches.

**Background Processing**
: Large device sets can be processed using NetBox background jobs with progress tracking and cancellation.

## Accessing the Feature

Navigate to the import interface through the NetBox menu:

**Plugins → LibreNMS Plugin → Import → LibreNMS Import**

This opens the device import page where you can search for and import devices from your LibreNMS instance.

## What Gets Created

When a device is imported, the plugin creates:

**Device or VirtualMachine Object**
: With all validated attributes (name, site, device type, role, platform, serial, rack, etc.). Note that rack assignment places the device in the rack without setting a specific rack unit (U) position—devices appear in the "Non racked" section and require manual U assignment.

**LibreNMS ID Custom Field**
: Automatically set to link the NetBox object to the LibreNMS device. This enables all other plugin features (interface sync, cable sync, etc.)

**Virtual Chassis** (if detected)
: For stackable devices, creates the Virtual Chassis object and assigns member positions based on detected inventory data.

After import, devices appear in NetBox with a comment indicating they were imported by the plugin, including the import timestamp.

## Multi-Server Support

If your NetBox installation is configured with multiple LibreNMS servers, the import feature automatically uses the currently selected server from Plugin Settings.

All imported devices are linked to the server used during import, allowing you to maintain devices from multiple LibreNMS instances in a single NetBox installation.

## Next Steps

Explore each step of the import workflow:

- [Search for Devices](search.md) - Learn about filters, matching rules, and search options
- [Validation & Configuration](validation.md) - Understand validation status and resolve issues
- [Import Settings](import_settings.md) - Configure device naming and import options
- [Background Jobs & Caching](background_jobs.md) - Job processing and performance optimization
