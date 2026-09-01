"""Integration tests for moving migrated donor resources to their winner."""

import pytest
from dcim.models import CableTermination, Device
from django.urls import reverse
from virtualization.models import VMInterface

from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    ip_on,
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms
from netbox_librenms_plugin.utils import (
    build_migrated_context,
    get_migrated_to_marker,
    mark_librenms_migrated,
    set_device_ip_fk,
)
from netbox_librenms_plugin.views.sync.migrate import (
    _parse_marker_winner_pk,
    _reconcile_donor_device_ip_fks,
    _resolve_winner_for_donor,
    _safe_referer,
    _server_key_from_request,
    _sync_tab_url,
)


SERVER_KEY = "default"


def _login(client, username):
    client.force_login(make_superuser(username))


def _mark(donor, winner, server_key=SERVER_KEY):
    mark_librenms_migrated(donor, winner.pk, server_key)
    donor.save(update_fields=["custom_field_data"])


def _move_interface(client, interface, *, server_key=SERVER_KEY):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:interface_move_to_winner", args=[interface.pk]),
        {"server_key": server_key},
        HTTP_HX_REQUEST="true",
    )


def _move_ip(client, address, *, server_key=SERVER_KEY):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:ipaddress_move_to_winner", args=[address.pk]),
        {"server_key": server_key},
        HTTP_HX_REQUEST="true",
    )


def _transfer_ip(client, donor, kind, *, server_key=SERVER_KEY):
    return client.post(
        reverse("plugins:netbox_librenms_plugin:device_transfer_ip", args=[donor.pk, kind]),
        {"server_key": server_key},
        HTTP_HX_REQUEST="true",
    )


@pytest.mark.django_db
class TestMigrationMarkerContract:
    def test_marker_round_trip_resolves_the_real_winner(self):
        donor = make_device("marker-donor")
        winner = make_device("marker-winner")

        _mark(donor, winner)
        donor.refresh_from_db()

        marker = get_migrated_to_marker(donor, SERVER_KEY)
        assert marker["device_id"] == winner.pk
        assert marker["server_key"] == SERVER_KEY
        resolved, resolved_marker = _resolve_winner_for_donor(donor, SERVER_KEY)
        assert resolved == winner
        assert resolved_marker == marker

    @pytest.mark.parametrize(
        "candidate",
        [None, True, False, 0, -1, "", " 1 ", "+1", "1.0", "1e2", object()],
    )
    def test_marker_winner_parser_rejects_non_positive_or_numeric_like_values(self, candidate):
        assert _parse_marker_winner_pk(candidate) is None

    @pytest.mark.parametrize("candidate", [1, "1", 42, "42"])
    def test_marker_winner_parser_accepts_positive_integer_shapes(self, candidate):
        assert _parse_marker_winner_pk(candidate) == int(candidate)

    def test_deleted_winner_is_distinct_from_missing_marker(self):
        donor = make_device("marker-deleted-donor")
        winner = make_device("marker-deleted-winner")
        _mark(donor, winner)
        marker = get_migrated_to_marker(donor, SERVER_KEY)
        Device.objects.filter(pk=winner.pk).delete()
        donor.refresh_from_db()

        resolved, stale_marker = _resolve_winner_for_donor(donor, SERVER_KEY)

        assert resolved is None
        assert stale_marker == marker

    def test_self_pointing_marker_never_enters_migrated_mode(self):
        donor = make_device("marker-self-donor")
        _mark(donor, donor)
        donor.refresh_from_db()

        winner, marker = _resolve_winner_for_donor(donor, SERVER_KEY)
        context = build_migrated_context(donor, SERVER_KEY)

        assert winner is None
        assert marker is not None
        assert context["migrated_to_marker"] is None
        assert context["migrated_to_winner"] is None

    def test_marker_scope_does_not_leak_between_servers(self):
        donor = make_device("marker-scope-donor")
        winner = make_device("marker-scope-winner")
        _mark(donor, winner, "secondary")
        donor.refresh_from_db()

        assert get_migrated_to_marker(donor, SERVER_KEY) is None
        assert get_migrated_to_marker(donor, "secondary")["device_id"] == winner.pk


