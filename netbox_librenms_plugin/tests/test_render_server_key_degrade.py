"""The modules/interfaces tab renders must degrade (not 500) when the client is unbuildable.

The lazy ``librenms_api`` property raises KeyError/ValueError when the LibreNMS server
config is missing or misconfigured. The cables and IP tabs resolve their render-path
server key through ``_render_server_key`` (None fallback); these tests pin the same
degrade on the modules and interfaces tabs, which resolved through the raising property.
"""

from copy import deepcopy

import pytest
from django.conf import settings
from django.test import RequestFactory
from django.test import override_settings

from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server


def _make_device(name):
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name="Degrade-Mfr", slug="degrade-mfr")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="Degrade-DT", slug="degrade-dt")
    role, _ = DeviceRole.objects.get_or_create(name="Degrade-Role", slug="degrade-role")
    site, _ = Site.objects.get_or_create(name="Degrade-Site", slug="degrade-site")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site, status="active")


def _patch_unbuildable_config():
    """No servers dict and no legacy url/token: LibreNMSAPI() raises ValueError."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"] = {"servers": {}, "librenms_url": "", "api_token": ""}
    return override_settings(PLUGINS_CONFIG=plugin_config)


@pytest.fixture
def librenms_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        yield server


def _api_for(server):
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "alpha": {"librenms_url": server.url, "api_token": "test-token"}
    }
    with override_settings(PLUGINS_CONFIG=plugin_config):
        return LibreNMSAPI(server_key="alpha")


@pytest.mark.django_db
class TestModulesTabDegradesOnUnbuildableClient:
    def test_get_context_data_returns_empty_panel_not_500(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = _make_device("degrade-mod-dev")
        request = RequestFactory().get("/")
        view = DeviceModuleTableView()
        view.request = request

        with _patch_unbuildable_config():
            context = view.get_context_data(request, device)

        assert context["table"] is None
        assert context["object"] == device
        assert context["server_key"] is None


@pytest.mark.django_db
class TestInterfacesTabDegradesOnUnbuildableClient:
    def test_get_context_data_renders_with_none_key_not_500(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = _make_device("degrade-if-dev")
        request = RequestFactory().get("/")
        view = DeviceInterfaceTableView()
        view.request = request

        with _patch_unbuildable_config():
            context = view.get_context_data(request, device, interface_name_field="ifName")

        # Degrades to the empty cached-table shape under the None (default) scope.
        assert context["table"] is None


@pytest.mark.django_db
class TestCablesPortsCacheShapeGuard:
    """get_ports_data must treat a truthy but malformed ports cache entry as a miss."""

    def test_corrupt_ports_cache_falls_through_to_live_fetch(self, librenms_server):
        from django.core.cache import cache as real_cache

        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = _make_device("degrade-cable-dev")
        view = DeviceCableTableView()
        view.request = RequestFactory().get("/")
        view.librenms_id = 4242
        view._librenms_api = _api_for(librenms_server)
        librenms_server.ports_response(device_id=4242, ports=[{"port_id": 1, "ifName": "eth0"}])

        cache_key = view.get_cache_key(device, "ports", "alpha")
        real_cache.set(cache_key, ["corrupt", "legacy", "shape"], timeout=60)
        try:
            ports_data = view.get_ports_data(device)

            # The corrupt entry is not returned (get_links_data would .get("ports") on it)...
            assert isinstance(ports_data, dict)
            assert ports_data["ports"][0]["ifName"] == "eth0"
            # ...and is purged so the next read doesn't serve it again.
            assert real_cache.get(cache_key) is None
        finally:
            real_cache.delete(cache_key)


@pytest.mark.django_db
class TestVlanTabDegradesOnUnbuildableClient:
    """The VLAN tab must resolve its render-path server key through _render_server_key too.

    get_vlan_context read the key via getattr(self.librenms_api, "server_key", None); the lazy
    librenms_api property raises ValueError (not AttributeError) on an unbuildable config, so getattr
    could not swallow it and the VLAN tab 500'd where the sibling tabs already degraded.
    """

    def test_get_vlan_context_returns_none_key_not_500(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

        device = _make_device("degrade-vlan-dev")
        request = RequestFactory().get("/")
        view = DeviceVLANTableView()
        view.request = request

        with _patch_unbuildable_config():
            context = view.get_vlan_context(request, device)

        assert context["server_key"] is None
        assert context["vlan_table"] is None
        assert context["object"] == device
