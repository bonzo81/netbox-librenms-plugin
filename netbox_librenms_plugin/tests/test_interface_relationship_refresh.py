import pytest
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory

from netbox_librenms_plugin.models import PortStackLagPattern
from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView


class _RelationshipAPI:
    """Use the real resolver while replacing only LibreNMS HTTP responses."""

    def __init__(self, resolver, *, device_info, port_stack):
        self._resolver = resolver
        self._device_info = device_info
        self._port_stack = port_stack
        self.device_info_calls = 0
        self.port_stack_calls = 0

    def get_device_info(self, _device_id):
        self.device_info_calls += 1
        return self._device_info

    def get_port_stack(self, _device_id):
        self.port_stack_calls += 1
        return self._port_stack

    def resolve_port_relationships(self, *args, **kwargs):
        return self._resolver.resolve_port_relationships(*args, **kwargs)


def _message_request():
    request = RequestFactory().post("/")
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


@pytest.mark.django_db
class TestInterfaceRelationshipRefresh:
    def test_structural_signal_resolves_the_device_os(self, mock_librenms_api):
        """The OS scopes the SAP colon skip, so a structural-only snapshot must resolve it too."""
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
        ]
        api = _RelationshipAPI(
            mock_librenms_api,
            device_info=(True, {"os": "junos"}),
            port_stack=(True, [{"high_port_id": 10, "low_port_id": 20}]),
        )
        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = api
        view.librenms_id = 42
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        assert api.device_info_calls == 1
        assert api.port_stack_calls == 1
        assert snapshot["port_stack_relationships"]["lag_members"] == {10: 20}
        assert "relationship_data_incomplete" not in snapshot

    def test_junos_breakout_and_aggregate_units_resolve_through_the_refresh(self, mock_librenms_api):
        """The live MX480 shape must survive the whole refresh path, colons and all."""
        # A structural signal alone used to leave the OS unknown, which kept the Nokia SAP skip
        # armed and dropped every relationship a channelized xe-1/1/3:1 takes part in.
        ports = [
            {"port_id": 4601, "ifName": "xe-1/1/3:1", "ifType": "ethernetCsmacd"},
            {"port_id": 4602, "ifName": "xe-1/1/3:1.0", "ifType": "propVirtual"},
            {"port_id": 4603, "ifName": "ae0", "ifType": "ieee8023adLag"},
            {"port_id": 4604, "ifName": "ae0.0", "ifType": "ieee8023adLag"},
        ]
        api = _RelationshipAPI(
            mock_librenms_api,
            device_info=(True, {"os": "junos"}),
            port_stack=(
                True,
                [
                    {"high_port_id": 4601, "low_port_id": 4602},
                    {"high_port_id": 4602, "low_port_id": 4604},
                ],
            ),
        )
        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = api
        view.librenms_id = 42
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        relationships = snapshot["port_stack_relationships"]
        assert relationships["lag_members"] == {4601: 4603}
        # ae0.0 -> ae0 exists in no ifStack row; it comes from the name fallback.
        assert relationships["sub_interfaces"] == {4602: 4601, 4604: 4603}

    def test_unknown_os_keeps_sap_rows_out_and_says_the_snapshot_is_incomplete(self, mock_librenms_api):
        """A structural-only snapshot whose OS lookup fails must not turn a SAP into a LAG member."""
        # Nokia shape: the aggregate is found structurally by ifType, so no LAG name pattern fires
        # and nothing else would have fetched the OS. Scoping the SAP rule by an OS we could not
        # read once left this device with no rule at all, and the SAP became a LAG member.
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},
        ]
        api = _RelationshipAPI(
            mock_librenms_api,
            device_info=(False, "unavailable"),
            port_stack=(
                True,
                [
                    {"high_port_id": 101, "low_port_id": 102},
                    {"high_port_id": 200, "low_port_id": 102},
                ],
            ),
        )
        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = api
        view.librenms_id = 42
        request = _message_request()
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(request, snapshot, ports, "ifName")

        assert snapshot["port_stack_relationships"]["lag_members"] == {101: 102}
        # The user is told the column may be incomplete rather than being shown a wrong edge.
        assert snapshot["relationship_data_incomplete"] is True
        assert any("device OS could not be determined" in str(message) for message in get_messages(request))

    def test_an_unregistered_os_does_not_turn_a_sap_into_a_lag_member(self, mock_librenms_api):
        """LibreNMS reporting an OS name nobody registered must not read as "no SAP notation"."""
        # The device is Nokia-shaped but reports an OS with no PortStackLagPattern row. Scoping
        # the rule to that name alone returned no pattern, and lag1:0 became a LAG member.
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},
        ]
        api = _RelationshipAPI(
            mock_librenms_api,
            device_info=(True, {"os": "sros-unregistered"}),
            port_stack=(
                True,
                [
                    {"high_port_id": 101, "low_port_id": 102},
                    {"high_port_id": 200, "low_port_id": 102},
                ],
            ),
        )
        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = api
        view.librenms_id = 42
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        assert snapshot["port_stack_relationships"]["lag_members"] == {101: 102}

    def test_nokia_sap_rows_stay_excluded_through_the_refresh(self, mock_librenms_api):
        """A Nokia device keeps the SAP skip that the Junos fix scoped away from everyone else."""
        ports = [
            {"port_id": 101, "ifName": "1/1/c1/1", "ifType": "ethernetCsmacd"},
            {"port_id": 102, "ifName": "lag-1", "ifType": "ieee8023adLag"},
            {"port_id": 200, "ifName": "lag1:0", "ifType": "ipForward"},
        ]
        api = _RelationshipAPI(
            mock_librenms_api,
            device_info=(True, {"os": "timos"}),
            port_stack=(
                True,
                [
                    {"high_port_id": 101, "low_port_id": 102},
                    {"high_port_id": 200, "low_port_id": 102},
                ],
            ),
        )
        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = api
        view.librenms_id = 42
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        assert snapshot["port_stack_relationships"]["lag_members"] == {101: 102}

    def test_mixed_structural_and_name_signals_warn_when_os_is_unknown(self, mock_librenms_api):
        # "ios" is one of the rows migration 0013 seeds, so take the existing pattern.
        PortStackLagPattern.objects.get_or_create(librenms_os="ios", defaults={"lag_name_pattern": r"^Po\d+$"})
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
            {"port_id": 30, "ifName": "Ethernet2", "ifType": "ethernetCsmacd"},
            {"port_id": 40, "ifName": "Po2", "ifType": "propVirtual"},
        ]
        api = _RelationshipAPI(
            mock_librenms_api,
            device_info=(False, "unavailable"),
            port_stack=(
                True,
                [
                    {"high_port_id": 10, "low_port_id": 20},
                    {"high_port_id": 30, "low_port_id": 40},
                ],
            ),
        )
        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = api
        view.librenms_id = 42
        request = _message_request()
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(request, snapshot, ports, "ifName")

        assert api.device_info_calls == 1
        assert api.port_stack_calls == 1
        assert snapshot["port_stack_relationships"]["lag_members"] == {10: 20}
        assert snapshot["relationship_data_incomplete"] is True
        assert any("device OS could not be determined" in str(message) for message in get_messages(request))

    def test_structural_signal_marks_snapshot_incomplete_when_port_stack_fetch_fails(self, mock_librenms_api):
        """A failed structural relationship fetch marks the rendered snapshot incomplete."""
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
        ]
        api = _RelationshipAPI(
            mock_librenms_api,
            device_info=(True, {"os": "junos"}),
            port_stack=(False, "unavailable"),
        )
        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = api
        view.librenms_id = 42
        request = _message_request()
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(request, snapshot, ports, "ifName")

        assert api.device_info_calls == 1
        assert api.port_stack_calls == 1
        assert snapshot["relationship_data_incomplete"] is True
        assert any("relationship data could not be fetched" in str(message) for message in get_messages(request))
