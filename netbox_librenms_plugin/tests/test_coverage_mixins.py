"""
Coverage tests for views/mixins.py missing lines.

Targets:
  - LibreNMSAPIMixin.get_context_data (lines 277-282): AttributeError fallback path
  - VlanAssignmentMixin.get_vlan_groups_for_device (lines 368-387): region/sitegroup/location/rack branches
  - VlanAssignmentMixin._build_vlan_lookup_maps (lines 406-442)
  - VlanAssignmentMixin._select_most_specific_group (lines 472, 487-490, 500-503, 507-510, 523)
  - VlanAssignmentMixin._get_vlan_groups_for_scope (lines 564-576)
  - VlanAssignmentMixin._find_vlan_in_group (lines 599-600): fallback to any VLAN
  - VlanAssignmentMixin._update_interface_vlan_assignment (lines 634, 643, 653, 666)
"""

from unittest.mock import MagicMock, patch

import pytest


class TestCacheRemainingTtl:
    """cache_remaining_ttl centralises the django-redis-only cache.ttl() guard."""

    def test_returns_value_when_backend_exposes_ttl(self):
        from netbox_librenms_plugin.utils import cache_remaining_ttl

        backend = MagicMock()
        backend.ttl.return_value = 123
        assert cache_remaining_ttl(backend, "some-key") == 123
        backend.ttl.assert_called_once_with("some-key")

    def test_returns_none_when_backend_lacks_ttl(self):
        from netbox_librenms_plugin.utils import cache_remaining_ttl

        # A core Django backend (e.g. LocMemCache) exposes no ttl() — the guard must degrade to
        # None rather than raising AttributeError mid-render.
        class _NoTtlCache:
            def get(self, *args, **kwargs):
                return None

        assert cache_remaining_ttl(_NoTtlCache(), "some-key") is None


# =============================================================================
# LibreNMSAPIMixin.get_context_data
# =============================================================================


class TestLibreNMSAPIMixinActiveServerKey:
    """LibreNMSAPIMixin.active_server_key: the bound client's key or 'default', never building a client."""

    def _mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        return object.__new__(LibreNMSAPIMixin)

    def test_returns_bound_client_server_key(self):
        m = self._mixin()
        m._librenms_api = MagicMock(server_key="prod")
        assert m.active_server_key == "prod"

    def test_returns_default_without_building_client_when_unbound(self):
        """On the rebind-fail render path no client is bound; the property must return 'default' WITHOUT constructing a LibreNMSAPI (the lazy librenms_api property would, and can raise on a misconfigured default)."""
        m = self._mixin()
        m._librenms_api = None
        with patch("netbox_librenms_plugin.views.mixins.LibreNMSAPI") as mock_api_cls:
            assert m.active_server_key == "default"
        mock_api_cls.assert_not_called()


@pytest.mark.django_db
class TestRenderSyncPartial:
    """render_sync_partial: the chokepoint that injects migrated-context into every partial render."""

    def test_merges_real_migrated_context_and_write_permission_into_view_context(self):
        """A real migrated donor's marker + winner + the request user's has_write_permission are merged into the partial context (real build_migrated_context, not a stub re-asserting the dict merge)."""
        from django.test import RequestFactory

        from netbox_librenms_plugin.tests.conftest import make_device, make_superuser
        from netbox_librenms_plugin.utils import mark_librenms_migrated
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin, LibreNMSPermissionMixin

        winner = make_device("rsp-winner")
        donor = make_device("rsp-donor")
        mark_librenms_migrated(donor, winner.pk, "prod")  # real _migrated_to marker under "prod"
        donor.save()

        # Real sync views combine both mixins (BaseLibreNMSSyncView / BaseIPAddressTableView); build a
        # matching self so render_sync_partial can resolve has_write_permission from the request user.
        class _SyncView(LibreNMSPermissionMixin, LibreNMSAPIMixin):
            partial_template_name = "tmpl.html"

        m = object.__new__(_SyncView)
        request = RequestFactory().post("/")
        request.user = make_superuser()
        m.request = request
        # Only render is stubbed (it needs a full request + template machinery); build_migrated_context
        # resolves the donor's real marker against the real winner row.
        with patch("netbox_librenms_plugin.views.mixins.render") as mock_render:
            m.render_sync_partial(request, donor, "prod", {"vlan_sync": "X"})

        _req, template, context = mock_render.call_args.args
        assert template == "tmpl.html"
        # The view's payload, the real migration flags, AND has_write_permission are present — a
        # partial-render exit routed through here can never silently drop the migration controls or
        # the write-permission flag the "Move to winner" buttons gate on.
        assert context["vlan_sync"] == "X"
        assert context["migrated_to_marker"]["device_id"] == winner.pk
        assert context["migrated_to_winner"].pk == winner.pk
        assert context["has_write_permission"] is True


