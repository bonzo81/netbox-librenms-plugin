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

        assert resp.status_code == 409

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

        assert resp.status_code == 409

    def test_happy_path_reassigns_device_and_returns_hx_refresh(self):
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
            patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction"),
            patch("netbox_librenms_plugin.views.sync.migrate.messages"),
        ):
            mock_iface_cls.objects.filter.return_value.exists.return_value = False
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.order_by.return_value = []
            resp = view.post(req, pk=5)

        assert interface.device is winner
        interface.save.assert_called_once()
        assert resp.headers.get("HX-Refresh") == "true"

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
        assert resp.status_code == 400

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

        assert resp.status_code == 409

    def test_happy_path_transfers_oob_ip(self):
        view = self._setup_view()
        req = _hx_request({"server_key": "default"})

        oob_ip = MagicMock(address="10.0.0.5/24")
        donor = MagicMock(pk=10, oob_ip=oob_ip)
        winner = MagicMock(pk=20, name="winner", oob_ip=None)

        with (
            patch("netbox_librenms_plugin.views.sync.migrate.get_object_or_404", return_value=donor),
            patch(
                "netbox_librenms_plugin.views.sync.migrate._resolve_winner_for_donor",
                return_value=(winner, {"device_id": 20, "server_key": "default", "at": "x"}),
            ),
            patch("netbox_librenms_plugin.views.sync.migrate.Device") as mock_device_cls,
            patch("netbox_librenms_plugin.views.sync.migrate.transaction"),
            patch("netbox_librenms_plugin.views.sync.migrate.messages"),
        ):
            mock_device_cls.objects.select_for_update.return_value.filter.return_value.order_by.return_value = []
            resp = view.post(req, pk=10, ip_kind="oob")

        assert winner.oob_ip is oob_ip
        assert donor.oob_ip is None
        winner.save.assert_called_once()
        donor.save.assert_called_once()
        assert resp.headers.get("HX-Refresh") == "true"


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

        assert resp.status_code == 409

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
            mock_objects.filter.return_value.first.return_value = None
            resp = view.post(req, pk=7)

        assert resp.status_code == 409
