"""Tests for data-shape anonymization: PII scrubbed, logic fields preserved, deterministic, replayable."""

from unittest.mock import patch

from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording, find_pii, pseudonymize_os
from netbox_librenms_plugin.serial_utils import map_sensors_to_serial_links
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


def test_inventory_model_name_preserved_as_module_match_key():
    """The entPhysicalModelName ModuleType match key is preserved verbatim, even though device hardware is pseudonymized."""
    rec = load_recording("cisco-stackwise-3member")
    anon = anonymize_recording(rec)

    members = anon["responses"]["GET /api/v0/inventory/1000?entPhysicalClass=chassis&entPhysicalContainedIn=1"][
        "inventory"
    ]
    model_names = [m.get("entPhysicalModelName") for m in members if m.get("entPhysicalModelName")]
    # The real chassis SKUs survive so a recording can match a provisioned NetBox ModuleType.
    assert "WS-C3750X-48P" in model_names
    assert not any(str(name).startswith("MODEL-") for name in model_names)


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


def test_find_pii_scans_top_level_fields_not_just_responses():
    """find_pii must scan the whole recording — residual PII in a top-level field (name/description/meta) is missed if only `responses` is scanned."""
    rec = _ports()
    rec["description"] = "captured from noc@example.com"  # top-level free text with a residual email
    rec["meta"] = {"note": "mgmt 10.4.5.6"}  # top-level meta with a residual IP

    kinds = {(f["kind"], f["value"]) for f in find_pii(rec)}
    assert ("email", "noc@example.com") in kinds
    assert ("ipv4", "10.4.5.6") in kinds


def test_find_pii_flags_compressed_ipv6():
    """find_pii must catch compressed IPv6 literals (e.g. 2001:4860::1), not only fully-expanded forms — the old regex missed them and they slipped past --validate."""
    rec = _ports({"port_id": 1, "ifName": "Gi0/1", "entPhysicalName": "uplink to 2001:4860::1"})
    kinds = {(f["kind"], f["value"]) for f in find_pii(rec)}
    assert ("ipv6", "2001:4860::1") in kinds


def test_find_pii_ipv6_no_false_positive_on_time_or_doc_range():
    """A plain colon time (12:34:56) is not IPv6, and the documentation range (2001:db8::) is exempt — neither must be flagged."""
    rec = _ports({"port_id": 1, "ifName": "Gi0/1", "entPhysicalName": "boot 12:34:56 doc 2001:DB8::1"})
    assert not any(f["kind"] == "ipv6" for f in find_pii(rec))


def test_find_pii_ignores_oid_and_version_dotted_decimals():
    """SNMP object IDs and version strings are dotted-decimal but not IPs — the safety-net must not flag them."""
    rec = _ports()
    rec["responses"]["GET /api/v0/devices/1"] = {
        "status": "ok",
        "devices": [{"device_id": 1, "sysObjectID": "1.3.6.1.4.1.9.1.2068", "version": "24.4.1.41I-ULH_800ZR"}],
    }
    anon = anonymize_recording(rec)
    assert find_pii(anon) == []


