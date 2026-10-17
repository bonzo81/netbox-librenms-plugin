"""
Row rebuild coverage for the verify endpoints in ``views/object_sync/devices.py``.

``SingleModuleVerifyView`` re-derives one module row from the cached inventory snapshot the
modules tab wrote, and ``SingleInterfaceVerifyView`` normalises a chassis member to its sync
device first. The primary home for both is ``test_verify_views.py``; these cases live in their
own file because higher branches of the PR stack grow that file's tail.
"""

import html
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
    },
    {
        "entPhysicalIndex": 2,
        "entPhysicalName": "GigabitEthernet0/1",
        "entPhysicalDescr": "1000BASE-T port",
        "entPhysicalModelName": "GLC-T",
        "entPhysicalClass": "port",
        "entPhysicalContainedIn": 1,
        "entPhysicalSerialNum": "PORT-SERIAL-1",
    },
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


def _register_inventory(server, inventory):
    """Register the three routes a modules-tab refresh reads."""
    server.inventory_response(LIBRENMS_ID, inventory)
    server.register(f"/api/v0/devices/{LIBRENMS_ID}/transceivers", {"status": "ok", "transceivers": []})
    server.register(f"/api/v0/devices/{LIBRENMS_ID}/ports", {"status": "ok", "ports": []})


def _refresh_modules(client, device):
    """Run the real modules-tab refresh so it writes the inventory snapshot to the cache."""
    from django.urls import reverse

    return client.post(
        reverse("plugins:netbox_librenms_plugin:device_module_sync", args=[device.pk]),
        {"server_key": SERVER_KEY},
        HTTP_HX_REQUEST="true",
    )


def _verify_module(client, body):
    """POST a JSON body to the single-module verify endpoint."""
    from django.urls import reverse

    return client.post(
        reverse("plugins:netbox_librenms_plugin:verify_module"),
        data=json.dumps(body),
        content_type="application/json",
    )


