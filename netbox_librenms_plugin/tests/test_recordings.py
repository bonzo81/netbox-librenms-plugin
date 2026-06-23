"""Outcome tests driven by captured LibreNMS data-shape recordings.

Each recording in ``data_shapes/recordings/*.json`` is replayed through the mock
LibreNMS HTTP server and the real LibreNMSAPI client, then the real detection
and relationship-resolution logic runs against it and is asserted against the
recording's ``expected`` block. A new recording with an ``expected`` block
becomes a passing test with no new code.

The flow is exercised end-to-end (real client, real HTTP, real parsing); only
the plugin-config lookup and the VC member-name pattern (a DB read) are stubbed,
so these tests need no database.
"""

from unittest.mock import patch

import pytest

from netbox_librenms_plugin.tests.recordings import iter_recording_paths, iter_recordings

_RECORDINGS = iter_recordings()


def _ids(recording):
    return recording.get("name", "unnamed")


def test_recordings_present():
    """The recordings directory must contain at least one scenario."""
    assert iter_recording_paths(), "no recording JSON files found in data_shapes/recordings/"


def test_load_recording_rejects_path_traversal():
    """A recording name that escapes the recordings directory must raise ValueError, not read an arbitrary file off disk."""
    from netbox_librenms_plugin.data_shapes.recordings_store import load_recording

    with pytest.raises(ValueError):
        load_recording("../../../../../../etc/passwd")


def test_recording_schema_errors_rejects_bool_int_fields():
    """Bool is an int subclass; True/False for schema_version or device_id must be rejected — a bare `!= 1` / `isinstance(int)` check would otherwise let a malformed recording validate."""
    from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

    rec = {"schema_version": True, "name": "x", "device_id": False, "responses": {"GET /x": {"status": "ok"}}}
    errors = recording_schema_errors(rec)
    assert any("schema_version must be 1" in e for e in errors)
    assert any("device_id must be an integer" in e for e in errors)


def test_get_ports_real_fetch_and_parse_via_recording(recording_server):
    """Real-HTTP de-mock demo: get_ports() fetches and parses a real captured recording through the live LibreNMSAPI client (real `requests` → MockLibreNMSServer → real parse), so a regression in the request build or response parsing is caught — unlike test_librenms_api.py::test_get_ports_all, which mocks requests.get and feeds canned JSON straight back."""
    from netbox_librenms_plugin.tests.recordings import load_recording

    _server, api = recording_server(load_recording("cisco-lag-and-subinterface"))
    success, data = api.get_ports(device_id=1002)

    assert success is True
    ports = data.get("ports")
    assert isinstance(ports, list) and len(ports) == 4  # the recording's real port count
    # The columns get_ports() actually requests survive the real fetch+parse round-trip.
    assert all(isinstance(p, dict) and "port_id" in p and "ifName" in p for p in ports)
    assert "Port-channel1" in {p["ifName"] for p in ports}


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_recording_carries_no_residual_pii(recording):
    """Every committed recording must be anonymized — the PII safety-net finds nothing.

    Guards against a maintainer accidentally committing a non-anonymized capture.
    """
    from netbox_librenms_plugin.data_shapes.anonymize import find_pii

    assert find_pii(recording) == []


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_recording_has_no_public_asn(recording):
    """A committed recording must not leak a public BGP ASN — bgpLocalAs is identifying, so it must be anonymized into the private 64512-65534 range (find_pii() is string-only and won't catch an int ASN)."""
    from netbox_librenms_plugin.data_shapes.anonymize import BGP_KEYS

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in BGP_KEYS and isinstance(v, int) and not isinstance(v, bool) and v != 0:
                    assert 64512 <= v <= 65534, f"{k}={v} is a public ASN; anonymize it to the private range"
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(recording.get("responses", {}))


def test_manifest_is_in_sync_with_bundled_recordings():
    """data_shapes/recordings/manifest.json must match the bundled recordings.

    Fails when a recording is added/changed without `librenms_recordings --rebuild-manifest`,
    so the in-plugin novelty check never goes stale.
    """
    from netbox_librenms_plugin.data_shapes import recordings_store
    from netbox_librenms_plugin.data_shapes.signature import build_manifest

    expected = build_manifest(recordings_store.load_bundled_recordings())
    assert recordings_store.load_manifest() == expected, "run: manage.py librenms_recordings --rebuild-manifest"


