"""
IPAM view scoping for the sync tabs and the interface sync path.

The VLAN helper contract lives in ``test_interface_vlan_sync.py`` and the verify-view case in
``test_verify_views.py``. This file covers the remaining readers: the interfaces tab, the VLAN
tab and ``SyncInterfacesView``.
"""

import pytest

from netbox_librenms_plugin.tests.conftest import (
    configure_default_librenms_server,
    make_device,
)
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_user_with_perms,
    make_view,
)

pytestmark = pytest.mark.django_db

VLAN_PERM = "ipam.view_vlan"
VLAN_GROUP_PERM = "ipam.view_vlangroup"


def _site_group_with_vlan(device, tag, vid=100):
    """Create a VLAN group scoped to the device's site, holding one VLAN."""
    from dcim.models import Site
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import VLAN, VLANGroup

    group = VLANGroup.objects.create(
        name=f"{tag} group",
        slug=f"{tag}-group",
        scope_type=ContentType.objects.get_for_model(Site),
        scope_id=device.site.pk,
    )
    vlan = VLAN.objects.create(vid=vid, name=f"{tag}-vlan", group=group, status="active")
    return group, vlan


def _reader(username, *, ipam=False):
    """A real non-superuser who may view devices, with the two IPAM view grants optional."""
    from dcim.models import Device
    from ipam.models import VLAN, VLANGroup

    user = make_user_with_perms(username, [("view", Device)], plugin_write=False)
    if ipam:
        user = grant(user, "view", VLANGroup)
        user = grant(user, "view", VLAN)
    return user


def _ports_payload(vid=100):
    """One host port carrying an untagged VLAN, in the cached snapshot shape."""
    return {
        "ports": [
            {
                "port_id": 99,
                "ifName": "Ethernet1",
                "ifDescr": "Ethernet1",
                "ifAlias": "",
                "ifType": "ethernetCsmacd",
                "ifSpeed": 1_000_000_000,
                "ifPhysAddress": "",
                "ifMtu": 1500,
                "ifAdminStatus": "up",
                "untagged_vlan": vid,
                "_source": "host",
            }
        ],
        "port_stack_relationships": {},
    }


# =============================================================================
# The interfaces tab render (views/base/interfaces_view.py)
# =============================================================================


class TestInterfacesTabIpamScoping:
    """BaseInterfaceTableView.get_context_data must read IPAM as the requesting user."""

    def _context(self, user, device, server_key):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        request = make_request("get", user=user)
        view = make_view(DeviceInterfaceTableView, request)
        view._librenms_api.get_stored_librenms_id.return_value = None
        return view.get_context_data(
            request,
            device,
            "ifName",
            server_key,
            fresh_data=_ports_payload(),
            sync_device=device,
        )

    def test_a_user_without_ipam_view_rights_gets_no_vlan_groups(self, settings):
        """The tab gate never asks for IPAM rights, so the VLAN reads must be restricted."""
        server_key = configure_default_librenms_server(settings)
        device = make_device("iface-tab-ipam-scope")
        group, _vlan = _site_group_with_vlan(device, "iface-tab")

        context = self._context(_reader("iface-tab-no-ipam"), device, server_key)

        assert list(context["vlan_groups"]) == []
        assert group.name not in _table_html(context["table"])

    def test_a_user_with_ipam_view_rights_still_sees_every_vlan_group(self, settings):
        """The scoping must not regress the permitted caller."""
        server_key = configure_default_librenms_server(settings)
        device = make_device("iface-tab-ipam-allowed")
        group, _vlan = _site_group_with_vlan(device, "iface-tab-allowed")

        context = self._context(_reader("iface-tab-ipam", ipam=True), device, server_key)

        assert list(context["vlan_groups"]) == [group]
        assert group.name in _table_html(context["table"])


def _table_html(table):
    """Render every cell of a bound table so a leaked group name is visible to an assertion."""
    if table is None:
        return ""
    return " ".join(str(cell) for row in table.rows for cell in row)


# =============================================================================
# The VLAN tab render (views/base/vlan_table_view.py)
# =============================================================================


