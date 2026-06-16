"""
Tests for Stage 2b: get_migrated_to_marker helper + the per-row
"Move to winner" view endpoints (MoveInterfaceToWinnerView,
MoveIPAddressToWinnerView, TransferDeviceIPView).

The view tests use light MagicMock-based plumbing — we patch the model
queryset chain so the views never touch a real database.  This is
appropriate because the views' job is glue: validate the marker, look
up the winner, run a small ORM mutation, and return an HTMX response.
"""

from unittest.mock import MagicMock, patch

import pytest

# Shared real-DB builders (see tests/conftest.py).
from netbox_librenms_plugin.tests.conftest import ip_on, make_device


# ── helper: get_migrated_to_marker ────────────────────────────────────────


def _make_migrate_device(name, librenms_cf=None):
    """Positional-arg adapter over the shared ``make_device`` builder.

    The marker/winner helpers read the librenms_id custom field through NetBox's ``device.cf``
    accessor and resolve the winner via ``Device.objects`` — so a real device with a real CF
    exercises the actual read + ORM lookup. This thin wrapper just preserves the positional
    ``librenms_cf`` call style used throughout this module.
    """
    return make_device(name, librenms_cf=librenms_cf)


@pytest.mark.django_db
class TestGetMigratedToMarker:
    def test_returns_marker_when_present_and_well_formed(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        marker_data = {"device_id": 42, "server_key": "default", "at": "2025-01-01T00:00:00Z"}
        donor = _make_migrate_device("mig-donor", {"default": {"_migrated_to": marker_data}})
        marker = get_migrated_to_marker(donor, "default")
        assert marker == marker_data

    def test_returns_none_when_no_cf(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = _make_migrate_device("mig-no-cf")
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_when_server_key_missing(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = _make_migrate_device("mig-wrong-key", {"primary": {"_migrated_to": {"device_id": 42}}})
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_when_marker_lacks_device_id(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = _make_migrate_device("mig-no-devid", {"default": {"_migrated_to": {"server_key": "default"}}})
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_when_legacy_bare_int(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = _make_migrate_device("mig-bare-int", 42)
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_for_none_device(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        assert get_migrated_to_marker(None, "default") is None

    def test_returns_none_when_device_id_is_bool(self):
        # bool is a subclass of int; a marker with device_id=True must not be
        # treated as a valid pk (would otherwise map to device #1).
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = _make_migrate_device(
            "mig-bool", {"default": {"_migrated_to": {"device_id": True, "server_key": "default"}}}
        )
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_when_device_id_not_positive(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        for i, bad in enumerate((0, -5)):
            donor = _make_migrate_device(
                f"mig-nonpos-{i}", {"default": {"_migrated_to": {"device_id": bad, "server_key": "default"}}}
            )
            assert get_migrated_to_marker(donor, "default") is None


@pytest.mark.django_db
class TestBuildMigratedContext:
    def test_no_marker_returns_none_pair(self):
        from netbox_librenms_plugin.utils import build_migrated_context

        donor = _make_migrate_device("mig-ctx-nomarker")
        ctx = build_migrated_context(donor, "default")
        # No marker → the winner lookup is short-circuited (None pair), no winner resolved.
        assert ctx == {"migrated_to_marker": None, "migrated_to_winner": None}

    def test_marker_present_resolves_winner(self):
        from netbox_librenms_plugin.utils import build_migrated_context

        winner = _make_migrate_device("mig-ctx-winner")
        donor = _make_migrate_device(
            "mig-ctx-donor",
            {"default": {"_migrated_to": {"device_id": winner.pk, "server_key": "default"}}},
        )
        ctx = build_migrated_context(donor, "default")
        assert ctx["migrated_to_marker"]["device_id"] == winner.pk
        assert ctx["migrated_to_winner"].pk == winner.pk


# ── helper: _resolve_winner_for_donor ─────────────────────────────────────


@pytest.mark.django_db
class TestResolveWinnerForDonor:
    def test_returns_winner_and_marker_when_both_exist(self):
        from netbox_librenms_plugin.views.sync.migrate import _resolve_winner_for_donor

        winner = _make_migrate_device("mig-rw-winner")
        donor = _make_migrate_device(
            "mig-rw-donor",
            {"default": {"_migrated_to": {"device_id": winner.pk, "server_key": "default", "at": "x"}}},
        )

        result_winner, result_marker = _resolve_winner_for_donor(donor, "default")

        assert result_winner.pk == winner.pk
        assert result_marker["device_id"] == winner.pk

    def test_returns_none_winner_when_winner_deleted(self):
        from netbox_librenms_plugin.views.sync.migrate import _resolve_winner_for_donor

        # Create then delete a winner so the marker references a now-missing pk.
        winner = _make_migrate_device("mig-rw-gone")
        gone_pk = winner.pk
        donor = _make_migrate_device(
            "mig-rw-donor2",
            {"default": {"_migrated_to": {"device_id": gone_pk, "server_key": "default", "at": "x"}}},
        )
        type(winner).objects.filter(pk=gone_pk).delete()

        winner_result, marker = _resolve_winner_for_donor(donor, "default")

        assert winner_result is None
        assert marker["device_id"] == gone_pk

    def test_returns_none_when_no_marker(self):
        from netbox_librenms_plugin.views.sync.migrate import _resolve_winner_for_donor

        donor = _make_migrate_device("mig-rw-nomarker")
        winner, marker = _resolve_winner_for_donor(donor, "default")
        assert winner is None
        assert marker is None

    def test_self_pointing_marker_returns_none_winner(self):
        """A marker pointing back at the donor (winner.pk == donor.pk) is corrupt — it would
        make the move operate on one Device as both donor and winner. Fail closed as stale."""
        from netbox_librenms_plugin.views.sync.migrate import _resolve_winner_for_donor

        donor = _make_migrate_device("mig-rw-self")
        # Point the marker at the donor's own pk.
        donor.custom_field_data["librenms_id"] = {
            "default": {"_migrated_to": {"device_id": donor.pk, "server_key": "default", "at": "x"}}
        }
        donor.save()

        winner, marker = _resolve_winner_for_donor(donor, "default")

        assert winner is None
        assert marker["device_id"] == donor.pk


# ── MoveInterfaceToWinnerView ─────────────────────────────────────────────


def _hx_request(post=None):
    """Build an HTMX request."""
    req = MagicMock()
    post = post or {}
    req.POST = MagicMock()
    req.POST.get = lambda k, d=None: post.get(k, d)
    req.headers = {"HX-Request": "true"}
    req.htmx = True  # pin branch intent: implementations may check `if request.htmx`
    req.META = {"HTTP_REFERER": "/back"}
    req.user = MagicMock(is_superuser=True)
    return req


class TestMoveInterfaceToWinnerView:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        view = MoveInterfaceToWinnerView()
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_rejects_when_donor_has_no_marker(self):
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        donor = MagicMock(pk=10, cf={})
        interface = MagicMock(pk=5, name="Eth0", device=donor)

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=interface),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(None, None),
            ),
        ):
            resp = view.post(req, pk=5)

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): the HTMX response carries HX-Reswap:none and
        # never the success HX-Refresh header, so a regression that returned HX-Refresh=true
        # (refreshing as if the move/transfer succeeded) would be caught here.
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None

    def test_rejects_on_name_collision(self):
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        donor = MagicMock(pk=10)
        winner = MagicMock(pk=20, name="winner")
        interface = MagicMock(pk=5, name="Eth0", device=donor)

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=interface),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch("netbox_librenms_plugin.views.sync.migrate.Interface") as mock_iface_cls,
        ):
            mock_iface_cls.objects.filter.return_value.exists.return_value = True
            resp = view.post(req, pk=5)

        # Lock in the actual collision query (winner device + same interface name).
        mock_iface_cls.objects.filter.assert_called_with(device=winner, name=interface.name)
        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): the HTMX response carries HX-Reswap:none and
        # never the success HX-Refresh header, so a regression that returned HX-Refresh=true
        # (refreshing as if the move/transfer succeeded) would be caught here.
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        # Prove the collision reject moved/persisted nothing before the toast: a regression
        # that reassigned the interface to the winner (then errored) would still 200 here.
        assert interface.device is donor
        interface.save.assert_not_called()
        winner.save.assert_not_called()

    def test_happy_path_reassigns_device_and_returns_hx_refresh(self):
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        donor = MagicMock(pk=10)
        winner = MagicMock(pk=20, name="winner")
        interface = MagicMock(pk=5, name="Eth0", device=donor)
        # The donor interface re-read under the row lock — distinct from the
        # pre-lock instance, with an explicit (non-MagicMock-name) name so the
        # collision re-check is asserted against the *locked* name.
        locked_iface = MagicMock(pk=5, device=donor)
        locked_iface.name = "Eth0"

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=interface),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch("netbox_librenms_plugin.views.sync.migrate.Interface") as mock_iface_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction"),
            patch("netbox_librenms_plugin.views.sync.migrate.messages"),
        ):
            # All objects.filter() calls are collision probes now: the move mutates and
            # saves the locked interface rather than running a filter(pk=...).update().
            collision_qs = MagicMock()
            collision_qs.exists.return_value = False
            mock_iface_cls.objects.filter.return_value = collision_qs
            mock_iface_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = locked_iface
            # Locked device rows (donor + winner) are now captured and re-verified
            # against the migration marker under the lock.
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.order_by.return_value = [
                donor,
                winner,
            ]
            resp = view.post(req, pk=5)

        # Donor interface is locked + re-read by (pk, device=donor) before the move.
        mock_iface_cls.objects.select_for_update.return_value.filter.assert_called_with(pk=interface.pk, device=donor)
        # Collision re-check uses the LOCKED interface name on the winner device.
        mock_iface_cls.objects.filter.assert_any_call(device=winner, name=locked_iface.name)
        # The move runs Interface.clean() via full_clean()+save() on the LOCKED row — a
        # bare .update() would bypass the cross-device parent/lag/bridge validation.
        assert locked_iface.device is winner
        locked_iface.full_clean.assert_called_once()
        locked_iface.save.assert_called_once()
        # The pre-lock instance is never the one persisted.
        interface.save.assert_not_called()
        assert resp.headers.get("HX-Refresh") == "true"

    def test_cross_device_relationship_rejected_with_409(self):
        """If the move would leave the interface with a parent/lag/bridge on the donor
        (a cross-device link), full_clean() raises and the move is rejected, not saved."""
        from django.core.exceptions import ValidationError

        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        donor = MagicMock(pk=10)
        winner = MagicMock(pk=20)
        winner.name = "winner"
        interface = MagicMock(pk=5, device=donor)
        interface.name = "Eth0"
        locked_iface = MagicMock(pk=5, device=donor)
        locked_iface.name = "Eth0"
        locked_iface.full_clean.side_effect = ValidationError({"lag": ["Must belong to the same device."]})

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=interface),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch("netbox_librenms_plugin.views.sync.migrate.Interface") as mock_iface_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction"),
            patch("netbox_librenms_plugin.views.sync.migrate.messages"),
        ):
            collision_qs = MagicMock()
            collision_qs.exists.return_value = False
            mock_iface_cls.objects.filter.return_value = collision_qs
            mock_iface_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = locked_iface
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.order_by.return_value = [
                donor,
                winner,
            ]
            resp = view.post(req, pk=5)

        # clean() failed → row not saved, error surfaced via the OOB toast (HTMX path).
        # HTMX errors are signalled by the OOB toast at HTTP 200 (see _fail docstring),
        # not by the status code; assert the *absence* of the success HX-Refresh header
        # so this stays distinct from the success response, which sets HX-Refresh=true.
        locked_iface.full_clean.assert_called_once()
        locked_iface.save.assert_not_called()
        assert b"django-messages" in resp.content
        # Error contract (see _fail): HTMX errors are an OOB toast at HTTP 200 with
        # HX-Reswap:none and no HX-Refresh — never the success refresh response.
        assert resp.status_code == 200
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None

    def test_save_integrity_error_rejected_with_409(self):
        """A concurrent winner-side rename/create can trip a unique constraint at save()
        time (after our name re-check); surface it as a 409, not a 500."""
        from django.db import IntegrityError

        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        donor = MagicMock(pk=10)
        winner = MagicMock(pk=20)
        winner.name = "winner"
        interface = MagicMock(pk=5, device=donor)
        interface.name = "Eth0"
        locked_iface = MagicMock(pk=5, device=donor)
        locked_iface.name = "Eth0"
        locked_iface.full_clean.return_value = None
        locked_iface.save.side_effect = IntegrityError("duplicate key value")

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=interface),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch("netbox_librenms_plugin.views.sync.migrate.Interface") as mock_iface_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction"),
            patch("netbox_librenms_plugin.views.sync.migrate.messages"),
        ):
            collision_qs = MagicMock()
            collision_qs.exists.return_value = False
            mock_iface_cls.objects.filter.return_value = collision_qs
            mock_iface_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = locked_iface
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.order_by.return_value = [
                donor,
                winner,
            ]
            resp = view.post(req, pk=5)

        # save() raised → surfaced as the collision OOB toast (HTMX path), not a 500.
        # As above: HTMX errors are an OOB toast at HTTP 200, distinguished from the
        # success response by the absence of the HX-Refresh header.
        locked_iface.save.assert_called_once()
        assert b"django-messages" in resp.content
        # Error contract (see _fail): HTMX errors are an OOB toast at HTTP 200 with
        # HX-Reswap:none and no HX-Refresh — never the success refresh response.
        assert resp.status_code == 200
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None

    def test_marker_repointed_under_lock_is_rejected(self):
        """If the donor's _migrated_to is repointed between the unlocked resolve
        and acquiring the row locks, the move must abort instead of targeting the
        stale winner."""
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        donor = MagicMock(pk=10)
        winner = MagicMock(pk=20)
        winner.name = "winner"
        other_winner = MagicMock(pk=99)  # marker now points here
        interface = MagicMock(pk=5, device=donor)
        interface.name = "Eth0"

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=interface),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                # 1st call (pre-lock) → winner; 2nd call (under lock) → a different winner.
                side_effect=[
                    (winner, {"device_id": 20, "server_key": "default", "at": "x"}),
                    (other_winner, {"device_id": 99, "server_key": "default", "at": "y"}),
                ],
            ),
            patch("netbox_librenms_plugin.views.sync.migrate.Interface") as mock_iface_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction"),
            patch("netbox_librenms_plugin.views.sync.migrate.messages"),
        ):
            mock_iface_cls.objects.filter.return_value.exists.return_value = False
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.order_by.return_value = [
                donor,
                winner,
            ]
            resp = view.post(req, pk=5)

        # Aborts with a conflict toast; the move query is never issued.
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): the HTMX response carries HX-Reswap:none and
        # never the success HX-Refresh header, so a regression that returned HX-Refresh=true
        # (refreshing as if the move/transfer succeeded) would be caught here.
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        mock_iface_cls.objects.select_for_update.assert_not_called()

    def test_perm_gate_short_circuits(self):
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        view = MoveInterfaceToWinnerView()
        view.require_all_permissions = MagicMock(return_value=HttpResponse(status=403))
        req = _hx_request()
        resp = view.post(req, pk=5)
        assert resp.status_code == 403


