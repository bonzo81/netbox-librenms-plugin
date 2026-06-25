"""Tests for data-shape signatures + novelty classification."""

from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording, pseudonymize_os
from netbox_librenms_plugin.data_shapes.signature import (
    build_manifest,
    classify_novelty,
    compute_shape_signature,
)
from netbox_librenms_plugin.tests.recordings import iter_recordings, load_recording


def test_signature_cisco_stackwise():
    """A Cisco StackWise recording fingerprints as a 3-member, 1-based, stack-rooted VC."""
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    assert sig["os"] == pseudonymize_os("ios")  # OS is pseudonymized in the recording
    assert sig["virtual_chassis"] == {
        "present": True,
        "root_class": "stack",
        "member_count": 3,
        "position_base": 1,
    }
    assert sig["lag"]["present"] is False
    assert sig["sub_interfaces"]["present"] is False


def test_signature_juniper_vc_zero_based_chassis_root():
    """A Juniper VC fingerprints as a 2-member, 0-based, chassis-rooted VC."""
    sig = compute_shape_signature(load_recording("juniper-vc-2member"))
    assert sig["os"] == pseudonymize_os("junos")  # OS is pseudonymized in the recording
    assert sig["virtual_chassis"]["root_class"] == "chassis"
    assert sig["virtual_chassis"]["member_count"] == 2
    assert sig["virtual_chassis"]["position_base"] == 0


def test_signature_lag_and_subinterface():
    """The LAG recording fingerprints LAG (ieee8023ad, Port-channel) + dot-numeric sub-interfaces."""
    sig = compute_shape_signature(load_recording("cisco-lag-and-subinterface"))
    assert sig["virtual_chassis"]["present"] is False
    assert sig["lag"] == {"present": True, "ieee8023ad": True, "name_prefix": "Port-channel"}
    assert sig["sub_interfaces"] == {"present": True, "styles": ["dot-numeric"]}
    assert sig["port_stack"] is True


def test_signature_vlans_requires_actual_vlan_data():
    """The vlans axis must reflect real VLAN data, not mere key presence — an empty/null ifVlan must not flip it true."""

    def _rec(ports):
        return {
            "schema_version": 1,
            "name": "x",
            "device_id": 1,
            "responses": {"GET /api/v0/devices/1/ports": {"status": "ok", "ports": ports}},
        }

    # Keys present but carrying no data → vlans False.
    assert (
        compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "ifVlan": None, "vlans": []}]))["vlans"]
        is False
    )
    assert compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "ifVlan": ""}]))["vlans"] is False
    # ifVlan 0 is LibreNMS's no-/default-VLAN sentinel on an access port, not real VLAN data → False.
    assert compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "ifVlan": 0}]))["vlans"] is False
    # Real VLAN data → vlans True.
    assert compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "ifVlan": 10}]))["vlans"] is True
    assert compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "vlans": [10, 20]}]))["vlans"] is True


def test_signature_handles_null_meta():
    """A recording with explicit meta:null must not crash; os falls back to the device row."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "meta": None,  # the recording schema does not validate meta, so a null can reach here
        "responses": {
            "GET /api/v0/devices/1": {"status": "ok", "devices": [{"device_id": 1, "os": "ios"}]},
            "GET /api/v0/devices/1/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "Gi0/1"}]},
        },
    }
    sig = compute_shape_signature(rec)  # must not raise AttributeError on meta.get(...)
    assert sig["os"] == "ios"


def test_signature_reads_host_ports_not_oob_controller_ports():
    """With a host and an OOB-controller /ports route, the signature fingerprints the host's ports (anchored on device_id), not whichever route comes first in dict order."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "responses": {
            # OOB controller's ports FIRST — a loose .endswith('/ports') would fingerprint these.
            "GET /api/v0/devices/2500/ports": {
                "status": "ok",
                "ports": [{"port_id": 9001, "ifName": "Bundle-Ether1", "ifType": "ieee8023adLag"}],
            },
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "Gi0/1", "ifType": "ethernetCsmacd"}],
            },
        },
    }
    # Only the OOB controller carries a LAG; the host does not. Anchoring on the host → lag absent.
    assert compute_shape_signature(rec)["lag"]["present"] is False


def test_signature_oob_axis_reflects_meta_oob_id():
    """OOB presence is an axis: meta.oob_id set → oob True, absent → oob False."""
    base = {"schema_version": 1, "name": "x", "device_id": 1, "responses": {}}
    assert compute_shape_signature({**base, "meta": {"os": "linux", "oob_id": 25}})["oob"] is True
    assert compute_shape_signature({**base, "meta": {"os": "linux"}})["oob"] is False
    assert compute_shape_signature({**base, "meta": None})["oob"] is False


def test_oob_and_non_oob_host_are_distinct_novelty_buckets():
    """An OOB-controller capture must not be reported as covered by the plain-host shape.

    linux-host and linux-host-oob share os/VC/LAG/sub-interface shape, so before OOB became a
    structural axis the OOB capture classified as 'likely-covered' by the plain host (and vice
    versa) — collapsing two genuinely different topologies into one novelty bucket.
    """
    oob_sig = compute_shape_signature(load_recording("linux-host-oob"))
    plain_sig = compute_shape_signature(load_recording("linux-host"))
    assert oob_sig["oob"] is True
    assert plain_sig["oob"] is False

    # With only the plain host known, the OOB capture is genuinely new (different shape axes).
    assert classify_novelty(oob_sig, build_manifest([load_recording("linux-host")]))["verdict"] == "new"
    # ...and symmetrically the plain host is new against an OOB-only manifest.
    assert classify_novelty(plain_sig, build_manifest([load_recording("linux-host-oob")]))["verdict"] == "new"


