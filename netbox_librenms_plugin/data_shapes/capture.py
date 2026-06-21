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

    def record(path, request_params=None, *, key_params="__same__"):
        """Issue one raw GET and store it under its route key (key_params=None → path-only)."""
        status, body = api._raw_get(path, request_params)
        if key_params == "__same__":
            key_params = request_params
        key = f"GET /api/v0/{path}"
        if key_params:
            key += "?" + "&".join(f"{k}={v}" for k, v in key_params.items())
        responses[key] = body if 200 <= status < 300 else [status, body]
        return status, body

    # 1. Device info (also the source of the os metadata).
    _, dev_body = record(f"devices/{device_id}")
    device_os = None
    if isinstance(dev_body, dict):
        devices = dev_body.get("devices")
        if isinstance(devices, list) and devices and isinstance(devices[0], dict):
            device_os = devices[0].get("os")

    # 2. VC detection inventory: root, then the chassis children at the resolved parent index.
    #    These two query variants share a path, so they MUST be keyed with their query so the
    #    loader can disambiguate them.
    _, root_body = record(f"inventory/{device_id}", {"entPhysicalContainedIn": "0"})
    root_items = root_body.get("inventory") if isinstance(root_body, dict) else None
    parent_index = _select_parent_index(root_items)
    if parent_index is not None:
        record(
            f"inventory/{device_id}",
            {"entPhysicalClass": "chassis", "entPhysicalContainedIn": str(parent_index)},
        )

    # 3. Ports (request VLAN data so the body matches what get_ports reads) and 4. port_stack
    #    (LAG / sub-interface relationships). Both are keyed path-only — there's a single variant
    #    per path, so the loader serves it for any query the production readers send.
    record(f"devices/{device_id}/ports", {"columns": _PORTS_COLUMNS, "with": "vlans"}, key_params=None)
    record(f"devices/{device_id}/port_stack")

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
        ss_ok, device_serial_sensors = api.get_serial_port_sensors(device_id)
        if ss_ok and device_serial_sensors:
            responses["GET /api/v0/resources/sensors"] = {"status": "ok", "sensors": device_serial_sensors}

    # 7. OOB controller ports — a SEPARATE LibreNMS device the interfaces view merges into the host.
    #    Record them under the controller's own /ports route so replay's get_ports(oob_id) serves them.
    meta_out = {"os": device_os, **(meta or {})}
    if oob_id is not None:
        record(f"devices/{oob_id}/ports", {"columns": _PORTS_COLUMNS, "with": "vlans"}, key_params=None)
        meta_out["oob_id"] = oob_id

    return {
        "schema_version": SCHEMA_VERSION,
        "name": name or f"device-{device_id}",
        "description": description,
        "meta": meta_out,
        "device_id": device_id,
        "responses": responses,
    }
