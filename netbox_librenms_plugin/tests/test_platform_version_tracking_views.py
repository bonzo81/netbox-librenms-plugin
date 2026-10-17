"""Platform version tracking through the real sync page, its writer views, and the import paths."""

from copy import deepcopy

import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.urls import reverse

from netbox_librenms_plugin.models import LibreNMSSettings
from netbox_librenms_plugin.platform_versions import HIDDEN_PLATFORM, version_platform_slug
from netbox_librenms_plugin.tests.conftest import make_device, make_superuser, make_vm
from netbox_librenms_plugin.tests.mapping_fixtures import seed_stored_mapping
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


def _linked_vm(name, server, device_id, *, platform=None, os="junos", version=VERSION):
    vm = make_vm(name)
    seed_stored_mapping(vm, {SERVER_KEY: device_id}, save=True)
    if platform is not None:
        vm.platform = platform
        vm.save()
    server.device_info_response(device_id=device_id, hostname=name, os=os, version=version)
    return vm


def _render_sync_page(obj, user=None):
    """GET the device or VM sync page through the real view and return its HTML."""
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView
    from netbox_librenms_plugin.views.object_sync.vms import VMLibreNMSSyncView

    request = RequestFactory().get("/")
    request.user = user or make_superuser("tracking-page-su")
    request.htmx = False
    SessionMiddleware(lambda _request: None).process_request(request)
    view = VMLibreNMSSyncView() if isinstance(obj, VirtualMachine) else DeviceLibreNMSSyncView()
    view.setup(request, pk=obj.pk)
    response = view.get(request, pk=obj.pk)
    assert response.status_code == 200
    return response.content.decode()


def _platform_blind_device_user(username):
    """A user who may view and change devices, but may not view platforms."""
    from dcim.models import Device

    return make_user_with_perms(username, [("view", Device), ("change", Device)])


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
        assert f"Create version platform &#x27;junos {VERSION}&#x27; and set it" in html

    def test_a_blocked_row_shows_its_reason(self, librenms_server):
        _tracking(True)
        _platform("junos")
        _platform(f"junos {VERSION}")
        device = _linked("page-blocked", librenms_server, 7303)

        html = _render_sync_page(device)

        assert UPDATE_PLATFORM_ACTION not in html
        assert 'data-platform-blocked="name_taken"' in html
        assert f"is taken by &#x27;junos {VERSION}&#x27;" in html

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
            f"You do not have permission to add platforms, so a new version platform for version '{VERSION}' "
            "cannot be created."
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
        device = _linked("create-tracked", librenms_server, 7321)
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


class TestImportDetailsPlatformCell:
    def _render(self, *, can_add_platform):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string

        from netbox_librenms_plugin.platform_versions import PlatformFacts, assess_platform
        from netbox_librenms_plugin.views.imports.actions import _platform_display

        _tracking(True)
        os_platform = _platform("junos")
        assessment = assess_platform(None, PlatformFacts.from_librenms("junos", VERSION), tracking=True)
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        return render_to_string(
            "netbox_librenms_plugin/htmx/device_validation_details.html",
            {
                "validation": {
                    "can_import": True,
                    "issues": [],
                    "warnings": [],
                    "existing_device": None,
                    "site": {},
                    "device_type": {},
                    "device_role": {},
                    "platform": {"platform": os_platform, "assessment": assessment, "reason_text": ""},
                },
                "libre_device": {
                    "device_id": 9,
                    "sysName": "details",
                    "hostname": "details",
                    "serial": "",
                    "hardware": "Test Router",
                    "os": "junos",
                    "version": VERSION,
                    "ip": "198.18.7.9",
                    "location": "Lab",
                    "status": True,
                },
                "server_key": SERVER_KEY,
                "existing_device_model_name": None,
                "sync_info": {},
                "existing_id_servers": [],
                "use_sysname": True,
                "strip_domain": False,
                "can_add_platform": can_add_platform,
                "platform_display": _platform_display(
                    {"platform": os_platform, "assessment": assessment}, make_superuser("details-cell-su")
                ),
            },
            request=request,
        )

    def test_the_version_platform_is_created_on_import_for_an_actor_who_may_add_it(self):
        html = self._render(can_add_platform=True)

        assert 'data-version-platform="create"' in html
        assert f"junos {VERSION}" in html

    def test_the_missing_version_platform_is_shown_for_an_actor_who_may_not_add_it(self):
        html = self._render(can_add_platform=False)

        assert 'data-version-platform="missing"' in html
        assert f"&#x27;junos {VERSION}&#x27; missing" in html