def test_load_manifest_returns_empty_on_unreadable_file():
    """load_manifest() is best-effort: an OSError from read_text (permission/IO) must return [], not propagate and break every caller. read_text's OSError is a filesystem boundary that can't be triggered reliably as root, so inject it."""
    from unittest.mock import MagicMock

    from netbox_librenms_plugin.data_shapes import recordings_store

    fake_path = MagicMock()
    fake_path.exists.return_value = True
    fake_path.read_text.side_effect = OSError("permission denied")
    with patch.object(recordings_store, "MANIFEST_PATH", fake_path):
        assert recordings_store.load_manifest() == []


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_recording_has_required_schema(recording):
    """Every recording declares the keys the replay harness depends on."""
    assert recording.get("schema_version") == 1
    assert recording.get("name")
    assert isinstance(recording.get("device_id"), int)
    assert isinstance(recording.get("responses"), dict) and recording["responses"]
    assert isinstance(recording.get("expected"), dict) and recording["expected"], (
        f"{recording.get('name')} has no expected outcomes to assert"
    )


def _assert_virtual_chassis(api, device_id, expected):
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

    with patch(
        "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
        return_value="{master}-m{position}",
    ):
        result = detect_virtual_chassis_from_inventory(api, device_id)

    if expected is None:
        assert result is None
        return

    assert result is not None
    assert result["is_stack"] == expected["is_stack"]
    assert result["member_count"] == expected["member_count"]
    if "member_serials" in expected:
        assert [m["serial"] for m in result["members"]] == expected["member_serials"]


def _assert_port_relationships(api, device_id, recording, expected):
    ok_ports, ports_data = api.get_ports(device_id)
    assert ok_ports, ports_data
    ok_stack, port_stack = api.get_port_stack(device_id)
    assert ok_stack, port_stack

    relationships = api.resolve_port_relationships(
        ports_data["ports"],
        port_stack,
        # Pass patterns explicitly (default {}) so resolution never touches the DB.
        lag_patterns=recording.get("lag_patterns", {}),
        device_os=recording.get("meta", {}).get("os"),
    )

    # port_stack and ports are independent payloads whose ids may differ in type,
    # so compare both sides normalised to str -- matching how production keys lookups.
    for key in ("lag_members", "sub_interfaces"):
        if key in expected:
            got = {str(k): str(v) for k, v in relationships[key].items()}
            want = {str(k): str(v) for k, v in expected[key].items()}
            assert got == want, f"{recording['name']} {key}: {got} != {want}"


def _assert_transceivers(api, device_id, expected):
    ok, transceivers = api.get_device_transceivers(device_id)
    assert ok, transceivers
    assert len(transceivers) == expected["count"]


def _assert_serial_ports(api, device_id, expected):
    from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

    ok, sensors = api.get_serial_port_sensors(device_id)
    assert ok, sensors
    rows = map_sensors_to_serial_links(sensors, device_id=device_id)
    assert len(rows) == expected["count"]
    if "configured" in expected:
        assert sum(1 for r in rows if r["is_configured"]) == expected["configured"]


def _assert_oob(api, recording, expected):
    """The linked OOB controller's ports are recorded and replayable (the interfaces view merges them)."""
    oob_id = recording.get("meta", {}).get("oob_id")
    assert oob_id is not None, f"{recording['name']} declares oob outcome but no meta.oob_id"
    ok, oob_data = api.get_ports(oob_id)
    assert ok, oob_data
    assert len(oob_data["ports"]) == expected["controller_ports"]


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_recording_outcomes(recording, recording_server):
    """Replay a recording and assert its declared outcomes against the real logic."""
    _server, api = recording_server(recording)
    device_id = recording["device_id"]
    expected = recording["expected"]

    if "virtual_chassis" in expected:
        _assert_virtual_chassis(api, device_id, expected["virtual_chassis"])

    if "lag_members" in expected or "sub_interfaces" in expected:
        _assert_port_relationships(api, device_id, recording, expected)

    if "transceivers" in expected:
        _assert_transceivers(api, device_id, expected["transceivers"])

    if "serial_ports" in expected:
        _assert_serial_ports(api, device_id, expected["serial_ports"])

    if "oob" in expected:
        _assert_oob(api, recording, expected["oob"])