@pytest.mark.django_db
class TestBuildMigratedContextLazyWinner:
    """build_migrated_context defers the winner Device lookup to a lazy proxy (cable/module/VLAN partials never read it)."""

    @staticmethod
    def _winner_lookups(cap, pk):
        return [q["sql"] for q in cap.captured_queries if "dcim_device" in q["sql"] and f"= {pk}" in q["sql"]]

    def test_winner_lookup_deferred_until_the_proxy_is_read(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.utils import build_migrated_context, mark_librenms_migrated

        winner = make_device("bmc-winner")
        donor = make_device("bmc-donor")
        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()

        # Building the context must NOT fetch the winner row (the boolean-only partials never read it).
        with CaptureQueriesContext(connection) as cap_build:
            ctx = build_migrated_context(donor, "default")
        assert ctx["migrated_to_marker"]["device_id"] == winner.pk  # banner boolean present
        assert self._winner_lookups(cap_build, winner.pk) == []  # winner Device not fetched yet

        # Reading the proxy (the interface/IP partials do) resolves the real Device — one query.
        with CaptureQueriesContext(connection) as cap_access:
            assert ctx["migrated_to_winner"].pk == winner.pk
        assert self._winner_lookups(cap_access, winner.pk)  # the deferred lookup fired on access

    def test_self_pointing_marker_suppressed_without_a_winner_query(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.utils import build_migrated_context

        donor = make_device("bmc-self")
        # A self-pointing marker (device_id == donor.pk) is corrupt: it must not flip the donor into
        # migrated mode, and the suppression happens in memory — no winner Device fetch.
        donor.custom_field_data["librenms_id"] = {
            "default": {"_migrated_to": {"device_id": donor.pk, "server_key": "default", "at": "x"}}
        }
        donor.save()
        with CaptureQueriesContext(connection) as cap:
            ctx = build_migrated_context(donor, "default")
        assert ctx["migrated_to_marker"] is None
        assert ctx["migrated_to_winner"] is None
        assert self._winner_lookups(cap, donor.pk) == []  # suppression is in-memory


class TestLibreNMSAPIMixinRebindApiForServer:
    """LibreNMSAPIMixin.rebind_api_for_server: POST-scoped API client for base views."""

    def _mixin(self, session_key="default"):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        m = object.__new__(LibreNMSAPIMixin)
        m._librenms_api = MagicMock(server_key=session_key)
        return m

    def test_empty_key_keeps_session_api(self):
        """No POSTed key → returns the session server key and does not rebind."""
        m = self._mixin("default")
        original = m._librenms_api
        assert m.rebind_api_for_server("") == "default"
        assert m.rebind_api_for_server(None) == "default"
        assert m._librenms_api is original  # unchanged

    def test_valid_key_rebinds_and_returns_key(self):
        m = self._mixin("default")
        new_api = MagicMock(server_key="prod")
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=new_api) as mock_build:
            result = m.rebind_api_for_server("prod")
        mock_build.assert_called_once_with("prod")
        assert result == "prod"
        assert m._librenms_api is new_api  # rebound

    def test_returns_resolved_key_not_raw_post_value(self):
        """build_librenms_api may normalize the posted key (e.g. resolve an alias); the mixin must return the resolved key, not the raw POST value."""
        m = self._mixin("default")
        resolved_api = MagicMock(server_key="primary")
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=resolved_api):
            result = m.rebind_api_for_server("default")
        assert result == "primary"  # resolved key, not the raw posted "default"
        assert m._librenms_api is resolved_api

    def test_unknown_key_returns_none_without_rebinding(self):
        """A stale/tampered key (build returns None) → None, API left untouched."""
        m = self._mixin("default")
        original = m._librenms_api
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
            assert m.rebind_api_for_server("ghost") is None
        assert m._librenms_api is original

    def test_empty_key_no_cached_api_builds_default(self):
        """No POSTed key and no cached client → build the default via build_librenms_api(None), cache it, and return its key — never touching the LibreNMSAPI() property directly."""
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        m = object.__new__(LibreNMSAPIMixin)
        m._librenms_api = None
        default_api = MagicMock(server_key="default")
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=default_api) as mock_build:
            assert m.rebind_api_for_server("") == "default"
        mock_build.assert_called_once_with(None)
        assert m._librenms_api is default_api  # cached for reuse

    def test_empty_key_misconfigured_default_returns_none(self):
        """No POSTed key, no cached client, and the default server is misconfigured (build_librenms_api returns None) → fail closed with None instead of raising."""
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        m = object.__new__(LibreNMSAPIMixin)
        m._librenms_api = None
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
            assert m.rebind_api_for_server("") is None
        assert m._librenms_api is None


class TestLibreNMSAPIMixinResolveGetRenderServerKey:
    """LibreNMSAPIMixin.resolve_get_render_server_key: GET-render cache-scope resolution."""

    class _Req:
        """Minimal request stand-in exposing only ``GET`` (a dict supports ``.get``)."""

        def __init__(self, server_key=None):
            self.GET = {} if server_key is None else {"server_key": server_key}

    def _mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        m = object.__new__(LibreNMSAPIMixin)
        m._librenms_api = None
        return m

    def test_blank_key_misconfigured_default_does_not_rebuild_client(self):
        """Blank key + no cached client + misconfigured default degrades to None scope without rebuilding."""
        m = self._mixin()
        with (
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None),
            patch("netbox_librenms_plugin.views.mixins.LibreNMSAPI") as mock_api_cls,
        ):
            scoped, unresolved = m.resolve_get_render_server_key(self._Req())
        mock_api_cls.assert_not_called()  # the lazy property must never reconstruct the default
        assert m._librenms_api is None  # left unbound, not a freshly-built client
        assert unresolved is False
        assert scoped is None

    def test_blank_key_reads_cached_client_key_without_rebuild(self):
        """Blank key with a cached client returns that client's key, read directly without rebuilding."""
        m = self._mixin()
        m._librenms_api = MagicMock(server_key="prod")
        with patch("netbox_librenms_plugin.views.mixins.LibreNMSAPI") as mock_api_cls:
            scoped, unresolved = m.resolve_get_render_server_key(self._Req())
        mock_api_cls.assert_not_called()
        assert unresolved is False
        assert scoped == "prod"

    def test_unknown_requested_key_flags_unresolved(self):
        """A non-blank server_key that no longer resolves returns (requested, True)."""
        m = self._mixin()
        with patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None):
            scoped, unresolved = m.resolve_get_render_server_key(self._Req("ghost"))
        assert (scoped, unresolved) == ("ghost", True)


