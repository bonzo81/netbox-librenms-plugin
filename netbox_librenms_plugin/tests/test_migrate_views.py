"""
Tests for Stage 2b: get_migrated_to_marker helper + the per-row
"Move to winner" view endpoints (MoveInterfaceToWinnerView,
MoveIPAddressToWinnerView, TransferDeviceIPView).

The view tests are real-DB end-to-end: they build real donor/winner devices,
interfaces and IPs (conftest make_device/make_interface/make_ip/ip_on), write a
real ``_migrated_to`` marker, drive the view's full select_for_update /
full_clean / save move path, and reload from the DB to prove what persisted.
MagicMock is reserved for the request object and the permission gate (exercised
separately); a handful of legacy unit tests still stub the ORM and are being
migrated to real rows.
"""

from unittest.mock import MagicMock, patch

import pytest

# Shared real-DB builders (see tests/conftest.py).
from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    ip_on,
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_vm,
)


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

    def test_blank_or_none_server_key_reads_the_default_marker(self):
        # A blank/None key means the default server everywhere else in the plugin; a reader
        # passing a raw unnormalized key must not miss the default-server marker (and flip a
        # migrated donor's UI back to ordinary sync mode).
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        marker_data = {"device_id": 42, "server_key": "default", "at": "2025-01-01T00:00:00Z"}
        donor = _make_migrate_device("mig-blank-key", {"default": {"_migrated_to": marker_data}})
        assert get_migrated_to_marker(donor, "") == marker_data
        assert get_migrated_to_marker(donor, None) == marker_data

    def test_build_migrated_context_with_blank_key_carries_the_marker(self):
        from netbox_librenms_plugin.utils import build_migrated_context

        winner = _make_migrate_device("mig-blank-ctx-winner")
        donor = _make_migrate_device(
            "mig-blank-ctx-donor",
            {
                "default": {
                    "_migrated_to": {"device_id": winner.pk, "server_key": "default", "at": "2025-01-01T00:00:00Z"}
                }
            },
        )
        ctx = build_migrated_context(donor, "")
        assert ctx["migrated_to_marker"] is not None
        assert bool(ctx["migrated_to_winner"])

    def test_mark_librenms_migrated_normalizes_blank_key_to_default(self):
        # Writer/reader pairing: stamping with a blank key must land under "default" so the
        # normalized readers find it (marker server_key stamped as "default" too).
        from netbox_librenms_plugin.utils import get_migrated_to_marker, mark_librenms_migrated

        winner = _make_migrate_device("mig-blank-write-winner")
        donor = _make_migrate_device("mig-blank-write-donor", {"default": {"id": 7}})
        mark_librenms_migrated(donor, winner.pk, "")
        donor.save()
        marker = get_migrated_to_marker(donor, "default")
        assert marker is not None
        assert marker["server_key"] == "default"
        assert marker["device_id"] == winner.pk

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

    def test_returns_self_pointing_marker_unchanged(self):
        # get_migrated_to_marker() deliberately does NOT reject a self-pointing marker: the move
        # views read it via _resolve_winner_for_donor() and need the marker present to classify it
        # as "stale/corrupt" rather than "not migrated". The self-pointing fail-closed lives in
        # build_migrated_context() (see TestBuildMigratedContext) instead.
        from netbox_librenms_plugin.utils import get_migrated_to_marker

        donor = _make_migrate_device("mig-selfpoint")
        donor.custom_field_data["librenms_id"] = {
            "default": {"_migrated_to": {"device_id": donor.pk, "server_key": "default"}}
        }
        donor.save()
        marker = get_migrated_to_marker(donor, "default")
        assert marker["device_id"] == donor.pk


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

    def test_self_pointing_marker_does_not_enter_migrated_mode(self):
        # build_migrated_context() reads get_migrated_to_marker() directly (not the move views'
        # _resolve_winner_for_donor), so a self-pointing marker must be rejected here too — otherwise
        # the donor's sync page flips into migrated mode and resolves the "winner" to itself.
        from netbox_librenms_plugin.utils import build_migrated_context

        donor = _make_migrate_device("mig-ctx-selfpoint")
        donor.custom_field_data["librenms_id"] = {
            "default": {"_migrated_to": {"device_id": donor.pk, "server_key": "default"}}
        }
        donor.save()
        ctx = build_migrated_context(donor, "default")
        assert ctx == {"migrated_to_marker": None, "migrated_to_winner": None}


# ── helper: _resolve_winner_for_donor ─────────────────────────────────────


class TestParseMarkerWinnerPk:
    """The shared marker device_id parser used by _resolve_winner_for_donor + _winner_unavailable_reason."""

    def test_accepts_int_and_digit_string(self):
        from netbox_librenms_plugin.views.sync.migrate import _parse_marker_winner_pk

        assert _parse_marker_winner_pk(5) == 5
        assert _parse_marker_winner_pk("5") == 5

    def test_rejects_bool_numeric_like_and_non_positive(self):
        from netbox_librenms_plugin.views.sync.migrate import _parse_marker_winner_pk

        for bad in (True, False, 1.9, "1.9", "  5 ", 0, -3, "0", None, "x", {}):
            assert _parse_marker_winner_pk(bad) is None, bad

    def test_delegates_positive_int_rule_to_coerce_librenms_id(self):
        """The int/positive coercion is delegated to coerce_librenms_id; only the marker-specific string strictness is local."""
        from netbox_librenms_plugin.utils import coerce_librenms_id
        from netbox_librenms_plugin.views.sync.migrate import _parse_marker_winner_pk

        # For every non-string input the marker parser matches coerce_librenms_id exactly (the shared
        # bool-reject / positive-int rule) — markers are always stamped as ints in practice.
        for value in (5, 1, 999999, 0, -3, True, False, None, 1.9):
            assert _parse_marker_winner_pk(value) == coerce_librenms_id(value), value
        # A plain digit string agrees; a whitespace-padded one is where the marker parser stays
        # deliberately stricter than coerce_librenms_id's lenient int() (a tampered marker fails closed).
        assert _parse_marker_winner_pk("5") == coerce_librenms_id("5") == 5
        assert _parse_marker_winner_pk("  5 ") is None
        assert coerce_librenms_id("  5 ") == 5


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
    req._messages = MagicMock()  # parity with _nonhtmx_request: HTMX rejection paths call messages.error()
    return req


