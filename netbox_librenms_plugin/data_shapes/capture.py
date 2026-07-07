"""
Capture a device's LibreNMS API responses verbatim into a data-shape recording.

The capture issues the same structural requests the sync logic reads — device info, the two
Virtual-Chassis detection inventory calls, ports (with VLAN data), and port_stack — and
assembles them into a recording dict that
:meth:`netbox_librenms_plugin.tests.mock_librenms_server.MockLibreNMSServer.load_recording`
can replay. See ``data_shapes/recordings/`` and issue #95.
"""

SCHEMA_VERSION = 1

# Columns get_ports() requests; mirror them so a captured ports payload carries the same fields
# the sync / relationship-resolution logic reads (port_id, ifName, ifType, ...).
_PORTS_COLUMNS = "port_id,ifName,ifType,ifSpeed,ifAdminStatus,ifDescr,ifAlias,ifPhysAddress,ifMtu,ifVlan,ifTrunk"


def _select_parent_index(root_items):
    """
    Pick the VC parent-container index from root inventory (stack preferred over chassis).

    Mirrors ``detect_virtual_chassis_from_inventory`` so the captured child-inventory query
    targets the same parent index VC detection requests on replay.

    Args:
        root_items: The ``inventory`` list from the ``entPhysicalContainedIn=0`` response.

    Returns:
        The chosen ``entPhysicalIndex`` (stack class wins over chassis), or None when neither
        a stack nor a chassis root entry is present.
    """
    stack_index = None
    chassis_index = None
    for item in root_items if isinstance(root_items, list) else []:
        if not isinstance(item, dict):
            continue
        item_class = item.get("entPhysicalClass")
        if item_class == "stack" and stack_index is None:
            stack_index = item.get("entPhysicalIndex")
        elif item_class == "chassis" and chassis_index is None:
            chassis_index = item.get("entPhysicalIndex")
    return stack_index if stack_index is not None else chassis_index


