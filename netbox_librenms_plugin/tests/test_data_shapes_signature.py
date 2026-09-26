"""Tests for data-shape signatures + novelty classification."""

import pytest

from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording, pseudonymize_os
from netbox_librenms_plugin.data_shapes.envelope import wrap_response
from netbox_librenms_plugin.data_shapes.signature import (
    build_manifest,
    classify_novelty,
    compute_shape_signature,
    signature_schema_errors,
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


def _vrf_signature_recording(vrfs, *, ports=None):
    return {
        "schema_version": 1,
        "name": "vrf-sig",
        "device_id": 5,
        "meta": {"os": "timos"},
        "responses": {
            "GET /api/v0/devices/5/ports": {
                "status": "ok",
                "ports": ports if ports is not None else [{"port_id": 1, "ifName": "1/1/c1/1", "ifVrf": 7}],
            },
            "GET /api/v0/routing/vrf": {"status": "ok", "vrfs": vrfs},
        },
    }


def test_signature_reports_the_vrf_axis():
    """Without a vrf axis a VRF capture and a plain one of the same OS share a novelty bucket."""
    sig = compute_shape_signature(_vrf_signature_recording([{"vrf_id": 7, "vrf_name": "vrf-abc", "device_id": 5}]))

    assert sig["vrf"] is True


def test_signature_vrf_axis_false_without_the_route():
    """The 11 recordings captured before the VRF routes existed must report vrf: false."""
    sig = compute_shape_signature(load_recording("cisco-stackwise-3member"))

    assert sig["vrf"] is False


def test_signature_vrf_axis_false_on_an_empty_vrf_list():
    """A recorded but empty list is not a VRF shape, mirroring the transceivers axis."""
    assert compute_shape_signature(_vrf_signature_recording([]))["vrf"] is False


def test_signature_vrf_axis_ignores_a_failed_response():
    """A captured non-2xx body carrying vrfs must not count as present data."""
    recording = _vrf_signature_recording([{"vrf_id": 7, "vrf_name": "vrf-abc", "device_id": 5}])
    recording["responses"]["GET /api/v0/routing/vrf"] = wrap_response(
        500, {"status": "ok", "vrfs": [{"vrf_id": 7, "vrf_name": "vrf-abc", "device_id": 5}]}
    )

    assert compute_shape_signature(recording)["vrf"] is False


def test_novelty_separates_a_vrf_shape_from_an_otherwise_identical_one():
    """vrf must be a structural axis, or a VRF capture reads as covered by a non-VRF sibling."""
    with_vrf = _vrf_signature_recording([{"vrf_id": 7, "vrf_name": "vrf-abc", "device_id": 5}])
    without_vrf = _vrf_signature_recording([])
    manifest = build_manifest([{**without_vrf, "name": "plain"}])

    verdict = classify_novelty(compute_shape_signature(with_vrf), manifest)

    assert verdict["verdict"] == "new"


def test_signature_schema_rejects_a_non_boolean_vrf_axis():
    sig = compute_shape_signature(_vrf_signature_recording([]))
    sig["vrf"] = "yes"

    assert any("vrf" in error for error in signature_schema_errors(sig))


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
    # ifVlan is string-valued JSON elsewhere in the client, so the string "0" is the SAME no-VLAN
    # sentinel as the int 0 and must not flip the vlans axis true.
    assert compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "ifVlan": "0"}]))["vlans"] is False
    # Real VLAN data → vlans True.
    assert compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "ifVlan": 10}]))["vlans"] is True
    assert compute_shape_signature(_rec([{"port_id": 1, "ifName": "Gi0/1", "vlans": [10, 20]}]))["vlans"] is True


