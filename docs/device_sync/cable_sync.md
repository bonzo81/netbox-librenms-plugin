# Cable Sync

The **Cables** tab on a device's LibreNMS sync page builds cable rows from LibreNMS and creates the matching NetBox Cable objects. Rows come from two sources:

- **Network links** — LibreNMS LLDP/CDP neighbour data (`links`). Both the local and the remote interface must resolve to NetBox objects for the row to be syncable.
- **Serial console ports** — console-server serial lines that LibreNMS models as *sensors*, not interfaces (see below). These rows carry a **Serial** badge and sync to `ConsoleServerPort ↔ ConsolePort` cables.

## Serial console ports

Console servers (e.g. Avocent ACS, Cisco IOS async lines) don't expose their serial ports in the SNMP interface table, so they never appear in LibreNMS port listings. LibreNMS instead models them as **state sensors** in the "Serial Ports" group. The plugin maps those sensors to console-port cable rows:

- The **local port** name is generated from the sensor's port number using the pattern configured for that sensor's `sensor_type` (e.g. `ttyS7`, `Line 2`). Matching is on the `sensor_type` value, not on the vendor, so a vendor that reports serial lines under more than one type needs a mapping row for each. The device must have a **ConsoleServerPort with that exact name** for the row to become syncable.
- The **remote side** is resolved from the sensor's label (LibreNMS description with the trailing `Status` suffix stripped, e.g. `PROD-SW01 Status` → `PROD-SW01`): the plugin looks up a NetBox device by that name and auto-picks its first un-cabled ConsolePort.

### Configuring recognized sensor types

Open **LibreNMS → Rules & Patterns**, then select the **Serial Sensor Types** tab. Each entry maps a LibreNMS `sensor_type` to a local port-name pattern. `{N}` is replaced by the port number (the trailing integer of the sensor index, e.g. `tsLineActive.2` → `2`).

Two vendors ship pre-seeded:

| Sensor type | Port name pattern | Vendor |
|---|---|---|
| `acsSerialPortTable` | `ttyS{N}` | Avocent ACS |
| `OLD-CISCO-TS-MIB::ltsLineTable` | `Line {N}` | Cisco IOS async lines |

To surface another serial sensor type, add a row with its `sensor_type` (matching is exact, including case) and the naming pattern you use for the device's ConsoleServerPorts — no code change or restart needed. Deleting a row stops sensors of that type from being recognized; there is no hidden fallback that resurrects the defaults. Entries support bulk YAML import/export and per-object change logging like the other rules.

## Cable provenance

Every cable the sync creates is stamped so the plugin can recognize its own cables later:

| Setting | Default | Purpose |
|---|---|---|
| Provenance tag | `librenms` | Tag added to every created or adopted cable (auto-created on first use). It records provenance and lets sync recognize an already adopted connection. |
| Tag / cable color | `009688` | Color of the auto-created tag and of the cables themselves. |
| Cable description | `Synced from LibreNMS` | Cable description; the acting server key is appended, e.g. `Synced from LibreNMS (production)`. |

