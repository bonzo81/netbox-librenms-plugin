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

from unittest.mock import patch

import pytest

from netbox_librenms_plugin.tests.conftest import configure_librenms_servers, configure_no_librenms_servers


def server_entry(key, *, display_name=None):
    """Return a usable server mapping, so build_librenms_api() binds a client for *key*."""
    return {
        "librenms_url": f"http://{key}.librenms.test",
        "api_token": f"token-{key}",
        "display_name": display_name or key.title(),
    }


class TestCacheRemainingTtl:
    """cache_remaining_ttl centralises the django-redis-only cache.ttl() guard."""

    @pytest.mark.django_db
    def test_returns_the_remaining_ttl_of_a_real_cache_entry(self):
        """NetBox runs on django-redis, so the live cache answers ttl() with the seconds left."""
        from django.core.cache import cache

        from netbox_librenms_plugin.utils import cache_remaining_ttl

        cache.set("ttl-probe", "value", 120)
        try:
            remaining = cache_remaining_ttl(cache, "ttl-probe")
        finally:
            cache.delete("ttl-probe")

        assert 0 < remaining <= 120

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


@pytest.mark.django_db
class TestLibreNMSAPIMixinActiveServerKey:
    """LibreNMSAPIMixin.active_server_key: the bound client's key or 'default', never building a client."""

    def _mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        return object.__new__(LibreNMSAPIMixin)

    def test_returns_bound_client_server_key(self, settings):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        configure_librenms_servers(settings, {"prod": server_entry("prod")})
        mixin = self._mixin()
        mixin._librenms_api = LibreNMSAPI(server_key="prod")

        assert mixin.active_server_key == "prod"

    def test_returns_default_without_building_client_when_unbound(self, settings):
        """The rebind-fail render path has no client bound; the property must answer without building one."""
        # No usable server at all: the lazy librenms_api property would raise here, so a
        # "default" answer proves the property was never consulted.
        configure_no_librenms_servers(settings)
        mixin = self._mixin()
        mixin._librenms_api = None

        assert mixin.active_server_key == "default"
        assert mixin._librenms_api is None


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


@pytest.mark.django_db
class TestLibreNMSAPIMixinRebindApiForServer:
    """LibreNMSAPIMixin.rebind_api_for_server: POST-scoped API client for base views."""

    def _mixin(self, bound_key=None):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = None if bound_key is None else LibreNMSAPI(server_key=bound_key)
        return mixin

    def test_empty_key_keeps_session_api(self, settings):
        """No POSTed key: return the bound client's key and leave the client in place."""
        configure_librenms_servers(settings, {"default": server_entry("default"), "prod": server_entry("prod")})
        mixin = self._mixin("default")
        original = mixin._librenms_api

        assert mixin.rebind_api_for_server("") == "default"
        assert mixin.rebind_api_for_server(None) == "default"
        assert mixin._librenms_api is original

    def test_valid_key_rebinds_and_returns_key(self, settings):
        configure_librenms_servers(settings, {"default": server_entry("default"), "prod": server_entry("prod")})
        mixin = self._mixin("default")
        original = mixin._librenms_api

        result = mixin.rebind_api_for_server("prod")

        assert result == "prod"
        assert mixin._librenms_api is not original
        assert mixin._librenms_api.server_key == "prod"

    def test_returns_resolved_key_not_raw_post_value(self, settings):
        """With no 'default' configured the auto-default falls back, so the resolved key differs."""
        configure_librenms_servers(settings, {"primary": server_entry("primary")})
        mixin = self._mixin("primary")

        result = mixin.rebind_api_for_server("default")

        assert result == "primary"
        assert mixin._librenms_api.server_key == "primary"

    def test_unknown_key_returns_none_without_rebinding(self, settings):
        """A stale or tampered key leaves the bound client untouched."""
        configure_librenms_servers(settings, {"default": server_entry("default")})
        mixin = self._mixin("default")
        original = mixin._librenms_api

        assert mixin.rebind_api_for_server("ghost") is None
        assert mixin._librenms_api is original

    def test_empty_key_no_cached_api_builds_default(self, settings):
        """No POSTed key and no cached client builds the default and caches it for reuse."""
        configure_librenms_servers(settings, {"default": server_entry("default")})
        mixin = self._mixin()

        assert mixin.rebind_api_for_server("") == "default"
        assert mixin._librenms_api is not None
        assert mixin._librenms_api.server_key == "default"

    def test_empty_key_misconfigured_default_returns_none(self, settings):
        """A default that cannot be bound fails closed with None instead of raising."""
        configure_librenms_servers(settings, {"default": {"librenms_url": "", "api_token": ""}})
        mixin = self._mixin()

        assert mixin.rebind_api_for_server("") is None
        assert mixin._librenms_api is None