def test_vendor_oids_pseudonymized_under_example_enterprise():
    """sysObjectID/sensor_oid/entPhysicalVendorType lose the vendor enterprise arc, keeping OID shape."""
    rec = _ports()
    rec["responses"]["GET /api/v0/devices/1"] = {
        "status": "ok",
        "devices": [
            {
                "device_id": 1,
                "sysObjectID": ".1.3.6.1.4.1.6527.1.3.17",  # Nokia
                "entPhysicalVendorType": "1.3.6.1.4.1.9.12.3.1.3.1234",  # Cisco (no leading dot)
            }
        ],
    }
    rec["responses"]["GET /api/v0/resources/sensors"] = {
        "status": "ok",
        "sensors": [
            {"sensor_id": 1, "device_id": 1, "sensor_type": "acsSerialPortTable", "sensor_oid": ".1.3.6.1.4.1.10418.1"}
        ],
    }
    anon = anonymize_recording(rec)
    dev = anon["responses"]["GET /api/v0/devices/1"]["devices"][0]
    sensor = anon["responses"]["GET /api/v0/resources/sensors"]["sensors"][0]

    # The example-enterprise arc (32473) replaces the real vendor numbers (6527, 9, 10418).
    assert dev["sysObjectID"].startswith(".1.3.6.1.4.1.32473.") and "6527" not in dev["sysObjectID"]
    assert dev["entPhysicalVendorType"].startswith("1.3.6.1.4.1.32473.")  # leading-dot convention preserved
    assert ".9.12." not in dev["entPhysicalVendorType"]
    assert sensor["sensor_oid"].startswith(".1.3.6.1.4.1.32473.") and "10418" not in sensor["sensor_oid"]
    # Deterministic, and find_pii stays clean (the pseudonym is a well-formed OID).
    assert (
        anonymize_recording(rec)["responses"]["GET /api/v0/devices/1"]["devices"][0]["sysObjectID"]
        == dev["sysObjectID"]
    )
    assert find_pii(anon) == []


def test_asset_id_and_alias_scrubbed():
    """Operator-configurable entPhysicalAssetID / entPhysicalAlias are scrubbed to empty."""
    rec = _ports(
        {
            "port_id": 1,
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "entPhysicalAssetID": "ASSET-2024-1337",
            "entPhysicalAlias": "rackA3-U12-core-rtr",
        }
    )
    port = anonymize_recording(rec)["responses"]["GET /api/v0/devices/1/ports"]["ports"][0]
    assert port["entPhysicalAssetID"] == ""
    assert port["entPhysicalAlias"] == ""


def test_find_pii_still_flags_a_real_ip_next_to_text():
    """A genuine IPv4 embedded in free-form text is still caught."""
    rec = _ports(
        {"port_id": 1, "ifName": "Gi0/1", "ifType": "ethernetCsmacd", "entPhysicalName": "uplink 10.7.8.9 core"}
    )
    anon = anonymize_recording(rec)
    assert any(f["kind"] == "ipv4" and f["value"] == "10.7.8.9" for f in find_pii(anon))


def test_entphysical_mfg_name_and_date_anonymized():
    """Vendor entPhysicalMfgName is pseudonymized (hides the platform the os hash masks) and the identifying entPhysicalMfgDate is scrubbed; neither is read by sync logic."""
    rec = _ports()
    rec["responses"]["GET /api/v0/inventory/1?entPhysicalContainedIn=0"] = {
        "status": "ok",
        "inventory": [
            {
                "entPhysicalIndex": 1,
                "entPhysicalClass": "chassis",
                "entPhysicalMfgName": "Cisco Systems Inc.",
                "entPhysicalMfgDate": "2021-03-15,12:00:00.0",
            }
        ],
    }
    key = "GET /api/v0/inventory/1?entPhysicalContainedIn=0"
    item = anonymize_recording(rec)["responses"][key]["inventory"][0]
    assert item["entPhysicalMfgName"] != "Cisco Systems Inc."  # vendor name no longer verbatim
    assert item["entPhysicalMfgName"].startswith("MFG-")
    assert item["entPhysicalMfgDate"] == ""  # identifying mfg date scrubbed
    # Deterministic, and the safety net stays clean (the pseudonym carries no PII).
    assert (
        anonymize_recording(rec)["responses"][key]["inventory"][0]["entPhysicalMfgName"] == item["entPhysicalMfgName"]
    )
    assert find_pii(anonymize_recording(rec)) == []