def test_signature_lag_present_via_configured_pattern():
    """A pattern-matched LAG (name matches a configured per-OS lag_patterns entry, ifType NOT ieee8023adLag) must set lag.present, mirroring resolve_port_relationships — otherwise a pattern-based aggregate collapses into a non-LAG novelty bucket."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "lag_patterns": {"ios": r"^Po\d+$"},
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                # propVirtual, NOT ieee8023adLag — only the configured Cisco "Po\d+" pattern marks it.
                "ports": [{"port_id": 1, "ifName": "Po1", "ifType": "propVirtual"}],
            }
        },
    }
    sig = compute_shape_signature(rec)
    assert sig["lag"]["present"] is True
    assert sig["lag"]["name_prefix"] == "Po"


def test_pattern_lag_prefix_comes_from_the_matching_name():
    """A pattern-only LAG must not fingerprint an unrelated ifName label."""
    recording = {
        "schema_version": 1,
        "name": "pattern-lag-ifdescr",
        "device_id": 1,
        "lag_patterns": {"example-os": r"^Bundle-Ether\d+$"},
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [
                    {
                        "port_id": 1,
                        "ifName": "operator-label",
                        "ifDescr": "Bundle-Ether42",
                        "ifType": "propVirtual",
                    }
                ],
            }
        },
    }

    assert compute_shape_signature(recording)["lag"]["name_prefix"] == "Bundle-Ether"


def test_signature_hardens_untrusted_lag_patterns():
    """`--validate` ingests community-submitted recordings, so compute_shape_signature must not crash on a non-string lag_patterns value and must bound the name it feeds to an untrusted regex (ReDoS)."""
    # (1) A non-string pattern raises TypeError (not re.error) on re.compile; it must be skipped,
    # not crash the command. The only pattern is unusable and the port isn't ieee8023adLag, so no LAG.
    rec_bad = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "lag_patterns": {"ios": None},
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "Po1", "ifType": "propVirtual"}],
            }
        },
    }
    assert compute_shape_signature(rec_bad)["lag"]["present"] is False

    # (2) An interface name longer than the cap is NOT fed to pat.search, bounding catastrophic
    # backtracking. A pattern that matches its prefix therefore does not classify it as a LAG.
    long_name = "Po" + "9" * 500
    rec_long = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "lag_patterns": {"ios": r"^Po"},
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": long_name, "ifType": "propVirtual"}],
            }
        },
    }
    assert compute_shape_signature(rec_long)["lag"]["present"] is False


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


def test_signature_handles_non_dict_meta_and_lag_patterns():
    """Truthy non-dict metadata and LAG patterns degrade like null without crashing validation."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "meta": "bad",  # truthy non-dict: `or {}` does not normalize it
        "lag_patterns": ["^Po"],  # a list has no .values()
        "responses": {
            "GET /api/v0/devices/1": {"status": "ok", "devices": [{"device_id": 1, "os": "ios"}]},
            "GET /api/v0/devices/1/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "Gi0/1"}]},
        },
    }
    sig = compute_shape_signature(rec)  # must not raise AttributeError
    assert sig["os"] == "ios"  # falls back to the device row, like meta:null
    assert sig["lag"]["present"] is False  # unusable lag_patterns = no pattern classification
    assert sig["oob"] is False  # meta.oob_id unreadable → no OOB axis


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
    """OOB and plain-host captures remain new against each other's otherwise identical shape manifests."""
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
    # Start with the real signature schema so new axes cannot be omitted when it grows. Change the
    # structural dimensions to a combination that no bundled recording covers.
    novel = compute_shape_signature(load_recording("cisco-stackwise-3member"))
    novel["os"] = "eos"
    novel["lag"] = {"present": True, "ieee8023ad": True, "name_prefix": "Port-Channel"}
    novel["sub_interfaces"] = {"present": True, "styles": ["dot-numeric"]}
    novel["port_stack"] = True
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
    # Mix malformed items before the valid entry so an unguarded .get()/_structural_axes() would
    # raise on the first one. Crucially this includes a DICT signature with a null/non-dict nested
    # section ({"virtual_chassis": null}, {"lag": "bad"}) — those pass the non-dict-signature guard
    # but still reach _structural_axes(), where dict.get(key, {}) returns the stored None and
    # vc.get(...) used to blow up.
    malformed_manifest = [
        "not-a-dict",
        {"name": "x", "signature": None},
        {"name": "y"},
        {"name": "z", "signature": {"virtual_chassis": None}},
        {"name": "w", "signature": {"lag": "bad", "sub_interfaces": 5}},
        # sub_interfaces IS a dict here, so it passes the non-dict guard, but styles is null —
        # tuple(None) used to raise inside _structural_axes().
        {"name": "v", "signature": {"sub_interfaces": {"present": True, "styles": None}}},
        *good,
    ]
    verdict = classify_novelty(sig, malformed_manifest)  # must not raise
    assert verdict["verdict"] == "likely-covered"
    assert verdict["closest"] == "cisco-stackwise-3member"


