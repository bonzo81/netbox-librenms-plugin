"""
Outcome tests driven by captured LibreNMS data-shape recordings.

Each recording in ``data_shapes/recordings/*.json`` is replayed through the mock
LibreNMS HTTP server and the real LibreNMSAPI client, then the real detection
and relationship-resolution logic runs against it and is asserted against the
recording's ``expected`` block. A new recording with an ``expected`` block
becomes a passing test with no new code.

The flow is exercised end-to-end (real client, real HTTP, real parsing); only
the plugin-config lookup is stubbed. VC detection reads the serial normalization
rules and the member-name pattern, so the outcome tests use the test database.
"""

from io import StringIO
import json
from threading import Barrier

import pytest

from netbox_librenms_plugin.constants import (
    GENERIC_CONTAINER_MODELS,
    LIBRENMS_EMPTY_MESSAGES,
    LIBRENMS_PLACEHOLDER_VALUES,
)
from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording
from netbox_librenms_plugin.data_shapes.envelope import unwrap_response
from netbox_librenms_plugin.data_shapes.ports import compile_lag_patterns, compile_sap_patterns

from netbox_librenms_plugin.tests.conftest import run_in_threads
from netbox_librenms_plugin.tests.recordings import iter_recording_paths, iter_recordings

_RECORDINGS = iter_recordings()


def _ids(recording):
    return recording.get("name", "unnamed")


