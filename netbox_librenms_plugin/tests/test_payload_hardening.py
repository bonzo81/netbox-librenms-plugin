"""Malformed LibreNMS payloads must fail closed instead of breaking tab renders."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device


class TestIsListOfDicts:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ([{"a": 1}, {"b": 2}], True),
            ([], True),
            ("oops", False),
            ({"a": 1}, False),
            (None, False),
            ([{"a": 1}, "scalar"], False),
        ],
    )
    def test_payload_shape(self, value, expected):
        from netbox_librenms_plugin.utils import is_list_of_dicts

        assert is_list_of_dicts(value) is expected


def _real_api():
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    return LibreNMSAPI()


@pytest.mark.django_db
class TestVlanCachedPayloadShape:
    @staticmethod
    def _view():
        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        view = BaseVLANTableView()
        view._librenms_api = _real_api()
        view.librenms_id = 1
        return view

    @pytest.mark.parametrize("bad", ["garbage-string", [42], [{"vlan_vlan": 1}, "scalar"]])
    def test_malformed_cached_vlans_are_purged_and_render_empty(self, bad):
        device = make_device(f"vlan-readguard-{type(bad).__name__}")
        view = self._view()
        request = RequestFactory().get("/")
        server_key = view.librenms_api.server_key
        vlans_key = view.get_cache_key(device, "vlans", server_key)
        cache.set(vlans_key, bad, timeout=300)

        context = view.get_vlan_context(request, device, server_key=server_key)

        assert context["vlan_table"] is None
        assert cache.get(vlans_key) is None

    def test_empty_cached_vlans_render_an_empty_table(self):
        device = make_device("vlan-empty-render")
        view = self._view()
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        server_key = view.librenms_api.server_key
        vlans_key = view.get_cache_key(device, "vlans", server_key)
        cache.set(vlans_key, [], timeout=300)

        context = view.get_vlan_context(request, device, server_key=server_key)

        assert context["vlan_table"] is not None
        assert len(list(context["vlan_table"].rows)) == 0


@pytest.mark.django_db
class TestInterfaceCachedPayloadShape:
    @pytest.mark.parametrize("bad_ports", [None, [{"port_id": 1}, "scalar"], "garbage-string"])
    def test_malformed_cached_ports_render_empty_without_error(self, bad_ports):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device(f"iface-cache-readguard-{type(bad_ports).__name__}")
        view = DeviceInterfaceTableView()
        view._librenms_api = _real_api()
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        view.setup(request)
        server_key = view.librenms_api.server_key
        ports_key = view.get_cache_key(device, "ports", server_key)
        cache.set(ports_key, {"ports": bad_ports}, timeout=300)

        context = view.get_context_data(request, device, "ifName", server_key=server_key)

        assert context["table"] is not None
        assert len(list(context["table"].rows)) == 0
        assert context["netbox_only_interfaces"] == []
