"""Behavioral coverage for migration endpoint sync-page claims."""

from copy import deepcopy

import pytest
from dcim.models import Device, Interface
from django.apps import apps as django_apps
from django.urls import reverse
from ipam.models import IPAddress

from netbox_librenms_plugin.sync_cache import SyncTab
from netbox_librenms_plugin.tests.cache_test_helpers import seed_every_tab, snapshot_state
from netbox_librenms_plugin.tests.conftest import ip_on, make_device, make_interface
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms
from netbox_librenms_plugin.utils import mark_librenms_migrated

SERVER_KEY = "primary"


def _configure_server(settings):
    """Configure the mapped server used by the migration requests."""
    plugin_config = deepcopy(settings.PLUGINS_CONFIG)
    plugin_config["netbox_librenms_plugin"]["servers"] = {
        SERVER_KEY: {
            "librenms_url": "https://librenms.example.com",
            "api_token": "test-token",
        }
    }
    settings.PLUGINS_CONFIG = plugin_config


def _mark_migrated(donor, winner):
    """Persist the donor's migration marker for the configured server."""
    mark_librenms_migrated(donor, winner.pk, SERVER_KEY)
    donor.save(update_fields=["custom_field_data"])


def _assert_source_survives_and_other_tabs_are_cleared(device, source_tab):
    """Assert the cache transition preserves only the active source snapshot."""
    state = snapshot_state(device, SERVER_KEY)
    assert state[source_tab] is True
    assert not [tab for tab, present in state.items() if tab != source_tab and present]


def _assert_every_tab_survives(device):
    """Assert an unrelated device retains every seeded snapshot."""
    state = snapshot_state(device, SERVER_KEY)
    assert state
    assert all(state.values())


@pytest.mark.django_db(
    transaction=True,
    available_apps=tuple(app.name for app in django_apps.get_app_configs()),
)
def test_interface_move_preserves_source_snapshots_for_donor_and_winner(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """An interface move preserves both devices' interface snapshots."""
    _configure_server(settings)
    donor = make_device("claim-interface-donor", librenms_cf={SERVER_KEY: {"id": 1}})
    winner = make_device("claim-interface-winner", librenms_cf={SERVER_KEY: {"id": 2}})
    unrelated = make_device("claim-interface-unrelated", librenms_cf={SERVER_KEY: {"id": 3}})
    _mark_migrated(donor, winner)

    interface = make_interface(donor, "Ethernet1", iface_type="1000base-t")

    for device in (donor, winner, unrelated):
        seed_every_tab(device, SERVER_KEY)
    assert snapshot_state(donor, SERVER_KEY)[SyncTab.INTERFACES] is True
    assert snapshot_state(winner, SERVER_KEY)[SyncTab.INTERFACES] is True

    user = make_user_with_perms(
        "claim-interface-user",
        [("change", Interface), ("change", Device)],
    )
    client.force_login(user)
    url = reverse("plugins:netbox_librenms_plugin:interface_move_to_winner", args=[interface.pk])

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"server_key": SERVER_KEY}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    assert response.headers.get("HX-Refresh") == "true"
    interface.refresh_from_db()
    assert interface.device_id == winner.pk
    _assert_source_survives_and_other_tabs_are_cleared(
        donor,
        SyncTab.INTERFACES,
    )
    _assert_source_survives_and_other_tabs_are_cleared(
        winner,
        SyncTab.INTERFACES,
    )
    _assert_every_tab_survives(unrelated)


@pytest.mark.django_db(
    transaction=True,
    available_apps=tuple(app.name for app in django_apps.get_app_configs()),
)
def test_ip_address_move_preserves_source_snapshots_for_donor_and_winner(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """An IP address move preserves both devices' IP address snapshots."""
    _configure_server(settings)
    donor = make_device("claim-ip-donor", librenms_cf={SERVER_KEY: {"id": 4}})
    winner = make_device("claim-ip-winner", librenms_cf={SERVER_KEY: {"id": 5}})
    unrelated = make_device("claim-ip-unrelated", librenms_cf={SERVER_KEY: {"id": 6}})
    _mark_migrated(donor, winner)

    address = ip_on(donor, "198.18.0.10/24", "Ethernet10")
    make_interface(winner, address.assigned_object.name, iface_type="1000base-t")

    for device in (donor, winner, unrelated):
        seed_every_tab(device, SERVER_KEY)
    assert snapshot_state(donor, SERVER_KEY)[SyncTab.IP_ADDRESSES] is True
    assert snapshot_state(winner, SERVER_KEY)[SyncTab.IP_ADDRESSES] is True

    user = make_user_with_perms(
        "claim-ip-user",
        [("change", IPAddress), ("change", Device)],
    )
    client.force_login(user)
    url = reverse("plugins:netbox_librenms_plugin:ipaddress_move_to_winner", args=[address.pk])

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(url, {"server_key": SERVER_KEY}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    assert response.headers.get("HX-Refresh") == "true"
    address.refresh_from_db()
    assert address.assigned_object.device_id == winner.pk
    _assert_source_survives_and_other_tabs_are_cleared(
        donor,
        SyncTab.IP_ADDRESSES,
    )
    _assert_source_survives_and_other_tabs_are_cleared(
        winner,
        SyncTab.IP_ADDRESSES,
    )
    _assert_every_tab_survives(unrelated)