def test_signature_pattern_only_lag_is_not_ieee8023ad():
    """lag.ieee8023ad reflects the ifType detection style specifically — a pattern-only LAG must report present=True but ieee8023ad=False, else the manifest falsely claims the 802.3ad-ifType style is covered."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "lag_patterns": {"ios": r"^Po\d+$"},
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "Po1", "ifType": "propVirtual"}],
            }
        },
    }
    sig = compute_shape_signature(rec)
    assert sig["lag"]["present"] is True
    assert sig["lag"]["ieee8023ad"] is False


def test_signature_ieee8023ad_true_for_iftype_lag():
    """A genuine ieee8023adLag ifType keeps the ieee8023ad axis true."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "Port-channel1", "ifType": "ieee8023adLag"}],
            }
        },
    }
    sig = compute_shape_signature(rec)
    assert sig["lag"]["present"] is True
    assert sig["lag"]["ieee8023ad"] is True


def test_signature_subinterface_detected_in_ifdescr():
    """ifDescr-mode devices carry the structured ".N" sub-unit name in ifDescr while ifName is an arbitrary (anonymized) label — the sub-interface axis must scan both name fields like every neighbouring detector."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "iface-4f2a", "ifDescr": "xe-0/0/0.100", "ifType": "l2vlan"}],
            }
        },
    }
    sig = compute_shape_signature(rec)
    assert sig["sub_interfaces"]["present"] is True
    assert sig["sub_interfaces"]["styles"] == ["dot-numeric"]


def test_signature_serial_axis_reflects_captured_sensors():
    """Only a non-empty captured serial sensor list sets the serial signature axis."""
    base = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "responses": {"GET /api/v0/devices/1/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "Gi0/1"}]}},
    }
    assert compute_shape_signature(base)["serial"] is False

    def _with_sensors(sensors):
        return {**base, "responses": {**base["responses"], "GET /api/v0/resources/sensors": {"sensors": sensors}}}

    assert (
        compute_shape_signature(
            _with_sensors([{"sensor_id": 5, "sensor_type": "acsSerialPortTable", "sensor_descr": "ttyS1"}])
        )["serial"]
        is True
    )
    # An empty (or error) sensors body must NOT flip the axis, mirroring the transceivers axis.
    assert compute_shape_signature(_with_sensors([]))["serial"] is False


def test_serial_and_non_serial_host_are_distinct_novelty_buckets():
    """Serial and non-serial captures remain new against each other's otherwise identical shape manifests."""
    base = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "meta": {"os": "linux"},
        "responses": {
            "GET /api/v0/devices/1": {"status": "ok", "devices": [{"device_id": 1, "os": "linux"}]},
            "GET /api/v0/devices/1/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "eth0"}]},
        },
    }
    serial = {
        **base,
        "responses": {
            **base["responses"],
            "GET /api/v0/resources/sensors": {
                "sensors": [{"sensor_id": 5, "sensor_type": "acsSerialPortTable", "sensor_descr": "ttyS1"}]
            },
        },
    }
    serial_sig = compute_shape_signature(serial)
    plain_sig = compute_shape_signature(base)
    assert serial_sig["serial"] is True and plain_sig["serial"] is False
    # With only the plain host known, the serial capture is genuinely new (distinct shape axis)...
    assert classify_novelty(serial_sig, build_manifest([base]))["verdict"] == "new"
    # ...and symmetrically.
    assert classify_novelty(plain_sig, build_manifest([serial]))["verdict"] == "new"


