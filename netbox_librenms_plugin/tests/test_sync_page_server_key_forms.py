"""The device sync page's POST forms must carry the active server_key.

Every device-info action (name/type/serial/platform/location sync, legacy-ID
conversion) and every tab refresh form rebinds server-side from the POSTed
``server_key``. A form that omits it silently falls back to the GLOBAL selected
server — a wrong-server write when the user is acting on a ``?server_key`` tab.

These tests render the real page through the real view, template, database,
cache, and loopback LibreNMS HTTP boundary. Each form must contain the hidden
``server_key`` input scoped to the tab.
"""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.test import RequestFactory

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

pytestmark = pytest.mark.django_db

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


def _configure_servers(settings, servers):
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        key: {
            "display_name": f"{key.title()} LibreNMS",
            "librenms_url": server.url,
            "api_token": f"{key}-test-token",
            "verify_ssl": False,
        }
        for key, server in vars(servers).items()
    }
    plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
    settings.PLUGINS_CONFIG = plugin_config


@pytest.fixture
def servers(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with ExitStack() as stack:
        running = SimpleNamespace(
            default=stack.enter_context(librenms_mock_server()),
            secondary=stack.enter_context(librenms_mock_server()),
        )
        _configure_servers(settings, running)
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "default"})
        cache.delete_many(
            [
                "librenms_device_info_default_42",
                "librenms_device_info_secondary_42",
                "librenms_poller_group_choices_default",
                "librenms_poller_group_choices_secondary",
            ]
        )
        yield running


def _register_device_info(server, *, found=True):
    status = 200 if found else 404
    payload = {"status": "ok", "devices": [dict(DEVICE_INFO)]} if found else {"status": "error"}
    server.register("/api/v0/devices/42", payload, status=status)


def _render_sync_page(device, servers, query=""):
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

    _register_device_info(servers.default)
    _register_device_info(servers.secondary)
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

    def _render_unknown_device(self, device, servers, query, requested_servers):
        """Render the page for a device LibreNMS does not know, so the Add-device forms appear."""
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
        cache.delete("librenms_device_info_secondary_42")
        for key, server in vars(servers).items():
            _register_device_info(server, found=False)

            def record_poller_request(*, _key=key, **_request):
                requested_servers.append(_key)
                return 200, {"status": "ok", "get_poller_group": []}

            server.register("/api/v0/poller_group", record_poller_request)
        return view.get(request, pk=device.pk)

    def test_poller_group_choices_come_from_the_active_server(self, servers):
        """A secondary-server page must not offer the default server's poller groups."""
        # The choices are cached per server key; clear so this render does the lookup.
        cache.delete("librenms_poller_group_choices_secondary")
        cache.delete("librenms_poller_group_choices_default")

        device = make_device("poller-scope", librenms_cf={"secondary": 42})
        requested_servers = []
        self._render_unknown_device(device, servers, "?server_key=secondary", requested_servers)

        assert requested_servers == ["secondary"]

    def test_a_stale_server_key_asks_no_server_for_poller_groups(self, servers):
        """The fail-closed page must not fall back to the installation default for its choices."""
        cache.delete("librenms_poller_group_choices_default")
        cache.delete("librenms_poller_group_choices_secondary")
        cache.delete("librenms_poller_group_choices_ghost")
        device = make_device("poller-stale", librenms_cf={"secondary": 42})
        requested_servers = []
        response = self._render_unknown_device(device, servers, "?server_key=ghost", requested_servers)

        # The page must still render: a 500 would otherwise satisfy the negative below on its own.
        assert response.status_code == 200
        assert requested_servers == []