class TestLibreNMSAPIMixinGetContextData:
    """Tests for LibreNMSAPIMixin.get_context_data (lines 275-282)."""

    def test_get_context_data_super_succeeds(self):
        """When super().get_context_data() works, it merges with server info."""
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        class FakeBase:
            def get_context_data(self, **kwargs):
                return {"from_super": True, **kwargs}

        class ConcreteView(LibreNMSAPIMixin, FakeBase):
            pass

        view = ConcreteView()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        with patch.object(view, "get_server_info", return_value={"display_name": "Default"}):
            ctx = view.get_context_data(extra="value")

        assert ctx["from_super"] is True
        assert ctx["extra"] == "value"
        assert "librenms_server_info" in ctx
        assert ctx["librenms_server_info"] == {"display_name": "Default"}

    def test_get_context_data_attribute_error_falls_back_to_kwargs(self):
        """When super().get_context_data() raises AttributeError, kwargs used as context."""
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        # object.__new__ ensures no base class has get_context_data,
        # so super() will raise AttributeError inside the method.
        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = MagicMock()
        mixin._librenms_api.server_key = "default"

        with patch.object(mixin, "get_server_info", return_value={"url": "http://example.com"}):
            ctx = mixin.get_context_data(foo="bar", num=42)

        assert ctx["foo"] == "bar"
        assert ctx["num"] == 42
        assert "librenms_server_info" in ctx
        assert ctx["librenms_server_info"]["url"] == "http://example.com"

    def test_get_context_data_empty_kwargs_still_adds_server_info(self):
        """With no kwargs and AttributeError fallback, server info is still added."""
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = MagicMock()
        mixin._librenms_api.server_key = "default"

        server_info = {"display_name": "Default Server", "is_legacy": True}
        with patch.object(mixin, "get_server_info", return_value=server_info):
            ctx = mixin.get_context_data()

        assert ctx == {"librenms_server_info": server_info}


# =============================================================================
# VlanAssignmentMixin.get_vlan_groups_for_device – inner branches
# =============================================================================


