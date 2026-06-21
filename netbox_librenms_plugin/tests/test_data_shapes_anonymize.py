"""Tests for data-shape anonymization: PII scrubbed, logic fields preserved, deterministic, replayable."""

from unittest.mock import patch

from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording, find_pii
from netbox_librenms_plugin.tests.recordings import load_recording


def _ports(*port_dicts):
    return {
        "schema_version": 1,
        "name": "synthetic",
        "device_id": 1,
        "responses": {"GET /api/v0/devices/1/ports": {"status": "ok", "ports": list(port_dicts)}},
    }


def test_logic_bearing_fields_preserved():
    """ifName/ifType/port ids and ENTITY-MIB class/index/position must survive verbatim."""
    rec = load_recording("cisco-stackwise-3member")
    anon = anonymize_recording(rec)

    root = anon["responses"]["GET /api/v0/inventory/1000?entPhysicalContainedIn=0"]["inventory"][0]
    assert root["entPhysicalClass"] == "stack"
    assert root["entPhysicalIndex"] == 1

    members = anon["responses"]["GET /api/v0/inventory/1000?entPhysicalClass=chassis&entPhysicalContainedIn=1"][
        "inventory"
    ]
    # Positions and indices (drive VC member ordering) are untouched.
    assert [m["entPhysicalParentRelPos"] for m in members] == [1, 2, 3]
    assert [m["entPhysicalIndex"] for m in members] == [100, 200, 300]
    assert all(m["entPhysicalClass"] == "chassis" for m in members)


def test_serials_pseudonymized_and_deterministic():
    """Serials become SN-<hash>, the same input maps to the same fake, distinct inputs differ."""
    rec = load_recording("cisco-stackwise-3member")
    a1 = anonymize_recording(rec)
    a2 = anonymize_recording(rec)

    key = "GET /api/v0/inventory/1000?entPhysicalClass=chassis&entPhysicalContainedIn=1"
    serials1 = [m["entPhysicalSerialNum"] for m in a1["responses"][key]["inventory"]]
    serials2 = [m["entPhysicalSerialNum"] for m in a2["responses"][key]["inventory"]]

    assert all(s.startswith("SN-") for s in serials1)
    assert "SN-a1b2c3" not in serials1  # original gone
    assert serials1 == serials2  # deterministic
    assert len(set(serials1)) == 3  # distinct originals stay distinct


def test_cross_reference_serial_preserved():
    """A device serial that equals a stack-member serial must map to the SAME pseudonym."""
    rec = load_recording("cisco-stackwise-3member")
    # Sanity: the fixture's device serial equals member #100's serial.
    dev_serial = rec["responses"]["GET /api/v0/devices/1000"]["devices"][0]["serial"]
    key = "GET /api/v0/inventory/1000?entPhysicalClass=chassis&entPhysicalContainedIn=1"
    member_serial = rec["responses"][key]["inventory"][0]["entPhysicalSerialNum"]
    assert dev_serial == member_serial == "SN-a1b2c3"

    anon = anonymize_recording(rec)
    anon_dev = anon["responses"]["GET /api/v0/devices/1000"]["devices"][0]["serial"]
    anon_member = anon["responses"][key]["inventory"][0]["entPhysicalSerialNum"]
    assert anon_dev == anon_member  # cross-reference intact → master detection still works


def test_hostname_and_model_pseudonymized():
    """hostname/sysName → device-<hash>; hardware/model SKU → MODEL-<hash>."""
    rec = load_recording("cisco-stackwise-3member")
    anon = anonymize_recording(rec)
    dev = anon["responses"]["GET /api/v0/devices/1000"]["devices"][0]

    assert dev["hostname"].startswith("device-")
    assert dev["sysName"].startswith("device-")
    assert dev["hardware"].startswith("MODEL-")
    assert "WS-C3750X" not in dev["hardware"]


def test_ip_mac_geo_location_freetext_scrubbed():
    """IP→doc range, MAC→synthetic, lat/lng→null, location→Lab, ifAlias→empty."""
    rec = _ports(
        {
            "port_id": 1,
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifPhysAddress": "aa:bb:cc:dd:ee:ff",
            "ifAlias": "uplink to corp-core in rack 4",
        }
    )
    rec["responses"]["GET /api/v0/devices/1"] = {
        "status": "ok",
        "devices": [{"device_id": 1, "ip": "10.1.2.3", "lat": 51.5, "lng": -0.12, "location": "London DC, Floor 3"}],
    }
    anon = anonymize_recording(rec)

    dev = anon["responses"]["GET /api/v0/devices/1"]["devices"][0]
    assert dev["ip"] == "192.0.2." + dev["ip"].split(".")[-1] and dev["ip"].startswith("192.0.2.")
    assert dev["lat"] is None and dev["lng"] is None
    assert dev["location"] == "Lab"

    port = anon["responses"]["GET /api/v0/devices/1/ports"]["ports"][0]
    assert port["ifPhysAddress"].startswith("02:00:00:")
    assert port["ifAlias"] == ""
    # Logic fields on the same row preserved.
    assert port["ifName"] == "Gi0/1"
    assert port["ifType"] == "ethernetCsmacd"


def test_ipv4_with_prefix_keeps_prefix_length():
    """An address carrying a /prefix keeps the prefix after the host part is anonymized."""
    rec = _ports()
    rec["responses"]["GET /api/v0/devices/1"] = {"status": "ok", "devices": [{"device_id": 1, "ip": "10.9.9.9/24"}]}
    anon = anonymize_recording(rec)
    ip = anon["responses"]["GET /api/v0/devices/1"]["devices"][0]["ip"]
    assert ip.startswith("192.0.2.") and ip.endswith("/24")


