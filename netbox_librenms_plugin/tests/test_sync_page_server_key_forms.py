"""The device sync page's POST forms must carry the active server_key.

Every device-info action (name/type/serial/platform/location sync, legacy-ID
conversion) and every tab refresh form rebinds server-side from the POSTed
``server_key``. A form that omits it silently falls back to the GLOBAL selected
server — a wrong-server write when the user is acting on a ``?server_key`` tab.

These tests render the REAL page through the real view (real device, real URL
routing, real template) with only the LibreNMS HTTP boundary patched, then
assert each form contains the hidden ``server_key`` input scoped to the tab.
"""

import os
from unittest.mock import patch

import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server as run_librenms_server

pytestmark = pytest.mark.django_db

TWO_SERVERS = {
    "default": {
        "librenms_url": "https://librenms-default.example.com",
        "api_token": "default-token-12345",
    },
    "secondary": {
        "librenms_url": "https://librenms-secondary.example.com",
        "api_token": "secondary-token-67890",
    },
}

DEVICE_INFO = {
    "device_id": 42,
    "sysName": "lnms-sysname.example.com",
    "hostname": "lnms-sysname.example.com",
    "ip": "10.99.0.1",
    "hardware": "TestHW-9000",
    "serial": "LNMS-SER-1",
    "os": "testos",
    "version": "1.0",
    "features": "-",
    "location": "LNMS-DC-1",
}


def _render_sync_page(device, query=""):
    """GET the LibreNMS sync page through the real view; return decoded HTML."""
    from django.contrib.auth import get_user_model

    from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

    user = get_user_model().objects.filter(username="sync-page-su").first()
    if user is None:
        user = get_user_model().objects.create_superuser(username="sync-page-su")

    request = RequestFactory().get(f"/x/{query}")
    request.user = user
    request.htmx = False
    SessionMiddleware(lambda _request: None).process_request(request)

    view = DeviceLibreNMSSyncView()
    view.setup(request, pk=device.pk)

    with (
        patch(
            "netbox_librenms_plugin.librenms_api.get_plugin_config",
            side_effect=lambda _plugin, key, default=None: TWO_SERVERS if key == "servers" else default,
        ),
        patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_device_info",
            return_value=(True, dict(DEVICE_INFO)),
        ),
    ):
        response = view.get(request, pk=device.pk)
    return response.content.decode()


def _enclosing_form(html, marker):
    """Return the <form>...</form> block whose body contains *marker*."""
    pos = html.find(marker)
    assert pos != -1, f"marker {marker!r} not found in rendered page"
    start = html.rfind("<form", 0, pos)
    end = html.find("</form>", pos)
    assert start != -1 and end != -1, f"no enclosing form around {marker!r}"
    return html[start:end]


