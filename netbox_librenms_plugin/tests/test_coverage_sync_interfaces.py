"""
Coverage tests for views/sync/interfaces.py

SyncInterfacesView + DeleteNetBoxInterfacesView
Target: 95%+ coverage
"""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.tests.conftest import (
    make_device,
    make_interface,
    make_virtual_chassis_members,
    make_vm,
)
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_user_with_perms,
    make_view,
    missing_pk,
    post as _post,
)

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


def _bump_synced(view):
    """Simulate the real sync_interface: bump the post()-level synced counter for a resolved port."""
    view._synced_count += 1


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
    """SyncInterfaceParentView supports VMs (VMInterface has a parent field), so its POST permission must be scoped to the object type, not hardcoded to Interface."""

    def _stop_after_perms(self):
        """Patch the JSON permission gate to short-circuit post() right after the dynamic permission dict is set, returning a sentinel response."""
        return patch.object(
            __import__(
                "netbox_librenms_plugin.views.sync.interfaces", fromlist=["SyncInterfaceParentView"]
            ).SyncInterfaceParentView,
            "require_all_permissions_json",
            return_value=_denied_response(),
        )

    def test_device_post_requires_interface_change(self):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        view = object.__new__(SyncInterfaceParentView)
        with self._stop_after_perms():
            view.post(_make_request(), "device", 1)
        # view_device too: the owner lookup resolves through a restricted queryset, so the gate
        # must state the read the endpoint actually performs instead of 404ing on it.
        assert view.required_object_permissions["POST"] == [("view", Device), ("change", Interface)]

    def test_vm_post_requires_vminterface_change(self):
        from virtualization.models import VirtualMachine, VMInterface

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        view = object.__new__(SyncInterfaceParentView)
        with self._stop_after_perms():
            view.post(_make_request(), "virtualmachine", 1)
        assert view.required_object_permissions["POST"] == [("view", VirtualMachine), ("change", VMInterface)]

    def test_invalid_type_raises_http404(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView
        from django.http import Http404
        import pytest

        view = object.__new__(SyncInterfaceParentView)
        with pytest.raises(Http404):
            view.post(_make_request(), "invalid", 1)


class TestSyncInterfaceLagViewPermissions:
    def test_post_permissions_are_resolved_by_the_shared_base(self):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        assert "required_object_permissions" not in SyncInterfaceLagView.__dict__
        view = object.__new__(SyncInterfaceLagView)
        with patch.object(SyncInterfaceLagView, "require_all_permissions_json", return_value=_denied_response()):
            view.post(_make_request(), "device", 1)
        assert view.required_object_permissions["POST"] == [("view", Device), ("change", Interface)]


def _js_source():
    from pathlib import Path

    import netbox_librenms_plugin

    return (
        Path(netbox_librenms_plugin.__file__).parent / "static" / "netbox_librenms_plugin" / "js" / "librenms_sync.js"
    ).read_text(encoding="utf-8")


def _js_block(source, start_anchor, end_anchor=None):
    """Slice a handler block by its anchor comments, failing with a clear message (not a bare ValueError) when an anchor was reworded or the block moved."""
    assert start_anchor in source, f"anchor comment missing from librenms_sync.js: {start_anchor!r}"
    start = source.index(start_anchor)
    if end_anchor is None:
        return source[start:]
    assert end_anchor in source[start:], f"anchor comment missing from librenms_sync.js: {end_anchor!r}"
    return source[start : source.index(end_anchor, start)]


def test_relationship_handler_reuses_shared_csrf_helper():
    import re

    handler = _js_block(_js_source(), "// Event delegation for LAG and parent interface sync buttons.")
    # Regexes tolerate spacing/quote reformatting; the tokens themselves are the contract.
    assert re.search(r"const\s+csrf\s*=\s*getCsrfToken\(\)", handler), "handler must use the shared getCsrfToken()"
    assert not re.search(r"querySelector\(\s*['\"]\[name=csrfmiddlewaretoken\]", handler), (
        "handler must not re-query the raw csrf input"
    )


def test_reenabling_relationship_autoselect_replays_checked_rows():
    """Checked children rebuild cross-page parent inputs when auto-select is re-enabled."""
    import re

    handler = _js_block(
        _js_source(),
        "// Keep injected cross-page parents symmetric",
        "Show a brief inline notice",
    )
    assert re.search(r"toggle\.matches\(\s*['\"]#autoSelectLagMembers['\"]\s*\)", handler), (
        "toggle handler must key on #autoSelectLagMembers"
    )
    assert re.search(r"if\s*\(\s*toggle\.checked\s*\)", handler), "re-enable branch must gate on toggle.checked"
    # Backreference pins the string CLOSING right after :checked — a suffix like :not(*)
    # (valid CSS, matches nothing) must fail this, not slip past a prefix check.
    assert re.search(r"querySelectorAll\(\s*(['\"])input\[name=.select.\]:checked\1\s*\)", handler), (
        "re-enable branch must replay exactly the checked rows"
    )
    assert re.search(r"dispatchEvent\(\s*new\s+Event\(\s*['\"]change['\"]\s*,\s*\{\s*bubbles:\s*true", handler), (
        "replay must re-dispatch a bubbling change event"
    )


def test_cross_page_parent_selectors_are_css_escaped():
    """The injected-parent lookups build selectors from data-parent-port-id; they must go through CSS.escape (like the notice code) so an unexpected id value can't throw a SyntaxError and abort the handler."""
    import re

    handler = _js_block(
        _js_source(),
        "// --- Sub-interface: select parent when checking ---",
        "// Keep injected cross-page parents symmetric",
    )
    assert re.search(r"querySelector\(\s*'#'\s*\+\s*CSS\.escape\(", handler), (
        "id selectors for injected parents must be CSS.escape'd"
    )
    assert re.search(r"data-parent-port-id=\"'\s*\+\s*CSS\.escape\(parentPortId\)", handler), (
        "the sibling-row attribute selector must CSS.escape the port id"
    )
    # Pin BOTH cleanup lookups individually — the injection lookup alone must not be able
    # to satisfy this test while an unescaped cleanup selector sneaks back in.
    assert re.search(r"CSS\.escape\(\s*'auto-parent-'\s*\+\s*parentPortId\s*\)", handler), (
        "the cleanup lookup for the injected parent input must CSS.escape its id"
    )
    assert re.search(r"CSS\.escape\(\s*'auto-parent-dev-'\s*\+\s*parentPortId\s*\)", handler), (
        "the cleanup lookup for the device-override input must CSS.escape its id"
    )
    # '#…' catches both the bare '#' + value form and a raw '#auto-parent-…' + value concat.
    assert not re.search(r"querySelector\(\s*'#[^']*'\s*\+(?!\s*CSS\.escape)", handler), (
        "no raw '#…' + value selector may remain in this block"
    )


@pytest.mark.django_db
class TestInterfacesSameOwnerGuard:
    """_interfaces_same_owner gates lag/parent links so a port_stack relationship that resolves across two VC members can't persist a NetBox-forbidden cross-device link."""

    def test_same_device_is_true(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.interfaces import _interfaces_same_owner

        device = make_device("same-owner")
        a = make_interface(device, "Gi0/1")
        b = make_interface(device, "Gi0/2")
        assert _interfaces_same_owner(a, b) is True

    def test_different_device_is_false(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.interfaces import _interfaces_same_owner

        a = make_interface(make_device("owner-a"), "Gi0/1")
        b = make_interface(make_device("owner-b"), "Gi0/1")
        assert _interfaces_same_owner(a, b) is False

    def test_same_vm_is_true(self):
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.tests.conftest import make_vm
        from netbox_librenms_plugin.views.sync.interfaces import _interfaces_same_owner

        vm = make_vm("same-vm")
        a = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        b = VMInterface.objects.create(virtual_machine=vm, name="eth1")
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


@pytest.mark.django_db
class TestInterfaceContextOOBRows:
    """get_context_data must not let OOB-controller rows hide / falsely-match main-device interfaces in the netbox-only reconciliation set."""

    def _make_view(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        # Concrete device view: its real get_interfaces()/get_select_related_field() drive the
        # real _build_interface_lookup_maps over obj.interfaces, and the real
        # get_stored_librenms_id (a local custom-field/cache read — NOT an HTTP call) resolves
        # idrac0's stored id to None for real. A real LibreNMSAPI is built under a config patch;
        # __init__ only reads config, so no network is touched.
        view = object.__new__(DeviceInterfaceTableView)
        servers = {
            "default": {
                "librenms_url": "https://librenms.example.com",
                "api_token": "test-token",
                "cache_timeout": 300,
                "verify_ssl": True,
            }
        }
        with patch("netbox_librenms_plugin.librenms_api.get_plugin_config", return_value=servers):
            view._librenms_api = LibreNMSAPI(server_key="default")
        view.get_vlan_groups_for_device = MagicMock(return_value=[])
        view._build_vlan_lookup_maps = MagicMock(return_value={})
        view._add_vlan_group_selection = MagicMock()
        view._add_missing_vlans_info = MagicMock()
        view.get_table = MagicMock(return_value=MagicMock())
        view.get_cache_key = MagicMock(return_value="ports-key")
        view.get_last_fetched_key = MagicMock(return_value="lf-key")
        view.get_vlan_overrides_key = MagicMock(return_value="ov-key")
        return view

    @staticmethod
    def _host_with_idrac():
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("host1")
        # Main device has an "idrac0" interface that LibreNMS only reports on the
        # OOB-controller side (same name) — it must still surface as netbox-only.
        make_interface(device, "idrac0")
        return device

    def test_oob_row_does_not_match_or_hide_host_interface(self):
        """An OOB row sharing a host interface name renders unmatched (not bound to the host interface) and still doesn't suppress that host interface from the netbox-only set."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        # Host owns its own idrac0; LibreNMS reports idrac0 only on the OOB-controller side.
        dev = make_device("oob-shared-host")
        make_interface(dev, "idrac0")

        # Real DeviceInterfaceTableView so the real get_interfaces + _build_interface_lookup_maps
        # + per-port reconciliation run; only peripheral plumbing (vlan helpers, table build,
        # cache-key derivation) is stubbed. A captured get_table lets us assert the OOB row's fields.
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

    def test_oob_row_does_not_hide_netbox_only_interface(self):
        view = self._make_view()
        obj = self._host_with_idrac()
        cached = {"ports": [{"ifName": "idrac0", "_source": "oob", "port_id": 999}]}
        req = _make_request()

        def cache_get(key):
            return cached if key == "ports-key" else ({} if key == "ov-key" else None)

        with patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache:
            mock_cache.get.side_effect = cache_get
            mock_cache.ttl.return_value = None
            ctx = view.get_context_data(req, obj, "ifName", "default")

        names = {i["name"] for i in ctx["netbox_only_interfaces"]}
        assert "idrac0" in names  # OOB row must not suppress the main-device interface

    def test_fresh_data_renders_without_reading_cache(self):
        """On the OOB-ports-fetch-failure path the (partial) cache is deleted, so
        get_context_data must render from the in-memory fresh_data snapshot instead of
        reading the now-empty cache — otherwise the table renders empty under a
        "showing host interfaces" banner."""
        view = self._make_view()
        obj = self._host_with_idrac()
        fresh = {"ports": [{"ifName": "idrac0", "_source": "oob", "port_id": 999}]}
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

    def test_malformed_port_stack_relationships_does_not_crash(self):
        """A cached snapshot whose port_stack_relationships is None / a non-dict (corruption, partial write, format migration) must fail soft, not AttributeError on the .get('lag_members') calls."""
        view = self._make_view()
        obj = self._host_with_idrac()
        req = _make_request()

        for bad in (None, ["not", "a", "dict"], "garbage"):
            fresh = {
                "ports": [{"ifName": "idrac0", "port_id": 999, "_source": "host"}],
                "port_stack_relationships": bad,
            }
            with patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache:
                mock_cache.get.return_value = None
                mock_cache.ttl.return_value = None
                ctx = view.get_context_data(req, obj, "ifName", "default", fresh_data=fresh)  # must not raise
            assert "table" in ctx

    def test_malformed_nested_relationship_maps_do_not_crash(self):
        """port_stack_relationships is a dict but its lag_members / sub_interfaces are None / non-dict (the present-but-None key defeats the .get(..., {}) default) — iterating .items() must fail soft, not AttributeError."""
        view = self._make_view()
        obj = self._host_with_idrac()
        req = _make_request()

        for bad in (None, ["not", "a", "dict"], "garbage", 42):
            fresh = {
                "ports": [{"ifName": "idrac0", "port_id": 999, "_source": "host"}],
                "port_stack_relationships": {"lag_members": bad, "sub_interfaces": bad},
            }
            with patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache:
                mock_cache.get.return_value = None
                mock_cache.ttl.return_value = None
                ctx = view.get_context_data(req, obj, "ifName", "default", fresh_data=fresh)  # must not raise
            assert "table" in ctx

    def test_malformed_cached_port_snapshot_fails_closed(self):
        """A stale/corrupt cached ports snapshot (non-dict, or ports not a list of dicts) must be dropped and re-rendered empty, not 500 the sync tab — and the bad entry purged so a later render re-fetches."""
        view = self._make_view()
        obj = self._host_with_idrac()
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
    def test_standalone_relationship_cache_read_fails_closed_on_non_dict(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        obj = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.sync.interfaces.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ports-key"),
        ):
            mock_cache.get.return_value = ["corrupt", "snapshot"]

            assert view._get_cached_relationships(obj, "default") == {}

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
        req = _make_request(
            post_data={
                "select": ["cross-page-parent"],
                "select_port_id": ["0010"],
                "server_key": "default",
            }
        )

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
            # The real sync_interface increments _synced_count past its resolve guard; simulate that
            # at the mock seam so post()'s count-based success banner fires.
            patch.object(view, "sync_interface", side_effect=lambda *a, **k: _bump_synced(view)),
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
            patch.object(view, "sync_interface", side_effect=lambda *a, **k: _bump_synced(view)),
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

    def _run_post_with_skips(self, mock_msgs, selected, skip_names):
        """Drive SyncInterfacesView.post selecting *selected*, skipping *skip_names* during sync."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")
        mock_device = MagicMock(pk=1)
        ports = [{"ifName": name, "port_id": 10 + i} for i, name in enumerate(selected)]
        req = _make_request(post_data={"select": list(selected), "server_key": "default"})

        def _record_skips(*args, **kwargs):
            view._skipped_conflicts.extend(skip_names)
            # Simulate the real sync_interface: each selected port that ISN'T skipped increments
            # the synced counter (post()'s success banner keys on that count, not on a
            # skip-vs-selected size comparison).
            view._synced_count += max(0, len(selected) - len(skip_names))

        with (
            patch(
                "netbox_librenms_plugin.views.mixins.NetBoxObjectPermissionMixin.restrict_object_or_404",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.sync.interfaces.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.sync.interfaces.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.sync.interfaces.redirect"),
            patch("netbox_librenms_plugin.views.sync.interfaces.reverse", return_value="/sync/"),
            patch("netbox_librenms_plugin.views.sync.interfaces.transaction"),
            patch.object(view, "sync_selected_interfaces", side_effect=_record_skips),
            patch.object(view, "_sync_lag_and_parent_relationships"),
            patch.object(view, "_get_cached_relationships", return_value={}),
            patch.object(type(view), "get_vlan_groups_for_device", return_value=[]),
            patch.object(view.__class__, "get_cache_key", return_value="k"),
            patch.object(view.__class__, "_build_vlan_lookup_maps", return_value={}),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ports": ports}
            view.request = req
            view.post(req, "device", 1)

    def test_post_all_skipped_warns_without_success_banner(self):
        """Every selected interface conflict-skipped → only a warning, NO 'synced successfully'.

        Nothing was written, so a green success banner alongside the skip warning is misleading.
        """
        with patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs:
            self._run_post_with_skips(
                mock_msgs,
                selected=["Gi0/1"],
                skip_names=["Gi0/1 (selected target unavailable)"],
            )

        mock_msgs.warning.assert_called_once()
        warning_msg = mock_msgs.warning.call_args[0][1]
        assert "Gi0/1" in warning_msg
        assert "selected target unavailable" in warning_msg
        assert "could not be safely matched" not in warning_msg
        mock_msgs.success.assert_not_called()

    def test_post_partial_skip_still_reports_success(self):
        """Some synced, some skipped → both the skip warning AND the success banner fire."""
        with patch("netbox_librenms_plugin.views.sync.interfaces.messages") as mock_msgs:
            self._run_post_with_skips(
                mock_msgs,
                selected=["Gi0/1", "Gi0/2"],
                skip_names=["Gi0/1 (selected target unavailable)"],
            )

        mock_msgs.warning.assert_called_once()
        mock_msgs.success.assert_called_once()

    def test_success_banner_shown_when_a_colliding_name_syncs_despite_a_skip(self):
        """A single selected display name can match MULTIPLE ports (ifName/ifDescr collision). If one colliding port is skipped and another synced, len(_skipped_conflicts) can equal len(selected) yet something WAS synced — the count-based banner must still fire (a size comparison would suppress it)."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.require_all_permissions = MagicMock(return_value=None)
        view.get_required_permissions_for_object_type = MagicMock(return_value=[])
        mock_api = MagicMock(server_key="default")
        mock_device = MagicMock(pk=1)
        # ONE selected name resolving to two ports: one is conflict-skipped, the other syncs.
        ports = [{"ifName": "eth0", "port_id": 1}, {"ifName": "eth0", "port_id": 2}]
        req = _make_request(post_data={"select": ["eth0"], "server_key": "default"})

        def _one_skip_one_sync(*args, **kwargs):
            view._skipped_conflicts.append("eth0")  # first colliding port fails
            view._synced_count += 1  # second colliding port syncs

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
            patch.object(view, "sync_selected_interfaces", side_effect=_one_skip_one_sync),
            patch.object(view, "_sync_lag_and_parent_relationships"),
            patch.object(view, "_get_cached_relationships", return_value={}),
            patch.object(type(view), "get_vlan_groups_for_device", return_value=[]),
            patch.object(view.__class__, "get_cache_key", return_value="k"),
            patch.object(view.__class__, "_build_vlan_lookup_maps", return_value={}),
            patch.object(type(view), "librenms_api", new_callable=lambda: property(lambda s: mock_api)),
        ):
            mock_cache.get.return_value = {"ports": ports}
            view.request = req
            view.post(req, "device", 1)

        # len(_skipped_conflicts) == len(selected_interfaces) == 1, but _synced_count == 1, so the
        # old size comparison would suppress the banner while the count-based check shows it.
        mock_msgs.success.assert_called_once()
        mock_msgs.warning.assert_called_once()


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
        """A port whose display name is not in 'select' is still synced when its port_id is in select_port_id (a cross-page parent auto-included by the LAG/parent JS)."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.interface_name_field = "ifName"
        view.sync_interface = MagicMock()
        view._selected_port_ids = {42}

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
    """Outcome tests for the bulk LAG/parent relationship sync, driven against real NetBox Device/Interface objects (the real _resolve_interface_by_port_id, _interfaces_same_owner and Interface.full_clean run) so the linking — and the new per-row owner pinning — is verified end-to-end rather than re-asserting mock calls."""

    @staticmethod
    def _make_device():
        from netbox_librenms_plugin.tests.conftest import make_device

        return make_device("lag-dev")

    @staticmethod
    def _iface(device, name, port_id, itype="1000base-t"):
        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        iface = make_interface(device, name, iface_type=itype)
        set_librenms_device_id(iface, port_id, "default")
        iface.save()
        return iface

    def _make_view(self, name_field="ifName", selected_port_ids=None):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        view.interface_name_field = name_field
        view.request = make_request("post", {})
        if selected_port_ids is not None:
            view._selected_port_ids = set(selected_port_ids)
        return view

    def test_duplicate_display_name_members_not_collapsed(self, db):
        """In ifDescr mode two member ports share a display name; both must still be linked to their LAG."""
        device = self._make_device()
        m1 = self._iface(device, "Gi0/1", 10)
        m2 = self._iface(device, "Gi0/2", 11)
        agg = self._iface(device, "Po1", 100, itype="lag")

        ports_data = [
            {"ifDescr": "Ethernet", "ifName": "Gi0/1", "port_id": 10},
            {"ifDescr": "Ethernet", "ifName": "Gi0/2", "port_id": 11},
            {"ifDescr": "Po1", "ifName": "Po1", "port_id": 100},
        ]
        relationships = {"lag_members": {10: 100, 11: 100}, "sub_interfaces": {}}

        view = self._make_view(name_field="ifDescr")
        view._sync_lag_and_parent_relationships(device, ["Ethernet"], ports_data, relationships, "default")

        m1.refresh_from_db()
        m2.refresh_from_db()
        assert m1.lag_id == agg.pk
        assert m2.lag_id == agg.pk

    def test_member_selected_only_by_stable_id_is_linked(self, db):
        """A port present only via select_port_id (not selected by display name) is still processed by the relationship sync."""
        device = self._make_device()
        member = self._iface(device, "Gi0/2", 11)
        agg = self._iface(device, "Po1", 100, itype="lag")

        ports_data = [
            {"ifName": "Gi0/2", "port_id": 11},
            {"ifName": "Po1", "port_id": 100},
        ]
        relationships = {"lag_members": {11: 100}, "sub_interfaces": {}}

        view = self._make_view(name_field="ifName", selected_port_ids={"11"})
        view._sync_lag_and_parent_relationships(device, [], ports_data, relationships, "default")

        member.refresh_from_db()
        assert member.lag_id == agg.pk

    def test_stable_ids_are_canonicalized_before_relationship_lookup(self, db):
        """Equivalent string and integer port IDs must resolve to one canonical key."""
        device = self._make_device()
        member = self._iface(device, "Gi0/3", 12)
        agg = self._iface(device, "Po2", 101, itype="lag")
        ports_data = [
            {"ifName": "Gi0/3", "port_id": "0012"},
            {"ifName": "Po2", "port_id": 101},
        ]
        relationships = {"lag_members": {12: 101}, "sub_interfaces": {}}

        view = self._make_view(selected_port_ids={"12"})
        view._sync_lag_and_parent_relationships(device, [], ports_data, relationships, "default")

        member.refresh_from_db()
        assert member.lag_id == agg.pk

    def test_bulk_relationship_sync_excludes_interfaces_outside_the_change_grant(self, db):
        """A constrained change grant must scope the shared bulk relationship index."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_user_with_perms

        device = self._make_device()
        self._iface(device, "allowed", 20)
        hidden_member = self._iface(device, "Gi0/4", 21)
        hidden_agg = self._iface(device, "Po3", 102)
        user = make_user_with_perms("bulk-rel-scope", [])
        user = grant(user, "change", Interface, constraints={"name": "allowed"})
        ports_data = [
            {"ifName": "Gi0/4", "port_id": 21},
            {"ifName": "Po3", "port_id": 102},
        ]
        relationships = {"lag_members": {21: 102}, "sub_interfaces": {}}

        view = self._make_view(selected_port_ids={"21"})
        view.request = make_request("post", {}, user=user)
        view._sync_lag_and_parent_relationships(device, [], ports_data, relationships, "default")

        hidden_member.refresh_from_db()
        hidden_agg.refresh_from_db()
        assert hidden_member.lag_id is None
        assert hidden_agg.type == "1000base-t"

    def test_non_dict_relationships_fails_soft_not_attributeerror(self, db):
        """
        A truthy but non-dict ``relationships`` (a list from a corrupt / partial-write cache) must
        fail soft. The local ``if not relationships`` guard only catches FALSY values, so the
        unfixed bulk path called ``relationships.get(...)`` on the list and raised AttributeError;
        the shared ``normalize_relationship_maps`` coerces it to ``{}`` so the sync is skipped.
        """
        device = self._make_device()
        member = self._iface(device, "Gi0/2", 11)
        ports_data = [{"ifName": "Gi0/2", "port_id": 11}]

        view = self._make_view(name_field="ifName", selected_port_ids={"11"})
        # Must NOT raise AttributeError on the non-dict relationships.
        view._sync_lag_and_parent_relationships(device, [], ports_data, ["garbage"], "default")

        member.refresh_from_db()
        assert member.lag_id is None  # nothing persisted from the corrupt map

    def test_invalid_lag_link_rejected_by_full_clean_is_skipped(self, db):
        """A relationship that fails Interface.full_clean() (a self-LAG from stale/crafted port_stack data) must be skipped, not persisted."""
        device = self._make_device()
        member = self._iface(device, "Gi0/2", 11)

        ports_data = [{"ifName": "Gi0/2", "port_id": 11}]
        # Self-LAG: the member's aggregate resolves back to itself (port_id 11 → 11), which
        # Interface.full_clean() rejects.
        relationships = {"lag_members": {11: 11}, "sub_interfaces": {}}

        view = self._make_view(name_field="ifName", selected_port_ids={"11"})
        view._sync_lag_and_parent_relationships(device, [], ports_data, relationships, "default")

        member.refresh_from_db()
        assert member.lag_id is None  # invalid self-LAG was not persisted

    def test_agg_type_restored_when_a_sharing_member_fails_validation(self, db):
        """A member whose LAG link fails full_clean must not leave the shared agg_iface.type='lag' dirty: a later valid member sharing that aggregate must still persist it as a LAG in the DB."""
        device = self._make_device()
        # A virtual interface cannot be assigned to a LAG, so its link fails full_clean — but only
        # after agg.type was set to 'lag' in memory on the shared index object.
        self._iface(device, "virt0", 10, itype="virtual")
        member2 = self._iface(device, "Gi0/2", 11)
        agg = self._iface(device, "Po1", 100, itype="1000base-t")  # not yet a LAG

        ports_data = [
            {"ifName": "virt0", "port_id": 10},
            {"ifName": "Gi0/2", "port_id": 11},
            {"ifName": "Po1", "port_id": 100},
        ]
        relationships = {"lag_members": {10: 100, 11: 100}, "sub_interfaces": {}}

        view = self._make_view(name_field="ifName", selected_port_ids={"10", "11"})
        view._sync_lag_and_parent_relationships(device, [], ports_data, relationships, "default")

        member2.refresh_from_db()
        agg.refresh_from_db()
        # Invariant: if the valid member was linked, the aggregate MUST be persisted as a LAG.
        assert member2.lag_id == agg.pk
        assert agg.type == "lag"

    def test_apply_relationship_restores_source_fk_on_validation_failure(self, db):
        """A failed link must leave source_iface's FK unmutated — it is reused across rows via the shared index."""
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.views.sync.interfaces import _apply_interface_relationship

        device = self._make_device()
        # A virtual interface cannot be assigned to a LAG, so the link fails full_clean.
        member = self._iface(device, "virt7", 71, itype="virtual")
        agg = self._iface(device, "Po7", 107, itype="lag")
        assert member.lag_id is None

        with pytest.raises(ValidationError):
            _apply_interface_relationship(member, "lag", agg, prepare_related=None)

        # The attempted FK is rolled back in memory so a later edge reusing this instance doesn't
        # validate against — or persist — the failed link.
        assert member.lag is None
        assert member.lag_id is None

    def test_name_hint_resolves_on_expected_owner_across_vc_duplicate_names(self, db):
        """On a VC, an id-less interface whose name is shared across members resolves to the SELECTED member, not chassis-wide ambiguity."""
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis
        from netbox_librenms_plugin.views.sync.interfaces import (
            _interface_owner_for_object,
            _resolve_interface_by_port_id,
        )

        dev1 = make_device("vc-nh-m1")
        dev2 = make_device("vc-nh-m2")
        make_virtual_chassis("vc-nh", dev1, dev2)
        i1 = make_interface(dev1, "Gi0/1", iface_type="1000base-t")  # id-less, on member 1
        make_interface(dev2, "Gi0/1", iface_type="1000base-t")  # SAME name on member 2

        # port_id 909 matches no stored id → name-hint fallback. "Gi0/1" is duplicated across the
        # chassis; without owner-pinning the name lookup reports ambiguity and never resolves.
        iface, err = _resolve_interface_by_port_id(
            dev1, "909", "default", name_hint="Gi0/1", expected_owner=_interface_owner_for_object(dev1)
        )
        assert err is None, err
        assert iface is not None and iface.pk == i1.pk

    def test_cross_page_parent_resolves_to_port_keyed_member_override(self, db):
        """A cross-page parent (only select_port_id + device_selection_port_<id>) pins to that member, not the page device."""
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

        page_dev = make_device("vc-page-master")
        member2 = make_device("vc-page-m2")
        make_virtual_chassis("vc-page", page_dev, member2)

        view = self._make_view()
        # The JS submits the off-page parent's member (from the child row's live .vc-member-select)
        # keyed by the parent's stable port_id; there is no device_selection_<name> for it.
        view.request.POST = {"device_selection_port_100": str(member2.id)}

        # Port-keyed override wins → the parent resolves onto member2.
        assert view._resolve_row_target_device(page_dev, "Po1", port_id="100").id == member2.id
        # No override for this port and no name selection → the page device (unchanged default).
        assert view._resolve_row_target_device(page_dev, "Po1", port_id="999").id == page_dev.id

    def test_oob_row_excluded_from_relationship_sync(self, db):
        """An OOB-controller row sharing a selected host display name must not contribute its port_id, or the LAG pass would link the hidden controller interface instead of the host."""
        device = self._make_device()
        self._iface(device, "Gi0/1", 10)  # real host member sharing the name
        self._iface(device, "Po1", 100, itype="lag")  # the LAG aggregate (port_id 100)
        # The OOB controller's interface, which the OOB row's port_id would resolve to.
        oob_iface = self._iface(device, "idrac0", 999)

        ports_data = [
            {"ifName": "Gi0/1", "port_id": 10},
            {"ifName": "Po1", "port_id": 100},
            # OOB controller row merged for display; shares the selected host name "Gi0/1".
            {"ifName": "Gi0/1", "port_id": 999, "_source": "oob"},
        ]
        # Crafted/colliding port_stack data maps the OOB row's port_id to the aggregate.
        relationships = {"lag_members": {"999": 100}, "sub_interfaces": {}}

        view = self._make_view(name_field="ifName")
        view._sync_lag_and_parent_relationships(device, ["Gi0/1"], ports_data, relationships, "default")

        oob_iface.refresh_from_db()
        assert oob_iface.lag_id is None  # OOB row skipped → no link persisted on the controller iface


# A POSTed valid non-default server_key must scope the sync to that server without 500ing on a
# misconfigured default client. Under the stack that behavior comes from rebind_api_for_server, so
# its coverage lives with the rebind seam in
# test_coverage_sync_views.TestSyncInterfacesViewServerRebind
# (test_posted_server_key_is_bound_for_the_sync / test_stale_server_key_fails_closed_without_sync).

# ===========================================================================
# _resolve_interface_by_port_id: correct librenms_id dict lookup
# ===========================================================================


@pytest.mark.django_db
class TestResolveInterfaceByPortId:
    """The function must correctly read the nested {'server_key': port_id} dict format."""

    def test_finds_interface_by_server_keyed_dict(self):
        """When librenms_id = {'production': 42}, resolves for port_id=42 and server_key='production'."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id

        device = make_device("pci-byid")
        iface = make_interface(device, "Gi0/1", iface_type="1000base-t")
        set_librenms_device_id(iface, 42, "production")  # stored as {"production": 42}
        iface.save()

        found, err = _resolve_interface_by_port_id(device, "42", "production")

        assert err is None
        assert found == iface

    def test_returns_error_when_not_found(self):
        """Returns (None, error) when no interface has matching port_id."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id

        device = make_device("pci-notfound")
        # An interface exists, but carries a different port id than the one we look up.
        iface = make_interface(device, "Gi0/1", iface_type="1000base-t")
        set_librenms_device_id(iface, 42, "production")
        iface.save()

        found, err = _resolve_interface_by_port_id(device, "99", "production")

        assert found is None
        assert err is not None

    def test_name_hint_fallback_when_no_librenms_id(self):
        """Falls back to exact name lookup when no interface has a matching librenms_id."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id

        device = make_device("pci-namehint")
        # The interface was created manually (no librenms_id) — only the name matches.
        iface = make_interface(device, "lag-1", iface_type="lag")

        found, err = _resolve_interface_by_port_id(device, "42", "production", name_hint="lag-1")

        assert err is None
        assert found == iface

    def test_ambiguous_port_id_returns_error_not_first_match(self):
        """Two interfaces carrying the same stale librenms_id must fail as ambiguous, not silently bind lag/parent to whichever happens to be first."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id

        device = make_device("pci-ambig")
        for name in ("Gi0/1", "Gi0/2"):
            iface = make_interface(device, name, iface_type="1000base-t")
            set_librenms_device_id(iface, 42, "production")  # same stale id on both
            iface.save()

        found, err = _resolve_interface_by_port_id(device, "42", "production")

        assert found is None
        assert err is not None
        assert "ambiguous" in err.lower()

    def test_name_hint_does_not_exist_falls_through_to_not_found(self):
        """A name-hint miss (DoesNotExist) is swallowed and reported as not-found."""
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id

        device = make_device("pci-namemiss")  # no interfaces at all

        found, err = _resolve_interface_by_port_id(device, "42", "production", name_hint="lag-1")

        assert found is None
        assert err is not None
        assert "not found" in err.lower()

    def test_name_hint_multiple_matches_returns_ambiguous(self):
        """A name-hint matching multiple interfaces returns an ambiguity error, not a silent not-found."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_virtual_chassis
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id

        member1 = make_device("pci-vc-m1")
        member2 = make_device("pci-vc-m2")
        make_virtual_chassis("pci-vc", member1, member2)
        # Same interface name on both members; neither has a librenms_id for port 42.
        make_interface(member1, "lag-1", iface_type="lag")
        make_interface(member2, "lag-1", iface_type="lag")

        found, err = _resolve_interface_by_port_id(member1, "42", "production", name_hint="lag-1")

        assert found is None
        assert err is not None
        assert "ambiguous" in err.lower()

    def test_unexpected_error_during_resolution_propagates(self):
        """A real DB/runtime fault while scanning the device's interfaces must propagate, not be masked as a silent not-found."""
        import pytest
        from unittest.mock import MagicMock, patch
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id
        from dcim.models import Device

        mock_device = MagicMock(spec=Device)
        mock_device.virtual_chassis = None

        # The index build iterates the interface queryset; a DB fault during that scan must bubble.
        failing_qs = MagicMock()
        failing_qs.__iter__.side_effect = RuntimeError("database is down")

        with patch("netbox_librenms_plugin.views.sync.interfaces.Interface") as mock_intf_cls:
            mock_intf_cls.objects.filter.return_value = failing_qs
            with pytest.raises(RuntimeError):
                _resolve_interface_by_port_id(mock_device, "42", "production", name_hint="lag-1")


class TestResolveInterfaceByPortIdExpectedOwner:
    """Real-DB coverage for the expected_owner guard in _resolve_interface_by_port_id."""

    @staticmethod
    def _make_vc_members():
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis

        member1 = make_device("vc-m1")
        member2 = make_device("vc-m2")
        make_virtual_chassis("VC-1", member1, member2)
        return member1, member2

    @staticmethod
    def _iface_with_librenms_id(device, name, port_id, server_key="default"):
        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        iface = make_interface(device, name, iface_type="1000base-t")
        set_librenms_device_id(iface, port_id, server_key)
        iface.save()
        return iface

    def test_rejects_match_on_a_different_vc_member(self, db):
        """port_id resolves uniquely onto member2's interface; resolving from member1 with expected_owner=member1 must reject it (the fix) — and accept it without the guard."""
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

    def test_owner_mismatch_falls_back_to_name_hint(self, db):
        """A stale port_id on a foreign VC member must not block the name_hint fallback to the manually-created interface on the expected owner."""
        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.views.sync.interfaces import _resolve_interface_by_port_id

        member1, member2 = self._make_vc_members()
        # Stale/reused librenms_id 42 lives on member2 (a foreign member).
        self._iface_with_librenms_id(member2, "Gi0/1", 42)
        # The real target: a manually-created aggregate on member1 with no stored librenms_id.
        agg = make_interface(member1, "Po1", iface_type="lag")

        # port_id 42 uniquely id-matches member2, but pinned to member1 with name_hint 'Po1'
        # the foreign id-match must be skipped and the name fallback must find member1's Po1.
        found, err = _resolve_interface_by_port_id(
            member1, "42", "default", name_hint="Po1", expected_owner=(member1.pk, None)
        )
        assert err is None, err
        assert found == agg


class TestBulkRelationshipRobustness:
    """Real-DB coverage for the bulk LAG/parent relationship pass robustness fixes."""

    def test_non_dict_relationship_maps_do_not_crash(self, db):
        """A corrupt list-shaped lag_members/sub_interfaces in the cached relationships must be coerced to {} (fail-soft), not crash the bulk sync POST."""
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("rel-shape")
        view = object.__new__(SyncInterfacesView)
        # Mirrors the hardened reader _build_relationship_maps; without the guard this raises
        # AttributeError ('list' object has no attribute 'items').
        view._sync_lag_and_parent_relationships(
            device, [], [], {"lag_members": [1, 2], "sub_interfaces": ["x"]}, "default"
        )

    def test_bulk_lag_persist_does_not_clobber_concurrent_edits(self, db):
        """The bulk LAG persist writes only the changed FK/type columns, so a concurrent edit to other fields of the stale in-memory objects isn't lost (no full-row overwrite)."""
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("toctou")
        member = make_interface(device, "Et1", iface_type="1000base-t")
        agg = make_interface(device, "Po1", iface_type="1000base-t")
        # In-memory objects now hold stale descriptions; a concurrent writer commits fresh
        # descriptions straight to the DB after these objects were loaded.
        member.description = "member-stale"
        agg.description = "agg-stale"
        Interface.objects.filter(pk=member.pk).update(description="member-fresh")
        Interface.objects.filter(pk=agg.pk).update(description="agg-fresh")

        view = object.__new__(SyncInterfacesView)
        view._apply_relationship_edge(member, "lag", agg, SyncInterfacesView._prepare_bulk_lag_aggregate, "LAG")

        agg.refresh_from_db()
        member.refresh_from_db()
        assert agg.type == "lag"  # the intended mutation persisted
        assert member.lag_id == agg.pk  # the link persisted
        assert agg.description == "agg-fresh"  # concurrent edit NOT clobbered
        assert member.description == "member-fresh"  # concurrent edit NOT clobbered


@pytest.mark.django_db
class TestInterfaceLinkValidationErrorNoStackTrace:
    """LAG/parent full_clean() failures return a fixed message and log the detail — the raw exception text must not be echoed to the client (CodeQL py/stack-trace-exposure). Real device/interfaces + view; only Interface.full_clean is patched to inject a known sentinel error."""

    _SENTINEL = "SENSITIVE_VALIDATION_INTERNALS"

    @staticmethod
    def _make_view(view_cls):
        view = object.__new__(view_cls)
        view._librenms_api = MagicMock(server_key="default")
        view.require_all_permissions_json = MagicMock(return_value=None)
        return view

    @staticmethod
    def _iface(device, name, port_id):
        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        iface = make_interface(device, name, iface_type="1000base-t")
        set_librenms_device_id(iface, port_id, "default")
        iface.save()
        return iface

    def test_lag_link_validation_error_does_not_leak_exception(self):
        import json

        from dcim.models import Interface
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        device = make_device("lag-noleak")
        self._iface(device, "Et1", 1)
        self._iface(device, "Po1", 2)
        view = self._make_view(SyncInterfaceLagView)
        req = _make_request({"port_id": "1", "lag_port_id": "2", "server_key": "default"})

        # Real resolution + real view; inject a known error at the validation boundary so we can
        # prove its text is not echoed back (a genuine self-link error gives no controllable string).
        with (
            patch.object(Interface, "full_clean", side_effect=ValidationError(self._SENTINEL)),
            patch("netbox_librenms_plugin.views.sync.interfaces.logger") as mock_logger,
        ):
            resp = _post(view, req, object_type="device", object_id=device.pk)

        assert resp.status_code == 409
        assert self._SENTINEL not in resp.content.decode()
        body = json.loads(resp.content)
        assert "Cannot link Et1 to LAG Po1" in body["error"]
        assert "NetBox rejected the LAG relationship" in body["error"]
        assert "cannot be its own" not in body["error"]
        # The real detail is logged server-side, not lost.
        assert any(self._SENTINEL in str(c.args) for c in mock_logger.warning.call_args_list)

    def test_parent_link_validation_error_does_not_leak_exception(self):
        import json

        from dcim.models import Interface
        from django.core.exceptions import ValidationError

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("parent-noleak")
        self._iface(device, "Et1.100", 1)
        self._iface(device, "Et1", 2)
        view = self._make_view(SyncInterfaceParentView)
        req = _make_request({"port_id": "1", "parent_port_id": "2", "server_key": "default"})

        with (
            patch.object(Interface, "full_clean", side_effect=ValidationError(self._SENTINEL)),
            patch("netbox_librenms_plugin.views.sync.interfaces.logger") as mock_logger,
        ):
            resp = _post(view, req, object_type="device", object_id=device.pk)

        assert resp.status_code == 409
        assert self._SENTINEL not in resp.content.decode()
        body = json.loads(resp.content)
        assert "Cannot link Et1.100 to parent Et1" in body["error"]
        assert "NetBox rejected the parent relationship" in body["error"]
        assert "cannot be its own" not in body["error"]
        assert any(self._SENTINEL in str(c.args) for c in mock_logger.warning.call_args_list)


@pytest.mark.django_db
class TestSyncInterfaceLagViewRealDB:
    """End-to-end (real DB) coverage for SyncInterfaceLagView.post."""

    def _make_view(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        view = object.__new__(SyncInterfaceLagView)
        view._librenms_api = MagicMock(server_key="default")
        view.require_all_permissions_json = MagicMock(return_value=None)
        return view

    @staticmethod
    def _iface(device, name, port_id, itype="1000base-t"):
        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        iface = make_interface(device, name, iface_type=itype)
        set_librenms_device_id(iface, port_id, "default")
        iface.save()
        return iface

    def test_links_member_to_aggregate(self):
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("lag-host")
        member = self._iface(device, "Gi0/1", 10)
        agg = self._iface(device, "Po1", 20)

        view = self._make_view()
        req = _make_request({"port_id": "10", "lag_port_id": "20", "server_key": "default"})
        resp = _post(view, req, object_type="device", object_id=device.pk)

        assert resp.status_code == 200
        member.refresh_from_db()
        agg.refresh_from_db()
        # The link actually persisted, and the aggregate was promoted to type=lag.
        assert member.lag_id == agg.pk
        assert agg.type == "lag"

    def test_builds_interface_index_once_per_post(self):
        """post() builds the VC-wide interface index once and shares it across both resolutions."""
        import netbox_librenms_plugin.views.sync.interfaces as sync_mod
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("lag-host-idx")
        self._iface(device, "Gi0/1", 50)
        self._iface(device, "Po1", 60)

        real_build = sync_mod._build_interface_index
        calls = []

        def counting_build(obj, server_key, **kwargs):
            calls.append((obj, kwargs))
            return real_build(obj, server_key, **kwargs)

        view = self._make_view()
        req = _make_request({"port_id": "50", "lag_port_id": "60", "server_key": "default"})
        with patch.object(sync_mod, "_build_interface_index", side_effect=counting_build):
            resp = _post(view, req, object_type="device", object_id=device.pk)

        assert resp.status_code == 200
        assert len(calls) == 1  # built once and shared; pre-fix each resolve rebuilt it (2)
        # ...and it is built SCOPED to the acting user, not against the plain manager.
        assert calls[0][1]["user"] is req.user

    def test_self_lag_rejected_by_real_full_clean(self):
        import json

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("lag-host-self")
        iface = self._iface(device, "Po1", 30)

        view = self._make_view()
        # port_id == lag_port_id resolves member == aggregate (same real interface); NetBox's
        # real Interface.clean() must reject the self-LAG with a 409 and persist nothing.
        req = _make_request({"port_id": "30", "lag_port_id": "30", "server_key": "default"})
        resp = _post(view, req, object_type="device", object_id=device.pk)

        assert resp.status_code == 409
        body = json.loads(resp.content)
        assert "NetBox rejected the LAG relationship" in body["error"]
        iface.refresh_from_db()
        assert iface.lag_id is None

    def test_concurrent_conflict_returns_409_not_500(self):
        """A DB conflict in the persist returns a JSON 409, mirroring the bulk pass.

        Pre-fix the IntegrityError propagated out of post() as an unhandled 500 to the
        fetch() caller. The related interface is deleted inside the _prepare_related hook —
        after resolution, before the FK write — with full_clean no-opped to open the
        validate/write TOCTOU window; SET CONSTRAINTS ALL IMMEDIATE makes the REAL FK
        violation fire at the write (the deferred commit-time form surfaces at the atomic's
        exit and is caught by the same wrapper).
        """
        import json

        from dcim.models import Interface
        from django.db import connection

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("lag-host-conflict")
        member = self._iface(device, "Gi0/1", 40)
        agg = self._iface(device, "Po9", 41)

        view = self._make_view()

        def racing_prepare(related_iface):
            # The concurrent delete lands after both ends resolved, right before the write.
            Interface.objects.filter(pk=related_iface.pk).delete()
            return None

        view._prepare_related = racing_prepare
        req = _make_request({"port_id": "40", "lag_port_id": "41", "server_key": "default"})

        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with patch.object(Interface, "full_clean", lambda self: None):
            resp = _post(view, req, object_type="device", object_id=device.pk)

        assert resp.status_code == 409
        body = json.loads(resp.content)
        assert "concurrent change" in body["error"]
        member.refresh_from_db()
        assert member.lag_id is None  # nothing half-persisted
        # The simulated concurrent delete ran INSIDE the endpoint's atomic (in production it's a
        # separate committed transaction), so the rollback restored it — proving the endpoint's
        # whole write unit rolled back rather than committing any partial state.
        assert Interface.objects.filter(pk=agg.pk).exists()

    def test_cross_member_aggregate_rejected(self):
        """The aggregate port_id resolving onto a *different* VC member must not link across devices — the expected_owner pin (obj = the posted member) rejects it at resolution."""
        import json

        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis

        member1 = make_device("vc-lag-m1")
        member2 = make_device("vc-lag-m2")
        make_virtual_chassis("VC-LAG", member1, member2)
        local = self._iface(member1, "Gi0/1", 40)
        # The "aggregate" id 50 lives on member2, not member1.
        self._iface(member2, "Po1", 50)

        view = self._make_view()
        # Posting against member1: the aggregate resolves onto member2 and must be refused.
        req = _make_request({"port_id": "40", "lag_port_id": "50", "server_key": "default"})
        resp = _post(view, req, object_type="device", object_id=member1.pk)

        # Pin the rejection to the expected_owner path: a bare `in (404, 409)` would still pass if
        # post() stopped pinning expected_owner and only the later cross-device guard (409) caught
        # it. The owner pin rejects at resolution with 404 + a "different owner" message.
        assert resp.status_code == 404
        body = json.loads(resp.content)
        assert "different owner" in body["error"]
        local.refresh_from_db()
        assert local.lag_id is None


@pytest.mark.django_db
class TestPromoteLagAggregateShared:
    """The bulk and single-row LAG endpoints promote the aggregate through one shared helper."""

    def test_bulk_returns_persist_restore_and_restore_reverts_in_memory(self):
        """_prepare_bulk_lag_aggregate returns (persist, restore); restore reverts the in-memory type."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        agg = make_interface(make_device("lag-bulk"), "ae0", iface_type="other")
        result = SyncInterfacesView._prepare_bulk_lag_aggregate(agg)

        assert isinstance(result, tuple)
        persist, restore = result
        assert agg.type == "lag"  # bumped in memory
        restore()
        assert agg.type == "other"  # restore reverts (aggregate reused across rows)

    def test_single_row_returns_bare_persist_and_saves_only_type(self):
        """SyncInterfaceLagView._prepare_related returns a bare persist that saves type=lag."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        agg = make_interface(make_device("lag-single"), "ae0", iface_type="other")
        view = object.__new__(SyncInterfaceLagView)
        persist = view._prepare_related(agg)

        assert callable(persist) and not isinstance(persist, tuple)
        assert agg.type == "lag"  # bumped in memory
        persist()
        agg.refresh_from_db()
        assert agg.type == "lag"  # persisted

    def test_already_lag_returns_none(self):
        """An aggregate already type=lag needs no promotion (both entry points return None)."""
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView, SyncInterfacesView

        agg = make_interface(make_device("lag-noop"), "ae0", iface_type="lag")
        assert SyncInterfacesView._prepare_bulk_lag_aggregate(agg) is None
        assert object.__new__(SyncInterfaceLagView)._prepare_related(agg) is None


@pytest.mark.django_db
class TestBulkRelationshipConcurrentConflict:
    """Concurrent DB conflicts in the bulk LAG/parent pass must not 500 the sync POST.

    Real failure mode: the related interface is deleted in the window between full_clean()
    (which DOES verify the FK row exists, raising ValidationError when it's already gone) and
    the FK write. full_clean is no-opped to open that window deterministically (same seam as
    TestInterfaceLinkValidationErrorNoStackTrace). Two layers:

    - statement-time IntegrityError (unique/check/immediate FK): caught per edge, under the
      edge's own savepoint — a bare catch would leave the batch transaction poisoned
      ("current transaction is aborted") for every later row, per the migrate.py precedent.
      Tested against REAL SQL via SET CONSTRAINTS ALL IMMEDIATE.
    - commit-time IntegrityError: Django's Postgres FK constraints are INITIALLY DEFERRED,
      so a stale-FK write only explodes at the batch atomic's COMMIT, after every per-row
      guard has passed — caught at the batch level and surfaced as a warning toast. A real
      deferred COMMIT can't fire inside the test's wrapping transaction (and transaction=True
      would flush migration-seeded rows for the rest of the suite), so that layer's wiring is
      verified by injecting the error at the edge seam.
    """

    def test_statement_time_conflict_skips_row_and_batch_continues(self):
        from dcim.models import Interface
        from django.db import connection, transaction

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("edge-integrity-host")
        member = make_interface(device, "Gi0/1")
        agg = make_interface(device, "Po9", iface_type="lag")
        # Concurrent delete: the DB row vanishes while the in-memory object (already resolved
        # into the batch's interface index) keeps its pk.
        Interface.objects.filter(pk=agg.pk).delete()

        view = object.__new__(SyncInterfacesView)
        with patch.object(Interface, "full_clean", lambda self: None):  # validate/write TOCTOU window
            with transaction.atomic():  # mirror the bulk pass's enclosing batch transaction
                # Check FKs per statement (as some deployments/constraints do) so the violation
                # raises AT the write — the savepoint path, not the deferred commit-time path.
                with connection.cursor() as cur:
                    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
                view._apply_relationship_edge(member, "lag", agg, None, "LAG")
                # Later rows keep processing in the SAME transaction: this INSERT raises
                # "current transaction is aborted" if the IntegrityError poisoned it.
                follow_up = make_interface(device, "Gi0/2")

        member.refresh_from_db()
        assert member.lag_id is None  # the conflicting row was skipped, nothing half-persisted
        follow_up.refresh_from_db()  # the batch's later work persisted fine

    def test_commit_time_conflict_warns_instead_of_500(self):
        from django.db import IntegrityError

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("batch-integrity-host")
        member = make_interface(device, "Gi0/1")
        set_librenms_device_id(member, 1, "default")
        member.save()
        agg = make_interface(device, "Po9", iface_type="lag")
        set_librenms_device_id(agg, 10, "default")
        agg.save()

        view = object.__new__(SyncInterfacesView)
        view.interface_name_field = "ifName"
        request = make_request("post", {})
        view.request = request

        ports_data = [
            {"port_id": 1, "ifName": "Gi0/1"},
            {"port_id": 10, "ifName": "Po9"},
        ]
        relationships = {"lag_members": {1: 10}, "sub_interfaces": {}}

        def deferred_commit_violation(view_self, source, field, related, prep, kind):
            # Stands in for the deferred FK check firing at the batch atomic's COMMIT —
            # past the per-row savepoint handler, so only the batch-level catch can see it.
            raise IntegrityError('insert or update on table "dcim_interface" violates foreign key constraint')

        with patch.object(SyncInterfacesView, "_apply_relationship_edge", deferred_commit_violation):
            # Pre-fix this propagated out of the view as a 500.
            view._sync_lag_and_parent_relationships(device, ["Gi0/1"], ports_data, relationships, "default")

        member.refresh_from_db()
        assert member.lag_id is None  # the relationship pass rolled back as a unit
        queued = [str(m) for m in request._messages._queued_messages]
        assert any("concurrent change" in m for m in queued)


@pytest.mark.django_db
class TestRelationshipSyncObjectScope:
    """The LAG/parent endpoints write both ends, so their resolution must run through a restricted
    queryset.

    NetBoxObjectPermissionMixin asks ``has_perm`` without an instance, so a CONSTRAINED
    ``change_interface`` grant clears the POST gate; an unrestricted interface index would then let
    it set ``lag``/``parent`` on interfaces it cannot see.
    """

    @staticmethod
    def _writer(username, specs):
        """A real non-superuser with plugin write access plus ``specs`` = [(model, action, constraints)] grants."""
        user = make_user_with_perms(username, [])
        for i, (model, action, constraints) in enumerate(specs):
            user = grant(
                user,
                action,
                model,
                constraints=constraints,
                name=f"{username}-{action}-{i}",
            )
        return user

    @staticmethod
    def _iface(device, name, port_id):
        from netbox_librenms_plugin.tests.conftest import make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        iface = make_interface(device, name)
        set_librenms_device_id(iface, port_id, "default")
        iface.save()
        return iface

    def _drive(self, user, device, port_id, lag_port_id):
        """POST the LAG link as *user*, with the real permission gate and real restrict() running."""
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        request = RequestFactory().post(
            "/lag/", {"port_id": str(port_id), "lag_port_id": str(lag_port_id), "server_key": "default"}
        )
        request.user = user
        view = SyncInterfaceLagView()
        return _post(view, request, object_type="device", object_id=device.pk)

    def test_lag_sync_refuses_an_out_of_scope_interface(self):
        from dcim.models import Device, Interface

        in_scope_device = make_device("relscope-in")
        out_of_scope_device = make_device("relscope-out")
        self._iface(in_scope_device, "Gi0/1", 700)
        member = self._iface(out_of_scope_device, "Gi0/1", 701)
        agg = self._iface(out_of_scope_device, "Po1", 702)
        user = self._writer(
            "relscope-user",
            [(Device, "view", None), (Interface, "change", {"device__name": "relscope-in"})],
        )

        response = self._drive(user, out_of_scope_device, 701, 702)

        assert response.status_code == 404
        member.refresh_from_db()
        agg.refresh_from_db()
        assert member.lag_id is None  # not linked
        assert agg.type != "lag"  # the aggregate was not promoted either

    def test_lag_sync_links_the_in_scope_interfaces(self):
        """The interfaces the grant DOES cover still link — the scoping must not over-block."""
        from dcim.models import Device, Interface

        device = make_device("relscope-ok")
        member = self._iface(device, "Gi0/1", 710)
        agg = self._iface(device, "Po1", 711)
        user = self._writer(
            "relscope-user-ok",
            [(Device, "view", None), (Interface, "change", {"device__name": "relscope-ok"})],
        )

        response = self._drive(user, device, 710, 711)

        assert response.status_code == 200
        member.refresh_from_db()
        agg.refresh_from_db()
        assert member.lag_id == agg.pk
        assert agg.type == "lag"