@pytest.mark.django_db
class TestLibreNMSAPIMixinResolveGetRenderServerKey:
    """LibreNMSAPIMixin.resolve_get_render_server_key: GET-render cache-scope resolution."""

    @staticmethod
    def _request(server_key=None):
        from django.test import RequestFactory

        query = {} if server_key is None else {"server_key": server_key}
        return RequestFactory().get("/", query)

    def _mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = None
        return mixin

    def test_blank_key_misconfigured_default_does_not_rebuild_client(self, settings):
        """A blank key with an unbindable default degrades to no scope and leaves the client unbound."""
        configure_librenms_servers(settings, {"default": {"librenms_url": "", "api_token": ""}})
        mixin = self._mixin()

        scoped, unresolved = mixin.resolve_get_render_server_key(self._request())

        assert mixin._librenms_api is None
        assert unresolved is False
        assert scoped is None

    def test_blank_key_reads_cached_client_key_without_rebuild(self, settings):
        """A blank key returns the already-bound client's key rather than rebuilding one."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        configure_librenms_servers(settings, {"default": server_entry("default"), "prod": server_entry("prod")})
        mixin = self._mixin()
        mixin._librenms_api = LibreNMSAPI(server_key="prod")
        original = mixin._librenms_api

        scoped, unresolved = mixin.resolve_get_render_server_key(self._request())

        assert mixin._librenms_api is original
        assert unresolved is False
        assert scoped == "prod"

    def test_unknown_requested_key_flags_unresolved(self, settings):
        """A named server that no longer resolves is reported back as unresolved."""
        configure_librenms_servers(settings, {"default": server_entry("default")})
        mixin = self._mixin()

        scoped, unresolved = mixin.resolve_get_render_server_key(self._request("ghost"))

        assert (scoped, unresolved) == ("ghost", True)


@pytest.mark.django_db
class TestLibreNMSAPIMixinGetContextData:
    """LibreNMSAPIMixin.get_context_data merges the active server's info into the view context."""

    def test_get_context_data_super_succeeds(self, settings):
        """A cooperating base class keeps its context, with the server info merged in."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        configure_librenms_servers(settings, {"default": server_entry("default", display_name="Default")})

        class FakeBase:
            def get_context_data(self, **kwargs):
                return {"from_super": True, **kwargs}

        class ConcreteView(LibreNMSAPIMixin, FakeBase):
            pass

        view = ConcreteView()
        view._librenms_api = LibreNMSAPI(server_key="default")

        ctx = view.get_context_data(extra="value")

        assert ctx["from_super"] is True
        assert ctx["extra"] == "value"
        assert ctx["librenms_server_info"]["display_name"] == "Default"
        assert ctx["librenms_server_info"]["server_key"] == "default"

    def test_get_context_data_attribute_error_falls_back_to_kwargs(self, settings):
        """With no cooperating base class the kwargs become the context."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        configure_librenms_servers(settings, {"default": server_entry("default")})
        # object.__new__ leaves no base get_context_data, so super() raises AttributeError inside.
        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = LibreNMSAPI(server_key="default")

        ctx = mixin.get_context_data(foo="bar", num=42)

        assert ctx["foo"] == "bar"
        assert ctx["num"] == 42
        assert ctx["librenms_server_info"]["url"] == "http://default.librenms.test"

    def test_get_context_data_empty_kwargs_still_adds_server_info(self, settings):
        """Server info is added even when the fallback context starts empty."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        configure_librenms_servers(settings, {"prod": server_entry("prod", display_name="Production")})
        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = LibreNMSAPI(server_key="prod")

        ctx = mixin.get_context_data()

        assert set(ctx) == {"librenms_server_info"}
        assert ctx["librenms_server_info"]["display_name"] == "Production"
        assert ctx["librenms_server_info"]["is_legacy"] is False


# =============================================================================
# VlanAssignmentMixin.get_vlan_groups_for_device – inner branches
# =============================================================================


class TestGetVlanGroupsForDeviceInnerBranches:
    """Cover the region, site-group, location and rack branches of get_vlan_groups_for_devices."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return object.__new__(VlanAssignmentMixin)

    @staticmethod
    def _scoped_group(name, scope):
        """Create a VLAN group scoped to one object, or a global group when scope is None."""
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        slug = name.lower().replace(" ", "-")
        if scope is None:
            return VLANGroup.objects.create(name=name, slug=slug)
        return VLANGroup.objects.create(
            name=name,
            slug=slug,
            scope_type=ContentType.objects.get_for_model(type(scope)),
            scope_id=scope.pk,
        )

    @pytest.mark.django_db
    def test_region_ancestors_are_searched(self):
        """A group scoped to the site's parent region is returned, so the region walk must run."""
        from dcim.models import Region, Site

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        parent_region = Region.objects.create(name="Scope parent region", slug="scope-parent-region")
        child_region = Region.objects.create(name="Scope child region", slug="scope-child-region", parent=parent_region)
        other_region = Region.objects.create(name="Scope other region", slug="scope-other-region")
        device = make_device("scope-region-device")
        device.site = Site.objects.create(name="Scope region site", slug="scope-region-site", region=child_region)
        device.save(update_fields=["site"])

        parent_group = self._scoped_group("Scope parent region group", parent_region)
        other_group = self._scoped_group("Scope other region group", other_region)

        groups = mixin.get_vlan_groups_for_device(device)

        assert parent_group in groups
        assert other_group not in groups

    @pytest.mark.django_db
    def test_site_group_ancestors_are_searched(self):
        """A group scoped to the site group's parent is returned, so the site-group walk must run."""
        from dcim.models import Site, SiteGroup

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        parent_site_group = SiteGroup.objects.create(name="Scope parent sitegroup", slug="scope-parent-sitegroup")
        child_site_group = SiteGroup.objects.create(
            name="Scope child sitegroup", slug="scope-child-sitegroup", parent=parent_site_group
        )
        other_site_group = SiteGroup.objects.create(name="Scope other sitegroup", slug="scope-other-sitegroup")
        device = make_device("scope-sitegroup-device")
        device.site = Site.objects.create(
            name="Scope sitegroup site", slug="scope-sitegroup-site", group=child_site_group
        )
        device.save(update_fields=["site"])

        parent_group = self._scoped_group("Scope parent sitegroup group", parent_site_group)
        other_group = self._scoped_group("Scope other sitegroup group", other_site_group)

        groups = mixin.get_vlan_groups_for_device(device)

        assert parent_group in groups
        assert other_group not in groups

    @pytest.mark.django_db
    def test_location_ancestors_are_searched(self):
        """A group scoped to the device location's parent is returned, so the location walk must run."""
        from dcim.models import Location

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        device = make_device("scope-location-device")
        parent_location = Location.objects.create(
            name="Scope parent location", slug="scope-parent-location", site=device.site, status="active"
        )
        child_location = Location.objects.create(
            name="Scope child location",
            slug="scope-child-location",
            site=device.site,
            status="active",
            parent=parent_location,
        )
        sibling_location = Location.objects.create(
            name="Scope sibling location", slug="scope-sibling-location", site=device.site, status="active"
        )
        device.location = child_location
        device.save(update_fields=["location"])

        parent_group = self._scoped_group("Scope parent location group", parent_location)
        sibling_group = self._scoped_group("Scope sibling location group", sibling_location)

        groups = mixin.get_vlan_groups_for_device(device)

        assert parent_group in groups
        assert sibling_group not in groups

    @pytest.mark.django_db
    def test_rack_scoped_groups_are_returned(self):
        """A group scoped to the device's rack is returned; another rack's group is not."""
        from dcim.models import Rack

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        device = make_device("scope-rack-device")
        rack = Rack.objects.create(name="Scope rack", site=device.site, status="active")
        other_rack = Rack.objects.create(name="Scope other rack", site=device.site, status="active")
        device.rack = rack
        device.save(update_fields=["rack"])

        rack_group = self._scoped_group("Scope rack group", rack)
        other_group = self._scoped_group("Scope other rack group", other_rack)

        groups = mixin.get_vlan_groups_for_device(device)

        assert rack_group in groups
        assert other_group not in groups

    @pytest.mark.django_db
    def test_every_scope_of_one_device_is_collected_and_sorted(self):
        """One device carrying all five scopes collects a group from each, plus the global groups."""
        from dcim.models import Location, Rack, Region, Site, SiteGroup

        from netbox_librenms_plugin.tests.conftest import make_device

        mixin = self._make_mixin()
        region = Region.objects.create(name="Combined region", slug="combined-region")
        site_group = SiteGroup.objects.create(name="Combined sitegroup", slug="combined-sitegroup")
        site = Site.objects.create(name="Combined site", slug="combined-site", region=region, group=site_group)
        device = make_device("scope-combined-device")
        device.site = site
        device.save(update_fields=["site"])
        location = Location.objects.create(
            name="Combined location", slug="combined-location", site=site, status="active"
        )
        rack = Rack.objects.create(name="Combined rack", site=site, status="active")
        device.location = location
        device.rack = rack
        device.save(update_fields=["location", "rack"])

        expected = {
            self._scoped_group("Combined site group", site),
            self._scoped_group("Combined region group", region),
            self._scoped_group("Combined sitegroup group", site_group),
            self._scoped_group("Combined location group", location),
            self._scoped_group("Combined rack group", rack),
            self._scoped_group("Combined global group", None),
        }
        unrelated = self._scoped_group(
            "Combined unrelated site group",
            Site.objects.create(name="Combined unrelated site", slug="combined-unrelated-site"),
        )

        groups = mixin.get_vlan_groups_for_device(device)

        assert expected.issubset(set(groups))
        assert unrelated not in groups
        assert [group.name.lower() for group in groups] == sorted(group.name.lower() for group in groups)


