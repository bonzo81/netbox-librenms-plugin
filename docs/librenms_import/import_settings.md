# Import Settings

Configure how devices are named and what data is imported from LibreNMS to NetBox.

## Setting Defaults

To configure global defaults for all imports:

1. Navigate to **Plugins → LibreNMS Plugin → Settings**
2. Click **Plugin Settings**
3. Configure Use sysName and Strip Domain to your preferred defaults
4. Save changes

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

The plugin provides two settings that control how device names are created in NetBox. Both are configured in Plugin Settings under **Plugins → LibreNMS Plugin → Settings → Plugin Settings** and can be overridden on the LibreNMS import page.

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

Configure it under **Plugins → LibreNMS Plugin → Settings → Plugin Settings**:

- **Location Parse Pattern** — describes the structure of your location string
- **Use regex** — treat the pattern as a raw regular expression instead of placeholders

Leave the pattern blank to keep the original behaviour: the whole location string is matched against the site (and location) name.

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

The pattern must contain at least one placeholder and use balanced braces. Separators are matched exactly — letters in a separator are case-sensitive, but commas and whitespace are unaffected.

### Regex Mode

Enable **Use regex** to supply a raw regular expression with named groups instead of placeholders. Group names must be one of the supported tokens (`region`, `site`, `location`, `rack`, `tenant`).

```
Pattern (regex): (?P<site>[^-]+)-(?P<rack>.+)
LibreNMS location: NYC-R12
  → site = "NYC", rack = "R12"
```

### How Parsed Tokens Are Matched

Each parsed token is resolved to a NetBox object during import. Matching is **case-insensitive**:

| Token | Matched against | Scope |
|-------|-----------------|-------|
| `site` | Site name, then a [Location Mapping](../usage_tips/mapping_rules.md#location-mappings) | Global |
| `location` | Location name within the matched site, then a Location Mapping | Scoped to the site |
| `rack` | Rack name within the matched site, then a Location Mapping | Scoped to the site |
| `tenant` | Tenant name, then a Location Mapping | Global |

When a token does not match a NetBox object's name exactly, add a [Location Mapping](../usage_tips/mapping_rules.md#location-mappings) to alias the LibreNMS value to a specific NetBox object.

**Notes:**

- A **rack** parsed from the location is only applied when you have not selected a rack manually in the validation details.
- `{region}` is accepted in the pattern so you can consume a region segment when splitting the string, but it is not assigned to the device during import and has no Location Mapping — in NetBox the region is inherited from the site.
- If a pattern is set but a particular location string does not match it, the plugin falls back to using the whole string for the site (and location), so unmatched devices still resolve a site.