@pytest.mark.django_db
class TestMoveInterfaceToWinnerView:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        view = MoveInterfaceToWinnerView()
        # Bind a superuser-backed request: restrict() reads self.request.user (Django sets it in dispatch()).
        view.request = _hx_request()
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

        view.request = req
        resp = view.post(req, pk=interface.pk)

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        # Reject path flows through _fail(): HX-Reswap:none and never the success
        # HX-Refresh header (a regression that "succeeded" would set HX-Refresh=true).
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        interface.refresh_from_db()
        assert interface.device_id == donor.pk  # not moved

    def test_reconcile_family_mismatch_fails_closed_and_rolls_back_move(self):
        """If reconciling a moved interface's donor FK hits a corrupt family (primary_ip4 → IPv6), the whole move fails closed with a 409 and rolls back, never a 500."""
        view = self._setup_view()
        donor = make_device("mi-fam-donor")
        winner = make_device("mi-fam-winner")
        self._mark(donor, winner)
        # eth0 on the donor carries an IPv6 address that is (corruptly) the donor's primary_ip4.
        bad_ip = ip_on(donor, "2001:db8::7/128", "eth0")
        iface = bad_ip.assigned_object
        donor.primary_ip4 = bad_ip
        donor.save()
        req = _hx_request({"server_key": "default"})

        view.request = req
        resp = view.post(req, pk=iface.pk)

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        # Whole move rolled back: interface stays on the donor, the donor FK is intact, and
        # the winner never claimed the mismatched address.
        iface.refresh_from_db()
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert iface.device_id == donor.pk
        assert donor.primary_ip4_id == bad_ip.pk
        assert winner.primary_ip4_id is None

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

        view.request = req
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

        view.request = req
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

        view.request = req
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

        view.request = req
        resp = view.post(req, pk=interface.pk)

        assert resp.status_code == 200
        assert resp.headers.get("HX-Refresh") == "true"
        interface.refresh_from_db()
        assert interface.device_id == winner.pk  # really moved to the winner

    def test_missing_server_key_falls_back_without_constructing_api(self):
        """A no-server_key POST resolves the marker via active_server_key, never constructing LibreNMSAPI() (a misconfigured default would 500 the move)."""
        view = self._setup_view()
        donor = make_device("mi-nokey-donor")
        winner = make_device("mi-nokey-winner")  # no "Eth0" -> the move can proceed
        self._mark(donor, winner)  # marker namespaced under "default"
        interface = make_interface(donor, "Eth0")
        req = _hx_request({})  # POST omits server_key -> default_factory fires

        # Simulate a misconfigured default server: constructing the client raises ValueError,
        # exactly as the lazy librenms_api property would on a bad deployment. The fixed view
        # must not touch it; the unfixed view calls self.librenms_api.server_key and 500s.
        with patch(
            "netbox_librenms_plugin.views.mixins.LibreNMSAPI",
            side_effect=ValueError("LibreNMS URL or API token is not configured for server 'default'."),
        ) as api_ctor:
            view.request = req
            resp = view.post(req, pk=interface.pk)

        api_ctor.assert_not_called()  # the fix never builds the (broken) client
        assert resp.status_code == 200
        assert resp.headers.get("HX-Refresh") == "true"  # the move still happened
        interface.refresh_from_db()
        assert interface.device_id == winner.pk

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

        view.request = req
        resp = view.post(req, pk=member.pk)

        # full_clean() failed for real → row not saved, error surfaced via the OOB toast.
        assert b"django-messages" in resp.content
        assert resp.status_code == 200
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        member.refresh_from_db()
        assert member.device_id == donor.pk  # not moved
        assert member.lag_id == donor_lag.pk  # relationship untouched

    def test_moving_lag_master_carries_donor_side_members(self):
        """Moving a LAG interface must carry its donor-side members, not orphan them behind a cross-device lag FK NetBox forbids creating."""
        view = self._setup_view()
        donor = make_device("mi-lagcasc-donor")
        winner = make_device("mi-lagcasc-winner")
        self._mark(donor, winner)
        lag = make_interface(donor, "Po1", iface_type="lag")
        member = make_interface(donor, "Eth0")
        member.lag = lag
        member.save()
        req = _hx_request({"server_key": "default"})

        view.request = req
        resp = view.post(req, pk=lag.pk)

        assert resp.status_code == 200
        lag.refresh_from_db()
        member.refresh_from_db()
        assert lag.device_id == winner.pk
        # The member followed in the same transaction — it never persists as a donor-side
        # interface whose lag lives on another device (a state only its NEXT save would reject).
        assert member.device_id == winner.pk
        assert member.lag_id == lag.pk

    def test_moving_parent_carries_child_and_bridged_interfaces(self):
        """Moving a parent/bridge target interface carries its donor-side children and bridged peers too."""
        view = self._setup_view()
        donor = make_device("mi-parcasc-donor")
        winner = make_device("mi-parcasc-winner")
        self._mark(donor, winner)
        parent = make_interface(donor, "eth0")
        child = make_interface(donor, "eth0.100", iface_type="virtual")
        child.parent = parent
        child.save()
        bridged = make_interface(donor, "eth7")
        bridged.bridge = parent
        bridged.save()
        req = _hx_request({"server_key": "default"})

        view.request = req
        resp = view.post(req, pk=parent.pk)

        assert resp.status_code == 200
        parent.refresh_from_db()
        child.refresh_from_db()
        bridged.refresh_from_db()
        assert parent.device_id == winner.pk
        assert child.device_id == winner.pk and child.parent_id == parent.pk
        assert bridged.device_id == winner.pk and bridged.bridge_id == parent.pk

    def test_moving_lag_master_with_colliding_member_name_rolls_back_family(self):
        """A winner-side name collision on ANY family member 409s and rolls back the whole family move."""
        view = self._setup_view()
        donor = make_device("mi-lagcoll-donor")
        winner = make_device("mi-lagcoll-winner")
        self._mark(donor, winner)
        lag = make_interface(donor, "Po1", iface_type="lag")
        member = make_interface(donor, "Eth0")
        member.lag = lag
        member.save()
        make_interface(winner, "Eth0")  # collides with the member, not the master
        req = _hx_request({"server_key": "default"})

        view.request = req
        resp = view.post(req, pk=lag.pk)

        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        lag.refresh_from_db()
        member.refresh_from_db()
        # Nothing moved: the master must not land on the winner while its member stays behind.
        assert lag.device_id == donor.pk
        assert member.device_id == donor.pk and member.lag_id == lag.pk

    def test_moved_interface_cable_termination_cache_points_at_winner(self):
        """The cable's denormalized CableTermination._device must follow the move — the winner's cable lists filter on it (NetBox #9788 class)."""
        from dcim.models import CableTermination

        view = self._setup_view()
        donor = make_device("mi-ctcache-donor")
        winner = make_device("mi-ctcache-winner")
        peer = make_device("mi-ctcache-peer")
        self._mark(donor, winner)
        iface = make_interface(donor, "Eth0")
        peer_iface = make_interface(peer, "Eth0")
        cable_together(iface, peer_iface)
        req = _hx_request({"server_key": "default"})

        view.request = req
        resp = view.post(req, pk=iface.pk)

        assert resp.status_code == 200
        iface.refresh_from_db()
        assert iface.device_id == winner.pk
        ct = CableTermination.objects.get(termination_type__model="interface", termination_id=iface.pk)
        assert ct._device_id == winner.pk

    def test_save_integrity_error_rejected_with_409(self):
        """A winner-side create that trips the (device,name) unique constraint at save() — after every in-transaction check passes — is caught and surfaced as a 409 toast, and the whole move rolls back."""
        from dcim.models import Interface
        from django.db import IntegrityError

        view = self._setup_view()
        donor = make_device("mi-ie-donor")
        winner = make_device("mi-ie-winner")  # no "Eth0" -> the pre-check, re-check and full_clean all pass
        self._mark(donor, winner)
        interface = make_interface(donor, "Eth0")
        req = _hx_request({"server_key": "default"})

        # The IntegrityError handler guards a true concurrency boundary: another transaction creates
        # the winner's same-named interface AFTER our full_clean() but BEFORE our save(), tripping the
        # DB unique constraint. That race can't be reproduced single-threaded (full_clean would see an
        # in-transaction collision first), so simulate only the move save() raising; all rows, locks,
        # full_clean and the atomic rollback are real.
        real_save = Interface.save

        def boom(self, *args, **kwargs):
            if self.pk == interface.pk:
                raise IntegrityError("duplicate key value violates unique constraint")
            return real_save(self, *args, **kwargs)

        with patch.object(Interface, "save", boom):
            view.request = req
            resp = view.post(req, pk=interface.pk)

        # Caught and surfaced as the collision 409 OOB toast (HTTP 200, HX-Reswap:none, no HX-Refresh).
        assert resp.status_code == 200
        assert b"already has an interface named" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        # The atomic block rolled back for real: the interface is still on the donor.
        interface.refresh_from_db()
        assert interface.device_id == donor.pk

    def test_real_unique_violation_at_save_rolls_back_and_reports_409(self):
        """A real save-time unique violation reports 409 and rolls back the move."""
        from dcim.models import Interface

        view = self._setup_view()
        donor = make_device("mi-realie-donor")
        winner = make_device("mi-realie-winner")  # no "Eth0" -> pre-check, re-check and full_clean pass
        self._mark(donor, winner)
        interface = make_interface(donor, "Eth0")
        req = _hx_request({"server_key": "default"})

        # Land the winner-side row AFTER full_clean() and BEFORE save(), which is the only window
        # the IntegrityError handler exists for. Unlike patching save() to raise, the violation
        # here is the real DB constraint, so the connection really is poisoned afterwards.
        real_full_clean = Interface.full_clean
        state = {"raced": False}

        def racing_full_clean(iface, *args, **kwargs):
            result = real_full_clean(iface, *args, **kwargs)
            if iface.pk == interface.pk and not state["raced"]:
                state["raced"] = True
                Interface.objects.create(device=winner, name=interface.name, type="1000base-t")
            return result

        with patch.object(Interface, "full_clean", racing_full_clean):
            view.request = req
            resp = view.post(req, pk=interface.pk)

        assert state["raced"]
        assert resp.status_code == 200
        assert b"already has an interface named" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        # Everything the transaction touched is gone: the interface never moved and the
        # racing winner-side row was rolled back with it.
        interface.refresh_from_db()
        assert interface.device_id == donor.pk
        assert not Interface.objects.filter(device=winner, name="Eth0").exists()

    def test_marker_repointed_under_lock_is_rejected(self):
        """If the donor's _migrated_to is repointed between the unlocked resolve and the under-lock re-resolve, the move aborts instead of targeting the stale winner."""
        import netbox_librenms_plugin.views.sync.migrate as migrate_mod
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        view = self._setup_view()
        donor = make_device("mi-repoint-donor")
        winner = make_device("mi-repoint-winner")  # the original (now stale) target
        other_winner = make_device("mi-repoint-other")  # the marker is concurrently repointed here
        self._mark(donor, winner)
        interface = make_interface(donor, "Eth0")
        req = _hx_request({"server_key": "default"})

        # The REAL _resolve_winner_for_donor runs for both calls; between the unlocked resolve and the
        # under-lock re-resolve we really repoint the donor's marker (a committed write that lands
        # before the atomic block re-reads it), so the under-lock winner differs and the move aborts.
        real_resolve = migrate_mod._resolve_winner_for_donor
        state = {"repointed": False}

        def repoint_then_resolve(d, server_key):
            result = real_resolve(d, server_key)
            if not state["repointed"]:
                state["repointed"] = True
                mark_librenms_migrated(donor, other_winner.pk, "default")
                donor.save()
            return result

        with patch.object(migrate_mod, "_resolve_winner_for_donor", side_effect=repoint_then_resolve):
            view.request = req
            resp = view.post(req, pk=interface.pk)

        # Aborted with the concurrency conflict toast; the interface is moved to neither winner.
        assert resp.status_code == 200
        assert b"changed concurrently" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        interface.refresh_from_db()
        assert interface.device_id == donor.pk

    def test_perm_gate_short_circuits(self):
        from django.http import HttpResponse

        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        view = MoveInterfaceToWinnerView()
        # Bind a superuser-backed request: restrict() reads self.request.user (Django sets it in dispatch()).
        view.request = _hx_request()
        view.require_all_permissions = MagicMock(return_value=HttpResponse(status=403))
        req = _hx_request()
        view.request = req
        resp = view.post(req, pk=5)
        assert resp.status_code == 403

    def test_fail_htmx_toast_is_the_shared_helper_markup(self):
        """_fail's HTMX toast is byte-identical to _htmx_error_response (one source of toast markup, not a divergent copy)."""
        from netbox_librenms_plugin.views.imports.actions import _htmx_error_response
        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        view = MoveInterfaceToWinnerView()
        resp = view._fail(_hx_request(), "boom message")
        expected = _htmx_error_response("boom message")
        assert resp.content == expected.content
        assert resp["HX-Reswap"] == expected["HX-Reswap"] == "none"


