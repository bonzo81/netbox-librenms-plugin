"""Real-DB e2e: a VLAN whose selected group no longer exists must not report as 'unchanged'.

SyncVLANsView._handle_create_vlans fails closed when a per-row ``vlan_group_{vid}`` points at a
missing group: it skips that VID (never creating it in the wrong scope) and emits its own error.
Previously that skip was folded into the shared ``skipped_count`` and surfaced as "N unchanged" in a
*success* message — implying the VID synced fine. This drives the real view end-to-end (real Device,
real cache, real message framework, real DB) to pin that the group-missing skip is counted and
messaged separately and never claims success.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache as real_cache
from django.test import RequestFactory


def _make_device(name):
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name="VLM-Mfr", slug="vlm-mfr")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="VLM-DT", slug="vlm-dt")
    role, _ = DeviceRole.objects.get_or_create(name="VLM-Role", slug="vlm-role")
    site, _ = Site.objects.get_or_create(name="VLM-Site", slug="vlm-site")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site, status="active")


def _post_request(device_pk, vid, group_pk):
    request = RequestFactory().post(
        "/sync/vlans/",
        data={
            "action": "create_vlans",
            "select": [str(vid)],
            f"vlan_group_{vid}": str(group_pk),
            "server_key": "default",
        },
    )
    request.user = get_user_model().objects.create_user(username="vlm-user", password="x", is_superuser=True)
    request.session = {}
    setattr(request, "_messages", FallbackStorage(request))
    return request


def _drive_sync(device, vid, group_pk):
    from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

    view = SyncVLANsView()
    request = _post_request(device.pk, vid, group_pk)
    view.request = request  # dispatch() normally wires this; we call post() directly
    # Seed the cache the exact way the view reads it (server_key "default" is posted, so the lazy
    # librenms_api property is never built — no LibreNMS boundary is touched).
    cache_key = view.get_cache_key(device, "vlans", "default")
    real_cache.set(cache_key, [{"vlan_vlan": vid, "vlan_name": "Management"}], timeout=60)
    try:
        view.post(request, object_type="device", object_id=device.pk)
    finally:
        real_cache.delete(cache_key)
    return [(m.level_tag, m.message) for m in request._messages]


@pytest.mark.django_db
class TestVlanSyncGroupMissing:
    def test_missing_group_not_reported_as_unchanged(self):
        from ipam.models import VLAN

        device = _make_device("vlm-dev-1")
        before = VLAN.objects.count()

        # 2**31 - 1: a pk no VLANGroup will ever have in a fresh test DB.
        messages = _drive_sync(device, vid=10, group_pk=2_147_483_647)

        joined = " || ".join(msg for _, msg in messages)
        # The per-VID fail-closed error is shown...
        assert any("no longer exists" in msg for _, msg in messages), joined
        # ...the skip is attributed to the missing group, NOT to "unchanged"...
        assert "VLAN group missing" in joined
        assert "unchanged" not in joined
        # ...and nothing claims a successful sync.
        assert not any(level == "success" for level, _ in messages), joined
        # ...and no VLAN was created in the wrong (global) scope.
        assert VLAN.objects.count() == before

    def test_valid_global_vlan_still_syncs(self):
        """Positive control: a row with no group selection still creates a real global VLAN and reports success."""
        from ipam.models import VLAN

        from netbox_librenms_plugin.views.sync.vlans import SyncVLANsView

        device = _make_device("vlm-dev-2")
        view = SyncVLANsView()
        request = RequestFactory().post(
            "/sync/vlans/",
            data={"action": "create_vlans", "select": ["20"], "server_key": "default"},
        )
        request.user = get_user_model().objects.create_user(username="vlm-user2", password="x", is_superuser=True)
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))
        view.request = request  # dispatch() normally wires this; we call post() directly

        cache_key = view.get_cache_key(device, "vlans", "default")
        real_cache.set(cache_key, [{"vlan_vlan": 20, "vlan_name": "Users"}], timeout=60)
        try:
            view.post(request, object_type="device", object_id=device.pk)
        finally:
            real_cache.delete(cache_key)

        messages = [(m.level_tag, m.message) for m in request._messages]
        assert any(level == "success" and "created" in msg for level, msg in messages), messages
        assert VLAN.objects.filter(vid=20, group__isnull=True).exists()