# ── TransferDeviceIPView ──────────────────────────────────────────────────


class TestTransferDeviceIPView:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import TransferDeviceIPView

        view = TransferDeviceIPView()
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_unknown_ip_kind_rejected(self):
        view = self._setup_view()
        req = _hx_request()
        resp = view.post(req, pk=10, ip_kind="bogus")
        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): the HTMX response carries HX-Reswap:none and
        # never the success HX-Refresh header, so a regression that returned HX-Refresh=true
        # (refreshing as if the move/transfer succeeded) would be caught here.
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None

    def test_rejects_when_winner_already_has_field(self):
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        donor_ip = MagicMock(address="10.0.0.1/24")
        donor = MagicMock(pk=10, primary_ip4=donor_ip)
        winner = MagicMock(pk=20, name="winner", primary_ip4=MagicMock(address="10.0.0.99/24"))

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=donor),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
        ):
            resp = view.post(req, pk=10, ip_kind="primary4")

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): the HTMX response carries HX-Reswap:none and
        # never the success HX-Refresh header, so a regression that returned HX-Refresh=true
        # (refreshing as if the move/transfer succeeded) would be caught here.
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        # Prove the reject left the donor's IP field intact and persisted neither device:
        # a regression that cleared/moved primary_ip4 before the toast would still 200 here.
        assert donor.primary_ip4 is donor_ip
        donor.save.assert_not_called()
        winner.save.assert_not_called()

    def test_happy_path_transfers_oob_ip(self):
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        from dcim.models import Interface as InterfaceModel

        oob_ip = MagicMock(address="10.0.0.5/24")
        # The address must already belong to the winner for the transfer to be valid
        # (NetBox requires oob_ip on one of the device's own interfaces). The owning
        # interface is re-locked, so the locked row carries the winner's device_id.
        # spec=Interface so the view's isinstance(assigned, Interface) guard accepts it.
        oob_ip.assigned_object = MagicMock(spec=InterfaceModel)
        oob_ip.assigned_object.pk = 99
        locked_iface = MagicMock(pk=99, device_id=20)
        # Pre-lock instances drive the pre-checks only; the view must mutate the
        # *locked* rows fetched inside the transaction.
        donor = MagicMock(pk=10, oob_ip=oob_ip)
        winner = MagicMock(pk=20, name="winner", oob_ip=None)
        locked_donor = MagicMock(pk=10, oob_ip=oob_ip)
        locked_winner = MagicMock(pk=20, name="winner", oob_ip=None)

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=donor),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.IPAddress") as mock_ip_cls,
            # Patch only the manager so Interface stays a real class for the isinstance guard.
            patch("netbox_librenms_plugin.views.sync.migrate.Interface.objects") as mock_iface_objects,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction"),
            patch("netbox_librenms_plugin.views.sync.migrate.messages"),
        ):
            # Locked queryset returns DISTINCT mocks so the test fails if the view
            # mutates the stale pre-lock instances instead of the locked rows.
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.order_by.return_value = [
                locked_donor,
                locked_winner,
            ]
            # The IPAddress row is now re-fetched under select_for_update; return the same
            # logical address so its assignment is validated from the locked row.
            mock_ip_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = oob_ip
            # The owning interface is re-locked; return a row owned by the winner (device_id=20).
            mock_iface_objects.select_for_update.return_value.filter.return_value.first.return_value = locked_iface
            # device oob_ip/primary_ip FKs are UNIQUE per address — the donor must release the FK
            # before the winner claims it. Capture save() order to pin that ordering.
            save_order = MagicMock()
            save_order.attach_mock(locked_donor.save, "donor_save")
            save_order.attach_mock(locked_winner.save, "winner_save")
            resp = view.post(req, pk=10, ip_kind="oob")
        # The locked rows are the ones mutated and saved.
        assert locked_donor.oob_ip is None
        assert locked_winner.oob_ip is oob_ip
        # Pin the targeted FK-only save: a regression to a full save() would run
        # full_clean() and reject the merge over pre-existing device inconsistencies.
        locked_winner.save.assert_called_once_with(update_fields=["oob_ip"])
        locked_donor.save.assert_called_once_with(update_fields=["oob_ip"])
        # Donor save MUST precede winner save (unique-per-address FK); the reverse order would
        # trip the unique constraint with the donor still holding the address.
        _save_names = [c[0] for c in save_order.mock_calls]
        assert _save_names.index("donor_save") < _save_names.index("winner_save")
        # Stale pre-lock instances must be left untouched.
        donor.save.assert_not_called()
        winner.save.assert_not_called()
        assert resp.headers.get("HX-Refresh") == "true"

    def test_rejects_when_address_still_attached_to_donor(self):
        """The transfer only flips the FK (save skips full_clean), so it must refuse to
        point the winner at an address still assigned to a donor interface — otherwise the
        winner would own an oob_ip/primary_ip that isn't on one of its interfaces."""
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        from dcim.models import Interface as InterfaceModel

        oob_ip = MagicMock(address="10.0.0.5/24")
        oob_ip.assigned_object = MagicMock(spec=InterfaceModel)
        oob_ip.assigned_object.pk = 99
        locked_iface = MagicMock(pk=99, device_id=10)  # still on the DONOR, not the winner
        donor = MagicMock(pk=10, oob_ip=oob_ip)
        winner = MagicMock(pk=20, name="winner", oob_ip=None)
        locked_donor = MagicMock(pk=10, oob_ip=oob_ip)
        locked_winner = MagicMock(pk=20, name="winner", oob_ip=None)

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=donor),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.IPAddress") as mock_ip_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.Interface.objects") as mock_iface_objects,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction"),
            patch("netbox_librenms_plugin.views.sync.migrate.messages"),
        ):
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.order_by.return_value = [
                locked_donor,
                locked_winner,
            ]
            # Locked re-fetch returns the address still assigned to the donor (device_id=10).
            mock_ip_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = oob_ip
            # The owning interface re-locks to a row still owned by the donor (device_id=10).
            mock_iface_objects.select_for_update.return_value.filter.return_value.first.return_value = locked_iface
            resp = view.post(req, pk=10, ip_kind="oob")

        # Refused (409 surfaced via toast); neither side mutated/saved.
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): the HTMX response carries HX-Reswap:none and
        # never the success HX-Refresh header, so a regression that returned HX-Refresh=true
        # (refreshing as if the move/transfer succeeded) would be caught here.
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        assert locked_winner.oob_ip is None
        locked_winner.save.assert_not_called()
        locked_donor.save.assert_not_called()

    def test_rejects_when_assignment_is_not_an_interface(self):
        """A non-Interface assignment (e.g. a VMInterface) must fail closed: the Interface
        re-lock is skipped (GenericForeignKey pks aren't unique across models, so filtering
        Interface by an unrelated pk could lock the wrong row), so the device_id check sees
        None and the transfer is refused."""
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        oob_ip = MagicMock(address="10.0.0.5/24")
        # NOT spec=Interface → isinstance(assigned, Interface) is False → re-lock skipped.
        oob_ip.assigned_object = MagicMock(pk=99)
        donor = MagicMock(pk=10, oob_ip=oob_ip)
        winner = MagicMock(pk=20, name="winner", oob_ip=None)
        locked_donor = MagicMock(pk=10, oob_ip=oob_ip)
        locked_winner = MagicMock(pk=20, name="winner", oob_ip=None)

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=donor),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.IPAddress") as mock_ip_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.Interface.objects") as mock_iface_objects,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction"),
            patch("netbox_librenms_plugin.views.sync.migrate.messages"),
        ):
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.order_by.return_value = [
                locked_donor,
                locked_winner,
            ]
            mock_ip_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = oob_ip
            resp = view.post(req, pk=10, ip_kind="oob")

        # Refused; the Interface manager was never queried (re-lock skipped), nothing saved.
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): the HTMX response carries HX-Reswap:none and
        # never the success HX-Refresh header, so a regression that returned HX-Refresh=true
        # (refreshing as if the move/transfer succeeded) would be caught here.
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        mock_iface_objects.select_for_update.assert_not_called()
        locked_winner.save.assert_not_called()
        locked_donor.save.assert_not_called()