# ── TransferDeviceIPView ──────────────────────────────────────────────────


@pytest.mark.django_db
class TestTransferDeviceIPView:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import TransferDeviceIPView

        view = TransferDeviceIPView()
        # Bind a superuser-backed request: restrict() reads self.request.user (Django sets it in dispatch()).
        view.request = _hx_request()
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
        view.request = req
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

        view.request = req
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

        view.request = req
        resp = view.post(req, pk=donor.pk, ip_kind="oob")

        assert resp.headers.get("HX-Refresh") == "true"
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert winner.oob_ip_id == oob_ip.pk  # winner claimed it
        assert donor.oob_ip_id is None  # donor released it

    def test_transfer_survives_interface_move_onto_winner_between_read_and_lock(self):
        """A concurrent interface move ONTO the winner, landing between the GFK read and the interface lock, must not spuriously 409 the transfer: the freshly locked (winner-owned) row is assigned back onto donor_ip before set_device_ip_fk re-checks ownership."""
        from dcim.models import Interface

        view = self._setup_view()
        donor = make_device("tx-race-donor")
        winner = make_device("tx-race-winner")
        self._mark(donor, winner)
        # The address starts on a DONOR interface, so the view's assigned_object read caches
        # a device_id=donor snapshot on the locked IP row.
        oob_ip = ip_on(donor, "10.0.0.7/24", "mgmt0")
        donor.oob_ip = oob_ip
        donor.save()
        iface_pk = oob_ip.assigned_object_id

        # Simulate the concurrent move: the owning-interface SELECT ... FOR UPDATE fires AFTER
        # the GFK read cached device_id=donor. Move the interface onto the winner right then,
        # so the locked row is winner-owned while the cached GFK snapshot is stale.
        real_sfu = Interface.objects.select_for_update
        state = {"moved": False}

        def moving_sfu(*args, **kwargs):
            queryset = real_sfu(*args, **kwargs)
            real_filter = queryset.filter

            def moving_filter(*f_args, **f_kwargs):
                if not state["moved"] and f_kwargs.get("pk") == iface_pk:
                    state["moved"] = True
                    Interface.objects.filter(pk=iface_pk).update(device=winner)
                return real_filter(*f_args, **f_kwargs)

            queryset.filter = moving_filter
            return queryset

        req = _hx_request({"server_key": "default"})
        view.request = req
        with patch.object(Interface.objects, "select_for_update", moving_sfu):
            resp = view.post(req, pk=donor.pk, ip_kind="oob")

        # The interface is winner-owned at lock time, so the transfer MUST complete — not be
        # rejected against the stale cached device_id and rolled back.
        assert resp.headers.get("HX-Refresh") == "true"
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert winner.oob_ip_id == oob_ip.pk
        assert donor.oob_ip_id is None
        assert state["moved"], "the injected interface move did not run"

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

        view.request = req
        resp = view.post(req, pk=donor.pk, ip_kind="oob")

        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert winner.oob_ip_id is None  # winner never claimed the donor-attached address
        assert donor.oob_ip_id == oob_ip.pk  # donor FK untouched

    def test_family_mismatch_fails_closed_not_500(self):
        """A corrupt donor FK (primary_ip4 referencing an IPv6 address) makes set_device_ip_fk refuse the claim; the view returns a graceful 409 toast and rolls back, never a 500."""
        view = self._setup_view()
        donor = make_device("tx-fam-donor")
        winner = make_device("tx-fam-winner")
        self._mark(donor, winner)
        # An IPv6 address on a WINNER interface, wrongly designated as the donor's primary_ip4
        # (save() skips full_clean(), so the family-mismatched FK persists).
        bad_ip = ip_on(winner, "2001:db8::5/128", "mgmt6")
        donor.primary_ip4 = bad_ip
        donor.save()
        req = _hx_request({"server_key": "default"})

        view.request = req
        resp = view.post(req, pk=donor.pk, ip_kind="primary4")

        # Graceful HTMX error toast, not an uncaught 500.
        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        # Rolled back: donor still holds the (corrupt) FK, winner never claimed it.
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.primary_ip4_id == bad_ip.pk
        assert winner.primary_ip4_id is None

    def test_rejects_when_assignment_is_not_an_interface(self):
        """A donor oob_ip whose address lives on a VMInterface (not a dcim Interface) fails closed: isinstance(assigned, Interface) is False, device_id never matches the winner, and nothing transfers."""
        from virtualization.models import VMInterface

        view = self._setup_view()
        donor = make_device("tx-vmi-donor")
        winner = make_device("tx-vmi-winner")
        self._mark(donor, winner)
        # The donor's oob_ip address is assigned to a real VMInterface (an owned NetBox model,
        # not an external boundary) — the cross-type case the isinstance guard fails closed on.
        vm = make_vm("tx-vmi-vm")
        vmi = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        oob_ip = make_ip("10.0.0.5/24", assigned_object=vmi)
        donor.oob_ip = oob_ip
        donor.save()  # save() skips full_clean(), so the cross-type dangling FK persists
        req = _hx_request({"server_key": "default"})

        view.request = req
        resp = view.post(req, pk=donor.pk, ip_kind="oob")

        # Reject path flows through _fail(): HX-Reswap:none and never the success HX-Refresh header.
        assert resp.status_code == 200
        assert b"django-messages" in resp.content
        assert resp.headers.get("HX-Reswap") == "none"
        assert resp.headers.get("HX-Refresh") is None
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert winner.oob_ip_id is None  # winner never claimed the VMInterface-attached address
        assert donor.oob_ip_id == oob_ip.pk  # donor FK untouched


