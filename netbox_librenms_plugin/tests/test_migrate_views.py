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
    """Positional-arg adapter over the shared ``make_device`` builder."""
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

    def test_returns_none_when_marker_server_key_mismatches_entry(self):
        # The marker lives under cf["default"] but its own stamped server_key says "edgelondon"
        # (malformed or copied across server sub-blocks). Reject it so a stray marker can't force
        # the donor into migrated mode for the "default" server it does not belong to.
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = _make_migrate_device(
            "mig-keymismatch",
            {"default": {"_migrated_to": {"device_id": 42, "server_key": "edgelondon"}}},
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
        """A marker pointing back at the donor (winner.pk == donor.pk) is corrupt — it would make the move operate on one Device as both donor and winner."""
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

    def test_non_positive_winner_id_classified_corrupt(self):
        """A marker with a non-positive device_id (0 / negative) is corrupt, not a deleted winner — int() succeeds but it can never be a real Device pk."""
        from netbox_librenms_plugin.views.sync.migrate import _winner_unavailable_reason

        donor = _make_migrate_device("mig-rw-nonpos")
        assert _winner_unavailable_reason(donor, {"device_id": 0}) == "corrupt"
        assert _winner_unavailable_reason(donor, {"device_id": -5}) == "corrupt"
        assert _winner_unavailable_reason(donor, {"device_id": "0"}) == "corrupt"
        # A well-formed positive id whose row is gone is still "deleted".
        assert _winner_unavailable_reason(donor, {"device_id": 999999}) == "deleted"

    def test_numeric_like_winner_id_is_corrupt_not_truncated(self):
        """int() truncates 1.9 / Decimal('1.9') / '1.9' to 1 — a corrupt marker must NOT resolve the wrong winner; both helpers must reject it."""
        from decimal import Decimal

        from netbox_librenms_plugin.views.sync.migrate import _resolve_winner_for_donor, _winner_unavailable_reason

        winner = _make_migrate_device("mig-num-winner")  # real device at some pk (e.g. truncation target)
        donor = _make_migrate_device("mig-num-donor")
        for bad in (1.9, Decimal("1.9"), "1.9", winner.pk + 0.4):
            assert _winner_unavailable_reason(donor, {"device_id": bad}) == "corrupt"
            # _resolve_winner_for_donor must fail closed (no winner), not truncate to a real pk.
            donor.custom_field_data["librenms_id"] = {
                "default": {"_migrated_to": {"device_id": bad, "server_key": "default"}}
            }
            donor.save()
            resolved, _marker = _resolve_winner_for_donor(donor, "default")
            assert resolved is None


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
        """Write a real ``_migrated_to`` marker so the view's _resolve_winner_for_donor() reads it from the donor's librenms_id custom field for real."""
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

    def test_corrupt_self_pointing_marker_reports_stale_not_deleted(self):
        """A self-pointing _migrated_to marker is corrupt state, not a deleted winner — the error must say so for recovery."""
        view = self._setup_view()
        donor = make_device("mi-corrupt-donor")
        # Marker points at the donor's own pk → corrupt (would move a device onto itself).
        donor.custom_field_data["librenms_id"] = {
            "default": {"_migrated_to": {"device_id": donor.pk, "server_key": "default", "at": "x"}}
        }
        donor.save()
        interface = make_interface(donor, "Eth0")
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=interface.pk)

        assert resp.status_code == 200
        assert b"stale or corrupt" in resp.content
        assert b"Winner device no longer exists" not in resp.content
        interface.refresh_from_db()
        assert interface.device_id == donor.pk  # not moved

    def test_deleted_winner_marker_reports_winner_gone(self):
        """A well-formed marker whose winner row was deleted still reports the winner as gone — distinct from a corrupt marker."""
        from dcim.models import Device

        view = self._setup_view()
        donor = make_device("mi-delwin-donor")
        winner = make_device("mi-delwin-winner")
        self._mark(donor, winner)
        Device.objects.filter(pk=winner.pk).delete()
        interface = make_interface(donor, "Eth0")
        req = _hx_request({"server_key": "default"})

        resp = view.post(req, pk=interface.pk)

        assert resp.status_code == 200
        assert b"Winner device no longer exists" in resp.content
        assert b"stale or corrupt" not in resp.content
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
        """Real cross-device LAG: the donor interface is a member of a LAG that lives on the donor."""
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
        """A concurrent winner-side rename/create can trip a unique constraint at save() time (after our name re-check); surface it as a 409, not a 500."""
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
        """If the donor's _migrated_to is repointed between the unlocked resolve and acquiring the row locks, the move must abort instead of targeting the stale winner."""
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
        """Real unique-constraint ordering: ``Device.oob_ip`` is UNIQUE per address, so the donor must release the FK before the winner claims it."""
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
        """The transfer only flips the FK (save skips full_clean), so it must refuse to point the winner at an address still assigned to a DONOR interface — otherwise the winner would own an oob_ip that isn't on one of its interfaces."""
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
        """A non-Interface assignment (e.g. a VMInterface) must fail closed: the device_id check sees None and the transfer is refused."""
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
        """Real DB end-to-end: the IP on the donor's interface is reassigned to the winner's same-named interface."""
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
    """Real-DB coverage for _reconcile_donor_device_ip_fks: it locks the owning Interface and re-reads device_id from the locked row before transferring a device-level primary/OOB IP FK, so the winner never ends up owning an address that sits on an interface it doesn't own."""

    @staticmethod
    def _make_devices():
        # Three real devices on the shared infra (winner / donor / a third owner).
        return (
            make_device("recon-winner"),
            make_device("recon-donor"),
            make_device("recon-other"),
        )

    def test_transfers_primary_ip_when_interface_is_on_winner(self):
        """The donor's primary_ip4 dangles on an address now sitting on a winner-owned interface; the FK is transferred to the winner and cleared on the donor."""
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
        """If the address sits on an interface owned by neither the winner (a stale/concurrent state), the locked-row device_id check rejects it: no FK is moved onto the winner."""
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


