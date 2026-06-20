"""Tests for data-shape signatures + novelty classification."""

from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording
from netbox_librenms_plugin.data_shapes.signature import (
    build_manifest,
    classify_novelty,
    compute_shape_signature,
)
from netbox_librenms_plugin.tests.recordings import iter_recordings, load_recording


def test_signature_cisco_stackwise():
    """A Cisco StackWise recording fingerprints as a 3-member, 1-based, stack-rooted VC."""
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    assert sig["os"] == "ios"
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
    assert sig["os"] == "junos"
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


def test_os_family_groups_cisco_variants():
    """Cisco ios and nxos collapse to one family, so a covered ios VC marks an nxos VC likely-covered."""
    manifest = build_manifest([load_recording("cisco-stackwise-3member")])
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    sig["os"] = "nxos"  # same family as the covered ios shape
    assert classify_novelty(sig, manifest)["verdict"] == "likely-covered"