def test_entphysical_name_and_descr_preserved_for_matching():
    """Logic-bearing entPhysicalName/entPhysicalDescr (module-type, VC-member and transceiver matching read them) survive verbatim; residual free-text PII there is the find_pii net's job, not a scrub."""
    rec = _ports()
    rec["responses"]["GET /api/v0/inventory/1?entPhysicalContainedIn=0"] = {
        "status": "ok",
        "inventory": [{"entPhysicalIndex": 1, "entPhysicalName": "FPC 1", "entPhysicalDescr": "10GBASE-LR SFP+"}],
    }
    key = "GET /api/v0/inventory/1?entPhysicalContainedIn=0"
    item = anonymize_recording(rec)["responses"][key]["inventory"][0]
    assert item["entPhysicalName"] == "FPC 1"
    assert item["entPhysicalDescr"] == "10GBASE-LR SFP+"


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


# ── ifName / ifDescr pattern-aware anonymization ──────────────────────────────

import pytest  # noqa: E402

_PORT_PATTERNS = [
    "ge-0/0/0.100",
    "xe-4/2/2",
    "ae42",
    "ae10.2221",
    "lag-1",
    "Po12",
    "Po10.100",
    "Bundle-Ether1",
    "GigabitEthernet0/0/0",
    "Te1/5",
    "HundredGigE0/0/0/18",
    "1/1/c31/3",
    "2/x1/1/c1/1",
    "A/1",
    "swp15.3",
    "bond1",
    "eth0",
    "lo0",
    # Junos digit-less pseudo-interfaces and their sub-units: the base/sub-unit names must both
    # survive so the resolver's name-based pairing (jsrv.1 -> jsrv) is preserved after anonymization.
    "jsrv",
    "jsrv.1",
    "irb",
    "irb.100",
]


@pytest.mark.parametrize("name", _PORT_PATTERNS)
def test_port_pattern_ifname_preserved_verbatim(name):
    """A real port-name (slot notation or known vendor prefix) survives anonymization unchanged."""
    rec = _ports({"port_id": 1, "ifName": name, "ifType": "ethernetCsmacd"})
    port = anonymize_recording(rec)["responses"]["GET /api/v0/devices/1/ports"]["ports"][0]
    assert port["ifName"] == name


@pytest.mark.parametrize(
    "custom",
    [
        "AORTA-SSP-CUSTOMER-1",
        "to_prod-lab03c-ra2",
        "IXIA",
        "OpenXR-100G-Testing",
        # Free text that merely STARTS with a digit-less interface name must still be scrubbed — the
        # word-bounded rule keeps "jsrv"/"irb" from preserving (leaking) a customer/host annotation.
        "jsrv-customer-rtr",
        "irbridge-core01",
    ],
)
def test_custom_ifname_pseudonymized(custom):
    """A custom (non-port-pattern) ifName is replaced by a stable iface-<hash> pseudonym, leaking nothing."""
    rec = _ports({"port_id": 1, "ifName": custom, "ifType": "ethernetCsmacd"})
    out = anonymize_recording(rec)["responses"]["GET /api/v0/devices/1/ports"]["ports"][0]["ifName"]
    assert out.startswith("iface-")
    assert custom not in out
    # Deterministic.
    assert out == anonymize_recording(rec)["responses"]["GET /api/v0/devices/1/ports"]["ports"][0]["ifName"]


@pytest.mark.parametrize(
    "raw, token",
    [
        ("eth0_customerA", "eth0"),
        ("ae42_lab03", "ae42"),
        ("xe-0/0/0.100_tenantX", "xe-0/0/0.100"),
        ("GigabitEthernet0/0/0_core-rtr", "GigabitEthernet0/0/0"),
    ],
)
def test_port_token_underscore_annotation_is_dropped(raw, token):
    """A port token with an underscore-joined annotation keeps only the token — the trailing annotation (which can carry customer/tenant/host names) must not survive verbatim."""
    rec = _ports({"port_id": 1, "ifName": raw, "ifType": "ethernetCsmacd"})
    out = anonymize_recording(rec)["responses"]["GET /api/v0/devices/1/ports"]["ports"][0]["ifName"]
    assert out == token
    assert raw.split("_", 1)[1] not in out  # the annotation tail is gone


