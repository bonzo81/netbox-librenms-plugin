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
from netbox_librenms_plugin.tests.conftest import ip_on, make_device, make_interface, make_ip


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


@pytest.mark.django_db
class TestMoveInterfaceToWinnerView:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        view = MoveInterfaceToWinnerView()
        # The plugin-write + object-perm gate is exercised separately in
        # test_perm_gate_short_circuits; null it here so each test drives the real
        # move logic against real rows.
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    @staticmethod
    def _mark(donor, winner):
        """Write a real ``_migrated_to`` marker so the view's _resolve_winner_for_donor()
        reads it from the donor's librenms_id custom field for real."""
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

    def test_rejects_when_donor_has_no_marker(self):
        # Real donor with no migration marker → the move is refused and the interface
        # stays put. No queryset patching: the marker read runs against the real cf.
        view = self._setup_view()
        donor = make_device("mi-nomark-donor")
        interface = make_interface(donor, "Eth0")
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=interface.pk)

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): HX-Reswap:none and never the success
        # HX-Refresh header (a regression that "succeeded" would set HX-Refresh=true).
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        interface.refresh_from_db()
        assert interface.device_id == donor.pk  # not moved

    def test_rejects_on_name_collision(self):
        # The winner already has a same-named interface → the real collision query finds
        # it and the move is refused; the donor interface is not reassigned.
        view = self._setup_view()
        donor = make_device("mi-coll-donor")
        winner = make_device("mi-coll-winner")
        self._mark(donor, winner)
        interface = make_interface(donor, "Eth0")
        make_interface(winner, "Eth0")  # collision on the winner side
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=interface.pk)

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        interface.refresh_from_db()
        assert interface.device_id == donor.pk  # stayed on the donor

    def test_happy_path_reassigns_device_and_returns_hx_refresh(self):
        # Real end-to-end move: the interface row's device FK is reassigned to the winner
        # through the view's full_clean()+save() path, and the reload proves it persisted.
        view = self._setup_view()
        donor = make_device("mi-happy-donor")
        winner = make_device("mi-happy-winner")
        self._mark(donor, winner)
        interface = make_interface(donor, "Eth0")
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=interface.pk)

        assert resp.status_code == 200
        assert resp.headers.get("HX-Refresh") == "true"
        interface.refresh_from_db()
        assert interface.device_id == winner.pk  # really moved to the winner

    def test_cross_device_relationship_rejected_with_409(self):
        """Real cross-device LAG: the donor interface is a member of a LAG that lives on the
        donor. Moving only the member to the winner would leave its ``lag`` on the donor —
        NetBox's real Interface.clean() rejects that, so the move aborts and nothing persists.
        (Previously this injected the ValidationError via a mock side_effect; now the actual
        validator runs.)"""
        view = self._setup_view()
        donor = make_device("mi-xdev-donor")
        winner = make_device("mi-xdev-winner")
        self._mark(donor, winner)
        donor_lag = make_interface(donor, "Po1", iface_type="lag")
        member = make_interface(donor, "Eth0")
        member.lag = donor_lag  # member's LAG lives on the donor
        member.save()
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=member.pk)

        # full_clean() failed for real → row not saved, error surfaced via the OOB toast.
        assert b"django-messages" in resp.content
        assert resp.status_code == 200
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        member.refresh_from_db()
        assert member.device_id == donor.pk  # not moved
        assert member.lag_id == donor_lag.pk  # relationship untouched

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


