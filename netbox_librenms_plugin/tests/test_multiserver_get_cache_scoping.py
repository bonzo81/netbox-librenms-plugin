"""Multi-server GET-render cache-scoping regression tests.

On a full page render the orchestrator (object_sync/devices.py) delegates to each tab's
``get_context_data`` without a ``server_key`` and never rebinds the client, so a non-default
server tab must rebind itself to ``?server_key`` — otherwise it reads the *default* server's
cache and renders an empty table right after a successful refresh on the other server.

These exercise the real ``rebind_api_for_server`` + real ``get_cache_key`` + real Django cache;
only ``build_librenms_api`` (the HTTP-client boundary) is patched to hand back a "prod"-scoped
client without contacting a server.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache as real_cache
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device


def _get_request(server_key):
    request = RequestFactory().get("/", {"server_key": server_key})
    request.user = get_user_model().objects.first() or get_user_model().objects.create_user(username="ms-tester")
    return request


@pytest.mark.django_db
class TestMultiServerGetRenderCacheScoping:
    """Each sync tab's GET render must scope its cache read to ?server_key, not the session server."""

    def test_interfaces_tab_rebinds_to_get_server_key(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("ms-iface")
        Interface.objects.create(device=device, name="Gi0/1", type="1000base-t")
        view = DeviceInterfaceTableView()
        view._librenms_api = MagicMock(server_key="default")  # session/default client
        prod_api = MagicMock(server_key="prod")
        request = _get_request("prod")
        view.request = request  # orchestrator sets view.request = copy.copy(request)

        # Seed ONLY the prod-scoped ports cache (real key, real cache). The default-scoped key
        # stays empty, so a render that ignores ?server_key produces an empty table.
        ports_key = view.get_cache_key(device, "ports", "prod")
        real_cache.set(ports_key, {"ports": [{"port_id": 99, "ifName": "Gi0/1", "ifAdminStatus": "up"}]})
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=prod_api):
                ctx = view.get_context_data(request, device, "ifName")
            assert ctx["server_key"] == "prod"
            # Rendered from the prod-scoped cache, so the table is populated.
            assert ctx["table"] is not None
        finally:
            real_cache.delete(ports_key)

    def test_cables_tab_rebinds_to_get_server_key(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = make_device("ms-cable")
        view = DeviceCableTableView()
        view._librenms_api = MagicMock(server_key="default")
        prod_api = MagicMock(server_key="prod")
        request = _get_request("prod")
        view.request = request

        links_key = view.get_cache_key(device, "links", "prod")
        real_cache.set(links_key, {"links": [{"local_port": "Gi0/1", "local_port_id": 11}]})
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=prod_api):
                ctx = view.get_context_data(request, device)
            assert ctx["server_key"] == "prod"
            assert ctx["table"] is not None
        finally:
            real_cache.delete(links_key)

    def test_vlan_tab_rebinds_to_get_server_key(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

        device = make_device("ms-vlan")
        view = DeviceVLANTableView()
        view._librenms_api = MagicMock(server_key="default")
        prod_api = MagicMock(server_key="prod")
        request = _get_request("prod")
        view.request = request

        vlans_key = view.get_cache_key(device, "vlans", "prod")
        real_cache.set(vlans_key, [{"vlan_vlan": 10, "vlan_name": "DATA"}])
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=prod_api):
                ctx = view.get_vlan_context(request, device)
            assert ctx["server_key"] == "prod"
            assert ctx["vlan_table"] is not None
        finally:
            real_cache.delete(vlans_key)


@pytest.mark.django_db
class TestUnresolvedServerKeyRendersEmpty:
    """A GET ?server_key that no longer resolves must render empty, not the default server's data."""

    def _ghost_request(self):
        return _get_request("ghost")

    def test_modules_tab_unresolved_key_renders_empty(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device("ghost-mod")
        view = DeviceModuleTableView()
        view._librenms_api = MagicMock(server_key="default")
        view.request = self._ghost_request()
        # Default-server inventory cache IS populated; it must NOT surface under ?server_key=ghost.
        default_key = view.get_cache_key(device, "inventory", "default")
        real_cache.set(default_key, {"inventory": [{"entPhysicalIndex": 1}], "librenms_id": 1, "oob_librenms_id": None})
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
                ctx = view.get_context_data(view.request, device)
            assert ctx["server_key"] == "ghost"
            assert ctx["table"] is None
        finally:
            real_cache.delete(default_key)

    def test_interfaces_tab_unresolved_key_renders_empty(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("ghost-iface")
        Interface.objects.create(device=device, name="Gi0/1", type="1000base-t")
        view = DeviceInterfaceTableView()
        view._librenms_api = MagicMock(server_key="default")
        view.request = self._ghost_request()
        default_key = view.get_cache_key(device, "ports", "default")
        real_cache.set(default_key, {"ports": [{"port_id": 5, "ifName": "Gi0/1", "ifAdminStatus": "up"}]})
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
                ctx = view.get_context_data(view.request, device, "ifName")
            assert ctx["server_key"] == "ghost"
            assert ctx["table"] is None
        finally:
            real_cache.delete(default_key)

    def test_cables_tab_unresolved_key_renders_empty(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = make_device("ghost-cable")
        view = DeviceCableTableView()
        view._librenms_api = MagicMock(server_key="default")
        view.request = self._ghost_request()
        default_key = view.get_cache_key(device, "links", "default")
        real_cache.set(default_key, {"links": [{"local_port": "Gi0/1", "local_port_id": 11}]})
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
                ctx = view.get_context_data(view.request, device)
            assert ctx["server_key"] == "ghost"
            assert ctx["table"] is None
        finally:
            real_cache.delete(default_key)

    def test_vlan_tab_unresolved_key_renders_empty(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

        device = make_device("ghost-vlan")
        view = DeviceVLANTableView()
        view._librenms_api = MagicMock(server_key="default")
        view.request = self._ghost_request()
        default_key = view.get_cache_key(device, "vlans", "default")
        real_cache.set(default_key, [{"vlan_vlan": 10, "vlan_name": "DATA"}])
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
                ctx = view.get_vlan_context(view.request, device)
            assert ctx["server_key"] == "ghost"
            assert ctx["vlan_table"] is None
        finally:
            real_cache.delete(default_key)
