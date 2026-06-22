"""Round-trip tests for data-shape capture: capture from a seeded mock, replay, assert same outcome.

These exercise the real LibreNMSAPI (via _raw_get) over real HTTP against the mock server, then
re-run the real detection / relationship-resolution logic on the captured recording — proving
capture and replay are faithful.
"""

from unittest.mock import patch

from netbox_librenms_plugin.data_shapes.capture import capture_device_recording
from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links
from netbox_librenms_plugin.tests.recordings import load_recording


def _transceiver_serial_seed():
    """A synthetic recording with a per-device transceivers route and an INSTANCE-WIDE sensors route.

    The /resources/sensors body carries serial sensors for the target device (2000), a *different*
    device (3000), and a non-serial sensor — so a capture can prove it records only the target
    device's serial sensors (no cross-device leak, non-serial types filtered out).
    """
    return {
        "schema_version": 1,
        "name": "transceiver-serial-seed",
        "device_id": 2000,
        "meta": {"os": "sros"},
        "responses": {
            "GET /api/v0/devices/2000": {
                "status": "ok",
                "devices": [{"device_id": 2000, "os": "sros", "hostname": "acs01.example.net"}],
            },
            "GET /api/v0/devices/2000/transceivers": {
                "status": "ok",
                "transceivers": [
                    {
                        "port_id": 519,
                        "entity_physical_index": 1610899520,
                        "type": "CFP2/QSFP28",
                        "model": "3HE10550AARA01",
                        "serial": "X42AU0D",
                        "channels": 4,
                        "connector": "LC",
                        "wavelength": 1301,
                    }
                ],
            },
            "GET /api/v0/resources/sensors": {
                "status": "ok",
                "sensors": [
                    {
                        "sensor_id": 1,
                        "device_id": 2000,
                        "sensor_type": "acsSerialPortTable",
                        "sensor_index": "acsSerialPortTableStatus.7",
                        "sensor_descr": "ttyS7 Status",
                        "sensor_class": "state",
                    },
                    {
                        "sensor_id": 2,
                        "device_id": 2000,
                        "sensor_type": "acsSerialPortTable",
                        "sensor_index": "acsSerialPortTableStatus.8",
                        "sensor_descr": "PROD-LAB03A-RA1 Status",
                        "sensor_class": "state",
                    },
                    {
                        "sensor_id": 99,
                        "device_id": 3000,
                        "sensor_type": "acsSerialPortTable",
                        "sensor_index": "acsSerialPortTableStatus.1",
                        "sensor_descr": "OTHER-DEVICE Status",
                        "sensor_class": "state",
                    },
                    {
                        "sensor_id": 50,
                        "device_id": 2000,
                        "sensor_type": "temperature",
                        "sensor_index": "tempSensor.1",
                        "sensor_descr": "CPU temperature",
                        "sensor_class": "temperature",
                    },
                ],
            },
        },
    }


def test_capture_records_verbatim_transceiver_body(recording_server):
    """The per-device transceivers route is recorded byte-for-byte (safe — not instance-wide)."""
    seed = _transceiver_serial_seed()
    _server, api = recording_server(seed)

    captured = capture_device_recording(api, 2000)

    assert (
        captured["responses"]["GET /api/v0/devices/2000/transceivers"]
        == seed["responses"]["GET /api/v0/devices/2000/transceivers"]
    )


def test_capture_meta_os_does_not_override_captured_device_os(recording_server):
    """A caller-supplied meta['os'] must NOT override the OS captured from the device-info response (it scopes signature/novelty); other caller meta keys are preserved."""
    seed = _transceiver_serial_seed()  # device 2000 reports os "sros"
    _server, api = recording_server(seed)

    captured = capture_device_recording(api, 2000, meta={"os": "SPOOFED", "note": "kept"})

    assert captured["meta"]["os"] == "sros"  # the captured device OS wins over the caller's
    assert captured["meta"]["note"] == "kept"  # other caller meta survives


def test_capture_serial_sensors_excludes_other_devices(recording_server):
    """The instance-wide /resources/sensors route is device-filtered before recording (no cross-device PII)."""
    seed = _transceiver_serial_seed()
    _server, api = recording_server(seed)

    captured = capture_device_recording(api, 2000)

    recorded = captured["responses"]["GET /api/v0/resources/sensors"]["sensors"]
    # Only the target device's serial sensors survive: device 3000's sensor and the non-serial
    # temperature sensor are both gone — so the recording can never publish another device's data.
    assert {s["sensor_id"] for s in recorded} == {1, 2}
    assert all(s["device_id"] == 2000 for s in recorded)
    assert all(s["sensor_type"] == "acsSerialPortTable" for s in recorded)