@pytest.mark.django_db
class TestSingleModuleVerifyRow:
    """The verify endpoint rebuilds one row from the snapshot the modules tab cached."""

    def test_a_cached_inventory_row_is_rebuilt_and_formatted(self, client, librenms_server):
        """A refreshed snapshot yields the matched bay, module type and serial for the asked index."""
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser

        device = make_device_with_module_bays("module-verify-row", ["Bay 1"], serial="CHASSIS-1")
        device.custom_field_data["librenms_id"] = {SERVER_KEY: LIBRENMS_ID}
        device.save()
        make_module_type("LC-24")
        _register_inventory(librenms_server, INVENTORY)
        client.force_login(make_superuser("module-verify-row-user"))

        assert _refresh_modules(client, device).status_code == 200

        response = _verify_module(
            client,
            {"device_id": device.pk, "ent_physical_index": 1, "server_key": SERVER_KEY},
        )

        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "success"
        row = body["formatted_row"]
        assert row["name"] == "Bay 1"
        assert row["description"] == "24 port line card"
        assert "MOD-SERIAL-1" in row["serial"]
        assert "Bay 1" in row["module_bay"]
        assert "LC-24" in row["module_type"]
        assert "Matched" in row["status"]
        # The rebuilt row is the one the asked index selected, and it carries the write actions
        # the caller's real permissions allow.
        assert 'name="ent_index" value="1"' in row["actions"]
        assert 'name="inventory_binding" value="' in row["actions"]
        assert 'name="inventory_binding" value=""' not in row["actions"]
        assert "Install" in row["actions"]

    def test_a_row_depth_that_matches_nothing_falls_back_to_the_index_match(self, client, librenms_server):
        """A posted depth no row carries still resolves the row through the index-only fallback."""
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser

        device = make_device_with_module_bays("module-verify-depth", ["Bay 1"], serial="CHASSIS-2")
        device.custom_field_data["librenms_id"] = {SERVER_KEY: LIBRENMS_ID}
        device.save()
        make_module_type("LC-24")
        _register_inventory(librenms_server, INVENTORY)
        client.force_login(make_superuser("module-verify-depth-user"))

        assert _refresh_modules(client, device).status_code == 200

        response = _verify_module(
            client,
            {"device_id": device.pk, "ent_physical_index": 1, "depth": 5, "server_key": SERVER_KEY},
        )

        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "success"
        assert body["formatted_row"]["name"] == "Bay 1"
        assert "MOD-SERIAL-1" in body["formatted_row"]["serial"]

    def test_a_verified_row_uses_the_same_carrier_rules_as_the_modules_table(self, client, librenms_server):
        """The verify adapter must preserve the production table builder's carrier-rule state."""
        from dcim.models import ModuleType

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
        inventory = [
            {
                "entPhysicalIndex": 3,
                "entPhysicalName": "Orphan Power Unit",
                "entPhysicalModelName": "UNMAPPED-POWER",
                "entPhysicalClass": "powerSupply",
                "entPhysicalContainedIn": 0,
                "entPhysicalSerialNum": "POWER-SERIAL-1",
            }
        ]
        _register_inventory(librenms_server, inventory)
        client.force_login(make_superuser("module-verify-carrier-user"))

        assert _refresh_modules(client, device).status_code == 200

        response = _verify_module(
            client,
            {"device_id": device.pk, "ent_physical_index": 3, "server_key": SERVER_KEY},
        )

        assert response.status_code == 200
        actions = response.json()["formatted_row"]["actions"]
        assert 'data-action="install-carrier"' in actions
        assert "Install Verify Carrier into 'Carrier Bay'" in html.unescape(actions)

    def test_a_manual_chassis_member_selection_overrides_inventory_position(self, client, librenms_server):
        """Row verification must bind actions to the member selected in the row dropdown."""
        from dcim.models import VirtualChassis

        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser

        first = make_device_with_module_bays("module-verify-vc-first", ["Bay 1"], serial="VERIFY-VC-FIRST")
        selected = make_device_with_module_bays("module-verify-vc-selected", ["Bay 1"], serial="VERIFY-VC-SELECTED")
        chassis = VirtualChassis.objects.create(name="module-verify-vc", master=first)
        for position, member in ((1, first), (2, selected)):
            member.virtual_chassis = chassis
            member.vc_position = position
            member.save(update_fields=["virtual_chassis", "vc_position"])
        first.custom_field_data["librenms_id"] = {SERVER_KEY: LIBRENMS_ID}
        first.save()
        make_module_type("LC-24")
        inventory = [{**INVENTORY[0], "entPhysicalParentRelPos": 1}]
        _register_inventory(librenms_server, inventory)
        client.force_login(make_superuser("module-verify-vc-user"))

        assert _refresh_modules(client, first).status_code == 200

        response = _verify_module(
            client,
            {"device_id": selected.pk, "ent_physical_index": 1, "server_key": SERVER_KEY},
        )

        assert response.status_code == 200
        actions = response.json()["formatted_row"]["actions"]
        assert f'name="selected_device_id" value="{selected.pk}"' in actions


@pytest.mark.django_db
class TestSingleInterfaceVerifyChassisGuard:
    """A chassis member with no resolvable sync device stops before any cache read."""

    def test_unresolvable_chassis_sync_device_returns_404(self, client, librenms_server):
        """A virtual chassis whose members resolve to no sync device returns the named 404."""
        from django.urls import reverse
        from netbox_librenms_plugin.tests.conftest import make_superuser, make_virtual_chassis_members
        from netbox_librenms_plugin.utils import get_librenms_sync_device

        _chassis, (device, _sibling) = make_virtual_chassis_members("iface-verify-vc")
        device.vc_position = None
        device.save(update_fields=["vc_position"])
        assert get_librenms_sync_device(device, server_key=SERVER_KEY) is None
        client.force_login(make_superuser("iface-verify-vc-user"))

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:verify_interface"),
            data=json.dumps({"device_id": device.pk, "port_id": 101, "server_key": SERVER_KEY}),
            content_type="application/json",
        )

        assert response.status_code == 404
        assert response.json() == {"status": "error", "message": "No sync device found for virtual chassis"}
