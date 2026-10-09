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
_DEVICE_ID = 1002
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
    return detect_virtual_chassis_from_inventory(api, _DEVICE_ID)


def test_fpc_members_carry_their_member_number_serial_and_model(recording_server):
    from netbox_librenms_plugin.import_utils.virtual_chassis import identify_vc_master, vc_serial_key

    recording = load_recording(_RECORDING)
    fpcs = {
        row["entPhysicalIndex"]: row for row in _rows(recording, _CHILDREN_KEY) if row["entPhysicalIndex"] in (120, 121)
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
    device = unwrap_response(recording["responses"][f"GET /api/v0/devices/{_DEVICE_ID}"])[1]["devices"][0]
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
