"""
Coverage tests for views/sync/interfaces.py

SyncInterfacesView + DeleteNetBoxInterfacesView
Target: 95%+ coverage
"""

from unittest.mock import MagicMock, patch

import pytest

from netbox_librenms_plugin.interface_relationships import (
    build_interface_index,
    interface_owner_for_object,
    resolve_interface_by_port_id,
)
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
    message_texts,
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


def _cache_relationship(view, obj, relation_field, source_id, related_id, source_name="", related_name=""):
    """Seed the real VC-scoped cache snapshot required by an inline relationship POST."""
    from django.core.cache import cache

    from netbox_librenms_plugin.utils import get_librenms_sync_device

    cache_obj = get_librenms_sync_device(obj, server_key="default") or obj
    cache_key = view.get_cache_key(cache_obj, "ports", "default")
    relationships = {"lag_members": {}, "sub_interfaces": {}}
    relationships[relation_field][source_id] = related_id
    ports = [{"port_id": source_id, "ifName": source_name or f"port-{source_id}"}]
    if related_id != source_id:
        ports.append({"port_id": related_id, "ifName": related_name or f"port-{related_id}"})
    cache.set(
        cache_key,
        {
            "ports": ports,
            "port_stack_relationships": relationships,
        },
    )
    return cache_key


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
    """Exercise the object-type-specific parent endpoint with real rows and grants."""

    def test_vm_post_links_real_vminterfaces(self):
        from types import SimpleNamespace

        from virtualization.models import VirtualMachine, VMInterface

        from netbox_librenms_plugin.tests.conftest import make_vm
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        vm = make_vm("parent-permissions-vm")
        child = VMInterface.objects.create(virtual_machine=vm, name="Ethernet1.100")
        parent = VMInterface.objects.create(virtual_machine=vm, name="Ethernet1")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(parent, 11, "default")
        child.save()
        parent.save()
        user = make_user_with_perms(
            "parent-permissions-vm",
            [("view", VirtualMachine), ("view", VMInterface), ("change", VMInterface)],
        )
        request = _make_request(
            {"port_id": "10", "parent_port_id": "11", "parent_name": "Ethernet1"},
            user=user,
        )
        view = SyncInterfaceParentView()
        view._librenms_api = SimpleNamespace(server_key="default")
        _cache_relationship(view, vm, "sub_interfaces", 10, 11, child.name, parent.name)

        response = _post(view, request, object_type="virtualmachine", object_id=vm.pk)

        assert response.status_code == 200, response.content
        child.refresh_from_db()
        assert child.parent_id == parent.pk

    def test_invalid_type_raises_http404(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView
        from django.http import Http404
        import pytest

        view = SyncInterfaceParentView()
        with pytest.raises(Http404):
            view.post(_make_request(), "invalid", 1)


class TestSyncInterfaceLagViewPermissions:
    def test_vm_post_is_rejected_before_any_lookup(self):
        from django.http import Http404
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        with pytest.raises(Http404):
            SyncInterfaceLagView().post(_make_request(), "virtualmachine", 1)


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


def test_interface_preference_save_checks_csrf_and_http_status():
    """Preference persistence must report missing CSRF and rejected HTTP responses."""
    handler = _js_block(
        _js_source(),
        "function updateInterfaceNameField()",
        "function setInterfaceNameFieldFromURL()",
    )

    assert "const csrfToken = getCsrfToken();" in handler
    assert "missing CSRF token" in handler
    assert "if (!response.ok)" in handler


def test_interface_member_verify_rebinds_replaced_vlan_controls():
    """Replacing the VLAN cell must bind the new edit button before the user can click it."""
    handler = _js_block(
        _js_source(),
        "function handleInterfaceChange(select, value)",
        "function handleCableChange(select, value)",
    )
    vlan_repaint = "vlanCell.innerHTML = formattedRow.vlans;"
    assert vlan_repaint in handler
    assert "initializeVlanGroupSelects();" in handler[handler.index(vlan_repaint) :]


def test_interface_member_verify_posts_the_origin_page_device():
    """The verify response must preserve page-level migrated mode across a member change."""
    handler = _js_block(
        _js_source(),
        "function handleInterfaceChange(select, value)",
        "function handleCableChange(select, value)",
    )
    assert "origin_device_id:" in handler
    assert "[data-interface-origin-device-id]" in handler


def test_vlan_apply_all_skips_groups_unavailable_to_a_target_row():
    handler = _js_block(
        _js_source(),
        "function applyButtonUpdates()",
        "// Persist overrides in server cache",
    )

    assert "const matchedGroup = groups.find" in handler
    assert "if (newGroupId && !matchedGroup) return;" in handler


def test_reenabling_relationship_autoselect_replays_checked_rows():
    """Checked children rebuild cross-page parent inputs when auto-select is re-enabled."""
    import re

    handler = _js_block(
        _js_source(),
        "// Keep cross-page parent notices symmetric",
        "Show a brief inline notice",
    )
    assert re.search(r"toggle\.matches\(\s*['\"]#autoSelectLagMembers['\"]\s*\)", handler), (
        "toggle handler must key on #autoSelectLagMembers"
    )
    assert re.search(r"if\s*\(\s*toggle\.checked\s*\)", handler), "re-enable branch must gate on toggle.checked"
    # Backreference pins the string closing after the disabled-row exclusion. A suffix such as
    # :not(*) (valid CSS, matches nothing) must fail this instead of slipping past a prefix check.
    assert re.search(
        r"querySelectorAll\(\s*(['\"])input\[name=.select.\]:checked:not\(:disabled\)\1\s*\)",
        handler,
    ), "re-enable branch must replay exactly the checked, enabled rows"
    assert re.search(r"dispatchEvent\(\s*new\s+Event\(\s*['\"]change['\"]\s*,\s*\{\s*bubbles:\s*true", handler), (
        "replay must re-dispatch a bubbling change event"
    )


def test_cross_page_parent_does_not_copy_child_member_target():
    """An off-page parent is resolved independently instead of inheriting the child's owner."""
    handler = _js_block(
        _js_source(),
        "// --- Sub-interface: select parent when checking ---",
        "// --- Sub-interface: undo parent auto-selection",
    )

    assert "_showParentCrossPageNotice" in handler
    assert "auto_parent_port_id" not in handler
    assert "device_selection_" not in handler


def test_interface_verify_application_failure_is_reported():
    """A 2xx rejection must expose its server-provided reason before rollback."""
    rejected = _js_block(
        _js_source(),
        "// 2xx with data.status !== 'success'",
        "rollbackToLastVerified();",
    )
    assert "console.error('Interface verification rejected:', data.error || data.message" in rejected


def test_relationship_sync_missing_data_shows_alert_icon():
    """A relationship button with incomplete data must show a visible failure state."""
    rejected = _js_block(
        _js_source(),
        "if (!portId || !relatedPortId || !objectId || !url)",
        "// Fail fast: a missing input",
    )
    assert "btn.innerHTML = '<i class=\"mdi mdi-alert text-danger\"></i>'" in rejected
    assert "Required relationship data is unavailable." in rejected


def test_cross_page_parent_notice_close_button_has_accessible_name():
    """The icon-only notice close button must expose its purpose to screen readers."""
    import re

    notice = _js_block(
        _js_source(),
        "function _showParentCrossPageNotice(parentName)",
        "// VIRTUAL CHASSIS & VRF HANDLING",
    )
    assert re.search(
        r"closeBtn\.setAttribute\(\s*['\"]aria-label['\"]\s*,\s*['\"]Close['\"]\s*\)",
        notice,
    )


def test_relationship_row_selectors_are_css_escaped():
    import re

    handler = _js_block(
        _js_source(),
        "// --- Sub-interface: select parent when checking ---",
        "// Keep cross-page parent notices symmetric",
    )
    source = _js_source()
    assert 'data-member-of-lag="' + "' + CSS.escape(portId)" in source, (
        "the LAG-member selector must CSS.escape the port id"
    )
    assert source.count('data-port-id="' + "' + CSS.escape(parentPortId)") == 2, (
        "both parent-row selectors must CSS.escape the port id"
    )
    assert re.search(r"data-parent-port-id=\"'\s*\+\s*CSS\.escape\(parentPortId\)", handler), (
        "the sibling-row attribute selector must CSS.escape the port id"
    )


def test_interface_select_all_has_one_change_handler():
    source = _js_source()
    assert source.count("toggleAll.addEventListener('change'") == 1


def test_interface_bulk_selection_ignores_disabled_rows():
    """Client bulk actions must match the browser form contract for disabled controls."""
    source = _js_source()
    table_handler = _js_block(source, "function initializeTableCheckboxes(tableId)", "function initializeCheckboxes()")
    bulk_handler = _js_block(source, "function initializeBulkEditApply()", "// TABLE FILTERING")

    assert 'td input[name="select"]:not(:disabled)' in table_handler
    assert 'input[name="select"]:checked:not(:disabled)' in bulk_handler
    assert 'input[name="select"]:not(:disabled)' in bulk_handler


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
# SyncInterfacesView.get_selected_port_ids
# ===========================================================================


class TestSyncInterfacesViewGetSelectedPortIds:
    def test_empty_selection_returns_none(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        req = _make_request(post_data={})
        result = view.get_selected_port_ids(req)
        assert result is None
        assert message_texts(req, "error") == ["No interfaces selected for synchronization."]

    def test_with_selection_returns_set(self):
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        view = object.__new__(SyncInterfacesView)
        req = _make_request(post_data={"select": ["10", "11"]})
        result = view.get_selected_port_ids(req)
        assert result == {10, 11}


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

    def test_name_fallback_does_not_match_an_interface_bound_to_another_port(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("table-conflicting-port-id")
        wrong_interface = make_interface(device, "Ethernet1")
        aggregate = make_interface(device, "Port-Channel1", iface_type="lag")
        set_librenms_device_id(wrong_interface, 30, "default")
        set_librenms_device_id(aggregate, 40, "default")
        wrong_interface.save()
        aggregate.save()
        snapshot = {
            "ports": [
                {"port_id": 20, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
                {"port_id": 40, "ifName": "Port-Channel1", "ifType": "ieee8023adLag"},
            ],
            "port_stack_relationships": {
                "lag_members": {20: 40},
                "sub_interfaces": {},
            },
        }
        request = _make_request()
        view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        view.request = request

        context = view.get_context_data(
            request,
            device,
            "ifName",
            "default",
            fresh_data=snapshot,
            sync_device=device,
        )

        row = snapshot["ports"][0]
        assert row["exists_in_netbox"] is False
        assert row["netbox_interface"] is None
        assert "lag-sync-btn" not in str(context["table"].render_parent(None, row))

    def test_relationship_button_is_hidden_when_related_interface_does_not_exist(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("table-missing-related-interface")
        source = make_interface(device, "Ethernet1")
        set_librenms_device_id(source, 10, "default")
        source.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
                {"port_id": 20, "ifName": "Port-Channel1", "ifType": "ieee8023adLag"},
            ],
            "port_stack_relationships": {
                "lag_members": {10: 20},
                "sub_interfaces": {},
            },
        }
        request = _make_request()
        view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        view.request = request

        context = view.get_context_data(
            request,
            device,
            "ifName",
            "default",
            fresh_data=snapshot,
            sync_device=device,
        )

        row = snapshot["ports"][0]
        assert row["lag_sync_status"] == "missing_nb"
        assert "lag-sync-btn" not in str(context["table"].render_parent(None, row))

    def test_relationship_button_resolves_an_unbound_same_name_source(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("table-unbound-relationship-source")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(parent, 20, "default")
        parent.save()
        snapshot = {
            "ports": [
                {"port_id": 11, "ifName": "Ethernet1.100", "ifType": "l2vlan"},
                {"port_id": 20, "ifName": "Ethernet1", "ifType": "ethernetCsmacd"},
            ],
            "port_stack_relationships": {
                "lag_members": {},
                "sub_interfaces": {11: 20},
            },
        }
        render_request = _make_request()
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                device,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=device,
            )

            row = snapshot["ports"][0]
            rendered = str(context["table"].render_parent(None, row))
            assert "parent-sync-btn" in rendered
            request = _make_request(
                {
                    "port_id": "11",
                    "source_name": "Ethernet1.100",
                    "interface_name_field": "ifName",
                    "parent_port_id": "20",
                    "parent_name": "Ethernet1",
                }
            )
            relationship_view = SyncInterfaceParentView()
            relationship_view._librenms_api = SimpleNamespace(server_key="default")

            response = _post(
                relationship_view,
                request,
                object_type="device",
                object_id=device.pk,
            )
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200, response.content
        child.refresh_from_db()
        assert child.parent_id == parent.pk

    def test_unrelated_duplicate_cached_id_does_not_break_inline_parent_sync(self):
        """Duplicate cached IDs outside the current edge must not make its button fail."""
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("inline-unrelated-duplicate-id")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        for interface, port_id in ((child, 10), (parent, 20)):
            set_librenms_device_id(interface, port_id, "default")
            interface.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": child.name, "ifType": "l2vlan"},
                {"port_id": 20, "ifName": parent.name, "ifType": "ethernetCsmacd"},
                {"port_id": 30, "ifName": "unrelated-a", "ifType": "ethernetCsmacd"},
                {"port_id": "030", "ifName": "unrelated-b", "ifType": "ethernetCsmacd"},
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        render_request = _make_request()
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                device,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=device,
            )
            source_row = snapshot["ports"][0]
            assert "parent-sync-btn" in str(context["table"].render_parent(None, source_row))

            relationship_view = SyncInterfaceParentView()
            relationship_view._librenms_api = SimpleNamespace(server_key="default")
            response = _post(
                relationship_view,
                _make_request(
                    {
                        "port_id": "10",
                        "interface_name_field": "ifName",
                        "parent_port_id": "20",
                    }
                ),
                object_type="device",
                object_id=device.pk,
            )
        finally:
            cache.delete(cache_key)

        assert response.status_code == 200, response.content
        child.refresh_from_db()
        assert child.parent_id == parent.pk

    def test_duplicate_display_name_does_not_make_unbound_source_actionable(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("table-ambiguous-unbound-source")
        source = make_interface(device, "Ethernet", iface_type="virtual")
        first_parent = make_interface(device, "Parent1")
        second_parent = make_interface(device, "Parent2")
        set_librenms_device_id(first_parent, 20, "default")
        set_librenms_device_id(second_parent, 21, "default")
        first_parent.save()
        second_parent.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": "Gi0/1", "ifDescr": "Ethernet", "ifType": "l2vlan"},
                {"port_id": 11, "ifName": "Gi0/2", "ifDescr": "Ethernet", "ifType": "l2vlan"},
                {"port_id": 20, "ifName": "Parent1", "ifDescr": "Parent1", "ifType": "ethernetCsmacd"},
                {"port_id": 21, "ifName": "Parent2", "ifDescr": "Parent2", "ifType": "ethernetCsmacd"},
            ],
            "port_stack_relationships": {
                "lag_members": {},
                "sub_interfaces": {10: 20, 11: 21},
            },
        }
        render_request = _make_request()
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                device,
                "ifDescr",
                "default",
                fresh_data=snapshot,
                sync_device=device,
            )
            for row in snapshot["ports"][:2]:
                assert row["netbox_interface"] is None
                assert "parent-sync-btn" not in str(context["table"].render_parent(None, row))

            request = _make_request(
                {
                    "port_id": "10",
                    "source_name": "Ethernet",
                    "interface_name_field": "ifDescr",
                    "parent_port_id": "20",
                    "parent_name": "Parent1",
                }
            )
            relationship_view = SyncInterfaceParentView()
            relationship_view._librenms_api = SimpleNamespace(server_key="default")
            response = _post(relationship_view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 404
        source.refresh_from_db()
        assert source.parent_id is None

    def test_inline_relationship_rejects_an_edge_replaced_in_the_current_cache(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("stale-inline-parent")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        old_parent = make_interface(device, "Ethernet1")
        current_parent = make_interface(device, "Ethernet2")
        for interface, port_id in ((child, 10), (old_parent, 20), (current_parent, 30)):
            set_librenms_device_id(interface, port_id, "default")
            interface.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": child.name, "ifType": "l2vlan"},
                {"port_id": 20, "ifName": old_parent.name, "ifType": "ethernetCsmacd"},
                {"port_id": 30, "ifName": current_parent.name, "ifType": "ethernetCsmacd"},
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 30}},
        }
        view = SyncInterfaceParentView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        request = _make_request(
            {
                "port_id": "10",
                "source_name": child.name,
                "interface_name_field": "ifName",
                "parent_port_id": "20",
                "parent_name": old_parent.name,
            }
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 409
        child.refresh_from_db()
        assert child.parent_id is None

    def test_inline_relationship_rejects_conflicting_canonical_cache_edges(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("conflicting-canonical-inline-edge")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        first_parent = make_interface(device, "Ethernet1")
        second_parent = make_interface(device, "Ethernet2")
        for interface, port_id in ((child, 10), (first_parent, 20), (second_parent, 30)):
            set_librenms_device_id(interface, port_id, "default")
            interface.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": child.name},
                {"port_id": 20, "ifName": first_parent.name},
                {"port_id": 30, "ifName": second_parent.name},
            ],
            "port_stack_relationships": {
                "lag_members": {},
                "sub_interfaces": {"10": 20, "010": 30},
            },
        }
        view = SyncInterfaceParentView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        request = _make_request(
            {
                "port_id": "10",
                "interface_name_field": "ifName",
                "parent_port_id": "20",
            }
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 409
        child.refresh_from_db()
        assert child.parent_id is None

    def test_duplicate_unbound_related_name_in_vc_has_no_inline_action(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        _virtual_chassis, (member1, member2) = make_virtual_chassis_members("ambiguous-related-vc")
        source = make_interface(member1, "Ethernet1")
        first_aggregate = make_interface(member1, "Port-Channel1")
        second_aggregate = make_interface(member2, "Port-Channel1")
        set_librenms_device_id(source, 10, "default")
        source.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": source.name, "ifType": "ethernetCsmacd"},
                {"port_id": 20, "ifName": "Port-Channel1", "ifType": "ieee8023adLag"},
            ],
            "port_stack_relationships": {"lag_members": {10: 20}, "sub_interfaces": {}},
        }
        render_request = _make_request()
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(member1, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                member1,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=member1,
            )
            source_row = snapshot["ports"][0]
            assert "lag-sync-btn" not in str(context["table"].render_parent(None, source_row))

            request = _make_request(
                {
                    "port_id": "10",
                    "interface_name_field": "ifName",
                    "lag_port_id": "20",
                }
            )
            relationship_view = SyncInterfaceLagView()
            relationship_view._librenms_api = SimpleNamespace(server_key="default")
            response = _post(relationship_view, request, object_type="device", object_id=member1.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 404
        source.refresh_from_db()
        first_aggregate.refresh_from_db()
        second_aggregate.refresh_from_db()
        assert source.lag_id is None
        assert first_aggregate.type != "lag"
        assert second_aggregate.type != "lag"

    def test_duplicate_related_port_id_in_vc_has_no_inline_action(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        _virtual_chassis, (member1, member2) = make_virtual_chassis_members("duplicate-related-id-vc")
        source = make_interface(member1, "Ethernet1")
        first_aggregate = make_interface(member1, "Port-Channel1")
        second_aggregate = make_interface(member2, "Port-Channel2")
        for interface, port_id in ((source, 10), (first_aggregate, 20), (second_aggregate, 20)):
            set_librenms_device_id(interface, port_id, "default")
            interface.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": source.name, "ifType": "ethernetCsmacd"},
                {"port_id": 20, "ifName": first_aggregate.name, "ifType": "ieee8023adLag"},
            ],
            "port_stack_relationships": {"lag_members": {10: 20}, "sub_interfaces": {}},
        }
        render_request = _make_request()
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(member1, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                member1,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=member1,
            )
            source_row = snapshot["ports"][0]
            assert "lag-sync-btn" not in str(context["table"].render_parent(None, source_row))

            request = _make_request(
                {
                    "port_id": "10",
                    "interface_name_field": "ifName",
                    "lag_port_id": "20",
                }
            )
            relationship_view = SyncInterfaceLagView()
            relationship_view._librenms_api = SimpleNamespace(server_key="default")
            response = _post(relationship_view, request, object_type="device", object_id=member1.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 404
        source.refresh_from_db()
        assert source.lag_id is None

    def test_duplicate_cached_related_port_id_has_no_inline_action(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("duplicate-cached-related-id")
        source = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(source, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        source.save()
        parent.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": source.name, "ifType": "l2vlan"},
                {"port_id": 20, "ifName": parent.name, "ifType": "ethernetCsmacd"},
                {"port_id": "020", "ifName": "duplicate-parent", "ifType": "ethernetCsmacd"},
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        render_request = _make_request()
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                device,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=device,
            )
            source_row = snapshot["ports"][0]
            assert "parent-sync-btn" not in str(context["table"].render_parent(None, source_row))

            request = _make_request({"port_id": "10", "interface_name_field": "ifName", "parent_port_id": "20"})
            relationship_view = SyncInterfaceParentView()
            relationship_view._librenms_api = SimpleNamespace(server_key="default")
            response = _post(relationship_view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 409
        source.refresh_from_db()
        assert source.parent_id is None

    def test_duplicate_cached_source_port_id_is_disabled_and_verify_rejects_it(self):
        import json

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import (
            DeviceInterfaceTableView,
            SingleInterfaceVerifyView,
        )

        device = make_device("duplicate-cached-source-id")
        source = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(source, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        source.save()
        parent.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": source.name, "ifType": "l2vlan"},
                {"port_id": "010", "ifName": "duplicate-source", "ifType": "l2vlan"},
                {"port_id": 20, "ifName": parent.name, "ifType": "ethernetCsmacd"},
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        user = make_superuser("duplicate-cached-source-id")
        render_request = _make_request(user=user)
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            table_view.get_context_data(
                render_request,
                device,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=device,
            )
            assert all(row["sync_target_resolvable"] is False for row in snapshot["ports"][:2])

            verify_request = make_request(
                "post",
                json.dumps(
                    {
                        "device_id": device.pk,
                        "interface_name": "duplicate-source",
                        "interface_name_field": "ifName",
                        "port_id": 10,
                    }
                ),
                user=user,
                path="/verify/",
                content_type="application/json",
            )
            verify_view = SingleInterfaceVerifyView()
            verify_view._librenms_api = api
            response = verify_view.post(verify_request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 404

    def test_missing_port_id_disables_member_selection_and_verify_rejects_it(self):
        import json

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
        from netbox_librenms_plugin.views.object_sync.devices import (
            DeviceInterfaceTableView,
            SingleInterfaceVerifyView,
        )

        _virtual_chassis, (page_device, _member) = make_virtual_chassis_members("missing-row-port-id")
        snapshot = {
            "ports": [
                {"port_id": None, "ifName": "Gi0/1", "ifDescr": "Ethernet", "ifType": "ethernetCsmacd"},
                {"port_id": None, "ifName": "Gi0/2", "ifDescr": "Ethernet", "ifType": "ethernetCsmacd"},
            ],
            "port_stack_relationships": {},
        }
        user = make_superuser("missing-row-port-id")
        render_request = _make_request(user=user)
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(page_device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                page_device,
                "ifDescr",
                "default",
                fresh_data=snapshot,
                sync_device=page_device,
            )
            assert snapshot["ports"][1]["sync_target_resolvable"] is False
            assert "disabled" in str(context["table"].render_device_selection(None, snapshot["ports"][1]))

            verify_request = make_request(
                "post",
                json.dumps(
                    {
                        "device_id": page_device.pk,
                        "interface_name": "Ethernet",
                        "interface_name_field": "ifDescr",
                        "port_id": None,
                    }
                ),
                user=user,
                path="/verify/",
                content_type="application/json",
            )
            verify_view = SingleInterfaceVerifyView()
            verify_view._librenms_api = api
            response = verify_view.post(verify_request)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 404

    def test_duplicate_source_port_id_has_no_inline_action(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        device = make_device("duplicate-source-id")
        source = make_interface(device, "Ethernet1")
        duplicate_source = make_interface(device, "Ethernet2")
        aggregate = make_interface(device, "Port-Channel1", iface_type="lag")
        for interface, port_id in ((source, 10), (duplicate_source, 10), (aggregate, 20)):
            set_librenms_device_id(interface, port_id, "default")
            interface.save()
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": source.name, "ifType": "ethernetCsmacd"},
                {"port_id": 20, "ifName": aggregate.name, "ifType": "ieee8023adLag"},
            ],
            "port_stack_relationships": {"lag_members": {10: 20}, "sub_interfaces": {}},
        }
        render_request = _make_request()
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                device,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=device,
            )
            source_row = snapshot["ports"][0]
            assert "lag-sync-btn" not in str(context["table"].render_parent(None, source_row))

            request = _make_request(
                {
                    "port_id": "10",
                    "interface_name_field": "ifName",
                    "lag_port_id": "20",
                }
            )
            relationship_view = SyncInterfaceLagView()
            relationship_view._librenms_api = SimpleNamespace(server_key="default")
            response = _post(relationship_view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 404
        source.refresh_from_db()
        duplicate_source.refresh_from_db()
        assert source.lag_id is None
        assert duplicate_source.lag_id is None

    def test_hidden_related_interface_has_no_inline_action(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("hidden-related-interface-action")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        child.save()
        parent.save()
        user = make_user_with_perms("hidden-related-interface-action", [("view", Device)])
        user = grant(user, "change", Interface, constraints={"id": child.pk})
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": child.name, "ifType": "l2vlan"},
                {"port_id": 20, "ifName": parent.name, "ifType": "ethernetCsmacd"},
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        render_request = _make_request(user=user)
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                device,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=device,
            )
            child_row = snapshot["ports"][0]
            assert "parent-sync-btn" not in str(context["table"].render_parent(None, child_row))

            request = _make_request(
                {
                    "port_id": "10",
                    "interface_name_field": "ifName",
                    "parent_port_id": "20",
                },
                user=user,
            )
            relationship_view = SyncInterfaceParentView()
            relationship_view._librenms_api = SimpleNamespace(server_key="default")
            response = _post(relationship_view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 404
        child.refresh_from_db()
        assert child.parent_id is None

    def test_hidden_virtual_chassis_owner_has_no_inline_action_or_target_option(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface, Rack
        from django.contrib.contenttypes.models import ContentType
        from django.core.cache import cache
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        _virtual_chassis, (page_device, hidden_member) = make_virtual_chassis_members("hidden-row-owner-vc")
        hidden_rack = Rack.objects.create(
            name="Hidden Row Owner Rack",
            site=hidden_member.site,
            status="active",
        )
        hidden_member.rack = hidden_rack
        hidden_member.save()
        hidden_group = VLANGroup.objects.create(
            name="Hidden Row Owner VLAN Group",
            slug="hidden-row-owner-vlan-group",
            scope_type=ContentType.objects.get_for_model(Rack),
            scope_id=hidden_rack.pk,
        )
        VLAN.objects.create(vid=100, name="Hidden Row Owner VLAN", group=hidden_group, status="active")
        source = make_interface(hidden_member, "Ethernet2.100", iface_type="virtual")
        parent = make_interface(page_device, "Ethernet1")
        set_librenms_device_id(source, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        source.save()
        parent.save()
        user = make_user_with_perms("hidden-row-owner-vc", [])
        user = grant(user, "view", Device, constraints={"id": page_device.pk})
        user = grant(user, "change", Interface, constraints={"id": source.pk})
        user = grant(user, "view", Interface, constraints={"id": parent.pk})
        snapshot = {
            "ports": [
                {
                    "port_id": 10,
                    "ifName": source.name,
                    "ifType": "l2vlan",
                    "untagged_vlan": None,
                    "tagged_vlans": [100],
                },
                {"port_id": 20, "ifName": parent.name, "ifType": "ethernetCsmacd"},
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        render_request = _make_request(user=user)
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(page_device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            context = table_view.get_context_data(
                render_request,
                page_device,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=page_device,
            )
            source_row = snapshot["ports"][0]
            table = context["table"]
            assert source_row["sync_target_resolvable"] is False
            assert [member.pk for member in table._vc_members] == [page_device.pk]
            assert str(hidden_member.pk) not in str(table.render_device_selection(None, source_row))
            assert "parent-sync-btn" not in str(table.render_parent(None, source_row))
            vlan_html = str(table.render_vlans(None, source_row))
            assert hidden_group.name not in vlan_html
            assert "vlan-group-hidden" not in vlan_html
            assert "vlan-edit-btn" not in vlan_html
            assert "data-vlan-groups" not in vlan_html

            request = _make_request(
                {"port_id": "10", "interface_name_field": "ifName", "parent_port_id": "20"},
                user=user,
            )
            relationship_view = SyncInterfaceParentView()
            relationship_view._librenms_api = SimpleNamespace(server_key="default")
            from django.http import Http404

            with pytest.raises(Http404):
                _post(
                    relationship_view,
                    request,
                    object_type="device",
                    object_id=hidden_member.pk,
                )
        finally:
            cache.delete(cache_key)

        source.refresh_from_db()
        assert source.parent_id is None

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


class TestInterfaceContextVirtualChassisOwner:
    def test_logical_row_uses_stable_id_owner_for_render_and_sync(self):
        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, other_member) = make_virtual_chassis_members("logical-row-owner")
        correct = make_interface(page_device, "Vlan2", iface_type="virtual")
        wrong = make_interface(other_member, "Vlan2", iface_type="virtual")
        correct.description = "original correct"
        wrong.description = "original other"
        set_librenms_device_id(correct, 10, "default")
        correct.save()
        wrong.save()

        port = {
            "port_id": 10,
            "ifName": "Vlan2",
            "ifDescr": "Vlan2",
            "ifAlias": "updated correct",
            "ifType": "l3ipvlan",
            "ifSpeed": None,
            "ifPhysAddress": None,
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "untagged_vlan": None,
            "tagged_vlans": [],
        }
        snapshot = {"ports": [port], "port_stack_relationships": {}}
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        request = _make_request()
        table_view = object.__new__(DeviceInterfaceTableView)
        table_view._librenms_api = api
        table_view.request = request

        context = table_view.get_context_data(
            request,
            page_device,
            "ifName",
            "default",
            fresh_data=snapshot,
            sync_device=page_device,
        )
        rendered_row = list(context["table"].data)[0]
        assert rendered_row["netbox_interface"].pk == correct.pk
        selected_member_id = context["table"]._resolve_row_member_id(rendered_row)
        assert selected_member_id == page_device.pk

        user = make_user_with_perms(
            "logical-row-owner",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        post_request = _make_request(
            post_data={
                "select": ["10"],
                "device_selection_10": str(selected_member_id),
                "exclude_columns": ["vlans", "mac_address", "mtu", "speed", "type"],
            },
            user=user,
        )
        sync_view = SyncInterfacesView()
        sync_view._librenms_api = api
        cache_key = sync_view.get_cache_key(page_device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            response = _post(sync_view, post_request, object_type="device", object_id=page_device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        correct.refresh_from_db()
        wrong.refresh_from_db()
        assert correct.description == "updated correct"
        assert wrong.description == "original other"

    def test_non_string_interface_type_does_not_crash_row_owner_resolution(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        _virtual_chassis, (page_device, _other_member) = make_virtual_chassis_members("invalid-row-type")
        port = {
            "port_id": 10,
            "ifName": "Vlan2",
            "ifDescr": "Vlan2",
            "ifAlias": "",
            "ifType": 123,
            "ifSpeed": None,
            "ifPhysAddress": None,
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "untagged_vlan": None,
            "tagged_vlans": [],
        }
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        request = _make_request()
        view = object.__new__(DeviceInterfaceTableView)
        view._librenms_api = api
        view.request = request

        context = view.get_context_data(
            request,
            page_device,
            "ifName",
            "default",
            fresh_data={"ports": [port], "port_stack_relationships": {}},
            sync_device=page_device,
        )

        rendered_row = list(context["table"].data)[0]
        assert rendered_row["netbox_interface"] is None
        assert rendered_row["selected_object_id"] == page_device.pk

    def test_remote_vc_row_uses_its_owner_rack_vlan_group(self):
        from django.core.cache import cache
        from django.contrib.contenttypes.models import ContentType
        from dcim.models import Rack
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        _virtual_chassis, (member1, member2) = make_virtual_chassis_members("row-vlan-owner")
        rack1 = Rack.objects.create(name="Row VLAN Rack 1", site=member1.site, status="active")
        rack2 = Rack.objects.create(name="Row VLAN Rack 2", site=member1.site, status="active")
        member1.rack = rack1
        member2.rack = rack2
        member1.save()
        member2.save()
        rack_type = ContentType.objects.get_for_model(Rack)
        group1 = VLANGroup.objects.create(
            name="Row VLAN Group 1",
            slug="row-vlan-group-1",
            scope_type=rack_type,
            scope_id=rack1.pk,
        )
        group2 = VLANGroup.objects.create(
            name="Row VLAN Group 2",
            slug="row-vlan-group-2",
            scope_type=rack_type,
            scope_id=rack2.pk,
        )
        VLAN.objects.create(vid=100, name="Rack 1 VLAN", group=group1, status="active")
        VLAN.objects.create(vid=100, name="Rack 2 VLAN", group=group2, status="active")
        interface = make_interface(member2, "Ethernet2")
        set_librenms_device_id(interface, 10, "default")
        interface.save()
        port = {
            "port_id": 10,
            "ifName": "Ethernet2",
            "ifType": "ethernetCsmacd",
            "ifAdminStatus": "up",
            "untagged_vlan": 100,
            "tagged_vlans": [],
        }
        request = _make_request()
        view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        view.request = request

        overrides_key = view.get_vlan_overrides_key(member1, "default")
        cache.set(overrides_key, {"100": str(group1.pk)})
        try:
            view.get_context_data(
                request,
                member1,
                "ifName",
                "default",
                fresh_data={"ports": [port], "port_stack_relationships": {}},
                sync_device=member1,
            )
        finally:
            cache.delete(overrides_key)

        assert port["selected_object_id"] == member2.pk
        assert port["vlan_group_map"][100]["group_id"] == str(group2.pk)

        VLAN.objects.filter(group=group2).delete()
        group2.delete()
        view.get_context_data(
            request,
            member1,
            "ifName",
            "default",
            fresh_data={"ports": [port], "port_stack_relationships": {}},
            sync_device=member1,
        )
        assert port["vlan_group_map"][100]["group_id"] == ""
        assert port["missing_vlans"] == [100]

    def test_remote_vc_target_sync_uses_its_rack_vlan_lookup(self):
        from types import SimpleNamespace

        from django.contrib.contenttypes.models import ContentType
        from django.core.cache import cache
        from dcim.models import Device, Interface, Rack
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (member1, member2) = make_virtual_chassis_members("sync-vlan-owner")
        rack1 = Rack.objects.create(name="Sync VLAN Rack 1", site=member1.site, status="active")
        rack2 = Rack.objects.create(name="Sync VLAN Rack 2", site=member1.site, status="active")
        member1.rack = rack1
        member2.rack = rack2
        member1.save()
        member2.save()
        rack_type = ContentType.objects.get_for_model(Rack)
        group1 = VLANGroup.objects.create(
            name="Sync VLAN Group 1",
            slug="sync-vlan-group-1",
            scope_type=rack_type,
            scope_id=rack1.pk,
        )
        group2 = VLANGroup.objects.create(
            name="Sync VLAN Group 2",
            slug="sync-vlan-group-2",
            scope_type=rack_type,
            scope_id=rack2.pk,
        )
        wrong_vlan = VLAN.objects.create(vid=100, name="Wrong member VLAN", group=group1, status="active")
        expected_vlan = VLAN.objects.create(vid=100, name="Target member VLAN", group=group2, status="active")
        VLAN.objects.create(vid=100, name="Global fallback VLAN", group=None, status="active")
        interface = make_interface(member2, "Ethernet2")
        set_librenms_device_id(interface, 10, "default")
        interface.save()
        snapshot = {
            "ports": [
                {
                    "port_id": "0010",
                    "ifName": "Ethernet2",
                    "ifType": "ethernetCsmacd",
                    "ifAdminStatus": "up",
                    "untagged_vlan": 100,
                    "tagged_vlans": [],
                }
            ],
            "port_stack_relationships": {},
        }
        render_request = _make_request()
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        context = table_view.get_context_data(
            render_request,
            member1,
            "ifName",
            "default",
            fresh_data=snapshot,
            sync_device=member1,
        )
        row = snapshot["ports"][0]
        assert row["port_id"] == 10
        assert 'name="device_selection_10"' in str(context["table"].render_device_selection(None, row))
        assert 'name="vlan_group_10_100"' in str(context["table"].render_vlans(None, row))
        user = make_user_with_perms(
            "sync-vlan-owner",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["10"],
                "device_selection_10": str(member2.pk),
                "vlan_group_10_100": str(group2.pk),
                "exclude_columns": ["mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(member1, "ports", "default")
        cache.set(cache_key, snapshot)

        try:
            response = _post(view, request, object_type="device", object_id=member1.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        interface.refresh_from_db()
        assert interface.untagged_vlan_id == expected_vlan.pk
        assert interface.untagged_vlan_id != wrong_vlan.pk

        interface.mode = ""
        interface.untagged_vlan = None
        interface.save(update_fields=["mode", "untagged_vlan"])
        stale_request = _make_request(
            post_data={
                "select": ["10"],
                "device_selection_10": str(member2.pk),
                "vlan_group_10_100": str(group1.pk),
                "exclude_columns": ["mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        stale_view = SyncInterfacesView()
        stale_view._librenms_api = SimpleNamespace(server_key="default")
        cache.set(cache_key, snapshot)
        try:
            stale_response = _post(stale_view, stale_request, object_type="device", object_id=member1.pk)
        finally:
            cache.delete(cache_key)

        assert stale_response.status_code == 302
        interface.refresh_from_db()
        assert interface.untagged_vlan_id == expected_vlan.pk

        snapshot["ports"][0]["untagged_vlan"] = None
        clear_request = _make_request(
            post_data={
                "select": ["10"],
                "device_selection_10": str(member2.pk),
                "exclude_columns": ["mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        clear_view = SyncInterfacesView()
        clear_view._librenms_api = SimpleNamespace(server_key="default")
        cache.set(cache_key, snapshot)
        try:
            clear_response = _post(clear_view, clear_request, object_type="device", object_id=member1.pk)
        finally:
            cache.delete(cache_key)

        assert clear_response.status_code == 302
        interface.refresh_from_db()
        assert interface.mode == ""
        assert interface.untagged_vlan_id is None


# ===========================================================================
# SyncInterfacesView.post — full flows
# ===========================================================================


class TestSyncInterfacesViewPost:
    def test_integrity_error_at_the_outer_commit_is_reported_not_a_500(self):
        """An IntegrityError escaping the outer atomic must redirect with an error, not propagate.

        The relationship pass catches IntegrityError around an inner savepoint. Postgres validates
        Django's DEFERRABLE INITIALLY DEFERRED foreign keys at the OUTERMOST commit, so a
        concurrently deleted related row surfaces past that handler. Injected here because a real
        deferred violation needs a concurrent deletion inside the commit window.
        """
        from types import SimpleNamespace

        from django.core.cache import cache
        from django.db import IntegrityError

        from netbox_librenms_plugin.tests.view_test_helpers import make_superuser, message_texts
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("outer-commit-integrity")
        port = {
            "port_id": 10,
            "ifName": "Ethernet1",
            "ifDescr": "Ethernet1",
            "ifAlias": "",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1_000_000_000,
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "untagged_vlan": None,
            "tagged_vlans": [],
        }
        request = _make_request(
            post_data={"select": ["10"], "exclude_columns": ["mac_address", "description", "mtu", "speed", "type"]},
            user=make_superuser("outer-commit-integrity"),
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, {"ports": [port], "port_stack_relationships": {}})

        try:
            with patch.object(
                SyncInterfacesView,
                "_sync_lag_and_parent_relationships",
                side_effect=IntegrityError("deferred FK violated at COMMIT"),
            ):
                response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert any("rolled back by a concurrent change" in text for text in message_texts(request, "error"))

    def test_vlan_maps_cover_only_selected_row_owners(self):
        """One selected row must not build VLAN scope maps for unrelated chassis members."""
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, target_device, _unrelated_member) = make_virtual_chassis_members(
            "selected-vlan-owner-scope",
            count=3,
        )
        port = {
            "port_id": 10,
            "ifName": "Ethernet2",
            "ifDescr": "Ethernet2",
            "ifAlias": "",
            "ifType": "ethernetCsmacd",
            "ifSpeed": 1_000_000_000,
            "ifPhysAddress": "",
            "ifMtu": 1500,
            "ifAdminStatus": "up",
            "untagged_vlan": None,
            "tagged_vlans": [],
        }
        request = _make_request(
            post_data={
                "select": ["10"],
                "device_selection_10": str(target_device.pk),
                "exclude_columns": ["mac_address", "description", "mtu", "speed", "type"],
            },
            user=make_superuser("selected-vlan-owner-scope"),
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(cache_key, {"ports": [port], "port_stack_relationships": {}})

        try:
            response = _post(view, request, object_type="device", object_id=page_device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert set(view._lookup_maps_by_owner) == {target_device.pk}

    def test_auto_selected_owner_materializes_only_port_id_candidates(self):
        """One inferred row must not hydrate every Interface in a large chassis."""
        from django.db.models.signals import post_init
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, target_device) = make_virtual_chassis_members("auto-owner-candidate-scope")
        target = make_interface(target_device, "Ethernet2")
        set_librenms_device_id(target, 10, "default")
        target.save()
        for index in range(40):
            make_interface(page_device, f"unrelated-{index}")
        request = _make_request(user=make_superuser("auto-owner-candidate-scope"))
        view = SyncInterfacesView()
        view.setup(request)
        materialized_interface_ids = []

        def capture_interface(instance, **_kwargs):
            materialized_interface_ids.append(instance.pk)

        post_init.connect(capture_interface, sender=Interface, weak=False)
        try:
            targets = view._resolve_auto_selected_target_ids(
                page_device,
                [{"port_id": 10, "ifName": "Ethernet2", "ifType": "ethernetCsmacd"}],
                {10},
                "ifName",
                "default",
                members=[page_device, target_device],
            )
        finally:
            post_init.disconnect(capture_interface, sender=Interface)

        assert targets == {10: target_device.pk}
        assert len(materialized_interface_ids) <= 5

    def test_auto_selected_owner_batches_large_candidate_lookup(self):
        """Many inferred rows must not produce one unbounded stable-ID query."""
        from django.db import connection

        from netbox_librenms_plugin.tests.view_test_helpers import make_superuser
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, target_device) = make_virtual_chassis_members("auto-owner-batched-lookup")
        port_ids = set(range(1, 301))
        ports = [
            {
                "port_id": port_id,
                "ifName": f"Ethernet2/{port_id}",
                "ifType": "ethernetCsmacd",
            }
            for port_id in port_ids
        ]
        request = _make_request(user=make_superuser("auto-owner-batched-lookup"))
        view = SyncInterfacesView()
        view.setup(request)
        parameter_counts = []

        def capture_parameters(execute, sql, params, many, context):
            parameter_counts.append(len(params or ()))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture_parameters):
            targets = view._resolve_auto_selected_target_ids(
                page_device,
                ports,
                port_ids,
                "ifName",
                "default",
                members=[page_device, target_device],
            )

        assert targets == {port_id: target_device.pk for port_id in port_ids}
        assert max(parameter_counts) < 2_000

    def test_excluding_type_does_not_promote_or_link_a_non_lag_aggregate(self):
        """The relationship pass must honor the form's explicit Type exclusion."""
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("bulk-exclude-lag-type")
        member = make_interface(device, "Ethernet1")
        aggregate = make_interface(device, "Port-Channel1", iface_type="other")
        for interface, port_id in ((member, 10), (aggregate, 20)):
            set_librenms_device_id(interface, port_id, "default")
            interface.save()
        user = make_user_with_perms(
            "bulk-exclude-lag-type",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["10"],
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": member.name,
                        "ifType": "ethernetCsmacd",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 20,
                        "ifName": aggregate.name,
                        "ifType": "ieee8023adLag",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {"lag_members": {10: 20}, "sub_interfaces": {}},
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        member.refresh_from_db()
        aggregate.refresh_from_db()
        assert aggregate.type == "other"
        assert member.lag_id is None

    def test_bulk_parent_link_resolves_a_unique_unbound_related_name(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("bulk-unbound-related-parent")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 11, "default")
        child.save()
        user = make_user_with_perms(
            "bulk-unbound-related-parent",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["11"],
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 11,
                        "ifName": child.name,
                        "ifType": "l2vlan",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 10,
                        "ifName": parent.name,
                        "ifType": "ethernetCsmacd",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {},
                    "sub_interfaces": {11: 10},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        child.refresh_from_db()
        parent.refresh_from_db()
        assert child.parent_id == parent.pk
        assert parent.custom_field_data.get("librenms_id") is None

    def test_cross_page_parent_keeps_unsubmitted_vlan_group(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("cross-page-parent-vlan")
        wrong_group = VLANGroup.objects.create(name="Parent fallback", slug="parent-fallback")
        kept_group = VLANGroup.objects.create(name="Parent selected", slug="parent-selected")
        wrong_vlan = VLAN.objects.create(vid=100, name="Parent fallback", group=wrong_group, status="active")
        kept_vlan = VLAN.objects.create(vid=100, name="Parent selected", group=kept_group, status="active")

        parent = make_interface(device, "Ethernet1")
        parent.mode = "access"
        parent.untagged_vlan = kept_vlan
        set_librenms_device_id(parent, 10, "default")
        parent.save()
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        set_librenms_device_id(child, 11, "default")
        child.save()

        user = make_user_with_perms(
            "cross-page-parent-vlan",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["11"],
                "auto_select_lag_members": "1",
                "exclude_columns": ["mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet1",
                        "ifType": "ethernetCsmacd",
                        "ifAdminStatus": "up",
                        "untagged_vlan": 100,
                        "tagged_vlans": [],
                    },
                    {
                        "port_id": 11,
                        "ifName": "Ethernet1.100",
                        "ifType": "l2vlan",
                        "ifAdminStatus": "up",
                        "untagged_vlan": None,
                        "tagged_vlans": [],
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {},
                    "sub_interfaces": {11: 10},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        parent.refresh_from_db()
        child.refresh_from_db()
        assert parent.untagged_vlan_id == kept_vlan.pk
        assert parent.untagged_vlan_id != wrong_vlan.pk
        assert child.parent_id == parent.pk

    def test_selected_lag_includes_member_from_another_page(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, member_device) = make_virtual_chassis_members("cross-page-lag-member")
        aggregate = make_interface(page_device, "Port-Channel1", iface_type="lag")
        member = make_interface(member_device, "Ethernet2")
        set_librenms_device_id(aggregate, 100, "default")
        set_librenms_device_id(member, 10, "default")
        aggregate.save()
        member.save()
        user = make_user_with_perms(
            "cross-page-lag-member",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["100"],
                "device_selection_100": str(page_device.pk),
                "auto_select_lag_members": "1",
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )

        class QueryCheckedSyncInterfacesView(SyncInterfacesView):
            target_lookup_queries = None

            def _sync_lag_and_parent_relationships(self, obj, *args, **kwargs):
                with CaptureQueriesContext(connection) as captured:
                    for port_id in self._selected_port_ids:
                        self._resolve_row_target_device(obj, port_id=port_id)
                self.target_lookup_queries = len(captured)
                return super()._sync_lag_and_parent_relationships(obj, *args, **kwargs)

        view = QueryCheckedSyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet2",
                        "ifType": "ethernetCsmacd",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 100,
                        "ifName": "Port-Channel1",
                        "ifType": "ieee8023adLag",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {10: 100},
                    "sub_interfaces": {},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=page_device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        aggregate.refresh_from_db()
        member.refresh_from_db()
        assert member.lag_id == aggregate.pk
        assert aggregate.device_id == page_device.pk
        assert member.device_id == member_device.pk
        assert view.target_lookup_queries == 0

    @pytest.mark.parametrize("physical_ifname", ["ge-2/0/0", "2/1/1"])
    def test_off_page_physical_member_uses_ifname_owner_in_ifdescr_mode(self, physical_ifname):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, member_device) = make_virtual_chassis_members("ifdescr-cross-page-member")
        aggregate = make_interface(member_device, "Bundle", iface_type="lag")
        set_librenms_device_id(aggregate, 100, "default")
        aggregate.save()
        user = make_user_with_perms(
            "ifdescr-cross-page-member",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["100"],
                "device_selection_100": str(member_device.pk),
                "auto_select_lag_members": "1",
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            get_data={"interface_name_field": "ifDescr"},
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": physical_ifname,
                        "ifDescr": "uplink",
                        "ifType": "ethernetCsmacd",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 100,
                        "ifName": "Port-Channel1",
                        "ifDescr": aggregate.name,
                        "ifType": "ieee8023adLag",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {10: 100},
                    "sub_interfaces": {},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=page_device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        created_member = Interface.objects.get(device=member_device, name="uplink")
        assert created_member.lag_id == aggregate.pk
        assert not Interface.objects.filter(device=page_device, name="uplink").exists()

    def test_same_page_lag_link_can_span_virtual_chassis_members(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, other_member) = make_virtual_chassis_members("cross-member-lag")
        aggregate = make_interface(page_device, "Port-Channel1", iface_type="lag")
        member = make_interface(other_member, "Ethernet2")
        set_librenms_device_id(aggregate, 100, "default")
        set_librenms_device_id(member, 10, "default")
        aggregate.save()
        member.save()
        user = make_user_with_perms(
            "cross-member-lag",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["100", "10"],
                "device_selection_100": str(page_device.pk),
                "device_selection_10": str(other_member.pk),
                "auto_select_lag_members": "1",
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet2",
                        "ifType": "ethernetCsmacd",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 100,
                        "ifName": "Port-Channel1",
                        "ifType": "ieee8023adLag",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {10: 100},
                    "sub_interfaces": {},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=page_device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        aggregate.refresh_from_db()
        member.refresh_from_db()
        assert member.lag_id == aggregate.pk
        assert aggregate.device_id == page_device.pk
        assert member.device_id == other_member.pk

    def test_same_page_parent_link_can_span_virtual_chassis_members(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, other_member) = make_virtual_chassis_members("cross-member-parent")
        parent = make_interface(page_device, "Ethernet1")
        child = make_interface(other_member, "Ethernet2.100", iface_type="virtual")
        set_librenms_device_id(parent, 10, "default")
        set_librenms_device_id(child, 11, "default")
        parent.save()
        child.save()
        user = make_user_with_perms(
            "cross-member-parent",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["11", "10"],
                "device_selection_11": str(other_member.pk),
                "device_selection_10": str(page_device.pk),
                "auto_select_lag_members": "1",
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet2",
                        "ifType": "ethernetCsmacd",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 11,
                        "ifName": "Ethernet2.100",
                        "ifType": "l2vlan",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {},
                    "sub_interfaces": {11: 10},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=page_device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        parent.refresh_from_db()
        child.refresh_from_db()
        assert child.parent_id == parent.pk
        assert parent.device_id == page_device.pk
        assert child.device_id == other_member.pk

    def test_invalid_selected_parent_target_does_not_fall_back_to_another_member(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, other_member) = make_virtual_chassis_members("invalid-parent-target")
        outside_device = make_device("invalid-parent-target-outside")
        child = make_interface(page_device, "Ethernet1.100", iface_type="virtual")
        existing_parent = make_interface(other_member, "Ethernet1")
        set_librenms_device_id(child, 11, "default")
        set_librenms_device_id(existing_parent, 10, "default")
        child.save()
        existing_parent.save()
        user = make_user_with_perms(
            "invalid-parent-target",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["11", "10"],
                "device_selection_11": str(page_device.pk),
                "device_selection_10": str(outside_device.pk),
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet1",
                        "ifType": "ethernetCsmacd",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 11,
                        "ifName": "Ethernet1.100",
                        "ifType": "l2vlan",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {},
                    "sub_interfaces": {11: 10},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=page_device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        child.refresh_from_db()
        existing_parent.refresh_from_db()
        assert child.parent_id is None
        assert existing_parent.device_id == other_member.pk

    def test_auto_selected_view_only_parent_keeps_its_existing_member_owner(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, other_member) = make_virtual_chassis_members("view-only-parent-owner")
        child = make_interface(page_device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(other_member, "Ethernet1", iface_type="virtual")
        set_librenms_device_id(child, 11, "default")
        set_librenms_device_id(parent, 10, "default")
        child.save()
        parent.save()
        user = make_user_with_perms(
            "view-only-parent-owner",
            [("view", Device), ("add", Interface)],
        )
        user = grant(user, "change", Interface, constraints={"pk": child.pk})
        user = grant(user, "view", Interface, constraints={"pk": parent.pk})
        request = _make_request(
            post_data={
                "select": ["11"],
                "device_selection_11": str(page_device.pk),
                "auto_select_lag_members": "1",
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet1",
                        "ifType": "l3ipvlan",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 11,
                        "ifName": "Ethernet1.100",
                        "ifType": "l2vlan",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {},
                    "sub_interfaces": {11: 10},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=page_device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        child.refresh_from_db()
        parent.refresh_from_db()
        assert child.parent_id == parent.pk
        assert parent.device_id == other_member.pk

    def test_auto_selected_unbound_logical_parent_without_owner_signal_is_skipped(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (page_device, child_member) = make_virtual_chassis_members("unbound-logical-parent-owner")
        child = make_interface(child_member, "Ethernet2.100", iface_type="virtual")
        set_librenms_device_id(child, 11, "default")
        child.save()
        user = make_user_with_perms(
            "unbound-logical-parent-owner",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["11"],
                "device_selection_11": str(child_member.pk),
                "auto_select_lag_members": "1",
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(page_device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Vlan100",
                        "ifType": "l3ipvlan",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 11,
                        "ifName": child.name,
                        "ifType": "l2vlan",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {},
                    "sub_interfaces": {11: 10},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=page_device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        child.refresh_from_db()
        assert child.parent_id is None
        assert not Interface.objects.filter(
            device__virtual_chassis=page_device.virtual_chassis, name="Vlan100"
        ).exists()

    def test_off_page_parent_selection_expands_to_all_ancestors(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.models import InterfaceTypeMapping
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("nested-off-page-parent")
        InterfaceTypeMapping.objects.create(librenms_type="nestedVirtual", netbox_type="virtual")
        InterfaceTypeMapping.objects.create(librenms_type="nestedPhysical", netbox_type="other")
        user = make_user_with_perms(
            "nested-off-page-parent",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["12"],
                "auto_select_lag_members": "1",
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet1",
                        "ifType": "nestedPhysical",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 11,
                        "ifName": "Ethernet1.100",
                        "ifType": "nestedVirtual",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 12,
                        "ifName": "Ethernet1.100.200",
                        "ifType": "nestedVirtual",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {
                    "lag_members": {},
                    "sub_interfaces": {12: 11, 11: 10},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        grandparent = Interface.objects.get(device=device, name="Ethernet1")
        parent = Interface.objects.get(device=device, name="Ethernet1.100")
        child = Interface.objects.get(device=device, name="Ethernet1.100.200")
        assert parent.parent_id == grandparent.pk
        assert child.parent_id == parent.pk

    def test_selected_row_with_blank_active_name_is_skipped(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("blank-selected-interface-name")
        child = make_interface(device, "Ethernet2", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 11, "default")
        set_librenms_device_id(parent, 10, "default")
        child.save()
        parent.save()
        user = make_user_with_perms(
            "blank-selected-interface-name",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["11"],
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            get_data={"interface_name_field": "ifDescr"},
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 11,
                        "ifName": "Ethernet2",
                        "ifDescr": None,
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 10,
                        "ifName": "Ethernet1",
                        "ifDescr": "Ethernet1",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {11: 10}},
            },
        )
        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        child.refresh_from_db()
        assert child.parent_id is None
        assert message_texts(request, "warning") == ["1 interface(s) skipped: (unnamed) (interface name is blank)."]

    def test_invalid_relationship_value_does_not_crash_auto_selection(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("invalid-relationship-value")
        user = make_user_with_perms(
            "invalid-relationship-value",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["100"],
                "auto_select_lag_members": "1",
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [{"port_id": 100, "ifName": "Port-Channel1", "ifAdminStatus": "up"}],
                "port_stack_relationships": {
                    "lag_members": {10: []},
                    "sub_interfaces": {},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert Interface.objects.filter(device=device, name="Port-Channel1").exists()

    def test_duplicate_display_name_sync_uses_selected_port_id_and_target(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        _virtual_chassis, (member1, member2) = make_virtual_chassis_members("stable-selection")
        selected_interface = make_interface(member1, "Ethernet")
        selected_interface.description = "old selected"
        set_librenms_device_id(selected_interface, 10, "default")
        selected_interface.save()
        untouched_interface = make_interface(member2, "Ethernet")
        untouched_interface.description = "old untouched"
        set_librenms_device_id(untouched_interface, 11, "default")
        untouched_interface.save()

        user = make_user_with_perms(
            "stable-interface-selection",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["10"],
                "device_selection_10": str(member1.pk),
                "device_selection_11": str(member2.pk),
                "exclude_columns": ["vlans", "mac_address", "mtu", "speed", "type"],
            },
            get_data={"interface_name_field": "ifDescr"},
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(member1, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet1",
                        "ifDescr": "Ethernet",
                        "ifAlias": "new selected",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 11,
                        "ifName": "Ethernet2",
                        "ifDescr": "Ethernet",
                        "ifAlias": "must not be applied",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {},
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=member1.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        selected_interface.refresh_from_db()
        untouched_interface.refresh_from_db()
        assert selected_interface.description == "new selected"
        assert untouched_interface.description == "old untouched"

    def test_same_device_duplicate_display_name_does_not_rebind_existing_port(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("same-device-stable-selection")
        existing = make_interface(device, "Ethernet")
        existing.description = "original port"
        set_librenms_device_id(existing, 10, "default")
        existing.save()

        user = make_user_with_perms(
            "same-device-stable-selection",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["11"],
                "exclude_columns": ["vlans", "mac_address", "mtu", "speed", "type"],
            },
            get_data={"interface_name_field": "ifDescr"},
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet1",
                        "ifDescr": "Ethernet",
                        "ifAlias": "original port",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 11,
                        "ifName": "Ethernet2",
                        "ifDescr": "Ethernet",
                        "ifAlias": "must not be applied",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {},
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        existing.refresh_from_db()
        assert get_librenms_device_id(existing, "default") == 10
        assert existing.description == "original port"
        assert Interface.objects.filter(device=device, name="Ethernet").count() == 1

    def test_same_vm_duplicate_display_name_does_not_rebind_existing_port(self):
        from types import SimpleNamespace

        from django.core.cache import cache
        from virtualization.models import VirtualMachine, VMInterface

        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        vm = make_vm("same-vm-stable-selection")
        existing = VMInterface.objects.create(virtual_machine=vm, name="Ethernet", description="original port")
        set_librenms_device_id(existing, 10, "default")
        existing.save()

        user = make_user_with_perms(
            "same-vm-stable-selection",
            [("view", VirtualMachine), ("add", VMInterface), ("change", VMInterface)],
        )
        request = _make_request(
            post_data={
                "select": ["11"],
                "exclude_columns": ["vlans", "mac_address", "mtu", "speed", "type"],
            },
            get_data={"interface_name_field": "ifDescr"},
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(vm, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 10,
                        "ifName": "Ethernet1",
                        "ifDescr": "Ethernet",
                        "ifAlias": "original port",
                        "ifAdminStatus": "up",
                    },
                    {
                        "port_id": 11,
                        "ifName": "Ethernet2",
                        "ifDescr": "Ethernet",
                        "ifAlias": "must not be applied",
                        "ifAdminStatus": "up",
                    },
                ],
                "port_stack_relationships": {},
            },
        )

        try:
            response = _post(view, request, object_type="virtualmachine", object_id=vm.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        existing.refresh_from_db()
        assert get_librenms_device_id(existing, "default") == 10
        assert existing.description == "original port"
        assert VMInterface.objects.filter(virtual_machine=vm, name="Ethernet").count() == 1

    @pytest.mark.parametrize("object_type", ["device", "virtualmachine"])
    @pytest.mark.parametrize(
        "stored_entry",
        [
            "corrupt",
            {},
            {"id": "corrupt"},
            None,
        ],
    )
    def test_corrupt_same_name_binding_is_not_claimed_by_selected_port(self, object_type, stored_entry):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache
        from virtualization.models import VirtualMachine, VMInterface

        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        if object_type == "device":
            obj = make_device("corrupt-name-fallback-device")
            interface_model = Interface
            owner_filter = {"device": obj}
            permissions = [("view", Device), ("add", Interface), ("change", Interface)]
        else:
            obj = make_vm("corrupt-name-fallback-vm")
            interface_model = VMInterface
            owner_filter = {"virtual_machine": obj}
            permissions = [("view", VirtualMachine), ("add", VMInterface), ("change", VMInterface)]

        existing = interface_model.objects.create(
            **owner_filter,
            name="Ethernet",
            description="original description",
            custom_field_data={"librenms_id": {"default": stored_entry}},
        )
        user = make_user_with_perms(f"corrupt-name-fallback-{object_type}", permissions)
        request = _make_request(
            post_data={
                "select": ["11"],
                "exclude_columns": ["vlans", "mac_address", "mtu", "speed", "type"],
            },
            get_data={"interface_name_field": "ifDescr"},
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(obj, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": 11,
                        "ifName": "Ethernet1",
                        "ifDescr": "Ethernet",
                        "ifAlias": "must not be applied",
                        "ifAdminStatus": "up",
                    }
                ],
                "port_stack_relationships": {},
            },
        )

        try:
            response = _post(view, request, object_type=object_type, object_id=obj.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        existing.refresh_from_db()
        assert existing.custom_field_data["librenms_id"] == {"default": stored_entry}
        assert get_librenms_device_id(existing, "default", auto_save=False) is None
        assert existing.description == "original description"
        assert interface_model.objects.filter(**owner_filter, name="Ethernet").count() == 1

    def test_standalone_relationship_cache_read_fails_closed_on_non_dict(self):
        from types import SimpleNamespace

        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        obj = make_device("malformed-standalone-relationship-cache")
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(obj, "ports", "default")
        cache.set(cache_key, ["corrupt", "snapshot"])
        try:
            assert view._get_cached_relationships(obj, "default") == {}
        finally:
            cache.delete(cache_key)

    def test_sync_selected_interfaces_skips_oob_rows(self):
        """OOB-controller rows are merged into the host list only for context and are never routed to a real device."""
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("oob-row-not-syncable")
        user = make_user_with_perms(
            "oob-row-not-syncable",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["99"],
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "ifName": "eth0",
                        "port_id": 99,
                        "ifAdminStatus": "up",
                        "_source": "oob",
                    }
                ]
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert not Interface.objects.filter(device=device).exists()

    def test_duplicate_normalized_selected_port_id_is_rejected_before_writes(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("duplicate-normalized-selected-id")
        user = make_user_with_perms(
            "duplicate-normalized-selected-id",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["10"],
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {"port_id": 10, "ifName": "eth0", "ifAdminStatus": "up"},
                    {"port_id": "010", "ifName": "eth1", "ifAdminStatus": "up"},
                ],
                "port_stack_relationships": {},
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        assert not Interface.objects.filter(device=device).exists()
        assert message_texts(request, "warning") == [
            "Selected LibreNMS port IDs are duplicated in the cached interface data. "
            "Refresh LibreNMS data and resolve the duplicate IDs before syncing."
        ]

    def test_duplicate_normalized_related_port_id_is_not_linked(self):
        from types import SimpleNamespace

        from dcim.models import Device
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("duplicate-normalized-related-id")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(parent, 20, "default")
        child.save()
        parent.save()
        user = make_user_with_perms(
            "duplicate-normalized-related-id",
            [("view", Device), ("add", type(child)), ("change", type(child))],
        )
        request = _make_request(
            post_data={
                "select": ["10"],
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {"port_id": 10, "ifName": child.name, "ifAdminStatus": "up"},
                    {"port_id": 20, "ifName": parent.name, "ifAdminStatus": "up"},
                    {"port_id": "020", "ifName": "duplicate-parent", "ifAdminStatus": "up"},
                ],
                "port_stack_relationships": {
                    "lag_members": {},
                    "sub_interfaces": {10: 20},
                },
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        child.refresh_from_db()
        assert child.parent_id is None

    def test_padded_cached_port_id_updates_its_existing_stable_match(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import get_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("padded-stable-port-id")
        interface = make_interface(device, "oldname")
        interface.custom_field_data["librenms_id"] = {"default": "0010"}
        interface.save()
        user = make_user_with_perms(
            "padded-stable-port-id",
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        request = _make_request(
            post_data={
                "select": ["10"],
                "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
            },
            user=user,
        )
        view = SyncInterfacesView()
        view._librenms_api = SimpleNamespace(server_key="default")
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(
            cache_key,
            {
                "ports": [
                    {
                        "port_id": "0010",
                        "ifName": "newname",
                        "ifAdminStatus": "up",
                    }
                ],
                "port_stack_relationships": {},
            },
        )

        try:
            response = _post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert response.status_code == 302
        interface.refresh_from_db()
        assert interface.name == "newname"
        assert get_librenms_device_id(interface, "default", auto_save=False) == 10
        assert Interface.objects.filter(device=device).count() == 1


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
        view = self._make_view(_make_request(post_data={"device_selection_10": str(sibling.pk)}))

        view.sync_interface(host, {"ifName": "Gi0/1", "port_id": 10}, [], "ifName")

        assert Interface.objects.filter(device=sibling, name="Gi0/1").exists()
        assert not Interface.objects.filter(device=host, name="Gi0/1").exists()

    def test_device_selection_invalid_is_skipped(self):
        """A device that is neither the page device nor a VC sibling is refused."""
        from dcim.models import Interface

        dev = make_device("selinvalid-page")
        other = make_device("selinvalid-other")
        view = self._make_view(_make_request(post_data={"device_selection_10": str(other.pk)}))
        view._skipped_conflicts = []

        view.sync_interface(dev, {"ifName": "Gi0/1", "port_id": 10}, [], "ifName")

        assert not Interface.objects.filter(device=dev, name="Gi0/1").exists()
        assert not Interface.objects.filter(device=other, name="Gi0/1").exists()
        assert view._skipped_conflicts == ["Gi0/1 (selected target unavailable)"]

    def test_device_selection_does_not_exist_is_skipped(self):
        from dcim.models import Device, Interface

        dev = make_device("selgone-page")
        absent_pk = missing_pk(Device)
        view = self._make_view(_make_request(post_data={"device_selection_10": str(absent_pk)}))
        view._skipped_conflicts = []

        view.sync_interface(dev, {"ifName": "Gi0/1", "port_id": 10}, [], "ifName")

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
            post_data={"device_selection_10": str(sibling.pk)},
            user=user,
        )
        view = self._make_view(request)
        view._skipped_conflicts = []

        view.sync_interface(host, {"ifName": "Gi0/1", "port_id": 10}, [], "ifName")

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
        return _sync_view()

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
        from netbox_librenms_plugin.utils import get_librenms_device_id

        view = self._make_view()
        interface = make_interface(make_device("port-id-write"), "Gi0/0")
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": None,
            "ifAlias": "",
            "ifMtu": None,
            "port_id": 42,
            "ifAdminStatus": "up",
        }

        view.update_interface_attributes(interface, librenms_port, "other", [], "ifName")

        interface = Interface.objects.get(pk=interface.pk)
        assert get_librenms_device_id(interface, "default", auto_save=False) == 42

    def test_port_id_conflict_does_not_overwrite(self):
        from dcim.models import Interface
        from netbox_librenms_plugin.utils import get_librenms_device_id, set_librenms_device_id

        view = self._make_view()
        conflicting_owner = make_interface(make_device("port-id-owner"), "Gi0/0")
        set_librenms_device_id(conflicting_owner, 42, "default")
        conflicting_owner.save(update_fields=["custom_field_data"])
        interface = make_interface(make_device("port-id-target"), "Gi0/0")
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": None,
            "ifAlias": "",
            "ifMtu": None,
            "port_id": 42,
            "ifAdminStatus": "up",
        }

        view.update_interface_attributes(interface, librenms_port, "other", [], "ifName")

        interface = Interface.objects.get(pk=interface.pk)
        assert get_librenms_device_id(interface, "default", auto_save=False) is None
        assert get_librenms_device_id(conflicting_owner, "default", auto_save=False) == 42

    def test_ifalias_not_set_when_same_as_name(self):
        """ifAlias should not overwrite when equal to interface name."""
        view = _sync_view()
        interface = make_interface(make_device("ifalias-same-as-name"), "Gi0/1")
        librenms_port = {
            "ifName": "Gi0/1",
            "ifType": "ethernetCsmacd",
            "ifSpeed": None,
            "ifAlias": "Gi0/1",
            "ifMtu": None,
            "port_id": None,
            "ifAdminStatus": "up",
        }

        view.update_interface_attributes(interface, librenms_port, "other", ["mac_address"], "ifName")

        interface.refresh_from_db()
        assert interface.description == ""


# ===========================================================================
# SyncInterfacesView._sync_interface_vlans
# ===========================================================================


class TestSyncInterfacesViewSyncInterfaceVlans:
    def test_missing_locked_owner_vlan_map_leaves_assignments_unchanged(self):
        from ipam.models import VLAN

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("joined-vlan-owner")
        interface = make_interface(device, "Ethernet1")
        existing_vlan = VLAN.objects.create(vid=100, name="Existing VLAN", status="active")
        interface.mode = "tagged"
        interface.save()
        interface.tagged_vlans.add(existing_vlan)
        view = object.__new__(SyncInterfacesView)
        view.request = _make_request()
        view._lookup_maps = view._index_vlans([existing_vlan])
        view._lookup_maps_by_owner = {}
        view._vlan_owners_by_id = {}

        view._sync_interface_vlans(interface, {"port_id": 10, "tagged_vlans": []})

        interface.refresh_from_db()
        assert interface.mode == "tagged"
        assert list(interface.tagged_vlans.all()) == [existing_vlan]

    def test_non_numeric_cached_vid_is_dropped_instead_of_aborting_the_sync(self):
        """A non-numeric VLAN id in the cached payload must not raise out of the sync.

        ``get_cached_ports_data`` only checks that each port is a dict, so the value reaches the
        int() coercions. A ValueError here aborts the enclosing sync transaction after other
        rows have already applied their changes.
        """
        from ipam.models import VLAN

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        device = make_device("bad-vid-owner")
        interface = make_interface(device, "Ethernet1")
        real_vlan = VLAN.objects.create(vid=100, name="Real VLAN", status="active")
        view = object.__new__(SyncInterfacesView)
        # The group override is keyed by port_id and VID, so post the malformed VID's key too.
        view.request = _make_request({"vlan_group_10_not-a-vid": "1", "vlan_group_10_100": "1"})
        view._lookup_maps = view._index_vlans([real_vlan])
        view._lookup_maps_by_owner = None
        view._vlan_owners_by_id = {}

        view._sync_interface_vlans(interface, {"port_id": 10, "untagged_vlan": "not-a-vid", "tagged_vlans": [100]})

        interface.refresh_from_db()
        # The good VID still applied; only the malformed one was dropped.
        assert list(interface.tagged_vlans.all()) == [real_vlan]


# ===========================================================================
# SyncInterfacesView._sync_lag_and_parent_relationships
# ===========================================================================


class TestSyncLagAndParentRelationships:
    """Outcome tests for bulk LAG/parent sync against real NetBox interfaces and validation."""

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

    def test_duplicate_display_name_links_only_selected_port(self, db):
        """Selecting one stable port ID must not link another port with the same ifDescr."""
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

        view = self._make_view(name_field="ifDescr", selected_port_ids={10})
        view._sync_lag_and_parent_relationships(device, ports_data, relationships, "default")

        m1.refresh_from_db()
        m2.refresh_from_db()
        assert m1.lag_id == agg.pk
        assert m2.lag_id is None

    def test_large_bulk_relationship_set_avoids_unbounded_candidate_predicates(self):
        """A large selection must bound SQL without locking unrelated interfaces."""
        from django.db import connection

        device = self._make_device()
        unrelated = make_interface(device, "unrelated")
        source_ids = range(1, 301)
        ports_data = [{"ifName": f"Ethernet{port_id}", "port_id": port_id} for port_id in source_ids] + [
            {"ifName": f"Port-Channel{port_id}", "port_id": 10_000 + port_id} for port_id in source_ids
        ]
        relationships = {
            "lag_members": {port_id: 10_000 + port_id for port_id in source_ids},
            "sub_interfaces": {},
        }
        view = self._make_view(selected_port_ids=source_ids)
        parameter_counts = []
        locked_interface_parameters = []

        def capture_parameters(execute, sql, params, many, context):
            parameter_counts.append(len(params or ()))
            if "FOR UPDATE" in sql.upper() and '"dcim_interface"' in sql:
                locked_interface_parameters.extend(params or ())
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture_parameters):
            view._sync_lag_and_parent_relationships(device, ports_data, relationships, "default")

        assert max(parameter_counts) < 2_000
        assert unrelated.pk not in locked_interface_parameters

    def test_member_selected_only_by_stable_id_is_linked(self, db):
        """A selected stable port ID is processed by the relationship sync."""
        device = self._make_device()
        member = self._iface(device, "Gi0/2", 11)
        agg = self._iface(device, "Po1", 100, itype="lag")

        ports_data = [
            {"ifName": "Gi0/2", "port_id": 11},
            {"ifName": "Po1", "port_id": 100},
        ]
        relationships = {"lag_members": {11: 100}, "sub_interfaces": {}}

        view = self._make_view(name_field="ifName", selected_port_ids={"11"})
        view._sync_lag_and_parent_relationships(device, ports_data, relationships, "default")

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
        view._sync_lag_and_parent_relationships(device, ports_data, relationships, "default")

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
        view._sync_lag_and_parent_relationships(device, ports_data, relationships, "default")

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
        view._sync_lag_and_parent_relationships(device, ports_data, ["garbage"], "default")

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
        view._sync_lag_and_parent_relationships(device, ports_data, relationships, "default")

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
        view._sync_lag_and_parent_relationships(device, ports_data, relationships, "default")

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

    # NetBox 4.4.x reads Interface.virtual_chassis (no such attribute) when the parent sits on
    # another device, so the validation it means to run raises AttributeError. The NetBox pinned
    # here validates that edge correctly, so the three tests below inject the failure. `name` is
    # what a real attribute access sets, and what the production check reads.
    _CORE_VC_BUG = AttributeError("'Interface' object has no attribute 'virtual_chassis'", name="virtual_chassis")

    def test_cross_member_parent_survives_a_netbox_that_cannot_validate_it(self, db):
        """The edge NetBox 4.4.0 cannot validate is the one it exists to allow: same chassis."""
        from dcim.models import Interface

        from netbox_librenms_plugin import utils
        from netbox_librenms_plugin.views.sync.interfaces import _apply_interface_relationship

        _vc, (member1, member2) = make_virtual_chassis_members("relationship-vc-clean-bug")
        child = make_interface(member2, "Ethernet4.100", iface_type="virtual")
        parent = make_interface(member1, "Ethernet4")

        with (
            patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 4, 0)),
            patch.object(Interface, "clean", side_effect=self._CORE_VC_BUG),
        ):
            _apply_interface_relationship(child, "parent", parent)

        child.refresh_from_db()
        assert child.parent_id == parent.pk

    def test_cross_member_parent_error_propagates_once_netbox_fixed_it(self, db):
        """The tolerance is scoped to 4.4.0: a later release must not hide the same failure."""
        from dcim.models import Interface

        from netbox_librenms_plugin import utils
        from netbox_librenms_plugin.views.sync.interfaces import _apply_interface_relationship

        _vc, (member1, member2) = make_virtual_chassis_members("relationship-vc-clean-fixed")
        child = make_interface(member2, "Ethernet7.100", iface_type="virtual")
        parent = make_interface(member1, "Ethernet7")

        with (
            patch.object(utils, "_get_netbox_version_tuple", return_value=(4, 4, 1)),
            patch.object(Interface, "clean", side_effect=self._CORE_VC_BUG),
            pytest.raises(AttributeError),
        ):
            _apply_interface_relationship(child, "parent", parent)

        child.refresh_from_db()
        assert child.parent_id is None

    def test_same_device_parent_does_not_swallow_the_attribute_error(self, db):
        """Only the cross-chassis edge is tolerated: NetBox never reaches that branch otherwise."""
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.interfaces import _apply_interface_relationship

        device = make_device("relationship-clean-bug-standalone")
        child = make_interface(device, "Ethernet5.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet5")

        with patch.object(Interface, "clean", side_effect=self._CORE_VC_BUG), pytest.raises(AttributeError):
            _apply_interface_relationship(child, "parent", parent)

        child.refresh_from_db()
        assert child.parent_id is None

    def test_an_unrelated_attribute_error_still_propagates(self, db):
        """The tolerance is keyed on the failing attribute, not on the shape of the edge alone."""
        from dcim.models import Interface

        from netbox_librenms_plugin.views.sync.interfaces import _apply_interface_relationship

        _vc, (member1, member2) = make_virtual_chassis_members("relationship-vc-other-error")
        child = make_interface(member2, "Ethernet6.100", iface_type="virtual")
        parent = make_interface(member1, "Ethernet6")

        with (
            patch.object(
                Interface,
                "clean",
                side_effect=AttributeError("'Interface' object has no attribute 'nope'", name="nope"),
            ),
            pytest.raises(AttributeError),
        ):
            _apply_interface_relationship(child, "parent", parent)

        child.refresh_from_db()
        assert child.parent_id is None

    def test_vm_parent_preserves_the_original_attribute_error(self, db):
        """A VMInterface validation error must not be replaced by a missing device attribute."""
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.views.sync.interfaces import _apply_interface_relationship

        vm = make_vm("relationship-vm-clean-error")
        child = VMInterface.objects.create(virtual_machine=vm, name="Ethernet1.100")
        parent = VMInterface.objects.create(virtual_machine=vm, name="Ethernet1")
        original = AttributeError("validation failed", name="unexpected")

        with patch.object(VMInterface, "clean", side_effect=original), pytest.raises(AttributeError) as raised:
            _apply_interface_relationship(child, "parent", parent)

        assert raised.value is original
        child.refresh_from_db()
        assert child.parent_id is None

    def test_name_hint_resolves_on_expected_owner_across_vc_duplicate_names(self, db):
        """On a VC, an id-less interface whose name is shared across members resolves to the SELECTED member, not chassis-wide ambiguity."""
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

        dev1 = make_device("vc-nh-m1")
        dev2 = make_device("vc-nh-m2")
        make_virtual_chassis("vc-nh", dev1, dev2)
        i1 = make_interface(dev1, "Gi0/1", iface_type="1000base-t")  # id-less, on member 1
        make_interface(dev2, "Gi0/1", iface_type="1000base-t")  # SAME name on member 2

        # port_id 909 matches no stored id → name-hint fallback. "Gi0/1" is duplicated across the
        # chassis; without owner-pinning the name lookup reports ambiguity and never resolves.
        iface, err = resolve_interface_by_port_id(
            dev1, "909", "default", name_hint="Gi0/1", expected_owner=interface_owner_for_object(dev1)
        )
        assert err is None, err
        assert iface is not None and iface.pk == i1.pk

    def test_cross_page_parent_resolves_to_port_keyed_member_override(self, db):
        """A cross-page parent's stable target override pins it to that member."""
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis

        page_dev = make_device("vc-page-master")
        member2 = make_device("vc-page-m2")
        make_virtual_chassis("vc-page", page_dev, member2)

        view = self._make_view()
        # The JS submits the off-page parent's member (from the child row's live .vc-member-select)
        # keyed by the parent's stable port_id.
        view.request.POST = {"device_selection_100": str(member2.id)}

        # Port-keyed override wins → the parent resolves onto member2.
        assert view._resolve_row_target_device(page_dev, port_id="100").id == member2.id
        # No override for this port and no name selection → the page device (unchanged default).
        assert view._resolve_row_target_device(page_dev, port_id="999").id == page_dev.id

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

        view = self._make_view(name_field="ifName", selected_port_ids={999})
        view._sync_lag_and_parent_relationships(device, ports_data, relationships, "default")

        oob_iface.refresh_from_db()
        assert oob_iface.lag_id is None  # OOB row skipped → no link persisted on the controller iface


# A POSTed valid non-default server_key must scope the sync to that server without 500ing on a
# misconfigured default client. Under the stack that behavior comes from rebind_api_for_server, so
# its coverage lives with the rebind seam in
# test_coverage_sync_views.TestSyncInterfacesViewServerRebind
# (test_posted_server_key_is_bound_for_the_sync / test_stale_server_key_fails_closed_without_sync).

# ===========================================================================
# Stable-ID interface resolution
# ===========================================================================


@pytest.mark.django_db
class TestResolveInterfaceByPortId:
    """The function must correctly read the nested {'server_key': port_id} dict format."""

    def test_finds_interface_by_server_keyed_dict(self):
        """When librenms_id = {'production': 42}, resolves for port_id=42 and server_key='production'."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("pci-byid")
        iface = make_interface(device, "Gi0/1", iface_type="1000base-t")
        set_librenms_device_id(iface, 42, "production")  # stored as {"production": 42}
        iface.save()

        found, err = resolve_interface_by_port_id(device, "42", "production")

        assert err is None
        assert found == iface

    def test_returns_error_when_not_found(self):
        """Returns (None, error) when no interface has matching port_id."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("pci-notfound")
        # An interface exists, but carries a different port id than the one we look up.
        iface = make_interface(device, "Gi0/1", iface_type="1000base-t")
        set_librenms_device_id(iface, 42, "production")
        iface.save()

        found, err = resolve_interface_by_port_id(device, "99", "production")

        assert found is None
        assert err is not None

    def test_name_hint_fallback_when_no_librenms_id(self):
        """Falls back to exact name lookup when no interface has a matching librenms_id."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device("pci-namehint")
        # The interface was created manually (no librenms_id) — only the name matches.
        iface = make_interface(device, "lag-1", iface_type="lag")

        found, err = resolve_interface_by_port_id(device, "42", "production", name_hint="lag-1")

        assert err is None
        assert found == iface

    def test_ambiguous_port_id_returns_error_not_first_match(self):
        """Two interfaces carrying the same stale librenms_id must fail as ambiguous, not silently bind lag/parent to whichever happens to be first."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("pci-ambig")
        for name in ("Gi0/1", "Gi0/2"):
            iface = make_interface(device, name, iface_type="1000base-t")
            set_librenms_device_id(iface, 42, "production")  # same stale id on both
            iface.save()

        found, err = resolve_interface_by_port_id(device, "42", "production")

        assert found is None
        assert err is not None
        assert "ambiguous" in err.lower()

    def test_name_hint_does_not_exist_falls_through_to_not_found(self):
        """A name-hint miss (DoesNotExist) is swallowed and reported as not-found."""
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("pci-namemiss")  # no interfaces at all

        found, err = resolve_interface_by_port_id(device, "42", "production", name_hint="lag-1")

        assert found is None
        assert err is not None
        assert "not found" in err.lower()

    def test_name_hint_multiple_matches_returns_ambiguous(self):
        """A name-hint matching multiple interfaces returns an ambiguity error, not a silent not-found."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_virtual_chassis

        member1 = make_device("pci-vc-m1")
        member2 = make_device("pci-vc-m2")
        make_virtual_chassis("pci-vc", member1, member2)
        # Same interface name on both members; neither has a librenms_id for port 42.
        make_interface(member1, "lag-1", iface_type="lag")
        make_interface(member2, "lag-1", iface_type="lag")

        found, err = resolve_interface_by_port_id(member1, "42", "production", name_hint="lag-1")

        assert found is None
        assert err is not None
        assert "ambiguous" in err.lower()

    def test_unexpected_error_during_resolution_propagates(self):
        """A real DB/runtime fault while scanning the device's interfaces must propagate, not be masked as a silent not-found."""
        from django.db import connection

        device = make_device("relationship-resolution-db-failure")

        def fail_interface_query(execute, sql, params, many, context):
            if 'FROM "dcim_interface"' in sql:
                raise RuntimeError("database is down")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail_interface_query):
            with pytest.raises(RuntimeError):
                resolve_interface_by_port_id(device, "42", "production", name_hint="lag-1")


class TestResolveInterfaceByPortIdExpectedOwner:
    """Real database coverage for owner-pinned stable-ID resolution."""

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
        member1, member2 = self._make_vc_members()
        iface2 = self._iface_with_librenms_id(member2, "Gi0/1", 42)

        # Without the guard: the VC-wide search finds member2's interface (the latent bug).
        found, err = resolve_interface_by_port_id(member1, "42", "default")
        assert err is None
        assert found == iface2

        # With expected_owner pinned to member1: the foreign-member match is rejected.
        found, err = resolve_interface_by_port_id(member1, "42", "default", expected_owner=(member1.pk, None))
        assert found is None
        assert err and "different owner" in err

        # With expected_owner matching the real owner (member2): accepted.
        found, err = resolve_interface_by_port_id(member1, "42", "default", expected_owner=(member2.pk, None))
        assert err is None
        assert found == iface2

    def test_accepts_match_on_the_expected_member(self, db):
        """An interface that genuinely lives on the expected member resolves cleanly."""
        member1, _ = self._make_vc_members()
        iface1 = self._iface_with_librenms_id(member1, "Gi0/1", 7)

        found, err = resolve_interface_by_port_id(member1, "7", "default", expected_owner=(member1.pk, None))
        assert err is None
        assert found == iface1

    def test_owner_mismatch_falls_back_to_name_hint(self, db):
        """A stale port_id on a foreign VC member must not block the name_hint fallback to the manually-created interface on the expected owner."""
        from netbox_librenms_plugin.tests.conftest import make_interface

        member1, member2 = self._make_vc_members()
        # Stale/reused librenms_id 42 lives on member2 (a foreign member).
        self._iface_with_librenms_id(member2, "Gi0/1", 42)
        # The real target: a manually-created aggregate on member1 with no stored librenms_id.
        agg = make_interface(member1, "Po1", iface_type="lag")

        # port_id 42 uniquely id-matches member2, but pinned to member1 with name_hint 'Po1'
        # the foreign id-match must be skipped and the name fallback must find member1's Po1.
        found, err = resolve_interface_by_port_id(
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
        # Mirrors the hardened relationship-map reader. Without the guard this raises
        # AttributeError ('list' object has no attribute 'items').
        view._sync_lag_and_parent_relationships(device, [], {"lag_members": [1, 2], "sub_interfaces": ["x"]}, "default")

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

    def test_locked_interface_index_preloads_relationship_validation_owners(self, django_assert_num_queries):
        from dcim.models import Location, Rack
        from django.db import connection, transaction
        from django.test.utils import CaptureQueriesContext
        from ipam.models import VLAN

        from netbox_librenms_plugin.views.sync.interfaces import _apply_interface_relationship

        _virtual_chassis, (member1, member2) = make_virtual_chassis_members("relationship-index")
        location = Location.objects.create(
            name="Relationship index location",
            slug="relationship-index-location",
            site=member2.site,
            status="active",
        )
        rack = Rack.objects.create(
            name="Relationship index rack", site=member2.site, location=location, status="active"
        )
        member2.location = location
        member2.rack = rack
        member2.save(update_fields=["location", "rack"])
        aggregate = make_interface(member1, "Port-Channel1", iface_type="lag")
        lag_member = make_interface(member2, "Ethernet3")
        lag_member.lag = aggregate
        lag_member.save(update_fields=["lag"])
        parent = make_interface(member2, "Ethernet2")
        bridge = make_interface(member2, "Bridge1", iface_type="bridge")
        vlan = VLAN.objects.create(vid=100, name="Relationship validation VLAN", site=member2.site, status="active")
        source = make_interface(member2, "Ethernet2.100", iface_type="virtual")
        source.parent = parent
        source.bridge = bridge
        source.mode = "access"
        source.untagged_vlan = vlan
        source.save(update_fields=["parent", "bridge", "mode", "untagged_vlan"])

        with transaction.atomic():
            index = build_interface_index(member1, "default", lock=True)
            indexed_source = index["by_name"][source.name][0]
            indexed_lag_member = index["by_name"][lag_member.name][0]
            with django_assert_num_queries(0):
                assert indexed_source.device.virtual_chassis_id == member2.virtual_chassis_id
                assert indexed_lag_member.lag.device.virtual_chassis_id == member1.virtual_chassis_id
                assert indexed_source.parent.device.virtual_chassis_id == member2.virtual_chassis_id
                assert indexed_source.bridge.device.virtual_chassis_id == member2.virtual_chassis_id
                assert indexed_source.device.site_id == member2.site_id
                assert indexed_source.device.location_id == location.pk
                assert indexed_source.device.rack_id == rack.pk
                assert indexed_source.untagged_vlan.site_id == member2.site_id
            indexed_parent = index["by_name"][parent.name][0]
            with CaptureQueriesContext(connection) as queries:
                _apply_interface_relationship(indexed_source, "parent", indexed_parent)
            assert not any('SELECT 1 AS "a" FROM "dcim_interface"' in query["sql"] for query in queries)

    def test_locked_vm_interface_index_preloads_full_clean_relationships(self, django_assert_num_queries):
        from django.db import connection, transaction
        from django.test.utils import CaptureQueriesContext
        from ipam.models import VLAN
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.views.sync.interfaces import _apply_interface_relationship

        vm = make_vm("relationship-index-vm")
        site = make_device("relationship-index-vm-site").site
        vm.site = site
        vm.save(update_fields=["site"])
        parent = VMInterface.objects.create(virtual_machine=vm, name="Ethernet1")
        bridge = VMInterface.objects.create(virtual_machine=vm, name="Bridge1")
        vlan = VLAN.objects.create(vid=101, name="VM relationship validation VLAN", site=site, status="active")
        source = VMInterface.objects.create(
            virtual_machine=vm,
            name="Ethernet1.101",
            parent=parent,
            bridge=bridge,
            mode="access",
            untagged_vlan=vlan,
        )

        with transaction.atomic():
            index = build_interface_index(vm, "default", lock=True)
            indexed_source = index["by_name"][source.name][0]
            with django_assert_num_queries(0):
                assert indexed_source.virtual_machine.site_id == site.pk
                assert indexed_source.parent.virtual_machine_id == vm.pk
                assert indexed_source.bridge.virtual_machine_id == vm.pk
                assert indexed_source.untagged_vlan.site_id == site.pk
            indexed_parent = index["by_name"][parent.name][0]
            with CaptureQueriesContext(connection) as queries:
                _apply_interface_relationship(indexed_source, "parent", indexed_parent)
            assert not any('SELECT 1 AS "a" FROM "virtualization_vminterface"' in query["sql"] for query in queries)


@pytest.mark.django_db
class TestSyncInterfaceLagViewRealDB:
    """End-to-end (real DB) coverage for SyncInterfaceLagView.post."""

    def _make_view(self):
        from types import SimpleNamespace

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        view = SyncInterfaceLagView()
        view._librenms_api = SimpleNamespace(server_key="default")
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
        _cache_relationship(view, device, "lag_members", 10, 20, member.name, agg.name)
        req = _make_request({"port_id": "10", "lag_port_id": "20"})
        resp = _post(view, req, object_type="device", object_id=device.pk)

        assert resp.status_code == 200, resp.content
        member.refresh_from_db()
        agg.refresh_from_db()
        # The link actually persisted, and the aggregate was promoted to type=lag.
        assert member.lag_id == agg.pk
        assert agg.type == "lag"

    def test_self_lag_rejected_by_real_full_clean(self):
        import json

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("lag-host-self")
        iface = self._iface(device, "Po1", 30)

        view = self._make_view()
        _cache_relationship(view, device, "lag_members", 30, 30, iface.name, iface.name)
        # port_id == lag_port_id resolves member == aggregate (same real interface); NetBox's
        # real Interface.clean() must reject the self-LAG with a 409 and persist nothing.
        req = _make_request({"port_id": "30", "lag_port_id": "30"})
        resp = _post(view, req, object_type="device", object_id=device.pk)

        assert resp.status_code == 409
        body = json.loads(resp.content)
        assert "NetBox rejected the LAG relationship" in body["error"]
        iface.refresh_from_db()
        assert iface.lag_id is None

    def test_cross_member_aggregate_is_linked_within_virtual_chassis(self):
        from netbox_librenms_plugin.tests.conftest import make_device, make_virtual_chassis
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        member1 = make_device("vc-lag-m1")
        member2 = make_device("vc-lag-m2")
        make_virtual_chassis("VC-LAG", member1, member2)
        local = self._iface(member1, "Gi0/1", 40)
        aggregate = self._iface(member2, "Po1", 50)

        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view = SyncInterfaceLagView()
        view._librenms_api = api
        _cache_relationship(view, member1, "lag_members", 40, 50, local.name, aggregate.name)
        req = _make_request({"port_id": "40", "lag_port_id": "50"})
        resp = _post(view, req, object_type="device", object_id=member1.pk)

        assert resp.status_code == 200, resp.content
        local.refresh_from_db()
        aggregate.refresh_from_db()
        assert local.lag_id == aggregate.pk
        assert aggregate.type == "lag"


@pytest.mark.django_db
class TestSyncInterfaceParentViewRealPermissions:
    def test_migrated_donor_rejects_direct_parent_sync(self):
        from types import SimpleNamespace

        from netbox_librenms_plugin.utils import mark_librenms_migrated, set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        donor = make_device("parent-migrated-donor")
        winner = make_device("parent-migrated-winner")
        child = make_interface(donor, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(donor, "Ethernet1")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(parent, 11, "default")
        child.save()
        parent.save()
        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

        request = _make_request({"port_id": "10", "parent_port_id": "11"})
        view = SyncInterfaceParentView()
        view._librenms_api = SimpleNamespace(server_key="default")
        _cache_relationship(view, donor, "sub_interfaces", 10, 11, child.name, parent.name)

        response = _post(view, request, object_type="device", object_id=donor.pk)

        assert response.status_code == 409
        child.refresh_from_db()
        assert child.parent_id is None

    def test_viewable_parent_does_not_require_change_permission(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("parent-related-view-permission")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(parent, 11, "default")
        child.save()
        parent.save()

        user = make_user_with_perms(
            "parent-related-view-permission",
            [("view", Device), ("view", Interface)],
        )
        user = grant(user, "change", Interface, constraints={"pk": child.pk})
        request = _make_request(
            {
                "port_id": "10",
                "parent_port_id": "11",
                "parent_name": "Ethernet1",
            },
            user=user,
        )
        view = SyncInterfaceParentView()
        view._librenms_api = SimpleNamespace(server_key="default")
        _cache_relationship(view, device, "sub_interfaces", 10, 11, child.name, parent.name)

        response = _post(view, request, object_type="device", object_id=device.pk)

        assert response.status_code == 200, response.content
        child.refresh_from_db()
        assert child.parent_id == parent.pk

    def test_self_parent_is_rejected_without_leaking_validation_detail(self):
        import json
        from types import SimpleNamespace

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("parent-self-link")
        interface = make_interface(device, "Ethernet1")
        set_librenms_device_id(interface, 12, "default")
        interface.save()
        request = _make_request({"port_id": "12", "parent_port_id": "12", "parent_name": "Ethernet1"})
        view = SyncInterfaceParentView()
        view._librenms_api = SimpleNamespace(server_key="default")
        _cache_relationship(view, device, "sub_interfaces", 12, 12, interface.name, interface.name)

        response = _post(view, request, object_type="device", object_id=device.pk)

        assert response.status_code == 409
        body = json.loads(response.content)
        assert "NetBox rejected the parent relationship" in body["error"]
        assert "cannot be its own" not in body["error"]
        interface.refresh_from_db()
        assert interface.parent_id is None

    def test_name_fallback_rejects_interface_bound_to_a_different_port(self):
        from types import SimpleNamespace

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("parent-conflicting-id")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        wrong_parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 10, "default")
        set_librenms_device_id(wrong_parent, 30, "default")
        child.save()
        wrong_parent.save()
        request = _make_request({"port_id": "10", "parent_port_id": "20", "parent_name": "Ethernet1"})
        view = SyncInterfaceParentView()
        view._librenms_api = SimpleNamespace(server_key="default")
        _cache_relationship(view, device, "sub_interfaces", 10, 20, child.name, wrong_parent.name)

        response = _post(view, request, object_type="device", object_id=device.pk)

        assert response.status_code == 404, response.content
        child.refresh_from_db()
        assert child.parent_id is None

    def test_unknown_server_key_fails_closed_before_relationship_lookup(self):
        from types import SimpleNamespace

        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        device = make_device("parent-stale-server")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        parent = make_interface(device, "Ethernet1")
        set_librenms_device_id(child, 10, "retired")
        set_librenms_device_id(parent, 11, "retired")
        child.save()
        parent.save()
        request = _make_request(
            {
                "port_id": "10",
                "parent_port_id": "11",
                "parent_name": "Ethernet1",
                "server_key": "retired",
            }
        )
        view = SyncInterfaceParentView()
        view._librenms_api = SimpleNamespace(server_key="default")

        response = _post(view, request, object_type="device", object_id=device.pk)

        assert response.status_code == 400
        child.refresh_from_db()
        assert child.parent_id is None


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
        from types import SimpleNamespace

        from django.test import RequestFactory

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceLagView

        request = RequestFactory().post("/lag/", {"port_id": str(port_id), "lag_port_id": str(lag_port_id)})
        request.user = user
        view = SyncInterfaceLagView()
        view._librenms_api = SimpleNamespace(server_key="default")
        _cache_relationship(view, device, "lag_members", port_id, lag_port_id)
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

    def test_parent_sync_rechecks_device_scope_after_owner_change(self):
        """A device that leaves the user's view scope before locking must not be changed."""
        from types import SimpleNamespace

        from dcim.models import Device, Interface, Site

        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView

        site_a = Site.objects.create(name="relationship-owner-site-a", slug="relationship-owner-site-a")
        site_b = Site.objects.create(name="relationship-owner-site-b", slug="relationship-owner-site-b")
        device = make_device("relationship-owner-moved")
        device.site = site_a
        device.save(update_fields=["site"])
        child = self._iface(device, "Ethernet1.100", 720)
        child.type = "virtual"
        child.save(update_fields=["type"])
        parent = self._iface(device, "Ethernet1", 721)
        user = self._writer(
            "relationship-owner-moved",
            [
                (Device, "view", {"site_id": site_a.pk}),
                (Interface, "view", None),
                (Interface, "change", {"pk": child.pk}),
            ],
        )

        class MoveOwnerAfterReadView(SyncInterfaceParentView):
            def _get_current_edge(self, *args, **kwargs):
                edge = super()._get_current_edge(*args, **kwargs)
                assert edge is not None
                Device.objects.filter(pk=device.pk).update(site=site_b)
                return edge

        request = _make_request(
            {"port_id": "720", "parent_port_id": "721"},
            user=user,
        )
        view = MoveOwnerAfterReadView()
        view._librenms_api = SimpleNamespace(server_key="default")
        _cache_relationship(view, device, "sub_interfaces", 720, 721, child.name, parent.name)
        assert (
            SyncInterfaceParentView._get_current_edge(
                view,
                device,
                "default",
                request,
                "720",
                "721",
            )
            is not None
        )

        response = _post(view, request, object_type="device", object_id=device.pk)

        assert response.status_code == 409
        assert b"interface owner changed concurrently" in response.content
        child.refresh_from_db()
        assert child.parent_id is None

    def test_parent_sync_rechecks_interface_scope_after_row_lock(self, monkeypatch):
        """A source that leaves its change grant before locking must not be changed."""
        from types import SimpleNamespace

        from dcim.models import Device, Interface

        from netbox_librenms_plugin.views.sync import interfaces as sync_interfaces

        device = make_device("relationship-interface-moved")
        child = self._iface(device, "Ethernet1.100", 722)
        child.type = "virtual"
        child.description = "managed"
        child.save(update_fields=["type", "description"])
        parent = self._iface(device, "Ethernet1", 723)
        user = self._writer(
            "relationship-interface-moved",
            [
                (Device, "view", None),
                (Interface, "view", {"pk": parent.pk}),
                (Interface, "change", {"description": "managed"}),
            ],
        )
        request = _make_request(
            {"port_id": "722", "parent_port_id": "723"},
            user=user,
        )
        view = sync_interfaces.SyncInterfaceParentView()
        view._librenms_api = SimpleNamespace(server_key="default")
        _cache_relationship(view, device, "sub_interfaces", 722, 723, child.name, parent.name)
        real_build_index = sync_interfaces.build_interface_index
        permission_revoked = False

        def revoke_before_lock(*args, **kwargs):
            nonlocal permission_revoked
            if kwargs.get("lock") and not permission_revoked:
                Interface.objects.filter(pk=child.pk).update(description="restricted")
                permission_revoked = True
            return real_build_index(*args, **kwargs)

        monkeypatch.setattr(sync_interfaces, "build_interface_index", revoke_before_lock)

        response = _post(view, request, object_type="device", object_id=device.pk)

        assert permission_revoked
        assert response.status_code == 404
        child.refresh_from_db()
        assert child.parent_id is None

    @pytest.mark.parametrize("hidden_stored_id", [20, "020"])
    def test_hidden_duplicate_stable_id_blocks_inline_and_bulk_parent_links(self, hidden_stored_id):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView, SyncInterfacesView

        device = make_device("hidden-duplicate-parent-id")
        child = make_interface(device, "Ethernet1.100", iface_type="virtual")
        visible_parent = make_interface(device, "Ethernet1")
        hidden_duplicate = make_interface(device, "Ethernet2")
        for interface, port_id in ((child, 10), (visible_parent, 20)):
            set_librenms_device_id(interface, port_id, "default")
            interface.save()
        hidden_duplicate.custom_field_data["librenms_id"] = {"default": hidden_stored_id}
        hidden_duplicate.save()
        user = make_user_with_perms(
            "hidden-duplicate-parent-id",
            [("view", Device), ("add", Interface)],
        )
        user = grant(user, "change", Interface, constraints={"pk": child.pk})
        user = grant(user, "view", Interface, constraints={"pk": visible_parent.pk})
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": child.name, "ifType": "l2vlan", "ifAdminStatus": "up"},
                {
                    "port_id": 20,
                    "ifName": visible_parent.name,
                    "ifType": "ethernetCsmacd",
                    "ifAdminStatus": "up",
                },
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        render_request = _make_request(user=user)
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)

        try:
            context = table_view.get_context_data(
                render_request,
                device,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=device,
            )
            assert "parent-sync-btn" not in str(context["table"].render_parent(None, snapshot["ports"][0]))

            inline_request = _make_request(
                {"port_id": "10", "parent_port_id": "20", "interface_name_field": "ifName"},
                user=user,
            )
            inline_view = SyncInterfaceParentView()
            inline_view._librenms_api = SimpleNamespace(server_key="default")
            inline_response = _post(inline_view, inline_request, object_type="device", object_id=device.pk)

            bulk_request = _make_request(
                post_data={
                    "select": ["10"],
                    "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
                },
                user=user,
            )
            bulk_view = SyncInterfacesView()
            bulk_view._librenms_api = SimpleNamespace(server_key="default")
            bulk_response = _post(bulk_view, bulk_request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)

        assert inline_response.status_code == 404
        assert bulk_response.status_code == 302
        child.refresh_from_db()
        assert child.parent_id is None

    def test_hidden_duplicate_unbound_name_blocks_inline_and_bulk_parent_links(self):
        from types import SimpleNamespace

        from dcim.models import Device, Interface
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.utils import set_librenms_device_id
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfaceParentView, SyncInterfacesView

        _vc, (member1, member2) = make_virtual_chassis_members("hidden-duplicate-parent-name")
        child = make_interface(member1, "Ethernet1.100", iface_type="virtual")
        visible_parent = make_interface(member1, "Ethernet1")
        make_interface(member2, "Ethernet1")
        set_librenms_device_id(child, 10, "default")
        child.save()
        user = make_user_with_perms(
            "hidden-duplicate-parent-name",
            [("view", Device), ("add", Interface)],
        )
        user = grant(user, "change", Interface, constraints={"pk": child.pk})
        user = grant(user, "view", Interface, constraints={"pk": visible_parent.pk})
        snapshot = {
            "ports": [
                {"port_id": 10, "ifName": child.name, "ifType": "l2vlan", "ifAdminStatus": "up"},
                {
                    "port_id": 20,
                    "ifName": visible_parent.name,
                    "ifType": "ethernetCsmacd",
                    "ifAdminStatus": "up",
                },
            ],
            "port_stack_relationships": {"lag_members": {}, "sub_interfaces": {10: 20}},
        }
        render_request = _make_request(user=user)
        table_view = DeviceInterfaceTableView()
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        table_view._librenms_api = api
        table_view.request = render_request
        cache_key = table_view.get_cache_key(member1, "ports", "default")
        cache.set(cache_key, snapshot)

        try:
            context = table_view.get_context_data(
                render_request,
                member1,
                "ifName",
                "default",
                fresh_data=snapshot,
                sync_device=member1,
            )
            assert "parent-sync-btn" not in str(context["table"].render_parent(None, snapshot["ports"][0]))

            inline_request = _make_request(
                {"port_id": "10", "parent_port_id": "20", "interface_name_field": "ifName"},
                user=user,
            )
            inline_view = SyncInterfaceParentView()
            inline_view._librenms_api = SimpleNamespace(server_key="default")
            inline_response = _post(inline_view, inline_request, object_type="device", object_id=member1.pk)

            bulk_request = _make_request(
                post_data={
                    "select": ["10"],
                    "exclude_columns": ["vlans", "mac_address", "description", "mtu", "speed", "type"],
                },
                user=user,
            )
            bulk_view = SyncInterfacesView()
            bulk_view._librenms_api = SimpleNamespace(server_key="default")
            bulk_response = _post(bulk_view, bulk_request, object_type="device", object_id=member1.pk)
        finally:
            cache.delete(cache_key)

        assert inline_response.status_code == 404
        assert bulk_response.status_code == 302
        child.refresh_from_db()
        assert child.parent_id is None
