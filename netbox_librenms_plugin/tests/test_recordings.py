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


def test_load_recording_rejects_manifest_and_non_dict(monkeypatch, tmp_path):
    """load_recording advertises a single recording dict: the manifest (a list) and any non-dict JSON must raise, not silently return a list."""
    from netbox_librenms_plugin.data_shapes import recordings_store
    from netbox_librenms_plugin.data_shapes.recordings_store import load_recording

    # The manifest is a list of signatures, not a recording — rejected by name.
    with pytest.raises(ValueError, match="not a recording"):
        load_recording("manifest")

    # A non-dict recording JSON is rejected by shape.
    monkeypatch.setattr(recordings_store, "RECORDINGS_DIR", tmp_path)
    (tmp_path / "listy.json").write_text("[1, 2, 3]")
    with pytest.raises(ValueError, match="not a recording object"):
        load_recording("listy")


def test_recording_schema_errors_rejects_bool_int_fields():
    """Bool is an int subclass; True/False for schema_version or device_id must be rejected — a bare `!= 1` / `isinstance(int)` check would otherwise let a malformed recording validate."""
    from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

    rec = {"schema_version": True, "name": "x", "device_id": False, "responses": {"GET /x": {"status": "ok"}}}
    errors = recording_schema_errors(rec)
    assert any("schema_version must be 1" in e for e in errors)
    assert any("device_id must be an integer" in e for e in errors)


def test_recording_variant_handler_requires_exact_query():
    """The replay matcher must require EXACT query equality: a request carrying an extra unexpected param must NOT subset-match a recorded variant (which would let a request-shape regression false-pass) — it must fail closed with 404."""
    from netbox_librenms_plugin.tests.mock_librenms_server import _recording_variant_handler

    # Recorded qdicts carry the full sorted value tuple per key (as the registration path builds them).
    variants = [({"columns": ("ifName",)}, 200, {"status": "ok"})]
    handler = _recording_variant_handler("/ports", variants)

    # Exact query → the recorded response.
    assert handler("GET", "/ports", {"columns": ["ifName"]}, {}, None) == (200, {"status": "ok"})
    # An extra unexpected param is a different request shape → 404, not a subset false-pass.
    status, _body = handler("GET", "/ports", {"columns": ["ifName"], "extra": ["x"]}, {}, None)
    assert status == 404


def test_load_recording_distinguishes_repeated_query_params():
    """Two variants differing only by a repeated param (?columns=A vs ?columns=A&columns=B) must register as distinct shapes and replay to their own bodies — collapsing to the first value would false-match."""
    from netbox_librenms_plugin.tests.mock_librenms_server import MockLibreNMSServer

    server = MockLibreNMSServer()
    try:
        server.load_recording(
            {
                "responses": {
                    "GET /api/v0/devices/1/ports?columns=ifName": {"status": "single"},
                    "GET /api/v0/devices/1/ports?columns=ifName&columns=ifDescr": {"status": "double"},
                }
            }
        )
        handler = server.routes["GET /api/v0/devices/1/ports"]
        assert callable(handler)  # two distinct variants → a selecting handler, not a static tuple
        # Each request shape routes to its OWN body (the old v[0] collapse served "single" for both).
        assert handler("GET", "/api/v0/devices/1/ports", {"columns": ["ifName"]}, {}, None) == (
            200,
            {"status": "single"},
        )
        assert handler("GET", "/api/v0/devices/1/ports", {"columns": ["ifName", "ifDescr"]}, {}, None) == (
            200,
            {"status": "double"},
        )
    finally:
        server._server.server_close()


def test_queryless_recording_serves_any_query_by_design():
    """A single queryless variant is registered as a bare path that serves ANY query — capture.py keys response-irrelevant endpoints (e.g. /ports) with key_params=None, and get_ports() sends columns=… the recording deliberately doesn't store, so the loader must match regardless."""
    from netbox_librenms_plugin.tests.mock_librenms_server import MockLibreNMSServer

    server = MockLibreNMSServer()
    try:
        server.load_recording({"responses": {"GET /api/v0/devices/1/ports": {"status": "ok"}}})
        route = server.routes["GET /api/v0/devices/1/ports"]
        # Bare-path static tuple (not a query-selecting handler) so a production reader's
        # columns=…&with=vlans query still resolves the recorded body.
        assert route == (200, {"status": "ok"})
    finally:
        server._server.server_close()