# ── MoveIPAddressToWinnerView ────────────────────────────────────────────


@pytest.mark.django_db
class TestMoveIPAddressToWinnerView:
    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import MoveIPAddressToWinnerView

        view = MoveIPAddressToWinnerView()
        # Bind a superuser-backed request: restrict() reads self.request.user (Django sets it in dispatch()).
        view.request = _hx_request()
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

        view.request = req
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

        view.request = req
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
        view.request = req
        resp = view.post(req, pk=ip.pk)

        assert resp.status_code == 200
        # The HTMX refresh contract must fire so the migrated row doesn't linger stale in the UI:
        # a bare 200 without HX-Refresh would leave the old assignment on screen.
        assert resp.headers.get("HX-Refresh") == "true"
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

    def test_transfer_survives_interface_moving_onto_winner_mid_check(self):
        """A move ONTO the winner landing between the GFK read and the interface lock must not spuriously reject the transfer.

        ip.assigned_object is read (and cached by the GenericForeignKey) before the owning
        interface is locked; set_device_ip_fk re-reads that cache, so pre-fix the stale
        pre-move snapshot (device_id != winner.pk) raised ValueError and rolled back an
        otherwise-valid move — even though the locked-row gate had already validated the
        CURRENT owner. The race is reproduced by patching the GFK descriptor to serve the
        pre-move snapshot until the code refreshes it (the setter stores the override), the
        same freshen-after-lock pattern TransferDeviceIPView already uses.
        """
        from dcim.models import Interface
        from django.db import transaction
        from ipam.models import IPAddress

        from netbox_librenms_plugin.views.sync.migrate import _reconcile_donor_device_ip_fks

        winner, donor, _ = self._make_devices()
        ip = ip_on(donor, "10.10.0.3/24", "eth0")  # address starts on a DONOR-owned interface
        donor.primary_ip4 = ip
        donor.save()

        stale = Interface.objects.get(pk=ip.assigned_object_id)  # pre-move snapshot (device_id == donor.pk)
        # The concurrent transaction moves the interface (with its address) onto the winner.
        Interface.objects.filter(pk=stale.pk).update(device=winner)

        def _gfk_get(self):
            return self.__dict__.get("_ao_override", stale)

        def _gfk_set(self, value):
            self.__dict__["_ao_override"] = value

        with (
            patch.object(IPAddress, "assigned_object", property(_gfk_get, _gfk_set)),
            transaction.atomic(),
        ):
            notes = _reconcile_donor_device_ip_fks(donor, winner)

        winner.refresh_from_db()
        donor.refresh_from_db()
        assert winner.primary_ip4_id == ip.pk  # the valid transfer went through
        assert donor.primary_ip4_id is None
        assert any("transferred" in n for n in notes)


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

    def test_referer_validation_delegates_to_shared_barrier(self):
        """_safe_referer routes the CWE-601 Referer check through the shared validated_referer helper.

        Patching the shared barrier (not a hand-copied inline copy) changes the outcome, so the two
        redirect helpers can't drift on the open-redirect guard.
        """
        from unittest.mock import patch

        from netbox_librenms_plugin.views.sync.migrate import _safe_referer

        with patch("netbox_librenms_plugin.views.sync.migrate.validated_referer", return_value="http://ok/x"):
            assert _safe_referer(self._req(referer="whatever"), fallback="/sync/") == "http://ok/x"
        with patch("netbox_librenms_plugin.views.sync.migrate.validated_referer", return_value=None):
            assert _safe_referer(self._req(referer="whatever"), fallback="/sync/") == "/sync/"