# ── MoveIPAddressToWinnerView ────────────────────────────────────────────


class TestMoveIPAddressToWinnerView:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import MoveIPAddressToWinnerView

        view = MoveIPAddressToWinnerView()
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_rejects_when_ip_not_assigned_to_interface(self):
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        ip = MagicMock(pk=7)
        ip.assigned_object = None  # not an Interface

        with patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=ip):
            resp = view.post(req, pk=7)

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): the HTMX response carries HX-Reswap:none and
        # never the success HX-Refresh header, so a regression that returned HX-Refresh=true
        # (refreshing as if the move/transfer succeeded) would be caught here.
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None

    def test_rejects_when_winner_lacks_same_name_interface(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.migrate import MoveIPAddressToWinnerView

        view = MoveIPAddressToWinnerView()
        view.require_all_permissions = MagicMock(return_value=None)

        req = _hx_request({"server_key": "default"})

        donor = MagicMock(pk=10)
        winner = MagicMock(pk=20, name="winner")
        donor_iface = MagicMock(spec=Interface)
        donor_iface.name = "Eth0"
        donor_iface.device = donor
        ip = MagicMock(pk=7, address="10.0.0.1/24")
        ip.assigned_object = donor_iface

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=ip),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch.object(Interface, "objects") as mock_objects,
        ):
            # exists() drives the pre-check; returns False so the view rejects
            # before transaction.atomic() is entered.
            mock_objects.filter.return_value.exists.return_value = False
            resp = view.post(req, pk=7)

        # Lock in the same-name lookup (winner device + donor interface name).
        mock_objects.filter.assert_called_with(device=winner, name="Eth0")
        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): the HTMX response carries HX-Reswap:none and
        # never the success HX-Refresh header, so a regression that returned HX-Refresh=true
        # (refreshing as if the move/transfer succeeded) would be caught here.
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        # Prove the IP stayed on the donor interface and was never persisted: a regression
        # that reassigned ip.assigned_object before the missing-interface check would still
        # 200 here.
        assert ip.assigned_object is donor_iface
        ip.save.assert_not_called()

    @pytest.mark.django_db
    def test_happy_path_reassigns_ip_to_winner_interface(self):
        """Real DB end-to-end: the IP on the donor's interface is reassigned to the winner's
        same-named interface. Because the move is driven against real rows, the donor re-lock
        ``filter(pk=..., device=donor)`` and the winner lookup ``filter(device=winner,
        name=...)`` must use the correct device/name — a wrong lookup would leave the IP off
        the winner's interface and the reload assertion would fail (no mock-call inspection
        needed to pin the exact kwargs)."""
        from netbox_librenms_plugin.tests.conftest import make_interface, make_ip
        from netbox_librenms_plugin.utils import mark_librenms_migrated
        from netbox_librenms_plugin.views.sync.migrate import MoveIPAddressToWinnerView

        view = MoveIPAddressToWinnerView()
        view.require_all_permissions = MagicMock(return_value=None)

        donor = make_device("donor-device")
        winner = make_device("winner-device")
        # Mark the donor migrated into the winner so the real _resolve_winner_for_donor()
        # (which reads the _migrated_to cf marker) resolves the winner for real.
        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

        donor_iface = make_interface(donor, "Eth0")
        winner_iface = make_interface(winner, "Eth0")  # same name on the winner
        ip = make_ip("10.0.0.1/24", assigned_object=donor_iface)

        req = _hx_request({"server_key": "default"})
        resp = view.post(req, pk=ip.pk)

        assert resp.status_code == 200
        ip.refresh_from_db()
        # The IP moved onto the winner's same-named interface — proving both the donor re-lock
        # and the winner lookup resolved the correct rows.
        assert ip.assigned_object == winner_iface
        assert ip.assigned_object.device_id == winner.pk
        # The donor interface no longer holds the IP.
        assert ip.assigned_object != donor_iface


