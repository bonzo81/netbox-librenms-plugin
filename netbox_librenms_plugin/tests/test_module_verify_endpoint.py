"""Integration coverage for module row verification."""

import json

import pytest


SERVER_KEY = "default"
LIBRENMS_ID = 42
INVENTORY = [
    {
        "entPhysicalIndex": 1,
        "entPhysicalName": "Bay 1",
        "entPhysicalDescr": "24 port line card",
        "entPhysicalModelName": "LC-24",
        "entPhysicalClass": "module",
        "entPhysicalContainedIn": 0,
        "entPhysicalSerialNum": "MOD-SERIAL-1",
    }
]


@pytest.fixture
def librenms_server(settings, monkeypatch):
    """Point the plugin at a loopback LibreNMS whose snapshots outlive one request."""
    from netbox_librenms_plugin.tests.conftest import configure_librenms_servers
    from netbox_librenms_plugin.tests.mock_librenms_server import librenms_mock_server

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    with librenms_mock_server() as server:
        configure_librenms_servers(
            settings,
            {
                SERVER_KEY: {
                    "librenms_url": server.url,
                    "api_token": "module-verify-token",
                    "cache_timeout": 300,
                    "verify_ssl": False,
                }
            },
        )
        yield server


def _register_inventory(server, inventory=None):
    """Register the three routes a modules-tab refresh reads."""
    server.inventory_response(LIBRENMS_ID, INVENTORY if inventory is None else inventory)
    server.register(f"/api/v0/devices/{LIBRENMS_ID}/transceivers", {"status": "ok", "transceivers": []})
    server.register(f"/api/v0/devices/{LIBRENMS_ID}/ports", {"status": "ok", "ports": []})


@pytest.mark.django_db
def test_verified_module_actions_carry_the_cached_inventory_binding(client, librenms_server):
    """A rebuilt actionable row must include a binding to its cached inventory snapshot."""
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser

    device = make_device_with_module_bays("module-verify-binding", ["Bay 1"], serial="CHASSIS-1")
    device.custom_field_data["librenms_id"] = {SERVER_KEY: LIBRENMS_ID}
    device.save()
    make_module_type("LC-24")
    _register_inventory(librenms_server)
    client.force_login(make_superuser("module-verify-binding-user"))

    refresh = client.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )
    assert refresh.status_code == 200

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_module"),
        data=json.dumps({"device_id": device.pk, "ent_physical_index": 1, "server_key": SERVER_KEY}),
        content_type="application/json",
    )

    assert response.status_code == 200
    row = response.json()["formatted_row"]
    assert 'name="ent_index" value="1"' in row["actions"]
    assert 'name="inventory_binding" value="' in row["actions"]
    assert 'name="inventory_binding" value=""' not in row["actions"]


@pytest.mark.django_db
def test_verified_module_actions_use_the_modules_table_carrier_rules(client, librenms_server):
    """A verified row must preserve the module table's carrier-rule state."""
    from dcim.models import ModuleType
    from django.urls import reverse

    from netbox_librenms_plugin.models import CarrierAutoInstallRule
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_superuser

    device = make_device_with_module_bays("module-verify-carrier", ["Carrier Bay"])
    device.custom_field_data["librenms_id"] = {SERVER_KEY: LIBRENMS_ID}
    device.save()
    carrier_type = ModuleType.objects.create(
        manufacturer=device.device_type.manufacturer,
        model="Verify Carrier",
    )
    CarrierAutoInstallRule.objects.create(
        manufacturer=device.device_type.manufacturer,
        device_type_pattern=device.device_type.model,
        librenms_child_class="powerSupply",
        librenms_child_name_pattern="Orphan Power Unit",
        netbox_bay_name_pattern="Carrier Bay",
        carrier_module_type=carrier_type,
    )
    _register_inventory(
        librenms_server,
        [
            {
                "entPhysicalIndex": 3,
                "entPhysicalName": "Orphan Power Unit",
                "entPhysicalModelName": "UNMAPPED-POWER",
                "entPhysicalClass": "powerSupply",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "POWER-SERIAL-1",
            }
        ],
    )
    client.force_login(make_superuser("module-verify-carrier-user"))

    refresh = client.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )
    assert refresh.status_code == 200

    response = client.post(
        reverse("plugins:netbox_librenms_plugin:verify_module"),
        data=json.dumps({"device_id": device.pk, "ent_physical_index": 3, "server_key": SERVER_KEY}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert "Install Verify Carrier into &#39;Carrier Bay&#39;" in response.json()["formatted_row"]["actions"]
