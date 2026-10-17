"""Platform version tracking through the real sync page and its writer views."""

from copy import deepcopy

import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.platform_versions import version_platform_slug
from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import (
    assert_update_logged,
    make_user_with_perms,
    message_texts,
)

pytestmark = pytest.mark.django_db

SERVER_KEY = "default"
VERSION = "21.2R3-S4.8"


@pytest.fixture
def librenms_server(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_config["netbox_librenms_plugin"]["servers"] = {
            SERVER_KEY: {"librenms_url": server.url, "api_token": "token", "cache_timeout": 300, "verify_ssl": False}
        }
        plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
        settings.PLUGINS_CONFIG = plugin_config
        LibreNMSSettings.objects.update_or_create(pk=1, defaults={"selected_server": SERVER_KEY})
        yield server


def _tracking(enabled):
    LibreNMSSettings.objects.update_or_create(pk=1, defaults={"platform_version_tracking": enabled})


def _platform(name, *, parent=None):
    from dcim.models import Platform

    return Platform.objects.create(name=name, slug=version_platform_slug(name), parent=parent)


def _linked(name, server, device_id, *, platform=None, os="junos", version=VERSION):
    device = make_device(name, librenms_cf={SERVER_KEY: device_id})
    if platform is not None:
        device.platform = platform
        device.save()
    server.device_info_response(device_id=device_id, hostname=name, os=os, version=version)
    return device


def _render_sync_page(device, user=None):
    """GET the device sync page through the real view and return its HTML."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

    request = RequestFactory().get("/")
    request.user = user or make_superuser("tracking-page-su")
    request.htmx = False
    SessionMiddleware(lambda _request: None).process_request(request)
    view = DeviceLibreNMSSyncView()
    view.setup(request, pk=device.pk)
    response = view.get(request, pk=device.pk)
    assert response.status_code == 200
    return response.content.decode()


def _post(client, name, device, data=None):
    url = reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"pk": device.pk})
    return client.post(url, {"server_key": SERVER_KEY, **(data or {})})


UPDATE_PLATFORM_ACTION = "update-platform/"


class TestSyncPagePlatformRow:
    def test_a_device_on_a_child_platform_is_current_when_tracking_is_off(self, librenms_server):
        """Turning tracking off never offers to move a device from its child platform back to the parent."""
        _tracking(False)
        os_platform = _platform("junos")
        device = _linked("page-child", librenms_server, 7301, platform=_platform("junos 20.1", parent=os_platform))

        html = _render_sync_page(device)

        assert UPDATE_PLATFORM_ACTION not in html
        assert 'title="Synced with LibreNMS"' in html

    def test_tracking_offers_to_create_the_missing_version_platform(self, librenms_server):
        _tracking(True)
        os_platform = _platform("junos")
        device = _linked("page-create", librenms_server, 7302, platform=os_platform)

        html = _render_sync_page(device)

        assert UPDATE_PLATFORM_ACTION in html
        assert f"Create version platform junos {VERSION} and set it" in html

    def test_a_blocked_row_shows_its_reason(self, librenms_server):
        _tracking(True)
        _platform("junos")
        _platform(f"junos {VERSION}")
        device = _linked("page-blocked", librenms_server, 7303)

        html = _render_sync_page(device)

        assert UPDATE_PLATFORM_ACTION not in html
        assert 'data-platform-blocked="name_taken"' in html
        assert f"already uses the name &#x27;junos {VERSION}&#x27;" in html

    def test_a_viewer_who_cannot_add_platforms_sees_the_block(self, librenms_server):
        from dcim.models import Device

        _tracking(True)
        _platform("junos")
        device = _linked("page-denied", librenms_server, 7304)
        viewer = make_user_with_perms("page-denied-viewer", [("view", Device), ("change", Device)])

        html = _render_sync_page(device, viewer)

        assert 'data-platform-blocked="add_platform_denied"' in html

    def test_the_create_modal_posts_the_version_when_tracking_is_on(self, librenms_server):
        _tracking(True)
        device = _linked("page-modal", librenms_server, 7305)

        html = _render_sync_page(device)

        assert 'id="create-platform-modal"' in html
        assert f'name="librenms_version" value="{VERSION}"' in html


class TestUpdateDevicePlatformView:
    def test_tracking_creates_and_assigns_the_version_platform(self, client, librenms_server):
        from dcim.models import Platform

        _tracking(True)
        os_platform = _platform("junos")
        device = _linked("update-create", librenms_server, 7311, platform=os_platform)
        client.force_login(make_superuser("update-create-su"))

        response = _post(client, "update_device_platform", device)

        version_platform = Platform.objects.get(parent=os_platform)
        device.refresh_from_db()
        assert device.platform == version_platform
        assert version_platform.name == f"junos {VERSION}"
        assert_update_logged(device, "platform", os_platform.pk, version_platform.pk)
        texts = message_texts(response.wsgi_request, "success")
        assert f"Created platform 'junos {VERSION}'" in texts
        assert f"Device platform updated from 'junos' to 'junos {VERSION}'" in texts

    def test_an_actor_who_cannot_add_platforms_is_blocked_with_the_reason(self, client, librenms_server):
        from dcim.models import Device, Platform

        _tracking(True)
        os_platform = _platform("junos")
        device = _linked("update-denied", librenms_server, 7312, platform=os_platform)
        client.force_login(make_user_with_perms("update-denied", [("view", Device), ("change", Device)]))

        response = _post(client, "update_device_platform", device)

        device.refresh_from_db()
        assert device.platform == os_platform
        assert not Platform.objects.filter(parent=os_platform).exists()
        assert message_texts(response.wsgi_request, "error") == [
            f"You do not have permission to add platform 'junos {VERSION}'."
        ]

    def test_an_ambiguous_version_platform_changes_nothing(self, client, librenms_server):
        _tracking(True)
        os_platform = _platform("junos")
        _platform(VERSION, parent=os_platform)
        _platform(f"Juniper {VERSION}", parent=os_platform)
        device = _linked("update-ambiguous", librenms_server, 7313, platform=os_platform)
        client.force_login(make_superuser("update-ambiguous-su"))

        response = _post(client, "update_device_platform", device)

        device.refresh_from_db()
        assert device.platform == os_platform
        assert any("Multiple version platforms" in text for text in message_texts(response.wsgi_request, "error"))


class TestCreateAndAssignPlatformView:
    def test_tracking_creates_the_os_and_the_version_platform_and_maps_the_os_platform(self, client, librenms_server):
        from dcim.models import Platform

        from netbox_librenms_plugin.models import PlatformMapping

        _tracking(True)
        device = make_device("create-tracked")
        client.force_login(make_superuser("create-tracked-su"))

        _post(
            client,
            "create_and_assign_platform",
            device,
            {
                "platform_name": "Juniper Junos",
                "librenms_os": "junos",
                "librenms_version": VERSION,
                "create_mapping": "on",
            },
        )

        os_platform = Platform.objects.get(name="Juniper Junos")
        device.refresh_from_db()
        assert device.platform.name == f"Juniper Junos {VERSION}"
        assert device.platform.parent == os_platform
        assert PlatformMapping.objects.get(librenms_os="junos").netbox_platform == os_platform
