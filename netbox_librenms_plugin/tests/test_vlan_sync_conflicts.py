"""End-to-end checks for confirmation-required VLAN synchronization changes."""

import pytest

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
    make_superuser,
)


def _seed_vlan_snapshot(device, rows, server_key="default"):
    """Store VLAN source rows under the real synchronization cache key."""
    from django.core.cache import cache

    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    view = SyncVLANsView()
    cache.set(view.get_cache_key(device, "vlans", server_key), rows, timeout=60)


def _sync_url(device):
    """Return the public VLAN synchronization endpoint for a device."""
    from django.urls import reverse

    return reverse(
        "plugins:netbox_librenms_plugin:sync_selected_vlans",
        kwargs={"object_type": "device", "object_id": device.pk},
    )


@pytest.mark.django_db
def test_selected_grouped_vlan_requires_confirmation_before_rename(client, settings):
    """A selected VID must not silently replace an existing VLAN name in its group."""
    from ipam.models import VLAN, VLANGroup

    configure_default_librenms_server(settings)
    device = make_device("vlan-conflict-device", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Conflict group", slug="conflict-group")
    existing = VLAN.objects.create(vid=100, group=group, name="Current name", status="active")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 100, "vlan_name": "Proposed name"}])
    client.force_login(make_superuser("vlan-conflict-user"))

    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "select": "100",
            "vlan_group_100": str(group.pk),
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert b"Confirm VLAN changes" in response.content
    assert b"Current name" in response.content
    assert b"Proposed name" in response.content
    assert group.name.encode() in response.content
    existing.refresh_from_db()
    assert existing.name == "Current name"


@pytest.mark.django_db
def test_confirmed_grouped_vlan_rename_applies_the_disclosed_name(client, settings):
    """A valid confirmation must rename only the VLAN named by its signed intent."""
    from ipam.models import VLAN, VLANGroup

    configure_default_librenms_server(settings)
    device = make_device("vlan-confirm-device", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Confirm group", slug="confirm-group")
    existing = VLAN.objects.create(vid=101, group=group, name="Current name", status="active")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 101, "vlan_name": "Confirmed name"}])
    client.force_login(make_superuser("vlan-confirm-user"))
    url = _sync_url(device)

    conflict_response = client.post(
        url,
        {
            "server_key": "default",
            "action": "create_vlans",
            "select": "101",
            "vlan_group_101": str(group.pk),
        },
        HTTP_HX_REQUEST="true",
    )
    conflict = conflict_response.context["conflicts"][0]

    response = client.post(
        url,
        {
            "server_key": "default",
            "action": "create_vlans",
            "confirm_conflicts": "1",
            "force_conflict": "101",
            "select": "101",
            "vlan_group_101": str(group.pk),
            "conflict_intent": conflict["intent"],
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"]
    existing.refresh_from_db()
    assert existing.name == "Confirmed name"


@pytest.mark.django_db
def test_stale_vlan_confirmation_fails_closed(client, settings):
    """A confirmation must not replace a VLAN name changed after disclosure."""
    from ipam.models import VLAN, VLANGroup

    configure_default_librenms_server(settings)
    device = make_device("vlan-stale-device", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Stale group", slug="stale-group")
    existing = VLAN.objects.create(vid=102, group=group, name="Original name", status="active")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 102, "vlan_name": "Proposed name"}])
    client.force_login(make_superuser("vlan-stale-user"))
    url = _sync_url(device)

    conflict_response = client.post(
        url,
        {
            "server_key": "default",
            "action": "create_vlans",
            "select": "102",
            "vlan_group_102": str(group.pk),
        },
        HTTP_HX_REQUEST="true",
    )
    conflict = conflict_response.context["conflicts"][0]
    VLAN.objects.filter(pk=existing.pk).update(name="Concurrent name")

    response = client.post(
        url,
        {
            "server_key": "default",
            "action": "create_vlans",
            "confirm_conflicts": "1",
            "force_conflict": "102",
            "select": "102",
            "vlan_group_102": str(group.pk),
            "conflict_intent": conflict["intent"],
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"]
    assert any("changed after confirmation" in str(message) for message in response.wsgi_request._messages)
    existing.refresh_from_db()
    assert existing.name == "Concurrent name"


@pytest.mark.django_db
def test_tampered_vlan_confirmation_syncs_nothing(client, settings):
    """An invalid intent must not fall back to the unsigned row fields."""
    from ipam.models import VLAN, VLANGroup

    configure_default_librenms_server(settings)
    device = make_device("vlan-tampered-device", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Tampered group", slug="tampered-group")
    existing = VLAN.objects.create(vid=103, group=group, name="Current name", status="active")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 103, "vlan_name": "Proposed name"}])
    client.force_login(make_superuser("vlan-tampered-user"))

    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "confirm_conflicts": "1",
            "force_conflict": "103",
            "select": "103",
            "vlan_group_103": str(group.pk),
            "conflict_intent": "invalid-token",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"]
    assert [str(message) for message in response.wsgi_request._messages] == [
        "VLAN confirmation is invalid or has expired. Refresh the VLAN data and try again."
    ]
    existing.refresh_from_db()
    assert existing.name == "Current name"