def capture_device_recording(api, device_id, *, name=None, description="", meta=None, oob_id=None):
    """
    Capture a device's structural LibreNMS responses into a recording dict.

    Args:
        api: A ``LibreNMSAPI`` instance pointed at the source server.
        device_id: LibreNMS device id to capture.
        name (str | None): Recording name; defaults to ``"device-<id>"``.
        description (str): Human-readable description of the scenario.
        meta (dict | None): Extra metadata to merge; ``os`` is auto-filled from device info.
        oob_id (int | None): LibreNMS id of a linked out-of-band controller. When set, its ports are
            recorded too (the interfaces view merges them into the host as ``_source="oob"`` rows and
            runs shared-LOM detection), and ``meta["oob_id"]`` is stored so replay can fetch them.

    Returns:
        dict: A recording with ``schema_version``, ``name``, ``description``, ``meta``,
            ``device_id``, and a ``responses`` map keyed by request string. Each response is the
            JSON body verbatim for a 2xx, or ``[status, body]`` otherwise so non-OK responses
            replay faithfully.
    """
    responses = {}

    def _route_key(path, key_params):
        key = f"GET /api/v0/{path}"
        if key_params:
            key += "?" + "&".join(f"{k}={v}" for k, v in key_params.items())
        return key

    def record(path, request_params=None, *, key_params="__same__", required=False):
        """Issue one raw GET and store it under its route key (key_params=None → path-only)."""
        status, body = api._raw_get(path, request_params)
        if key_params == "__same__":
            key_params = request_params
        # A transport error yields status 0 (no HTTP response was received). Recording it as
        # [0, body] would bake an un-replayable route into the recording — the mock replay server
        # calls send_response(0), an out-of-range HTTP status — so skip any route that never produced
        # a real HTTP status. A genuine non-2xx (e.g. 404) IS a real status and is still recorded.
        if not (100 <= status < 600):
            # A required structural route that never answered means the capture is incomplete:
            # persisting it would ship a partial fixture (missing device/ports/port_stack) as if
            # capture succeeded. Fail loudly instead. (Optional routes like transceivers may be
            # legitimately absent/timed out, so they are not required.)
            if required:
                raise RuntimeError(f"Capture failed for {path!r}: no HTTP response (status {status})")
            return status, body
        # A real error status on a REQUIRED structural route is just as fatal: a stale
        # librenms_id pointing at a device deleted from LibreNMS answers 404 on every route,
        # and recording those [404, error-body] pairs would present a junk recording (all-false
        # signature, "likely a new shape") as a successful capture inviting submission.
        if required and not (200 <= status < 300):
            raise RuntimeError(f"Capture failed for {path!r}: HTTP {status} (is the LibreNMS device id stale?)")
        responses[_route_key(path, key_params)] = body if 200 <= status < 300 else [status, body]
        return status, body

    _all_inventory = []  # memoized /inventory/{id}/all body, fetched lazily at most once

    def record_inventory_filtered(query_params, *, contained_in=None, ent_class=None):
        """
        Record a filtered inventory query, mirroring ``get_inventory_filtered``'s ``/all`` fallback.

        A LibreNMS server that does not honor the ``entPhysical*`` query params returns an empty
        filtered inventory but still populates ``/inventory/{id}/all``; production's
        ``get_inventory_filtered`` falls back to that endpoint + client-side filtering. Reproduce the
        SAME entities under the FILTERED key here — which both ``compute_shape_signature`` and the
        production replay read — otherwise a Virtual-Chassis device is silently captured as a plain
        one. When the filtered query already returns data (the common case), nothing changes.
        """
        filtered_status, body = record(f"inventory/{device_id}", query_params)
        # record() skips storing the route on a transport failure (status outside 100–599), so track
        # whether the filtered key was actually persisted. If it was NOT, we must synthesize it below
        # even when the client-side filter yields [], otherwise replay 404s on this structural route.
        filtered_route_recorded = 100 <= filtered_status < 600
        items = body.get("inventory") if isinstance(body, dict) else None
        if items:
            return items
        if not _all_inventory:
            all_status, all_body = api._raw_get(f"inventory/{device_id}/all")
            # This /all fetch bypasses record(), so a TRANSPORT failure (no HTTP response, status 0)
            # would silently degrade to an empty inventory and still ship a "successful" capture —
            # recording a VC device with the wrong topology/signature. Once the filtered query came
            # back empty, /all is the only inventory source, so a no-response failure is fatal here,
            # mirroring record(required=True). A real HTTP answer (incl. 404 / 2xx-empty) is a
            # definitive "no inventory" for a plain device and stays an empty list.
            if not (100 <= all_status < 600):
                raise RuntimeError(
                    f"Capture failed for inventory/{device_id}/all: no HTTP response (status {all_status})"
                )
            all_items = all_body.get("inventory") if isinstance(all_body, dict) else None
            _all_inventory.append(all_items if isinstance(all_items, list) else [])
        filtered = [i for i in _all_inventory[0] if isinstance(i, dict)]
        if ent_class is not None:
            filtered = [i for i in filtered if i.get("entPhysicalClass") == ent_class]
        if contained_in is not None:
            filtered = [i for i in filtered if str(i.get("entPhysicalContainedIn")) == str(contained_in)]
        if filtered or not filtered_route_recorded:
            # Overwrite the (empty or never-stored) filtered response with the client-filtered
            # entities so the recording looks as if captured from a filter-honoring server (faithful
            # signature + replay), and so the structural route always exists for replay even when the
            # /all fallback filters down to [].
            responses[_route_key(f"inventory/{device_id}", query_params)] = {"status": "ok", "inventory": filtered}
        return filtered

    # 1. Device info (also the source of the os metadata).
    _, dev_body = record(f"devices/{device_id}", required=True)
    device_os = None
    if isinstance(dev_body, dict):
        devices = dev_body.get("devices")
        if isinstance(devices, list) and devices and isinstance(devices[0], dict):
            device_os = devices[0].get("os")

    # 2. VC detection inventory: root, then the chassis children at the resolved parent index. Both
    #    query variants share a path, so they MUST be keyed with their query for the loader to
    #    disambiguate them; each mirrors get_inventory_filtered's /all fallback (see helper).
    root_items = record_inventory_filtered({"entPhysicalContainedIn": "0"}, contained_in="0")
    parent_index = _select_parent_index(root_items)
    if parent_index is not None:
        record_inventory_filtered(
            {"entPhysicalClass": "chassis", "entPhysicalContainedIn": str(parent_index)},
            contained_in=str(parent_index),
            ent_class="chassis",
        )

    # 3. Ports (request VLAN data so the body matches what get_ports reads) and 4. port_stack
    #    (LAG / sub-interface relationships). Both are keyed path-only — there's a single variant
    #    per path, so the loader serves it for any query the production readers send.
    record(f"devices/{device_id}/ports", {"columns": _PORTS_COLUMNS, "with": "vlans"}, key_params=None, required=True)
    record(f"devices/{device_id}/port_stack", required=True)

    # 5. Transceivers (optics shape). Per-device route — safe to record verbatim; anonymization
    #    pseudonymizes the transceiver serial and preserves the optics shape plus the `model` SKU
    #    (the module-matching key for ModuleType resolution).
    record(f"devices/{device_id}/transceivers")

    # 6. Serial-port sensors (Avocent console servers). The underlying LibreNMS route,
    #    /api/v0/resources/sensors, is INSTANCE-WIDE — it returns every sensor on every device, so
    #    recording it verbatim would embed other devices' data (cross-device PII). Fetch through the
    #    device-filtered accessor and synthesize a sensors body carrying ONLY this device's serial
    #    sensors, which is exactly what replay's get_serial_port_sensors pipeline reads back. Recorded
    #    only when the device actually has serial sensors (most don't), so the instance-wide route is
    #    never added to unrelated recordings.
    if hasattr(api, "get_serial_port_sensors"):
        # Bypass the per-server sensor cache: a capture records the CURRENT LibreNMS shape, so a
        # subset cached by an earlier refresh would embed stale sensors into the recording.
        ss_ok, device_serial_sensors = api.get_serial_port_sensors(device_id, use_cache=False)
        if ss_ok and device_serial_sensors:
            responses["GET /api/v0/resources/sensors"] = {"status": "ok", "sensors": device_serial_sensors}

    # 7. OOB controller ports — a SEPARATE LibreNMS device the interfaces view merges into the host.
    #    Record them under the controller's own /ports route so replay's get_ports(oob_id) serves them.
    # Spread caller meta first, then stamp the captured device_os last so it always wins — a
    # caller-supplied meta["os"] must not override the OS we actually captured (it scopes
    # signature/novelty behavior). Drop any caller-supplied oob_id: it's the authoritative OOB
    # signal (signature reads meta["oob_id"]), so it must be set ONLY below when controller ports
    # are actually recorded — never spoofed in by a caller without an OOB capture.
    meta_out = {k: v for k, v in (meta or {}).items() if k != "oob_id"}
    meta_out["os"] = device_os
    # Skip a (misconfigured) OOB controller whose LibreNMS id equals the host's own id: its ports
    # route devices/<oob_id>/ports is the SAME key as the host's, so recording it would overwrite the
    # host's ports — and a device is not its own out-of-band controller. Compare COERCED ids: the
    # custom field can store the OOB id as a string ("123"), and "123" != 123 would bypass this
    # guard, re-record the host's own ports and falsely stamp the recording as an OOB topology.
    from netbox_librenms_plugin.utils import coerce_librenms_id

    oob_id = coerce_librenms_id(oob_id)
    if oob_id is not None and oob_id != coerce_librenms_id(device_id):
        oob_status, _ = record(f"devices/{oob_id}/ports", {"columns": _PORTS_COLUMNS, "with": "vlans"}, key_params=None)
        # Only mark the recording OOB once the controller ports were actually captured: record()
        # silently skips a route that produced no HTTP response (transport failure), so stamping
        # oob_id unconditionally would label the recording OOB with no controller ports behind it —
        # and signature reads meta["oob_id"] as the authoritative OOB signal.
        if 200 <= oob_status < 300:
            meta_out["oob_id"] = oob_id

    # 8. LAG name patterns. compute_shape_signature and the replay's
    #    resolve_port_relationships(lag_patterns=recording["lag_patterns"]) both read this key —
    #    without it a LAG detected only via a configured PortStackLagPattern regex (ifType not
    #    ieee8023adLag) fingerprints as lag.present=False and the fixture can never reproduce the
    #    pattern-based behavior it was captured for. Mirrors compiled_patterns_for_os exactly:
    #    None (payload carried no OS at all) embeds every stored pattern (legacy unscoped);
    #    a present-but-blank/non-string OS embeds NONE — production matches nothing for such a
    #    device, and signature.py compiles the recorded patterns unscoped, so embedding them all
    #    would fingerprint pattern-LAG behavior production never applies.
    from netbox_librenms_plugin.models import PortStackLagPattern

    pattern_qs = PortStackLagPattern.objects.all()
    if device_os is not None:
        os_filter = device_os.strip() if isinstance(device_os, str) else ""
        pattern_qs = pattern_qs.filter(librenms_os__iexact=os_filter) if os_filter else pattern_qs.none()
    lag_patterns = {row.librenms_os: row.lag_name_pattern for row in pattern_qs}

    # 9. Serial sensor recognition map. Same fidelity argument as lag_patterns: recognition
    #    lives in the SerialSensorTypePattern table, so a recording captured under a custom map
    #    could not reproduce its serial rows on a host with different (or no) rows. Replay
    #    feeds this through the sensor_types injection points (get_serial_port_sensors /
    #    map_sensors_to_serial_links). Global, not OS-scoped — sensor types identify vendor
    #    sensor tables, not the captured device's OS.
    from netbox_librenms_plugin.serial_utils import get_serial_sensor_type_patterns

    return {
        "schema_version": SCHEMA_VERSION,
        "name": name or f"device-{device_id}",
        "description": description,
        "meta": meta_out,
        "device_id": device_id,
        "lag_patterns": lag_patterns,
        "serial_type_patterns": get_serial_sensor_type_patterns(),
        "responses": responses,
    }
