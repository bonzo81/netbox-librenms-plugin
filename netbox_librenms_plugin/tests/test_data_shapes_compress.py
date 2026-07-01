"""Tests for data-shape port compression: collapse redundant cardinality, preserve outcome + signature.

The invariants: resolve_port_relationships and compute_shape_signature produce identical output for
the full and compressed recordings (verified by replaying both over real HTTP), while redundant
high-cardinality ports are dropped.
"""

from netbox_librenms_plugin.data_shapes.compress import compress_recording
from netbox_librenms_plugin.data_shapes.signature import compute_shape_signature


def _port(port_id, name, iftype, **extra):
    p = {"port_id": port_id, "ifName": name, "ifType": iftype}
    p.update(extra)
    return p


def _large_recording():
    """A recording exercising LAG (direct + base-name-resolved), a sub-interface, and 200 boring ports.

    Relationship-bearing ports (101/102 direct LAG, 205/206 sub-interface, 301/303 junos-style LAG whose
    physical members 300/302 are reached only by base-name resolution) sit among 200 near-identical
    access ports that carry VLAN data but no relationships — pure cardinality the compressor should drop.
    """
    ports = [
        _port(102, "lag-1", "ieee8023adLag"),  # LAG aggregate (first ieee8023adLag → name_prefix source)
        _port(101, "1/1/c1/1", "ethernetCsmacd"),  # its member
        _port(205, "ae10", "ieee8023adLag"),  # sub-interface parent
        _port(206, "ae10.2221", "l2vlan"),  # sub-interface child
        _port(300, "xe-0/0/0", "ethernetCsmacd"),  # base of 301, reached only by name resolution
        _port(301, "xe-0/0/0.0", "ethernetCsmacd"),  # junos LAG member (sub-unit)
        _port(302, "ae1", "ieee8023adLag"),  # base of 303 (the aggregate), reached by name resolution
        _port(303, "ae1.0", "ieee8023adLag"),  # junos LAG aggregate (sub-unit)
    ]
    # 200 access ports — all the same shape (ethernetCsmacd + VLAN), no relationships. Redundant.
    for i in range(200):
        ports.append(_port(1000 + i, f"access-{i}", "ethernetCsmacd", ifVlan=10 + (i % 5)))

    mappings = [
        {"high_port_id": 101, "low_port_id": 102},  # member -> lag-1
        {"high_port_id": 205, "low_port_id": 206},  # ae10 -> ae10.2221 (sub)
        {"high_port_id": 301, "low_port_id": 303},  # xe-0/0/0.0 -> ae1.0 (resolves to phys 300 -> 302)
    ]
    return {
        "schema_version": 1,
        "name": "big",
        "description": "",
        "meta": {"os": "junos"},
        "device_id": 4242,
        "responses": {
            "GET /api/v0/devices/4242": {"status": "ok", "devices": [{"device_id": 4242, "os": "junos"}]},
            "GET /api/v0/devices/4242/ports": {"status": "ok", "ports": ports},
            "GET /api/v0/devices/4242/port_stack": {"status": "ok", "mappings": mappings},
        },
    }


def _resolve(recording, recording_server):
    server, api = recording_server(recording)
    _ok, ports = api.get_ports(recording["device_id"])
    _ok2, stack = api.get_port_stack(recording["device_id"])
    rel = api.resolve_port_relationships(ports["ports"], stack, lag_patterns={})
    return (
        {str(k): str(v) for k, v in rel["lag_members"].items()},
        {str(k): str(v) for k, v in rel["sub_interfaces"].items()},
    )


def test_compression_preserves_relationship_outcome(recording_server):
    """resolve_port_relationships yields the same LAG/sub maps before and after compression."""
    rec = _large_recording()
    full_lag, full_sub = _resolve(rec, recording_server)
    comp_lag, comp_sub = _resolve(compress_recording(rec), recording_server)

    assert full_lag == comp_lag
    assert full_sub == comp_sub
    # Non-vacuous: both relationship kinds are really exercised, incl. the base-name-resolved LAG.
    assert full_lag == {"101": "102", "300": "302"}
    assert full_sub == {"206": "205"}


def test_compression_preserves_signature():
    """The novelty signature is identical for the full and compressed recordings."""
    rec = _large_recording()
    assert compute_shape_signature(compress_recording(rec)) == compute_shape_signature(rec)


def test_compression_drops_redundant_cardinality():
    """The 200 same-shape access ports collapse to a single representative; relationship ports stay."""
    rec = _large_recording()
    comp = compress_recording(rec)

    full_ports = rec["responses"]["GET /api/v0/devices/4242/ports"]["ports"]
    comp_ports = comp["responses"]["GET /api/v0/devices/4242/ports"]["ports"]
    assert len(full_ports) == 208
    # 8 relationship/structural ports + exactly 1 access representative = 9.
    assert len(comp_ports) == 9

    kept_ids = {p["port_id"] for p in comp_ports}
    # Every port named by port_stack survives (referential integrity)...
    assert {101, 102, 205, 206, 301, 303}.issubset(kept_ids)
    # ...as do the base-name ports reached only by .N resolution...
    assert {300, 302}.issubset(kept_ids)
    # ...and exactly one of the 200 access ports remains.
    assert len([p for p in comp_ports if p["ifName"].startswith("access-")]) == 1

    assert comp["meta"]["compressed_ports"] == {"from": 208, "to": 9}