class TestNonHtmxFallbackRedirect:
    """A plain POST with no Referer still lands on the donor sync tab + server."""

    def _setup_view(self):
        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        view = MoveInterfaceToWinnerView()
        # Bind a superuser-backed request: restrict() reads self.request.user (Django sets it in dispatch()).
        view.request = _hx_request()
        view.require_all_permissions = MagicMock(return_value=None)
        return view

    def test_missing_referer_preserves_tab_and_server_key(self):
        from django.urls import reverse

        view = self._setup_view()
        req = _nonhtmx_request({"server_key": "siteB"})
        donor = MagicMock(pk=10, cf={})
        # `name` is a reserved MagicMock constructor kwarg (sets the mock's repr, not `.name`),
        # so set the attribute explicitly — otherwise interface.name is a Mock, not "Eth0".
        interface = MagicMock(pk=5, device=donor)
        interface.name = "Eth0"

        with (
            patch.object(type(view), "restrict_object_or_404", return_value=interface),
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
            view.request = req
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
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}) as mock_mig,
            patch("netbox_librenms_plugin.views.mixins.render", return_value="rendered"),
        ):
            view.request = request
            result = view.post(request, pk=1)

        mock_mig.assert_called_once_with(obj, "test-server")  # session key, not "ghost-server"
        assert result == "rendered"


class TestServerKeyFromRequest:
    """_server_key_from_request must normalize the POSTed server_key."""

    def test_strips_whitespace_padded_key(self):
        from netbox_librenms_plugin.views.sync.migrate import _server_key_from_request

        request = MagicMock()
        request.POST = {"server_key": "  prod  "}
        assert _server_key_from_request(request) == "prod"

    def test_whitespace_only_key_falls_back_to_factory(self):
        from netbox_librenms_plugin.views.sync.migrate import _server_key_from_request

        request = MagicMock()
        request.POST = {"server_key": "   "}  # whitespace-only → treated as blank
        assert _server_key_from_request(request, default_factory=lambda: "sessionkey") == "sessionkey"


@pytest.mark.django_db
class TestMigratedContextServerKeyFallback:
    """When the POSTed server_key fails to rebind (malformed/stale), the cable/IP/interface sync error
    render must still resolve the migrated marker — under the SESSION key, not the failed posted key —
    so a migrated donor's sync controls stay suppressed instead of being silently re-enabled.

    Real-DB: a real donor+winner+marker and the real build_migrated_context exercise the resolution;
    only the NetBox template render (needs full request context) and the external LibreNMS rebind are
    stubbed. Asserts the actual resolved winner, not merely that the helper was called.
    """

    _counter = 0

    def _assert_resolves_under_session(self, view_cls, posted):
        from dcim.models import Device

        type(self)._counter += 1
        n = type(self)._counter
        winner = make_device(f"sk-winner-{n}")
        donor = make_device(
            f"sk-donor-{n}",
            librenms_cf={
                "sessionkey": {"_migrated_to": {"device_id": winner.pk, "server_key": "sessionkey", "at": "x"}}
            },
        )

        view = object.__new__(view_cls)
        view.model = Device
        view._librenms_api = MagicMock(server_key="sessionkey")
        # The posted key fails to rebind (unconfigured/stale) — the external-config boundary.
        view.rebind_api_for_server = MagicMock(return_value=None)
        request = _hx_request(posted)

        # Stub only the NetBox template render (it needs full request context unavailable in a unit
        # test); build_migrated_context runs for REAL against the donor's marker.
        with (
            patch("netbox_librenms_plugin.views.mixins.render", return_value="rendered") as mock_render,
            patch("netbox_librenms_plugin.views.base.cables_view.messages"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.messages"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
        ):
            view.request = request
            result = view.post(request, pk=donor.pk)

        assert result == "rendered"
        _req, _template, ctx = mock_render.call_args.args
        # Resolved under the SESSION key → the real winner is found. A wrong/None key would resolve
        # no winner (migrated_to_winner None), re-enabling the donor's sync controls.
        assert ctx["migrated_to_marker"]["device_id"] == winner.pk
        assert ctx["migrated_to_winner"].pk == winner.pk

    def test_cables_view_empty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        self._assert_resolves_under_session(BaseCableTableView, {})

    def test_cables_view_stale_nonempty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        self._assert_resolves_under_session(BaseCableTableView, {"server_key": "ghost"})

    def test_ip_view_empty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        self._assert_resolves_under_session(BaseIPAddressTableView, {})

    def test_ip_view_stale_nonempty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        self._assert_resolves_under_session(BaseIPAddressTableView, {"server_key": "ghost"})

    def test_interfaces_view_empty_key_uses_session_key(self):
        # interfaces_view previously redirected on a stale key (dropping migrated context); it must
        # now render the partial with migrated context resolved under the session key.
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        self._assert_resolves_under_session(BaseInterfaceTableView, {})

    def test_interfaces_view_stale_nonempty_key_uses_session_key(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        self._assert_resolves_under_session(BaseInterfaceTableView, {"server_key": "ghost"})

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
            patch("netbox_librenms_plugin.views.mixins.render"),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}) as bmc,
            # The lazy property must NOT be touched on this path; make it explode if it is.
            patch.object(
                BaseInterfaceTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.request = request
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
            patch("netbox_librenms_plugin.views.mixins.render"),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}) as bmc,
            patch.object(
                BaseVLANTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.request = request
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
            patch("netbox_librenms_plugin.views.mixins.render"),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}) as bmc,
            patch.object(
                BaseModuleTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.request = request
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
            patch("netbox_librenms_plugin.views.mixins.render"),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}) as bmc,
            patch.object(
                BaseCableTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.request = request
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
            patch("netbox_librenms_plugin.views.mixins.render"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.resolve_set_primary_ip", return_value=False),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}) as bmc,
            patch.object(
                BaseIPAddressTableView,
                "librenms_api",
                new_callable=PropertyMock,
                side_effect=AssertionError("lazy librenms_api touched on the rebind-fail path"),
            ),
        ):
            view.request = request
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
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}),
        ):
            view.request = request
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
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}),
        ):
            view.request = request
            view.post(request, pk=1)

        ip_sync = mock_render.call_args.args[2]["ip_sync"]
        assert ip_sync["set_primary_ip"] is True