class TestGetVlanGroupsForDeviceInnerBranches:
    """Cover lines 368-387: region, site-group, location, rack branches."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return object.__new__(VlanAssignmentMixin)

    def test_site_with_region_triggers_region_scope_query(self):
        """When device.site has a region, region-scoped VLAN groups are queried."""
        mixin = self._make_mixin()

        region = MagicMock()
        region.parent = None

        site = MagicMock()
        site.pk = 1
        site.region = region
        site.group = None

        device = MagicMock()
        device.site = site
        device.location = None
        device.rack = None

        scope_calls = []

        def fake_scope(model_cls, objects):
            scope_calls.append(model_cls)
            return []

        with (
            patch("dcim.models.Site") as MockSite,
            patch("dcim.models.Region") as MockRegion,
            patch("dcim.models.SiteGroup"),
            patch("dcim.models.Location"),
            patch("dcim.models.Rack"),
            patch("ipam.models.VLANGroup") as MockVLANGroup,
            patch.object(mixin, "_get_vlan_groups_for_scope", side_effect=fake_scope),
            patch.object(mixin, "_get_ancestors", return_value=[region]),
        ):
            MockVLANGroup.objects.filter.return_value = []
            mixin.get_vlan_groups_for_device(device)

        # Both Site and Region model classes should have been passed to _get_vlan_groups_for_scope
        assert MockSite in scope_calls, "Site should be queried for VLAN groups"
        assert MockRegion in scope_calls, "Region should be queried for VLAN groups"

    def test_site_with_group_triggers_site_group_scope_query(self):
        """When device.site has a group, site-group-scoped VLAN groups are queried."""
        mixin = self._make_mixin()

        site_group = MagicMock()
        site_group.parent = None

        site = MagicMock()
        site.pk = 5
        site.region = None
        site.group = site_group

        device = MagicMock()
        device.site = site
        device.location = None
        device.rack = None

        scope_calls = []

        def fake_scope(model_cls, objects):
            scope_calls.append((model_cls, list(objects)))
            return []

        with (
            patch("dcim.models.Site"),
            patch("dcim.models.Region"),
            patch("dcim.models.SiteGroup") as MockSiteGroup,
            patch("dcim.models.Location"),
            patch("dcim.models.Rack"),
            patch("ipam.models.VLANGroup") as MockVLANGroup,
            patch.object(mixin, "_get_vlan_groups_for_scope", side_effect=fake_scope),
            patch.object(mixin, "_get_ancestors", return_value=[site_group]),
        ):
            MockVLANGroup.objects.filter.return_value = []
            mixin.get_vlan_groups_for_device(device)

        # SiteGroup ancestors should have been processed
        assert len(scope_calls) >= 1
        # Verify the SiteGroup model class and ancestor objects were passed to _get_vlan_groups_for_scope
        assert any(c[0] is MockSiteGroup for c in scope_calls)
        assert any(c[0] is MockSiteGroup and c[1] == [site_group] for c in scope_calls)

    def test_device_with_location_triggers_location_scope_query(self):
        """When device.location is set, location-scoped VLAN groups are queried."""
        mixin = self._make_mixin()

        location = MagicMock()
        location.parent = None

        device = MagicMock()
        device.site = None
        device.location = location
        device.rack = None

        scope_calls = []

        def fake_scope(model_cls, objects):
            scope_calls.append((model_cls, list(objects)))
            return []

        with (
            patch("dcim.models.Site"),
            patch("dcim.models.Region"),
            patch("dcim.models.SiteGroup"),
            patch("dcim.models.Location") as MockLocation,
            patch("dcim.models.Rack"),
            patch("ipam.models.VLANGroup") as MockVLANGroup,
            patch.object(mixin, "_get_vlan_groups_for_scope", side_effect=fake_scope),
            patch.object(mixin, "_get_ancestors", return_value=[location]),
        ):
            MockVLANGroup.objects.filter.return_value = []
            mixin.get_vlan_groups_for_device(device)

        assert len(scope_calls) >= 1
        # Verify the Location model class was passed to _get_vlan_groups_for_scope
        assert any(c[0] is MockLocation for c in scope_calls)

    def test_device_with_rack_triggers_rack_scope_query(self):
        """When device.rack is set, rack-scoped VLAN groups are queried."""
        mixin = self._make_mixin()

        rack = MagicMock()
        rack.pk = 7

        device = MagicMock()
        device.site = None
        device.location = None
        device.rack = rack

        scope_calls = []

        def fake_scope(model_cls, objects):
            scope_calls.append((model_cls, list(objects)))
            return []

        with (
            patch("dcim.models.Site"),
            patch("dcim.models.Region"),
            patch("dcim.models.SiteGroup"),
            patch("dcim.models.Location"),
            patch("dcim.models.Rack") as MockRack,
            patch("ipam.models.VLANGroup") as MockVLANGroup,
            patch.object(mixin, "_get_vlan_groups_for_scope", side_effect=fake_scope),
        ):
            MockVLANGroup.objects.filter.return_value = []
            mixin.get_vlan_groups_for_device(device)

        # Rack must appear in the objects for one of the calls
        rack_calls = [objects for (_cls, objects) in scope_calls if rack in objects]
        assert len(rack_calls) >= 1
        # Verify the Rack model class was passed to _get_vlan_groups_for_scope
        assert any(c[0] is MockRack for c in scope_calls)

    def test_all_scope_branches_combined(self):
        """Device with site+region+sitegroup+location+rack hits all scope branches."""
        mixin = self._make_mixin()

        region = MagicMock()
        region.parent = None

        site_group = MagicMock()
        site_group.parent = None

        location = MagicMock()
        location.parent = None

        rack = MagicMock()
        rack.pk = 3

        site = MagicMock()
        site.pk = 1
        site.region = region
        site.group = site_group

        device = MagicMock()
        device.site = site
        device.location = location
        device.rack = rack

        scope_calls_by_class = []

        def fake_scope(model_cls, objects):
            scope_calls_by_class.append(model_cls)
            return []

        site_group_ancestor = MagicMock()
        site_group_ancestor.parent = None
        location_ancestor = MagicMock()
        location_ancestor.parent = None

        def fake_ancestors(obj):
            # Return distinct ancestors per branch so site-group and location paths are exercised
            if obj is site_group:
                return [site_group_ancestor]
            if obj is location:
                return [location_ancestor]
            return [region]

        with (
            patch("dcim.models.Site") as MockSite,
            patch("dcim.models.Region") as MockRegion,
            patch("dcim.models.SiteGroup") as MockSiteGroup,
            patch("dcim.models.Location") as MockLocation,
            patch("dcim.models.Rack") as MockRack,
            patch("ipam.models.VLANGroup") as MockVLANGroup,
            patch.object(mixin, "_get_vlan_groups_for_scope", side_effect=fake_scope),
            patch.object(mixin, "_get_ancestors", side_effect=fake_ancestors),
        ):
            MockVLANGroup.objects.filter.return_value = []
            mixin.get_vlan_groups_for_device(device)

        # All 5 scope types must have been queried
        assert MockSite in scope_calls_by_class, "Site branch not hit"
        assert MockRegion in scope_calls_by_class, "Region branch not hit"
        assert MockSiteGroup in scope_calls_by_class, "SiteGroup branch not hit"
        assert MockLocation in scope_calls_by_class, "Location branch not hit"
        assert MockRack in scope_calls_by_class, "Rack branch not hit"


# =============================================================================
# VlanAssignmentMixin._build_vlan_lookup_maps
# =============================================================================


class TestBuildVlanLookupMaps:
    """Tests for VlanAssignmentMixin._build_vlan_lookup_maps (lines 406-442)."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return object.__new__(VlanAssignmentMixin)

    def test_empty_groups_returns_empty_maps(self):
        """No groups and no global VLANs produces empty maps."""
        mixin = self._make_mixin()

        with patch("ipam.models.VLAN") as MockVLAN:
            MockVLAN.objects.filter.return_value.select_related.return_value = []
            maps = mixin._build_vlan_lookup_maps([])

        assert maps["vid_to_groups"] == {}
        assert maps["vid_group_to_vlan"] == {}
        assert maps["vid_to_vlans"] == {}
        assert maps["vid_name_to_vlan"] == {}

    def test_group_vlan_indexed_in_all_maps(self):
        """A VLAN within a group is added to all four lookup structures."""
        mixin = self._make_mixin()

        group = MagicMock()
        group.pk = 10

        vlan = MagicMock()
        vlan.vid = 100
        vlan.group = group
        vlan.name = "CORP-DATA"

        with patch("ipam.models.VLAN") as MockVLAN:
            # First call = group VLANs (needs .select_related()), second call = global VLANs
            first_qs = MagicMock()
            first_qs.select_related.return_value = [vlan]
            MockVLAN.objects.filter.side_effect = [first_qs, []]
            maps = mixin._build_vlan_lookup_maps([group])

        assert 100 in maps["vid_to_groups"]
        assert group in maps["vid_to_groups"][100]
        assert maps["vid_group_to_vlan"][(100, 10)] is vlan
        assert vlan in maps["vid_to_vlans"][100]
        assert maps["vid_name_to_vlan"][(100, "CORP-DATA")] is vlan

    def test_global_vlan_indexed_with_none_group(self):
        """A global VLAN (no group) uses None as group key."""
        mixin = self._make_mixin()

        vlan = MagicMock()
        vlan.vid = 200
        vlan.group = None
        vlan.name = "MGMT"

        with patch("ipam.models.VLAN") as MockVLAN:
            first_qs = MagicMock()
            first_qs.select_related.return_value = []
            MockVLAN.objects.filter.side_effect = [first_qs, [vlan]]
            maps = mixin._build_vlan_lookup_maps([])

        assert maps["vid_group_to_vlan"][(200, None)] is vlan
        assert vlan in maps["vid_to_vlans"][200]
        # Global VLANs should not appear in vid_to_groups
        assert 200 not in maps["vid_to_groups"]

    def test_multiple_groups_same_vid_both_tracked(self):
        """Same VID in two groups: both groups appear in vid_to_groups."""
        mixin = self._make_mixin()

        group_a = MagicMock()
        group_a.pk = 1
        group_b = MagicMock()
        group_b.pk = 2

        vlan_a = MagicMock()
        vlan_a.vid = 50
        vlan_a.group = group_a
        vlan_a.name = "VLAN50-A"

        vlan_b = MagicMock()
        vlan_b.vid = 50
        vlan_b.group = group_b
        vlan_b.name = "VLAN50-B"

        with patch("ipam.models.VLAN") as MockVLAN:
            first_qs = MagicMock()
            first_qs.select_related.return_value = [vlan_a, vlan_b]
            MockVLAN.objects.filter.side_effect = [first_qs, []]
            maps = mixin._build_vlan_lookup_maps([group_a, group_b])

        assert group_a in maps["vid_to_groups"][50]
        assert group_b in maps["vid_to_groups"][50]
        assert maps["vid_group_to_vlan"][(50, 1)] is vlan_a
        assert maps["vid_group_to_vlan"][(50, 2)] is vlan_b

    def test_filter_called_with_group_pks(self):
        """_build_vlan_lookup_maps queries VLAN with the correct group PKs."""
        mixin = self._make_mixin()

        group1 = MagicMock()
        group1.pk = 11
        group2 = MagicMock()
        group2.pk = 22

        with patch("ipam.models.VLAN") as MockVLAN:
            MockVLAN.objects.filter.return_value.select_related.return_value = []
            mixin._build_vlan_lookup_maps([group1, group2])

        # First filter call should include the group PKs
        first_call = MockVLAN.objects.filter.call_args_list[0]
        assert "group__pk__in" in first_call[1]
        assert set(first_call[1]["group__pk__in"]) == {11, 22}


