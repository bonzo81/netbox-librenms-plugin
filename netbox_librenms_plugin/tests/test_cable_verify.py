"""
Regression tests for SingleCableVerifyView.post().

Covers:
- Stale derived fields are stripped before re-enrichment (prevents
  DoesNotExist when remote objects are deleted after caching).
- LibreNMS-sourced labels are HTML-escaped to prevent XSS.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_view(server_key="default"):
    """Create a SingleCableVerifyView instance without database access."""
    from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

    view = object.__new__(SingleCableVerifyView)
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = server_key
    view.request = MagicMock()
    return view


def _make_request(body_dict):
    """Create a mock POST request with JSON body."""
    request = MagicMock()
    request.method = "POST"
    request.body = json.dumps(body_dict).encode()
    request.META = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
    return request


class TestStaleFieldStripping:
    """Cached link data with stale derived fields must be stripped before use."""

    def test_stale_remote_fields_stripped_before_enrichment(self):
        """Stale netbox_remote_device_id / remote_device_url must not reach check_cable_status()."""
        view = _make_view()

        # Cached link with stale derived fields (from a previous enrichment)
        cached_link = {
            "local_port": "eth0",
            "local_port_id": 100,
            "remote_port": "eth1",
            "remote_device": "switch-remote",
            "remote_port_id": 200,
            "remote_device_id": 42,
            # Stale derived fields — remote device was deleted after caching
            "netbox_remote_device_id": 999,
            "remote_device_url": "/dcim/devices/999/",
            "netbox_remote_interface_id": 888,
            "remote_port_url": "/dcim/interfaces/888/",
            "cable_status": "No Cable",
            "can_create_cable": True,
        }

        cached_data = {"links": [cached_link]}

        device = MagicMock()
        device.pk = 1
        device.id = 1
        device.virtual_chassis = None
        interface_mock = MagicMock()
        interface_mock.pk = 10

        # Track what link_data check_cable_status receives
        received_link_data = {}

        def fake_check_cable_status(link):
            received_link_data.update(link)
            link["cable_status"] = "No Cable"
            link["can_create_cable"] = True
            return link

        def fake_process_remote_device(link, hostname, device_id, server_key=None):
            assert link is not None
            assert hostname is not None
            assert device_id is not None
            assert server_key == "default"
            # Simulate successful remote enrichment with fresh IDs
            link["remote_device_url"] = "/dcim/devices/777/"
            link["netbox_remote_device_id"] = 777
            link["remote_port_url"] = "/dcim/interfaces/666/"
            link["netbox_remote_interface_id"] = 666
            link["remote_port_name"] = "eth1"
            return link

        request = _make_request({"device_id": 1, "local_port_id": 100})

        with (
            patch.object(view, "restrict_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test_key"),
            patch.object(view, "check_cable_status", side_effect=fake_check_cable_status),
            patch.object(view, "process_remote_device", side_effect=fake_process_remote_device),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=device),
            patch("netbox_librenms_plugin.views.base.cables_view.get_virtual_chassis_member", return_value=device),
            patch("netbox_librenms_plugin.views.base.cables_view._librenms_id_q", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.base.cables_view.get_token", return_value="csrf123"),
            patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/fake/"),
        ):
            mock_cache.get.return_value = cached_data
            # Make the interface filter return our mock
            device.interfaces.filter.return_value.first.return_value = interface_mock

            view.post(request)

        # check_cable_status should have received fresh IDs from process_remote_device,
        # NOT the stale 999/888 from cache
        assert received_link_data.get("netbox_remote_device_id") == 777
        assert received_link_data.get("netbox_remote_interface_id") == 666

    def test_post_strips_derived_fields_from_cached_link(self):
        """post() must strip derived fields (URLs, IDs) before re-enrichment.

        Both _prepare_context and post() define a _raw_keys set that controls
        which cached fields survive into re-enrichment. This test verifies the
        behavior: derived fields in the cached link must not leak through.
        """
        view = _make_view()

        # Cached link with both raw and derived (stale) fields
        cached_link = {
            "local_port": "eth0",
            "local_port_id": 100,
            "remote_port": "eth1",
            "remote_device": "switch-a",
            "remote_port_id": 200,
            "remote_device_id": 42,
            # Derived fields that must be stripped:
            "netbox_local_interface_id": 999,
            "netbox_remote_interface_id": 888,
            "netbox_remote_device_id": 777,
            "local_port_url": "/stale/",
            "remote_port_url": "/stale/",
            "remote_device_url": "/stale/",
            "cable_status": "stale",
            "can_create_cable": True,
        }

        # Mock process_remote_device to avoid DB access during re-enrichment;
        # it should receive the link WITHOUT derived fields.
        received_link = {}

        def fake_process_remote(link, hostname, device_id, server_key=None):
            received_link.update(link)
            return link

        view.process_remote_device = fake_process_remote

        with (
            patch.object(view, "restrict_object_or_404") as mock_get,
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.get_token", return_value="tok"),
        ):
            device = MagicMock()
            device.pk = 1
            device.virtual_chassis = None
            device.interfaces.filter.return_value.first.return_value = None
            mock_get.return_value = device
            mock_cache.get.return_value = {"links": [cached_link]}

            request = MagicMock()
            request.body = json.dumps(
                {
                    "device_id": 1,
                    "local_port_id": 100,
                    "server_key": "default",
                }
            )
            view.post(request)

            # The link passed to process_remote_device must have derived fields stripped
            assert "netbox_local_interface_id" not in received_link
            assert "netbox_remote_interface_id" not in received_link
            assert "netbox_remote_device_id" not in received_link
            assert "local_port_url" not in received_link
            assert "cable_status" not in received_link


class TestXSSEscaping:
    """LibreNMS-sourced labels must be HTML-escaped in cable verify output."""

    def test_xss_in_local_port_name_escaped(self):
        """A malicious local_port name must be escaped in the HTML output."""
        view = _make_view()

        xss_port_name = '<script>alert("xss")</script>'
        cached_link = {
            "local_port": xss_port_name,
            "local_port_id": 100,
            "remote_port": "eth1",
            "remote_device": "safe-switch",
            "remote_port_id": 200,
            "remote_device_id": 42,
        }

        cached_data = {"links": [cached_link]}

        device = MagicMock()
        device.pk = 1
        device.id = 1
        device.virtual_chassis = None
        interface_mock = MagicMock()
        interface_mock.pk = 10

        def fake_process_remote_device(link, hostname, device_id, server_key=None):
            link["remote_device_url"] = "/dcim/devices/2/"
            link["netbox_remote_device_id"] = 2
            link["remote_port_url"] = "/dcim/interfaces/20/"
            link["netbox_remote_interface_id"] = 20
            link["remote_port_name"] = "eth1"
            return link

        def fake_check_cable_status(link):
            link["cable_status"] = "No Cable"
            link["can_create_cable"] = False
            return link

        request = _make_request({"device_id": 1, "local_port_id": 100})

        with (
            patch.object(view, "restrict_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test_key"),
            patch.object(view, "check_cable_status", side_effect=fake_check_cable_status),
            patch.object(view, "process_remote_device", side_effect=fake_process_remote_device),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=device),
            patch("netbox_librenms_plugin.views.base.cables_view._librenms_id_q", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.base.cables_view.get_token", return_value="csrf123"),
            patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/fake/"),
        ):
            mock_cache.get.return_value = cached_data
            device.interfaces.filter.return_value.first.return_value = interface_mock

            response = view.post(request)

        content = json.loads(response.content)
        row = content.get("formatted_row", {})
        local_port_html = row.get("local_port", "")

        # The raw script tag must NOT appear unescaped
        assert "<script>" not in local_port_html
        # The escaped version should be present
        assert "&lt;script&gt;" in local_port_html

    def test_xss_in_remote_device_name_escaped(self):
        """A malicious remote_device name must be escaped in the HTML output."""
        view = _make_view()

        xss_device = "<img src=x onerror=alert(1)>"
        cached_link = {
            "local_port": "eth0",
            "local_port_id": 100,
            "remote_port": "eth1",
            "remote_device": xss_device,
            "remote_port_id": 200,
            "remote_device_id": 42,
        }

        cached_data = {"links": [cached_link]}

        device = MagicMock()
        device.pk = 1
        device.id = 1
        device.virtual_chassis = None
        interface_mock = MagicMock()
        interface_mock.pk = 10

        def fake_process_remote_device(link, hostname, device_id, server_key=None):
            # Remote device found — but name is the XSS payload
            link["remote_device_url"] = "/dcim/devices/2/"
            link["netbox_remote_device_id"] = 2
            link["remote_port_url"] = "/dcim/interfaces/20/"
            link["netbox_remote_interface_id"] = 20
            link["remote_port_name"] = "eth1"
            return link

        def fake_check_cable_status(link):
            link["cable_status"] = "No Cable"
            link["can_create_cable"] = False
            return link

        request = _make_request({"device_id": 1, "local_port_id": 100})

        with (
            patch.object(view, "restrict_object_or_404", return_value=device),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test_key"),
            patch.object(view, "check_cable_status", side_effect=fake_check_cable_status),
            patch.object(view, "process_remote_device", side_effect=fake_process_remote_device),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=device),
            patch("netbox_librenms_plugin.views.base.cables_view._librenms_id_q", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.base.cables_view.get_token", return_value="csrf123"),
            patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/fake/"),
        ):
            mock_cache.get.return_value = cached_data
            device.interfaces.filter.return_value.first.return_value = interface_mock

            response = view.post(request)

        content = json.loads(response.content)
        row = content.get("formatted_row", {})
        remote_device_html = row.get("remote_device", "")

        # Raw HTML tag must not appear — angle brackets must be escaped
        assert "<img " not in remote_device_html
        # Escaped version should be present (browser renders as text, not tag)
        assert "&lt;img" in remote_device_html


class TestServerKeyGuard:
    """A forged non-string server_key must not crash the membership check (get_available_servers() is a dict)."""

    def _view(self, available):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        # A REAL dict so the `in` membership test really hashes the key (the crash point).
        view._librenms_api.get_available_servers.return_value = available
        view.require_object_permissions_json = MagicMock(return_value=None)
        return view

    @pytest.mark.parametrize("forged", [["prod"], {"prod": 1}])
    def test_unhashable_server_key_falls_back_without_crashing(self, forged):
        """A JSON array/object server_key must fall back to the active server, not raise TypeError."""
        # No device_id → post resolves server_key (the guarded line) then returns the empty row.
        view = self._view({"default": "Default Server"})
        request = _make_request({"server_key": forged})

        response = view.post(request)  # must not raise unhashable-type TypeError

        assert response.status_code == 200

    def test_valid_string_server_key_is_still_honoured(self):
        """A configured string key still passes the isinstance guard (regression for the str check)."""
        view = self._view({"prod": "Prod Server"})
        request = _make_request({"server_key": "prod", "device_id": ""})

        response = view.post(request)

        assert response.status_code == 200


@pytest.mark.django_db
class TestCablesGetRenderDegradesOnBrokenDefaultServer:
    """A cached cables GET must degrade cleanly when the default LibreNMS server is misconfigured, not 500 on the lazy LibreNMSAPI() construction."""

    def test_cached_render_does_not_500_when_default_server_build_fails(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        tag = "cables-broken-srv"
        mfr, _ = Manufacturer.objects.get_or_create(name=f"Mfr-{tag}", slug=f"mfr-{tag}")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"DT-{tag}", slug=f"dt-{tag}")
        role, _ = DeviceRole.objects.get_or_create(name=f"Role-{tag}", slug=f"role-{tag}")
        site, _ = Site.objects.get_or_create(name=f"Site-{tag}", slug=f"site-{tag}")
        device = Device.objects.create(name=f"host-{tag}", device_type=dt, role=role, site=site, status="active")
        view = DeviceCableTableView()
        request = RequestFactory().get("/")
        User = get_user_model()
        request.user = User.objects.filter(is_superuser=True, is_active=True).first() or User.objects.create(
            username="cables-su", is_superuser=True, is_active=True
        )
        view.request = request

        # Seed the links cache under the DEGRADED (None) server_key — what the broken-server render reads.
        key = view.get_cache_key(device, "links", None)
        cache.set(
            key,
            {"links": [{"local_port": "Gi0/1", "local_port_id": "1", "remote_device": None}]},
            timeout=300,
        )

        def broken_cfg(_plugin, cfg_key, *args, **kwargs):
            # A non-empty 'servers' with no 'default' and no valid dict entry makes LibreNMSAPI()
            # raise ValueError in its constructor — the exact broken-default-server condition.
            return {"alpha": "not-a-dict"} if cfg_key == "servers" else None

        try:
            with patch("netbox_librenms_plugin.librenms_api.get_plugin_config", side_effect=broken_cfg):
                context = view.get_context_data(request, device)
            # Degrades: the cached links still render (server_key falls back to None) instead of a 500.
            assert context is not None
            assert context["table"] is not None
            assert context["server_key"] is None
        finally:
            cache.delete(key)


@pytest.mark.django_db
class TestSingleCableVerifyMisconfiguredDefault:
    """SingleCableVerifyView resolves configured servers via the classmethod, not the raising instance property."""

    def test_configured_key_does_not_500_when_default_broken(self):
        """A configured 'prod' key resolves without constructing the broken default client (issue #108/#109)."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.test import RequestFactory, override_settings

        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        mfr, _ = Manufacturer.objects.get_or_create(name="Mfr-scv", slug="mfr-scv")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-scv", slug="dt-scv")
        role, _ = DeviceRole.objects.get_or_create(name="Role-scv", slug="role-scv")
        site, _ = Site.objects.get_or_create(name="Site-scv", slug="site-scv")
        device = Device.objects.create(name="host-scv", device_type=dt, role=role, site=site, status="active")

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = None  # live property: accessing it would build LibreNMSAPI() (default)
        view.require_object_permissions_json = MagicMock(return_value=None)

        # DEFAULT present-but-broken (no url/token) → LibreNMSAPI() raises; get_available_servers()={'prod'}.
        cfg = {
            "netbox_librenms_plugin": {
                "servers": {
                    "prod": {"librenms_url": "http://prod.example", "api_token": "tok"},
                    "default": {},
                }
            }
        }
        request = RequestFactory().post(
            "/verify/",
            data=json.dumps({"device_id": device.pk, "local_port_id": "1", "server_key": "prod"}),
            content_type="application/json",
        )
        # RequestFactory skips AuthenticationMiddleware; the object-scoped device lookup reads
        # request.user, so attach a real superuser (this test is about server-key degradation, not
        # object scope).
        from django.contrib.auth import get_user_model

        request.user = get_user_model().objects.create_user(username="scv-user", password="x", is_superuser=True)
        view.request = request  # dispatch() normally wires this; the object-scoped lookup reads self.request.user
        with override_settings(PLUGINS_CONFIG=cfg):
            response = view.post(request)

        assert response.status_code == 200
        assert json.loads(response.content)["status"] == "success"