def test_recordings_present():
    """The recordings directory must contain at least one scenario."""
    assert iter_recording_paths(), "no recording JSON files found in data_shapes/recordings/"


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_empty_responses_replay_as_success(recording_server, recording):
    _server, api = recording_server(recording)
    readers = {
        f"GET /api/v0/devices/{recording['device_id']}/links": (api.get_device_links, "links"),
        f"GET /api/v0/devices/{recording['device_id']}/ip": (api.get_device_ips, "addresses"),
        "GET /api/v0/routing/vrf": (api.get_device_vrfs, "vrfs"),
        f"GET /api/v0/inventory/{recording['device_id']}/all": (api.get_device_inventory, "inventory"),
    }
    for route, (reader, field) in readers.items():
        if route not in recording["responses"]:
            continue
        status, body = unwrap_response(recording["responses"][route])
        if status == 404 or (status == 200 and body.get(field) == []):
            success, result = reader(recording["device_id"])
            assert success, (route, result)
            assert (result["links"] if field == "links" else result) == []
    route = "GET /api/v0/resources/sensors"
    if route in recording["responses"]:
        status, body = unwrap_response(recording["responses"][route])
        if status == 404 or (status == 200 and body.get("sensors") == []):
            assert api.get_serial_port_sensors(recording["device_id"], sensor_types={"serial": "Port {N}"}) == (
                True,
                [],
            )


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_response_labels_and_revisions_are_anonymized(recording):
    import re

    def check(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and value and value != "-":
                    if key in {"display_template", "display_name", "label"}:
                        assert re.fullmatch(r"device-[0-9a-f]{6}", value), key
                    if key in {"entPhysicalHardwareRev", "revision"}:
                        assert re.fullmatch(r"fw-[0-9a-f]{6}", value), key
                    if key == "message":
                        assert value.casefold() in LIBRENMS_EMPTY_MESSAGES
                    if key == "group":
                        pytest.fail(f"Private response text in {key}")
                check(value)
        elif isinstance(obj, list):
            for value in obj:
                check(value)

    check(recording.get("responses", {}))


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_inventory_display_text_is_anonymized(recording):
    import re

    allowed = re.compile(r"(?:entity-[0-9a-f]{6}(?: \d+(?:/(?:\d+|[xc]\d+))+)?)|(?:\d+(?:/(?:\d+|[xc]\d+))+)")
    # A description may also keep the Junos Virtual Chassis markers that VC detection reads and the
    # Juniper MX "@ fpc/pic/port" locators that the dangling-parent repair reads.
    junos_descr = re.compile(
        r"(?:(?:FPC(?: \d+)? )?entity-[0-9a-f]{6} @ \d+/\d+/\d+)|(?:PIC: entity-[0-9a-f]{6} @ \d+/\d+/\*)"
        r"|(?:FPC: entity-[0-9a-f]{6} @ \d+/\*/\*)|(?:FPC(?: \d+)? entity-[0-9a-f]{6})"
        r"|(?:entity-[0-9a-f]{6} Virtual Chassis)"
    )
    # A Juniper switch control board name keeps the prefix an include rule matches, never the SKU.
    scb_name = re.compile(r"SCB entity-[0-9a-f]{6}")
    # An IOS XE description keeps the leading "Front Panel" a description rule matches, as typed.
    front_panel_descr = re.compile(rf"\s*(?i:front panel)\s*(?:{allowed.pattern})?")
    for key, value in recording.get("responses", {}).items():
        body = unwrap_response(value)[1]
        if "/inventory/" not in key or not isinstance(body, dict):
            continue
        for item in body.get("inventory") or []:
            for field in ("entPhysicalName", "entPhysicalDescr"):
                if isinstance(item, dict) and item.get(field):
                    value = item[field]
                    kept = (
                        field == "entPhysicalDescr"
                        and (junos_descr.fullmatch(value) or front_panel_descr.fullmatch(value))
                    ) or (field == "entPhysicalName" and scb_name.fullmatch(value))
                    assert allowed.fullmatch(value) or kept, (recording["name"], field, value)


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_ipv6_representations_name_the_same_address(recording):
    from ipaddress import ip_address

    for key, value in recording.get("responses", {}).items():
        body = unwrap_response(value)[1]
        if not key.endswith("/ip") or not isinstance(body, dict):
            continue
        for row in body.get("addresses", []):
            if row.get("ipv6_address") and row.get("ipv6_compressed"):
                assert ip_address(row["ipv6_address"]) == ip_address(row["ipv6_compressed"])


def test_default_stub_catalog_is_listed_in_the_development_readme():
    from pathlib import Path

    from netbox_librenms_plugin.tests.mock_librenms_server import DEFAULT_STUB_RECORDINGS

    readme = (Path(__file__).resolve().parents[2] / ".devcontainer" / "README.md").read_text(encoding="utf-8")
    catalog = readme.split("The default catalog contains these scenarios:", 1)[1].split("The structural device", 1)[0]

    for recording_name in DEFAULT_STUB_RECORDINGS:
        assert f"`{recording_name}`" in catalog


def test_load_recording_rejects_path_traversal():
    """A recording name that escapes the recordings directory must raise ValueError, not read an arbitrary file off disk."""
    from netbox_librenms_plugin.data_shapes.recordings_store import load_recording

    with pytest.raises(ValueError):
        load_recording("../../../../../../etc/passwd")


def test_recording_and_manifest_use_utf8_under_an_ascii_default(monkeypatch, tmp_path):
    from pathlib import Path

    from netbox_librenms_plugin.data_shapes import recordings_store

    original_read_text = Path.read_text

    def ascii_default(path, *args, encoding=None, **kwargs):
        return original_read_text(path, *args, encoding=encoding or "ascii", **kwargs)

    manifest = json.loads(original_read_text(recordings_store.MANIFEST_PATH, encoding="utf-8"))
    manifest[0]["name"] = "caf\u00e9"
    monkeypatch.setattr(Path, "read_text", ascii_default)
    monkeypatch.setattr(recordings_store, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recordings_store, "MANIFEST_PATH", tmp_path / "manifest.json")
    (tmp_path / "unicode.json").write_text('{"name": "caf\u00e9"}', encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    assert recordings_store.load_recording("unicode")["name"] == "caf\u00e9"
    assert recordings_store.load_manifest()[0]["name"] == "caf\u00e9"


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


def test_iter_recordings_rejects_a_non_object_fixture(monkeypatch, tmp_path):
    """Bulk loading must enforce the same object contract as single-record loading."""
    from netbox_librenms_plugin.data_shapes import recordings_store

    monkeypatch.setattr(recordings_store, "RECORDINGS_DIR", tmp_path)
    (tmp_path / "listy.json").write_text("[1, 2, 3]")

    with pytest.raises(ValueError, match="not a recording object"):
        recordings_store.iter_recordings()


def test_recording_schema_errors_rejects_bool_int_fields():
    """Bool is an int subclass; True/False for schema_version or device_id must be rejected — a bare `!= 1` / `isinstance(int)` check would otherwise let a malformed recording validate."""
    from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

    rec = {"schema_version": True, "name": "x", "device_id": False, "responses": {"GET /x": {"status": "ok"}}}
    errors = recording_schema_errors(rec)
    assert any("schema_version must be 1" in e for e in errors)
    assert any("device_id must be an integer" in e for e in errors)


@pytest.mark.parametrize("status", [True, False, "404", None, 404.0, 0, 99, 600, 999])
def test_recording_schema_errors_rejects_malformed_status_envelopes(status):
    """A present status marker must be a non-Boolean integer inside the replayable HTTP range."""
    from netbox_librenms_plugin.data_shapes.envelope import BODY_KEY, STATUS_KEY, unwrap_response
    from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

    response = {STATUS_KEY: status, BODY_KEY: {"status": "error"}}
    recording = {
        "schema_version": 1,
        "name": "malformed-status-envelope",
        "device_id": 1,
        "responses": {"GET /api/v0/devices/1": response},
    }

    assert any(STATUS_KEY in error for error in recording_schema_errors(recording))
    with pytest.raises(ValueError, match="status envelope"):
        unwrap_response(response)


@pytest.mark.parametrize(
    "expected",
    [
        {"lag_member": {"302": "301"}},
        {"virtual_chassis": None, "lag_member": {"302": "301"}},
    ],
)
def test_recording_schema_errors_rejects_unknown_expected_outcomes(expected):
    """Unknown outcome names must not let a recording pass without a matching assertion."""
    from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

    recording = {
        "schema_version": 1,
        "name": "unknown-outcome",
        "device_id": 1,
        "responses": {"GET /api/v0/devices/1": {"status": "ok"}},
        "expected": expected,
    }

    assert any("unknown" in error for error in recording_schema_errors(recording))


@pytest.mark.parametrize("expected", [None, {}, []])
def test_recording_schema_errors_rejects_malformed_present_expected(expected):
    """An explicitly present expected field must contain at least one outcome object."""
    from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

    recording = {
        "schema_version": 1,
        "name": "malformed-outcomes",
        "device_id": 1,
        "responses": {"GET /api/v0/devices/1": {"status": "ok"}},
        "expected": expected,
    }

    assert any("non-empty object" in error for error in recording_schema_errors(recording))


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

    recording = load_recording("cisco-lag-and-subinterface")
    _server, api = recording_server(recording)
    success, data = api.get_ports(device_id=recording["device_id"])

    assert success is True
    ports = data.get("ports")
    assert isinstance(ports, list) and len(ports) == 4  # the recording's real port count
    # The columns get_ports() actually requests survive the real fetch+parse round-trip.
    assert all(isinstance(p, dict) and "port_id" in p and "ifName" in p for p in ports)
    assert "Port-channel1" in {p["ifName"] for p in ports}


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_recording_carries_no_residual_pii(recording):
    """Verify committed recordings remain anonymized and contain no residual PII."""
    from netbox_librenms_plugin.data_shapes.anonymize import find_pii

    assert find_pii(recording) == []


def _assert_recording_has_no_public_asn(recording):
    """Reject integer BGP ASNs outside the private 16-bit range."""
    from netbox_librenms_plugin.data_shapes.anonymize import BGP_KEYS

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                asn = int(v) if isinstance(v, str) and v.isdigit() else v
                if k in BGP_KEYS and isinstance(asn, int) and not isinstance(asn, bool) and asn != 0:
                    assert 64512 <= asn <= 65534, f"{k}={v} is a public ASN; anonymize it to the private range"
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(recording.get("responses", {}))


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_recording_has_no_public_asn(recording):
    """A committed recording must anonymize identifying BGP ASNs into the private range."""
    _assert_recording_has_no_public_asn(recording)


def test_bundled_recording_guard_rejects_a_digit_string_asn():
    """A JSON string must not bypass the same ASN range check as an integer."""
    recording = {"responses": {"GET /example": {"bgpLocalAs": "64496"}}}

    with pytest.raises(AssertionError, match="public ASN"):
        _assert_recording_has_no_public_asn(recording)


# OUIs that shipped verbatim in the corpus before the anonymizer masked them. 36965 is 0x009065
# (Finisar); re-introducing any of them means a capture bypassed the rule.
_LEAKED_OUIS = frozenset({"36965", "713", "21327", "8172124", "9098", "2589"})


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_recording_has_anonymized_vendor_metadata(recording):
    """
    A committed recording carries the normalized icon and pseudonymized manufacturer names.

    The icon is generic.svg and each manufacturer name is MFG-<hash>. A raw value means the fixture
    predates an anonymizer rule and re-leaks vendor metadata.

    Only response bodies are checked: ``meta.vendor`` is corpus metadata the anonymizer keeps on
    purpose, exactly like ``meta.os``, so it names the platform rather than leaking a device's.
    """
    import re

    mfg_re = re.compile(r"^MFG-[0-9a-f]{6}$")
    model_re = re.compile(r"^MODEL-[0-9a-f]{6}$")
    model_placeholders = GENERIC_CONTAINER_MODELS | LIBRENMS_PLACEHOLDER_VALUES
    leaked = []

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "icon" and isinstance(v, str) and v and v != "images/os/generic.svg":
                    leaked.append((k, v))
                if k in ("entPhysicalMfgName", "vendor") and isinstance(v, str) and v and not mfg_re.match(v):
                    leaked.append((k, v))
                if k in ("hardware", "remote_platform", "entPhysicalModelName", "model") and isinstance(v, str):
                    if v.strip().lower() not in model_placeholders and not model_re.fullmatch(v):
                        leaked.append((k, v))
                # The OUI is the IEEE manufacturer prefix; a registered one names the vendor even
                # when the vendor field is null. Masked values are < 2**24 like real ones, so this
                # cannot check the value itself: it checks the ones known to have shipped raw.
                if k == "oui" and str(v) in _LEAKED_OUIS:
                    leaked.append((k, v))
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(recording.get("responses", {}))
    assert not leaked, f"{recording.get('name')}: un-anonymized vendor metadata: {leaked}"


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_bundled_recording_transceivers_reference_present_ports(recording):
    """Every transceiver must reference a port that survived compression — a dangling port_id means the replay isn't a self-consistent LibreNMS dataset (the transceiver-merge can't map it to a port name)."""

    def _body(suffix):
        for k, v in recording.get("responses", {}).items():
            if k.split("?", 1)[0].endswith(suffix):
                return unwrap_response(v)[1]
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


def test_make_recording_api_delegates_non_servers_config_to_real(settings):
    """The recording API override must preserve every non-server plugin setting."""
    from copy import deepcopy

    import netbox_librenms_plugin.librenms_api as api_mod
    from netbox_librenms_plugin.tests.conftest import make_recording_api

    # A plugin-level value unlike the 300 default, so only a real fallback can produce it.
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config.setdefault("netbox_librenms_plugin", {})["cache_timeout"] = 123
    settings.PLUGINS_CONFIG = plugin_config
    expected_other = 123

    original_config = settings.PLUGINS_CONFIG
    api = make_recording_api("http://127.0.0.1:9", server_key="test")

    assert api.server_key == "test"
    assert api.librenms_url == "http://127.0.0.1:9"
    assert api.cache_timeout == expected_other
    assert settings.PLUGINS_CONFIG is original_config
    assert api_mod.get_plugin_config("netbox_librenms_plugin", "cache_timeout", 300) == expected_other


def test_manifest_is_in_sync_with_bundled_recordings():
    """Verify the manifest matches bundled recordings so novelty detection cannot become stale."""
    from netbox_librenms_plugin.data_shapes import recordings_store
    from netbox_librenms_plugin.data_shapes.signature import build_manifest

    expected = build_manifest(recordings_store.load_bundled_recordings())
    assert recordings_store.load_manifest() == expected, "run: manage.py librenms_recordings --rebuild-manifest"


def test_concurrent_manifest_rebuilds_do_not_share_a_temporary_file(monkeypatch, tmp_path):
    """Two rebuild commands must each replace their own completed temporary file."""
    from netbox_librenms_plugin.data_shapes import recordings_store
    from netbox_librenms_plugin.management.commands.librenms_recordings import Command

    manifest_path = tmp_path / "manifest.json"
    original_replace = type(manifest_path).replace
    writers_ready = Barrier(2, timeout=10)
    synchronized_writes = []

    def synchronized_replace(path, target):
        if target == manifest_path:
            synchronized_writes.append(path)
            writers_ready.wait(timeout=5)
        return original_replace(path, target)

    monkeypatch.setattr(type(manifest_path), "replace", synchronized_replace)
    monkeypatch.setattr(recordings_store, "MANIFEST_PATH", manifest_path)
    run_in_threads(
        Command(stdout=StringIO())._rebuild_manifest,
        Command(stdout=StringIO())._rebuild_manifest,
        barriers=(writers_ready,),
        timeout=10,
    )

    assert manifest_path.exists()
    assert len(synchronized_writes) == 2
    assert len(set(synchronized_writes)) == 2


def test_load_manifest_fails_when_file_is_unreadable(tmp_path, monkeypatch):
    """An unreadable manifest must not make every captured shape look new."""
    from netbox_librenms_plugin.data_shapes import recordings_store

    manifest_path = tmp_path / "manifest.json"
    manifest_path.mkdir()

    monkeypatch.setattr(recordings_store, "MANIFEST_PATH", manifest_path)
    with pytest.raises(RuntimeError, match="manifest"):
        recordings_store.load_manifest()


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        "{}",
        "null",
        '["old-format"]',
        '[{"name":"x","signature":null}]',
        '[{"name":"x","signature":{"virtual_chassis":null}}]',
    ],
)
def test_load_manifest_fails_when_content_is_invalid(tmp_path, content, monkeypatch):
    """A corrupt or wrong-type manifest must not become an empty coverage set."""
    from netbox_librenms_plugin.data_shapes import recordings_store

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(content)

    monkeypatch.setattr(recordings_store, "MANIFEST_PATH", manifest_path)
    with pytest.raises(RuntimeError, match="manifest"):
        recordings_store.load_manifest()


