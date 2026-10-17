# Import Settings

Configure how devices are named and what data is imported from LibreNMS to NetBox.

## Setting Defaults

To configure global defaults for all imports:

1. Navigate to **LibreNMS → Settings → Plugin Settings**
2. Configure Use sysName and Strip Domain to your preferred defaults
3. Save changes

These defaults apply to all future imports unless overridden during the import process.

## User Preferences and Defaults

The plugin uses a two-tier preference system for the **Use sysName** and **Strip Domain** toggles:

1. **Plugin defaults** (set by admins on the Settings page) apply to all users who have not yet changed their own toggle settings.
2. **Per-user preferences** are saved automatically when a user changes a toggle on the import page. Once saved, the user's preference takes priority over the plugin default.

**Important notes:**

- Changing the plugin defaults does **not** override existing user preferences. Users who have previously changed a toggle keep their personal setting.
- When an admin saves import settings, only the admin's own preferences are updated to match the new defaults. Other users are unaffected.
- There is no "reset to defaults" for individual users. To revert to the plugin default, a user simply needs to toggle the setting to match.

## Device Naming Options

The plugin provides two settings that control how device names are created in NetBox. Both are configured in Plugin Settings under **LibreNMS → Settings → Plugin Settings** and can be overridden on the LibreNMS import page.

### Use sysName

Controls which field from LibreNMS becomes the device name in NetBox.

- **Enabled** (default): Uses the SNMP sysName, falling back to LibreNMS hostname if sysName is not available
- **Disabled**: Uses the LibreNMS hostname field

### Strip Domain

Removes domain suffixes from device names to create shorter, cleaner names.

- **Enabled**: Removes domain suffixes (e.g., "router.example.com" becomes "router"). IP addresses are preserved without modification
- **Disabled**: Keeps the full name as-is

### Naming Examples

```
LibreNMS sysName: router-core-01.example.com
LibreNMS hostname: 10.0.0.1

Use sysName + Strip domain → "router-core-01"
Use sysName + Keep domain → "router-core-01.example.com"
Use hostname + Strip domain → "10.0.0.1" (IP preserved)
Use hostname + Keep domain → "10.0.0.1"
```

If neither sysName nor hostname exists, the plugin generates a name as `device-{librenms_id}`.



## Per-Import Overrides