# =============================================================================
# VlanAssignmentMixin._select_most_specific_group – uncovered priority paths
# =============================================================================


class TestSelectMostSpecificGroupPriorityPaths:
    """Tests for _select_most_specific_group priority calculation paths (lines 472-539)."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return object.__new__(VlanAssignmentMixin)

    @pytest.mark.django_db
    def test_returns_none_when_groups_empty(self):
        """Returns None immediately when groups list is empty (line 472)."""
        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        device = make_device("priority-empty-groups")
        result = mixin._select_most_specific_group([], device)
        assert result is None

    @pytest.mark.django_db
    def test_returns_none_when_device_is_none(self):
        """Returns None immediately when device is None (line 472)."""
        from ipam.models import VLANGroup

        mixin = self._make_mixin()
        group = VLANGroup.objects.create(name="Priority no device", slug="priority-no-device")
        result = mixin._select_most_specific_group([group], None)
        assert result is None

    @pytest.mark.django_db
    def test_rack_priority_path_executed(self):
        """Rack group beats site and global groups (highest priority)."""
        from dcim.models import Rack, Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        device = make_device("priority-rack-device")
        rack = Rack.objects.create(name="Priority rack", site=device.site, status="active")
        device.rack = rack
        device.save(update_fields=["rack"])

        rack_group = VLANGroup.objects.create(
            name="Priority rack group",
            slug="priority-rack-group",
            scope_type=ContentType.objects.get_for_model(Rack),
            scope_id=rack.pk,
        )
        site_group = VLANGroup.objects.create(
            name="Priority site competitor",
            slug="priority-site-competitor",
            scope_type=ContentType.objects.get_for_model(Site),
            scope_id=device.site.pk,
        )
        global_group = VLANGroup.objects.create(name="Priority global competitor", slug="priority-global-competitor")

        result = mixin._select_most_specific_group([site_group, global_group, rack_group], device)

        # rack_group must win over site and global groups
        assert result == rack_group

    @pytest.mark.django_db
    def test_location_priority_path_executed(self):
        """Device with location executes location priority path (lines 487-490)."""
        from dcim.models import Location
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        device = make_device("priority-location-device")
        parent_location = Location.objects.create(
            name="Priority parent location",
            slug="priority-parent-location",
            site=device.site,
            status="active",
        )
        child_location = Location.objects.create(
            name="Priority child location",
            slug="priority-child-location",
            site=device.site,
            status="active",
            parent=parent_location,
        )
        device.location = child_location
        device.save(update_fields=["location"])

        location_type = ContentType.objects.get_for_model(Location)
        child_group = VLANGroup.objects.create(
            name="Priority child location group",
            slug="priority-child-location-group",
            scope_type=location_type,
            scope_id=child_location.pk,
        )
        parent_group = VLANGroup.objects.create(
            name="Priority parent location group",
            slug="priority-parent-location-group",
            scope_type=location_type,
            scope_id=parent_location.pk,
        )

        result = mixin._select_most_specific_group([parent_group, child_group], device)

        # Child location (first in ancestry) has lower priority number = more specific
        assert result == child_group

    @pytest.mark.django_db
    def test_ancestor_location_group_beats_a_global_group(self):
        """Only the ancestor walk can find a group scoped to the device's PARENT location."""
        from dcim.models import Location
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        device = make_device("ancestor-location-device")
        parent_location = Location.objects.create(
            name="Ancestor parent location",
            slug="ancestor-parent-location",
            site=device.site,
            status="active",
        )
        child_location = Location.objects.create(
            name="Ancestor child location",
            slug="ancestor-child-location",
            site=device.site,
            status="active",
            parent=parent_location,
        )
        device.location = child_location
        device.save(update_fields=["location"])

        # The device sits in the child; the only location-scoped group is on the parent.
        parent_group = VLANGroup.objects.create(
            name="Ancestor parent location group",
            slug="ancestor-parent-location-group",
            scope_type=ContentType.objects.get_for_model(Location),
            scope_id=parent_location.pk,
        )
        global_group = VLANGroup.objects.create(name="Ancestor global group", slug="ancestor-global-group")

        result = mixin._select_most_specific_group([global_group, parent_group], device)

        assert result == parent_group

    @pytest.mark.django_db
    def test_site_priority_path_executed(self):
        """Device with site (no rack/location) executes site priority path (lines 500-503)."""
        from dcim.models import Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        device = make_device("priority-site-device")
        site_group = VLANGroup.objects.create(
            name="Priority site group",
            slug="priority-site-group",
            scope_type=ContentType.objects.get_for_model(Site),
            scope_id=device.site.pk,
        )
        global_group = VLANGroup.objects.create(name="Priority site global", slug="priority-site-global")

        result = mixin._select_most_specific_group([global_group, site_group], device)

        # site-scoped group wins over global group
        assert result == site_group

    @pytest.mark.django_db
    def test_region_priority_path_executed(self):
        """Device with site.region executes region hierarchy path (lines 507-510)."""
        from dcim.models import Region, Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        region = Region.objects.create(name="Priority region", slug="priority-region")
        site = Site.objects.create(
            name="Priority region site",
            slug="priority-region-site",
            status="active",
            region=region,
        )
        device = make_device("priority-region-device")
        device.site = site
        device.save(update_fields=["site"])

        region_group = VLANGroup.objects.create(
            name="Priority region group",
            slug="priority-region-group",
            scope_type=ContentType.objects.get_for_model(Region),
            scope_id=region.pk,
        )
        global_group = VLANGroup.objects.create(name="Priority region global", slug="priority-region-global")

        result = mixin._select_most_specific_group([global_group, region_group], device)

        # region-scoped group wins over global group
        assert result == region_group

    @pytest.mark.django_db
    def test_global_scope_group_lowest_priority(self):
        """Global scope group (scope_type=None) loses to any scoped group."""
        from dcim.models import Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        device = make_device("priority-global-device")
        global_group = VLANGroup.objects.create(name="Priority global group", slug="priority-global-group")
        site_group = VLANGroup.objects.create(
            name="Priority global site competitor",
            slug="priority-global-site-competitor",
            scope_type=ContentType.objects.get_for_model(Site),
            scope_id=device.site.pk,
        )

        result = mixin._select_most_specific_group([global_group, site_group], device)

        # site-scoped group wins over global (global has lowest priority)
        assert result == site_group

    @pytest.mark.django_db
    def test_site_group_priority_path_executed(self):
        """Device with site.group executes site-group hierarchy path."""
        from dcim.models import Site, SiteGroup
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        site_group_scope = SiteGroup.objects.create(name="Priority site group scope", slug="priority-site-group-scope")
        site = Site.objects.create(
            name="Priority grouped site",
            slug="priority-grouped-site",
            status="active",
            group=site_group_scope,
        )
        device = make_device("priority-site-group-device")
        device.site = site
        device.save(update_fields=["site"])

        site_group = VLANGroup.objects.create(
            name="Priority scoped site group",
            slug="priority-scoped-site-group",
            scope_type=ContentType.objects.get_for_model(SiteGroup),
            scope_id=site_group_scope.pk,
        )
        global_group = VLANGroup.objects.create(
            name="Priority site group global",
            slug="priority-site-group-global",
        )

        result = mixin._select_most_specific_group([global_group, site_group], device)

        # site-group-scoped group wins over global group
        assert result == site_group


