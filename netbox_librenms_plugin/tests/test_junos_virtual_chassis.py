"""
Junos Virtual Chassis detection from the EX4400 capture.

LibreNMS models a Junos VC as one chassis root ("... Virtual Chassis ...") whose members are
direct "FPC" container children, each with the member's own serial. The root serial is the
master's. Detection replays the bundled capture through the real client and HTTP.
"""

import copy

import pytest

from netbox_librenms_plugin.data_shapes.envelope import unwrap_response
from netbox_librenms_plugin.tests.recordings import load_recording

pytestmark = pytest.mark.django_db

_RECORDING = "juniper-ex4400-vc-2member"
_DEVICE_ID = load_recording(_RECORDING)["device_id"]
_ROOT_KEY = f"GET /api/v0/inventory/{_DEVICE_ID}?entPhysicalContainedIn=0"
_CHILDREN_KEY = f"GET /api/v0/inventory/{_DEVICE_ID}?entPhysicalContainedIn=1"
_ALL_KEY = f"GET /api/v0/inventory/{_DEVICE_ID}/all"


def _rows(recording, key):
    return unwrap_response(recording["responses"][key])[1]["inventory"]


def _edit_rows(recording, index, change):
    """Apply *change* to the row with *index* in every inventory body; None drops the row."""
    for key in (_ROOT_KEY, _CHILDREN_KEY, _ALL_KEY):
        rows = _rows(recording, key)
        for row in list(rows):
            if row["entPhysicalIndex"] == index:
                if change is None:
                    rows.remove(row)
                else:
                    change(row)


def _detect(recording_server, recording):
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

    _server, api = recording_server(recording)
    return detect_virtual_chassis_from_inventory(api, recording["device_id"])


@pytest.mark.parametrize("anonymized", [False, True])
def test_fpc_members_carry_their_member_number_serial_and_model(recording_server, anonymized):
    from netbox_librenms_plugin.import_utils.virtual_chassis import identify_vc_master, vc_serial_key

    recording = load_recording(_RECORDING)
    if anonymized:
        from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording

        recording = anonymize_recording(recording)
    device_id = recording["device_id"]
    fpcs = {
        row["entPhysicalIndex"]: row
        for row in _rows(recording, f"GET /api/v0/inventory/{device_id}?entPhysicalContainedIn=1")
        if row["entPhysicalIndex"] in (120, 121)
    }

    result = _detect(recording_server, recording)

    assert result["is_stack"] is True
    assert [member["index"] for member in result["members"]] == [120, 121]
    assert [member["position"] for member in result["members"]] == [0, 1]
    assert [member["serial"] for member in result["members"]] == [
        fpcs[120]["entPhysicalSerialNum"],
        fpcs[121]["entPhysicalSerialNum"],
    ]
    # The LibreNMS device serial is the root serial, which FPC 0 carries.
    device = unwrap_response(recording["responses"][f"GET /api/v0/devices/{device_id}"])[1]["devices"][0]
    assert identify_vc_master(result["members"], device["serial"], vc_serial_key()) is result["members"][0]
    # Junos reports the member model as the FPC name; the model-name field holds a part number.
    assert [member["model"] for member in result["members"]] == [
        fpcs[120]["entPhysicalName"],
        fpcs[121]["entPhysicalName"],
    ]


def _drop_vc_marker(row):
    row["entPhysicalDescr"] = "entity-19748e"


def _serial_of_no_fpc(row):
    row["entPhysicalSerialNum"] = "SN-ffffff"


@pytest.mark.parametrize(
    ("index", "change"),
    [
        pytest.param(1, _drop_vc_marker, id="root-does-not-name-a-virtual-chassis"),
        pytest.param(1, _serial_of_no_fpc, id="root-serial-matches-no-fpc"),
        pytest.param(121, None, id="single-fpc"),
        pytest.param(121, lambda row: row.update(entPhysicalSerialNum="BUILTIN"), id="fpc-without-a-real-serial"),
        pytest.param(121, lambda row: row.update(entPhysicalSerialNum=""), id="fpc-without-a-serial"),
    ],
)
def test_an_incomplete_fpc_shape_is_not_a_stack(recording_server, index, change):
    """A false stack creates bogus member devices on import, so every condition must hold."""
    recording = copy.deepcopy(load_recording(_RECORDING))
    _edit_rows(recording, index, change)

    assert _detect(recording_server, recording) is None


