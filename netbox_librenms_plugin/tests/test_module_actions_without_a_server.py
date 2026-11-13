"""Module sync actions must fail closed when no LibreNMS server can be bound."""

import pytest
from dcim.models import Module
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    configure_no_librenms_servers,
    make_device,
    make_module_bay,
    make_module_type,
    make_superuser,
)
from netbox_librenms_plugin.views.sync.modules import NO_LIBRENMS_SERVER_MESSAGE


def _configure_default_server(settings):
    """Configure one bindable server without depending on the devcontainer settings."""
    configure_librenms_servers(
        settings, {"default": {"librenms_url": "https://librenms.example.test", "api_token": "test-token"}}
    )


def _seed_device_with_installed_module(name):
    """Create a device carrying one installed module, the state these actions operate on."""
    device = make_device(name)
    bay = make_module_bay(device, "Slot 1")
    module_type = make_module_type(f"{name}-type")
    module = Module.objects.create(device=device, module_bay=bay, module_type=module_type, serial="SEEDED")
    return device, bay, module_type, module


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url_name", "method", "payload_for"),
    [
        (
            "install_module",
            "post",
            lambda bay, module_type, module: {
                "module_bay_id": str(bay.pk),
                "module_type_id": str(module_type.pk),
            },
        ),
        ("install_branch", "post", lambda bay, module_type, module: {"parent_index": "1"}),
        ("install_selected", "post", lambda bay, module_type, module: {"select": ["1"]}),
        (
            "update_module_interface",
            "post",
            lambda bay, module_type, module: {"module_id": str(module.pk), "port_id": "7801"},
        ),
        (
            "replace_module",
            "post",
            lambda bay, module_type, module: {"module_id": str(module.pk), "ent_index": "1"},
        ),
        (
            "module_mismatch_preview",
            "get",
            lambda bay, module_type, module: {"module_id": str(module.pk), "ent_index": "1"},
        ),
    ],
)
@pytest.mark.parametrize("posted_keys", [None, ["removed"], ["removed", "default"]], ids=["none", "stale", "repeated"])
def test_module_action_reports_the_missing_server_instead_of_failing(
    client, settings, url_name, method, payload_for, posted_keys
):
    """An unusable configuration, or a posted key that selects no single configured server, must not 500 or fall back."""
    if posted_keys is None:
        configure_no_librenms_servers(settings)
    else:
        _configure_default_server(settings)
    tag = f"{url_name}-{len(posted_keys or ())}"
    device, bay, module_type, module = _seed_device_with_installed_module(f"no-server-{tag}")
    client.force_login(make_superuser(f"no-server-{tag}-user"))
    url = reverse(f"plugins:netbox_librenms_plugin:{url_name}", kwargs={"pk": device.pk})
    payload = payload_for(bay, module_type, module)
    if posted_keys is not None:
        payload["server_key"] = posted_keys

    response = getattr(client, method)(url, payload)

    if method == "get":
        assert response.status_code == 400
        assert NO_LIBRENMS_SERVER_MESSAGE in response.content.decode()
    else:
        assert response.status_code == 302
        assert any(NO_LIBRENMS_SERVER_MESSAGE in str(message) for message in get_messages(response.wsgi_request))
    # Fail closed: these actions scope their cache reads and LibreNMS id writes by server key,
    # so none of them may touch NetBox without one.
    assert list(Module.objects.filter(device=device)) == [module]
    module.refresh_from_db()
    assert module.serial == "SEEDED"


@pytest.mark.django_db
def test_a_resolved_action_server_key_is_never_blank(settings):
    """The missing-server guard is the only no-server path, so a resolved key always scopes a bind."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

    _configure_default_server(settings)
    view = DeviceInterfaceTableView()

    assert view.resolve_posted_server_key_or_none({"server_key": ""}) == "default"
    assert view.resolve_posted_server_key_or_none({"server_key": "forged"}) is None

    configure_no_librenms_servers(settings)
    assert DeviceInterfaceTableView().resolve_posted_server_key_or_none({"server_key": ""}) is None
