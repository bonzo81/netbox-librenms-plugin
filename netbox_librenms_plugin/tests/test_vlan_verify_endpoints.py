"""
Branch coverage for the VLAN verify and override endpoints in ``views/object_sync/devices.py``.

The primary home for these endpoints is ``test_coverage_devices.py``. These cases live in their
own file because higher branches of the PR stack grow that file's tail, so appending there
conflicts on every restack.
"""

import json

import pytest
from django.core.cache import cache
from django.urls import reverse

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_superuser,
    make_virtual_chassis_members,
)
from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms


SERVER_KEY = "default"


def _json_post(client, route_name, body):
    """POST a JSON body to one of the plugin's verify endpoints."""
    return client.post(
        reverse(f"plugins:netbox_librenms_plugin:{route_name}"),
        data=json.dumps(body),
        content_type="application/json",
    )


def _plugin_reader(username):
    """Create a real user with plugin read access only: no plugin write, no NetBox object grants."""
    from django.apps import apps
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username=username, password="x")
    settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
    return grant(user, "view", settings_model, name=f"{username}-plugin-view")


def _vlan_group(name):
    """Create a real VLANGroup."""
    from ipam.models import VLANGroup

    return VLANGroup.objects.create(name=name, slug=name.lower().replace(" ", "-"))


def _vlan(vid, name, *, group=None):
    """Create a real VLAN, optionally inside *group*."""
    from ipam.models import VLAN

    return VLAN.objects.create(vid=vid, name=name, group=group)


def _tagged_interface(device, name, vlans):
    """Create a real tagged interface carrying *vlans*."""
    interface = make_interface(device, name)
    interface.mode = "tagged"
    interface.save()
    interface.tagged_vlans.set(vlans)
    return interface


@pytest.mark.django_db
class TestSingleVlanGroupVerifyGate:
    """The endpoint checks the NetBox object permissions before it resolves the device."""

    def test_missing_object_permissions_are_reported_instead_of_the_device(self, client):
        """A plugin writer without the DCIM/IPAM view grants gets 403, not a device-scoped 404."""
        device = make_device("vlan-verify-gate")
        client.force_login(make_user_with_perms("vlan-verify-gate-user", []))

        response = _json_post(client, "verify_vlan_group", {"device_id": device.pk, "vid": "110"})

        assert response.status_code == 403
        assert response.json() == {
            "error": "Missing permissions: dcim.view_device, ipam.view_vlangroup, ipam.view_vlan"
        }


@pytest.mark.django_db
class TestSingleVlanGroupVerifyPayloadGuards:
    """Each required payload field is rejected with its own structured 400."""

    def test_missing_device_id_is_rejected(self, client):
        """A payload without device_id returns the device error, not a 500."""
        client.force_login(make_superuser("vlan-verify-no-device-user"))

        response = _json_post(client, "verify_vlan_group", {"vid": "110"})

        assert response.status_code == 400
        assert response.json() == {"status": "error", "message": "No device ID provided"}

    def test_missing_vid_is_rejected(self, client):
        """A payload with a device but no VID returns the VID error."""
        device = make_device("vlan-verify-no-vid")
        client.force_login(make_superuser("vlan-verify-no-vid-user"))

        response = _json_post(client, "verify_vlan_group", {"device_id": device.pk})

        assert response.status_code == 400
        assert response.json() == {"status": "error", "message": "No VID provided"}

    def test_non_numeric_vid_is_rejected(self, client):
        """A VID that is not an integer returns a structured 400 instead of raising."""
        device = make_device("vlan-verify-bad-vid")
        client.force_login(make_superuser("vlan-verify-bad-vid-user"))

        response = _json_post(client, "verify_vlan_group", {"device_id": device.pk, "vid": "not-a-vid"})

        assert response.status_code == 400
        assert response.json() == {"status": "error", "message": "Invalid VID"}