class TestAddDeviceFormsScopeToTheActiveServer:
    """The Add-device forms build their choices from the server the page is scoped to."""

    def _render_unknown_device(self, device, query):
        """Render the page for a device LibreNMS does not know, so the Add-device forms appear.

        Both servers are real loopback LibreNMS instances, so the request path, headers and
        response parsing are exercised rather than stubbed. Returns ``(response, requested)``,
        where *requested* holds one ``(server_key, path)`` pair per request that actually
        reached either server.
        """
        from django.contrib.auth import get_user_model

        from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

        user = get_user_model().objects.filter(username="poller-scope-su").first()
        if user is None:
            user = get_user_model().objects.create_superuser(username="poller-scope-su")

        request = RequestFactory().get(f"/x/{query}")
        request.user = user
        request.htmx = False
        SessionMiddleware(lambda _request: None).process_request(request)

        view = DeviceLibreNMSSyncView()
        view.setup(request, pk=device.pk)

        requested = []

        def _route(server_key, status, body):
            """Register-able route that records which server was asked for which path."""

            def route(*, path, **_kwargs):
                requested.append((server_key, path))
                return status, body

            return route

        with run_librenms_server() as default_server, run_librenms_server() as secondary_server:
            live = {"default": default_server, "secondary": secondary_server}
            for server_key, server in live.items():
                # Absent from LibreNMS (a real 404), so the page offers the Add-device forms.
                server.register(
                    f"/api/v0/devices/{DEVICE_INFO['device_id']}",
                    _route(server_key, 404, {"status": "error", "message": "device not found"}),
                )
                # Registered on BOTH servers so a request to the wrong one is recorded rather
                # than lost in the handler's catch-all 404.
                server.register(
                    "/api/v0/poller_group",
                    _route(server_key, 200, {"status": "ok", "get_poller_group": []}),
                )
            servers = {key: {**TWO_SERVERS[key], "librenms_url": live[key].url} for key in TWO_SERVERS}
            with (
                patch.dict(
                    os.environ,
                    {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
                ),
                patch(
                    "netbox_librenms_plugin.librenms_api.get_plugin_config",
                    side_effect=lambda _plugin, key, default=None: servers if key == "servers" else default,
                ),
            ):
                response = view.get(request, pk=device.pk)
        return response, requested

    def test_poller_group_choices_come_from_the_active_server(self):
        """A secondary-server page must not offer the default server's poller groups."""
        from django.core.cache import cache

        # The choices are cached per server key; clear so this render does the lookup.
        cache.delete("librenms_poller_group_choices_secondary")
        cache.delete("librenms_poller_group_choices_default")

        device = make_device("poller-scope", librenms_cf={"secondary": 42})
        _response, requested = self._render_unknown_device(device, "?server_key=secondary")

        poller_requests = [(key, path) for key, path in requested if "poller_group" in path]
        assert poller_requests, "the page never asked for poller groups, so the check below is vacuous"
        assert not [key for key, _path in poller_requests if key == "default"], (
            f"the secondary-server page asked the DEFAULT server for poller groups: {poller_requests}"
        )

    def test_a_stale_server_key_asks_no_server_for_poller_groups(self):
        """The fail-closed page must not fall back to the installation default for its choices."""
        from django.core.cache import cache

        cache.delete("librenms_poller_group_choices_default")
        cache.delete("librenms_poller_group_choices_secondary")
        cache.delete("librenms_poller_group_choices_ghost")
        device = make_device("poller-stale", librenms_cf={"secondary": 42})
        _response, requested = self._render_unknown_device(device, "?server_key=ghost")

        assert not [(key, path) for key, path in requested if "poller_group" in path], (
            f"a stale server selection asked for poller groups anyway: {requested}"
        )


class TestSyncPageFormsCarryServerKey:
    """Rendered with ?server_key=secondary, every POST form must scope to it."""

    def _device(self):
        # Legacy bare-int librenms_id → resolvable on any server + Convert-ID form renders.
        # Serial/type/platform differ from LibreNMS values → the sync forms render.
        from dcim.models import DeviceType, Manufacturer, Platform

        device = make_device("sync-page-forms", serial="NB-SER-1", librenms_cf=42)
        # A DeviceType matching the LibreNMS hardware string (≠ the device's own type)
        # → the Device Type sync form renders.
        mfr = Manufacturer.objects.get(slug="test-mfr")
        DeviceType.objects.get_or_create(manufacturer=mfr, model="TestHW-9000", defaults={"slug": "testhw-9000"})
        # A Platform matching the LibreNMS OS (device has no platform) → the Platform sync form renders.
        Platform.objects.get_or_create(name="testos", defaults={"slug": "testos"})
        return device

    @pytest.fixture
    def html(self):
        return _render_sync_page(self._device(), "?server_key=secondary")

    @pytest.mark.parametrize(
        "action_name",
        [
            "update_device_name",
            "update_device_type",
            "update_device_serial",
            "update_device_platform",
            "update_device_location",
            "convert_legacy_librenms_id",
        ],
    )
    def test_device_info_form_posts_server_key(self, html, action_name):
        """Each device-info action form carries the tab's server_key hidden input."""
        form = _enclosing_form(html, reverse_fragment(action_name))
        assert 'name="server_key"' in form and 'value="secondary"' in form, (
            f"{action_name} form must post server_key=secondary; got: {form[:400]}"
        )

    @pytest.mark.parametrize(
        "refresh_url_name",
        [
            "device_interface_sync",
            "device_cable_sync",
            "device_ipaddress_sync",
            "device_vlan_sync",
            "device_module_sync",
        ],
    )
    def test_tab_refresh_form_posts_server_key(self, html, refresh_url_name):
        """Each tab's Refresh form carries the tab's server_key hidden input."""
        form = _enclosing_form(html, reverse_fragment(refresh_url_name))
        assert 'name="server_key"' in form and 'value="secondary"' in form, (
            f"{refresh_url_name} refresh form must post server_key=secondary; got: {form[:400]}"
        )


def reverse_fragment(url_name):
    """Reverse a plugin URL for a placeholder pk and strip the pk-specific tail."""
    from django.urls import reverse

    from dcim.models import Device

    pk = Device.objects.get(name="sync-page-forms").pk
    return reverse(f"plugins:netbox_librenms_plugin:{url_name}", kwargs={"pk": pk})


class TestSyncPageMisconfiguredDefaultDegrades:
    """Verify that a broken default degrades the sync page without lazy API client reconstruction or a 500."""

    def test_get_with_broken_default_renders_degraded_page(self):
        from django.contrib.auth import get_user_model
        from django.contrib.messages.storage.fallback import FallbackStorage

        from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

        device = make_device("sync-page-degraded")
        user = get_user_model().objects.create_superuser(username="sync-degraded-su")

        request = RequestFactory().get("/x/")  # plain GET, no ?server_key
        request.user = user
        request.htmx = False
        request.session = {}
        request._messages = FallbackStorage(request)

        view = DeviceLibreNMSSyncView()
        view.setup(request, pk=device.pk)

        with (
            # The default server can't build a client (config typo / rotated secret)...
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None),
            # ...so any lazy LibreNMSAPI() reconstruction would raise — exactly what a
            # misconfigured default does in production.
            patch(
                "netbox_librenms_plugin.views.mixins.LibreNMSAPI",
                side_effect=ValueError("LibreNMS URL or API token is not configured"),
            ),
        ):
            response = view.get(request, pk=device.pk)

        assert response.status_code == 200
        assert "not configured correctly" in response.content.decode()

    def test_get_with_stale_server_key_and_broken_default_renders_degraded_page(self):
        """Verify that a stale server key with a broken default renders through the active key fallback without a 500."""
        from django.contrib.auth import get_user_model
        from django.contrib.messages.storage.fallback import FallbackStorage

        from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

        device = make_device("sync-page-degraded-stale")
        user = get_user_model().objects.create_superuser(username="sync-degraded-stale-su")

        assert "gone-server" not in TWO_SERVERS  # the key really is stale under the pinned config

        request = RequestFactory().get("/x/", {"server_key": "gone-server"})
        request.user = user
        request.htmx = False
        request.session = {}
        request._messages = FallbackStorage(request)

        view = DeviceLibreNMSSyncView()
        view.setup(request, pk=device.pk)

        with (
            # Pin the configured servers (as _render_sync_page does) so "gone-server" is stale
            # against a real two-server config rather than an unconfigured plugin.
            patch(
                "netbox_librenms_plugin.librenms_api.get_plugin_config",
                side_effect=lambda _plugin, key, default=None: TWO_SERVERS if key == "servers" else default,
            ),
            # Neither the requested key nor the default can build a client...
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None) as mock_build,
            # ...so any lazy LibreNMSAPI() reconstruction would raise, as in production.
            patch(
                "netbox_librenms_plugin.views.mixins.LibreNMSAPI",
                side_effect=ValueError("LibreNMS URL or API token is not configured"),
            ),
        ):
            response = view.get(request, pk=device.pk)

        # The stale key was routed to the factory (so the unresolved branch is the one taken),
        # and the broken default was then tried as the fallback bind.
        build_keys = [c.args[0] for c in mock_build.call_args_list]
        assert "gone-server" in build_keys
        assert None in build_keys
        assert response.status_code == 200
        # The header's server-info block degrades to the configuration-error display
        # (get_server_info's fail-soft branch) instead of the page 500ing...
        assert "Configuration error" in response.content.decode()
        # ...and the page is NOT the minimal early-return render the *blank*-key branch produces
        # (the unresolved path must still render the tabbed page scoped to the requested key).
        assert view._server_key_unresolved is True
        assert view._scoped_render_server_key == "gone-server"
        assert view.librenms_id is None  # failed closed: no default-server mapping attributed