These are managed in the UI on the plugin **Settings → Cable Sync** tab (no `PLUGINS_CONFIG` entry, no NetBox restart to change them). Renaming or recoloring the provenance tag updates the same managed Tag, so existing tagged cables show the new tag name and color. The cable color and description apply when a cable is created; existing Cable fields do not change. The cable's **tenant** follows the remote device's tenant; the cable **type** is left blank (LibreNMS doesn't report the physical cable, and NetBox has no serial/rollover type). Serial console sensor types are likewise DB-managed through rows under the Rules & Patterns tables.

## Re-syncing already-cabled ports

A cabled row is not a dead end — its **Cable Status** compares the existing NetBox cabling against what LibreNMS reports, and offers a Sync action whenever there is something useful to do:

| Cable Status | Meaning | Sync action offered |
|---|---|---|
| **No Cable** | Both endpoints free. | Yes — creates the cable. |
| **Cable Found** | The LibreNMS-reported connection is already cabled directly. | Only while the cable is **untagged**: syncing *adopts* it (adds the configured provenance tag; the cable is never recreated). Once tagged, there is nothing to do. |
| **Connected via Patch Path** | The endpoints are cabled and the traced path **reaches the LibreNMS target through patch panels** — you remodeled the link in more detail. | No — a remodel is a better model of the same link, not a mismatch. |
| **Cable Mismatch** | An endpoint is cabled somewhere that does *not* reach the LibreNMS target. | Yes — re-syncing re-points the connection, protected by the overwrite gate below. |

For serial rows the LibreNMS target is the **device** matched from the port label (the exact remote port isn't knowable from a label), so a cable landing on *any* console port of that device counts as matched. A cable that already carries the configured provenance tag is **trusted over the label**: labels are only hints, and a tagged cable was placed deliberately (possibly via a manual pick), so a wrong-name label never flips it to a mismatch.

## Picking the remote end manually

Remote matching is name-based — the serial label or the LLDP port name — and names simply don't always match. Any row whose local end resolved but whose remote is unresolved, free, or pointing at the wrong place shows a **pick-remote** button (<i class="mdi mdi-connection"></i>) next to its Sync action. It opens a picker where you:

1. **Search for the remote device** by name (the label-matched device is pre-filled when there is one).
2. **Pick the port** — console ports for serial rows, interfaces otherwise; free ports are listed first, already-cabled ones are marked (and remain overwrite-protected at sync time).

The pick is stored in a user-scoped cache entry (marked with a small hand icon in the Remote Port column) and makes the row immediately syncable. The shared LibreNMS snapshot does not contain the pick. A full **Refresh Cables** creates a new snapshot and drops unsynced picks, so sync soon after picking.

### Changing an existing cable

The picker is also available on rows that are **already cabled** (including tagged "Cable Found" and "Connected via Patch Path" rows). Picking a different remote re-points the connection. A re-point that would remove an existing cable **always** stops at the warning modal first, including LibreNMS-driven changes and cables with the provenance tag.

## Overwrite protection

Re-running the sync never silently destroys a cable. The configured provenance tag (default `librenms`) identifies cables that the plugin created or adopted, even when other tags are also present. Provenance does not bypass overwrite confirmation.

| Situation on sync | Action |
|---|---|
| Neither endpoint cabled | Create a fresh cable. |
| The desired cable already exists and carries the tag | Nothing to do. |
| The desired cable already exists but is untagged | The configured provenance tag is added (non-destructive, no confirmation needed). |
| A *different* cable occupies either endpoint | **Force confirmation required.** |

When force confirmation is required, the sync leaves the database untouched and pops a warning modal showing, for each conflicting port, the **full cable path for context** — with the segment that would actually be deleted highlighted. Only the cable segment(s) directly attached to the endpoints are ever removed: **patch-panel trunks (e.g. rear-to-rear inter-rack cables) and every other mid-path segment stay in place** — they carry other circuits and are treated as permanent infrastructure. Submitting is blocked until you explicitly tick the force checkbox; forcing deletes the highlighted segment(s) and connects the port with a single LibreNMS-managed cable.

Apart from the breakout channels below, cable sync does not change multi-termination or breakout cables. These cables can carry unrelated lanes, so the table marks them as unsupported and does not offer a sync action.

## Breakout channels (NetBox 4.7 and later)

NetBox 4.7 models a breakout port as a channelized parent interface with one channel interface for each lane. A channel cannot have its own cable. The cable connects to the parent and has a breakout profile, for example `breakout-1c4p-4c1p`, and NetBox shows it on each channel. When the local interface of a row is a channel, cable sync writes that breakout cable on the parent:

- The parent has no cable: sync creates one cable with the breakout profile for the number of channels. The A side is the parent. The B side is the far end of each channel from channel 1 to the channel of the row.
- The parent has a breakout cable with the provenance tag and the same profile, and the cable has a far end for each lower channel: sync adds the far end of the row's channel.
- The cable already connects the channel to the LibreNMS far end: the row shows **Cable Found**.

NetBox numbers the far ends of a breakout cable by their order, so a channel can get its far end only when each lower channel has one. Otherwise the row shows **Breakout Not Synced** with the cause, and sync writes nothing. Other causes are: NetBox has no breakout profile for that number of channels, the cable on the parent does not have the provenance tag or has another profile, or the far end is a channel or already has a cable. When the far end of a plain port is a channel, sync the cable from the device of the channel. Cable sync never removes or changes the far ends of an existing breakout cable, also when you confirm an overwrite.