class TestPlatformNamesFollowTheViewerScope:
    def test_a_blocked_row_does_not_name_a_platform_the_viewer_cannot_view(self, librenms_server):
        from netbox_librenms_plugin.models import PlatformMapping

        _tracking(True)
        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=_platform("Secret Junos"))
        _platform(f"Secret Junos {VERSION}")
        device = _linked("page-hidden-holder", librenms_server, 7331)

        html = _render_sync_page(device, _platform_blind_device_user("page-hidden-holder-viewer"))

        assert 'data-platform-blocked="name_taken"' in html
        assert HIDDEN_PLATFORM in html
        assert "Secret" not in html

    def test_ambiguous_version_platforms_are_not_named_to_a_viewer_who_cannot_view_them(self, librenms_server):
        _tracking(True)
        os_platform = _platform("junos")
        _platform(f"Lab {VERSION}", parent=os_platform)
        _platform(f"Core {VERSION}", parent=os_platform)
        device = _linked("page-hidden-ambiguous", librenms_server, 7332)

        html = _render_sync_page(device, _platform_blind_device_user("page-hidden-ambiguous-viewer"))

        assert 'data-platform-blocked="version_platform_ambiguous"' in html
        assert "Lab " not in html
        assert "Core " not in html


class TestVirtualMachinePlatformRow:
    def test_the_vm_sync_page_offers_the_platform_sync(self, librenms_server):
        _tracking(True)
        os_platform = _platform("junos")
        vm = _linked_vm("vm-page", librenms_server, 7341, platform=os_platform)

        html = _render_sync_page(vm)

        assert f"/virtual-machines/{vm.pk}/update-platform/" in html
        assert f"Create version platform &#x27;junos {VERSION}&#x27; and set it" in html

    def test_the_vm_sync_page_offers_create_and_sync_for_a_missing_os_platform(self, librenms_server):
        _tracking(False)
        vm = _linked_vm("vm-page-missing", librenms_server, 7342)

        html = _render_sync_page(vm)

        assert 'id="create-platform-modal"' in html
        assert f"/virtual-machines/{vm.pk}/create-and-assign-platform/" in html

    def test_the_vm_sync_creates_and_assigns_the_version_platform(self, client, librenms_server):
        from dcim.models import Platform
        from virtualization.models import VirtualMachine

        _tracking(True)
        os_platform = _platform("junos")
        vm = _linked_vm("vm-update", librenms_server, 7343, platform=os_platform)
        from netbox_librenms_plugin.tests.view_test_helpers import grant

        actor = make_user_with_perms("vm-update-adder", [("change", VirtualMachine), ("view", Platform)])
        client.force_login(grant(actor, "add", Platform))

        response = _post(client, "update_vm_platform", vm)

        vm.refresh_from_db()
        assert vm.platform.parent == os_platform
        assert response.url.startswith(reverse("plugins:netbox_librenms_plugin:vm_librenms_sync", kwargs={"pk": vm.pk}))
        assert f"Virtual machine platform updated from 'junos' to 'junos {VERSION}'" in message_texts(
            response.wsgi_request, "success"
        )

    def test_the_vm_sync_without_add_platform_is_blocked(self, client, librenms_server):
        from dcim.models import Platform
        from virtualization.models import VirtualMachine

        _tracking(True)
        os_platform = _platform("junos")
        vm = _linked_vm("vm-update-denied", librenms_server, 7344, platform=os_platform)
        client.force_login(make_user_with_perms("vm-update-denied", [("change", VirtualMachine)]))

        response = _post(client, "update_vm_platform", vm)

        vm.refresh_from_db()
        assert vm.platform == os_platform
        assert not Platform.objects.filter(parent=os_platform).exists()
        assert any("permission to add platforms" in text for text in message_texts(response.wsgi_request, "error"))

    def test_the_vm_sync_needs_change_on_the_virtual_machine(self, client, librenms_server):
        from dcim.models import Device, Platform

        _tracking(True)
        os_platform = _platform("junos")
        vm = _linked_vm("vm-update-forbidden", librenms_server, 7345, platform=os_platform)
        client.force_login(make_user_with_perms("vm-update-forbidden", [("change", Device), ("add", Platform)]))

        response = _post(client, "update_vm_platform", vm)

        vm.refresh_from_db()
        assert vm.platform == os_platform
        assert not Platform.objects.filter(parent=os_platform).exists()
        errors = message_texts(response.wsgi_request, "error")
        assert any("virtualization.change_virtualmachine" in text for text in errors), errors

    def test_vm_create_and_sync_creates_the_os_platform(self, client, librenms_server):
        _tracking(False)
        vm = _linked_vm("vm-create", librenms_server, 7346)
        client.force_login(make_superuser("vm-create-su"))

        response = _post(
            client, "create_and_assign_vm_platform", vm, {"platform_name": "Juniper Junos", "librenms_os": "junos"}
        )

        vm.refresh_from_db()
        assert vm.platform.name == "Juniper Junos"
        assert response.url.startswith(reverse("plugins:netbox_librenms_plugin:vm_librenms_sync", kwargs={"pk": vm.pk}))