@pytest.mark.django_db
class TestSetDeviceIpFk:
    """Real-DB coverage for utils.set_device_ip_fk: the single guarded chokepoint every device primary/OOB IP-FK write (which bypass full_clean via update_fields) goes through."""

    def test_sets_and_saves_fk_when_address_on_device_interface(self):
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("sdf-owner")
        ip = ip_on(device, "10.20.0.1/24", "eth0")
        ret = set_device_ip_fk(device, "oob_ip", ip)
        assert ret == "oob_ip"
        device.refresh_from_db()
        assert device.oob_ip_id == ip.pk  # persisted

    def test_raises_and_does_not_persist_when_address_on_another_device(self):
        # The address lives on a DIFFERENT device's interface → the helper must refuse and
        # never persist the off-device FK (the bare update_fields save would have accepted it).
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("sdf-dev")
        other = make_device("sdf-other")
        ip = ip_on(other, "10.20.0.2/24", "eth0")
        with pytest.raises(ValueError, match="not assigned to an interface on that device"):
            set_device_ip_fk(device, "primary_ip4", ip)
        device.refresh_from_db()
        assert device.primary_ip4_id is None  # nothing persisted

    def test_clearing_is_always_allowed(self):
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("sdf-clear")
        ip = ip_on(device, "10.20.0.3/24", "eth0")
        device.oob_ip = ip
        device.save()
        set_device_ip_fk(device, "oob_ip", None)
        device.refresh_from_db()
        assert device.oob_ip_id is None

    def test_save_false_assigns_without_persisting(self):
        # save=False: in-memory assignment + return field, but the DB row is untouched until
        # the caller runs its own (batched) save — used where oob_ip rides a larger update_fields.
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("sdf-nosave")
        ip = ip_on(device, "10.20.0.4/24", "eth0")
        ret = set_device_ip_fk(device, "oob_ip", ip, save=False)
        assert ret == "oob_ip"
        assert device.oob_ip_id == ip.pk  # set in memory
        device.refresh_from_db()
        assert device.oob_ip_id is None  # not yet persisted

    def test_unsupported_field_raises(self):
        from netbox_librenms_plugin.utils import set_device_ip_fk

        device = make_device("sdf-badfield")
        with pytest.raises(ValueError, match="unsupported field"):
            set_device_ip_fk(device, "name", None)