class TestUpdateDeviceLocationRebindsServer:
    """UpdateDeviceLocationView must write to the POSTed server, not the global default."""

    def test_location_write_goes_to_posted_server(self):
        """POSTing server_key=secondary rebinds the client before update_device_field."""
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        device = make_device("loc-rebind", librenms_cf=42)
        request = RequestFactory().post("/x/", {"server_key": "secondary"})
        request.user = MagicMock(is_superuser=True)
        request.user.has_perm.return_value = True
        request._messages = MagicMock()

        view = UpdateDeviceLocationView()
        view.setup(request, pk=device.pk)

        secondary_api = MagicMock()
        secondary_api.server_key = "secondary"
        secondary_api.get_librenms_id.return_value = 42
        secondary_api.update_device_field.return_value = (True, "ok")

        def _build(key):
            assert key == "secondary", f"expected rebind to 'secondary', got {key!r}"
            return secondary_api

        with patch(
            "netbox_librenms_plugin.librenms_api.build_librenms_api",
            side_effect=_build,
        ) as mock_build:
            response = view.post(request, pk=device.pk)

        # The write ran on the secondary-bound client, never on a lazily-built default.
        mock_build.assert_called_once_with("secondary")
        secondary_api.update_device_field.assert_called_once()
        # Redirect preserves the acting server's tab.
        assert response.status_code == 302
        assert "server_key=secondary" in response["Location"]

    def test_stale_server_key_fails_closed(self):
        """A POSTed key that no longer resolves errors out without any LibreNMS write."""
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.views.sync.devices import UpdateDeviceLocationView

        device = make_device("loc-rebind-stale", librenms_cf=42)
        request = RequestFactory().post("/x/", {"server_key": "ghost"})
        request.user = MagicMock(is_superuser=True)
        request.user.has_perm.return_value = True
        request._messages = MagicMock()

        view = UpdateDeviceLocationView()
        view.setup(request, pk=device.pk)

        with patch(
            "netbox_librenms_plugin.librenms_api.build_librenms_api",
            return_value=None,
        ):
            response = view.post(request, pk=device.pk)

        assert response.status_code == 302
        # No client was ever bound — nothing could have been written.
        assert getattr(view, "_librenms_api", None) is None