@pytest.mark.django_db
class TestMoveInterfaceToWinner:
    def test_real_interface_move_persists_and_requests_refresh(self, client):
        donor = make_device("interface-move-donor")
        winner = make_device("interface-move-winner")
        _mark(donor, winner)
        interface = make_interface(donor, "Ethernet1")
        _login(client, "interface-move-user")

        response = _move_interface(client, interface)

        assert response.status_code == 200
        assert response.headers["HX-Refresh"] == "true"
        interface.refresh_from_db()
        assert interface.device == winner

    def test_unmarked_donor_is_rejected_without_moving(self, client):
        donor = make_device("interface-unmarked-donor")
        interface = make_interface(donor, "Ethernet1")
        _login(client, "interface-unmarked-user")

        response = _move_interface(client, interface)

        assert response.status_code == 200
        assert response.headers["HX-Reswap"] == "none"
        assert "HX-Refresh" not in response.headers
        interface.refresh_from_db()
        assert interface.device == donor

    def test_winner_name_collision_rejects_the_move(self, client):
        donor = make_device("interface-collision-donor")
        winner = make_device("interface-collision-winner")
        _mark(donor, winner)
        interface = make_interface(donor, "Ethernet1")
        make_interface(winner, "Ethernet1")
        _login(client, "interface-collision-user")

        response = _move_interface(client, interface)

        assert response.status_code == 200
        assert b"already has an interface named" in response.content
        interface.refresh_from_db()
        assert interface.device == donor

    def test_lag_move_carries_its_donor_side_members(self, client):
        donor = make_device("interface-lag-donor")
        winner = make_device("interface-lag-winner")
        _mark(donor, winner)
        lag = make_interface(donor, "Port-Channel1", iface_type="lag")
        member = make_interface(donor, "Ethernet1")
        member.lag = lag
        member.save(update_fields=["lag"])
        _login(client, "interface-lag-user")

        response = _move_interface(client, lag)

        assert response.headers["HX-Refresh"] == "true"
        lag.refresh_from_db()
        member.refresh_from_db()
        assert lag.device == winner
        assert member.device == winner
        assert member.lag == lag

    def test_parent_move_carries_children_and_bridged_interfaces(self, client):
        donor = make_device("interface-parent-donor")
        winner = make_device("interface-parent-winner")
        _mark(donor, winner)
        parent = make_interface(donor, "Ethernet1")
        child = make_interface(donor, "Ethernet1.100", iface_type="virtual")
        child.parent = parent
        child.save(update_fields=["parent"])
        bridged = make_interface(donor, "Ethernet2")
        bridged.bridge = parent
        bridged.save(update_fields=["bridge"])
        _login(client, "interface-parent-user")

        response = _move_interface(client, parent)

        assert response.headers["HX-Refresh"] == "true"
        for interface in (parent, child, bridged):
            interface.refresh_from_db()
            assert interface.device == winner
        assert child.parent == parent
        assert bridged.bridge == parent

    def test_dependent_name_collision_rolls_back_the_whole_family(self, client):
        donor = make_device("interface-family-collision-donor")
        winner = make_device("interface-family-collision-winner")
        _mark(donor, winner)
        lag = make_interface(donor, "Port-Channel1", iface_type="lag")
        member = make_interface(donor, "Ethernet1")
        member.lag = lag
        member.save(update_fields=["lag"])
        make_interface(winner, member.name)
        _login(client, "interface-family-collision-user")

        response = _move_interface(client, lag)

        assert response.headers["HX-Reswap"] == "none"
        lag.refresh_from_db()
        member.refresh_from_db()
        assert lag.device == donor
        assert member.device == donor

    def test_cable_termination_device_cache_follows_interface_move(self, client):
        donor = make_device("interface-cable-donor")
        winner = make_device("interface-cable-winner")
        peer = make_device("interface-cable-peer")
        _mark(donor, winner)
        interface = make_interface(donor, "Ethernet1")
        peer_interface = make_interface(peer, "Ethernet1")
        cable_together(interface, peer_interface)
        _login(client, "interface-cable-user")

        response = _move_interface(client, interface)

        assert response.headers["HX-Refresh"] == "true"
        termination = CableTermination.objects.get(
            termination_type__model="interface",
            termination_id=interface.pk,
        )
        assert termination._device_id == winner.pk

    def test_user_without_change_grant_cannot_move_interface(self, client):
        donor = make_device("interface-permission-donor")
        winner = make_device("interface-permission-winner")
        _mark(donor, winner)
        interface = make_interface(donor, "Ethernet1")
        client.force_login(make_user_with_perms("interface-permission-user", []))

        response = _move_interface(client, interface)

        assert response.status_code == 200
        assert "HX-Refresh" not in response.headers
        interface.refresh_from_db()
        assert interface.device == donor