class TestSyncPageFormsCarryServerKey:
    """Rendered with ?server_key=secondary, every POST form must scope to it."""

    def _device(self):
        # The installation default differs from the query-selected server. An explicit secondary
        # mapping makes every ordinary action prove that the query key wins.
        # Serial/type/platform differ from LibreNMS values → the sync forms render.
        from dcim.models import DeviceType, Manufacturer, Platform

        device = make_device("sync-page-forms", serial="NB-SER-1", librenms_cf={"secondary": {"id": 42}})
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "default"})
        # A DeviceType matching the LibreNMS hardware string (≠ the device's own type)
        # → the Device Type sync form renders.
        mfr = Manufacturer.objects.get(slug="test-mfr")
        DeviceType.objects.get_or_create(manufacturer=mfr, model="TestHW-9000", defaults={"slug": "testhw-9000"})
        # A Platform matching the LibreNMS OS (device has no platform) → the Platform sync form renders.
        Platform.objects.get_or_create(name="testos", defaults={"slug": "testos"})
        return device

    @pytest.fixture
    def html(self, servers):
        return _render_sync_page(self._device(), servers, "?server_key=secondary")

    @pytest.mark.parametrize(
        "action_name",
        [
            "update_device_name",
            "update_device_type",
            "update_device_serial",
            "update_device_platform",
            "update_device_location",
        ],
    )
    def test_device_info_form_posts_server_key(self, html, action_name):
        """Each device-info action form carries the tab's server_key hidden input."""
        form = _enclosing_form(html, reverse_fragment(action_name))
        assert 'name="server_key"' in form and 'value="secondary"' in form, (
            f"{action_name} form must post server_key=secondary; got: {form[:400]}"
        )

    def test_legacy_conversion_form_posts_the_query_selected_server_key(self, servers):
        """The legacy-only action keeps its active server key in the submitted form."""
        from django.urls import reverse

        device = make_device("sync-page-legacy-form", serial="NB-SER-2", librenms_cf=42)
        # The installation default must MATCH the query key here: server_selection.py:248 accepts a
        # requested key for a legacy bare-integer mapping only when it equals the installation
        # default, so separating them makes this scenario unreachable rather than stronger.
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "secondary"})

        html = _render_sync_page(device, servers, "?server_key=secondary")
        form = _enclosing_form(
            html,
            reverse(
                "plugins:netbox_librenms_plugin:convert_legacy_librenms_id",
                kwargs={"pk": device.pk},
            ),
        )

        assert 'name="server_key"' in form and 'value="secondary"' in form

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


def test_stale_installation_server_does_not_select_its_cached_locations(servers):
    """A removed selected server must not win the first location-cache lookup."""
    from netbox_librenms_plugin.forms import LibreNMSImportFilterForm
    from netbox_librenms_plugin.import_utils.cache import get_location_choices_cache_key

    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "removed-server"})

    stale_choices = [("", "All Locations"), ("stale", "Removed server location")]
    default_choices = [("", "All Locations"), ("current", "Default server location")]
    stale_key = get_location_choices_cache_key("removed-server")
    default_key = get_location_choices_cache_key("default")
    cache.set(stale_key, stale_choices)
    cache.set(default_key, default_choices)
    location_requests = []

    def record_location_request(**request):
        location_requests.append(request)
        return 200, {"status": "ok", "locations": [{"id": 1, "location": "Unexpected live location"}]}

    servers.default.register("/api/v0/resources/locations", record_location_request)
    servers.secondary.register("/api/v0/resources/locations", record_location_request)

    try:
        form = LibreNMSImportFilterForm()

        assert list(form.fields["librenms_location"].choices) == default_choices
        assert location_requests == []
    finally:
        cache.delete_many([stale_key, default_key])