@pytest.mark.django_db
class TestSingleVlanGroupVerifyGlobalScope:
    """With no group selected only group-less VLANs count as available."""

    def test_a_grouped_vlan_is_missing_when_no_group_is_selected(self, client):
        """Without a VLAN group only global VLANs resolve, so a grouped VID reads as missing."""
        device = make_device("vlan-verify-global-scope")
        group = _vlan_group("Verify Campus")
        _vlan(940, "grouped-only", group=group)
        global_vlan = _vlan(950, "global-only")
        interface = make_interface(device, "Ethernet1")
        interface.mode = "access"
        interface.untagged_vlan = global_vlan
        interface.save()
        client.force_login(make_superuser("vlan-verify-global-scope-user"))

        grouped = _json_post(
            client,
            "verify_vlan_group",
            {"device_id": device.pk, "interface_name": "Ethernet1", "vid": "940", "vlan_type": "U"},
        )
        globally = _json_post(
            client,
            "verify_vlan_group",
            {"device_id": device.pk, "interface_name": "Ethernet1", "vid": "950", "vlan_type": "U"},
        )

        assert grouped.json()["is_missing"] is True
        assert grouped.json()["css_class"] == "text-danger"
        assert globally.json()["is_missing"] is False
        assert globally.json()["css_class"] == "text-success"


@pytest.mark.django_db
class TestSingleVlanGroupVerifyTaggedRows:
    """The tagged branch compares against the interface's real tagged VLANs and their groups."""

    def test_tagged_vlan_group_comparison_uses_the_netbox_vlan_group(self, client):
        """The same tagged VID matches in its own group and reads as a group mismatch in another."""
        device = make_device("vlan-verify-tagged")
        own_group = _vlan_group("Verify Tagged Own")
        other_group = _vlan_group("Verify Tagged Other")
        own_vlan = _vlan(930, "tagged-own", group=own_group)
        _vlan(930, "tagged-other", group=other_group)
        _tagged_interface(device, "Ethernet2", [own_vlan])
        client.force_login(make_superuser("vlan-verify-tagged-user"))

        same_group = _json_post(
            client,
            "verify_vlan_group",
            {
                "device_id": device.pk,
                "interface_name": "Ethernet2",
                "vid": "930",
                "vlan_type": "T",
                "vlan_group_id": own_group.pk,
            },
        )
        other = _json_post(
            client,
            "verify_vlan_group",
            {
                "device_id": device.pk,
                "interface_name": "Ethernet2",
                "vid": "930",
                "vlan_type": "T",
                "vlan_group_id": other_group.pk,
            },
        )

        assert same_group.json()["is_missing"] is False
        assert same_group.json()["css_class"] == "text-success"
        assert other.json()["is_missing"] is False
        assert other.json()["css_class"] == "text-warning"
        assert ">930(T)</span>" in same_group.json()["formatted_vlans"]

    def test_a_tagged_vlan_outside_every_group_carries_the_missing_warning(self, client):
        """A tagged VID that exists in no visible VLAN renders the missing-VLAN warning icon."""
        device = make_device("vlan-verify-tagged-missing")
        _tagged_interface(device, "Ethernet3", [])
        client.force_login(make_superuser("vlan-verify-tagged-missing-user"))

        response = _json_post(
            client,
            "verify_vlan_group",
            {"device_id": device.pk, "interface_name": "Ethernet3", "vid": "931", "vlan_type": "T"},
        )

        body = response.json()
        assert body["is_missing"] is True
        assert body["css_class"] == "text-danger"
        assert "931(T)" in body["formatted_vlans"]
        assert "mdi-alert" in body["formatted_vlans"]

    def test_a_row_that_renders_no_vlan_falls_back_to_a_dash(self, client):
        """VID 0 renders nothing, so the cell degrades to the placeholder instead of an empty string."""
        device = make_device("vlan-verify-empty-cell")
        client.force_login(make_superuser("vlan-verify-empty-cell-user"))

        response = _json_post(
            client,
            "verify_vlan_group",
            {"device_id": device.pk, "interface_name": "Ethernet1", "vid": "0", "vlan_type": "U"},
        )

        assert response.status_code == 200
        assert response.json()["formatted_vlans"] == "\u2014"


