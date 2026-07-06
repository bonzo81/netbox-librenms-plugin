# Cable Sync

The **Cables** tab on a device's LibreNMS sync page builds cable rows from LibreNMS and creates the matching NetBox Cable objects. Rows come from two sources:

- **Network links** — LibreNMS LLDP/CDP neighbour data (`links`). Both the local and the remote interface must resolve to NetBox objects for the row to be syncable.
- **Serial console ports** — console-server serial lines that LibreNMS models as *sensors*, not interfaces (see below). These rows carry a **Serial** badge and sync to `ConsoleServerPort ↔ ConsolePort` cables.

## Serial console ports

Console servers (e.g. Avocent ACS, Cisco IOS async lines) don't expose their serial ports in the SNMP interface table, so they never appear in LibreNMS port listings. LibreNMS instead models them as **state sensors** in the "Serial Ports" group. The plugin maps those sensors to console-port cable rows:

- The **local port** name is generated from the sensor's port number using a per-vendor pattern (e.g. `ttyS7`, `Line 2`). The device must have a **ConsoleServerPort with that exact name** for the row to become syncable.
- The **remote side** is resolved from the sensor's label (LibreNMS description with the trailing ` Status` stripped, e.g. `PROD-SW01 Status` → `PROD-SW01`): the plugin looks up a NetBox device by that name and auto-picks its first un-cabled ConsolePort.

### Configuring recognized sensor types

Which sensor types are treated as serial ports — and how the local ports are named — is controlled by the plugin-level `serial_sensor_types` setting: a map of LibreNMS `sensor_type` to a local port-name pattern, where `{N}` is replaced by the port number (the trailing integer of the sensor index, e.g. `tsLineActive.2` → `2`).

```python
PLUGINS_CONFIG = {
    'netbox_librenms_plugin': {
        'servers': {
            # ... per-server settings ...
        },
        # Plugin-level: applies to all servers. This is the default value.
        'serial_sensor_types': {
            'acsSerialPortTable': 'ttyS{N}',   # Avocent ACS
            'ciscoAsyncLine': 'Line {N}',      # Cisco IOS async lines
        },
    }
}
```

To surface another vendor's serial lines, add its `sensor_type` with the naming pattern you use for the device's ConsoleServerPorts — no code change needed.

!!! note "Overriding replaces the defaults"
    When you set `serial_sensor_types` yourself, your value **replaces** the built-in map (defaults apply only while the key is absent). Include the `acsSerialPortTable` / `ciscoAsyncLine` entries in your override if you still want them recognized.

A bare list of sensor types (e.g. `['acsSerialPortTable']`) is also accepted; every type in it then uses the fallback pattern `ttyS{N}`.

## Cable provenance

Every cable the sync creates is stamped so the plugin can recognize its own cables later:

| Setting | Default | Purpose |
|---|---|---|
| `cable_sync_tag` | `librenms` | Tag added to every created cable (auto-created on first use). This is the ownership marker used by overwrite protection. |
| `cable_sync_tag_color` | `009688` | Color of the auto-created tag and of the cables themselves. |
| `cable_sync_description` | `Synced from LibreNMS` | Cable description; the acting server key is appended, e.g. `Synced from LibreNMS (production)`. |

These are plugin-level settings (set beside `servers`, like `serial_sensor_types`). The cable's **tenant** follows the remote device's tenant; the cable **type** is left blank (LibreNMS doesn't report the physical cable, and NetBox has no serial/rollover type).

## Re-syncing already-cabled ports

A cabled row is not a dead end — its **Cable Status** compares the existing NetBox cabling against what LibreNMS reports, and offers a Sync action whenever there is something useful to do:

| Cable Status | Meaning | Sync action offered |
|---|---|---|
| **No Cable** | Both endpoints free. | Yes — creates the cable. |
| **Cable Found** | The LibreNMS-reported connection is already cabled directly. | Only while the cable is **untagged**: syncing *adopts* it (adds the `librenms` tag; the cable is never recreated). Once tagged, there is nothing to do. |
| **Connected via Patch Path** | The endpoints are cabled and the traced path **reaches the LibreNMS target through patch panels** — you remodeled the link in more detail. | No — a remodel is a better model of the same link, not a mismatch. |
| **Cable Mismatch** | An endpoint is cabled somewhere that does *not* reach the LibreNMS target. | Yes — re-syncing re-points the connection, protected by the overwrite gate below. |

For serial rows the LibreNMS target is the **device** matched from the port label (the exact remote port isn't knowable from a label), so a cable landing on *any* console port of that device counts as matched. A cable that already carries the `librenms` tag is **trusted over the label**: labels are only hints, and a tagged cable was placed deliberately (possibly via a manual pick), so a wrong-name label never flips it to a mismatch.

## Picking the remote end manually

Remote matching is name-based — the serial label or the LLDP port name — and names simply don't always match. Any row whose local end resolved but whose remote is unresolved, free, or pointing at the wrong place shows a **pick-remote** button (<i class="mdi mdi-connection"></i>) next to its Sync action. It opens a picker where you:

1. **Search for the remote device** by name (the label-matched device is pre-filled when there is one).
2. **Pick the port** — console ports for serial rows, interfaces otherwise; free ports are listed first, already-cabled ones are marked (and remain overwrite-protected at sync time).

The pick is stored on the cached row (marked with a small hand icon in the Remote Port column) and makes it immediately syncable — the sync creates the cable to *your* pick, with the usual provenance stamp. Picks live as long as the cached snapshot: a full **Refresh Cables** rebuilds the rows from LibreNMS and drops unsynced picks, so sync soon after picking.

### Changing an existing cable

The picker is also available on rows that are **already cabled** (including tagged "Cable Found" and "Connected via Patch Path" rows) — picking a different remote re-points the connection. A manual re-point that would remove an existing cable **always** stops at the warning modal first, showing the full path that would be deleted — even when the cable is plugin-owned. (The silent overwrite of plugin-owned cables applies only to LibreNMS-driven re-points, where the refresh data itself moved.)

## Overwrite protection

Re-running the sync never silently destroys a cable the plugin does not solely own. A cable counts as **plugin-owned** only when its tags are *exactly* `{librenms}` — a foreign tag, an extra tag, or no tags at all mean someone else has a stake in it.

| Situation on sync | Action |
|---|---|
| Neither endpoint cabled | Create a fresh cable. |
| The desired cable already exists and carries the tag | Nothing to do. |
| The desired cable already exists but is untagged | The `librenms` tag is added (non-destructive, no confirmation needed). |
| A *different* cable occupies an endpoint and every such cable is plugin-owned | Replaced silently. |
| A *different* cable occupies an endpoint and any such cable is **not** plugin-owned | **Force confirmation required.** |

When force confirmation is required, the sync leaves the database untouched and pops a warning modal listing, for each conflicting port, the **full cable path that would be deleted** — including any patch-panel segments through to the far-end device. Submitting is blocked until you explicitly tick the force checkbox; forcing deletes the listed path(s) and replaces them with a single LibreNMS-managed cable.