@pytest.mark.django_db
class TestTransferDeviceIPView:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import TransferDeviceIPView

        view = TransferDeviceIPView()
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    @staticmethod
    def _mark(donor, winner):
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

    def test_unknown_ip_kind_rejected(self):
        # Pure guard clause before any DB work: an unknown ip_kind is rejected outright,
        # so a bare request (no device needed) is sufficient.
        view = self._setup_view()
        req = _hx_request()
        resp = view.post(req, pk=10, ip_kind="bogus")
        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None

    def test_rejects_when_winner_already_has_field(self):
        # Real rows: both donor and winner already have a primary IPv4 → the transfer is
        # refused (the winner slot is occupied) and neither device's FK is touched.
        view = self._setup_view()
        donor = make_device("tx-occ-donor")
        winner = make_device("tx-occ-winner")
        self._mark(donor, winner)
        donor_ip = ip_on(donor, "10.0.0.1/24", "mgmt0")
        winner_ip = ip_on(winner, "10.0.0.99/24", "mgmt0")
        donor.primary_ip4 = donor_ip
        donor.save()
        winner.primary_ip4 = winner_ip
        winner.save()
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=donor.pk, ip_kind="primary4")

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.primary_ip4_id == donor_ip.pk  # donor FK intact
        assert winner.primary_ip4_id == winner_ip.pk  # winner FK intact

    def test_happy_path_transfers_oob_ip(self):
        """Real unique-constraint ordering: ``Device.oob_ip`` is UNIQUE per address, so the
        donor must release the FK before the winner claims it. The address already lives on a
        winner-owned interface (NetBox requires oob_ip on one of the device's own interfaces);
        the donor's FK dangles at it after a prior interface move. Driving real rows exercises
        the actual constraint — a reversed save order would trip it for real."""
        view = self._setup_view()
        donor = make_device("tx-happy-donor")
        winner = make_device("tx-happy-winner")
        self._mark(donor, winner)
        # Address sits on a WINNER interface; donor.oob_ip dangles at it (save skips clean()).
        oob_ip = ip_on(winner, "10.0.0.5/24", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=donor.pk, ip_kind="oob")

        assert resp.headers.get("HX-Refresh") == "true"
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert winner.oob_ip_id == oob_ip.pk  # winner claimed it
        assert donor.oob_ip_id is None  # donor released it

    def test_rejects_when_address_still_attached_to_donor(self):
        """The transfer only flips the FK (save skips full_clean), so it must refuse to point
        the winner at an address still assigned to a DONOR interface — otherwise the winner
        would own an oob_ip that isn't on one of its interfaces. Driven against real rows:
        the owning interface's real device_id is what the view checks."""
        view = self._setup_view()
        donor = make_device("tx-attached-donor")
        winner = make_device("tx-attached-winner")
        self._mark(donor, winner)
        # Address sits on a DONOR interface (device_id == donor), so the transfer must refuse.
        oob_ip = ip_on(donor, "10.0.0.5/24", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=donor.pk, ip_kind="oob")

        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert winner.oob_ip_id is None  # winner never claimed the donor-attached address
        assert donor.oob_ip_id == oob_ip.pk  # donor FK untouched

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


@pytest.mark.django_db
class TestMoveIPAddressToWinnerView:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import MoveIPAddressToWinnerView

        view = MoveIPAddressToWinnerView()
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    @staticmethod
    def _mark(donor, winner):
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

    def test_rejects_when_ip_not_assigned_to_interface(self):
        # A real unassigned IP picked from the donor sync page → no donor relationship to
        # migrate, so the move is refused and the IP's assignment stays empty.
        view = self._setup_view()
        ip = make_ip("10.0.0.1/24")  # unassigned
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=ip.pk)

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        ip.refresh_from_db()
        assert ip.assigned_object is None

    def test_rejects_when_winner_lacks_same_name_interface(self):
        # The IP is on a donor interface whose name does NOT exist on the winner → the real
        # same-name lookup finds nothing, so the move is refused and the IP stays on the donor.
        view = self._setup_view()
        donor = make_device("mip-noname-donor")
        winner = make_device("mip-noname-winner")  # no matching interface
        self._mark(donor, winner)
        donor_iface = make_interface(donor, "Eth0")
        ip = make_ip("10.0.0.1/24", assigned_object=donor_iface)
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=ip.pk)

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        ip.refresh_from_db()
        assert ip.assigned_object == donor_iface  # stayed on the donor interface

    def test_happy_path_reassigns_ip_to_winner_interface(self):
        """Real DB end-to-end: the IP on the donor's interface is reassigned to the winner's
        same-named interface. Because the move is driven against real rows, the donor re-lock
        ``filter(pk=..., device=donor)`` and the winner lookup ``filter(device=winner,
        name=...)`` must use the correct device/name — a wrong lookup would leave the IP off
        the winner's interface and the reload assertion would fail (no mock-call inspection
        needed to pin the exact kwargs)."""
        view = self._setup_view()

        donor = make_device("donor-device")
        winner = make_device("winner-device")
        self._mark(donor, winner)

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


@pytest.mark.django_db
class TestMigratedTransferIpDeviceOnlyGate:
    """librenms_sync_base.html serves both Devices and VMs, but the migrated-donor transfer-IP
    buttons all POST to ``device_transfer_ip`` with ``pk=object.pk`` (a Device lookup). The gate
    ``object|meta:"model_name" == "device"`` keeps those buttons off VM pages so a VM can't drive
    a mutation against a same-pk Device. build_migrated_context() doesn't itself gate on Device,
    so a VM with a stale ``_migrated_to`` marker would otherwise reach the buttons.

    The full template extends generic/object.html (not cheaply renderable in isolation), so this
    exercises the exact gate predicate from the template via the real ``meta`` filter and real
    ORM objects.
    """

    GATE = "{% load helpers %}{% if object|meta:'model_name' == 'device' %}TRANSFER{% endif %}"

    def _render(self, obj):
        from django.template import engines

        return engines["django"].from_string(self.GATE).render({"object": obj})

    def test_device_page_renders_transfer_block(self):
        device = make_device("transfer-gate-dev")
        assert self._render(device) == "TRANSFER"

    def test_vm_page_omits_transfer_block(self):
        from netbox_librenms_plugin.tests.conftest import make_vm

        vm = make_vm("transfer-gate-vm")
        assert self._render(vm) == ""