# =============================================================================
# VlanAssignmentMixin._get_vlan_groups_for_scope
# =============================================================================


class TestGetVlanGroupsForScope:
    """Tests for VlanAssignmentMixin._get_vlan_groups_for_scope (lines 564-576)."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return object.__new__(VlanAssignmentMixin)

    @pytest.mark.django_db
    def test_empty_objects_returns_none_queryset(self, django_assert_num_queries):
        """Empty objects list → VLANGroup.objects.none() (line 568)."""
        from dcim.models import Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        mixin = self._make_mixin()
        site = Site.objects.create(name="Empty scope site", slug="empty-scope-site")
        VLANGroup.objects.create(
            name="Empty scope existing group",
            slug="empty-scope-existing-group",
            scope_type=ContentType.objects.get_for_model(Site),
            scope_id=site.pk,
        )

        result = mixin._get_vlan_groups_for_scope(Site, [])

        with django_assert_num_queries(0):
            assert list(result) == []

    @pytest.mark.django_db
    def test_all_none_pks_returns_none_queryset(self, django_assert_num_queries):
        """Objects with only None PKs → VLANGroup.objects.none() (line 574)."""
        from dcim.models import Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        mixin = self._make_mixin()
        saved_site = Site.objects.create(name="Saved scope site", slug="saved-scope-site")
        VLANGroup.objects.create(
            name="Saved scope existing group",
            slug="saved-scope-existing-group",
            scope_type=ContentType.objects.get_for_model(Site),
            scope_id=saved_site.pk,
        )
        unsaved_site = Site(name="Unsaved scope site", slug="unsaved-scope-site")

        result = mixin._get_vlan_groups_for_scope(Site, [unsaved_site])

        with django_assert_num_queries(0):
            assert list(result) == []

    @pytest.mark.django_db
    def test_valid_objects_queries_vlan_groups(self):
        """Valid objects list queries VLANGroup with correct scope args (line 576)."""
        from dcim.models import Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        mixin = self._make_mixin()
        site = Site.objects.create(name="Valid scope site", slug="valid-scope-site")
        other_site = Site.objects.create(name="Other valid scope site", slug="other-valid-scope-site")
        site_type = ContentType.objects.get_for_model(Site)
        expected_group = VLANGroup.objects.create(
            name="Valid scope group",
            slug="valid-scope-group",
            scope_type=site_type,
            scope_id=site.pk,
        )
        VLANGroup.objects.create(
            name="Other valid scope group",
            slug="other-valid-scope-group",
            scope_type=site_type,
            scope_id=other_site.pk,
        )
        VLANGroup.objects.create(name="Valid scope global group", slug="valid-scope-global-group")

        result = mixin._get_vlan_groups_for_scope(Site, [site])

        assert list(result) == [expected_group]

    @pytest.mark.django_db
    def test_mixed_none_and_valid_pks_excludes_none(self):
        """Objects with mixed None/valid PKs: only valid PKs used in filter."""
        from dcim.models import Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        mixin = self._make_mixin()
        valid_site = Site.objects.create(name="Mixed valid scope site", slug="mixed-valid-scope-site")
        other_site = Site.objects.create(name="Mixed other scope site", slug="mixed-other-scope-site")
        unsaved_site = Site(name="Mixed unsaved scope site", slug="mixed-unsaved-scope-site")
        site_type = ContentType.objects.get_for_model(Site)
        expected_group = VLANGroup.objects.create(
            name="Mixed valid scope group",
            slug="mixed-valid-scope-group",
            scope_type=site_type,
            scope_id=valid_site.pk,
        )
        VLANGroup.objects.create(
            name="Mixed other scope group",
            slug="mixed-other-scope-group",
            scope_type=site_type,
            scope_id=other_site.pk,
        )

        result = mixin._get_vlan_groups_for_scope(Site, [unsaved_site, valid_site])

        assert list(result) == [expected_group]


# =============================================================================
# VlanAssignmentMixin._find_vlan_in_group – fallback to any VLAN
# =============================================================================


class TestFindVlanInGroupFallback:
    """Tests for _find_vlan_in_group fallback path (lines 607-609)."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return object.__new__(VlanAssignmentMixin)

    @pytest.mark.django_db
    def test_fallback_to_first_vlan_when_no_group_or_global_match(self):
        """When no group or global VLAN exists for a VID, first from vid_to_vlans is returned."""
        from ipam.models import VLAN, VLANGroup

        mixin = self._make_mixin()
        group = VLANGroup.objects.create(name="Fallback VLAN group", slug="fallback-vlan-group")
        any_vlan = VLAN.objects.create(vid=100, name="Fallback VLAN", group=group)
        lookup_maps = mixin._build_vlan_lookup_maps([group])

        result = mixin._find_vlan_in_group(100, None, lookup_maps)

        assert result == any_vlan

    @pytest.mark.django_db
    def test_returns_none_when_vid_not_in_vid_to_vlans(self):
        """Returns None when VID has no entries at all."""
        from ipam.models import VLAN, VLANGroup

        mixin = self._make_mixin()
        group = VLANGroup.objects.create(name="Missing VID group", slug="missing-vid-group")
        VLAN.objects.create(vid=100, name="Present VLAN", group=group)
        lookup_maps = mixin._build_vlan_lookup_maps([group])

        result = mixin._find_vlan_in_group(999, None, lookup_maps)
        assert result is None

    @pytest.mark.django_db
    def test_invalid_group_id_skips_group_lookup_and_falls_back(self):
        """Non-integer vlan_group_id raises ValueError → falls back to global/any."""
        from ipam.models import VLAN

        mixin = self._make_mixin()
        global_vlan = VLAN.objects.create(vid=100, name="Global fallback VLAN")
        lookup_maps = mixin._build_vlan_lookup_maps([])

        result = mixin._find_vlan_in_group(100, "not-a-number", lookup_maps)

        assert result == global_vlan