# =============================================================================
# VlanAssignmentMixin._build_vlan_lookup_maps
# =============================================================================


class TestBuildVlanLookupMaps:
    """Tests for VlanAssignmentMixin._build_vlan_lookup_maps."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return object.__new__(VlanAssignmentMixin)

    @staticmethod
    def _make_group(name):
        from ipam.models import VLANGroup

        return VLANGroup.objects.create(name=name, slug=name.lower().replace(" ", "-"))

    @staticmethod
    def _make_vlan(vid, name, group=None):
        from ipam.models import VLAN

        return VLAN.objects.create(vid=vid, name=name, group=group)

    @pytest.mark.django_db
    def test_no_groups_and_no_global_vlans_produces_empty_maps(self):
        """A VLAN that belongs to an unrequested group leaves every map empty."""
        mixin = self._make_mixin()
        self._make_vlan(700, "MAPS-UNREQUESTED", self._make_group("Maps unrequested"))

        maps = mixin._build_vlan_lookup_maps([])

        assert maps["vid_to_groups"] == {}
        assert maps["vid_group_to_vlan"] == {}
        assert maps["vid_to_vlans"] == {}
        assert maps["vid_name_to_vlan"] == {}

    @pytest.mark.django_db
    def test_group_vlan_is_indexed_in_all_maps(self):
        """A VLAN inside a requested group lands in all four lookup structures."""
        mixin = self._make_mixin()
        group = self._make_group("Maps corp")
        vlan = self._make_vlan(100, "CORP-DATA", group)

        maps = mixin._build_vlan_lookup_maps([group])

        assert maps["vid_to_groups"][100] == [group]
        assert maps["vid_group_to_vlan"][(100, group.pk)] == vlan
        assert maps["vid_to_vlans"][100] == [vlan]
        assert maps["vid_name_to_vlan"][(100, "CORP-DATA")] == vlan

    @pytest.mark.django_db
    def test_global_vlan_is_indexed_under_a_none_group(self):
        """A group-less VLAN is keyed on None and stays out of the ambiguity map."""
        mixin = self._make_mixin()
        vlan = self._make_vlan(200, "MGMT")

        maps = mixin._build_vlan_lookup_maps([])

        assert maps["vid_group_to_vlan"][(200, None)] == vlan
        assert maps["vid_to_vlans"][200] == [vlan]
        assert 200 not in maps["vid_to_groups"]

    @pytest.mark.django_db
    def test_same_vid_in_two_groups_tracks_both(self):
        """One VID present in two requested groups records both groups for ambiguity detection."""
        mixin = self._make_mixin()
        group_a = self._make_group("Maps group a")
        group_b = self._make_group("Maps group b")
        vlan_a = self._make_vlan(50, "VLAN50-A", group_a)
        vlan_b = self._make_vlan(50, "VLAN50-B", group_b)

        maps = mixin._build_vlan_lookup_maps([group_a, group_b])

        assert set(maps["vid_to_groups"][50]) == {group_a, group_b}
        assert maps["vid_group_to_vlan"][(50, group_a.pk)] == vlan_a
        assert maps["vid_group_to_vlan"][(50, group_b.pk)] == vlan_b

    @pytest.mark.django_db
    def test_only_the_requested_groups_are_loaded(self):
        """A VLAN in a group the caller did not ask for is left out of every map."""
        mixin = self._make_mixin()
        requested = self._make_group("Maps requested")
        skipped = self._make_group("Maps skipped")
        wanted = self._make_vlan(11, "WANTED", requested)
        unwanted = self._make_vlan(22, "UNWANTED", skipped)

        maps = mixin._build_vlan_lookup_maps([requested])

        assert maps["vid_to_vlans"] == {11: [wanted]}
        assert unwanted not in maps["vid_to_vlans"].get(22, [])
        assert (22, skipped.pk) not in maps["vid_group_to_vlan"]


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


@pytest.mark.django_db
class TestUpdateInterfaceVlanAssignmentBranches:
    """Cover the mode, untagged and tagged branches of _update_interface_vlan_assignment."""

    def _make_mixin(self):
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return object.__new__(VlanAssignmentMixin)

    def _fixture(self, tag, *, vlans=()):
        """Return a mixin, a real interface, a VLAN group and the lookup maps over *vlans*."""
        from ipam.models import VLAN, VLANGroup

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        mixin = self._make_mixin()
        interface = make_interface(make_device(f"vlan-assign-{tag}"), "eth0")
        group = VLANGroup.objects.create(name=f"Assign {tag}", slug=f"assign-{tag}")
        created = [VLAN.objects.create(vid=vid, name=name, group=group) for vid, name in vlans]
        return mixin, interface, group, mixin._build_vlan_lookup_maps([group]), created

    def test_untagged_only_sets_access_mode(self):
        """One untagged VID and no tagged VIDs puts the interface in access mode."""
        mixin, interface, group, maps, (vlan,) = self._fixture("access", vlans=[(100, "ACCESS-100")])

        result = mixin._update_interface_vlan_assignment(
            interface, {"untagged_vlan": 100, "tagged_vlans": []}, {"100": str(group.pk)}, maps
        )

        interface.refresh_from_db()
        assert interface.mode == "access"
        assert interface.untagged_vlan == vlan
        assert result["mode_set"] == "access"
        assert result["untagged_set"] == vlan

    def test_tagged_vids_set_tagged_mode_and_the_membership(self):
        """Tagged VIDs win over the untagged VID when the mode is chosen."""
        mixin, interface, group, maps, (untagged, tagged) = self._fixture(
            "tagged", vlans=[(10, "UNTAGGED-10"), (20, "TAGGED-20")]
        )

        result = mixin._update_interface_vlan_assignment(
            interface,
            {"untagged_vlan": 10, "tagged_vlans": [20]},
            {"10": str(group.pk), "20": str(group.pk)},
            maps,
        )

        interface.refresh_from_db()
        assert interface.mode == "tagged"
        assert interface.untagged_vlan == untagged
        assert list(interface.tagged_vlans.all()) == [tagged]
        assert result["tagged_set"] == [tagged]
        assert result["changed"] is True

    def test_no_vlans_clears_a_mode_the_interface_already_had(self):
        """An interface that had a mode loses it when LibreNMS reports no VLANs."""
        mixin, interface, group, maps, (vlan,) = self._fixture("clear-mode", vlans=[(30, "OLD-30")])
        interface.mode = "access"
        interface.untagged_vlan = vlan
        interface.save()

        result = mixin._update_interface_vlan_assignment(
            interface, {"untagged_vlan": None, "tagged_vlans": []}, {}, maps
        )

        interface.refresh_from_db()
        assert interface.mode is None
        assert interface.untagged_vlan is None
        assert result["mode_set"] is None
        assert result["untagged_set"] is None
        assert result["changed"] is True

    def test_an_unchanged_interface_is_not_written(self):
        """A payload matching what NetBox already holds reports no change."""
        mixin, interface, group, maps, _ = self._fixture("unchanged")

        result = mixin._update_interface_vlan_assignment(
            interface, {"untagged_vlan": None, "tagged_vlans": []}, {}, maps
        )

        assert result["changed"] is False
        assert result["mode_set"] is None
        assert result["untagged_set"] is None

    def test_existing_tagged_vlans_are_cleared_when_the_payload_has_none(self):
        """Remove existing tagged VLANs when LibreNMS reports no tagged VIDs."""
        mixin, interface, group, maps, (vlan,) = self._fixture("clear-tagged", vlans=[(101, "TAGGED-101")])
        interface.mode = "tagged"
        interface.save()
        interface.tagged_vlans.set([vlan])
        assert list(interface.tagged_vlans.all()) == [vlan]

        result = mixin._update_interface_vlan_assignment(
            interface, {"untagged_vlan": None, "tagged_vlans": []}, {}, maps
        )

        interface.refresh_from_db()
        assert list(interface.tagged_vlans.all()) == []
        assert result["tagged_set"] == []
        assert result["changed"] is True

    def test_a_non_dict_group_map_is_read_as_one_group_id(self):
        """A bare group id stands in for the per-VID map and decides which duplicate VID wins."""
        from ipam.models import VLAN, VLANGroup

        mixin, interface, decoy_group, _maps, (decoy,) = self._fixture("single-group", vlans=[(100, "DECOY-100")])
        wanted_group = VLANGroup.objects.create(name="Assign single-group wanted", slug="assign-single-wanted")
        wanted = VLAN.objects.create(vid=100, name="WANTED-100", group=wanted_group)
        # Both groups hold VID 100, so only the group id passed in can pick the right one.
        maps = mixin._build_vlan_lookup_maps([decoy_group, wanted_group])

        result = mixin._update_interface_vlan_assignment(
            interface, {"untagged_vlan": 100, "tagged_vlans": []}, str(wanted_group.pk), maps
        )

        assert result["untagged_set"] == wanted
        assert result["untagged_set"] != decoy
        interface.refresh_from_db()
        assert interface.untagged_vlan == wanted

    def test_an_unknown_untagged_vid_is_reported_missing(self):
        """A VID absent from NetBox is reported and leaves the interface without an untagged VLAN."""
        mixin, interface, _group, maps, _ = self._fixture("missing")

        result = mixin._update_interface_vlan_assignment(
            interface, {"untagged_vlan": 999, "tagged_vlans": []}, {}, maps
        )

        interface.refresh_from_db()
        assert result["missing_vlans"] == [999]
        assert interface.untagged_vlan is None

    def test_the_result_reports_every_key(self):
        """The caller always receives the full result contract."""
        mixin, interface, _group, maps, _ = self._fixture("contract")

        result = mixin._update_interface_vlan_assignment(
            interface, {"untagged_vlan": None, "tagged_vlans": []}, {}, maps
        )

        assert set(result) == {"mode_set", "untagged_set", "tagged_set", "missing_vlans", "changed"}


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

    @pytest.mark.django_db
    def test_sync_page_subclass_keeps_render_server_key_method_callable(self, settings):
        """A real sync-page subclass must not shadow the mixin's render-key resolver."""
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.views.object_sync.devices import DeviceLibreNMSSyncView

        configure_librenms_servers(settings, {"active": server_entry("active")})
        view = DeviceLibreNMSSyncView()
        view._librenms_api = LibreNMSAPI(server_key="active")

        assert view._render_server_key() == "active"