def test_load_manifest_fails_when_file_is_missing(tmp_path, monkeypatch):
    """A missing packaged manifest must surface the packaging error."""
    from netbox_librenms_plugin.data_shapes import recordings_store

    monkeypatch.setattr(recordings_store, "MANIFEST_PATH", tmp_path / "missing.json")
    with pytest.raises(RuntimeError, match="manifest"):
        recordings_store.load_manifest()


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_recording_has_required_schema(recording):
    """Every recording declares the keys the replay harness depends on."""
    from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

    assert isinstance(recording.get("expected"), dict) and recording["expected"], (
        f"{recording.get('name')} has no expected outcomes to assert"
    )
    assert not (errors := recording_schema_errors(recording)), f"{recording.get('name')}: {errors}"


def _assert_virtual_chassis(api, device_id, expected):
    from netbox_librenms_plugin.import_utils.virtual_chassis import (
        detect_virtual_chassis_from_inventory,
        identify_vc_master,
        vc_serial_key,
    )

    result = detect_virtual_chassis_from_inventory(api, device_id)

    if expected is None:
        assert result is None
        return

    assert result is not None
    assert result["is_stack"] == expected["is_stack"]
    assert result["member_count"] == expected["member_count"]
    if "member_serials" in expected:
        assert [m["serial"] for m in result["members"]] == expected["member_serials"]
    if "member_positions" in expected:
        assert [m["position"] for m in result["members"]] == expected["member_positions"]
    if "master_position" in expected:
        _found, device = api.get_device_info(device_id)
        master = identify_vc_master(result["members"], device["serial"], vc_serial_key())
        assert master["position"] == expected["master_position"]