# =============================================================================
# VlanAssignmentMixin._update_interface_vlan_assignment – uncovered branches
# =============================================================================


class TestUpdateInterfaceVlanAssignmentBranches:
    """Cover lines 634 (access), 643 (empty), 653 (untagged set), 666 (clear untagged)."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return object.__new__(VlanAssignmentMixin)

    def _make_interface(self):
        iface = MagicMock()
        iface.tagged_vlans = MagicMock()
        return iface

    def test_access_mode_set_for_untagged_only_no_tagged(self):
        """Sets interface.mode = 'access' when only untagged VID present (line 634)."""
        mixin = self._make_mixin()
        iface = self._make_interface()
        vlan = MagicMock()

        lookup_maps = {
            "vid_group_to_vlan": {(100, None): vlan},
            "vid_to_vlans": {100: [vlan]},
        }

        result = mixin._update_interface_vlan_assignment(
            iface,
            {"untagged_vlan": 100, "tagged_vlans": []},
            {},
            lookup_maps,
        )

        assert iface.mode == "access"
        assert result["mode_set"] == "access"

    def test_empty_mode_set_when_no_vlans_at_all(self):
        """Sets interface.mode = '' when no untagged or tagged VLANs (line 643)."""
        mixin = self._make_mixin()
        iface = self._make_interface()

        lookup_maps = {"vid_group_to_vlan": {}, "vid_to_vlans": {}}

        result = mixin._update_interface_vlan_assignment(
            iface,
            {"untagged_vlan": None, "tagged_vlans": []},
            {},
            lookup_maps,
        )

        assert iface.mode == ""
        assert result["mode_set"] == ""

    def test_untagged_vlan_assigned_to_interface_when_found(self):
        """interface.untagged_vlan is set to the resolved VLAN object (line 653)."""
        mixin = self._make_mixin()
        iface = self._make_interface()
        vlan = MagicMock()

        lookup_maps = {
            "vid_group_to_vlan": {(200, None): vlan},
            "vid_to_vlans": {200: [vlan]},
        }

        result = mixin._update_interface_vlan_assignment(
            iface,
            {"untagged_vlan": 200, "tagged_vlans": []},
            {},
            lookup_maps,
        )

        assert iface.untagged_vlan is vlan
        assert result["untagged_set"] is vlan
        iface.save.assert_called()

    def test_untagged_vlan_set_none_when_no_untagged_vid(self):
        """interface.untagged_vlan = None when untagged_vid is None (line 666)."""
        mixin = self._make_mixin()
        iface = self._make_interface()

        lookup_maps = {"vid_group_to_vlan": {}, "vid_to_vlans": {}}

        result = mixin._update_interface_vlan_assignment(
            iface,
            {"untagged_vlan": None, "tagged_vlans": []},
            {},
            lookup_maps,
        )

        assert iface.untagged_vlan is None
        assert result["untagged_set"] is None
        iface.save.assert_called()

    def test_existing_tagged_vlans_are_cleared_when_the_payload_has_none(self):
        """Remove existing tagged VLANs when LibreNMS reports no tagged VIDs."""
        mixin = self._make_mixin()
        iface = self._make_interface()
        iface.tagged_vlans.values_list.return_value = [101]

        lookup_maps = {"vid_group_to_vlan": {}, "vid_to_vlans": {}}

        result = mixin._update_interface_vlan_assignment(
            iface,
            {"untagged_vlan": None, "tagged_vlans": []},
            {},
            lookup_maps,
        )

        iface.tagged_vlans.clear.assert_called_once_with()
        iface.tagged_vlans.set.assert_not_called()
        iface.tagged_vlans.remove.assert_not_called()
        assert result["tagged_set"] == []
        assert result["changed"] is True

    def test_backward_compat_single_group_id_string(self):
        """Non-dict vlan_group_map (legacy single group ID) is handled correctly."""
        mixin = self._make_mixin()
        iface = self._make_interface()
        vlan = MagicMock()

        lookup_maps = {
            "vid_group_to_vlan": {(100, 5): vlan},
            "vid_to_vlans": {100: [vlan]},
        }

        # Pass a string (backward compat for single group ID)
        result = mixin._update_interface_vlan_assignment(
            iface,
            {"untagged_vlan": 100, "tagged_vlans": []},
            "5",  # non-dict, single group id
            lookup_maps,
        )

        assert result["untagged_set"] is vlan

    def test_missing_untagged_vlan_added_to_missing_list(self):
        """If untagged VID not found, it's in missing_vlans and untagged_vlan stays None."""
        mixin = self._make_mixin()
        iface = self._make_interface()

        lookup_maps = {"vid_group_to_vlan": {}, "vid_to_vlans": {}}

        result = mixin._update_interface_vlan_assignment(
            iface,
            {"untagged_vlan": 999, "tagged_vlans": []},
            {},
            lookup_maps,
        )

        assert 999 in result["missing_vlans"]
        assert iface.untagged_vlan is None

    def test_return_dict_has_all_keys(self):
        """Return dict always contains mode_set, untagged_set, tagged_set, missing_vlans."""
        mixin = self._make_mixin()
        iface = self._make_interface()

        lookup_maps = {"vid_group_to_vlan": {}, "vid_to_vlans": {}}

        result = mixin._update_interface_vlan_assignment(
            iface,
            {"untagged_vlan": None, "tagged_vlans": []},
            {},
            lookup_maps,
        )

        for key in ("mode_set", "untagged_set", "tagged_set", "missing_vlans"):
            assert key in result


class TestRenderServerKeyDegradation:
    """LibreNMSAPIMixin._render_server_key degrades a misconfigured default to None and is shared across views."""

    @pytest.mark.django_db
    def test_render_server_key_returns_none_on_broken_default(self):
        """A misconfigured default must not raise out of a render path; it degrades to None."""
        from django.test import override_settings

        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = None  # live property -> LibreNMSAPI() would raise ValueError

        with override_settings(PLUGINS_CONFIG={"netbox_librenms_plugin": {"servers": {"default": {}}}}):
            assert view._render_server_key() is None

    def test_render_server_key_is_shared_from_mixin(self):
        """The IP and cable table views inherit ONE _render_server_key (no per-view duplicate)."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        assert BaseIPAddressTableView._render_server_key is LibreNMSAPIMixin._render_server_key
        assert BaseCableTableView._render_server_key is LibreNMSAPIMixin._render_server_key

    def test_sync_page_subclass_keeps_render_server_key_method_callable(self):
        """A real sync-page subclass must not shadow the mixin's render-key resolver."""
        from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

        view = DeviceLibreNMSSyncView()
        view._librenms_api = MagicMock(server_key="active")

        assert view._render_server_key() == "active"
