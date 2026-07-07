"""Round-trip tests for data-shape capture: capture from a seeded mock, replay, assert same outcome.

These exercise the real LibreNMSAPI (via _raw_get) over real HTTP against the mock server, then
re-run the real detection / relationship-resolution logic on the captured recording — proving
capture and replay are faithful.
"""

from unittest.mock import patch

import pytest

from netbox_librenms_plugin.data_shapes.capture import capture_device_recording
from netbox_librenms_plugin.data_shapes.signature import compute_shape_signature
from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links
from netbox_librenms_plugin.tests.recordings import load_recording

# capture_device_recording snapshots the device OS's PortStackLagPattern rows into the
# recording (recording["lag_patterns"]), so every capture touches the DB.
pytestmark = pytest.mark.django_db


class _StubApi:
    """Minimal LibreNMS API serving recorded ``(status, body)`` per path — for capture-logic tests.

    Lets a test inject a transport error (status 0) or differing OOB ports without an HTTP server.
    No ``get_serial_port_sensors`` attribute, so capture skips the serial-sensor route.
    """

    def __init__(self, routes):
        self.routes = routes  # {path (no /api/v0/ prefix, no query): (status, body)}
        self.server_key = "stub"

    def _raw_get(self, path, params=None):
        return self.routes.get(path, (200, {"status": "ok"}))


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
            # Required structural routes: capture fails loudly on an error status for these
            # (a stale librenms_id answers 404 everywhere), so the seed must serve them.
            "GET /api/v0/devices/2000/ports": {"status": "ok", "ports": []},
            "GET /api/v0/devices/2000/port_stack": {"status": "ok", "mappings": []},
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


def test_capture_serial_sensors_bypasses_stale_cache(recording_server):
    """Capture must read sensors fresh (use_cache=False), not a stale per-server cache from an earlier refresh."""
    from django.core.cache import cache

    seed = _transceiver_serial_seed()  # server serves sensors 1 & 2 for device 2000
    _server, api = recording_server(seed)

    # Poison the per-server serial-sensor cache with a subset that does NOT match what the server
    # now serves. A cached read would record this stale sensor into the recording instead of the
    # device's current shape.
    stale = [
        {
            "sensor_id": 777,
            "device_id": 2000,
            "sensor_type": "acsSerialPortTable",
            "sensor_index": "acsSerialPortTableStatus.99",
            "sensor_descr": "STALE Status",
            "sensor_class": "state",
        }
    ]
    cache.set(f"librenms_serial_sensors_{api.server_key}", stale, timeout=300)

    captured = capture_device_recording(api, 2000)

    recorded = {s["sensor_id"] for s in captured["responses"]["GET /api/v0/resources/sensors"]["sensors"]}
    # The capture pulled FRESH server data (sensors 1 & 2), not the poisoned cache entry (777).
    assert recorded == {1, 2}
    assert 777 not in recorded


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
    # The seed serves real (empty) ports/port_stack, so capture records them verbatim as ok
    # responses — NOT the [404, error] entries it would store if the seed omitted those routes
    # (capture requests both with required=True, and the mock 404s unregistered routes).
    assert captured["responses"]["GET /api/v0/devices/1000/ports"] == {"status": "ok", "ports": []}
    assert captured["responses"]["GET /api/v0/devices/1000/port_stack"] == {"status": "ok", "mappings": []}

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