class TestSyncTabUrl:
    """_sync_tab_url builds the donor device sync URL with tab + server_key."""

    def test_includes_tab_and_configured_server_key(self):
        from django.urls import reverse

        from netbox_librenms_plugin.views.sync.migrate import _sync_tab_url

        base = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[7])
        # Only reflect a server_key that matches a configured server (allowlist re-source).
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"siteB": "Site B"},
        ):
            assert _sync_tab_url(7, "ipaddresses", "siteB") == f"{base}?tab=ipaddresses&server_key=siteB"

    def test_quotes_configured_server_key(self):
        from netbox_librenms_plugin.views.sync.migrate import _sync_tab_url

        # quote_plus still applies to a configured key (defense-in-depth against odd chars).
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"a b/c": "Weird"},
        ):
            assert "server_key=a+b%2Fc" in _sync_tab_url(7, "interfaces", "a b/c")

    def test_omits_unconfigured_server_key(self):
        # A stale/tampered key that isn't a configured server is dropped, not reflected into
        # the redirect target. Mirrors device_fields._sync_redirect's allowlist guard.
        from netbox_librenms_plugin.views.sync.migrate import _sync_tab_url

        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"default": "Default Server"},
        ):
            url = _sync_tab_url(7, "interfaces", "siteB")
        assert url.endswith("?tab=interfaces")
        assert "server_key=" not in url

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
            # siteB must be a configured server for the fallback URL to reflect it (allowlist).
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"siteB": "Site B"},
            ),
        ):
            resp = view.post(req, pk=5)

        # Degraded non-HTMX path: a real redirect carrying tab + server context,
        # not the bare app mount path.
        expected = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[10])
        assert resp.status_code in (301, 302)
        assert resp.url == f"{expected}?tab=interfaces&server_key=siteB"


class TestMigratedTransferIpDeviceOnlyGate:
    """The shipped librenms_sync_base.html must gate the migrated-donor transfer-IP buttons to Devices.

    These POST to ``device_transfer_ip`` with ``pk=object.pk`` (a Device lookup), so they must not
    render on a VM page. Assert against the real template SOURCE so the test fails if the guard is
    dropped — not a hand-copied mini-template that can drift from what ships.
    """

    def _template_source(self):
        from pathlib import Path

        import netbox_librenms_plugin

        return (
            Path(netbox_librenms_plugin.__file__).parent
            / "templates"
            / "netbox_librenms_plugin"
            / "librenms_sync_base.html"
        ).read_text()

    def test_transfer_ip_buttons_are_gated_to_devices_in_shipped_template(self):
        source = self._template_source()
        assert "device_transfer_ip" in source
        # The device-only guard must open before the transfer-IP controls so a VM page can't
        # render them; if the guard is dropped, this fails.
        guard_idx = source.find('object|meta:"model_name" == "device"')
        assert guard_idx != -1, "device-only guard missing from migrated transfer block"
        assert guard_idx < source.find("device_transfer_ip"), "transfer-IP controls are not behind the device guard"


@pytest.mark.django_db
class TestVlanStaleServerMigratedContext:
    """The VLAN stale-server error path resolves migrated context under the session key, not the stale posted key."""

    def test_stale_key_resolves_migrated_context_under_session_key(self):
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        view = object.__new__(BaseVLANTableView)
        view._librenms_api = MagicMock(server_key="test-server")
        obj = MagicMock()
        view.get_object = MagicMock(return_value=obj)
        view.rebind_api_for_server = MagicMock(return_value=None)  # stale/unconfigured posted key
        view._get_error_context = MagicMock(return_value={})
        request = MagicMock()
        request.POST.get.side_effect = lambda k, d=None: {"server_key": "ghost-server"}.get(k, d)

        with (
            patch("netbox_librenms_plugin.views.base.vlan_table_view.messages"),
            patch(
                "netbox_librenms_plugin.views.base.vlan_table_view.build_migrated_context", return_value={}
            ) as mock_mig,
            patch("netbox_librenms_plugin.views.base.vlan_table_view.render", return_value="rendered"),
        ):
            result = view.post(request, pk=1)

        mock_mig.assert_called_once_with(obj, "test-server")  # session key, not "ghost-server"
        assert result == "rendered"