@pytest.mark.django_db
class TestMoveIPAddressToWinner:
    def test_real_ip_move_uses_the_winner_interface_with_the_same_name(self, client):
        donor = make_device("ip-move-donor")
        winner = make_device("ip-move-winner")
        _mark(donor, winner)
        donor_interface = make_interface(donor, "Ethernet1")
        winner_interface = make_interface(winner, "Ethernet1")
        address = make_ip("198.18.201.10/24", assigned_object=donor_interface)
        _login(client, "ip-move-user")

        response = _move_ip(client, address)

        assert response.headers["HX-Refresh"] == "true"
        address.refresh_from_db()
        assert address.assigned_object == winner_interface

    def test_unassigned_ip_is_rejected(self, client):
        address = make_ip("198.18.202.10/24")
        _login(client, "ip-unassigned-user")

        response = _move_ip(client, address)

        assert response.headers["HX-Reswap"] == "none"
        address.refresh_from_db()
        assert address.assigned_object is None

    def test_missing_winner_interface_rejects_without_reassignment(self, client):
        donor = make_device("ip-no-target-donor")
        winner = make_device("ip-no-target-winner")
        _mark(donor, winner)
        donor_interface = make_interface(donor, "Ethernet1")
        address = make_ip("198.18.203.10/24", assigned_object=donor_interface)
        _login(client, "ip-no-target-user")

        response = _move_ip(client, address)

        assert response.headers["HX-Reswap"] == "none"
        address.refresh_from_db()
        assert address.assigned_object == donor_interface


@pytest.mark.django_db
class TestTransferDeviceIPToWinner:
    def test_oob_ip_transfers_after_donor_releases_unique_fk(self, client):
        donor = make_device("transfer-oob-donor")
        winner = make_device("transfer-oob-winner")
        _mark(donor, winner)
        address = ip_on(winner, "198.18.211.10/24", "mgmt0")
        donor.oob_ip = address
        donor.save(update_fields=["oob_ip"])
        _login(client, "transfer-oob-user")

        response = _transfer_ip(client, donor, "oob")

        assert response.headers["HX-Refresh"] == "true"
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.oob_ip_id is None
        assert winner.oob_ip_id == address.pk

    def test_occupied_winner_slot_rejects_the_transfer(self, client):
        donor = make_device("transfer-occupied-donor")
        winner = make_device("transfer-occupied-winner")
        _mark(donor, winner)
        donor_address = ip_on(donor, "198.18.212.10/24", "mgmt0")
        winner_address = ip_on(winner, "198.18.212.20/24", "mgmt0")
        donor.primary_ip4 = donor_address
        donor.save(update_fields=["primary_ip4"])
        winner.primary_ip4 = winner_address
        winner.save(update_fields=["primary_ip4"])
        _login(client, "transfer-occupied-user")

        response = _transfer_ip(client, donor, "primary4")

        assert response.headers["HX-Reswap"] == "none"
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.primary_ip4 == donor_address
        assert winner.primary_ip4 == winner_address

    def test_address_on_donor_interface_cannot_be_claimed_by_winner(self, client):
        donor = make_device("transfer-owned-donor")
        winner = make_device("transfer-owned-winner")
        _mark(donor, winner)
        address = ip_on(donor, "198.18.213.10/24", "mgmt0")
        donor.oob_ip = address
        donor.save(update_fields=["oob_ip"])
        _login(client, "transfer-owned-user")

        response = _transfer_ip(client, donor, "oob")

        assert response.headers["HX-Reswap"] == "none"
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.oob_ip == address
        assert winner.oob_ip is None

    def test_vm_interface_assignment_cannot_become_device_oob_ip(self, client):
        donor = make_device("transfer-vm-donor")
        winner = make_device("transfer-vm-winner")
        _mark(donor, winner)
        vm = make_vm("transfer-vm")
        vm_interface = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        address = make_ip("198.18.214.10/24", assigned_object=vm_interface)
        donor.oob_ip = address
        donor.save(update_fields=["oob_ip"])
        _login(client, "transfer-vm-user")

        response = _transfer_ip(client, donor, "oob")

        assert response.headers["HX-Reswap"] == "none"
        winner.refresh_from_db()
        assert winner.oob_ip is None