def test_two_fpcs_that_share_a_serial_are_not_a_stack(recording_server):
    recording = copy.deepcopy(load_recording(_RECORDING))
    master_serial = next(row for row in _rows(recording, _CHILDREN_KEY) if row["entPhysicalIndex"] == 120)[
        "entPhysicalSerialNum"
    ]
    _edit_rows(recording, 121, lambda row: row.update(entPhysicalSerialNum=master_serial))

    assert _detect(recording_server, recording) is None


def test_anonymization_keeps_a_placeholder_fpc_serial_so_the_shape_stays_rejected(recording_server):
    """A BUILTIN FPC serial is no member serial; anonymizing it must not turn the shape into a stack."""
    from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording
    from netbox_librenms_plugin.data_shapes.signature import compute_shape_signature

    recording = copy.deepcopy(load_recording(_RECORDING))
    _edit_rows(recording, 121, lambda row: row.update(entPhysicalSerialNum="BUILTIN"))
    anonymized = anonymize_recording(recording)

    assert _detect(recording_server, recording) is None
    assert _detect(recording_server, anonymized) is None
    assert compute_shape_signature(anonymized) == compute_shape_signature(recording)


@pytest.mark.parametrize("scoped", [False, True], ids=["global", "manufacturer"])
@pytest.mark.parametrize("surface", ["signature", "anonymized-replay"])
def test_recordings_keep_the_production_serial_equality(recording_server, scoped, surface):
    from dcim.models import Manufacturer

    from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording
    from netbox_librenms_plugin.data_shapes.signature import compute_shape_signature
    from netbox_librenms_plugin.import_utils.virtual_chassis import vc_serial_key
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.filter(scope="serial").delete()
    manufacturer = Manufacturer.objects.create(name="Capture maker", slug="capture-maker")
    NormalizationRule.objects.create(
        scope="serial",
        manufacturer=manufacturer if scoped else None,
        match_pattern=r"^S/N\s+(.+)$",
        replacement=r"\1",
    )
    recording = copy.deepcopy(load_recording(_RECORDING))
    _edit_rows(recording, 1, lambda row: row.update(entPhysicalSerialNum="S/N MEMBER-A"))
    _edit_rows(recording, 120, lambda row: row.update(entPhysicalSerialNum="MEMBER-A"))
    _edit_rows(recording, 121, lambda row: row.update(entPhysicalSerialNum="MEMBER-B"))
    device = unwrap_response(recording["responses"][f"GET /api/v0/devices/{_DEVICE_ID}"])[1]["devices"][0]
    device["serial"] = "S/N MEMBER-A"
    recording["expected"]["virtual_chassis"]["member_serials"] = ["MEMBER-A", "MEMBER-B"]
    original = copy.deepcopy(recording)
    assert _detect(recording_server, recording)["member_count"] == 2
    serial_key = vc_serial_key(manufacturer)

    if surface == "signature":
        signature = compute_shape_signature(recording, serial_key=serial_key)
    else:
        anonymized = anonymize_recording(recording, serial_key=serial_key)
        # A recording must replay on a host that has none of the capture's rules.
        NormalizationRule.objects.filter(scope="serial").delete()
        assert _detect(recording_server, anonymized)["member_count"] == 2
        signature = compute_shape_signature(anonymized)
        import json

        assert "MEMBER-A" not in json.dumps(anonymized)
        assert "MEMBER-B" not in json.dumps(anonymized)
    assert signature["virtual_chassis"]["present"] is True
    assert signature["virtual_chassis"]["member_count"] == 2
    assert recording == original


