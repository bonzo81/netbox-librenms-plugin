"""
The device import actions show NetBox's validation message only to a superuser.

Each test drives a real HTMX POST through an import view against the stub LibreNMS server. A
restricted viewer gets the refused field or the model, and a superuser gets the message, which
names a device outside the viewer's scope.
"""

from html import unescape

import pytest
from django.db.models.signals import pre_save
from django.urls import reverse
from extras.validators import CustomValidator

from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server
from netbox_librenms_plugin.tests.test_modules_view import configure_servers
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

SERVER_KEY = "vetext-imports"
LIBRENMS_ID = 7201
HIDDEN = "(only a superuser sees the message)"


class _Refuses(CustomValidator):
    """An admin validator that refuses every clean() with *text* and no field."""

    def __init__(self, text):
        super().__init__()
        self._text = text

    def validate(self, instance, request):
        self.fail(self._text)


@pytest.fixture
def librenms_server(settings, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        configure_servers(
            settings, {SERVER_KEY: {"librenms_url": server.url, "api_token": "test-token", "verify_ssl": False}}
        )
        yield server


def _register(server, hostname):
    server.device_info_response(device_id=LIBRENMS_ID, hostname=hostname, hardware="Unmatched", os="", serial="", ip="")
    server.vc_inventory_callable(LIBRENMS_ID, [], {})


def _post(client, name, data):
    url = reverse(f"plugins:netbox_librenms_plugin:{name}", kwargs={"device_id": LIBRENMS_ID})
    response = client.post(url, {"server_key": SERVER_KEY, **data}, HTTP_HX_REQUEST="true")
    return unescape(response.content.decode())


def _assert_shown(content, hidden, superuser, restricted_text):
    if superuser:
        assert f"Conflicts with {hidden.name}." in content, content
    else:
        assert f"NetBox refuses {restricted_text} {HIDDEN}" in content, content
        assert hidden.name not in content, content


@pytest.mark.django_db
@pytest.mark.parametrize("superuser", [False, True], ids=["restricted", "superuser"])
class TestAnImportActionShowsNetBoxsMessageOnlyToASuperuser:
    def test_the_name_sync_of_a_matched_device(self, client, librenms_server, superuser):
        """The save skips clean(), so another plugin's pre_save receiver refuses the write."""
        from dcim.models import Device
        from django.core.exceptions import ValidationError

        tag = f"vetext-importname-{int(superuser)}"
        device, hidden = make_device(tag, librenms_cf={SERVER_KEY: LIBRENMS_ID}), make_device(f"{tag}-hidden")
        _register(librenms_server, f"{tag}-renamed")
        viewer = make_superuser(f"{tag}-superuser") if superuser else None
        if viewer is None:
            viewer = make_user_with_perms(
                f"{tag}-viewer", [("view", Device), ("change", Device)], constraints={"pk": device.pk}
            )
        client.force_login(viewer)

        def refuse(sender, instance, **kwargs):
            if instance.pk == device.pk:
                raise ValidationError({hidden.name: [f"Conflicts with {hidden.name}."]})

        pre_save.connect(refuse, sender=Device, weak=False)
        try:
            content = _post(client, "device_conflict_action", {"action": "sync_name", "existing_device_id": device.pk})
        finally:
            pre_save.disconnect(refuse, sender=Device)

        _assert_shown(content, hidden, superuser, "the device")

    def test_the_platform_create(self, client, settings, librenms_server, superuser):
        from dcim.models import Platform

        tag = f"vetext-importplatform-{int(superuser)}"
        hidden = make_device(f"{tag}-hidden")
        _register(librenms_server, tag)
        settings.CUSTOM_VALIDATORS = {"dcim.platform": [_Refuses(f"Conflicts with {hidden.name}.")]}
        client.force_login(
            make_superuser(f"{tag}-superuser")
            if superuser
            else make_user_with_perms(f"{tag}-viewer", [("add", Platform)])
        )

        content = _post(client, "create_platform_from_import", {"platform_name": f"Platform {tag}"})

        _assert_shown(content, hidden, superuser, "the platform")