On the import page, the **Use sysName** and **Strip Domain** toggles are pre-populated from your saved preference (or the plugin default if you haven't set one). Changing a toggle immediately saves your preference for next time and applies to the current import.

This allows you to:

- Import some devices with sysName and others with hostname
- Apply domain stripping selectively based on device type or location
- Test different naming conventions — your last choice is remembered automatically

## Location Parsing

LibreNMS stores a device's location as a single free-text string (the SNMP `sysLocation`, e.g. `NYC, Suite 400, R12`). The **Location Parse Pattern** tells the plugin how to split that string into separate NetBox fields — **site**, **location**, **rack**, and **tenant** — during device import.

Configure it under **LibreNMS → Settings → Plugin Settings**:

- **Location Parse Pattern** — describes the structure of your location string
- **Use regex** — treat the pattern as a raw regular expression instead of placeholders

Leave the pattern blank to match the whole location string against the site (and location) name.

### Placeholder Mode (default)

Write the pattern using placeholders for the parts you want to extract. Any literal text between placeholders (commas, dashes, spaces) is treated as a separator.

Available placeholders: `{region}`, `{site}`, `{location}`, `{rack}`, `{tenant}`

```
Pattern: {site}, {location}, {rack}
LibreNMS location: NYC, Suite 400, R12
  → site = "NYC", location = "Suite 400", rack = "R12"

Pattern: {site} - {rack}
LibreNMS location: NYC - R12
  → site = "NYC", rack = "R12"
```

The pattern must contain at least one placeholder and use balanced braces. Separators, including their spaces and punctuation, are matched literally. For example, `{site} - {rack}` does not match `NYC-R12`.

### Regex Mode

Enable **Use regex** to supply a raw regular expression with named groups instead of placeholders. Group names must be one of the supported tokens (`region`, `site`, `location`, `rack`, `tenant`).

```
Pattern (regex): (?P<site>[^-]+)-(?P<rack>.+)
LibreNMS location: NYC-R12
  → site = "NYC", rack = "R12"
```

Regex mode searches within the LibreNMS location value. Add `^` and `$` when the entire value must follow the pattern.

### How Parsed Tokens Are Matched

Each parsed token is resolved to a NetBox object during import. Matching is **case-insensitive**:

| Token | Matched against | Scope |
|-------|-----------------|-------|
| `site` | Site name, then a [Location Mapping](mappings.md#location-mappings) | Global |
| `location` | Location name or ancestor name within the matched site, then a Location Mapping | Scoped to the site |
| `rack` | Rack name within the matched site, then a Location Mapping | Scoped to the site |
| `tenant` | Tenant name, then a Location Mapping | Global |

When a token does not match a NetBox object's name exactly, add a [Location Mapping](mappings.md#location-mappings) to alias the LibreNMS value to a specific NetBox object. Create the target NetBox Site, Location, Rack, or Tenant before creating its mapping.

**Notes:**

- A **rack** parsed from the location is only applied when you have not selected a rack manually in the validation details.
- `{region}` is accepted in the pattern so you can consume a region segment when splitting the string, but it is not assigned to the device during import and has no Location Mapping. In NetBox, the device inherits its region from its site.
- If a pattern is set but a particular location string does not match it, the plugin falls back to using the whole string for the site (and location), so unmatched devices still resolve a site.

## Platform Version Tracking

**Track OS versions as platforms** is off by default. Turn it on in **LibreNMS → Settings → Plugin Settings**. The setting applies to devices and virtual machines on every LibreNMS server.

When it is off, a device is in sync when its platform is the **OS platform** (the platform that matches the LibreNMS OS by name or through a [Platform Mapping](mappings.md#platform-mappings)) or any platform nested under it. The sync page does not offer to move a device from a nested platform back to the OS platform.

When it is on, each device or virtual machine gets a **version platform**: a child of the OS platform for the exact version string that LibreNMS reports.

- An existing child of the OS platform is the version platform when its name is the version, or ends with a space and the version (case-insensitive). For example, `Cisco IOS-XR 24.4.2` under `iosxr` is the version platform for `24.4.2`. Hand-made platforms and a renamed OS platform are therefore kept.
- A missing version platform is created as `<OS platform name> <version>`, for example `Juniper Junos 21.2R3-S4.8`. It has the manufacturer of the OS platform. Its slug turns dots into dashes, so `21.2.1` and `2.12.1` get different slugs. When the slug is already used by a platform of the same manufacturer (for example `6.1.0` and `6.1.0+`), the plugin adds `-2`, `-3`, and so on.
- When LibreNMS reports no version, the OS platform or any platform nested under it is in sync.
- Platform Mappings still point at the OS platform.
- The plugin never moves, renames, or deletes your platforms, and it does not delete version platforms that no device uses.

The sync page and import block the change, with the reason, when:

- two children of the OS platform match the version, or
- another platform outside the OS platform already has the name of the new version platform.

Creating a version platform needs the NetBox `add` permission on Platform. On the sync page, a user without it sees the reason instead of the **Sync** button. Import then assigns the OS platform and shows the missing version platform as a warning.

The sync page of a device and of a virtual machine both show the platform row. **Create & Sync** reads the current OS and version from LibreNMS, never from the page. With tracking on, if the version differs from the version on the page, or if a platform or Platform Mapping now matches the OS, nothing changes and the page asks you to refresh. A Platform Mapping that cannot be added after the platform is assigned is reported as a warning; the assignment stays.

The **Create Platform** modal on the import page follows the same rules. A platform with the name you enter is used as it is, when you may view it. When the assignment to the matched device or virtual machine is refused, the new platform is not kept either. The Platform Mapping uses the OS that LibreNMS reports for the device. Without a matched object, the modal creates only the OS platform; the import creates the version platform.

Messages and reasons name only platforms that you may view. Another platform is shown as "a platform you cannot view".

Members of a virtual chassis keep the platform of the chassis master.