def test_compression_keeps_transceiver_linked_ports():
    """A port referenced by a transceiver survives compression even when fingerprint-dedup would drop it — otherwise the transceiver dangles and the replay is no longer self-consistent."""
    rec = _large_recording()
    # Point a transceiver at access-50 (port_id 1050), one of the redundant same-fingerprint access
    # ports that dedup would otherwise collapse into the access-0 representative.
    rec["responses"]["GET /api/v0/devices/4242/transceivers"] = {
        "status": "ok",
        "transceivers": [{"port_id": 1050, "type": "QSFP28", "serial": "SN-x", "model": "M1"}],
    }

    comp = compress_recording(rec)
    comp_ports = comp["responses"]["GET /api/v0/devices/4242/ports"]["ports"]
    comp_ids = {p["port_id"] for p in comp_ports}

    # The transceiver's port is kept (referential integrity)...
    assert 1050 in comp_ids
    # ...while the OTHER 198 redundant access ports still collapse: only access-0 (fingerprint
    # representative) and access-50 (transceiver-linked) remain.
    access_kept = sorted(p["ifName"] for p in comp_ports if p["ifName"].startswith("access-"))
    assert access_kept == ["access-0", "access-50"]


def test_compression_is_noop_without_redundancy():
    """A recording with no droppable ports is returned unchanged (no meta annotation added)."""
    rec = _large_recording()
    # Keep only the structural ports — every one is relationship-bearing or a distinct shape.
    structural = rec["responses"]["GET /api/v0/devices/4242/ports"]["ports"][:8]
    rec["responses"]["GET /api/v0/devices/4242/ports"]["ports"] = structural

    comp = compress_recording(rec)

    assert comp is rec  # untouched
    assert "compressed_ports" not in comp["meta"]


def test_compression_no_ports_route_is_noop():
    """A recording without a ports response is returned unchanged."""
    rec = {"schema_version": 1, "name": "x", "device_id": 1, "meta": {}, "responses": {}}
    assert compress_recording(rec) is rec


def test_compression_targets_main_device_not_oob_controller():
    """With an OOB controller's /ports route present, compression trims the host's ports, not the OOB's."""
    host_ports = [_port(i, f"eth{i}", "ethernetCsmacd", ifVlan=10) for i in range(20)]  # redundant
    oob_ports = [_port(900 + i, f"oob{i}", "ethernetCsmacd") for i in range(10)]
    rec = {
        "schema_version": 1,
        "name": "host-oob",
        "device_id": 39,
        "meta": {"os": "linux", "oob_id": 25},
        "responses": {
            "GET /api/v0/devices/39/ports": {"status": "ok", "ports": host_ports},
            "GET /api/v0/devices/25/ports": {"status": "ok", "ports": oob_ports},
        },
    }
    comp = compress_recording(rec)

    # The host's 20 same-shape ports collapse to 1; the OOB controller's 10 ports are untouched.
    assert len(comp["responses"]["GET /api/v0/devices/39/ports"]["ports"]) == 1
    assert len(comp["responses"]["GET /api/v0/devices/25/ports"]["ports"]) == 10
    assert comp["meta"]["compressed_ports"] == {"from": 20, "to": 1}