def test_classify_novelty_distinguishes_lag_detection_style():
    """Novelty classification keeps pattern-based and IEEE 802.3ad LAG detection in distinct buckets."""
    pattern_lag = {
        "schema_version": 1,
        "name": "pat",
        "device_id": 1,
        "meta": {"os": "ios"},
        "lag_patterns": {"ios": r"^Po\d+$"},
        "responses": {
            "GET /api/v0/devices/1": {"status": "ok", "devices": [{"device_id": 1, "os": "ios"}]},
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "Po1", "ifType": "propVirtual"}],
            },
        },
    }
    iftype_lag = {
        "schema_version": 1,
        "name": "ift",
        "device_id": 1,
        "meta": {"os": "ios"},
        "responses": {
            "GET /api/v0/devices/1": {"status": "ok", "devices": [{"device_id": 1, "os": "ios"}]},
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "Po1", "ifType": "ieee8023adLag"}],
            },
        },
    }
    pat_sig = compute_shape_signature(pattern_lag)
    ift_sig = compute_shape_signature(iftype_lag)
    # Same OS, same name_prefix, same present — differ ONLY in the ieee8023ad detection style.
    assert pat_sig["lag"]["ieee8023ad"] is False and ift_sig["lag"]["ieee8023ad"] is True
    assert pat_sig["lag"]["name_prefix"] == ift_sig["lag"]["name_prefix"] == "Po"
    assert classify_novelty(pat_sig, build_manifest([iftype_lag]))["verdict"] != "likely-covered"
    assert classify_novelty(ift_sig, build_manifest([pattern_lag]))["verdict"] != "likely-covered"


