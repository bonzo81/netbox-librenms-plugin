"""
Coverage tests for views/sync/interfaces.py

SyncInterfacesView + DeleteNetBoxInterfacesView
Target: 95%+ coverage
"""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_virtual_chassis_members, make_vm
from netbox_librenms_plugin.tests.view_test_helpers import make_request, make_user_with_perms, make_view, missing_pk

# The views here are built with real requests and real users, so the whole file needs the DB.
pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(post_data=None, get_data=None, user=None):
    """A real request. POST wins when both are given; a GET-only call builds a GET request."""
    if post_data is None and get_data:
        return make_request("get", get_data, user=user)
    request = make_request("post", post_data or {}, user=user)
    request.GET = get_data or request.GET
    return request


def _sync_view(request=None):
    """The real SyncInterfacesView; only the LibreNMS client is stubbed."""
    from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

    view = make_view(SyncInterfacesView, request)
    view._post_server_key = "default"
    return view


def _denied_response():
    resp = MagicMock()
    resp.status_code = 403
    return resp


# ===========================================================================
# SyncInterfacesView.get_required_permissions_for_object_type
# ===========================================================================


class TestSyncInterfacesViewPermissions:
    def test_device_type_returns_interface_perms(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        from dcim.models import Interface

        view = object.__new__(SyncInterfacesView)
        perms = view.get_required_permissions_for_object_type("device")
        assert ("change", Interface) in perms

    def test_vm_type_returns_vminterface_perms(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        from virtualization.models import VMInterface

        view = object.__new__(SyncInterfacesView)
        perms = view.get_required_permissions_for_object_type("virtualmachine")
        assert ("change", VMInterface) in perms

    def test_invalid_type_raises_http404(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        from django.http import Http404

        view = object.__new__(SyncInterfacesView)
        with pytest.raises(Http404):
            view.get_required_permissions_for_object_type("invalid")


class TestSyncInterfaceParentViewPermissions:
    """SyncInterfaceParentView supports VMs (VMInterface has a parent field), so its
    POST permission must be scoped to the object type, not hardcoded to Interface."""

    def _stop_after_perms(self):
        """Patch the JSON permission gate to short-circuit post() right after the dynamic
        permission dict is set, returning a sentinel response. The view uses the _json variant
        (it's a fetch() endpoint), so that's the method to intercept."""
        return patch.object(
            __import__(
                "netbox_librenms_plugin.views.sync.interfaces", fromlist=["SyncInterfaceParentView"]
            ).SyncInterfaceParentView,
            "require_all_permissions_json",
            return_value=_denied_response(),
        )

    def test_device_post_requires_interface_change(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView
        from dcim.models import Interface

        view = object.__new__(SyncInterfaceParentView)
        with self._stop_after_perms():
            view.post(_make_request(), "device", 1)
        assert view.required_object_permissions["POST"] == [("change", Interface)]

    def test_vm_post_requires_vminterface_change(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView
        from virtualization.models import VMInterface

        view = object.__new__(SyncInterfaceParentView)
        with self._stop_after_perms():
            view.post(_make_request(), "virtualmachine", 1)
        assert view.required_object_permissions["POST"] == [("change", VMInterface)]

    def test_invalid_type_raises_http404(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView
        from django.http import Http404
        import pytest

        view = object.__new__(SyncInterfaceParentView)
        with pytest.raises(Http404):
            view.post(_make_request(), "invalid", 1)


class TestInterfacesSameOwnerGuard:
    """_interfaces_same_owner gates lag/parent links so a port_stack relationship that
    resolves across two VC members can't persist a NetBox-forbidden cross-device link."""

    def test_same_device_is_true(self):
        from netbox_librenms_plugin.views.sync.interfaces import _interfaces_same_owner

        a = MagicMock(device_id=1, virtual_machine_id=None)
        b = MagicMock(device_id=1, virtual_machine_id=None)
        assert _interfaces_same_owner(a, b) is True

    def test_different_device_is_false(self):
        from netbox_librenms_plugin.views.sync.interfaces import _interfaces_same_owner

        a = MagicMock(device_id=1, virtual_machine_id=None)
        b = MagicMock(device_id=2, virtual_machine_id=None)
        assert _interfaces_same_owner(a, b) is False

    def test_same_vm_is_true(self):
        from netbox_librenms_plugin.views.sync.interfaces import _interfaces_same_owner

        a = MagicMock(device_id=None, virtual_machine_id=7)
        b = MagicMock(device_id=None, virtual_machine_id=7)
        assert _interfaces_same_owner(a, b) is True


# ===========================================================================
# SyncInterfacesView.get_object
# ===========================================================================


class TestSyncInterfacesViewGetObject:
    def test_get_device(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        mock_device = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_device,
        ):
            result = view.get_object("device", 1)
        assert result is mock_device

    def test_get_vm(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        mock_vm = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
            return_value=mock_vm,
        ):
            result = view.get_object("virtualmachine", 2)
        assert result is mock_vm

    def test_invalid_type_raises_http404(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView
        from django.http import Http404

        view = object.__new__(SyncInterfacesView)
        with pytest.raises(Http404):
            view.get_object("invalid", 1)


# ===========================================================================
# SyncInterfacesView.get_selected_interfaces
# ===========================================================================


class TestSyncInterfacesViewGetSelectedInterfaces:
    def test_empty_selection_returns_none(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        req = _make_request(post_data={})
        with patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs:
            result = view.get_selected_interfaces(req, "ifName")
        assert result is None
        mock_msgs.error.assert_called_once()

    def test_with_selection_returns_list(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        req = _make_request(post_data={"select": ["Gi0/1", "Gi0/2"]})
        result = view.get_selected_interfaces(req, "ifName")
        assert result == ["Gi0/1", "Gi0/2"]


# ===========================================================================
# SyncInterfacesView.get_cached_ports_data
# ===========================================================================


class TestSyncInterfacesViewGetCachedPortsData:
    def test_cache_miss_warns_and_returns_none(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.get_cache_key = MagicMock(return_value="k")
        req = _make_request()
        mock_obj = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
        ):
            mock_cache.get.return_value = None
            result = view.get_cached_ports_data(req, mock_obj, "default")

        assert result is None
        mock_msgs.warning.assert_called_once()

    def test_cache_hit_returns_ports(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.get_cache_key = MagicMock(return_value="k")
        req = _make_request()
        mock_obj = MagicMock(pk=1)
        ports = [{"ifName": "Gi0/1"}]

        with patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache:
            mock_cache.get.return_value = {"ports": ports}
            result = view.get_cached_ports_data(req, mock_obj, "default")

        assert result == ports

    def test_malformed_cached_ports_treated_as_miss(self):
        """A stale/malformed cache entry (ports not a list of dicts, e.g."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.get_cache_key = MagicMock(return_value="k")
        req = _make_request()
        mock_obj = MagicMock(pk=1)

        for bad in ({"ports": None}, {"ports": "oops"}, {"ports": [None]}, ["not-a-dict"]):
            with (
                patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
                patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
            ):
                mock_cache.get.return_value = bad
                result = view.get_cached_ports_data(req, mock_obj, "default")

            assert result is None, f"malformed {bad!r} should be a miss"
            mock_msgs.warning.assert_called_once()

    def test_dict_without_ports_key_is_noop_not_miss(self):
        """A cached dict that simply lacks a 'ports' key is a harmless empty no-op (historical behavior), not a 'refresh first' abort — only PRESENT-but-malformed ports fail closed."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.get_cache_key = MagicMock(return_value="k")
        req = _make_request()
        mock_obj = MagicMock(pk=1)

        for empty in ({}, {"librenms_id": 5}, {"ports": []}):
            with (
                patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
                patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
            ):
                mock_cache.get.return_value = empty
                result = view.get_cached_ports_data(req, mock_obj, "default")

            assert result == [], f"{empty!r} should be an empty no-op, not a miss"
            mock_msgs.warning.assert_not_called()

    def test_no_server_key_uses_librenms_api(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.get_cache_key = MagicMock(return_value="k")
        mock_api = MagicMock(server_key="mykey")
        req = _make_request()
        mock_obj = MagicMock(pk=1)

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages"),
            # The reader resolves the VC sync device unconditionally (mirroring the
            # writers); identity here = the non-VC case.
            patch(
                "netbox_librenms_plugin.views.sync.interfaces.get_librenms_sync_device",
                side_effect=lambda obj, server_key=None: obj,
            ),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = None
            view.get_cached_ports_data(req, mock_obj, None)

        # get_cache_key should have been called with (obj, "ports", resolved_server_key)
        view.get_cache_key.assert_called_once_with(mock_obj, "ports", "mykey")


class TestInterfaceContextOOBRows:
    """get_context_data must not let OOB-controller rows hide / falsely-match main-device interfaces in the netbox-only reconciliation set."""

    def _make_view(self, cached_ports):
        from dcim.models import Device

        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        # get_context_data resolves select_related via self.model.__name__, so a
        # well-formed view under test must set it (a Device here, matching obj below).
        view.model = Device
        # Main device has an "idrac0" interface that LibreNMS only reports on the
        # OOB-controller side (same name) — it must still surface as netbox-only.
        iface = MagicMock(id=10, enabled=True, description="")
        iface.name = "idrac0"  # `name` is reserved in the MagicMock constructor
        iface.get_absolute_url.return_value = "/iface/10/"
        view._build_interface_lookup_maps = MagicMock(return_value={"by_name": {"idrac0": iface}, "by_librenms_id": {}})
        view.get_vlan_groups_for_device = MagicMock(return_value=[])
        view._build_vlan_lookup_maps = MagicMock(return_value={})
        view._add_vlan_group_selection = MagicMock()
        view._add_missing_vlans_info = MagicMock()
        table = MagicMock()
        view.get_table = MagicMock(return_value=table)
        view.get_cache_key = MagicMock(return_value="ports-key")
        view.get_last_fetched_key = MagicMock(return_value="lf-key")
        view.get_vlan_overrides_key = MagicMock(return_value="ov-key")
        return view, cached_ports

    @pytest.mark.django_db
    def test_oob_row_does_not_match_or_hide_host_interface(self):
        """An OOB row sharing a host interface name renders unmatched (not bound to the host interface) and still doesn't suppress that host interface from the netbox-only set."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        # Host owns its own idrac0; LibreNMS reports idrac0 only on the OOB-controller side.
        dev = make_device("oob-shared-host")
        make_interface(dev, "idrac0")

        # Real DeviceInterfaceTableView so the real get_interfaces + _build_interface_lookup_maps
        # + per-port reconciliation run; only peripheral plumbing (vlan helpers, table build,
        # cache-key derivation) is stubbed.
        view = object.__new__(DeviceInterfaceTableView)
        # Stub only the LibreNMS client boundary; these interfaces genuinely carry no stored id.
        view._librenms_api = MagicMock()
        view._librenms_api.get_stored_librenms_id.return_value = None
        view.get_vlan_groups_for_device = MagicMock(return_value=[])
        view._build_vlan_lookup_maps = MagicMock(return_value={})
        view._add_vlan_group_selection = MagicMock()
        view._add_missing_vlans_info = MagicMock()
        captured = {}
        view.get_table = lambda ports_data, *a, **k: captured.update(ports=ports_data) or MagicMock()
        view.get_cache_key = MagicMock(return_value="ports-key")
        view.get_last_fetched_key = MagicMock(return_value="lf-key")
        view.get_vlan_overrides_key = MagicMock(return_value="ov-key")

        cached = {"ports": [{"ifName": "idrac0", "_source": "oob", "port_id": 999, "ifSpeed": 1000000000}]}
        req = _make_request()

        def cache_get(key):
            return cached if key == "ports-key" else ({} if key == "ov-key" else None)

        with patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache:
            mock_cache.get.side_effect = cache_get
            mock_cache.ttl.return_value = None
            ctx = view.get_context_data(req, dev, "ifName", "default")

        oob_row = next(p for p in captured["ports"] if p.get("_source") == "oob")
        # The OOB row is a different LibreNMS device's port — it must NOT bind to the host's idrac0.
        assert oob_row["exists_in_netbox"] is False
        assert oob_row["netbox_interface"] is None
        # ...and the host's own idrac0 still surfaces as netbox-only (the OOB row doesn't suppress it).
        assert "idrac0" in {i["name"] for i in ctx["netbox_only_interfaces"]}

    def test_fresh_data_renders_without_reading_cache(self):
        """On the OOB-ports-fetch-failure path the (partial) cache is deleted, so get_context_data must render from the in-memory fresh_data snapshot instead of reading the now-empty cache — otherwise the table renders empty under a "showing host interfaces" banner."""
        view, fresh = self._make_view({"ports": [{"ifName": "idrac0", "_source": "oob", "port_id": 999}]})
        obj = MagicMock(id=1, name="host1")
        obj.virtual_chassis = None
        req = _make_request()

        with patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache:
            # Simulate the deleted cache: every cache.get returns None.
            mock_cache.get.return_value = None
            mock_cache.ttl.return_value = None
            ctx = view.get_context_data(req, obj, "ifName", "default", fresh_data=fresh)

        # The ports cache key must never be read — fresh_data short-circuits it.
        assert all(not (c.args and c.args[0] == "ports-key") for c in mock_cache.get.call_args_list), (
            "ports cache key was read despite fresh_data override"
        )
        # The fresh snapshot was still processed (the OOB row drove dedup) and a table built.
        assert "table" in ctx
        assert "idrac0" in {i["name"] for i in ctx["netbox_only_interfaces"]}

    def test_malformed_cached_port_snapshot_fails_closed(self):
        """A stale/corrupt cached ports snapshot (non-dict, or ports not a list of dicts) must be dropped and re-rendered empty, not 500 the sync tab — and the bad entry purged so a later render re-fetches."""
        view, _ = self._make_view({"ports": []})
        obj = MagicMock(id=1, name="host1")
        obj.virtual_chassis = None
        req = _make_request()

        for bad in ("garbage-string", {"ports": "not-a-list"}, {"ports": [42]}):
            with patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache:
                mock_cache.get.side_effect = lambda key, bad=bad: (
                    bad if key == "ports-key" else ({} if key == "ov-key" else None)
                )
                mock_cache.ttl.return_value = None
                ctx = view.get_context_data(req, obj, "ifName", "default")  # must not raise
            assert "table" in ctx
            assert any(c.args and c.args[0] == "ports-key" for c in mock_cache.delete.call_args_list)


# ===========================================================================
# SyncInterfacesView.post — full flows
# ===========================================================================


class TestSyncInterfacesViewPost:
    def test_permission_denied_returns_early(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=_denied_response())
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        req = _make_request(post_data={"select": ["Gi0/1"]})
        view.request = req
        result = view.post(req, "device", 1)
        assert result.status_code == 403

    def test_no_selection_redirects(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")
        view._librenms_api = mock_api  # blank-POST rebind reuses the cached client

        mock_device = MagicMock(pk=1)
        req = _make_request(post_data={})  # No selection

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            view.request = req
            view.post(req, "device", 1)

        mock_msgs.error.assert_called_once()
        mock_redirect.assert_called_once()

    def test_sync_selected_interfaces_skips_oob_rows(self):
        """OOB-controller rows are merged into the host list only for context and are never routed to a real device."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        synced = []
        view.sync_interface = lambda obj, port, ex, field: synced.append(port)
        ports_data = [
            {"ifName": "eth0", "port_id": 1, "_source": "main"},
            {"ifName": "eth0", "port_id": 99, "_source": "oob"},
        ]
        with patch("netbox_librenms_plugin.views.sync.interfaces.transaction"):
            view.sync_selected_interfaces(MagicMock(), ["eth0"], ports_data, [], "ifName")

        # Pre-fix both same-named rows matched the selection and synced (the OOB row overwrote
        # the host interface); now only the main row syncs.
        assert len(synced) == 1
        assert synced[0]["port_id"] == 1
        assert synced[0]["_source"] == "main"

    def test_cache_miss_redirects(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")
        view._librenms_api = mock_api  # blank-POST rebind reuses the cached client

        mock_device = MagicMock(pk=1)
        req = _make_request(post_data={"select": ["Gi0/1"]})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages"),
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch.object(type(view), "get_vlan_groups_for_device", return_value=[]),
            patch.object(view.__class__, "get_cache_key", return_value="k"),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = None
            view.request = req
            view.post(req, "device", 1)

        mock_redirect.assert_called()

    def test_device_post_success(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        ports = [{"ifName": "Gi0/1", "port_id": 10}]
        req = _make_request(post_data={"select": ["Gi0/1"], "server_key": "default"})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
            patch.object(view, "sync_interface"),
            patch.object(type(view), "get_vlan_groups_for_device", return_value=[]),
            patch.object(view.__class__, "get_cache_key", return_value="k"),
            patch.object(view.__class__, "_build_vlan_lookup_maps", return_value={}),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ports": ports}
            view.request = req
            view.post(req, "device", 1)

        mock_msgs.success.assert_called_once()
        mock_redirect.assert_called_once()

    def test_vm_post_success(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")

        mock_vm = MagicMock(pk=5)
        ports = [{"ifName": "eth0", "port_id": 20}]
        req = _make_request(post_data={"select": ["eth0"], "server_key": "default"})

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_vm,
            ),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
            patch.object(view, "sync_interface"),
            patch.object(type(view), "get_vlan_groups_for_device", return_value=[]),
            patch.object(view.__class__, "get_cache_key", return_value="k"),
            patch.object(view.__class__, "_build_vlan_lookup_maps", return_value={}),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ports": ports}
            view.request = req
            view.post(req, "virtualmachine", 5)

        mock_msgs.success.assert_called_once()
        mock_redirect.assert_called_once()

    def test_post_surfaces_skipped_conflicts_warning(self):
        """When an interface is skipped (port_id owned by another device), post() surfaces a warning naming it — not just a log line."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")

        mock_device = MagicMock(pk=1)
        ports = [{"ifName": "Gi0/1", "port_id": 10}]
        req = _make_request(post_data={"select": ["Gi0/1"], "server_key": "default"})

        def _record_skip(*args, **kwargs):
            view._skipped_conflicts.append("Gi0/1 (selected target unavailable)")

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect"),
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
            patch.object(view, "sync_interface", side_effect=_record_skip),
            patch.object(type(view), "get_vlan_groups_for_device", return_value=[]),
            patch.object(view.__class__, "get_cache_key", return_value="k"),
            patch.object(view.__class__, "_build_vlan_lookup_maps", return_value={}),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ports": ports}
            view.request = req
            view.post(req, "device", 1)

        mock_msgs.warning.assert_called_once()
        warning_msg = mock_msgs.warning.call_args[0][1]
        assert "Gi0/1" in warning_msg
        assert "selected target unavailable" in warning_msg
        assert "could not be safely matched" not in warning_msg
        mock_msgs.success.assert_called_once()


class TestSyncInterfacesViewServerKeyAndRedirect:
    """Issue #107: interface_name_field must be URL-escaped in the post-sync redirect.

    The POSTed server_key rebind / fail-closed behavior (#108/#109) is covered by
    test_coverage_sync_views.TestSyncInterfacesViewServerRebind, which exercises the same
    rebind_api_for_server seam SyncInterfacesView.post actually uses.
    """

    def _make_view(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        return view

    def _run_no_selection(self, view, mock_api, post_data, name_field="ifName"):
        """Drive post() down the no-selection redirect path (server_key + redirect_url are set before that), returning the redirect call mock."""
        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=MagicMock(pk=1),
            ),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value=name_field),
            patch("netbox_librenms_plugin.views.sync.interfaces.messages"),
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            # post() rebinds the client to the POSTed server via build_librenms_api (the fail-closed
            # seam); return a mock so a blank/valid key resolves without touching the DB or a real client.
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=mock_api),
        ):
            view.post(_make_request(post_data=post_data), "device", 1)
        return mock_redirect

    def test_interface_name_field_is_url_escaped_in_redirect(self):
        view = self._make_view()
        mock_api = MagicMock(server_key="default")
        mock_redirect = self._run_no_selection(view, mock_api, {}, name_field="ifName&injected=1")
        url = mock_redirect.call_args.args[0]
        assert "ifName%26injected%3D1" in url
        assert "ifName&injected=1" not in url


# ===========================================================================
# SyncInterfacesView.sync_interface — Device paths
# ===========================================================================


class TestSyncInterfacesViewSyncInterfaceDevice:
    def _make_view(self, request=None):
        """The real view; only its own next steps (attribute copy, VLAN sync) are stubbed."""
        view = _sync_view(request)
        view._lookup_maps = {}
        view.interface_name_field = "ifName"
        view.update_interface_attributes = MagicMock()
        view._sync_interface_vlans = MagicMock()
        return view

    @pytest.mark.django_db
    def test_device_interface_created(self):
        """End-to-end: sync_interface creates a real Interface on a real Device and persists the synced attributes (the real get_or_create + update_interface_attributes + save run)."""
        from dcim.models import Interface

        view = self._make_view()
        del view.update_interface_attributes  # exercise the real attribute sync + save
        dev = make_device("sync-create")
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1000000000,
            "ifAlias": "uplink",
            "ifMtu": 1500,
            "port_id": None,
            "ifAdminStatus": "up",
        }

        # exclude "vlans" so the (separately tested) VLAN sub-sync isn't exercised here.
        view.sync_interface(dev, librenms_port, ["vlans"], "ifName")

        iface = Interface.objects.get(device=dev, name="Gi0/1")
        assert iface.speed == 1000000
        assert iface.description == "uplink"
        assert iface.mtu == 1500
        assert iface.enabled is True
        assert iface.type  # a real NetBox type was resolved from ifType (non-empty)

    @pytest.mark.django_db
    def test_foreign_port_id_falls_back_to_local_same_named_interface(self):
        """A port_id owned by another device's interface still updates this device's own same-named interface."""
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id

        view = self._make_view()
        del view.update_interface_attributes  # run the real attribute sync + save

        # The device the user is syncing — it legitimately owns its own Gi0/1.
        dev = make_device("sync-fallback-own")
        own_iface = make_interface(dev, "Gi0/1")

        # A DIFFERENT device whose Gi0/1 carries the LibreNMS port_id (stale/duplicate id).
        other = make_device("sync-fallback-other")
        other_iface = make_interface(other, "Gi0/1")
        set_librenms_device_id(other_iface, 77, "default")
        other_iface.save()

        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1000000000,
            "ifAlias": "uplink-desc",
            "ifMtu": 9000,
            "port_id": 77,
            "ifAdminStatus": "up",
        }

        view.sync_interface(dev, librenms_port, ["vlans"], "ifName")

        own_iface.refresh_from_db()
        other_iface.refresh_from_db()
        # The current device's own interface was updated (not skipped).
        assert own_iface.mtu == 9000
        assert own_iface.description == "uplink-desc"
        assert own_iface.speed == 1000000
        # The other device's interface (the real owner of port_id 77) is untouched...
        assert other_iface.mtu != 9000
        assert get_librenms_device_id(other_iface, "default") == 77
        # ...and the port_id is NOT reassigned onto the current device's interface.
        assert get_librenms_device_id(own_iface, "default") is None

    def test_foreign_port_id_skip_is_recorded(self):
        """When the resolver returns None (port_id belongs to another device), the row is skipped and its name recorded in _skipped_conflicts for the post() summary."""
        from dcim.models import Device

        view = self._make_view()
        view._skipped_conflicts = []
        mock_device = MagicMock()
        mock_device.__class__ = Device
        mock_device.virtual_chassis = None

        librenms_port = {"ifName": "Gi0/1", "port_id": 99}
        view._resolve_device_interface = MagicMock(return_value=None)

        view.sync_interface(mock_device, librenms_port, [], "ifName")

        assert view._skipped_conflicts == ["Gi0/1 (port already mapped elsewhere or ambiguous)"]
        view.update_interface_attributes.assert_not_called()

    def test_device_selection_with_vc_valid(self):
        """A posted sibling of the same chassis receives the interface."""
        from dcim.models import Interface

        _vc, (host, sibling) = make_virtual_chassis_members("selvalid")
        view = self._make_view(_make_request(post_data={"device_selection_Gi0/1": str(sibling.pk)}))

        view.sync_interface(host, {"ifName": "Gi0/1", "port_id": None}, [], "ifName")

        assert Interface.objects.filter(device=sibling, name="Gi0/1").exists()
        assert not Interface.objects.filter(device=host, name="Gi0/1").exists()

    def test_device_selection_invalid_is_skipped(self):
        """A device that is neither the page device nor a VC sibling is refused."""
        from dcim.models import Interface

        dev = make_device("selinvalid-page")
        other = make_device("selinvalid-other")
        view = self._make_view(_make_request(post_data={"device_selection_Gi0/1": str(other.pk)}))
        view._skipped_conflicts = []

        view.sync_interface(dev, {"ifName": "Gi0/1", "port_id": None}, [], "ifName")

        assert not Interface.objects.filter(device=dev, name="Gi0/1").exists()
        assert not Interface.objects.filter(device=other, name="Gi0/1").exists()
        assert view._skipped_conflicts == ["Gi0/1 (selected target unavailable)"]

    def test_device_selection_does_not_exist_is_skipped(self):
        from dcim.models import Device, Interface

        dev = make_device("selgone-page")
        absent_pk = missing_pk(Device)
        view = self._make_view(_make_request(post_data={"device_selection_Gi0/1": str(absent_pk)}))
        view._skipped_conflicts = []

        view.sync_interface(dev, {"ifName": "Gi0/1", "port_id": None}, [], "ifName")

        assert not Interface.objects.filter(device=dev, name="Gi0/1").exists()
        assert view._skipped_conflicts == ["Gi0/1 (selected target unavailable)"]

    def test_explicit_vc_member_outside_grant_is_skipped(self):
        """A hidden posted target must not silently sync the row onto the page device."""
        from dcim.models import Device, Interface

        _vc, (host, sibling) = make_virtual_chassis_members("selhidden")
        user = make_user_with_perms(
            "selhidden-user",
            [("view", Device)],
            constraints={"pk": host.pk},
        )
        request = _make_request(
            post_data={"device_selection_Gi0/1": str(sibling.pk)},
            user=user,
        )
        view = self._make_view(request)
        view._skipped_conflicts = []

        view.sync_interface(host, {"ifName": "Gi0/1", "port_id": None}, [], "ifName")

        assert not Interface.objects.filter(name="Gi0/1").exists()
        assert view._skipped_conflicts == ["Gi0/1 (selected target unavailable)"]

    @pytest.mark.django_db
    def test_device_port_id_prefers_existing_librenms_id_match(self):
        """A port_id stored on this device's own interface updates that interface directly, no duplicate."""
        from dcim.models import Interface

        from netbox_librenms_plugin.utils import set_librenms_device_id

        view = self._make_view()
        del view.update_interface_attributes  # run the real attribute sync + save

        dev = make_device("sync-prefers-own")
        iface = make_interface(dev, "Gi0/1")
        set_librenms_device_id(iface, 42, "default")
        iface.save()

        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1000000000,
            "ifAlias": "by-id",
            "ifMtu": 1400,
            "port_id": 42,
            "ifAdminStatus": "up",
        }

        view.sync_interface(dev, librenms_port, ["vlans"], "ifName")

        iface.refresh_from_db()
        assert iface.mtu == 1400
        assert iface.description == "by-id"
        # The existing interface matched by port_id — no duplicate was created.
        assert Interface.objects.filter(device=dev, name="Gi0/1").count() == 1

    @pytest.mark.django_db
    def test_device_port_id_conflict_without_local_name_match_skips(self):
        """A port_id owned by another device with no same-named local interface is skipped, not created."""
        from dcim.models import Interface

        from netbox_librenms_plugin.utils import set_librenms_device_id

        view = self._make_view()
        view._skipped_conflicts = []
        del view.update_interface_attributes

        dev = make_device("sync-conflict-nolocal")  # deliberately has NO Gi0/1
        other = make_device("sync-conflict-other")
        other_iface = make_interface(other, "Gi0/1")
        set_librenms_device_id(other_iface, 77, "default")
        other_iface.save()

        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1000000000,
            "ifMtu": 9000,
            "port_id": 77,
            "ifAdminStatus": "up",
        }
        view.sync_interface(dev, librenms_port, ["vlans"], "ifName")

        # No same-named local interface to fall back to, and we never get_or_create one here.
        assert view._skipped_conflicts == ["Gi0/1 (port already mapped elsewhere or ambiguous)"]
        assert not Interface.objects.filter(device=dev, name="Gi0/1").exists()


class TestSyncInterfacesViewSyncInterfaceVM:
    def test_vm_interface_created(self):
        from virtualization.models import VMInterface

        vm = make_vm("vmsync-created")
        view = _sync_view()
        view._lookup_maps = {}
        view.update_interface_attributes = MagicMock()
        view._sync_interface_vlans = MagicMock()

        view.sync_interface(vm, {"ifName": "eth0", "port_id": None}, [], "ifName")

        assert VMInterface.objects.filter(virtual_machine=vm, name="eth0").exists()
        view.update_interface_attributes.assert_called_once()

    def test_vm_port_id_prefers_existing_librenms_id_match(self):
        """A port_id already stored on this VM's interface updates it; no second interface."""
        from virtualization.models import VMInterface

        vm = make_vm("vmsync-portid")
        matched = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        matched.custom_field_data["librenms_id"] = {"default": 55}
        matched.save()
        view = _sync_view()
        view._lookup_maps = {}
        view.update_interface_attributes = MagicMock()
        view._sync_interface_vlans = MagicMock()
        librenms_port = {"ifName": "renamed-in-librenms", "port_id": 55}

        view.sync_interface(vm, librenms_port, [], "ifName")

        assert VMInterface.objects.filter(virtual_machine=vm).count() == 1
        view.update_interface_attributes.assert_called_once_with(matched, librenms_port, None, [], "ifName")

    def test_invalid_obj_raises_value_error(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request()
        librenms_port = {"ifName": "eth0"}

        with pytest.raises(ValueError):
            view.sync_interface(MagicMock(), librenms_port, [], "ifName")


class TestSyncInterfacesViewUpdateInterfaceAttributes:
    def _make_view(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request()
        view._post_server_key = "default"
        view.handle_mac_address = MagicMock()  # MAC parsing has its own dedicated test class
        return view

    def test_basic_attributes_set(self):
        """Real Interface: the LibreNMS→NetBox field mapping is applied and persisted; the real convert_speed_to_kbps runs (bps→kbps)."""
        from dcim.models import Interface

        view = self._make_view()
        dev = make_device("intf-attrs")
        interface = make_interface(dev, "Gi0/0")
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1000000000,
            "ifAlias": "uplink",
            "ifMtu": 1500,
            "port_id": None,
            "ifAdminStatus": "up",
        }

        view.update_interface_attributes(interface, librenms_port, "1000base-t", [], "ifName")

        reloaded = Interface.objects.get(pk=interface.pk)
        assert reloaded.name == "Gi0/1"
        assert reloaded.type == "1000base-t"
        assert reloaded.speed == 1000000  # 1e9 bps → 1e6 kbps via the real convert_speed_to_kbps
        assert reloaded.description == "uplink"
        assert reloaded.mtu == 1500
        assert reloaded.enabled is True

    def test_excluded_columns_skipped(self):
        """Excluded columns are left untouched on a real Interface (verified via reload)."""
        from dcim.models import Interface

        view = self._make_view()
        dev = make_device("intf-excluded")
        interface = make_interface(dev, "orig-name", iface_type="1000base-t")
        interface.mtu = 9000
        interface.description = "keep-me"
        interface.save()
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 0,
            "ifAlias": "would-change",
            "ifMtu": 1500,
            "port_id": None,
            "ifAdminStatus": None,
        }

        view.update_interface_attributes(
            interface,
            librenms_port,
            "other",
            ["name", "type", "speed", "description", "mtu", "enabled", "mac_address"],
            "ifName",
        )

        # MAC handler must not be invoked when "mac_address" is excluded.
        view.handle_mac_address.assert_not_called()
        # Excluded attributes keep their pre-update values.
        reloaded = Interface.objects.get(pk=interface.pk)
        assert reloaded.name == "orig-name"
        assert reloaded.type == "1000base-t"
        assert reloaded.mtu == 9000
        assert reloaded.description == "keep-me"

    def test_admin_status_down_sets_disabled(self):
        from dcim.models import Interface

        view = self._make_view()
        dev = make_device("intf-down")
        interface = make_interface(dev, "Gi0/0")
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": None,
            "ifSpeed": None,
            # ifAlias "" (not None): a None ifAlias would set description=None and trip the
            # NOT-NULL constraint on a real save — see note. Empty string is the safe no-op.
            "ifAlias": "",
            "ifMtu": None,
            "port_id": None,
            "ifAdminStatus": "down",
        }

        # netbox_type "other" (the real get_netbox_interface_type fallback); passing None would
        # set the NOT-NULL type column to NULL — another real-save constraint the mock hid.
        view.update_interface_attributes(interface, librenms_port, "other", [], "ifName")

        assert Interface.objects.get(pk=interface.pk).enabled is False

    def test_port_id_calls_set_librenms_device_id(self):
        from dcim.models import Interface

        view = self._make_view()
        interface = MagicMock()
        interface.__class__ = Interface
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": None,
            "ifSpeed": None,
            "ifAlias": None,
            "ifMtu": None,
            "port_id": 42,
            "ifAdminStatus": "up",
        }

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None),
            patch("netbox_librenms_plugin.views.sync.interfaces.find_by_librenms_id", return_value=None),
            patch("netbox_librenms_plugin.views.sync.interfaces.set_librenms_device_id") as mock_set,
        ):
            view.update_interface_attributes(interface, librenms_port, None, [], "ifName")

        mock_set.assert_called_once_with(interface, 42, "default")

    def test_port_id_conflict_does_not_overwrite(self):
        from dcim.models import Interface

        view = self._make_view()
        interface = MagicMock()
        interface.__class__ = Interface
        interface.pk = 1
        conflicting_owner = MagicMock()
        conflicting_owner.pk = 2
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": None,
            "ifSpeed": None,
            "ifAlias": None,
            "ifMtu": None,
            "port_id": 42,
            "ifAdminStatus": "up",
        }

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None),
            patch("netbox_librenms_plugin.views.sync.interfaces.find_by_librenms_id", return_value=conflicting_owner),
            patch("netbox_librenms_plugin.views.sync.interfaces.set_librenms_device_id") as mock_set,
        ):
            view.update_interface_attributes(interface, librenms_port, None, [], "ifName")

        mock_set.assert_not_called()

    def test_ifalias_not_set_when_same_as_name(self):
        """ifAlias should not overwrite when equal to interface name."""
        from dcim.models import Interface

        view = self._make_view()
        # MagicMock(spec=Interface) with explicit init so plain assignments are detectable
        interface = MagicMock(spec=Interface)
        interface.description = None
        interface.save = MagicMock()
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": None,
            "ifSpeed": None,
            "ifAlias": "Gi0/1",  # Same as interface name → should not set description
            "ifMtu": None,
            "port_id": None,
            "ifAdminStatus": "up",
        }

        with patch("netbox_librenms_plugin.views.sync.interfaces.convert_speed_to_kbps", return_value=None):
            view.update_interface_attributes(interface, librenms_port, None, [], "ifName")

        # description should remain None since ifAlias == interface_name
        assert interface.description is None


# ===========================================================================
# SyncInterfacesView._sync_interface_vlans
# ===========================================================================


class TestSyncInterfacesViewSyncInterfaceVlans:
    def test_no_vlans_calls_update_with_empty(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request()
        view._lookup_maps = {}
        view._update_interface_vlan_assignment = MagicMock()

        interface = MagicMock()
        librenms_port = {}

        view._sync_interface_vlans(interface, librenms_port, "Gi0/1")

        view._update_interface_vlan_assignment.assert_called_once()

    def test_with_vlans_builds_group_map(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.request = _make_request(post_data={"vlan_group_Gi0_1_100": "5"})
        view._lookup_maps = {}
        view._update_interface_vlan_assignment = MagicMock()

        interface = MagicMock()
        librenms_port = {"untagged_vlan": 100, "tagged_vlans": [200]}

        view._sync_interface_vlans(interface, librenms_port, "Gi0/1")

        call_args = view._update_interface_vlan_assignment.call_args
        vlan_group_map = call_args[0][2]
        assert vlan_group_map.get("100") == "5"


# ===========================================================================
# SyncInterfacesView.sync_selected_interfaces
# ===========================================================================


class TestSyncInterfacesViewSyncSelected:
    def test_syncs_matching_ports(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.interface_name_field = "ifName"
        view.sync_interface = MagicMock()

        ports_data = [{"ifName": "Gi0/1"}, {"ifName": "Gi0/2"}]
        selected = ["Gi0/1"]

        with patch("netbox_librenms_plugin.views.sync.interfaces.transaction"):
            view.sync_selected_interfaces(MagicMock(), selected, ports_data, [], "ifName")

        assert view.sync_interface.call_count == 1
        call_args = view.sync_interface.call_args
        assert call_args[0][1]["ifName"] == "Gi0/1"

    def test_syncs_port_selected_by_stable_id(self):
        """A port whose display name is not in 'select' is still synced when its port_id
        is in select_port_id (e.g. a cross-page parent auto-included by the LAG/parent JS)."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.interface_name_field = "ifName"
        view.sync_interface = MagicMock()
        view._selected_port_ids = {"42"}

        ports_data = [{"ifName": "Gi0/1", "port_id": 7}, {"ifName": "Gi0/2", "port_id": 42}]
        selected = ["Gi0/1"]  # Gi0/2 is selected only by stable port_id, not by name

        with patch("netbox_librenms_plugin.views.sync.interfaces.transaction"):
            view.sync_selected_interfaces(MagicMock(), selected, ports_data, [], "ifName")

        synced = sorted(c[0][1]["ifName"] for c in view.sync_interface.call_args_list)
        assert synced == ["Gi0/1", "Gi0/2"]


# ===========================================================================
# SyncInterfacesView._sync_lag_and_parent_relationships
# ===========================================================================


class TestSyncLagAndParentRelationships:
    """Outcome tests for the bulk LAG/parent relationship sync, driven against real NetBox
    Device/Interface objects (the real _resolve_interface_by_port_id, _interfaces_same_owner and
    Interface.full_clean run) so the linking — and the new per-row owner pinning — is verified
    end-to-end rather than re-asserting mock calls."""

    @staticmethod
    def _make_device():
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        site = Site.objects.create(name="LagSite", slug="lag-site")
        mfr = Manufacturer.objects.create(name="LagMfr", slug="lag-mfr")
        dtype = DeviceType.objects.create(manufacturer=mfr, model="LagDT", slug="lag-dt")
        role = DeviceRole.objects.create(name="LagRole", slug="lag-role", color="0000ff")
        return Device.objects.create(name="lag-dev", device_type=dtype, role=role, site=site, status="active")

    @staticmethod
    def _iface(device, name, port_id, itype="1000base-t"):
        from dcim.models import Interface

        from netbox_librenms_plugin.utils import set_librenms_device_id

        iface = Interface.objects.create(device=device, name=name, type=itype)
        set_librenms_device_id(iface, port_id, "default")
        iface.save()
        return iface

    def _make_view(self, name_field="ifName", selected_port_ids=None):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.interface_name_field = name_field
        view.request = MagicMock()
        view.request.POST = {}  # no per-row device_selection → owner defaults to the page device
        if selected_port_ids is not None:
            view._selected_port_ids = set(selected_port_ids)
        return view

    def test_duplicate_display_name_members_not_collapsed(self, db):
        """In ifDescr mode two member ports share a display name; both must still be linked to
        their LAG. Keying selection by port_id (not the colliding name) is the fix."""
        device = self._make_device()
        m1 = self._iface(device, "Gi0/1", 10)
        m2 = self._iface(device, "Gi0/2", 11)
        agg = self._iface(device, "Po1", 100, itype="lag")

        ports_data = [
            {"ifDescr": "Ethernet", "ifName": "Gi0/1", "port_id": 10},
            {"ifDescr": "Ethernet", "ifName": "Gi0/2", "port_id": 11},
            {"ifDescr": "Po1", "ifName": "Po1", "port_id": 100},
        ]
        relationships = {"lag_members": {"10": 100, "11": 100}, "sub_interfaces": {}}

        view = self._make_view(name_field="ifDescr")
        view._sync_lag_and_parent_relationships(device, ["Ethernet"], ports_data, relationships, "default")

        m1.refresh_from_db()
        m2.refresh_from_db()
        assert m1.lag_id == agg.pk
        assert m2.lag_id == agg.pk

    def test_member_selected_only_by_stable_id_is_linked(self, db):
        """A port present only via select_port_id (not selected by display name) is still
        processed by the relationship sync."""
        device = self._make_device()
        member = self._iface(device, "Gi0/2", 11)
        agg = self._iface(device, "Po1", 100, itype="lag")

        ports_data = [
            {"ifName": "Gi0/2", "port_id": 11},
            {"ifName": "Po1", "port_id": 100},
        ]
        relationships = {"lag_members": {"11": 100}, "sub_interfaces": {}}

        view = self._make_view(name_field="ifName", selected_port_ids={"11"})
        view._sync_lag_and_parent_relationships(device, [], ports_data, relationships, "default")

        member.refresh_from_db()
        assert member.lag_id == agg.pk

    def test_invalid_lag_link_rejected_by_full_clean_is_skipped(self, db):
        """A relationship that fails Interface.full_clean() (a self-LAG from stale/crafted
        port_stack data) must be skipped, not persisted."""
        device = self._make_device()
        member = self._iface(device, "Gi0/2", 11)

        ports_data = [{"ifName": "Gi0/2", "port_id": 11}]
        # Self-LAG: the member's aggregate resolves back to itself (port_id 11 → 11), which
        # Interface.full_clean() rejects.
        relationships = {"lag_members": {"11": 11}, "sub_interfaces": {}}

        view = self._make_view(name_field="ifName", selected_port_ids={"11"})
        view._sync_lag_and_parent_relationships(device, [], ports_data, relationships, "default")

        member.refresh_from_db()
        assert member.lag_id is None  # invalid self-LAG was not persisted


# A POSTed valid non-default server_key must scope the sync to that server without 500ing on a
# misconfigured default client. Under the stack that behavior comes from rebind_api_for_server, so
# its coverage lives with the rebind seam in
# test_coverage_sync_views.TestSyncInterfacesViewServerRebind
# (test_posted_server_key_is_bound_for_the_sync / test_stale_server_key_fails_closed_without_sync).

# ===========================================================================
# _resolve_interface_by_port_id: correct librenms_id dict lookup
# ===========================================================================


class TestResolveInterfaceByPortId:
    """The function must correctly read the nested {'server_key': port_id} dict format."""

    def test_finds_interface_by_server_keyed_dict(self):
        """When librenms_id = {'production': 42}, resolves for port_id=42 and server_key='production'."""
        from unittest.mock import MagicMock, patch
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id
        from dcim.models import Device, Interface

        mock_device = MagicMock(spec=Device)
        mock_iface = MagicMock(spec=Interface)

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.get_librenms_device_id") as mock_get_id,
        ):
            mock_intf_cls.objects.filter.return_value = [mock_iface]
            mock_get_id.return_value = 42  # correctly extracts 42 from {"production": 42}

            iface, err = _resolve_interface_by_port_id(mock_device, "42", "production")

        assert err is None
        assert iface is mock_iface
        mock_get_id.assert_called_once_with(mock_iface, "production", auto_save=False)

    def test_returns_error_when_not_found(self):
        """Returns (None, error) when no interface has matching port_id."""
        from unittest.mock import MagicMock, patch
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id
        from dcim.models import Device

        mock_device = MagicMock(spec=Device)

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.get_librenms_device_id", return_value=None),
        ):
            mock_intf_cls.objects.filter.return_value = [MagicMock()]

            iface, err = _resolve_interface_by_port_id(mock_device, "99", "production")

        assert iface is None
        assert err is not None

    def test_name_hint_fallback_when_no_librenms_id(self):
        """Falls back to name lookup when no interface has matching librenms_id."""
        from unittest.mock import MagicMock, patch
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id
        from dcim.models import Device, Interface

        mock_device = MagicMock(spec=Device)
        mock_device.virtual_chassis = None
        mock_iface_by_name = MagicMock(spec=Interface)

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.get_librenms_device_id", return_value=None),
        ):
            mock_intf_cls.objects.filter.return_value = []
            mock_intf_cls.objects.get.return_value = mock_iface_by_name

            iface, err = _resolve_interface_by_port_id(mock_device, "42", "production", name_hint="lag-1")

        assert err is None
        assert iface is mock_iface_by_name
        mock_intf_cls.objects.get.assert_called_once_with(device=mock_device, name="lag-1")

    def test_ambiguous_port_id_returns_error_not_first_match(self):
        """Two interfaces carrying the same stale librenms_id must fail as ambiguous,
        not silently bind lag/parent to whichever happens to be first."""
        from unittest.mock import MagicMock, patch
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id
        from dcim.models import Device, Interface

        mock_device = MagicMock(spec=Device)
        iface_a = MagicMock(spec=Interface)
        iface_b = MagicMock(spec=Interface)

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.get_librenms_device_id", return_value=42),
        ):
            mock_intf_cls.objects.filter.return_value = [iface_a, iface_b]
            iface, err = _resolve_interface_by_port_id(mock_device, "42", "production")

        assert iface is None
        assert err is not None
        assert "ambiguous" in err.lower()

    def test_name_hint_does_not_exist_falls_through_to_not_found(self):
        """A name-hint miss (DoesNotExist) is swallowed and reported as not-found."""
        from unittest.mock import MagicMock, patch
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id
        from dcim.models import Device, Interface

        mock_device = MagicMock(spec=Device)
        mock_device.virtual_chassis = None

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.get_librenms_device_id", return_value=None),
        ):
            mock_intf_cls.objects.filter.return_value = []
            # Wire the real exception classes so the narrowed except clause is valid.
            mock_intf_cls.DoesNotExist = Interface.DoesNotExist
            mock_intf_cls.MultipleObjectsReturned = Interface.MultipleObjectsReturned
            mock_intf_cls.objects.get.side_effect = Interface.DoesNotExist
            iface, err = _resolve_interface_by_port_id(mock_device, "42", "production", name_hint="lag-1")

        assert iface is None
        assert err is not None
        assert "not found" in err.lower()

    def test_name_hint_multiple_matches_returns_ambiguous(self):
        """A name-hint matching multiple interfaces returns an ambiguity error, not a silent
        not-found — the narrowed except surfaces MultipleObjectsReturned distinctly."""
        from unittest.mock import MagicMock, patch
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id
        from dcim.models import Device, Interface

        mock_device = MagicMock(spec=Device)
        mock_device.virtual_chassis = None

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.get_librenms_device_id", return_value=None),
        ):
            mock_intf_cls.objects.filter.return_value = []
            mock_intf_cls.DoesNotExist = Interface.DoesNotExist
            mock_intf_cls.MultipleObjectsReturned = Interface.MultipleObjectsReturned
            mock_intf_cls.objects.get.side_effect = Interface.MultipleObjectsReturned
            iface, err = _resolve_interface_by_port_id(mock_device, "42", "production", name_hint="lag-1")

        assert iface is None
        assert err is not None
        assert "ambiguous" in err.lower()

    def test_name_hint_unexpected_error_propagates(self):
        """A real DB/runtime fault during the name-hint lookup must propagate, not be masked
        as a silent not-found (the old bare `except Exception` hid these faults)."""
        import pytest
        from unittest.mock import MagicMock, patch
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id
        from dcim.models import Device, Interface

        mock_device = MagicMock(spec=Device)
        mock_device.virtual_chassis = None

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls,
            patch("netbox_librenms_plugin.views.sync.interfaces.get_librenms_device_id", return_value=None),
        ):
            mock_intf_cls.objects.filter.return_value = []
            mock_intf_cls.DoesNotExist = Interface.DoesNotExist
            mock_intf_cls.MultipleObjectsReturned = Interface.MultipleObjectsReturned
            mock_intf_cls.objects.get.side_effect = RuntimeError("database is down")
            with pytest.raises(RuntimeError):
                _resolve_interface_by_port_id(mock_device, "42", "production", name_hint="lag-1")


class TestResolveInterfaceByPortIdExpectedOwner:
    """Real-DB coverage for the expected_owner guard in _resolve_interface_by_port_id.

    The librenms_id search spans every VC member, so a stale/reused id can resolve uniquely onto
    a *different* member than the row was synced to. Exercised against real VC + Interface objects
    (not mocks) so the cross-member resolution and the owner check are validated end-to-end.
    Each test takes pytest-django's ``db`` fixture to enable real database access.
    """

    @staticmethod
    def _make_vc_members():
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site, VirtualChassis

        site = Site.objects.create(name="VCSite", slug="vc-site")
        mfr = Manufacturer.objects.create(name="VCMfr", slug="vc-mfr")
        dtype = DeviceType.objects.create(manufacturer=mfr, model="VCDT", slug="vc-dt")
        role = DeviceRole.objects.create(name="VCRole", slug="vc-role", color="00ff00")
        vc = VirtualChassis.objects.create(name="VC-1")
        common = {"device_type": dtype, "role": role, "site": site, "status": "active", "virtual_chassis": vc}
        member1 = Device.objects.create(name="vc-m1", vc_position=1, **common)
        member2 = Device.objects.create(name="vc-m2", vc_position=2, **common)
        return member1, member2

    @staticmethod
    def _iface_with_librenms_id(device, name, port_id, server_key="default"):
        from dcim.models import Interface

        from netbox_librenms_plugin.utils import set_librenms_device_id

        iface = Interface.objects.create(device=device, name=name, type="1000base-t")
        set_librenms_device_id(iface, port_id, server_key)
        iface.save()
        return iface

    def test_rejects_match_on_a_different_vc_member(self, db):
        """port_id resolves uniquely onto member2's interface; resolving from member1 with
        expected_owner=member1 must reject it (the fix) — and accept it without the guard."""
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id

        member1, member2 = self._make_vc_members()
        iface2 = self._iface_with_librenms_id(member2, "Gi0/1", 42)

        # Without the guard: the VC-wide search finds member2's interface (the latent bug).
        found, err = _resolve_interface_by_port_id(member1, "42", "default")
        assert err is None
        assert found == iface2

        # With expected_owner pinned to member1: the foreign-member match is rejected.
        found, err = _resolve_interface_by_port_id(member1, "42", "default", expected_owner=(member1.pk, None))
        assert found is None
        assert err and "different owner" in err

        # With expected_owner matching the real owner (member2): accepted.
        found, err = _resolve_interface_by_port_id(member1, "42", "default", expected_owner=(member2.pk, None))
        assert err is None
        assert found == iface2

    def test_accepts_match_on_the_expected_member(self, db):
        """An interface that genuinely lives on the expected member resolves cleanly."""
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id

        member1, _ = self._make_vc_members()
        iface1 = self._iface_with_librenms_id(member1, "Gi0/1", 7)

        found, err = _resolve_interface_by_port_id(member1, "7", "default", expected_owner=(member1.pk, None))
        assert err is None
        assert found == iface1


class TestInterfaceLinkValidationErrorNoStackTrace:
    """LAG/parent full_clean() failures return a fixed message and log the detail —
    the exception text must not be echoed to the client (CodeQL py/stack-trace-exposure)."""

    _SENTINEL = "SENSITIVE_VALIDATION_INTERNALS"

    def test_lag_link_validation_error_does_not_leak_exception(self):
        import json

        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView, _PortIdResolveMixin

        view = object.__new__(SyncInterfaceLagView)
        view.require_all_permissions_json = MagicMock(return_value=None)
        view._librenms_api = MagicMock(server_key="default")

        member = MagicMock()
        member.name = "Et1"
        member.full_clean.side_effect = ValidationError(self._SENTINEL)
        agg = MagicMock()
        agg.name = "Po1"
        agg.type = "lag"

        req = _make_request({"port_id": "1", "lag_port_id": "2", "server_key": "default"})

        with (
            patch.object(SyncInterfaceLagView, "_get_object", return_value=MagicMock()),
            patch.object(
                _PortIdResolveMixin,
                "_resolve_interface_by_port_id",
                side_effect=[(member, None), (agg, None)],
            ),
            patch("netbox_librenms_plugin.views.sync.interfaces._interfaces_same_owner", return_value=True),
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
            patch("netbox_librenms_plugin.views.sync.interfaces.logger") as mock_logger,
        ):
            resp = view.post(req, object_type="device", object_id=1)

        assert resp.status_code == 409
        assert self._SENTINEL not in resp.content.decode()
        body = json.loads(resp.content)
        assert "Cannot link Et1 to LAG Po1" in body["error"]
        # The real detail is logged server-side, not lost.
        assert any(self._SENTINEL in str(c.args) for c in mock_logger.warning.call_args_list)

    def test_parent_link_validation_error_does_not_leak_exception(self):
        import json

        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView, _PortIdResolveMixin

        view = object.__new__(SyncInterfaceParentView)
        view.require_all_permissions_json = MagicMock(return_value=None)
        view._librenms_api = MagicMock(server_key="default")

        child = MagicMock()
        child.name = "Et1.100"
        child.full_clean.side_effect = ValidationError(self._SENTINEL)
        parent = MagicMock()
        parent.name = "Et1"

        req = _make_request({"port_id": "1", "parent_port_id": "2", "server_key": "default"})

        with (
            patch.object(SyncInterfaceParentView, "_get_object", return_value=MagicMock()),
            patch.object(
                _PortIdResolveMixin,
                "_resolve_interface_by_port_id",
                side_effect=[(child, None), (parent, None)],
            ),
            patch("netbox_librenms_plugin.views.sync.interfaces._interfaces_same_owner", return_value=True),
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
            patch("netbox_librenms_plugin.views.sync.interfaces.logger") as mock_logger,
        ):
            resp = view.post(req, object_type="device", object_id=1)

        assert resp.status_code == 409
        assert self._SENTINEL not in resp.content.decode()
        body = json.loads(resp.content)
        assert "Cannot link Et1.100 to parent Et1" in body["error"]
        assert any(self._SENTINEL in str(c.args) for c in mock_logger.warning.call_args_list)