class TestMigratedContextServerKeyFallback:
    """When the POSTed server_key is None (malformed/stale request) and the API rebind fails, the cable/IP sync error render must still resolve the migrated marker — by falling back to the session server key — so a migrated donor's sync controls stay suppressed instead of being silently re-enabled."""

    def _post_with_server_key(self, view_cls, module_path, posted):
        from unittest.mock import MagicMock, patch

        view = object.__new__(view_cls)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "sessionkey"

        request = MagicMock()
        request.POST = posted

        obj = MagicMock()
        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "rebind_api_for_server", return_value=None),  # rebind fails
            patch(f"{module_path}.messages"),
            patch(f"{module_path}.render"),
            patch(f"{module_path}.build_migrated_context", return_value={}) as bmc,
        ):
            view.post(request, pk=1)

        bmc.assert_called_once()
        # Always resolve under the session key, never the (failed-to-rebind) POSTed key — a None
        # key would skip the marker, a non-empty stale key would look it up under the wrong server.
        assert bmc.call_args.args[1] == "sessionkey"

    def test_cables_view_empty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        self._post_with_server_key(BaseCableTableView, "netbox_librenms_plugin.views.base.cables_view", {})

    def test_cables_view_stale_nonempty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        self._post_with_server_key(
            BaseCableTableView, "netbox_librenms_plugin.views.base.cables_view", {"server_key": "ghost"}
        )

    def test_ip_view_empty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        self._post_with_server_key(BaseIPAddressTableView, "netbox_librenms_plugin.views.base.ip_addresses_view", {})

    def test_ip_view_stale_nonempty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        self._post_with_server_key(
            BaseIPAddressTableView, "netbox_librenms_plugin.views.base.ip_addresses_view", {"server_key": "ghost"}
        )

    def test_interfaces_view_empty_key_uses_session_key(self):
        # interfaces_view previously redirected on a stale key (dropping migrated context); it must
        # now render the partial with migrated context, so build_migrated_context is reached.
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        self._post_with_server_key(BaseInterfaceTableView, "netbox_librenms_plugin.views.base.interfaces_view", {})

    def test_interfaces_view_stale_nonempty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        self._post_with_server_key(
            BaseInterfaceTableView, "netbox_librenms_plugin.views.base.interfaces_view", {"server_key": "ghost"}
        )

    def test_interfaces_view_rebind_fail_avoids_lazy_api_property(self):
        """On rebind failure the migrated-context render must read the cached client key, not the lazy librenms_api property — which can re-construct a missing client and 500 the HTMX error path."""
        from unittest.mock import MagicMock, PropertyMock, patch

        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "sessionkey"
        request = MagicMock()
        request.POST = {"server_key": "ghost"}

        with (
            patch.object(view, "get_object", return_value=MagicMock()),
            patch.object(view, "rebind_api_for_server", return_value=None),  # stale key → error path
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.render"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.build_migrated_context", return_value={}) as bmc,
            # The lazy property must NOT be touched on this path; make it explode if it is.
            patch.object(
                BaseInterfaceTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.post(request, pk=1)

        bmc.assert_called_once()
        assert bmc.call_args.args[1] == "sessionkey"  # used the cached client key

    def test_vlan_view_rebind_fail_avoids_lazy_api_property(self):
        """vlan_table_view rebind-fail render must read the cached client key, not the lazy librenms_api property (which can reconstruct a missing client and 500 the HTMX error path)."""
        from unittest.mock import MagicMock, PropertyMock, patch

        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        view = object.__new__(BaseVLANTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "sessionkey"
        view._get_error_context = MagicMock(return_value={})
        request = MagicMock()
        request.POST = {"server_key": "ghost"}

        with (
            patch.object(view, "get_object", return_value=MagicMock()),
            patch.object(view, "rebind_api_for_server", return_value=None),
            patch("netbox_librenms_plugin.views.base.vlan_table_view.messages"),
            patch("netbox_librenms_plugin.views.base.vlan_table_view.render"),
            patch("netbox_librenms_plugin.views.base.vlan_table_view.build_migrated_context", return_value={}) as bmc,
            patch.object(
                BaseVLANTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.post(request, pk=1)

        bmc.assert_called_once()
        assert bmc.call_args.args[1] == "sessionkey"

    def test_modules_view_rebind_fail_avoids_lazy_api_property(self):
        """modules_view rebind-fail render must read the cached client key, not the lazy librenms_api property (which can reconstruct a missing client and 500 the HTMX error path)."""
        from unittest.mock import MagicMock, PropertyMock, patch

        from netbox_librenms_plugin.views.base.modules_view import BaseModuleTableView

        view = object.__new__(BaseModuleTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "sessionkey"
        view.has_write_permission = MagicMock(return_value=True)
        request = MagicMock()
        request.POST = {"server_key": "ghost"}

        with (
            patch.object(view, "get_object", return_value=MagicMock()),
            patch.object(view, "rebind_api_for_server", return_value=None),
            patch("netbox_librenms_plugin.views.base.modules_view.messages"),
            patch("netbox_librenms_plugin.views.base.modules_view.render"),
            patch("netbox_librenms_plugin.views.base.modules_view.build_migrated_context", return_value={}) as bmc,
            patch.object(
                BaseModuleTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.post(request, pk=1)

        bmc.assert_called_once()
        assert bmc.call_args.args[1] == "sessionkey"

    def test_cables_view_rebind_fail_avoids_lazy_api_property(self):
        """cables_view rebind-fail render must read the cached client key, not the lazy librenms_api property (which can reconstruct a missing client and 500 the HTMX error path)."""
        from unittest.mock import MagicMock, PropertyMock, patch

        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "sessionkey"
        request = MagicMock()
        request.POST = {"server_key": "ghost"}

        with (
            patch.object(view, "get_object", return_value=MagicMock()),
            patch.object(view, "rebind_api_for_server", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.messages"),
            patch("netbox_librenms_plugin.views.base.cables_view.render"),
            patch("netbox_librenms_plugin.views.base.cables_view.build_migrated_context", return_value={}) as bmc,
            patch.object(
                BaseCableTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.post(request, pk=1)

        bmc.assert_called_once()
        assert bmc.call_args.args[1] == "sessionkey"

    def test_ip_view_rebind_fail_avoids_lazy_api_property(self):
        """ip_addresses_view rebind-fail render must read the cached client key, not the lazy librenms_api property (which can reconstruct a missing client and 500 the HTMX error path)."""
        from unittest.mock import MagicMock, PropertyMock, patch

        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "sessionkey"
        request = MagicMock()
        request.POST = {"server_key": "ghost"}

        with (
            patch.object(view, "get_object", return_value=MagicMock()),
            patch.object(view, "rebind_api_for_server", return_value=None),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.messages"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.render"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.resolve_set_primary_ip", return_value=False),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.build_migrated_context", return_value={}) as bmc,
            patch.object(
                BaseIPAddressTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.post(request, pk=1)

        bmc.assert_called_once()
        assert bmc.call_args.args[1] == "sessionkey"

    def test_ip_view_stale_key_fallback_preserves_set_primary_ip(self):
        """The stale-server-key error fallback must carry set_primary_ip so the template doesn't silently uncheck the user's preference."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "sessionkey"
        request = MagicMock()
        request.POST = {"server_key": "ghost", "set-primary-ip-toggle": "on"}

        with (
            patch.object(view, "get_object", return_value=MagicMock()),
            patch.object(view, "rebind_api_for_server", return_value=None),  # rebind fails → stale-key fallback
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.messages"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.build_migrated_context", return_value={}),
        ):
            view.post(request, pk=1)

        ip_sync = mock_render.call_args.args[2]["ip_sync"]
        assert "set_primary_ip" in ip_sync  # the field was omitted before, silently unchecking the box
        assert ip_sync["set_primary_ip"] is True  # reflects the POSTed toggle

    def test_ip_view_failed_refresh_fallback_preserves_set_primary_ip(self):
        """The failed-live-refresh error fallback must also carry set_primary_ip."""
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "good"
        request = MagicMock()
        request.POST = {"server_key": "good", "set-primary-ip-toggle": "on"}

        with (
            patch.object(view, "get_object", return_value=MagicMock()),
            patch.object(view, "rebind_api_for_server", return_value="good"),  # rebind OK
            patch.object(view, "_prepare_context", return_value=None),  # live fetch failed
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.messages"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.build_migrated_context", return_value={}),
        ):
            view.post(request, pk=1)

        ip_sync = mock_render.call_args.args[2]["ip_sync"]
        assert ip_sync["set_primary_ip"] is True