@pytest.mark.django_db
class TestMovableIpsForMigration:
    """_movable_ips_for_migration lists a migrated donor's interface-assigned IPs (Move-to-winner candidates)."""

    @staticmethod
    def _movable(donor, server_key="default"):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        return DeviceIPAddressTableView._movable_ips_for_migration(donor, server_key)

    def test_empty_when_not_migrated(self):
        donor = make_device("mv-nomark")
        ip_on(donor, "10.10.0.5/24", "eth0")  # real IP on a real interface, but no marker
        assert self._movable(donor) == []

    def test_lists_interface_assigned_ips_when_migrated(self):
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        winner = make_device("mv-winner")
        donor = make_device("mv-donor")
        ip = ip_on(donor, "10.10.0.5/24", "eth0")
        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

        result = self._movable(donor)
        assert len(result) == 1
        assert result[0]["id"] == ip.pk
        assert result[0]["address"] == "10.10.0.5/24"
        assert result[0]["interface_name"] == "eth0"

    def test_none_server_key_uses_the_default_marker_scope(self):
        """A degraded render with no bound key still finds a marker stored in default scope."""
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        winner = make_device("mv-winner-none-key")
        donor = make_device("mv-donor-none-key")
        ip_on(donor, "10.10.0.8/24", "eth0")
        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

        assert len(self._movable(donor, None)) == 1

    def test_excludes_unassigned_ip(self):
        from netbox_librenms_plugin.tests.conftest import make_ip
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        winner = make_device("mv-winner2")
        donor = make_device("mv-donor2")
        ip_on(donor, "10.10.0.6/24", "eth0")  # assigned -> candidate
        make_ip("10.10.0.99/24")  # unassigned -> not a candidate
        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

        assert [r["address"] for r in self._movable(donor)] == ["10.10.0.6/24"]

    def test_scoped_to_marker_server_key(self):
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        winner = make_device("mv-winner3")
        donor = make_device("mv-donor3")
        ip_on(donor, "10.10.0.7/24", "eth0")
        mark_librenms_migrated(donor, winner.pk, "edgelondon")  # marker under a non-default server
        donor.save()

        # The marker's own key surfaces the candidates; an unrelated key sees none.
        assert self._movable(donor, "default") == []
        assert len(self._movable(donor, "edgelondon")) == 1


@pytest.mark.django_db
class TestMigratedDonorRendersSyncTabs:
    """A migrated (not-found) donor must render the sync tabs so the per-row Move-to-winner controls are reachable.

    The merge clears the donor's active librenms_id, so it is never found_in_librenms; without gating the
    tabs on migrated_to_marker too, the banner promises per-row interface/IP move actions that never render.
    Full HTTP request -> view -> DB -> template, no LibreNMS API (librenms_id is None for a migrated donor).
    """

    def test_full_page_shows_tabs_and_reachable_ip_move_button(self, client):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import ip_on, make_device
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        user = get_user_model().objects.create_superuser("mig-tabs-admin", "a@b.c", "pw")
        client.force_login(user)

        winner = make_device("mt-winner")
        donor = make_device("mt-donor")
        ip = ip_on(donor, "10.50.0.5/24", "eth0")  # a real interface-assigned IP to move
        active_server_key = LibreNMSAPI(server_key="default").server_key
        mark_librenms_migrated(donor, winner.pk, active_server_key)  # clears the active id -> donor not found
        donor.save()

        url = reverse("plugins:netbox_librenms_plugin:device_librenms_sync", args=[donor.pk])
        # Pass an explicit ?server_key so the view resolves the server via LibreNMSAPI("default")
        # (which skips the LibreNMSSettings.selected_server read); otherwise this real-client render
        # is flaky under a suite where another test leaks a LibreNMSSettings mock.
        resp = client.get(url, {"tab": "ipaddresses", "server_key": "default"})

        assert resp.status_code == 200
        html = resp.content.decode()
        # Migrated mode now renders the tab structure (not the "device not found, match it" dead-end)...
        assert 'id="librenmsTabContent"' in html
        assert "To match a device, use one of these methods" not in html
        # ...so the per-row IP "Move to winner" button is actually reachable.
        move_url = reverse("plugins:netbox_librenms_plugin:ipaddress_move_to_winner", kwargs={"pk": ip.pk})
        assert move_url in html
        assert "Move IP addresses" in html  # the migrated move card header
        # The gating-rationale comment must be a {% comment %} block, not a multi-line {# #} (which
        # Django can't span across lines and would leak into the page as visible text).
        assert "never found_in_librenms" not in html


class TestDeviceIpLabelsSingleSource:
    """Device IP FK labels live in one map (utils.DEVICE_IP_FK_LABELS); the transfer view derives from it."""

    def test_labels_cover_all_fk_fields_and_view_maps_only_to_them(self):
        from netbox_librenms_plugin.utils import DEVICE_IP_FK_FIELDS, DEVICE_IP_FK_LABELS
        from netbox_librenms_plugin.views.sync.migrate import TransferDeviceIPView

        # One label per reconcilable FK field, and the transfer view maps ip_kind kwargs only to those
        # fields — the human label is then read from the shared map, so there is no second copy to drift.
        assert set(DEVICE_IP_FK_LABELS) == set(DEVICE_IP_FK_FIELDS)
        assert set(TransferDeviceIPView._IP_KIND_TO_FIELD.values()) == set(DEVICE_IP_FK_FIELDS)


