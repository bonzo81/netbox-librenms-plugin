"""Outcome tests driven by captured LibreNMS data-shape recordings.

Each recording in ``tests/recordings/*.json`` is replayed through the mock
LibreNMS HTTP server and the real LibreNMSAPI client, then the real detection
and relationship-resolution logic runs against it and is asserted against the
recording's ``expected`` block. A new recording with an ``expected`` block
becomes a passing test with no new code.

The flow is exercised end-to-end (real client, real HTTP, real parsing); only
the plugin-config lookup and the VC member-name pattern (a DB read) are stubbed,
so these tests need no database.
"""

from unittest.mock import patch

import pytest

from netbox_librenms_plugin.tests.recordings import iter_recording_paths, iter_recordings

_RECORDINGS = iter_recordings()


def _ids(recording):
    return recording.get("name", "unnamed")


def test_recordings_present():
    """The recordings directory must contain at least one scenario."""
    assert iter_recording_paths(), "no recording JSON files found in tests/recordings/"


@pytest.mark.parametrize("recording", _RECORDINGS, ids=_ids)
def test_recording_has_required_schema(recording):
    """Every recording declares the keys the replay harness depends on."""
    assert recording.get("schema_version") == 1
    assert recording.get("name")
    assert isinstance(recording.get("device_id"), int)
    assert isinstance(recording.get("responses"), dict) and recording["responses"]
    assert isinstance(recording.get("expected"), dict) and recording["expected"], (
        f"{recording.get('name')} has no expected outcomes to assert"
    )


def _assert_virtual_chassis(api, device_id, expected):
    from netbox_librenms_plugin.import_utils.virtual_chassis import detect_virtual_chassis_from_inventory

    with patch(
        "netbox_librenms_plugin.import_utils.virtual_chassis._load_vc_member_name_pattern",
        return_value="{master}-m{position}",
    ):
        result = detect_virtual_chassis_from_inventory(api, device_id)

    if expected is None:
        assert result is None
        return

    assert result is not None
    assert result["is_stack"] == expected["is_stack"]
    assert result["member_count"] == expected["member_count"]
    if "member_serials" in expected:
        assert [m["serial"] for m in result["members"]] == expected["member_serials"]


def _assert_port_relationships(api, device_id, recording, expected):
    ok_ports, ports_data = api.get_ports(device_id)
    assert ok_ports, ports_data
    ok_stack, port_stack = api.get_port_stack(device_id)
    assert ok_stack, port_stack

    relationships = api.resolve_port_relationships(
        ports_data["ports"],
        port_stack,
        # Pass patterns explicitly (default {}) so resolution never touches the DB.
        lag_patterns=recording.get("lag_patterns", {}),
        device_os=recording.get("meta", {}).get("os"),
    )

    # port_stack and ports are independent payloads whose ids may differ in type,
    # so compare both sides normalised to str -- matching how production keys lookups.
    for key in ("lag_members", "sub_interfaces"):
        if key in expected:
            got = {str(k): str(v) for k, v in relationships[key].items()}
            want = {str(k): str(v) for k, v in expected[key].items()}
            assert got == want, f"{recording['name']} {key}: {got} != {want}"


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