class TestCreateAndSyncChecksTheFormAgainstNetBoxAndLibreNMS:
    def test_a_version_that_changed_since_the_form_was_rendered_changes_nothing(self, client, librenms_server):
        from dcim.models import Platform

        _tracking(True)
        device = _linked("create-stale", librenms_server, 7351, version="22.1R1")
        client.force_login(make_superuser("create-stale-su"))
        count = Platform.objects.count()

        response = _post(
            client,
            "create_and_assign_platform",
            device,
            {"platform_name": "Juniper Junos", "librenms_os": "junos", "librenms_version": VERSION},
        )

        device.refresh_from_db()
        assert device.platform is None
        assert Platform.objects.count() == count
        assert any("now reports version '22.1R1'" in text for text in message_texts(response.wsgi_request, "error"))

    def test_unreadable_librenms_facts_refuse_the_create(self, client, librenms_server):
        from dcim.models import Platform

        _tracking(True)
        device = make_device("create-unreadable", librenms_cf={SERVER_KEY: 7352})
        librenms_server.register("/api/v0/devices/7352", {"status": "error"}, status=404)
        client.force_login(make_superuser("create-unreadable-su"))
        count = Platform.objects.count()

        response = _post(
            client,
            "create_and_assign_platform",
            device,
            {"platform_name": "Juniper Junos", "librenms_os": "junos", "librenms_version": VERSION},
        )

        device.refresh_from_db()
        assert device.platform is None
        assert Platform.objects.count() == count
        assert any("Failed to retrieve device info" in text for text in message_texts(response.wsgi_request, "error"))

    def test_a_mapping_added_after_the_form_was_rendered_changes_nothing(self, client, librenms_server):
        from dcim.models import Platform

        from netbox_librenms_plugin.models import PlatformMapping

        _tracking(True)
        device = _linked("create-mapped-since", librenms_server, 7353)
        PlatformMapping.objects.create(librenms_os="junos", netbox_platform=_platform("Mapped Junos"))
        client.force_login(make_superuser("create-mapped-since-su"))
        count = Platform.objects.count()

        response = _post(
            client,
            "create_and_assign_platform",
            device,
            {"platform_name": "Juniper Junos", "librenms_os": "junos", "librenms_version": VERSION},
        )

        device.refresh_from_db()
        assert device.platform is None
        assert Platform.objects.count() == count
        assert any("now matches LibreNMS OS 'junos'" in text for text in message_texts(response.wsgi_request, "error"))

    def test_a_lock_conflict_on_the_mapping_keeps_the_committed_assignment(self, client, librenms_server):
        from netbox_librenms_plugin.models import PlatformMapping
        from netbox_librenms_plugin.tests.lock_conflict_helpers import failing_statement
        from netbox_librenms_plugin.transactions import TRY_AGAIN_MESSAGE

        _tracking(False)
        device = make_device("create-mapping-conflict")
        client.force_login(make_superuser("create-mapping-conflict-su"))

        with failing_statement(
            lambda sql, params: sql.startswith('INSERT INTO "netbox_librenms_plugin_platformmapping"'), "55P03"
        ) as failed:
            response = _post(
                client,
                "create_and_assign_platform",
                device,
                {"platform_name": "Juniper Junos", "librenms_os": "junos", "create_mapping": "on"},
            )

        assert failed
        device.refresh_from_db()
        assert response.status_code == 302
        assert device.platform.name == "Juniper Junos"
        assert not PlatformMapping.objects.filter(librenms_os="junos").exists()
        assert f"Platform assigned; OS mapping not created: {TRY_AGAIN_MESSAGE}" in message_texts(
            response.wsgi_request, "warning"
        )
