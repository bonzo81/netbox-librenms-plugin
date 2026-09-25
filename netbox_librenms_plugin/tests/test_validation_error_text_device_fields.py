"""
The device field-sync views show NetBox's validation message only to a superuser.

Each test drives a real POST through a view. An admin ``CUSTOM_VALIDATORS`` rule (or, where the
view calls no ``clean()``, a real ``pre_save`` receiver of another plugin) refuses the write with a
message that names a device outside the viewer's scope. A restricted viewer gets the refused field
or the model, and a superuser gets the message.
"""

from copy import deepcopy

import pytest
from django.db.models.signals import pre_save
from django.urls import reverse
from extras.validators import CustomValidator

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser, make_virtual_chassis
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms, message_texts

SERVER_KEY = "default"
SECONDARY_KEY = "secondary"
HIDDEN = "(only a superuser sees the message)"


class _Refuses(CustomValidator):
    """An admin validator that refuses every clean() with *text* under *key*."""

    def __init__(self, text, key=None):
        super().__init__()
        self._text, self._key = text, key

    def validate(self, instance, request):
        self.fail(self._text, field=self._key)


@pytest.fixture
def librenms_server(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        servers = {
            key: {"librenms_url": server.url, "api_token": f"{key}-token", "cache_timeout": 300, "verify_ssl": False}
            for key in (SERVER_KEY, SECONDARY_KEY)
        }
        plugin_config["netbox_librenms_plugin"]["servers"] = servers
        plugin_config["netbox_librenms_plugin"].pop("librenms_url", None)
        settings.PLUGINS_CONFIG = plugin_config
        yield server


def _post(client, name, obj, data=None):
    url = reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"pk": obj.pk})
    return client.post(url, {"server_key": SERVER_KEY, **(data or {})})


def _viewer(tag, superuser, *devices):
    """Return a superuser, or a user who may view and change only *devices*."""
    from dcim.models import Device

    if superuser:
        return make_superuser(f"{tag}-superuser")
    return make_user_with_perms(
        f"{tag}-viewer", [("view", Device), ("change", Device)], constraints={"pk__in": [d.pk for d in devices]}
    )


def _assert_shown(response, hidden, superuser, restricted_text):
    texts = " | ".join(message_texts(response.wsgi_request))
    if superuser:
        assert f"Conflicts with {hidden.name}." in texts, texts
    else:
        assert f"NetBox refuses {restricted_text} {HIDDEN}" in texts, texts
        assert hidden.name not in texts, texts