@pytest.mark.django_db
class TestDeviceIPReconciliation:
    def test_primary_ip_moves_with_an_interface_when_winner_slot_is_empty(self):
        donor = make_device("reconcile-transfer-donor")
        winner = make_device("reconcile-transfer-winner")
        address = ip_on(winner, "198.18.221.10/24", "Ethernet1")
        donor.primary_ip4 = address
        donor.save(update_fields=["primary_ip4"])

        notes = _reconcile_donor_device_ip_fks(donor, winner)

        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.primary_ip4 is None
        assert winner.primary_ip4 == address
        assert any("transferred" in note for note in notes)

    def test_reconciliation_ignores_address_on_unrelated_device(self):
        donor = make_device("reconcile-ignore-donor")
        winner = make_device("reconcile-ignore-winner")
        unrelated = make_device("reconcile-ignore-unrelated")
        address = ip_on(unrelated, "198.18.222.10/24", "Ethernet1")
        donor.primary_ip4 = address
        donor.save(update_fields=["primary_ip4"])

        notes = _reconcile_donor_device_ip_fks(donor, winner)

        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.primary_ip4 == address
        assert winner.primary_ip4 is None
        assert notes == []

    def test_set_device_ip_fk_enforces_interface_ownership_and_family(self):
        device = make_device("set-ip-device")
        other = make_device("set-ip-other")
        own_address = ip_on(device, "198.18.223.10/24", "Ethernet1")
        other_address = ip_on(other, "198.18.223.20/24", "Ethernet1")
        ipv6_address = ip_on(device, "2001:db8:223::10/128", "Ethernet2")

        set_device_ip_fk(device, "primary_ip4", own_address)
        device.refresh_from_db()
        assert device.primary_ip4 == own_address
        with pytest.raises(ValueError, match="assigned"):
            set_device_ip_fk(device, "oob_ip", other_address)
        with pytest.raises(ValueError, match="IPv4"):
            set_device_ip_fk(device, "primary_ip4", ipv6_address)

    def test_set_device_ip_fk_allows_clearing(self):
        device = make_device("clear-ip-device")
        address = ip_on(device, "198.18.224.10/24", "Ethernet1")
        set_device_ip_fk(device, "primary_ip4", address)

        set_device_ip_fk(device, "primary_ip4", None)

        device.refresh_from_db()
        assert device.primary_ip4 is None


@pytest.mark.django_db
class TestMigrationPresentationHelpers:
    def test_server_key_from_real_request_strips_padding_and_defaults(self):
        padded = make_request("post", {"server_key": "  production  "})
        blank = make_request("post", {"server_key": "   "})

        assert _server_key_from_request(padded) == "production"
        assert _server_key_from_request(blank, default_factory=lambda: "session") == "session"

    def test_sync_tab_url_includes_only_configured_server_key(self, live_librenms):
        configured = _sync_tab_url(7, "interfaces", SERVER_KEY)
        stale = _sync_tab_url(7, "interfaces", "removed")

        assert configured.endswith("?tab=interfaces&server_key=default")
        assert stale.endswith("?tab=interfaces")
        assert "server_key" not in stale

    def test_safe_referer_accepts_same_host_and_rejects_external_host(self):
        local = make_request("post", path="/move/", HTTP_REFERER="http://testserver/device/1/")
        external = make_request("post", path="/move/", HTTP_REFERER="https://external.example.test/device/1/")

        assert _safe_referer(local, fallback="/fallback/") == "http://testserver/device/1/"
        assert _safe_referer(external, fallback="/fallback/") == "/fallback/"

    def test_non_htmx_failure_redirect_preserves_device_tab(self, client, live_librenms):
        donor = make_device("redirect-donor")
        interface = make_interface(donor, "Ethernet1")
        _login(client, "redirect-user")

        response = client.post(
            reverse("plugins:netbox_librenms_plugin:interface_move_to_winner", args=[interface.pk]),
            {"server_key": SERVER_KEY},
        )

        assert response.status_code == 302
        assert response.url == (
            reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[donor.pk])
            + "?tab=interfaces&server_key=default"
        )


@pytest.mark.django_db
class TestMovableMigrationIPs:
    def _movable(self, donor, server_key=SERVER_KEY):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        return DeviceIPAddressTableView._movable_ips_for_migration(donor, server_key)

    def test_unmarked_device_has_no_move_candidates(self):
        donor = make_device("movable-unmarked-donor")
        ip_on(donor, "198.18.231.10/24", "Ethernet1")

        assert self._movable(donor) == []

    def test_migrated_donor_lists_only_interface_assigned_addresses(self):
        donor = make_device("movable-donor")
        winner = make_device("movable-winner")
        _mark(donor, winner)
        assigned = ip_on(donor, "198.18.232.10/24", "Ethernet1")
        make_ip("198.18.232.20/24")

        movable = self._movable(donor)

        assert [row["id"] for row in movable] == [assigned.pk]
        assert movable[0]["address"] == str(assigned.address)


@pytest.mark.django_db
def test_device_ip_fk_labels_cover_every_supported_field():
    from netbox_librenms_plugin.utils import DEVICE_IP_FK_FIELDS, DEVICE_IP_FK_LABELS

    assert set(DEVICE_IP_FK_LABELS) == set(DEVICE_IP_FK_FIELDS)
    assert set(DEVICE_IP_FK_FIELDS) == {"primary_ip4", "primary_ip6", "oob_ip"}