def test_signature_ignores_failed_response_frames():
    """Non-2xx response frames cannot inflate the signature with data that replay discards."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "responses": {
            "GET /api/v0/devices/1/ports": {"status": "ok", "ports": [{"port_id": 1, "ifName": "Gi0/1"}]},
            "GET /api/v0/devices/1/transceivers": wrap_response(500, {"transceivers": [{"port_id": 1, "type": "sfp"}]}),
            "GET /api/v0/devices/1/port_stack": wrap_response(
                503, {"mappings": [{"high_port_id": 1, "low_port_id": 2}]}
            ),
        },
    }
    sig = compute_shape_signature(rec)
    assert sig["transceivers"] is False
    assert sig["port_stack"] is False


def test_signature_lag_name_prefix_ignores_aggregate_number_for_sub_units():
    """A sub-unit first LAG port (ae1.0) yields the naming convention (ae), not the aggregate number."""

    def _rec(name):
        return {
            "schema_version": 1,
            "name": "x",
            "device_id": 1,
            "lag_patterns": {"junos": r"^ae\d"},
            "responses": {
                "GET /api/v0/devices/1/ports": {
                    "status": "ok",
                    "ports": [{"port_id": 1, "ifName": name, "ifType": "ieee8023adLag"}],
                }
            },
        }

    assert compute_shape_signature(_rec("ae1.0"))["lag"]["name_prefix"] == "ae"
    assert (
        compute_shape_signature(_rec("ae1.0"))["lag"]["name_prefix"]
        == compute_shape_signature(_rec("ae2.0"))["lag"]["name_prefix"]
    )
    # The base-aggregate case is unchanged (regression guard for the common shape).
    assert compute_shape_signature(_rec("ae7"))["lag"]["name_prefix"] == "ae"


def test_signature_normalizes_salted_interface_pseudonyms():
    """Equivalent custom LAG names must have one salt-independent signature."""
    recording = {
        "schema_version": 1,
        "name": "custom-lag",
        "device_id": 1,
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "customer-lag", "ifType": "ieee8023adLag"}],
            }
        },
    }

    first = compute_shape_signature(anonymize_recording(recording, salt="first"))
    second = compute_shape_signature(anonymize_recording(recording, salt="second"))

    assert first["lag"]["name_prefix"] == "custom"
    assert second["lag"]["name_prefix"] == "custom"
    assert first == second


def test_signature_normalizes_salted_interface_pseudonym_sub_units():
    """Equivalent custom LAG sub-units must have one salt-independent signature."""
    recording = {
        "schema_version": 1,
        "name": "custom-lag-sub-unit",
        "device_id": 1,
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "tenantlag.100", "ifType": "ieee8023adLag"}],
            }
        },
    }

    first = compute_shape_signature(anonymize_recording(recording, salt="first"))
    second = compute_shape_signature(anonymize_recording(recording, salt="second"))

    assert first["lag"]["name_prefix"] == "custom"
    assert second["lag"]["name_prefix"] == "custom"
    assert first == second


@pytest.mark.parametrize(
    "pattern,name",
    [
        (r"^(a+)+$", "aaaa"),
        (r"^((a+))+$", "aaaa"),
        (r"^(a|aa)+$", "aaaa"),
        (r"^(a+){20}$", "a" * 20),
        (r"^(a+){20,20}$", "a" * 20),
        (r"^(a|aa){100}$", "a" * 100),
        (r"^(Po|Te)\d+$", "Po42"),
        (r"^(Bundle-Ether|Port-channel)\d+$", "Bundle-Ether7"),
    ],
)
def test_recording_regex_accepts_supported_patterns_without_a_backtracking_heuristic(pattern, name):
    from netbox_librenms_plugin.data_shapes.ports import compile_lag_patterns

    [compiled] = compile_lag_patterns({"lag_patterns": {"example": pattern}})
    assert compiled.fullmatch(name)


@pytest.mark.parametrize("pattern", [r"^(a{2})+$", r"^(a{2,2})+$", r"^a{1,999}$"])
def test_fixed_or_unnested_bounded_patterns_remain_usable(pattern):
    """Fixed-width groups and plain bounded repetition remain usable."""
    from netbox_librenms_plugin.data_shapes.ports import compile_lag_patterns, name_matches_lag_pattern

    compiled = compile_lag_patterns({"lag_patterns": {"example": pattern}})
    assert len(compiled) == 1
    assert name_matches_lag_pattern("aaaa", compiled)


def test_signature_skips_uncompilable_lag_pattern():
    """A schema-valid recording can contain a repetition outside RE2 limits."""
    from netbox_librenms_plugin.data_shapes.recordings_store import recording_schema_errors

    recording = {
        "schema_version": 1,
        "name": "example",
        "device_id": 1,
        "lag_patterns": {"example": r"^a{1001}$"},
        "responses": {
            "GET /api/v0/devices/1/ports": {
                "status": "ok",
                "ports": [{"port_id": 1, "ifName": "aaaa", "ifType": "ethernetCsmacd"}],
            }
        },
    }

    assert recording_schema_errors(recording) == []
    assert compute_shape_signature(recording)["lag"]["present"] is False


def test_sap_compiler_keeps_valid_patterns_after_an_uncompilable_repeat():
    """An oversized repetition does not discard another usable SAP pattern."""
    from netbox_librenms_plugin.data_shapes.ports import compile_sap_patterns

    patterns = compile_sap_patterns({"sap_patterns": {"oversized": r"^a{1001}$", "usable": r"^sap-[0-9]+$"}})

    assert len(patterns) == 1
    assert patterns[0].fullmatch("sap-42") is not None


@pytest.mark.parametrize("pattern,name", [(r"^(a?){30}a{30}$", "a" * 30), (r"^((a|aa))+$", "a" * 45 + "!")])
@pytest.mark.parametrize("pipeline", ["signature", "replay"])
@pytest.mark.parametrize("map_name", ["lag_patterns", "sap_patterns"])
def test_recording_regex_pipeline_completes_with_ambiguous_patterns(pattern, name, pipeline, map_name):
    """Untrusted patterns cannot exhaust either classification or real HTTP replay."""
    import json
    import subprocess
    import sys

    script = r"""
