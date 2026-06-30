# Contributing LibreNMS data shapes

The plugin's sync logic (Virtual Chassis detection, LAG / sub-interface resolution, …) is
validated by **outcome tests** that replay real LibreNMS API responses through the real client
and assert the result. The maintainer can't generate every vendor/topology without the hardware,
so the project accepts **community-contributed, anonymized data shapes** that become tests
automatically.

A *recording* is one device scenario: the LibreNMS responses captured verbatim plus an
`expected` block describing the outcome. Recordings live in
`netbox_librenms_plugin/data_shapes/recordings/*.json`; `netbox_librenms_plugin/tests/test_recordings.py`
parametrizes over them, so **a new JSON with an `expected` block is a new passing test, no code required**.

## Capturing a shape (contributors)

1. Open a device's **LibreNMS** sync tab in NetBox.
2. Click **Capture data shape** (top of the page, in the *LibreNMS Connections* card).
3. The modal shows:
   - a **novelty verdict** (is this shape likely new, or already covered?),
   - the **anonymized recording JSON** (Copy / Download),
   - a **residual-PII warning** if the safety-net found anything to review,
   - an **Open prefilled issue** link.
4. **Review the JSON.** Then open the issue and paste the JSON into the *Anonymized recording*
   field of the data-shape issue form.

The plugin never submits on your behalf — you stay in control of what leaves your browser.

## What gets anonymized

`netbox_librenms_plugin/data_shapes/anonymize.py` applies field-aware rules so the shape stays
test-useful while losing identifying detail:

- **Preserved verbatim** — the logic-bearing fields the tests read: `ifName`, `ifType`, port
  ids, `entPhysicalClass` / `entPhysicalIndex` / `entPhysicalContainedIn` /
  `entPhysicalParentRelPos`, VLANs, transceiver optics, `os`.
- **Pseudonymized deterministically** — serials, hostnames, model SKUs become `SN-…` /
  `device-…` / `MODEL-…`. The mapping is stable, so cross-references (a device serial that equals
  a stack member's serial — how the master is identified) still match.
- **Scrubbed** — IPs → documentation ranges, MACs → a synthetic `02:00:00` block, lat/lng →
  null, location → `Lab`, free-text (`ifAlias`, `sysContact`, `sysDescr`, …) → empty.

`find_pii()` is a regex safety-net that flags any IP/MAC/email the field rules missed. The capture
modal surfaces its findings; **review them before submitting.**

## Promoting a submission to a recording (maintainers)

1. Save the anonymized JSON to `netbox_librenms_plugin/data_shapes/recordings/<name>.json`.
2. Add an `expected` block describing the outcome to assert, e.g.:

   ```json
   "expected": {
     "virtual_chassis": {"is_stack": true, "member_count": 2, "member_serials": ["SN-…", "SN-…"]},
     "lag_members": {"<member_port_id>": "<aggregate_port_id>"},
     "sub_interfaces": {"<child_port_id>": "<parent_port_id>"}
   }
   ```

   All three keys are optional — include only what the shape exercises.
3. Validate and refresh the novelty manifest:

   ```console
   python manage.py librenms_recordings --validate netbox_librenms_plugin/data_shapes/recordings/<name>.json
   python manage.py librenms_recordings --rebuild-manifest
   ```

4. Run the suite — the new recording is now a test:

   ```console
   pytest netbox_librenms_plugin/tests/test_recordings.py
   ```

## The `librenms_recordings` command

- `--validate PATH` — schema-check a recording, re-run the PII safety-net, and report novelty.
  Exits non-zero on a schema or PII failure (suitable for CI/pre-commit).
- `--rebuild-manifest` — regenerate `netbox_librenms_plugin/data_shapes/recordings/manifest.json` (the
  novelty reference the capture view reads).
- `--list` — print each bundled recording with its computed signature.

## CI guards

The standard test run (`.github/workflows/test.yaml`) covers data shapes: every recording's
schema and outcomes are asserted, every bundled recording is checked to carry no residual PII,
and `manifest.json` is checked to be in sync with the recordings (so it never goes stale).
