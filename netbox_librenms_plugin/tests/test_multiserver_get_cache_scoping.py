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
from django.core.cache import cache as real_cache
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip, make_superuser

# A real cache backend that does NOT expose .ttl() (Django's default LocMemCache, like
# Memcached/DB backends — only django-redis exposes ttl()). Used to prove the cable tab's
# get_context_data degrades instead of raising AttributeError on a non-Redis backend.
_NO_TTL_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "no-ttl"}}


def _get_request(server_key):
    """Build an authorized GET request so permission filtering cannot hide cache-scoping failures."""
    request = RequestFactory().get("/", {"server_key": server_key})
    request.user = make_superuser()
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

    def test_ip_tab_rebinds_to_get_server_key(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device = make_device("ms-ip")
        view = DeviceIPAddressTableView()
        view._librenms_api = MagicMock(server_key="default")
        prod_api = MagicMock(server_key="prod")
        request = _get_request("prod")
        view.request = request

        # Seed ONLY the prod-scoped IP cache (mgmt_ip + ports_by_id present so the cached render
        # makes no live LibreNMS calls). The default-scoped key stays empty, so a render that
        # ignores ?server_key cache-misses and renders an empty table.
        ip_key = view.get_cache_key(device, "ip_addresses", "prod")
        real_cache.set(ip_key, {"ip_addresses": [], "mgmt_ip": "", "ports_by_id": {}})
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=prod_api):
                ctx = view.get_context_data(request, device)
            assert ctx["server_key"] == "prod"
            # Rendered from the prod-scoped cache (truthy entry), so the table is built.
            assert ctx["table"] is not None
        finally:
            real_cache.delete(ip_key)

    def test_modules_build_context_receives_scoped_server_key(self):
        """Verify module context receives the resolved server key explicitly instead of relying on the rebound client."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device("scoped-mod")
        view = DeviceModuleTableView()
        view._librenms_api = MagicMock(server_key="default")
        prod_api = MagicMock(server_key="prod")
        prod_api.get_librenms_id.return_value = 7
        request = _get_request("prod")
        view.request = request

        inv_key = view.get_cache_key(device, "inventory", "prod")
        real_cache.set(inv_key, {"inventory": [{"entPhysicalIndex": 1}], "librenms_id": 7, "oob_librenms_id": None})

        captured = {}

        def _spy(*args, **kwargs):
            captured["server_key"] = kwargs.get("server_key")
            return {"table": None, "object": device}

        try:
            with (
                patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=prod_api),
                patch("netbox_librenms_plugin.views.base.modules_view.get_librenms_oob", return_value=None),
                patch.object(view, "_build_context", side_effect=_spy),
            ):
                view.get_context_data(request, device)
            assert captured.get("server_key") == "prod"
        finally:
            real_cache.delete(inv_key)


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

    def test_ip_tab_unresolved_key_renders_empty(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device = make_device("ghost-ip")
        view = DeviceIPAddressTableView()
        view._librenms_api = MagicMock(server_key="default")
        view.request = self._ghost_request()
        # Default-server IP cache IS populated; it must NOT surface under ?server_key=ghost.
        default_key = view.get_cache_key(device, "ip_addresses", "default")
        real_cache.set(default_key, {"ip_addresses": [], "mgmt_ip": "", "ports_by_id": {}})
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
                ctx = view.get_context_data(view.request, device)
            assert ctx["server_key"] == "ghost"
            assert ctx["table"] is None
        finally:
            real_cache.delete(default_key)

    def test_ip_tab_unresolved_key_preserves_action_context(self):
        """An unresolved IP render keeps the move candidates and set-primary preference."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device = make_device("ghost-ip-actions")
        winner = make_device("ghost-ip-actions-winner")
        device.custom_field_data["librenms_id"] = {
            "ghost": {
                "_migrated_to": {
                    "device_id": winner.pk,
                    "server_key": "ghost",
                }
            }
        }
        device.save(update_fields=["custom_field_data"])
        interface = make_interface(device, "eth0")
        ip = make_ip("198.18.0.1/24", assigned_object=interface)
        view = DeviceIPAddressTableView()
        view._librenms_api = MagicMock(server_key="default")
        view.request = self._ghost_request()

        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
            ctx = view.get_context_data(view.request, device)

        assert ctx["movable_ips"] == [
            {
                "id": ip.pk,
                "address": "198.18.0.1/24",
                "interface_name": "eth0",
            }
        ]
        assert ctx["set_primary_ip"] is False

    # The two tests above seed the DEFAULT-server cache and prove the request doesn't fall back to
    # it — but they pass even unfixed, because the read is already scoped to the requested key so
    # default's cache never surfaces. The real regression is subtler: when the UNRESOLVED requested
    # server's OWN cache still holds a stale snapshot, the pre-fix code renders it while the
    # per-object librenms_id index is built against the default-bound client (the failed rebind left
    # librenms_api on the default) — mismatching already-synced rows. These seed the *requested*
    # (ghost) key to exercise that path; only honoring the `unresolved` flag renders empty.

    def test_interfaces_unresolved_key_ignores_that_servers_own_stale_cache(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("ghost-iface-stale")
        Interface.objects.create(device=device, name="Gi0/1", type="1000base-t")
        view = DeviceInterfaceTableView()
        view._librenms_api = MagicMock(server_key="default")
        view.request = self._ghost_request()
        ghost_key = view.get_cache_key(device, "ports", "ghost")
        real_cache.set(ghost_key, {"ports": [{"port_id": 5, "ifName": "Gi0/1", "ifAdminStatus": "up"}]})
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
                ctx = view.get_context_data(view.request, device, "ifName")
            assert ctx["server_key"] == "ghost"
            # Unfixed renders the stale ghost ports (table populated); fixed forces a miss → None.
            assert ctx["table"] is None
        finally:
            real_cache.delete(ghost_key)

    def test_ip_unresolved_key_ignores_that_servers_own_stale_cache(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        device = make_device("ghost-ip-stale")
        view = DeviceIPAddressTableView()
        view._librenms_api = MagicMock(server_key="default")
        view.request = self._ghost_request()
        ghost_key = view.get_cache_key(device, "ip_addresses", "ghost")
        real_cache.set(ghost_key, {"ip_addresses": [], "mgmt_ip": "", "ports_by_id": {}})
        try:
            with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
                ctx = view.get_context_data(view.request, device)
            assert ctx["server_key"] == "ghost"
            # Unfixed renders from the stale ghost IP cache (truthy entry → table); fixed short-circuits.
            assert ctx["table"] is None
        finally:
            real_cache.delete(ghost_key)


@pytest.mark.django_db
def test_cables_get_render_survives_backend_without_ttl(settings):
    """Verify cable GET rendering supports cache backends without ttl(), such as Django LocMemCache."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

    settings.CACHES = _NO_TTL_CACHE  # rebuilds the default-cache proxy to a backend without .ttl()
    assert not hasattr(real_cache, "ttl"), "test precondition: backend must lack .ttl()"

    device = make_device("nottl-cable")
    view = DeviceCableTableView()
    view._librenms_api = MagicMock(server_key="prod")
    prod_api = MagicMock(server_key="prod")
    request = _get_request("prod")
    view.request = request

    # Seed the (LocMemCache) links cache so the render takes the cache-hit path that reaches the
    # cache.ttl() backfill + expiry computation rather than returning early on a miss.
    links_key = view.get_cache_key(device, "links", "prod")
    real_cache.set(links_key, {"links": [{"local_port": "Gi0/1", "local_port_id": 11}]})
    try:
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=prod_api):
            # Must not raise AttributeError: 'LocMemCache' object has no attribute 'ttl'.
            ctx = view.get_context_data(request, device)
        assert ctx["server_key"] == "prod"
        assert ctx["table"] is not None
        # No .ttl() on this backend → expiry degrades to None rather than crashing.
        assert ctx["cache_expiry"] is None
    finally:
        real_cache.delete(links_key)
