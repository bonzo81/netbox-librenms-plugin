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
    def test_structural_signal_skips_device_info_request(self, mock_librenms_api):
        ports = [
            {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            {"port_id": 20, "ifName": "Bundle1", "ifType": "ieee8023adLag"},
        ]
        api = _RelationshipAPI(
            mock_librenms_api,
            device_info=(False, "must not be requested"),
            port_stack=(True, [{"port_id_high": 10, "port_id_low": 20}]),
        )
        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = api
        view.librenms_id = 42
        snapshot = {"ports": ports}

        view._enrich_port_stack_relationships(_message_request(), snapshot, ports, "ifName")

        assert api.device_info_calls == 0
        assert api.port_stack_calls == 1
        assert snapshot["port_stack_relationships"]["lag_members"] == {10: 20}
        assert "relationship_data_incomplete" not in snapshot

    def test_mixed_structural_and_name_signals_warn_when_os_is_unknown(self, mock_librenms_api):
        PortStackLagPattern.objects.create(librenms_os="ios", lag_name_pattern=r"^Po\d+$")
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
                    {"port_id_high": 10, "port_id_low": 20},
                    {"port_id_high": 30, "port_id_low": 40},
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