@pytest.mark.django_db
class TestMoveToWinnerConcurrencyHelpers:
    """The move-to-winner resolve + lock/re-verify TOCTOU guards live in one shared pair of helpers.

    Exercises _resolve_winner_or_fail and _lock_donor_winner_and_reverify directly (all three move
    endpoints route through them) so the extracted concurrency guard is pinned in one place.
    """

    def _view(self):
        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        view = object.__new__(MoveInterfaceToWinnerView)
        view._fallback_url = "/x/"
        # restrict() reads self.request.user; Django binds it in dispatch(), which these
        # direct-helper calls skip.
        view.request = self._req()
        return view

    def _req(self):
        from django.test import RequestFactory

        # HX-Request → _fail returns an HTMX toast response (no messages middleware needed).
        request = RequestFactory().post("/x/", HTTP_HX_REQUEST="true")
        request.user = make_superuser()
        return request

    def _migrated_donor_and_winner(self):
        donor = make_device("mv-helper-donor")
        winner = make_device("mv-helper-winner")
        donor.custom_field_data["librenms_id"] = {
            "default": {"_migrated_to": {"device_id": winner.pk, "server_key": "default"}}
        }
        donor.save()
        return donor, winner

    def test_resolve_winner_or_fail_resolves_migrated_winner(self):
        donor, winner = self._migrated_donor_and_winner()
        got, err = self._view()._resolve_winner_or_fail(self._req(), donor, "default")
        assert err is None
        assert got.pk == winner.pk

    def test_resolve_winner_or_fail_rejects_unmarked_donor(self):
        donor = make_device("mv-helper-unmarked")
        got, err = self._view()._resolve_winner_or_fail(self._req(), donor, "default")
        assert got is None
        assert err is not None  # "Donor device is not marked as migrated."

    def test_lock_and_reverify_success_returns_locked_pair(self):
        from django.db import transaction

        donor, winner = self._migrated_donor_and_winner()
        with transaction.atomic():
            d, w, err = self._view()._lock_donor_winner_and_reverify(self._req(), donor, winner, "default")
        assert err is None
        assert d.pk == donor.pk and w.pk == winner.pk

    def test_lock_and_reverify_detects_marker_cleared_under_lock(self):
        from django.db import transaction

        donor, winner = self._migrated_donor_and_winner()
        # Marker cleared between the unlocked resolve and acquiring the row locks (TOCTOU):
        donor.custom_field_data["librenms_id"] = {"default": {}}
        donor.save()
        with transaction.atomic():
            d, w, err = self._view()._lock_donor_winner_and_reverify(self._req(), donor, winner, "default")
        assert d is None and w is None
        assert err is not None  # "Donor migration changed concurrently; refresh and retry."


