"""
Real-DB e2e: UpdateModuleInterfaceView must not announce "Updated interface" for a no-op rebind.

When the clicked row's LibreNMS port already sits on the module's interface, ``_bind_interface_librenms_id``
returns ``{"status": "bound", "changed": False, ...}`` — nothing was written. The success message
must gate on ``changed`` so a no-op click doesn't tell the user an interface was updated when it wasn't.

These drive the real view end-to-end: real Device/ModuleType/ModuleBay/Module/Interface, real request,
real message framework. The LibreNMS HTTP boundary is never touched (server_key is posted, and the
module type carries no interface templates so adoption resolves to a real no-op).
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import configure_librenms_servers

SERVER_KEY = "default"


@pytest.fixture(autouse=True)
def _configure_default_server(settings):
    """Configure the server key used by these real request tests."""
    configure_librenms_servers(
        settings, {SERVER_KEY: {"librenms_url": "https://default.example.com", "api_token": "test-token"}}
    )


def _seed(name, *, port_id_on_interface):
    from dcim.models import (
        Device,
        DeviceRole,
        DeviceType,
        Interface,
        Manufacturer,
        Module,
        ModuleBay,
        ModuleType,
        Site,
    )

    mfr, _ = Manufacturer.objects.get_or_create(name="MIB-Mfr", slug="mib-mfr")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="MIB-DT", slug="mib-dt")
    role, _ = DeviceRole.objects.get_or_create(name="MIB-Role", slug="mib-role")
    site, _ = Site.objects.get_or_create(name="MIB-Site", slug="mib-site")
    device = Device.objects.create(name=name, device_type=dt, role=role, site=site, status="active")

    # A module type with NO interface templates: _adopt_existing_template_interfaces resolves to a
    # real "skipped" (nothing to adopt), leaving the port-bind result as the summary.
    mtype = ModuleType.objects.create(manufacturer=mfr, model=f"MT-{name}")
    bay = ModuleBay.objects.create(device=device, name=f"Bay-{name}")
    module = Module.objects.create(device=device, module_bay=bay, module_type=mtype)

    iface = Interface.objects.create(device=device, name="Gi0/1", type="1000base-t", module=module)
    if port_id_on_interface is not None:
        # Pre-bind the port ID under the real server so the rebind is a genuine no-op.
        iface.custom_field_data = {"librenms_id": {SERVER_KEY: port_id_on_interface}}
        iface.save()
    return device, module, iface


ENT_INDEX = 4077


def _post(device, module, inventory_item):
    from netbox_librenms_plugin.utils import module_inventory_binding_token, module_inventory_row_digest

    request = RequestFactory().post(
        f"/modules/{device.pk}/interface/",
        data={
            "module_id": str(module.pk),
            "server_key": SERVER_KEY,
            "ent_index": str(ENT_INDEX),
            "inventory_binding": module_inventory_binding_token(
                device.pk,
                SERVER_KEY,
                "update_module_interface",
                {"module_id": module.pk},
                ENT_INDEX,
                module_inventory_row_digest(inventory_item),
            ),
        },
    )
    request.user = get_user_model().objects.create_superuser(username=f"mib-{device.pk}", email="", password="x")
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def _drive(device, module, *, port_id, ifname):
    """Seed the row the clicked button names, then drive the real view over it."""
    from django.core.cache import cache

    from netbox_librenms_plugin.tests.view_test_helpers import (
        post as post_view,
        trusted_module_inventory_payload,
    )
    from netbox_librenms_plugin.views.sync.modules import UpdateModuleInterfaceView

    view = UpdateModuleInterfaceView()
    inventory_item = {"entPhysicalIndex": ENT_INDEX, "_librenms_port_id": port_id, "_librenms_ifname": ifname}
    request = _post(device, module, inventory_item)
    # The view reads the port identity from the cached row for ent_index, never from the post.
    cache_key = view.get_cache_key(device, "inventory", server_key=SERVER_KEY)
    cache.set(
        cache_key,
        trusted_module_inventory_payload(
            device,
            [inventory_item],
            server_key=SERVER_KEY,
            librenms_id=4077,
        ),
        timeout=300,
    )
    try:
        post_view(view, request, pk=device.pk)
    finally:
        cache.delete(cache_key)
    return [(m.level_tag, m.message) for m in request._messages]


@pytest.mark.django_db
class TestUpdateModuleInterfaceBindMessage:
    def test_noop_rebind_shows_no_updated_interface_message(self):
        # Interface already mapped to port_id 500 → posting the same port_id is a no-op.
        device, module, _iface = _seed("mib-noop", port_id_on_interface=500)
        messages = _drive(device, module, port_id=500, ifname="Gi0/1")

        joined = " || ".join(msg for _, msg in messages)
        assert not any("Updated interface" in msg for _, msg in messages), joined

    def test_real_rebind_still_announces_updated_interface(self):
        # Positive control: the interface has NO port binding yet, so posting a port_id actually
        # writes it (changed=True) and the confirmation message must appear.
        device, module, _iface = _seed("mib-real", port_id_on_interface=None)
        messages = _drive(device, module, port_id=600, ifname="Gi0/1")

        joined = " || ".join(msg for _, msg in messages)
        assert any("Updated interface" in msg for _, msg in messages), joined
