"""Unit tests for the shared utils helpers extracted to de-duplicate view logic.

* ``is_valid_ports_payload`` — the ports-payload shape gate used by the interface and module
  refresh paths (host, OOB and cached snapshots).
* ``resolve_server_mapping_display_id`` — the per-server display-id resolver (host id, else the
  nested OOB controller id) shared by the device-sync page and the import-validation modal.

Both are pure functions, so these exercise the real implementations directly with no test doubles.
"""

import pytest

from netbox_librenms_plugin.utils import is_valid_ports_payload, resolve_server_mapping_display_id


class TestIsValidPortsPayload:
    @pytest.mark.parametrize(
        "payload",
        [
            {"ports": [{"port_id": 1}, {"port_id": 2}]},
            {"ports": [{}]},
            {"ports": []},  # empty but well-formed
            {"ports": [], "count": 0},  # extra keys are fine
        ],
    )
    def test_valid_shapes(self, payload):
        assert is_valid_ports_payload(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            "ports",
            42,
            {},  # missing "ports" -> .get returns None, not a list
            {"ports": None},
            {"ports": {}},  # dict, not list
            {"ports": "x"},
            {"ports": [{}, 5]},  # a non-dict row
            {"ports": [None]},
        ],
    )
    def test_invalid_shapes(self, payload):
        assert is_valid_ports_payload(payload) is False


class TestResolveServerMappingDisplayId:
    def test_scalar_valid_int(self):
        assert resolve_server_mapping_display_id(42) == (42, False)

    def test_scalar_valid_digit_string(self):
        assert resolve_server_mapping_display_id("42") == (42, False)

    @pytest.mark.parametrize("entry", [0, -1, "0", "abc", None, True, False])
    def test_scalar_invalid(self, entry):
        assert resolve_server_mapping_display_id(entry) == (None, False)

    def test_dict_host_id_wins(self):
        assert resolve_server_mapping_display_id({"id": 10}) == (10, False)

    def test_dict_host_id_wins_over_oob(self):
        # A valid host id is preferred; the OOB fallback is not consulted.
        assert resolve_server_mapping_display_id({"id": 10, "oob": {"id": 7}}) == (10, False)

    def test_dict_falls_back_to_oob_when_host_absent(self):
        assert resolve_server_mapping_display_id({"oob": {"id": 7}}) == (7, True)

    def test_dict_falls_back_to_oob_when_host_invalid(self):
        # Host id present but corrupt (0) -> still surface the real OOB-only linkage.
        assert resolve_server_mapping_display_id({"id": 0, "oob": {"id": 7}}) == (7, True)

    def test_dict_neither_host_nor_oob(self):
        assert resolve_server_mapping_display_id({"_migrated_to": "prod"}) == (None, False)

    def test_dict_host_and_oob_both_invalid(self):
        assert resolve_server_mapping_display_id({"id": 0, "oob": {"id": -1}}) == (None, False)

    def test_dict_oob_not_a_dict(self):
        assert resolve_server_mapping_display_id({"id": 0, "oob": 5}) == (None, False)