class TestVlanTabIpamScoping:
    """BaseVLANTableView must read IPAM as the requesting user on all three paths."""

    def _view(self, user):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

        request = make_request("get", user=user)
        return make_view(DeviceVLANTableView, request), request

    def test_a_user_without_ipam_view_rights_gets_no_vlan_groups(self, settings):
        """get_vlan_context serialises the group id and name into the table, so scope it."""
        from django.core.cache import cache

        server_key = configure_default_librenms_server(settings)
        device = make_device("vlan-tab-ipam-scope")
        group, _vlan = _site_group_with_vlan(device, "vlan-tab")
        view, request = self._view(_reader("vlan-tab-no-ipam"))
        cache.set(view.get_cache_key(device, "vlans", server_key), [{"vlan_vlan": 100, "vlan_name": "vlan-tab-vlan"}])

        try:
            context = view.get_vlan_context(request, device, server_key)
        finally:
            cache.delete(view.get_cache_key(device, "vlans", server_key))

        assert list(context["vlan_groups"]) == []
        row = context["vlan_table"].data[0]
        assert row["exists_in_netbox"] is False
        assert row["netbox_vlan_group"] is None
        assert row["auto_selected_group_id"] is None

    def test_a_user_with_ipam_view_rights_still_matches_the_vlan(self, settings):
        """The scoping must not regress the permitted caller."""
        from django.core.cache import cache

        server_key = configure_default_librenms_server(settings)
        device = make_device("vlan-tab-ipam-allowed")
        group, vlan = _site_group_with_vlan(device, "vlan-tab-allowed")
        view, request = self._view(_reader("vlan-tab-ipam", ipam=True))
        cache.set(
            view.get_cache_key(device, "vlans", server_key),
            [{"vlan_vlan": 100, "vlan_name": "vlan-tab-allowed-vlan"}],
        )

        try:
            context = view.get_vlan_context(request, device, server_key)
        finally:
            cache.delete(view.get_cache_key(device, "vlans", server_key))

        assert list(context["vlan_groups"]) == [group]
        row = context["vlan_table"].data[0]
        assert row["netbox_vlan_id"] == vlan.pk
        assert row["netbox_vlan_group"] == group.name

    def test_the_unresolved_server_branch_is_scoped(self, settings):
        """The early return renders the same group list, so it needs the same scope."""
        server_key = configure_default_librenms_server(settings)
        device = make_device("vlan-tab-unresolved-scope")
        _site_group_with_vlan(device, "vlan-tab-unresolved")
        view, _request = self._view(_reader("vlan-tab-unresolved-user"))
        request = make_request("get", {"server_key": "gone"}, user=view.request.user)
        view.request = request

        context = view.get_vlan_context(request, device)

        assert server_key != "gone"
        assert list(context["vlan_groups"]) == []

    def test_the_error_fragment_is_scoped(self, settings):
        """_get_error_context takes no request, so it must resolve the bound one."""
        server_key = configure_default_librenms_server(settings)
        device = make_device("vlan-tab-error-scope")
        _site_group_with_vlan(device, "vlan-tab-error")
        view, _request = self._view(_reader("vlan-tab-error-user"))

        context = view._get_error_context(device, "boom", server_key=server_key)

        assert list(context["vlan_groups"]) == []

    def test_the_error_fragment_works_without_a_bound_request(self):
        """A request-less caller must degrade to the unscoped list, not raise on self.request."""
        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        device = make_device("vlan-tab-error-no-request")
        group, _vlan = _site_group_with_vlan(device, "vlan-tab-no-request")
        view = object.__new__(BaseVLANTableView)

        context = view._get_error_context(device, "boom", server_key="default")

        assert list(context["vlan_groups"]) == [group]


# =============================================================================
# The interface sync path (views/sync/interfaces.py)
# =============================================================================


class TestSyncInterfacesIpamScoping:
    """SyncInterfacesView writes VLANs, so its lookup maps must be the caller's."""

    def _sync(self, user, device, server_key):
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.view_test_helpers import post as call_post
        from netbox_librenms_plugin.views.sync.interfaces import SyncInterfacesView

        request = make_request("post", {"select": ["99"], "server_key": server_key}, user=user)
        view = make_view(SyncInterfacesView, request)
        cache_key = view.get_cache_key(device, "ports", server_key)
        cache.set(cache_key, _ports_payload())
        try:
            call_post(view, request, object_type="device", object_id=device.pk)
        finally:
            cache.delete(cache_key)
        return request

    @staticmethod
    def _writer(username, *, ipam=False):
        from dcim.models import Device, Interface
        from ipam.models import VLAN, VLANGroup

        user = make_user_with_perms(
            username,
            [("view", Device), ("add", Interface), ("change", Interface)],
        )
        if ipam:
            user = grant(user, "view", VLANGroup)
            user = grant(user, "view", VLAN)
        return user

    def test_a_writer_without_ipam_view_rights_syncs_no_vlan_and_is_told_why(self, settings):
        """Restricting the sync path changes behaviour, so the skipped match must be visible."""
        from dcim.models import Interface

        server_key = configure_default_librenms_server(settings)
        device = make_device("sync-ipam-scope")
        _site_group_with_vlan(device, "sync-scope")

        self._sync(self._writer("sync-no-ipam"), device, server_key)

        interface = Interface.objects.get(device=device, name="Ethernet1")
        assert interface.untagged_vlan is None

    def test_a_writer_with_ipam_view_rights_still_assigns_the_vlan(self, settings):
        """The scoping must not regress the permitted caller, and must stay quiet for them."""
        from dcim.models import Interface

        server_key = configure_default_librenms_server(settings)
        device = make_device("sync-ipam-allowed")
        _group, vlan = _site_group_with_vlan(device, "sync-allowed")

        self._sync(self._writer("sync-ipam", ipam=True), device, server_key)

        interface = Interface.objects.get(device=device, name="Ethernet1")
        assert interface.untagged_vlan == vlan