@pytest.mark.django_db
class TestMoveEndpointsObjectScope:
    """The move-to-winner endpoints must resolve their URL pk through a restricted queryset.

    NetBoxObjectPermissionMixin checks the required ``change`` perms at model level only, so a
    constrained grant clears the gate; a raw ``get_object_or_404`` would then let it move an
    interface, IP or device FK it can't see.
    """

    @staticmethod
    def _writer(username, specs):
        """A real non-superuser with plugin write access plus ``specs`` = [(model, constraints|None)] change grants."""
        from core.models import ObjectType
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from users.models import ObjectPermission

        LibreNMSSettings = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")

        user = get_user_model().objects.create_user(username=username, password="x")
        write = ObjectPermission.objects.create(name=f"{username}-plugin-write", actions=["change"])
        write.object_types.set([ObjectType.objects.get_for_model(LibreNMSSettings)])
        write.users.set([user])

        for i, (model, constraints) in enumerate(specs):
            perm = ObjectPermission.objects.create(
                name=f"{username}-change-{i}", actions=["change"], constraints=constraints
            )
            perm.object_types.set([ObjectType.objects.get_for_model(model)])
            perm.users.set([user])

        return get_user_model().objects.get(pk=user.pk)  # clear the per-request perm cache

    @staticmethod
    def _request(user):
        """A real HTMX POST request bound to *user* so the gate and restrict() both run for real."""
        from django.test import RequestFactory

        from django.contrib.messages.storage.fallback import FallbackStorage

        request = RequestFactory().post("/move/", {"server_key": "default"}, HTTP_HX_REQUEST="true")
        request.user = user
        # The success path calls messages.add_message(); RequestFactory skips MessageMiddleware.
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def _drive(self, view_cls, user, **kwargs):
        view = view_cls()
        request = self._request(user)
        view.request = request
        return view.post(request, **kwargs)

    def test_interface_move_404s_an_out_of_scope_interface(self):
        """A change_interface grant constrained to another interface must not reach the move logic."""
        from dcim.models import Device, Interface
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        donor = make_device("scope-mi-donor")
        in_scope = make_interface(make_device("scope-mi-other"), "Eth0")
        out_of_scope = make_interface(donor, "Eth1")
        user = self._writer("scoped-move-iface", [(Interface, {"pk": in_scope.pk}), (Device, None)])

        with pytest.raises(Http404):
            self._drive(MoveInterfaceToWinnerView, user, pk=out_of_scope.pk)

        out_of_scope.refresh_from_db()
        assert out_of_scope.device_id == donor.pk  # not moved

    def test_interface_move_resolves_the_in_scope_interface(self):
        """The interface the grant DOES cover resolves and reaches the marker check (no over-block)."""
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        donor = make_device("scope-mi-in-donor")
        iface = make_interface(donor, "Eth0")
        user = self._writer("scoped-move-iface-ok", [(Interface, {"pk": iface.pk}), (Device, None)])

        response = self._drive(MoveInterfaceToWinnerView, user, pk=iface.pk)

        # No marker on the donor, so the move is refused on its merits — not 404'd at the lookup.
        assert response.status_code == 200
        assert b"not marked" in response.content or b"django-messages" in response.content

    def test_ip_move_404s_an_out_of_scope_ip(self):
        """A change_ipaddress grant constrained to another IP must not reach the move logic."""
        from dcim.models import Device
        from django.http import Http404
        from ipam.models import IPAddress

        from netbox_librenms_plugin.views.sync.migrate import MoveIPAddressToWinnerView

        donor = make_device("scope-ip-donor")
        in_scope = ip_on(make_device("scope-ip-other"), "10.60.0.1/24", "eth0")
        out_of_scope = ip_on(donor, "10.60.1.1/24", "eth0")
        user = self._writer("scoped-move-ip", [(IPAddress, {"pk": in_scope.pk}), (Device, None)])

        with pytest.raises(Http404):
            self._drive(MoveIPAddressToWinnerView, user, pk=out_of_scope.pk)

        out_of_scope.refresh_from_db()
        assert out_of_scope.assigned_object.device_id == donor.pk  # not moved

    def test_device_ip_transfer_404s_an_out_of_scope_donor(self):
        """A pk-constrained change_device grant must not transfer another device's primary IP."""
        from dcim.models import Device
        from django.http import Http404

        from netbox_librenms_plugin.views.sync.migrate import TransferDeviceIPView

        in_scope = make_device("scope-xfer-in")
        donor = make_device("scope-xfer-donor")
        donor_ip = ip_on(donor, "10.61.0.1/24", "eth0")
        donor.primary_ip4 = donor_ip
        donor.save()
        user = self._writer("scoped-xfer-device", [(Device, {"pk": in_scope.pk})])

        with pytest.raises(Http404):
            self._drive(TransferDeviceIPView, user, pk=donor.pk, ip_kind="primary4")

        donor.refresh_from_db()
        assert donor.primary_ip4_id == donor_ip.pk  # not transferred

    @staticmethod
    def _mark(donor, winner):
        """Write a real ``_migrated_to`` marker so the views resolve *winner* from the donor's cf."""
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

    def test_interface_move_denied_when_a_family_member_is_out_of_scope(self):
        """The carried lag/parent/bridge family is saved too, so one out-of-scope member must block the whole move."""
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        donor = make_device("scope-fam-donor")
        winner = make_device("scope-fam-winner")
        master = make_interface(donor, "Eth0")
        child = Interface.objects.create(device=donor, name="Eth0.100", type="virtual", parent=master)
        self._mark(donor, winner)
        # The grant covers the interface the user clicked, but not the child that must move with it.
        user = self._writer("scoped-family-partial", [(Interface, {"pk": master.pk}), (Device, None)])

        response = self._drive(MoveInterfaceToWinnerView, user, pk=master.pk)

        assert response.headers.get("HX-Refresh") is None  # not the success path
        master.refresh_from_db()
        child.refresh_from_db()
        assert master.device_id == donor.pk  # nothing moved
        assert child.device_id == donor.pk

    def test_interface_move_carries_the_family_when_all_of_it_is_in_scope(self):
        """Widening the grant to the whole family lets the move through (no over-block)."""
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        donor = make_device("scope-fam-ok-donor")
        winner = make_device("scope-fam-ok-winner")
        master = make_interface(donor, "Eth0")
        child = Interface.objects.create(device=donor, name="Eth0.100", type="virtual", parent=master)
        self._mark(donor, winner)
        user = self._writer("scoped-family-whole", [(Interface, {"pk__in": [master.pk, child.pk]}), (Device, None)])

        self._drive(MoveInterfaceToWinnerView, user, pk=master.pk)

        master.refresh_from_db()
        child.refresh_from_db()
        assert master.device_id == winner.pk
        assert child.device_id == winner.pk

    def test_interface_move_denied_when_the_winner_device_is_out_of_scope(self):
        """The move writes onto the winner device, so a change_device grant that excludes it must block."""
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.views.sync.migrate import MoveInterfaceToWinnerView

        donor = make_device("scope-win-mi-donor")
        winner = make_device("scope-win-mi-winner")
        iface = make_interface(donor, "Eth0")
        self._mark(donor, winner)
        user = self._writer("scoped-move-iface-winner", [(Interface, None), (Device, {"pk": donor.pk})])

        response = self._drive(MoveInterfaceToWinnerView, user, pk=iface.pk)

        assert response.headers.get("HX-Refresh") is None
        iface.refresh_from_db()
        assert iface.device_id == donor.pk  # not moved onto an unauthorized device

    def test_ip_move_denied_when_the_winner_device_is_out_of_scope(self):
        """The IP move reconciles both devices' IP FKs, so an out-of-scope winner must block it."""
        from dcim.models import Device
        from ipam.models import IPAddress

        from netbox_librenms_plugin.views.sync.migrate import MoveIPAddressToWinnerView

        donor = make_device("scope-win-ip-donor")
        winner = make_device("scope-win-ip-winner")
        ip = ip_on(donor, "10.63.0.1/24", "eth0")
        make_interface(winner, "eth0")
        self._mark(donor, winner)
        user = self._writer("scoped-move-ip-winner", [(IPAddress, None), (Device, {"pk": donor.pk})])

        response = self._drive(MoveIPAddressToWinnerView, user, pk=ip.pk)

        assert response.headers.get("HX-Refresh") is None
        ip.refresh_from_db()
        assert ip.assigned_object.device_id == donor.pk  # not moved

    def test_device_ip_transfer_denied_when_the_winner_is_out_of_scope(self):
        """The transfer claims the winner's primary-IP FK, so a donor-only change_device grant must block it."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.migrate import TransferDeviceIPView

        donor = make_device("scope-xfer-win-donor")
        winner = make_device("scope-xfer-win-winner")
        # The address already sits on a winner-owned interface — the state this transfer exists for.
        ip = ip_on(winner, "10.64.0.1/24", "eth0")
        Device.objects.filter(pk=donor.pk).update(primary_ip4=ip)
        donor.refresh_from_db()
        self._mark(donor, winner)
        user = self._writer("scoped-xfer-winner", [(Device, {"pk": donor.pk})])

        response = self._drive(TransferDeviceIPView, user, pk=donor.pk, ip_kind="primary4")

        assert response.headers.get("HX-Refresh") is None
        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.primary_ip4_id == ip.pk  # donor FK not released
        assert winner.primary_ip4_id is None  # winner FK not claimed

    def test_device_ip_transfer_runs_when_both_devices_are_in_scope(self):
        """Widening the grant to the winner lets the same transfer through (no over-block)."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.sync.migrate import TransferDeviceIPView

        donor = make_device("scope-xfer-ok-donor")
        winner = make_device("scope-xfer-ok-winner")
        ip = ip_on(winner, "10.65.0.1/24", "eth0")
        Device.objects.filter(pk=donor.pk).update(primary_ip4=ip)
        donor.refresh_from_db()
        self._mark(donor, winner)
        user = self._writer("scoped-xfer-both", [(Device, {"pk__in": [donor.pk, winner.pk]})])

        self._drive(TransferDeviceIPView, user, pk=donor.pk, ip_kind="primary4")

        donor.refresh_from_db()
        winner.refresh_from_db()
        assert donor.primary_ip4_id is None
        assert winner.primary_ip4_id == ip.pk


# transaction=True: the view schedules the winner cleanup after its atomic block, so only a real
# commit runs it inline the way production does.
@pytest.mark.django_db(transaction=True)
def test_moving_an_ip_to_an_unmapped_winner_reports_no_cleanup_failure(client):
    """A winner with no mapping for this server has nothing cached, so cleanup must be a no-op."""
    import json

    from django.urls import reverse

    from netbox_librenms_plugin.utils import mark_librenms_migrated

    winner = make_device("mtw-unmapped-winner")
    donor = make_device("mtw-unmapped-donor")
    make_interface(winner, "eth0")
    ip = ip_on(donor, "10.51.0.5/24", "eth0")
    mark_librenms_migrated(donor, winner.pk, "default")
    donor.save()
    client.force_login(make_superuser("mtw-unmapped-user"))

    move_url = reverse("plugins:netbox_librenms_plugin:ipaddress_move_to_winner", kwargs={"pk": ip.pk})
    response = client.post(move_url, {"server_key": "default"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    ip.refresh_from_db()
    assert ip.assigned_object.device_id == winner.pk
    transition = json.loads(response["X-LibreNMS-Cache-Transition"])
    assert transition["cleanup_failed"] is False