import json, sys
import django
django.setup()
from django.conf import settings
from netbox_librenms_plugin.data_shapes.signature import compute_shape_signature
from netbox_librenms_plugin.data_shapes.compress import compress_recording
from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording
from netbox_librenms_plugin.librenms_api import LibreNMSAPI
from netbox_librenms_plugin.tests.mock_librenms_server import MockLibreNMSServer
from netbox_librenms_plugin.tests.test_recordings import _assert_port_relationships
pattern, name, pipeline, map_name = json.loads(sys.argv[1])
recording = {
    "schema_version": 1, "name": "bounded-regex", "device_id": 1,
    map_name: {"example": pattern},
    "responses": {
        "GET /api/v0/devices/1/ports": {"status": "ok", "ports": [
            {"port_id": 1, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 2, "ifName": name, "ifType": "propVirtual"},
        ]},
        "GET /api/v0/devices/1/port_stack": {"status": "ok", "mappings": [
            {"high_port_id": 2, "low_port_id": 1},
        ]},
    },
}
if pipeline == "signature":
    assert compute_shape_signature(recording)["lag"]["present"] is (map_name == "lag_patterns" and not name.endswith("!"))
    compress_recording(recording)
    anonymize_recording(recording)
else:
    server = MockLibreNMSServer()
    server.start()
    try:
        server.load_recording(recording)
        settings.PLUGINS_CONFIG["netbox_librenms_plugin"] = {
            "servers": {"bounded-regex": {"librenms_url": server.url, "api_token": "token", "verify_ssl": False}},
            "enable_caching": False,
        }
        api = LibreNMSAPI(server_key="bounded-regex")
        expected = {"lag_members": {1: 2} if map_name == "lag_patterns" and not name.endswith("!") else {}}
        _assert_port_relationships(api, 1, recording, expected)
    finally:
        server.stop()
"""
    subprocess.run(
        [sys.executable, "-c", script, json.dumps([pattern, name, pipeline, map_name])],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("map_name", ["lag_patterns", "sap_patterns"])
def test_recording_regex_compilation_has_a_backend_memory_budget(map_name):
    from netbox_librenms_plugin.data_shapes.ports import compile_lag_patterns, compile_sap_patterns

    compile_patterns = compile_lag_patterns if map_name == "lag_patterns" else compile_sap_patterns
    recording = {map_name: {"exhausted": "(abcdefghij){1000}" * 9, "valid": r"^bond[0-9]+$"}}
    patterns = compile_patterns(recording)
    assert len(patterns) == 1
    assert patterns[0].fullmatch("bond42")


@pytest.mark.parametrize("pattern", [r"(?=bond)bond", r"(bond)\1"])
def test_recording_regex_excludes_unsupported_python_features(pattern):
    from netbox_librenms_plugin.data_shapes.ports import compile_lag_patterns

    assert compile_lag_patterns({"lag_patterns": {"unsupported": pattern}}) == []


def test_recording_regex_states_unicode_and_newline_semantics():
    from netbox_librenms_plugin.data_shapes.ports import compile_lag_patterns

    [compiled] = compile_lag_patterns({"lag_patterns": {"example": r"^bond\d+$"}})
    assert compiled.search("bond42")
    assert compiled.search("bond\u0661") is None
    assert compiled.search("bond42\n") is None
    with pytest.raises(UnicodeEncodeError):
        compiled.search("bond" + chr(0xD800))


def test_recording_regex_skips_malformed_unicode_but_keeps_later_patterns():
    from netbox_librenms_plugin.data_shapes.ports import compile_lag_patterns

    patterns = compile_lag_patterns({"lag_patterns": {"malformed": chr(0xD800), "valid": "^bond[0-9]+$"}})
    assert len(patterns) == 1
    assert patterns[0].fullmatch("bond7")


def test_recording_regex_uses_native_re2_literal_repeat_semantics():
    from netbox_librenms_plugin.data_shapes.ports import compile_lag_patterns

    [compiled] = compile_lag_patterns({"lag_patterns": {"example": r"^a{99999999999999999999}$"}})
    assert compiled.fullmatch("a{99999999999999999999}")
    assert compiled.search("aaaa") is None


def test_recording_regex_bounds_pattern_count_and_length():
    from netbox_librenms_plugin.data_shapes.ports import compile_lag_patterns

    assert len(compile_lag_patterns({"lag_patterns": {str(i): "^bond[0-9]+$" for i in range(101)}})) == 100
    assert compile_lag_patterns({"lag_patterns": {"long": "a" * 201, "nonstring": 3}}) == []