def test_capture_mirrors_inventory_all_fallback_when_server_ignores_filters(recording_server):
    """A server that returns empty filtered inventory but populates /all must still capture the VC."""
    root = [{"entPhysicalIndex": 1, "entPhysicalClass": "stack", "entPhysicalContainedIn": 0}]
    members = [
        {
            "entPhysicalIndex": 100,
            "entPhysicalClass": "chassis",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 1,
        },
        {
            "entPhysicalIndex": 200,
            "entPhysicalClass": "chassis",
            "entPhysicalContainedIn": 1,
            "entPhysicalParentRelPos": 2,
        },
    ]
    seed = {
        "schema_version": 1,
        "name": "no-filter-srv",
        "device_id": 1000,
        "meta": {"os": "ios"},
        "responses": {
            "GET /api/v0/devices/1000": {"status": "ok", "devices": [{"device_id": 1000, "os": "ios"}]},
            # The server ignores the entPhysical* filter params → empty filtered inventory...
            "GET /api/v0/inventory/1000?entPhysicalContainedIn=0": {"status": "ok", "inventory": []},
            "GET /api/v0/inventory/1000?entPhysicalClass=chassis&entPhysicalContainedIn=1": {
                "status": "ok",
                "inventory": [],
            },
            # ...but /inventory/{id}/all is populated (the get_inventory_filtered fallback source).
            "GET /api/v0/inventory/1000/all": {"status": "ok", "inventory": root + members},
            "GET /api/v0/devices/1000/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "Gi1/0/1", "ifType": "ethernetCsmacd"}],
            },
            "GET /api/v0/devices/1000/port_stack": {"status": "ok", "mappings": []},
        },
    }
    _server, api = recording_server(seed)
    captured = capture_device_recording(api, 1000, name="captured-fallback")

    # Without the fallback the filtered inventory is empty → the VC is silently downgraded to a plain
    # device. The capture must reproduce the entities under the filtered key the signature/replay read.
    sig = compute_shape_signature(captured)
    assert sig["virtual_chassis"]["present"] is True
    assert sig["virtual_chassis"]["member_count"] == 2


def test_capture_synthesizes_filtered_inventory_route_after_transport_failure():
    """A filtered inventory query that fails at transport but whose /all fallback filters to [] must still record the filtered route, or replay 404s on a structural route."""
    routes = {
        "devices/1000": (200, {"status": "ok", "devices": [{"device_id": 1000, "os": "ios"}]}),
        # The filtered inventory query fails at the transport layer → record() skips storing it.
        "inventory/1000": (0, None),
        # /all answers, but client-side filtering yields [] (no containedIn=0 entities).
        "inventory/1000/all": (200, {"status": "ok", "inventory": []}),
        "devices/1000/ports": (200, {"status": "ok", "ports": []}),
        "devices/1000/port_stack": (200, {"status": "ok", "mappings": []}),
    }
    api = _StubApi(routes)

    captured = capture_device_recording(api, 1000)

    # The structural route is still present (synthesized empty), not missing — so replay won't 404.
    key = "GET /api/v0/inventory/1000?entPhysicalContainedIn=0"
    assert key in captured["responses"]
    assert captured["responses"][key] == {"status": "ok", "inventory": []}


def test_capture_skips_route_with_transport_error_status():
    """A route that fails at the transport layer (status 0) must not be baked into the recording (it would replay as an invalid send_response(0))."""
    routes = {
        "devices/1000": {"status": "ok", "devices": [{"device_id": 1000, "os": "ios"}]},
        "inventory/1000": {"status": "ok", "inventory": []},
        "inventory/1000/all": {"status": "ok", "inventory": []},
        "devices/1000/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "Gi0/1"}]},
        "devices/1000/port_stack": {"status": "ok", "mappings": []},
    }
    api = _StubApi({k: (200, v) for k, v in routes.items()})
    api.routes["devices/1000/transceivers"] = (0, None)  # transport error on this route

    captured = capture_device_recording(api, 1000)

    assert "GET /api/v0/devices/1000/transceivers" not in captured["responses"]
    # Other routes are still recorded — only the un-replayable status-0 route is skipped.
    assert "GET /api/v0/devices/1000/ports" in captured["responses"]


