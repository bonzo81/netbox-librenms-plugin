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

from netbox_librenms_plugin.tests.mapping_fixtures import seed_mapping
from netbox_librenms_plugin.tests.view_test_helpers import module_tab_forms


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


def _verify_module(client, refreshed, page, body):
    """POST the verify request that the member select of the asked row on the refreshed tab sends."""
    from django.urls import reverse

    (select,) = [
        attrs
        for attrs in module_tab_forms(refreshed.content.decode()).selects
        if attrs.get("data-module") == str(body["ent_physical_index"])
    ]
    return client.post(
        reverse("plugins:netbox_librenms_plugin:verify_module"),
        data=json.dumps({"page_device_id": page.pk, "verify_binding": select["data-verify-binding"], **body}),
        content_type="application/json",
    )


def _in_a_chassis(device):
    """Enroll *device* in a two-member Virtual Chassis, so its Modules table renders a member select per row."""
    from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_virtual_chassis

    make_virtual_chassis(f"{device.name}-vc", device, make_device_with_module_bays(f"{device.name}-member", []))
    device.refresh_from_db()
    return device


@pytest.mark.django_db
class TestSingleModuleVerifyRow:
    """The verify endpoint rebuilds one row from the snapshot the modules tab cached."""

    def test_a_cached_inventory_row_is_rebuilt_and_formatted(self, client, librenms_server):
        """A refreshed snapshot yields the matched bay, module type and serial for the asked index."""
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser

        device = make_device_with_module_bays("module-verify-row", ["Bay 1"], serial="CHASSIS-1")
        seed_mapping(device, SERVER_KEY, own=LIBRENMS_ID, save=False)
        device.save()
        make_module_type("LC-24")
        _register_inventory(librenms_server, INVENTORY)
        client.force_login(make_superuser("module-verify-row-user"))

        refreshed = _refresh_modules(client, _in_a_chassis(device))
        assert refreshed.status_code == 200

        response = _verify_module(
            client,
            refreshed,
            device,
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
        seed_mapping(device, SERVER_KEY, own=LIBRENMS_ID, save=False)
        device.save()
        make_module_type("LC-24")
        _register_inventory(librenms_server, INVENTORY)
        client.force_login(make_superuser("module-verify-depth-user"))

        refreshed = _refresh_modules(client, _in_a_chassis(device))
        assert refreshed.status_code == 200

        response = _verify_module(
            client,
            refreshed,
            device,
            {"device_id": device.pk, "ent_physical_index": 1, "depth": 5, "server_key": SERVER_KEY},
        )

        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "success"
        assert body["formatted_row"]["name"] == "Bay 1"
        assert "MOD-SERIAL-1" in body["formatted_row"]["serial"]

    def test_a_rule_broken_after_the_render_is_named_in_the_verify_answer(self, client, librenms_server):
        """A refresh may never evaluate the broken rule, so the verify answer itself must identify it."""
        from netbox_librenms_plugin.models import ModuleBayMapping
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_module_type, make_superuser

        device = make_device_with_module_bays("module-verify-broken-rule", ["Bay 1"], serial="CHASSIS-6")
        seed_mapping(device, SERVER_KEY, own=LIBRENMS_ID, save=False)
        device.save()
        make_module_type("LC-24")
        _register_inventory(librenms_server, INVENTORY)
        client.force_login(make_superuser("module-verify-broken-rule-user"))
        refreshed = _refresh_modules(client, _in_a_chassis(device))
        assert refreshed.status_code == 200
        # bulk_create skips clean(), the way an import or old data stores a broken pattern.
        (mapping,) = ModuleBayMapping.objects.bulk_create(
            [ModuleBayMapping(librenms_name="(", librenms_class="", netbox_bay_name="Bay 1", is_regex=True)]
        )

        response = _verify_module(
            client,
            refreshed,
            device,
            {"device_id": device.pk, "ent_physical_index": 1, "server_key": SERVER_KEY},
        )

        assert response.status_code == 409
        assert f"(ID {mapping.pk})" in response.json()["message"]

    def test_a_verified_row_uses_the_same_carrier_rules_as_the_modules_table(self, client, librenms_server):
        """The verify adapter must preserve the production table builder's carrier-rule state."""
        from dcim.models import ModuleType

        from netbox_librenms_plugin.models import CarrierAutoInstallRule
        from netbox_librenms_plugin.tests.conftest import make_device_with_module_bays, make_superuser

        device = make_device_with_module_bays("module-verify-carrier", ["Carrier Bay"])
        seed_mapping(device, SERVER_KEY, own=LIBRENMS_ID, save=False)
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

        refreshed = _refresh_modules(client, _in_a_chassis(device))
        assert refreshed.status_code == 200

        response = _verify_module(
            client,
            refreshed,
            device,
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
        seed_mapping(first, SERVER_KEY, own=LIBRENMS_ID, save=False)
        first.save()
        make_module_type("LC-24")
        inventory = [{**INVENTORY[0], "entPhysicalParentRelPos": 1}]
        _register_inventory(librenms_server, inventory)
        client.force_login(make_superuser("module-verify-vc-user"))

        refreshed = _refresh_modules(client, first)
        assert refreshed.status_code == 200

        response = _verify_module(
            client,
            refreshed,
            first,
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
        from netbox_librenms_plugin.server_selection import get_librenms_sync_device

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


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("endpoint", ["cached-page", "refresh", "verify"])
def test_a_module_render_refuses_a_page_that_leaves_its_chassis_during_the_policy_read(
    client, librenms_server, endpoint
):
    from dcim.models import Device, VirtualChassis
    from django.db import close_old_connections, connection, transaction
    from django.urls import reverse

    from netbox_librenms_plugin.tests.conftest import (
        bounded_executor,
        make_device_with_module_bays,
        make_module_type,
        make_superuser,
    )

    device = _in_a_chassis(make_device_with_module_bays("module-policy-race", ["Bay 1"], serial="CHASSIS-6"))
    seed_mapping(device, SERVER_KEY, own=LIBRENMS_ID, save=False)
    device.save()
    make_module_type("LC-24")
    _register_inventory(librenms_server, INVENTORY)
    client.force_login(make_superuser("module-policy-race-user"))
    refreshed = _refresh_modules(client, device)
    assert refreshed.status_code == 200
    page_query, page_params = (
        Device.objects.select_related("virtual_chassis").filter(pk=device.pk).order_by().query.sql_with_params()
    )
    page_read = False
    changed = False

    def leave_chassis():
        close_old_connections()
        try:
            with transaction.atomic():
                VirtualChassis.objects.filter(pk=device.virtual_chassis_id).update(master=None)
                Device.objects.filter(pk=device.pk).update(virtual_chassis=None, vc_position=None)
        finally:
            connection.close()

    with bounded_executor(max_workers=1) as executor:

        def change_between_policy_reads(execute, sql, params, many, context):
            nonlocal page_read, changed
            if sql.startswith(page_query) and tuple(params) == tuple(page_params):
                page_read = True
            elif (
                page_read
                and not changed
                and '"dcim_manufacturer"' in sql
                and '"dcim_device"."virtual_chassis_id" = %s' in sql
            ):
                # Commit on another connection after the page read and before its member read.
                executor.submit(leave_chassis).result(timeout=10)
                changed = True
            return execute(sql, params, many, context)

        with connection.execute_wrapper(change_between_policy_reads):
            if endpoint == "cached-page":
                response = client.get(
                    reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[device.pk]),
                    {"tab": "modules", "server_key": SERVER_KEY},
                )
            elif endpoint == "refresh":
                response = _refresh_modules(client, device)
            else:
                response = _verify_module(
                    client,
                    refreshed,
                    device,
                    {"device_id": device.pk, "ent_physical_index": 1, "server_key": SERVER_KEY},
                )

    assert changed, "the competing transaction did not reach the policy read"
    if endpoint == "verify":
        assert response.status_code == 404
        assert response.json() == {"status": "error", "message": "No cached inventory data"}
    else:
        assert response.status_code == 200
        assert response.context["module_sync"]["table"] is None
        assert "inventory_binding" not in response.content.decode()
        if endpoint == "refresh":
            from django.core.cache import cache

            from netbox_librenms_plugin.sync_cache import SyncCacheConsistency, SyncTab, SyncTabState

            assert "Inventory data refreshed successfully." not in response.content.decode()
            state = cache.get(SyncCacheConsistency(device).state_key(SyncTab.MODULES, SERVER_KEY))
            assert state["state"] == SyncTabState.REFRESH_FAILED.value