# ── non-HTMX redirect fallback (preserve tab + server_key) ────────────────────


def _nonhtmx_request(post=None, referer=None):
    """Build a plain (non-HTMX) request; no usable Referer unless *referer* given."""
    req = MagicMock()
    post = post or {}
    req.POST = MagicMock()
    req.POST.get = lambda k, d=None: post.get(k, d)
    req.headers = {}  # no HX-Request -> the degraded redirect path
    req.htmx = False  # pin branch intent: a bare MagicMock.htmx is truthy and would mis-route
    req.META = {"HTTP_REFERER": referer} if referer else {}
    req.get_host = lambda: "testserver"
    req.is_secure = lambda: False
    req.user = MagicMock(is_superuser=True)
    req._messages = MagicMock()  # so messages.error() has storage to write to
    return req


@pytest.mark.django_db
class TestReconcileDonorDeviceIpFks:
    """Real-DB coverage for _reconcile_donor_device_ip_fks: it locks the owning Interface and
    re-reads device_id from the locked row before transferring a device-level primary/OOB IP FK,
    so the winner never ends up owning an address that sits on an interface it doesn't own.

    Exercised against real NetBox models (not mocks) so the FK transfer, the Interface lookup,
    and the owner comparison are validated against actual ORM behavior end-to-end.
    """

    @staticmethod
    def _make_devices():
        # Three real devices on the shared infra (winner / donor / a third owner).
        return (
            make_device("recon-winner"),
            make_device("recon-donor"),
            make_device("recon-other"),
        )

    def test_transfers_primary_ip_when_interface_is_on_winner(self):
        """The donor's primary_ip4 dangles on an address now sitting on a winner-owned interface;
        the FK is transferred to the winner and cleared on the donor."""
        from django.db import transaction

        from netbox_librenms_plugin.views.sync.migrate import _reconcile_donor_device_ip_fks

        winner, donor, _ = self._make_devices()
        ip = ip_on(winner, "10.10.0.1/24", "eth0")
        # Dangling FK: donor still points at the moved address (save() skips clean()).
        donor.primary_ip4 = ip
        donor.save()

        with transaction.atomic():
            notes = _reconcile_donor_device_ip_fks(donor, winner)

        winner.refresh_from_db()
        donor.refresh_from_db()
        assert winner.primary_ip4_id == ip.pk
        assert donor.primary_ip4_id is None
        assert any("transferred" in n for n in notes)

    def test_skips_when_interface_belongs_to_a_different_device(self):
        """If the address sits on an interface owned by neither the winner (a stale/concurrent
        state), the locked-row device_id check rejects it: no FK is moved onto the winner."""
        from django.db import transaction

        from netbox_librenms_plugin.views.sync.migrate import _reconcile_donor_device_ip_fks

        winner, donor, other = self._make_devices()
        ip = ip_on(other, "10.10.0.2/24", "eth0")
        donor.primary_ip4 = ip
        donor.save()

        with transaction.atomic():
            notes = _reconcile_donor_device_ip_fks(donor, winner)

        winner.refresh_from_db()
        donor.refresh_from_db()
        assert winner.primary_ip4_id is None  # interface not on winner → not transferred
        # The skip path must not clear the donor's IP either — it still points at the address.
        assert donor.primary_ip4_id == ip.pk
        assert notes == []


