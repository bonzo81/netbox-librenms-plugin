"""Round-trip tests for data-shape capture: capture from a seeded mock, replay, assert same outcome.

These exercise the real LibreNMSAPI (via _raw_get) over real HTTP against the mock server, then
re-run the real detection / relationship-resolution logic on the captured recording — proving
capture and replay are faithful.
"""

from unittest.mock import patch

from netbox_librenms_plugin.data_shapes.capture import capture_device_recording
from netbox_librenms_plugin.tests.recordings import load_recording


def test_capture_roundtrip_preserves_vc_outcome(recording_server):
    """Capturing a VC device and replaying the capture yields the same detection result."""
    seed = load_recording("cisco-stackwise-3member")
    _server, api = recording_server(seed)

    captured = capture_device_recording(api, seed["device_id"], name="captured-cisco")

    # os metadata is lifted from the captured device-info response.
    assert captured["meta"]["os"] == "ios"
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