@pytest.mark.django_db
def test_native_vlan_conflict_renders_a_complete_page(client, settings):
    """A native conflict response must render a full page around the confirmation form."""
    from ipam.models import VLAN, VLANGroup

    configure_default_librenms_server(settings)
    device = make_device("vlan-native-device", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Native group", slug="native-group")
    existing = VLAN.objects.create(vid=104, group=group, name="Current name", status="active")
    _seed_vlan_snapshot(device, [{"vlan_vlan": 104, "vlan_name": "Proposed name"}])
    client.force_login(make_superuser("vlan-native-user"))

    response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "select": "104",
            "vlan_group_104": str(group.pk),
        },
    )

    assert response.status_code == 200
    assert any(template.name == "netbox_librenms_plugin/vlan_conflicts_page.html" for template in response.templates)
    assert b"Confirm VLAN changes" in response.content
    existing.refresh_from_db()
    assert existing.name == "Current name"


@pytest.mark.django_db
def test_bulk_vlan_sync_commits_safe_rows_before_confirmation(
    client,
    settings,
    django_capture_on_commit_callbacks,
):
    """A safe row must commit while a conflicting rename remains pending."""
    from django.core.cache import cache
    from ipam.models import VLAN, VLANGroup

    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    configure_default_librenms_server(settings)
    device = make_device("vlan-bulk-device", librenms_cf={"default": {"id": 42}})
    group = VLANGroup.objects.create(name="Bulk group", slug="bulk-group")
    existing = VLAN.objects.create(vid=105, group=group, name="Current name", status="active")
    snapshot = [
        {"vlan_vlan": 105, "vlan_name": "Proposed name"},
        {"vlan_vlan": 106, "vlan_name": "Safe name"},
    ]
    _seed_vlan_snapshot(device, snapshot)
    client.force_login(make_superuser("vlan-bulk-user"))

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            _sync_url(device),
            {
                "server_key": "default",
                "action": "create_vlans",
                "select": ["105", "106"],
                "vlan_group_105": str(group.pk),
                "vlan_group_106": str(group.pk),
            },
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert b"Confirm VLAN changes" in response.content
    assert VLAN.objects.filter(vid=106, group=group, name="Safe name").exists()
    existing.refresh_from_db()
    assert existing.name == "Current name"
    assert cache.get(SyncVLANsView().get_cache_key(device, "vlans", "default")) == snapshot

    conflict = response.context["conflicts"][0]
    confirm_response = client.post(
        _sync_url(device),
        {
            "server_key": "default",
            "action": "create_vlans",
            "force_all": "1",
            "conflict_intent": conflict["intent"],
        },
        HTTP_HX_REQUEST="true",
    )

    assert confirm_response.status_code == 200
    assert confirm_response.headers["HX-Redirect"]
    existing.refresh_from_db()
    assert existing.name == "Proposed name"