@pytest.mark.parametrize(("pattern", "replacement"), [(r"^CAPTURE-.+()$", r"\1"), (r"^CAPTURE-.+$", "unknown")])
def test_a_serial_rule_that_removes_identity_does_not_publish_the_original_serial(pattern, replacement):
    import json

    from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording
    from netbox_librenms_plugin.import_utils.virtual_chassis import vc_serial_key
    from netbox_librenms_plugin.models import NormalizationRule

    NormalizationRule.objects.filter(scope="serial").delete()
    NormalizationRule.objects.create(scope="serial", match_pattern=pattern, replacement=replacement)
    recording = {"device_id": 1, "responses": {"GET /api/v0/devices/1": {"devices": [{"serial": "CAPTURE-A"}]}}}

    anonymized = anonymize_recording(recording, serial_key=vc_serial_key())

    assert "CAPTURE-A" not in json.dumps(anonymized)
    assert anonymized["responses"][f"GET /api/v0/devices/{anonymized['device_id']}"]["devices"][0]["serial"] == ""
    assert recording["responses"]["GET /api/v0/devices/1"]["devices"][0]["serial"] == "CAPTURE-A"


@pytest.mark.django_db
@pytest.mark.parametrize("scoped", [False, True], ids=["global", "manufacturer"])
def test_capture_download_replays_the_same_normalized_virtual_chassis(client, recording_server, scoped):
    import json
    from copy import deepcopy

    from django.urls import reverse

    from netbox_librenms_plugin.data_shapes.envelope import unwrap_response
    from netbox_librenms_plugin.data_shapes.signature import compute_shape_signature
    from netbox_librenms_plugin.import_utils.virtual_chassis import (
        detect_virtual_chassis_from_inventory,
        identify_vc_master,
        vc_serial_key,
    )
    from netbox_librenms_plugin.models import NormalizationRule
    from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
    from netbox_librenms_plugin.tests.test_data_shapes_view import _captured_recording, _override_servers

    recording = deepcopy(load_recording("juniper-ex4400-vc-2member"))
    device_id = recording["device_id"]
    _edit_rows(recording, 1, lambda row: row.update(entPhysicalSerialNum="S/N CAPTURE-A"))
    _edit_rows(recording, 120, lambda row: row.update(entPhysicalSerialNum="CAPTURE-A"))
    _edit_rows(recording, 121, lambda row: row.update(entPhysicalSerialNum="CAPTURE-B"))
    unwrap_response(recording["responses"][f"GET /api/v0/devices/{device_id}"])[1]["devices"][0]["serial"] = (
        "S/N CAPTURE-A"
    )
    device = make_device("normalized-capture", librenms_cf={"test": {"id": device_id}})
    NormalizationRule.objects.filter(scope="serial").delete()
    NormalizationRule.objects.create(
        scope="serial",
        manufacturer=device.device_type.manufacturer if scoped else None,
        match_pattern=r"^S/N\s+(.+)$",
        replacement=r"\1",
    )
    server, api = recording_server(recording)
    for suffix, field in [
        ("ports", "ports"),
        ("port_stack", "mappings"),
        ("ip", "addresses"),
        ("links", "links"),
        ("transceivers", "transceivers"),
    ]:
        server.register(f"/api/v0/devices/{device_id}/{suffix}", {"status": "ok", field: []})
    assert detect_virtual_chassis_from_inventory(api, device_id)["member_count"] == 2
    client.force_login(make_superuser("normalized-capture-user"))

    with _override_servers(server):
        response = client.get(
            reverse("plugins:netbox_librenms_plugin:capture_data_shape", kwargs={"device_id": device.pk}),
            {"server_key": "test"},
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert b'id="capture-json"' in response.content, response.content.decode()
    captured = _captured_recording(response)
    assert compute_shape_signature(captured)["virtual_chassis"]["member_count"] == 2
    assert "CAPTURE-A" not in json.dumps(captured)
    assert "CAPTURE-B" not in json.dumps(captured)
    NormalizationRule.objects.filter(scope="serial").delete()
    _server, replay_api = recording_server(captured)
    replay_id = captured["device_id"]
    detected = detect_virtual_chassis_from_inventory(replay_api, replay_id)
    assert detected["member_count"] == 2
    ok, replay_device = replay_api.get_device_info(replay_id)
    assert ok
    assert identify_vc_master(detected["members"], replay_device["serial"], vc_serial_key()) is detected["members"][0]