@pytest.mark.django_db
class TestInterfaceSyncRefreshButtonDeduped:
    """Verify that one Refresh Interfaces button uses the object-specific URL and shared pagination and server values."""

    def _render(self, obj):
        from unittest.mock import MagicMock

        from django.template.loader import render_to_string

        # A real request (with a user) so {% csrf_token %} and the context processors the
        # template relies on resolve instead of rendering empty with a warning.
        request = RequestFactory().get("/")
        request.user = MagicMock(is_authenticated=True)
        return render_to_string(
            "netbox_librenms_plugin/_interface_sync.html",
            {
                "object": obj,
                "has_librenms_id": True,
                "librenms_server_info": {"server_key": "default"},
            },
            request=request,
        )

    def _refresh_button_tag(self, html, path):
        hx_post = html.index(f'hx-post="{path}"')
        return html[html.rfind("<button", 0, hx_post) : html.index(">", hx_post) + 1]

    def test_device_refresh_button_single_with_device_url(self):
        from django.urls import reverse

        device = make_device("refresh-btn-dev")
        html = self._render(device)
        device_path = reverse("plugins:netbox_librenms_plugin:device_interface_sync", kwargs={"pk": device.pk})
        vm_path = reverse("plugins:netbox_librenms_plugin:vm_interface_sync", kwargs={"pk": device.pk})

        # Exactly one refresh action button (hx-post), pointing at the device URL, with the
        # shared hx-vals — and never the VM branch's URL.
        assert html.count("hx-post=") == 1
        assert f'hx-post="{device_path}"' in html
        button = self._refresh_button_tag(html, device_path)
        assert "hx-vals=" in button and "interfaces_per_page" in button and "server_key" in button
        assert vm_path not in html

    def test_vm_refresh_button_single_with_vm_url(self):
        from django.urls import reverse

        from netbox_librenms_plugin.tests.conftest import make_vm

        vm = make_vm("refresh-btn-vm")
        html = self._render(vm)
        vm_path = reverse("plugins:netbox_librenms_plugin:vm_interface_sync", kwargs={"pk": vm.pk})
        device_path = reverse("plugins:netbox_librenms_plugin:device_interface_sync", kwargs={"pk": vm.pk})

        assert html.count("hx-post=") == 1
        assert f'hx-post="{vm_path}"' in html
        button = self._refresh_button_tag(html, vm_path)
        assert "hx-vals=" in button and "interfaces_per_page" in button and "server_key" in button
        assert device_path not in html