def test_signature_transceivers_requires_non_empty_list():
    """The transceivers axis must reflect an actual non-empty transceivers list, not a captured error/404 body — a present-but-empty body must not inflate the novelty signature."""

    def _rec(transceiver_body):
        return {
            "schema_version": 1,
            "name": "x",
            "device_id": 1,
            "responses": {
                "GET /api/v0/devices/1/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "Gi0/1"}]},
                "GET /api/v0/devices/1/transceivers": transceiver_body,
            },
        }

    # An error/404 body or an empty list is captured-but-empty → transceivers False.
    assert compute_shape_signature(_rec({"status": "error", "message": "not found"}))["transceivers"] is False
    assert compute_shape_signature(_rec({"transceivers": []}))["transceivers"] is False
    # A real non-empty transceivers list → True.
    assert compute_shape_signature(_rec({"transceivers": [{"id": 1, "type": "sfp"}]}))["transceivers"] is True


def test_signature_stable_under_anonymization():
    """Anonymization preserves all structural fields, so the signature is unchanged."""
    rec = load_recording("cisco-stackwise-3member")
    assert compute_shape_signature(rec) == compute_shape_signature(anonymize_recording(rec))


def test_classify_novelty_likely_covered_for_known_shape():
    """A signature whose axes match a manifest entry is reported likely-covered with the closest name."""
    manifest = build_manifest(iter_recordings())
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    verdict = classify_novelty(sig, manifest)
    assert verdict["verdict"] == "likely-covered"
    assert verdict["closest"] == "cisco-stackwise-3member"


def test_classify_novelty_new_for_unseen_axes():
    """A shape with axes no manifest entry shares is reported new."""
    manifest = build_manifest(iter_recordings())
    # An Arista device that is BOTH a VC and has LAG + sub-interfaces — not covered by the seeds.
    novel = {
        "os": "eos",
        "virtual_chassis": {"present": True, "root_class": "stack", "member_count": 2, "position_base": 1},
        "lag": {"present": True, "ieee8023ad": True, "name_prefix": "Port-Channel"},
        "sub_interfaces": {"present": True, "styles": ["dot-numeric"]},
        "port_stack": True,
        "vlans": False,
        "transceivers": False,
    }
    verdict = classify_novelty(novel, manifest)
    assert verdict["verdict"] == "new"
    assert verdict["closest"] is None


def test_classify_novelty_similar_for_related_os_variant():
    """A different cisco OS variant with the same shape is 'similar' — neither fully covered nor new."""
    manifest = build_manifest([load_recording("cisco-stackwise-3member")])  # ios VC
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    sig["os"] = "iosxr"  # same vendor family as the covered ios shape, but a distinct OS
    verdict = classify_novelty(sig, manifest)
    assert verdict["verdict"] == "similar"
    assert verdict["closest"] == "cisco-stackwise-3member"


def test_classify_novelty_exact_os_is_covered_not_merely_similar():
    """The same OS + same shape is 'likely-covered' (the stronger verdict wins over 'similar')."""
    manifest = build_manifest([load_recording("cisco-stackwise-3member")])
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    assert classify_novelty(sig, manifest)["verdict"] == "likely-covered"


def test_classify_novelty_new_for_different_vendor_same_shape():
    """A different VENDOR with the same shape is 'new' — variants relate, vendors don't."""
    manifest = build_manifest([load_recording("cisco-stackwise-3member")])  # cisco VC
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    sig["os"] = "junos"  # different vendor family, same VC shape
    assert classify_novelty(sig, manifest)["verdict"] == "new"


def test_classify_novelty_distinguishes_vc_member_count():
    """A capture identical except for VC member_count must not be reported 'likely-covered' — member_count is a structural axis, so the narrower set of axes would wrongly collapse them."""
    manifest = build_manifest([load_recording("cisco-stackwise-3member")])
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    vc = sig["virtual_chassis"]
    assert vc["member_count"] >= 2  # the recording really is a multi-member VC
    sig["virtual_chassis"] = {**vc, "member_count": vc["member_count"] - 1}  # same OS/shape, fewer members
    assert classify_novelty(sig, manifest)["verdict"] != "likely-covered"


def test_classify_novelty_distinguishes_transceiver_presence():
    """A capture identical except for transceiver presence must not be reported 'likely-covered' — transceivers is a structural axis the narrower set dropped."""
    manifest = build_manifest([load_recording("cisco-stackwise-3member")])
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    sig["transceivers"] = not sig["transceivers"]  # flip optics presence, keep OS + everything else
    assert classify_novelty(sig, manifest)["verdict"] != "likely-covered"


def test_classify_novelty_skips_malformed_manifest_entries():
    """A malformed manifest item (a non-dict entry, or a null/non-dict signature) must be skipped, not crash novelty eval — the valid sibling entry is still matched."""
    good = build_manifest([load_recording("cisco-stackwise-3member")])
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    # Mix malformed items (a bare string, a null signature, a missing signature) before the
    # valid entry so an unguarded .get()/_structural_axes() would raise on the first one.
    malformed_manifest = ["not-a-dict", {"name": "x", "signature": None}, {"name": "y"}, *good]
    verdict = classify_novelty(sig, malformed_manifest)  # must not raise
    assert verdict["verdict"] == "likely-covered"
    assert verdict["closest"] == "cisco-stackwise-3member"
