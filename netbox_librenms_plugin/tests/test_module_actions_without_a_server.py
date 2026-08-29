"""Module sync actions must fail closed when no LibreNMS server can be bound."""

from copy import deepcopy

import pytest
from dcim.models import Module
from django.contrib.messages import get_messages
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    configure_no_librenms_servers,
    make_device,
    make_module_bay,
    make_module_type,
    make_superuser,
)
from netbox_librenms_plugin.views.sync.modules import NO_LIBRENMS_SERVER_MESSAGE


def _configure_default_server(settings):
    """Configure one bindable server without depending on the devcontainer settings."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        "default": {"librenms_url": "https://librenms.example.test", "api_token": "test-token"}
    }
    settings.PLUGINS_CONFIG = plugin_config


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
def test_module_action_reports_the_missing_server_instead_of_failing(client, settings, url_name, method, payload_for):
    """Each action reads the LibreNMS server key first, so an unusable configuration must not 500."""
    configure_no_librenms_servers(settings)
    device, bay, module_type, module = _seed_device_with_installed_module(f"no-server-{url_name}")
    client.force_login(make_superuser(f"no-server-{url_name}-user"))
    url = reverse(f"plugins:netbox_librenms_plugin:{url_name}", kwargs={"pk": device.pk})

    response = getattr(client, method)(url, payload_for(bay, module_type, module))

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
    assert view.resolve_posted_server_key_or_none({"server_key": "forged"}) == "default"

    configure_no_librenms_servers(settings)
    assert DeviceInterfaceTableView().resolve_posted_server_key_or_none({"server_key": ""}) is None