def test_capture_skips_oob_ports_when_oob_id_equals_host():
    """When a (misconfigured) OOB controller id equals the host device id, the OOB ports fetch must be skipped so it can't overwrite the host's ports under the shared key."""
    routes = {
        "devices/1000": {"status": "ok", "devices": [{"device_id": 1000, "os": "ios"}]},
        "inventory/1000": {"status": "ok", "inventory": []},
        "inventory/1000/all": {"status": "ok", "inventory": []},
        "devices/1000/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "Gi0/1"}]},
        "devices/1000/port_stack": {"status": "ok", "mappings": []},
        "devices/1000/transceivers": {"status": "ok", "transceivers": []},
    }
    api = _StubApi({k: (200, v) for k, v in routes.items()})

    captured = capture_device_recording(api, 1000, oob_id=1000)

    # The host id is not recorded as its own OOB controller, so no self-collision on the ports key.
    assert "oob_id" not in captured["meta"]
    assert captured["responses"]["GET /api/v0/devices/1000/ports"]["ports"] == [{"port_id": 1, "ifName": "Gi0/1"}]


def _all_ok_routes():
    """Routes where every structural endpoint answers 200 — a happy-path capture base."""
    return {
        "devices/1000": {"status": "ok", "devices": [{"device_id": 1000, "os": "ios"}]},
        "inventory/1000": {"status": "ok", "inventory": []},
        "inventory/1000/all": {"status": "ok", "inventory": []},
        "devices/1000/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "Gi0/1"}]},
        "devices/1000/port_stack": {"status": "ok", "mappings": []},
        "devices/1000/transceivers": {"status": "ok", "transceivers": []},
    }


def test_capture_raises_when_required_route_has_transport_error():
    """A required structural route (here port_stack) with no HTTP response must fail the capture, not persist a partial recording."""
    api = _StubApi({k: (200, v) for k, v in _all_ok_routes().items()})
    api.routes["devices/1000/port_stack"] = (0, None)  # transport error on a REQUIRED route

    with pytest.raises(RuntimeError, match="port_stack"):
        capture_device_recording(api, 1000)


def test_capture_ignores_caller_supplied_oob_id_without_controller():
    """A caller-supplied meta['oob_id'] must not mark a plain recording as OOB — oob_id is authoritative and set only when controller ports are actually captured."""
    api = _StubApi({k: (200, v) for k, v in _all_ok_routes().items()})

    captured = capture_device_recording(api, 1000, meta={"oob_id": 999, "note": "kept"})

    # The spoofed oob_id is dropped (no controller was captured); unrelated caller meta is preserved.
    assert "oob_id" not in captured["meta"]
    assert captured["meta"].get("note") == "kept"
    assert compute_shape_signature(captured)["oob"] is False


def test_capture_raises_when_inventory_all_fallback_fails():
    """Once the filtered inventory comes back empty, /all is the ONLY inventory source — a transport failure there must fail the capture, not silently record an empty (wrong-topology) inventory."""
    api = _StubApi({k: (200, v) for k, v in _all_ok_routes().items()})
    # Filtered inventory is empty (see _all_ok_routes), so capture falls back to /all — make it fail.
    api.routes["inventory/1000/all"] = (0, None)  # transport error on the /all fallback

    with pytest.raises(RuntimeError, match="inventory/1000/all"):
        capture_device_recording(api, 1000)


def test_capture_does_not_mark_oob_when_controller_ports_fail():
    """record() skips an un-replayable controller-ports route (transport error); the recording must NOT then be marked OOB with no controller ports behind it."""
    api = _StubApi({k: (200, v) for k, v in _all_ok_routes().items()})
    api.routes["devices/2000/ports"] = (0, None)  # OOB controller ports fail at the transport layer

    captured = capture_device_recording(api, 1000, oob_id=2000)

    # The un-replayable controller route was skipped, so the recording isn't marked OOB.
    assert "GET /api/v0/devices/2000/ports" not in captured["responses"]
    assert "oob_id" not in captured["meta"]
    assert compute_shape_signature(captured)["oob"] is False


def test_capture_required_route_http_error_raises():
    """A real HTTP error (404) on a required structural route must fail the capture loudly — a stale librenms_id answers 404 everywhere and would otherwise ship a junk all-404 recording presented as a success."""
    api = _StubApi({"devices/55": (404, {"status": "error", "message": "Device not found"})})

    with pytest.raises(RuntimeError, match="HTTP 404"):
        capture_device_recording(api, 55)