def _assert_port_relationships(api, device_id, recording, expected):
    ok_ports, ports_data = api.get_ports(device_id)
    assert ok_ports, ports_data
    ok_stack, port_stack = api.get_port_stack(device_id)
    assert ok_stack, port_stack

    relationships = api.resolve_port_relationships(
        ports_data["ports"],
        port_stack,
        # Normalize missing and explicit null patterns so resolution never touches the DB.
        compiled_lag_patterns=compile_lag_patterns(recording),
        # `(meta or {})` not `meta, {}`: an explicit "meta": null (a shape recording_schema_errors()
        # doesn't reject) returns None, and the chained .get("os") would then AttributeError.
        device_os=(recording.get("meta") or {}).get("os"),
        # Supplying compiled LAG patterns keeps resolution off the DB, so the stored SAP
        # rule is not read; feed the recording's own snapshot so replay matches production.
        compiled_sap_patterns=compile_sap_patterns(recording),
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
    """Verify serial rows use the recording's serial_type_patterns map instead of live database state."""
    from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links

    ok, sensors = api.get_serial_port_sensors(device_id, sensor_types=sensor_types)
    assert ok, sensors
    rows = map_sensors_to_serial_links(sensors, device_id=device_id, sensor_types=sensor_types)
    assert len(rows) == expected["count"]
    if "configured" in expected:
        assert sum(1 for r in rows if r["is_configured"]) == expected["configured"]


def _assert_oob(api, recording, expected):
    """The linked OOB controller's ports are recorded and replayable (the interfaces view merges them)."""
    # `(meta or {})` not `meta, {}`: an explicit "meta": null returns None and .get would crash.
    oob_id = (recording.get("meta") or {}).get("oob_id")
    assert oob_id is not None, f"{recording['name']} declares oob outcome but no meta.oob_id"
    ok, oob_data = api.get_ports(oob_id)
    assert ok, oob_data
    assert len(oob_data["ports"]) == expected["controller_ports"]


def test_assert_port_relationships_tolerates_explicit_null_meta():
    """An explicit meta: null recording must normalize to {} in the replay assert, not AttributeError."""
    from types import SimpleNamespace

    api = SimpleNamespace(
        get_ports=lambda device_id: (True, {"ports": []}),
        get_port_stack=lambda device_id: (True, []),
        # **kwargs: the stub must not pin the resolver's keyword list, or every new pattern
        # argument breaks this test instead of the behaviour it guards.
        resolve_port_relationships=lambda ports, stack, **kwargs: {},
    )
    # "meta": null is a shape recording_schema_errors() accepts; os is optional. The helper must
    # read (meta or {}).get("os") — reading meta.get directly would AttributeError on None.
    recording = {"name": "null-meta", "meta": None, "lag_patterns": {}}
    _assert_port_relationships(api, 1, recording, expected={})


def test_assert_port_relationships_tolerates_explicit_null_lag_patterns():
    """An explicit lag_patterns: null recording must not load live database patterns."""

    class RecordingAPI:
        lag_patterns = None

        def get_ports(self, device_id):
            return True, {"ports": []}

        def get_port_stack(self, device_id):
            return True, []

        def resolve_port_relationships(self, ports, stack, compiled_lag_patterns=None, **kwargs):
            self.lag_patterns = compiled_lag_patterns
            self.sap_patterns = kwargs.get("compiled_sap_patterns")
            return {}

    api = RecordingAPI()
    recording = {"name": "null-lag-patterns", "meta": {}, "lag_patterns": None}

    _assert_port_relationships(api, 1, recording, expected={})

    assert api.lag_patterns == []
    # A null lag_patterns must not pull the stored SAP rule from the database either.
    assert api.sap_patterns == []


@pytest.mark.django_db
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
        _assert_serial_ports(api, device_id, expected["serial_ports"], recording.get("serial_type_patterns") or {})

    if "oob" in expected:
        _assert_oob(api, recording, expected["oob"])


@pytest.mark.django_db
def test_serial_outcome_without_patterns_replays_as_empty(recording_server):
    recording = {
        **next(item for item in _RECORDINGS if "serial_ports" in item["expected"]),
        "expected": {"serial_ports": {"count": 0}},
    }
    recording.pop("serial_type_patterns", None)

    test_recording_outcomes(recording, recording_server)


@pytest.mark.django_db
@pytest.mark.parametrize("mapping", [False, True], ids=["module-type", "module-type-mapping"])
def test_anonymized_inventory_resolves_a_token_keyed_module_type(recording_server, mapping):
    from netbox_librenms_plugin.models import ModuleTypeMapping
    from netbox_librenms_plugin.tests.conftest import make_module_type
    from netbox_librenms_plugin.utils import get_module_types_indexed, resolve_module_type

    recording = next(item for item in _RECORDINGS if item["name"] == "cisco-stackwise-3member")
    recording = json.loads(json.dumps(recording))
    device_id = recording["device_id"]
    member_route = next(route for route in recording["responses"] if "entPhysicalContainedIn=1" in route)
    model = recording["responses"][member_route]["inventory"][0]["entPhysicalModelName"]
    recording["responses"][f"GET /api/v0/devices/{device_id}"]["devices"][0]["hardware"] = model
    anonymized = anonymize_recording(recording, salt="module-matching")
    token = anonymized["responses"][f"GET /api/v0/devices/{anonymized['device_id']}"]["devices"][0]["hardware"]
    module_type = make_module_type("Recorded module" if mapping else token)
    if mapping:
        ModuleTypeMapping.objects.create(librenms_model=token, netbox_module_type=module_type)
    _server, api = recording_server(anonymized)

    success, inventory = api.get_inventory_filtered(anonymized["device_id"], ent_physical_contained_in=1)

    assert success and inventory
    assert resolve_module_type(inventory[0]["entPhysicalModelName"], get_module_types_indexed()) == module_type


def test_arcos_recording_preserves_breakout_port_names():
    """The fresh source retains ArcOS breakout names needed by interface inference."""
    from netbox_librenms_plugin.data_shapes.recordings_store import load_recording

    recording = load_recording("arcos-lag-transceivers")
    ports = unwrap_response(recording["responses"][f"GET /api/v0/devices/{recording['device_id']}/ports"])[1]["ports"]
    import re

    assert any(re.fullmatch(r"swp[0-9]+s[0-9]+", port.get("ifName", "")) for port in ports)


@pytest.mark.django_db
@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_anonymized_recording_replays_declared_outcomes(recording, recording_server):
    """ID remapping preserves replay outcomes without changing the bundled source."""
    anonymized = anonymize_recording(recording, salt="recording-replay")

    assert anonymized["device_id"] != recording["device_id"]
    test_recording_outcomes(anonymized, recording_server)

    def bare_ids(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "id":
                    yield child
                else:
                    yield from bare_ids(child)
        elif isinstance(value, list):
            for child in value:
                yield from bare_ids(child)

    original_ids = list(bare_ids(list(recording["responses"].values())))
    anonymous_ids = list(bare_ids(list(anonymized["responses"].values())))
    assert len(original_ids) == len(anonymous_ids)
    assert set(original_ids).isdisjoint(anonymous_ids)
    assert [type(value) for value in original_ids] == [type(value) for value in anonymous_ids]


@pytest.mark.django_db
def test_timos_refresh_and_sync_put_each_ip_interface_under_its_port(client, settings, recording_server):
    """A refresh of the TiMOS recording and a sync write one LAG member and parent the L3 interfaces."""
    from django.urls import reverse
    from dcim.models import Interface

    from netbox_librenms_plugin.models import PortStackLagPattern
    from netbox_librenms_plugin.tests.conftest import bind_librenms_server, make_device
    from netbox_librenms_plugin.tests.interface_sync_post_helpers import SERVER_KEY, post_interface_sync
    from netbox_librenms_plugin.tests.recordings import load_recording
    from netbox_librenms_plugin.tests.view_test_helpers import make_superuser

    recording = load_recording("nokia-timos-transceivers")
    server, _api = recording_server(recording)
    bind_librenms_server(settings, server, server_key=SERVER_KEY)
    device_os = recording["meta"]["os"]
    PortStackLagPattern.objects.update_or_create(
        librenms_os=device_os,
        defaults={
            "lag_name_pattern": recording["lag_patterns"][device_os],
            "sap_name_pattern": recording["sap_patterns"][device_os],
        },
    )
    device = make_device("timos-ip-interfaces", librenms_cf={SERVER_KEY: {"id": recording["device_id"]}})
    client.force_login(make_superuser("timos-ip-interfaces-user"))
    refresh_url = reverse("plugins:netbox_librenms_plugin:device_interface_sync", kwargs={"pk": device.pk})
    assert client.post(refresh_url, {"server_key": SERVER_KEY, "interface_name_field": "ifName"}).status_code == 200

    # lag-2 with members 2/x1/1/c12/1 and 2/x1/1/c36/1 and L3 interface lag2; L3 interface iface-91ad81 on 1/1/c9/1.
    port_ids = [711982042, 1775758580, 1901702767, 627424630, 1461496686, 1354216783]
    post_interface_sync(client, device, port_ids, htmx=False)

    interfaces = {interface.name: interface for interface in Interface.objects.filter(device=device)}
    lag = interfaces["lag-2"]
    assert lag.type == "lag"
    assert {name for name, interface in interfaces.items() if interface.lag_id == lag.pk} == {
        "2/x1/1/c12/1",
        "2/x1/1/c36/1",
    }
    assert (interfaces["lag2"].parent_id, interfaces["lag2"].type) == (lag.pk, "virtual")
    assert interfaces["lag2"].lag_id is None
    assert interfaces["iface-91ad81"].parent_id == interfaces["1/1/c9/1"].pk
