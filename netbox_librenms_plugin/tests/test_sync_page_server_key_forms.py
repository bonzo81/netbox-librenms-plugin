"""The device sync page's POST forms must carry the active server_key.

Every device-info action (name/type/serial/platform/location sync, legacy-ID
conversion) and every tab refresh form rebinds server-side from the POSTed
``server_key``. A form that omits it silently falls back to the GLOBAL selected
server — a wrong-server write when the user is acting on a ``?server_key`` tab.

These tests render the REAL page through the real view (real device, real URL
routing, real template) with only the LibreNMS HTTP boundary patched, then
assert each form contains the hidden ``server_key`` input scoped to the tab.
"""

from unittest.mock import patch

import pytest
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device

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
    """A misconfigured default server must degrade the sync page, not 500 it.

    resolve_get_render_server_key deliberately swallows the construction error
    (build_librenms_api(None) → None); get() must not re-enter the lazy
    librenms_api property, which would reconstruct LibreNMSAPI() and re-raise it.
    """

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