def test_ifdescr_keeps_port_token_drops_freetext_annotation():
    """An ifDescr with a leading port token + free-text annotation keeps only the token (no infra leak)."""
    rec = _ports(
        {
            "port_id": 1,
            "ifName": "lag2",
            "ifType": "ipForward",
            "ifDescr": "lag2, IP interface, ** prod-lab03d-rc1 ae42 PCE testing jdoe **",
        },
        {"port_id": 2, "ifName": "AORTA-CUST-9", "ifType": "other", "ifDescr": "AORTA-CUST-9, customer Microsoft"},
    )
    ports = anonymize_recording(rec)["responses"]["GET /api/v0/devices/1/ports"]["ports"]
    assert ports[0]["ifDescr"] == "lag2"  # token kept, annotation dropped
    assert ports[1]["ifDescr"].startswith("iface-")  # no port token → pseudonym
    blob = str(ports)
    for infra in ("prod-lab03d-rc1", "Microsoft", "jdoe", "PCE testing"):
        assert infra not in blob


def test_bgp_local_as_anonymized_to_private_asn():
    """The bgpLocalAs int is mapped to a deterministic private ASN, not passed through."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "responses": {"GET /api/v0/devices/1": {"devices": [{"device_id": 1, "bgpLocalAs": 6730}]}},
    }
    dev = anonymize_recording(rec)["responses"]["GET /api/v0/devices/1"]["devices"][0]
    assert dev["bgpLocalAs"] != 6730
    assert 64512 <= dev["bgpLocalAs"] <= 65534


def test_name_and_description_are_scrubbed_of_hostname():
    """The capture auto-fills name/description from the device name; anonymization neutralizes them."""
    rec = {
        "schema_version": 1,
        "name": "core-rtr01.dc1.example.net-shape",
        "description": "Captured from core-rtr01.dc1.example.net.",
        "meta": {"os": "junos"},
        "device_id": 1,
        "responses": {},
    }
    anon = anonymize_recording(rec)
    assert "core-rtr01" not in anon["name"] and "core-rtr01" not in anon["description"]
    # The name keys off the pseudonymized OS token, not the raw OS.
    assert anon["name"].startswith(f"{pseudonymize_os('junos')}-shape-")
    assert anon["description"] == "Anonymized LibreNMS data-shape capture."


def test_version_pseudonymized_to_fw_hash():
    """A device firmware version is pseudonymized to a deterministic fw-<hash> (no raw build leaks)."""
    rec = _ports()
    rec["responses"]["GET /api/v0/devices/1"] = {
        "status": "ok",
        "devices": [{"device_id": 1, "version": "21.2R3-S4.8", "entPhysicalSoftwareRev": "21.2R3-S4.8"}],
    }
    a1 = anonymize_recording(rec)
    a2 = anonymize_recording(rec)
    dev1 = a1["responses"]["GET /api/v0/devices/1"]["devices"][0]
    dev2 = a2["responses"]["GET /api/v0/devices/1"]["devices"][0]

    assert dev1["version"].startswith("fw-")
    assert "21.2R3" not in dev1["version"]
    assert dev1["entPhysicalSoftwareRev"].startswith("fw-")
    assert dev1["version"] == dev2["version"]  # deterministic
    assert find_pii(a1) == []


def test_os_pseudonymized_to_stable_unsalted_token():
    """Every OS (even common ones) becomes a stable os-<hash>; meta.os and the name follow, salt-independent."""
    rec = _ports()
    rec["meta"] = {"os": "weirdos9000"}
    rec["responses"]["GET /api/v0/devices/1"] = {"status": "ok", "devices": [{"device_id": 1, "os": "weirdos9000"}]}
    anon = anonymize_recording(rec)

    body_os = anon["responses"]["GET /api/v0/devices/1"]["devices"][0]["os"]
    assert body_os == pseudonymize_os("weirdos9000")
    assert "weirdos9000" not in body_os
    # meta.os (outside `responses`) is pseudonymized too, and the neutral name keys off it.
    assert anon["meta"]["os"] == body_os
    assert anon["name"].startswith(f"{body_os}-shape-")
    # Unsalted: a per-contributor salt must NOT change the OS token, or cross-contributor novelty
    # comparison would break.
    assert anonymize_recording(rec, salt="contributor-x")["meta"]["os"] == body_os
    # Common OSes are hashed the same way (treated equal) — junos doesn't pass through verbatim.
    common = _ports()
    common["responses"]["GET /api/v0/devices/1"] = {"status": "ok", "devices": [{"device_id": 1, "os": "junos"}]}
    out = anonymize_recording(common)["responses"]["GET /api/v0/devices/1"]["devices"][0]["os"]
    assert out == pseudonymize_os("junos") and out != "junos"


def test_find_pii_flags_residual_fqdn_but_not_icon_or_version():
    """find_pii catches a leaked FQDN in free text, but not static asset paths or firmware versions."""
    rec = {
        "schema_version": 1,
        "name": "x",
        "device_id": 1,
        "responses": {
            "GET /api/v0/devices/1": {
                "devices": [
                    {
                        "device_id": 1,
                        "icon": "images/os/nokia.svg",
                        "version": "3.9.0.4",
                        "sysDescr": "core-rtr01.dc1.example.net leaked here",
                    }
                ]
            }
        },
    }
    # sysDescr is scrubbed to "" by the field rules, so plant the FQDN in an unclassified field
    # to exercise the safety net directly.
    rec["responses"]["GET /api/v0/devices/1"]["devices"][0]["custom_note"] = "see rtr.dc1.example.net"
    anon = anonymize_recording(rec)
    findings = find_pii(anon)
    kinds = {(f["kind"], f["value"]) for f in findings}
    assert ("fqdn", "rtr.dc1.example.net") in kinds
    assert not any(f["value"] == "3.9.0.4" for f in findings)  # version pseudonymized to fw-<hash>
    # The icon names the OS/vendor, so it's genericized (and not flagged).
    assert anon["responses"]["GET /api/v0/devices/1"]["devices"][0]["icon"] == "images/os/generic.svg"
    assert not any(f["value"] == "nokia.svg" for f in findings)


@pytest.mark.parametrize(
    ("recording_name", "expect_lag"),
    [("cisco-lag-and-subinterface", True), ("junos-subinterfaces", False)],
)
def test_anonymization_preserves_port_relationships(recording_server, recording_name, expect_lag):
    """The LAG/sub-interface maps resolve identically before and after anonymization (logic intact)."""
    rec = load_recording(recording_name)

    def _resolve(recording):
        _server, api = recording_server(recording)
        _ok, ports = api.get_ports(recording["device_id"])
        _ok2, stack = api.get_port_stack(recording["device_id"])
        rel = api.resolve_port_relationships(
            ports["ports"],
            stack,
            lag_patterns=recording.get("lag_patterns", {}),
            device_os=recording.get("meta", {}).get("os"),
        )
        return (
            {str(k): str(v) for k, v in rel["lag_members"].items()},
            {str(k): str(v) for k, v in rel["sub_interfaces"].items()},
        )

    raw_lag, raw_sub = _resolve(rec)
    anon_lag, anon_sub = _resolve(anonymize_recording(rec))
    assert raw_lag == anon_lag and raw_sub == anon_sub
    # Non-vacuous: the recording really exercises the sub-interface relationship (the Junos fixture's
    # jsrv/irb digit-less base <-> .unit pairing is exactly what the anonymizer must keep intact).
    assert raw_sub
    if expect_lag:
        assert raw_lag  # LAG coverage when the fixture includes a LAG aggregate


def test_transceiver_serial_pseudonymized_model_and_optics_preserved():
    """A transceiver's serial is pseudonymized; the optics shape AND the model SKU (ModuleType match key) survive."""
    rec = {
        "schema_version": 1,
        "name": "optics",
        "device_id": 1,
        "responses": {
            "GET /api/v0/devices/1/transceivers": {
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
            }
        },
    }
    t = anonymize_recording(rec)["responses"]["GET /api/v0/devices/1/transceivers"]["transceivers"][0]
    # Serial is a per-unit identifier (not a match key) → pseudonymized.
    assert t["serial"].startswith("SN-") and "X42AU0D" not in t["serial"]
    # Model SKU is the ModuleType-matching key (public part number) → preserved verbatim.
    assert t["model"] == "3HE10550AARA01"
    # Logic-bearing optics shape (what an outcome test asserts) is untouched.
    assert t["port_id"] == 519
    assert t["type"] == "CFP2/QSFP28"
    assert t["channels"] == 4
    assert t["connector"] == "LC"
    assert t["wavelength"] == 1301
    assert find_pii(anonymize_recording(rec)) == []