def test_capture_serial_sensors_roundtrip_outcome(recording_server):
    """Replaying the captured recording yields the same serial-link rows the source produced."""
    seed = _transceiver_serial_seed()
    _server, api = recording_server(seed)
    source_ok, source_sensors = api.get_serial_port_sensors(2000)
    assert source_ok
    source_links = map_sensors_to_serial_links(source_sensors, device_id=2000)

    captured = capture_device_recording(api, 2000)

    _server2, api2 = recording_server(captured, server_key="replay")
    replay_ok, replay_sensors = api2.get_serial_port_sensors(2000)
    assert replay_ok
    replay_links = map_sensors_to_serial_links(replay_sensors, device_id=2000)

    assert replay_links == source_links
    # Non-vacuous: the rows really exercise both is_configured states (default ttyS7 vs custom label).
    assert {row["is_configured"] for row in replay_links} == {True, False}


def test_capture_skips_sensors_route_when_device_has_none(recording_server):
    """A device with no serial sensors must NOT add the instance-wide /resources/sensors route."""
    seed = _transceiver_serial_seed()
    # Strip every serial sensor for the target device; only device 3000 remains.
    seed["responses"]["GET /api/v0/resources/sensors"]["sensors"] = [
        s for s in seed["responses"]["GET /api/v0/resources/sensors"]["sensors"] if s["device_id"] != 2000
    ]
    _server, api = recording_server(seed)

    captured = capture_device_recording(api, 2000)

    assert "GET /api/v0/resources/sensors" not in captured["responses"]


def test_capture_roundtrip_preserves_vc_outcome(recording_server):
    """Capturing a VC device and replaying the capture yields the same detection result."""
    seed = load_recording("cisco-stackwise-3member")
    _server, api = recording_server(seed)

    captured = capture_device_recording(api, seed["device_id"], name="captured-cisco")

    # os metadata is lifted verbatim from the captured device-info response (the bundled seed's OS
    # is already pseudonymized, and capture does not anonymize — it records what the server returns).
    assert captured["meta"]["os"] == seed["responses"]["GET /api/v0/devices/1000"]["devices"][0]["os"]
    # The capture recorded device info, both inventory variants, ports, and port_stack.
    keys = list(captured["responses"])
    assert "GET /api/v0/devices/1000" in keys
    assert any("inventory/1000?entPhysicalContainedIn=0" in k for k in keys)
    assert any("entPhysicalClass=chassis" in k for k in keys)
    assert "GET /api/v0/devices/1000/ports" in keys
    assert "GET /api/v0/devices/1000/port_stack" in keys

    # Round-trip: replay the CAPTURED recording in a fresh mock and re-run VC detection.
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

    _server2, api2 = recording_server(captured)
    with patch(
        "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
        return_value="{master}-m{position}",
    ):
        result = detect_virtual_chassis_from_inventory(api2, 1000)

    assert result is not None
    assert result["member_count"] == 3
    assert [m["serial"] for m in result["members"]] == seed["expected"]["virtual_chassis"]["member_serials"]


def test_capture_roundtrip_preserves_port_relationships(recording_server):
    """Capturing a device with LAG + sub-interface rows preserves ports/port_stack on replay."""
    seed = load_recording("cisco-lag-and-subinterface")
    _server, api = recording_server(seed)

    captured = capture_device_recording(api, seed["device_id"], name="captured-lag")

    _server2, api2 = recording_server(captured)
    ok_ports, ports_data = api2.get_ports(1002)
    ok_stack, port_stack = api2.get_port_stack(1002)
    assert ok_ports and ok_stack

    relationships = api2.resolve_port_relationships(ports_data["ports"], port_stack, lag_patterns={})
    lag = {str(k): str(v) for k, v in relationships["lag_members"].items()}
    sub = {str(k): str(v) for k, v in relationships["sub_interfaces"].items()}
    assert lag == {str(k): str(v) for k, v in seed["expected"]["lag_members"].items()}
    assert sub == {str(k): str(v) for k, v in seed["expected"]["sub_interfaces"].items()}


def test_capture_records_verbatim_port_body(recording_server):
    """The captured ports body matches the source server's ports payload byte-for-byte."""
    seed = load_recording("cisco-lag-and-subinterface")
    _server, api = recording_server(seed)

    captured = capture_device_recording(api, 1002)

    captured_ports = captured["responses"]["GET /api/v0/devices/1002/ports"]
    assert captured_ports == seed["responses"]["GET /api/v0/devices/1002/ports"]