class TestSyncPageMisconfiguredDefaultDegrades:
    """Verify that a broken default degrades the sync page without lazy API client reconstruction or a 500."""

    @staticmethod
    def _configure_broken_default(settings, *, include_secondary=False):
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        servers = {
            "default": {
                "display_name": "Broken default",
                "librenms_url": "",
                "api_token": "",
            }
        }
        if include_secondary:
            servers["secondary"] = {
                "display_name": "Secondary LibreNMS",
                "librenms_url": "http://127.0.0.1:9",
                "api_token": "misconfiguration-test-token",
                "verify_ssl": False,
            }
        plugin_config["netbox_librenms_plugin"]["servers"] = servers
        plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
        plugin_config["netbox_librenms_plugin"].pop("api_token", None)
        settings.PLUGINS_CONFIG = plugin_config
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": "default"})

    def test_get_with_broken_default_renders_degraded_page(self, settings):
        from django.contrib.messages.storage.fallback import FallbackStorage

        from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

        self._configure_broken_default(settings)
        device = make_device("sync-page-degraded")

        request = RequestFactory().get("/x/")  # plain GET, no ?server_key
        request.user = make_superuser("sync-degraded-su")
        request.htmx = False
        request.session = {}
        request._messages = FallbackStorage(request)

        view = DeviceLibreNMSSyncView()
        view.setup(request, pk=device.pk)

        response = view.get(request, pk=device.pk)

        assert response.status_code == 200
        assert "not configured correctly" in response.content.decode()

    def test_get_with_stale_server_key_and_broken_default_renders_degraded_page(self, settings):
        """A stale server key must fail closed before API client construction."""
        from django.contrib.messages.storage.fallback import FallbackStorage

        from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

        self._configure_broken_default(settings, include_secondary=True)
        device = make_device("sync-page-degraded-stale")

        request = RequestFactory().get("/x/", {"server_key": "gone-server"})
        request.user = make_superuser("sync-degraded-stale-su")
        request.htmx = False
        request.session = {}
        request._messages = FallbackStorage(request)

        view = DeviceLibreNMSSyncView()
        view.setup(request, pk=device.pk)

        response = view.get(request, pk=device.pk)

        assert response.status_code == 200
        assert "is not an available mapping for this object" in response.content.decode()
        assert view._server_key_unresolved is True
        assert view._scoped_render_server_key == "gone-server"
        assert view.librenms_id is None  # failed closed: no default-server mapping attributed


class TestUpdateDeviceLocationRebindsServer:
    """UpdateDeviceLocationView must write to the POSTed server, not the global default."""

    def test_location_write_goes_to_posted_server(self, client, servers):
        """The real action writes only to the server selected by the POST."""
        from dcim.models import Device
        from django.contrib.messages import get_messages
        from django.urls import reverse

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        device = make_device("loc-rebind", librenms_cf={"default": 41, "secondary": 42})
        writes = []

        def record_write(**request):
            writes.append(request)
            return 200, {"status": "ok"}

        servers.default.register("/api/v0/devices/41", record_write, method="PATCH")
        servers.secondary.register("/api/v0/devices/42", record_write, method="PATCH")
        client.force_login(make_user_with_perms("loc-rebind-writer", [("view", Device)]))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:update_device_location", args=[device.pk]),
            {"server_key": "secondary"},
        )

        assert response.status_code == 302
        assert response.url.endswith("?server_key=secondary")
        assert [(write["path"], write["body"]) for write in writes] == [
            (
                "/api/v0/devices/42",
                {"field": ["location", "override_sysLocation"], "data": [device.site.name, "1"]},
            )
        ]
        assert any(message.level_tag == "success" for message in get_messages(response.wsgi_request))

    def test_stale_server_key_fails_closed(self, client, servers):
        """A stale POSTed key reports an error without writing to another server."""
        from dcim.models import Device
        from django.contrib.messages import get_messages
        from django.urls import reverse

        from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

        device = make_device("loc-rebind-stale", librenms_cf={"default": 41})
        writes = []

        def record_write(**request):
            writes.append(request)
            return 200, {"status": "ok"}

        servers.default.register("/api/v0/devices/41", record_write, method="PATCH")
        client.force_login(make_user_with_perms("loc-rebind-stale-writer", [("view", Device)]))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:update_device_location", args=[device.pk]),
            {"server_key": "ghost"},
        )

        assert response.status_code == 302
        assert "server_key=" not in response.url
        assert writes == []
        assert [str(message) for message in get_messages(response.wsgi_request)] == [
            "Selected LibreNMS server is no longer configured."
        ]


@pytest.mark.django_db
class TestInterfaceSyncRefreshButtonDeduped:
    """Verify that one Refresh Interfaces button uses the object-specific URL and shared pagination and server values."""

    def _render(self, obj):
        from django.template.loader import render_to_string

        # A real request (with a user) so {% csrf_token %} and the context processors the
        # template relies on resolve instead of rendering empty with a warning.
        request = RequestFactory().get("/")
        request.user = make_superuser("interface-refresh-renderer")
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