def test_replay_matches_request_with_blank_valued_query_param(recording_server):
    """A recorded route whose only variant carries a blank-valued query param (?probe=) must still match a byte-for-byte request that carries ?probe= — the request side must parse with keep_blank_values too (load_recording already does), or replay 404s on an exact-shape match."""
    import http.client
    from urllib.parse import urlparse

    recording = {
        "schema_version": 1,
        "name": "blank-param-route",
        "device_id": 1,
        "meta": {"os": "ios"},
        "responses": {
            # The recording side normalizes ?probe= as a present empty value; the request side must too.
            "GET /api/v0/devices/1/ports?probe=": {"status": "ok", "ports": [{"port_id": 7}]},
        },
    }
    server, _api = recording_server(recording)
    parsed = urlparse(server.url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port)
    try:
        conn.request("GET", "/api/v0/devices/1/ports?probe=")
        resp = conn.getresponse()
        status = resp.status
        body = resp.read()
    finally:
        conn.close()

    # Without keep_blank_values on the request side, ?probe= parses to {} and falls through to 404.
    assert status == 200
    assert b'"port_id"' in body


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


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_recording_has_anonymized_vendor_metadata(recording):
    """A committed recording must carry the anonymizer's normalized icon (generic.svg) and a pseudonymized entPhysicalMfgName (MFG-<hash>) — a raw value means the fixture predates an anonymizer rule and re-leaks vendor metadata."""
    import re

    mfg_re = re.compile(r"^MFG-[0-9a-f]{6}$")
    leaked = []

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "icon" and isinstance(v, str) and v and v != "images/os/generic.svg":
                    leaked.append((k, v))
                if k == "entPhysicalMfgName" and isinstance(v, str) and v and not mfg_re.match(v):
                    leaked.append((k, v))
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(recording)
    assert not leaked, f"{recording.get('name')}: un-anonymized vendor metadata: {leaked}"


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_recording_transceivers_reference_present_ports(recording):
    """Every transceiver must reference a port that survived compression — a dangling port_id means the replay isn't a self-consistent LibreNMS dataset (the transceiver-merge can't map it to a port name)."""

    def _body(suffix):
        for k, v in recording.get("responses", {}).items():
            if k.split("?", 1)[0].endswith(suffix):
                return v[1] if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int) else v
        return None

    device_id = recording.get("device_id")
    tx_body = _body(f"/devices/{device_id}/transceivers")
    ports_body = _body(f"/devices/{device_id}/ports")
    if not isinstance(tx_body, dict) or not isinstance(ports_body, dict):
        return  # no transceivers/ports route to cross-check

    port_ids = {str(p.get("port_id")) for p in ports_body.get("ports", []) if isinstance(p, dict)}
    dangling = sorted(
        str(t.get("port_id"))
        for t in tx_body.get("transceivers", [])
        if isinstance(t, dict) and t.get("port_id") not in (None, 0, "0") and str(t.get("port_id")) not in port_ids
    )
    assert not dangling, f"{recording.get('name')}: transceivers reference ports missing from /ports: {dangling}"


def test_make_recording_api_delegates_non_servers_config_to_real():
    """make_recording_api patches ONLY the 'servers' lookup: other keys (including a 3-arg defaulted call) must delegate to the real config — never returning None or raising a TypeError on the extra arg."""
    import netbox_librenms_plugin.librenms_api as api_mod
    from netbox_librenms_plugin.tests.conftest import make_recording_api

    # What the real config returns for a non-'servers' key (computed outside the patch).
    expected_other = api_mod.get_plugin_config("netbox_librenms_plugin", "cache_timeout", 300)

    captured = {}
    real_init = api_mod.LibreNMSAPI.__init__

    def spy_init(self, *args, **kwargs):
        # While make_recording_api's patch is active, exercise the patched get_plugin_config the
        # way a construction-time read would: the 'servers' key (mocked) and another key with a
        # default 3rd arg (must delegate to the real config, not return None or raise).
        captured["servers"] = api_mod.get_plugin_config("netbox_librenms_plugin", "servers")
        captured["other"] = api_mod.get_plugin_config("netbox_librenms_plugin", "cache_timeout", 300)
        return real_init(self, *args, **kwargs)

    with patch.object(api_mod.LibreNMSAPI, "__init__", spy_init):
        make_recording_api("http://127.0.0.1:9", server_key="test")

    # 'servers' is the injected mock; every other key faithfully delegates to the real config.
    assert "test" in captured["servers"]
    assert captured["other"] == expected_other


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
    # Reject booleans the same way recording_schema_errors() does: bool is a subclass of int, so
    # `== 1` / `isinstance(..., int)` alone would let True/False pass this contract check.
    assert isinstance(recording.get("schema_version"), int) and not isinstance(recording.get("schema_version"), bool)
    assert recording.get("schema_version") == 1
    assert recording.get("name")
    assert isinstance(recording.get("device_id"), int) and not isinstance(recording.get("device_id"), bool)
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


def _assert_serial_ports(api, device_id, expected, sensor_types):
    """Replay serial rows with the recording's OWN snapshot map (serial_type_patterns).

    Recognition lives in the SerialSensorTypePattern table, so replaying against the live
    table would tie a checked-in recording's outcome to local DB state; the embedded map
    keeps the outcome self-sufficient (and this test DB-independent).
    """
    from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

    ok, sensors = api.get_serial_port_sensors(device_id, sensor_types=sensor_types)
    assert ok, sensors
    rows = map_sensors_to_serial_links(sensors, device_id=device_id, sensor_types=sensor_types)
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
        _assert_serial_ports(api, device_id, expected["serial_ports"], recording["serial_type_patterns"])

    if "oob" in expected:
        _assert_oob(api, recording, expected["oob"])