@pytest.mark.django_db
@pytest.mark.parametrize("superuser", [False, True], ids=["restricted", "superuser"])
class TestADeviceFieldSyncShowsNetBoxsMessageOnlyToASuperuser:
    def test_the_name_sync(self, client, settings, librenms_server, superuser):
        tag = f"vetext-name-{int(superuser)}"
        device, hidden = make_device(tag, librenms_cf={SERVER_KEY: 7101}), make_device(f"{tag}-hidden")
        librenms_server.device_info_response(device_id=7101, hostname=f"{tag}-renamed")
        settings.CUSTOM_VALIDATORS = {"dcim.device": [_Refuses(f"Conflicts with {hidden.name}.", "serial")]}
        client.force_login(_viewer(tag, superuser, device))

        response = _post(client, "update_device_name", device)

        _assert_shown(response, hidden, superuser, "the serial field")

    def test_the_serial_sync(self, client, settings, librenms_server, superuser):
        tag = f"vetext-serial-{int(superuser)}"
        device, hidden = make_device(tag, librenms_cf={SERVER_KEY: 7102}), make_device(f"{tag}-hidden")
        librenms_server.device_info_response(device_id=7102, hostname=device.name, serial="NEW-SERIAL")
        settings.CUSTOM_VALIDATORS = {"dcim.device": [_Refuses(f"Conflicts with {hidden.name}.", hidden.name)]}
        client.force_login(_viewer(tag, superuser, device))

        response = _post(client, "update_device_serial", device)

        _assert_shown(response, hidden, superuser, "the device")

    def test_the_device_type_sync(self, client, settings, librenms_server, superuser):
        from dcim.models import DeviceType

        tag = f"vetext-type-{int(superuser)}"
        device, hidden = make_device(tag, librenms_cf={SERVER_KEY: 7103}), make_device(f"{tag}-hidden")
        replacement = DeviceType.objects.create(
            manufacturer=device.device_type.manufacturer, model=f"Model {tag}", slug=f"model-{tag}"
        )
        librenms_server.device_info_response(device_id=7103, hostname=device.name, hardware=replacement.model)
        settings.CUSTOM_VALIDATORS = {"dcim.device": [_Refuses(f"Conflicts with {hidden.name}.", "device_type")]}
        client.force_login(_viewer(tag, superuser, device))

        response = _post(client, "update_device_type", device)

        _assert_shown(response, hidden, superuser, "the device_type field")

    def test_the_platform_sync(self, client, settings, librenms_server, superuser):
        from dcim.models import Platform

        tag = f"vetext-platform-{int(superuser)}"
        device, hidden = make_device(tag, librenms_cf={SERVER_KEY: 7104}), make_device(f"{tag}-hidden")
        platform = Platform.objects.create(name=f"Platform {tag}", slug=f"platform-{tag}")
        librenms_server.device_info_response(device_id=7104, hostname=device.name, os=platform.name)
        settings.CUSTOM_VALIDATORS = {"dcim.device": [_Refuses(f"Conflicts with {hidden.name}.")]}
        viewer = _viewer(tag, superuser, device)
        client.force_login(viewer if superuser else grant(viewer, "view", Platform))

        response = _post(client, "update_device_platform", device)

        _assert_shown(response, hidden, superuser, "the device")

    def test_the_platform_create(self, client, settings, librenms_server, superuser):
        from dcim.models import Platform

        tag = f"vetext-newplatform-{int(superuser)}"
        device, hidden = make_device(tag), make_device(f"{tag}-hidden")
        settings.CUSTOM_VALIDATORS = {"dcim.platform": [_Refuses(f"Conflicts with {hidden.name}.")]}
        viewer = _viewer(tag, superuser, device)
        client.force_login(viewer if superuser else grant(viewer, "add", Platform))

        response = _post(client, "create_and_assign_platform", device, {"platform_name": f"Platform {tag}"})

        _assert_shown(response, hidden, superuser, "the platform")

    def test_the_platform_assignment(self, client, settings, librenms_server, superuser):
        from dcim.models import Platform

        tag = f"vetext-assignplatform-{int(superuser)}"
        device, hidden = make_device(tag), make_device(f"{tag}-hidden")
        platform = Platform.objects.create(name=f"Platform {tag}", slug=f"platform-{tag}")
        settings.CUSTOM_VALIDATORS = {"dcim.device": [_Refuses(f"Conflicts with {hidden.name}.", "platform")]}
        viewer = _viewer(tag, superuser, device)
        client.force_login(viewer if superuser else grant(viewer, "view", Platform))

        response = _post(client, "create_and_assign_platform", device, {"platform_name": platform.name})

        _assert_shown(response, hidden, superuser, "the platform field")

    def test_the_platform_mapping_create(self, client, settings, librenms_server, superuser):
        from dcim.models import Platform

        from netbox_librenms_plugin.models import PlatformMapping

        tag = f"vetext-mapping-{int(superuser)}"
        device, hidden = make_device(tag), make_device(f"{tag}-hidden")
        platform = Platform.objects.create(name=f"Platform {tag}", slug=f"platform-{tag}")
        settings.CUSTOM_VALIDATORS = {
            "netbox_librenms_plugin.platformmapping": [_Refuses(f"Conflicts with {hidden.name}.", "netbox_platform")]
        }
        viewer = _viewer(tag, superuser, device)
        if not superuser:
            viewer = grant(grant(viewer, "view", Platform), "add", PlatformMapping)
        client.force_login(viewer)

        response = _post(
            client,
            "create_and_assign_platform",
            device,
            {"platform_name": platform.name, "librenms_os": f"os-{tag}", "create_mapping": "on"},
        )

        _assert_shown(response, hidden, superuser, "the netbox_platform field")

    def test_the_virtual_chassis_serial_assignment(self, client, settings, librenms_server, superuser):
        tag = f"vetext-vcserial-{int(superuser)}"
        first, second, hidden = make_device(f"{tag}-1"), make_device(f"{tag}-2"), make_device(f"{tag}-hidden")
        make_virtual_chassis(tag, first, second)
        settings.CUSTOM_VALIDATORS = {"dcim.device": [_Refuses(f"Conflicts with {hidden.name}.", hidden.name)]}
        client.force_login(_viewer(tag, superuser, first, second))

        response = _post(client, "assign_vc_serial", first, {"serial_1": "VC-SERIAL", "member_id_1": str(second.pk)})

        _assert_shown(response, hidden, superuser, "the device")

    def test_the_legacy_id_conversion(self, client, settings, librenms_server, superuser):
        tag = f"vetext-legacy-{int(superuser)}"
        device, hidden = make_device(tag, serial="LEGACY", librenms_cf="7105"), make_device(f"{tag}-hidden")
        librenms_server.device_info_response(device_id=7105, hostname=device.name, serial="LEGACY")
        settings.CUSTOM_VALIDATORS = {"dcim.device": [_Refuses(f"Conflicts with {hidden.name}.", "custom_field_data")]}
        client.force_login(_viewer(tag, superuser, device))

        response = _post(client, "convert_legacy_librenms_id", device, {"object_type": "device"})

        _assert_shown(response, hidden, superuser, "the custom_field_data field")

    @pytest.mark.parametrize(
        "view_name, data, key",
        [
            ("remove_server_mapping", {"object_type": "device", "server_key": "retired"}, None),
            ("set_preferred_server", {"object_type": "device", "server_key": SECONDARY_KEY}, "custom_field_data"),
        ],
    )
    def test_the_server_mapping_edits(self, client, librenms_server, superuser, view_name, data, key):
        """These views call no clean(), so another plugin's pre_save receiver refuses the write."""
        from dcim.models import Device
        from django.core.exceptions import ValidationError

        tag = f"vetext-{view_name}-{int(superuser)}"
        mapping = {SERVER_KEY: 7106, SECONDARY_KEY: 7107, "retired": 7108}
        device, hidden = make_device(tag, librenms_cf=mapping), make_device(f"{tag}-hidden")
        client.force_login(_viewer(tag, superuser, device))

        def refuse(sender, instance, **kwargs):
            if instance.pk == device.pk:
                raise ValidationError({key or hidden.name: [f"Conflicts with {hidden.name}."]})

        pre_save.connect(refuse, sender=Device, weak=False)
        try:
            response = _post(client, view_name, device, data)
        finally:
            pre_save.disconnect(refuse, sender=Device)

        _assert_shown(response, hidden, superuser, "the custom_field_data field" if key else "the device")
