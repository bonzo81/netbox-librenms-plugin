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


# ── helper: get_migrated_to_marker ────────────────────────────────────────


class TestGetMigratedToMarker:
    def test_returns_marker_when_present_and_well_formed(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = MagicMock()
        donor.cf = {
            "librenms_id": {
                "default": {"_migrated_to": {"device_id": 42, "server_key": "default", "at": "2025-01-01T00:00:00Z"}}
            }
        }
        marker = get_migrated_to_marker(donor, "default")
        assert marker == {"device_id": 42, "server_key": "default", "at": "2025-01-01T00:00:00Z"}

    def test_returns_none_when_no_cf(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = MagicMock()
        donor.cf = {}
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_when_server_key_missing(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = MagicMock()
        donor.cf = {"librenms_id": {"primary": {"_migrated_to": {"device_id": 42}}}}
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_when_marker_lacks_device_id(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = MagicMock()
        donor.cf = {"librenms_id": {"default": {"_migrated_to": {"server_key": "default"}}}}
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_when_legacy_bare_int(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = MagicMock()
        donor.cf = {"librenms_id": 42}
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_for_none_device(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        assert get_migrated_to_marker(None, "default") is None

    def test_returns_none_when_device_id_is_bool(self):
        # bool is a subclass of int; a marker with device_id=True must not be
        # treated as a valid pk (would otherwise map to device #1).
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = MagicMock()
        donor.cf = {"librenms_id": {"default": {"_migrated_to": {"device_id": True, "server_key": "default"}}}}
        assert get_migrated_to_marker(donor, "default") is None

    def test_returns_none_when_device_id_not_positive(self):
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        for bad in (0, -5):
            donor = MagicMock()
            donor.cf = {"librenms_id": {"default": {"_migrated_to": {"device_id": bad, "server_key": "default"}}}}
            assert get_migrated_to_marker(donor, "default") is None


class TestBuildMigratedContext:
    def test_no_marker_returns_none_pair_without_db_hit(self):
        from netbox_librenms_plugin.utils import build_migrated_context

        donor = MagicMock()
        donor.cf = {}
        # A MagicMock would let any attribute query through; assert no Device lookup.
        with patch("dcim.models.Device") as mock_device:
            ctx = build_migrated_context(donor, "default")
        assert ctx == {"migrated_to_marker": None, "migrated_to_winner": None}
        mock_device.objects.filter.assert_not_called()

    def test_marker_present_resolves_winner(self):
        from netbox_librenms_plugin.utils import build_migrated_context

        donor = MagicMock()
        donor.cf = {"librenms_id": {"default": {"_migrated_to": {"device_id": 42, "server_key": "default"}}}}
        winner = MagicMock(pk=42)
        with patch("dcim.models.Device") as mock_device:
            mock_device.objects.filter.return_value.first.return_value = winner
            ctx = build_migrated_context(donor, "default")
        assert ctx["migrated_to_marker"]["device_id"] == 42
        assert ctx["migrated_to_winner"] is winner


# ── helper: _resolve_winner_for_donor ─────────────────────────────────────


class TestResolveWinnerForDonor:
    def test_returns_winner_and_marker_when_both_exist(self):
        from netbox_librenms_plugin.views.sync.migrate import _resolve_winner_for_donor

        donor = MagicMock()
        donor.cf = {"librenms_id": {"default": {"_migrated_to": {"device_id": 42, "server_key": "default", "at": "x"}}}}
        winner = MagicMock(pk=42)

        with patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device:
            mock_device.objects.filter.return_value.first.return_value = winner
            result_winner, result_marker = _resolve_winner_for_donor(donor, "default")

        assert result_winner is winner
        assert result_marker["device_id"] == 42

    def test_returns_none_winner_when_winner_deleted(self):
        from netbox_librenms_plugin.views.sync.migrate import _resolve_winner_for_donor

        donor = MagicMock()
        donor.cf = {"librenms_id": {"default": {"_migrated_to": {"device_id": 42, "server_key": "default", "at": "x"}}}}

        with patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device:
            mock_device.objects.filter.return_value.first.return_value = None
            winner, marker = _resolve_winner_for_donor(donor, "default")

        assert winner is None
        assert marker["device_id"] == 42

    def test_returns_none_when_no_marker(self):
        from netbox_librenms_plugin.views.sync.migrate import _resolve_winner_for_donor

        donor = MagicMock()
        donor.cf = {}
        winner, marker = _resolve_winner_for_donor(donor, "default")
        assert winner is None
        assert marker is None


# ── MoveInterfaceToWinnerView ─────────────────────────────────────────────


def _hx_request(post=None):
    """Build an HTMX request."""
    req = MagicMock()
    post = post or {}
    req.POST = MagicMock()
    req.POST.get = lambda k, d=None: post.get(k, d)
    req.headers = {"HX-Request": "true"}
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

    def test_happy_path_transfers_oob_ip(self):
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        oob_ip = MagicMock(address="10.0.0.5/24")
        # The address must already belong to the winner for the transfer to be valid
        # (NetBox requires oob_ip on one of the device's own interfaces). The owning
        # interface is re-locked, so the locked row carries the winner's device_id.
        oob_ip.assigned_object = MagicMock(pk=99)
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
            patch("netbox_librenms_plugin.views.sync.migrate.Interface") as mock_iface_cls,
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
            mock_iface_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = locked_iface
            resp = view.post(req, pk=10, ip_kind="oob")
        # The locked rows are the ones mutated and saved.
        assert locked_donor.oob_ip is None
        assert locked_winner.oob_ip is oob_ip
        # Pin the targeted FK-only save: a regression to a full save() would run
        # full_clean() and reject the merge over pre-existing device inconsistencies.
        locked_winner.save.assert_called_once_with(update_fields=["oob_ip"])
        locked_donor.save.assert_called_once_with(update_fields=["oob_ip"])
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

        oob_ip = MagicMock(address="10.0.0.5/24")
        oob_ip.assigned_object = MagicMock(pk=99)
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
            patch("netbox_librenms_plugin.views.sync.migrate.Interface") as mock_iface_cls,
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
            mock_iface_cls.objects.select_for_update.return_value.filter.return_value.first.return_value = locked_iface
            resp = view.post(req, pk=10, ip_kind="oob")

        # Refused (409 surfaced via toast); neither side mutated/saved.
        assert b"django-messages" in resp.content
        assert locked_winner.oob_ip is None
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

    def test_happy_path_reassigns_ip_to_winner_interface(self):
        from dcim.models import Device, Interface
        from ipam.models import IPAddress as IPAddressModel

        from netbox_librenms_plugin.views.sync.migrate import MoveIPAddressToWinnerView

        view = MoveIPAddressToWinnerView()
        view.require_all_permissions = MagicMock(return_value=None)

        req = _hx_request({"server_key": "default"})

        donor = MagicMock(pk=10, name="donor-device")
        winner = MagicMock(pk=20, name="winner-device")
        donor_iface = MagicMock(spec=Interface)
        donor_iface.name = "Eth0"
        donor_iface.device = donor
        donor_iface.device_id = 10
        winner_iface = MagicMock(spec=Interface, name="Eth0")
        winner_iface.name = "Eth0"

        ip = MagicMock(pk=7, address="10.0.0.1/24")
        ip.assigned_object = donor_iface

        locked_donor = MagicMock(pk=10, name="donor-device")
        locked_winner = MagicMock(pk=20, name="winner-device")

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=ip),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch.object(Interface, "objects") as mock_iface_objects,
            patch.object(Device, "objects") as mock_device_objects,
            patch.object(IPAddressModel, "objects") as mock_ip_objects,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction") as mock_tx,
        ):
            mock_iface_objects.filter.return_value.exists.return_value = True

            # Distinguish the donor re-lock (filter(pk=..., device=donor)) from the
            # winner lookup (filter(device=winner, name=...)) so the test fails if the
            # donor-side re-lock is dropped — both previously returned winner_iface.
            def iface_sfu_filter(*args, **kwargs):
                qs = MagicMock()
                qs.first.return_value = winner_iface if "name" in kwargs else donor_iface
                return qs

            mock_iface_objects.select_for_update.return_value.filter.side_effect = iface_sfu_filter

            locked_list = [locked_donor, locked_winner]
            mock_device_objects.select_for_update.return_value.filter.return_value.order_by.return_value = locked_list

            locked_ip = MagicMock(pk=7, address="10.0.0.1/24")
            locked_ip.assigned_object = donor_iface
            mock_ip_objects.select_for_update.return_value.filter.return_value.first.return_value = locked_ip

            mock_tx.atomic.return_value.__enter__ = MagicMock(return_value=None)
            mock_tx.atomic.return_value.__exit__ = MagicMock(return_value=False)

            resp = view.post(req, pk=7)

        assert locked_ip.assigned_object is winner_iface
        locked_ip.save.assert_called_once_with(update_fields=["assigned_object_type", "assigned_object_id"])
        assert resp.status_code == 200
        # Both the donor re-lock and the winner lookup must have run under the lock.
        sfu_filter_calls = mock_iface_objects.select_for_update.return_value.filter.call_args_list
        assert any("pk" in c.kwargs for c in sfu_filter_calls)  # donor re-lock
        assert any("name" in c.kwargs for c in sfu_filter_calls)  # winner lookup