def test_find_pii_passes_clean_anonymized_recording():
    """A field-rule-anonymized recording carries no residual IP/MAC/email."""
    rec = load_recording("cisco-lag-and-subinterface")
    anon = anonymize_recording(rec)
    assert find_pii(anon) == []


def test_find_pii_flags_residual_pii_in_unexpected_field():
    """The safety net catches PII the field rules don't cover (e.g. an IP/email in a description)."""
    rec = _ports({"port_id": 1, "ifName": "Gi0/1", "ifType": "ethernetCsmacd", "entPhysicalName": "mgmt 10.4.5.6"})
    rec["responses"]["GET /api/v0/devices/1"] = {
        "status": "ok",
        "devices": [{"device_id": 1, "sysContact": "noc@example.com"}],
    }
    anon = anonymize_recording(rec)  # sysContact is scrubbed, but entPhysicalName is preserved

    kinds = {(f["kind"], f["value"]) for f in find_pii(anon)}
    assert ("ipv4", "10.4.5.6") in kinds  # residual IP in a preserved free-form label is flagged
    # sysContact email was scrubbed to "" by the field rule, so it must NOT appear.
    assert not any(f["kind"] == "email" for f in find_pii(anon))


def test_find_pii_ignores_oid_and_version_dotted_decimals():
    """SNMP object IDs and version strings are dotted-decimal but not IPs — the safety-net must not flag them."""
    rec = _ports()
    rec["responses"]["GET /api/v0/devices/1"] = {
        "status": "ok",
        "devices": [{"device_id": 1, "sysObjectID": "1.3.6.1.4.1.9.1.2068", "version": "24.4.1.41I-ULH_800ZR"}],
    }
    anon = anonymize_recording(rec)
    assert find_pii(anon) == []


def test_find_pii_still_flags_a_real_ip_next_to_text():
    """A genuine IPv4 embedded in free-form text is still caught."""
    rec = _ports(
        {"port_id": 1, "ifName": "Gi0/1", "ifType": "ethernetCsmacd", "entPhysicalName": "uplink 10.7.8.9 core"}
    )
    anon = anonymize_recording(rec)
    assert any(f["kind"] == "ipv4" and f["value"] == "10.7.8.9" for f in find_pii(anon))


def test_snmp_credentials_scrubbed():
    """SNMP community / v3 auth+priv secrets in the device row are scrubbed to empty, not leaked."""
    rec = _ports()
    rec["responses"]["GET /api/v0/devices/1"] = {
        "status": "ok",
        "devices": [
            {
                "device_id": 1,
                "community": "s3cr3t-community",
                "authname": "snmpuser",
                "authpass": "authPassw0rd",
                "cryptopass": "privKey12345",
                "authalgo": "SHA",
                "cryptoalgo": "AES",
                "snmpver": "v3",
            }
        ],
    }
    dev = anonymize_recording(rec)["responses"]["GET /api/v0/devices/1"]["devices"][0]
    for field in ("community", "authname", "authpass", "cryptopass", "authalgo", "cryptoalgo", "snmpver"):
        assert dev[field] == "", f"{field} must be scrubbed"


def test_display_name_pseudonymized():
    """The device `display` field (often a real FQDN) is pseudonymized like hostname/sysName."""
    rec = _ports()
    rec["responses"]["GET /api/v0/devices/1"] = {
        "status": "ok",
        "devices": [{"device_id": 1, "display": "core-sw-01.nyc.corp.example.com"}],
    }
    dev = anonymize_recording(rec)["responses"]["GET /api/v0/devices/1"]["devices"][0]
    assert dev["display"].startswith("device-")
    assert "example.com" not in dev["display"]


def test_find_pii_flags_nonempty_value_under_secret_looking_key():
    """A secret under an unexpected key the rules don't scrub is still flagged (value redacted)."""
    rec = _ports()
    rec["responses"]["GET /api/v0/devices/1"] = {
        "status": "ok",
        "devices": [{"device_id": 1, "api_token": "abc123def456"}],
    }
    anon = anonymize_recording(rec)  # api_token isn't in any rule set → survives
    findings = find_pii(anon)
    token_findings = [f for f in findings if f["kind"] == "credential" and f["path"].endswith("api_token")]
    assert token_findings, "an unexpected secret-keyed value must be flagged"
    assert token_findings[0]["value"] == "<redacted>"  # the secret itself is never echoed


def test_salt_changes_pseudonyms():
    """Different salts produce different pseudonyms for the same input."""
    rec = load_recording("cisco-stackwise-3member")
    key = "GET /api/v0/inventory/1000?entPhysicalClass=chassis&entPhysicalContainedIn=1"
    s_a = anonymize_recording(rec, salt="alpha")["responses"][key]["inventory"][0]["entPhysicalSerialNum"]
    s_b = anonymize_recording(rec, salt="beta")["responses"][key]["inventory"][0]["entPhysicalSerialNum"]
    assert s_a != s_b


def test_anonymized_recording_still_detects_vc(recording_server):
    """An anonymized recording replays to the same VC outcome (member count + position order)."""
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

    anon = anonymize_recording(load_recording("cisco-stackwise-3member"))
    _server, api = recording_server(anon)
    with patch(
        "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
        return_value="{master}-m{position}",
    ):
        result = detect_virtual_chassis_from_inventory(api, 1000)

    assert result is not None
    assert result["member_count"] == 3
    assert [m["position"] for m in result["members"]] == [1, 2, 3]
    # The master is still identified by the (now pseudonymized) device serial matching a member.
    assert any(m["serial"] == result["members"][0]["serial"] for m in result["members"])