def test_compression_vlan_axis_stays_in_lockstep_with_signature():
    """A no-VLAN port (ifVlan None) must not share a fingerprint with a real-VLAN port and collapse it; the compressed recording's vlans signature must equal the full one's."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 7,
        "meta": {"os": "ios"},
        "responses": {
            "GET /api/v0/devices/7": {"status": "ok", "devices": [{"device_id": 7, "os": "ios"}]},
            "GET /api/v0/devices/7/ports": {
                "status": "ok",
                "ports": [
                    # Same ifType/naming, differing only in VLAN value. A key-presence fingerprint would
                    # treat them as one shape and keep only the first (no-VLAN) representative.
                    _port(1, "Gi0/1", "ethernetCsmacd", ifVlan=None),
                    _port(2, "Gi0/2", "ethernetCsmacd", ifVlan=10),
                ],
            },
        },
    }
    full = compute_shape_signature(rec)
    comp = compute_shape_signature(compress_recording(rec))
    assert full["vlans"] is True  # the recording genuinely carries VLAN data (Gi0/2)
    assert comp["vlans"] == full["vlans"]  # compression must not flip the vlans axis to False


def test_fingerprint_detects_subinterface_named_in_ifdescr():
    """_fingerprint must scan BOTH name fields (like the retention path's _port_names).

    An ifDescr-mode sub-interface ('.N' in ifDescr, ifName blank) must fingerprint distinctly from a
    non-subinterface — otherwise dedup could fold it into a non-sub representative, dropping the
    base/sub port a downstream _port_names consumer (the resolver) needs.
    """
    from netbox_librenms_plugin.data_shapes.compress import _fingerprint

    base = {"port_id": 1, "ifName": "", "ifType": "ethernetCsmacd", "ifDescr": "ge-1/0/0"}
    sub = {"port_id": 2, "ifName": "", "ifType": "ethernetCsmacd", "ifDescr": "ge-1/0/0.5"}
    # Same ifType + no VLAN, so only the sub-interface naming axis (index 1) can distinguish them.
    assert _fingerprint(base)[1] is False
    assert _fingerprint(sub)[1] is True
    assert _fingerprint(base) != _fingerprint(sub)


def _ifdescr_mode_recording():
    """An ifDescr-mode junos recording: the structured ``.N`` names live in ifDescr, ifName is blank.

    The aggregate's physical base ports (500 ``ge-1/0/0`` and 502 ``ae5``) are reached ONLY by
    stripping the ``.N`` off the port_stack-referenced sub-units (501 ``ge-1/0/0.0`` and 503
    ``ae5.0``) and looking the base up by name — a name that lives in ifDescr. The bases are ordered
    AFTER the sub-units and share their fingerprints, so fingerprint-dedup drops them unless
    _add_base_name_ports keeps them; an ifName-only scan misses them entirely.
    """
    ports = [
        _port(501, "", "ethernetCsmacd", ifDescr="ge-1/0/0.0"),  # member sub-unit (port_stack ref)
        _port(503, "", "ieee8023adLag", ifDescr="ae5.0"),  # aggregate sub-unit (port_stack ref)
        _port(500, "", "ethernetCsmacd", ifDescr="ge-1/0/0"),  # member base — reached only via ifDescr
        _port(502, "", "ieee8023adLag", ifDescr="ae5"),  # aggregate base — reached only via ifDescr
    ]
    # Redundant same-shape access ports (distinct fingerprint: they carry VLAN) so the fixed path
    # still compresses — proving the bases survive REAL compression, not a no-op.
    for i in range(3):
        ports.append(_port(1000 + i, "", "ethernetCsmacd", ifDescr=f"ge-2/0/{i}", ifVlan=10))
    mappings = [{"high_port_id": 501, "low_port_id": 503}]  # ge-1/0/0.0 -> ae5.0 (phys 500 -> 502)
    return {
        "schema_version": 1,
        "name": "ifdescr",
        "description": "",
        "meta": {"os": "junos"},
        "device_id": 4242,
        "responses": {
            "GET /api/v0/devices/4242": {"status": "ok", "devices": [{"device_id": 4242, "os": "junos"}]},
            "GET /api/v0/devices/4242/ports": {"status": "ok", "ports": ports},
            "GET /api/v0/devices/4242/port_stack": {"status": "ok", "mappings": mappings},
        },
    }


def test_compression_keeps_ifdescr_mode_base_ports(recording_server):
    """Base ports reached only via a ``.N`` name in ifDescr survive compression, so the resolver yields the same relationships before and after — an ifName-only base scan silently dropped them."""
    rec = _ifdescr_mode_recording()
    full_lag, full_sub = _resolve(rec, recording_server)
    comp = compress_recording(rec)
    comp_lag, comp_sub = _resolve(comp, recording_server)

    # Non-vacuous: the full recording really resolves a relationship through the ifDescr-only bases.
    assert full_lag or full_sub
    # The invariant: identical resolved relationships before and after compression.
    assert full_lag == comp_lag
    assert full_sub == comp_sub
    # Compression actually ran (redundant access ports dropped) yet BOTH ifDescr-only base ports
    # survived — the exact ports an ifName-only base scan would have dropped.
    comp_ids = {p["port_id"] for p in comp["responses"]["GET /api/v0/devices/4242/ports"]["ports"]}
    assert {500, 502}.issubset(comp_ids)
    assert "compressed_ports" in comp["meta"]


def test_build_name_index_scans_ifdescr_and_drops_ambiguous():
    """_build_name_index keys ports by ifDescr as well as ifName, and drops a name two different ports share so it can't bind a base/sub-unit lookup to the wrong port."""
    from netbox_librenms_plugin.data_shapes.compress import _build_name_index

    ports = [
        {"port_id": 1, "ifName": "", "ifDescr": "ae1"},  # name lives only in ifDescr
        {"port_id": 2, "ifName": "xe-0/0/0", "ifDescr": ""},  # name lives only in ifName
        {"port_id": 3, "ifName": "dup", "ifDescr": ""},  # 'dup' shared by 3 and 4 -> ambiguous
        {"port_id": 4, "ifName": "dup", "ifDescr": ""},
    ]
    index = _build_name_index(ports)
    # An ifDescr-only and an ifName-only name are both indexed to their single owning port.
    assert index["ae1"]["port_id"] == 1
    assert index["xe-0/0/0"]["port_id"] == 2
    # A name two distinct ports carry is dropped entirely (can't disambiguate which port it means).
    assert "dup" not in index
