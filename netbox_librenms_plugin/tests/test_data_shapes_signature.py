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
    # Real VLAN data → vlans True.
    assert compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "ifVlan": 10}]))["vlans"] is True
    assert compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "vlans": [10, 20]}]))["vlans"] is True


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