class TestSyncTabUrl:
    """_sync_tab_url builds the donor device sync URL with tab + server_key."""

    def test_includes_tab_and_server_key(self):
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.migrate import _sync_tab_url

        base = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[7])
        assert _sync_tab_url(7, "ipaddresses", "siteB") == f"{base}?tab=ipaddresses&server_key=siteB"

    def test_quotes_server_key(self):
        from netbox_librenms_plugin.views.sync.migrate import _sync_tab_url

        assert "server_key=a+b%2Fc" in _sync_tab_url(7, "interfaces", "a b/c")

    def test_omits_server_key_when_empty(self):
        from netbox_librenms_plugin.views.sync.migrate import _sync_tab_url

        url = _sync_tab_url(7, "interfaces", "")
        assert url.endswith("?tab=interfaces")
        assert "server_key=" not in url


class TestSafeRefererFallback:
    """_safe_referer prefers a valid same-site Referer, else the server-built fallback."""

    def _req(self, referer=None):
        req = MagicMock()
        req.META = {"HTTP_REFERER": referer} if referer else {}
        req.get_host = lambda: "testserver"
        req.is_secure = lambda: False
        return req

    def test_returns_fallback_when_no_referer(self):
        from netbox_librenms_plugin.views.sync.migrate import _safe_referer

        assert _safe_referer(self._req(), fallback="/sync/?tab=interfaces") == "/sync/?tab=interfaces"

    def test_valid_same_site_referer_wins_over_fallback(self):
        from netbox_librenms_plugin.views.sync.migrate import _safe_referer

        req = self._req(referer="http://testserver/p/")
        assert _safe_referer(req, fallback="/sync/") == "http://testserver/p/"

    def test_external_referer_rejected_uses_fallback(self):
        # The open-redirect guard must still reject a cross-host Referer, then
        # land on the internal fallback rather than the attacker URL.
        from netbox_librenms_plugin.views.sync.migrate import _safe_referer

        req = self._req(referer="http://evil.example/p/")
        assert _safe_referer(req, fallback="/sync/") == "/sync/"

    def test_no_referer_no_fallback_uses_script_prefix(self):
        from django.urls import get_script_prefix

        from netbox_librenms_plugin.views.sync.migrate import _safe_referer

        assert _safe_referer(self._req(), fallback=None) == get_script_prefix()


class TestNonHtmxFallbackRedirect:
    """A plain POST with no Referer still lands on the donor sync tab + server."""

    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        view = MoveInterfaceToWinnerView()
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_missing_referer_preserves_tab_and_server_key(self):
        from django.urls import reverse

        view = self._setup_view()
        req = _nonhtmx_request({"server_key": "siteB"})
        donor = MagicMock(pk=10, cf={})
        interface = MagicMock(pk=5, name="Eth0", device=donor)

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=interface),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(None, None),
            ),
        ):
            resp = view.post(req, pk=5)

        # Degraded non-HTMX path: a real redirect carrying tab + server context,
        # not the bare app mount path.
        expected = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[10])
        assert resp.status_code in (301, 302)
        assert resp.url == f"{expected}?tab=interfaces&server_key=siteB"