def test_capture_string_oob_id_equal_to_host_is_rejected():
    """The self-OOB guard compares COERCED ids: a string-typed OOB id equal to the host's own int id must be treated as self (no oob_id stamp), not slip past on "123" != 123."""
    api = _StubApi(
        {
            "devices/123": (200, {"status": "ok", "devices": [{"device_id": 123, "os": "ios"}]}),
            "devices/123/ports": (200, {"status": "ok", "ports": []}),
            "devices/123/port_stack": (200, {"status": "ok", "mappings": []}),
        }
    )

    recording = capture_device_recording(api, 123, oob_id="123")

    assert "oob_id" not in recording["meta"]


@pytest.mark.django_db
def test_capture_snapshots_os_scoped_lag_patterns():
    """The recording carries the device OS's PortStackLagPattern regexes — signature and replay read recording["lag_patterns"], so a pattern-based LAG shape is unfingerprintable/unreplayable without the snapshot."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    # Unique OS names: default patterns for real OSes may already be seeded (CI-unique).
    PortStackLagPattern.objects.create(librenms_os="captest-os", lag_name_pattern=r"^Po\d+$")
    PortStackLagPattern.objects.create(librenms_os="captest-other", lag_name_pattern=r"^ae\d+$")
    api = _StubApi(
        {
            "devices/77": (200, {"status": "ok", "devices": [{"device_id": 77, "os": "captest-os"}]}),
            "devices/77/ports": (200, {"status": "ok", "ports": []}),
            "devices/77/port_stack": (200, {"status": "ok", "mappings": []}),
        }
    )

    recording = capture_device_recording(api, 77)

    # Scoped to the captured OS: the other platform's pattern is not embedded.
    assert recording["lag_patterns"] == {"captest-os": r"^Po\d+$"}


@pytest.mark.django_db
@pytest.mark.parametrize("blank_os", ["", "   "])
def test_capture_blank_os_embeds_no_lag_patterns(blank_os):
    """A present-but-blank OS embeds NO patterns, mirroring compiled_patterns_for_os.

    Production's ``compiled_patterns_for_os("")`` deliberately matches nothing, while
    ``signature.py`` compiles every recorded pattern unscoped — so embedding all stored
    patterns for a blank-OS device would fingerprint/replay pattern-LAG behavior that
    production never applies to that device.
    """
    from netbox_librenms_plugin.models import PortStackLagPattern

    PortStackLagPattern.objects.create(librenms_os="captest-blank", lag_name_pattern=r"^Po\d+$")
    api = _StubApi(
        {
            "devices/78": (200, {"status": "ok", "devices": [{"device_id": 78, "os": blank_os}]}),
            "devices/78/ports": (200, {"status": "ok", "ports": []}),
            "devices/78/port_stack": (200, {"status": "ok", "mappings": []}),
        }
    )

    recording = capture_device_recording(api, 78)

    assert recording["lag_patterns"] == {}


@pytest.mark.django_db
def test_capture_no_os_at_all_keeps_legacy_unscoped_lag_patterns():
    """No ``os`` key in the device payload (device_os=None): the legacy embed-everything path stays, matching compiled_patterns_for_os(None)."""
    from netbox_librenms_plugin.models import PortStackLagPattern

    PortStackLagPattern.objects.create(librenms_os="captest-noneos", lag_name_pattern=r"^Po\d+$")
    api = _StubApi(
        {
            "devices/79": (200, {"status": "ok", "devices": [{"device_id": 79}]}),
            "devices/79/ports": (200, {"status": "ok", "ports": []}),
            "devices/79/port_stack": (200, {"status": "ok", "mappings": []}),
        }
    )

    recording = capture_device_recording(api, 79)

    # `in`, not `==`: default patterns for real OSes may already be seeded by other tests.
    assert "captest-noneos" in recording["lag_patterns"]