@pytest.mark.django_db  # the blank rebind reads the selected server from LibreNMSSettings
class TestRebindApiForServerOrDefault:
    """Verify that rebinding keeps valid keys and avoids the literal default when a configured key can resolve."""

    _CONFIG = {
        "netbox_librenms_plugin": {
            "servers": {
                "prod-a": {"librenms_url": "http://a.example", "api_token": "tok-a"},
                "prod-b": {"librenms_url": "http://b.example", "api_token": "tok-b"},
            }
        }
    }

    def _mixin(self):
        from netbox_librenms_plugin.views.mixins import LibreNMSAPIMixin

        mixin = object.__new__(LibreNMSAPIMixin)
        mixin._librenms_api = None
        return mixin

    def test_unresolvable_key_degrades_to_a_configured_server(self):
        from django.test import override_settings

        mixin = self._mixin()
        with override_settings(PLUGINS_CONFIG=self._CONFIG):
            resolved = mixin.rebind_api_for_server_or_default("server-that-was-deleted")

        assert resolved in {"prod-a", "prod-b"}
        assert resolved != "default"  # the literal would be a namespace nothing else reads
        assert mixin._librenms_api.server_key == resolved  # and the client is actually bound to it

    def test_resolvable_key_is_kept(self):
        from django.test import override_settings

        mixin = self._mixin()
        with override_settings(PLUGINS_CONFIG=self._CONFIG):
            assert mixin.rebind_api_for_server_or_default("prod-b") == "prod-b"

    def test_falls_back_to_active_key_when_no_server_can_be_built(self):
        """With nothing configurable at all, the degrade must still answer (and never raise)."""
        from django.test import override_settings

        mixin = self._mixin()
        with override_settings(PLUGINS_CONFIG={"netbox_librenms_plugin": {"servers": {}}}):
            assert mixin.rebind_api_for_server_or_default("anything") == "default"