@pytest.mark.django_db
class TestVerifyVlanSyncGroupBranches:
    """The VLAN sync tab endpoint gates on IPAM permissions and validates the group id."""

    def test_missing_vlan_permissions_are_reported(self, client):
        """A plugin writer without the IPAM view grants gets 403 before any VLAN is read."""
        client.force_login(make_user_with_perms("vlan-sync-verify-gate-user", []))

        response = _json_post(client, "verify_vlan_sync_group", {"vid": "120", "name": "Users"})

        assert response.status_code == 403
        assert response.json() == {"error": "Missing permissions: ipam.view_vlan, ipam.view_vlangroup"}

    def test_non_numeric_vlan_group_id_is_rejected(self, client):
        """A VLAN group id that is not an integer returns a structured 400."""
        client.force_login(make_superuser("vlan-sync-verify-bad-group-user"))

        response = _json_post(
            client,
            "verify_vlan_sync_group",
            {"vid": "120", "name": "Users", "vlan_group_id": "not-an-id"},
        )

        assert response.status_code == 400
        assert response.json() == {"status": "error", "message": "Invalid VLAN group ID"}


@pytest.mark.django_db
class TestSaveVlanGroupOverridesBranches:
    """Persisting overrides needs plugin write access, a device id, and a resolvable cache scope."""

    def test_a_plugin_reader_cannot_persist_overrides(self, client, live_librenms):
        """Plugin read access alone is refused with 403 and writes nothing to the cache."""
        from netbox_librenms_plugin.views.object_sync.devices import SaveVlanGroupOverridesView

        device = make_device("vlan-overrides-reader", librenms_cf={SERVER_KEY: {"id": 161}})
        overrides_key = SaveVlanGroupOverridesView().get_vlan_overrides_key(device, SERVER_KEY)
        client.force_login(_plugin_reader("vlan-overrides-reader-user"))

        response = _json_post(
            client,
            "save_vlan_group_overrides",
            {"device_id": device.pk, "server_key": SERVER_KEY, "vid_group_map": {"940": "21"}},
        )

        assert response.status_code == 403
        assert cache.get(overrides_key) is None

    def test_missing_device_id_is_rejected(self, client, live_librenms):
        """A payload without device_id returns a structured 400 before the device lookup."""
        client.force_login(make_superuser("vlan-overrides-no-device-user"))

        response = _json_post(
            client,
            "save_vlan_group_overrides",
            {"server_key": SERVER_KEY, "vid_group_map": {"941": "22"}},
        )

        assert response.status_code == 400
        assert response.json() == {"status": "error", "message": "No device ID provided"}

    def test_an_unresolvable_chassis_falls_back_to_the_posted_device(self, client, live_librenms):
        """A chassis with no resolvable sync member stores the overrides under the posted device."""
        from netbox_librenms_plugin.utils import get_librenms_sync_device
        from netbox_librenms_plugin.views.object_sync.devices import SaveVlanGroupOverridesView

        _chassis, (device, _sibling) = make_virtual_chassis_members("vlan-overrides-vc")
        device.vc_position = None
        device.save(update_fields=["vc_position"])
        assert get_librenms_sync_device(device, server_key=SERVER_KEY) is None

        view = SaveVlanGroupOverridesView()
        cache.set(view.get_cache_key(device, "ports", SERVER_KEY), {"ports": []}, timeout=300)
        client.force_login(make_superuser("vlan-overrides-vc-user"))

        response = _json_post(
            client,
            "save_vlan_group_overrides",
            {"device_id": device.pk, "server_key": SERVER_KEY, "vid_group_map": {"942": "23"}},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        assert cache.get(view.get_vlan_overrides_key(device, SERVER_KEY)) == {"942": "23"}