def test_serial_sensor_label_anonymized_preserving_is_configured():
    """sensor_descr hostnames are pseudonymized but generic port labels stay, so is_configured is unchanged."""
    sensors = [
        {
            "sensor_id": 1,
            "device_id": 1,
            "sensor_type": "acsSerialPortTable",
            "sensor_index": "acsSerialPortTableStatus.7",
            "sensor_descr": "ttyS7 Status",
            "sensor_oid": ".1.3.6.1.4.1.10418.16.2.5.1.5.7",
        },
        {
            "sensor_id": 2,
            "device_id": 1,
            "sensor_type": "acsSerialPortTable",
            "sensor_index": "acsSerialPortTableStatus.8",
            "sensor_descr": "PROD-LAB03A-RA1 Status",
            "sensor_oid": ".1.3.6.1.4.1.10418.16.2.5.1.5.8",
        },
    ]
    rec = {
        "schema_version": 1,
        "name": "serial",
        "device_id": 1,
        "responses": {"GET /api/v0/resources/sensors": {"status": "ok", "sensors": sensors}},
    }
    anon = anonymize_recording(rec)
    asens = anon["responses"]["GET /api/v0/resources/sensors"]["sensors"]

    # Default port label kept verbatim (no PII); customised hostname label pseudonymized.
    assert asens[0]["sensor_descr"] == "ttyS7 Status"
    assert asens[1]["sensor_descr"].startswith("device-")
    assert "PROD-LAB03A-RA1" not in asens[1]["sensor_descr"]
    # Logic-bearing fields preserved so the mapping still resolves.
    assert asens[0]["sensor_index"] == "acsSerialPortTableStatus.7"
    assert asens[0]["sensor_type"] == "acsSerialPortTable"

    # The is_configured OUTCOME resolves identically before and after anonymization.
    raw_flags = [r["is_configured"] for r in map_sensors_to_serial_links(sensors, device_id=1)]
    anon_flags = [r["is_configured"] for r in map_sensors_to_serial_links(asens, device_id=1)]
    assert raw_flags == anon_flags == [False, True]  # default ttyS7 vs custom label — both states exercised
    # find_pii is clean — incl. no false-positive on the dotted SNMP sensor_oid.
    assert find_pii(anon) == []


def test_serial_sensor_short_hostname_label_is_pseudonymized():
    """A short hostname label (e.g. "core1") looks like a port name but must be pseudonymized, not kept."""
    rec = {
        "schema_version": 1,
        "name": "serial",
        "device_id": 1,
        "responses": {
            "GET /api/v0/resources/sensors": {
                "status": "ok",
                "sensors": [
                    {
                        "sensor_id": 1,
                        "device_id": 1,
                        "sensor_type": "acsSerialPortTable",
                        "sensor_index": "acsSerialPortTableStatus.3",
                        "sensor_descr": "core1 Status",  # a device shorthand, not a generic port label
                    }
                ],
            }
        },
    }
    descr = anonymize_recording(rec)["responses"]["GET /api/v0/resources/sensors"]["sensors"][0]["sensor_descr"]
    assert descr.startswith("device-")
    assert "core1" not in descr
