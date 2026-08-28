"""
Tests for interface VLAN sync functionality (Phase 2).

Tests cover:
- VlanAssignmentMixin methods
- Port VLAN enrichment
- VLAN sync action
"""

import pytest

from netbox_librenms_plugin.tests import test_librenms_api_helpers

# Bind the helper's autouse fixture into this module so it patches the config here only.
# `pytest_plugins` would register it session-wide and shadow PLUGINS_CONFIG for later tests.
mock_librenms_config = test_librenms_api_helpers.mock_librenms_config


@pytest.mark.django_db
class TestVlanAssignmentMixin:
    """Tests for VlanAssignmentMixin methods."""

    @staticmethod
    def _mixin():
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return VlanAssignmentMixin()

    @staticmethod
    def _group(name, scope=None):
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

    def test_get_vlan_groups_for_device_includes_site_scoped(self):
        """A group scoped to the device's site is returned; another site's group is not."""
        from dcim.models import Site

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("ivs-site-scoped")
        other_site = Site.objects.create(name="IVS other site", slug="ivs-other-site")
        site_group = self._group("IVS site group", device.site)
        other_group = self._group("IVS other site group", other_site)

        groups = self._mixin().get_vlan_groups_for_device(device)

        assert site_group in groups
        assert other_group not in groups

    def test_get_vlan_groups_for_device_includes_global(self):
        """A device with no location context still receives the global groups."""
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("ivs-global")
        global_group = self._group("IVS global group")

        groups = self._mixin().get_vlan_groups_for_device(device)

        assert global_group in groups

    def test_select_most_specific_group_prefers_rack(self):
        """A rack-scoped group outranks a site-scoped one for a racked device."""
        from dcim.models import Rack

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("ivs-rack-priority")
        rack = Rack.objects.create(name="IVS priority rack", site=device.site, status="active")
        device.rack = rack
        device.save(update_fields=["rack"])
        rack_group = self._group("IVS rack group", rack)
        site_group = self._group("IVS site competitor", device.site)

        result = self._mixin()._select_most_specific_group([rack_group, site_group], device)

        assert result == rack_group

    def test_select_most_specific_group_returns_none_for_ambiguous(self):
        """Two groups scoped to the same site tie, so no group wins."""
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("ivs-ambiguous")
        first = self._group("IVS ambiguous a", device.site)
        second = self._group("IVS ambiguous b", device.site)

        result = self._mixin()._select_most_specific_group([first, second], device)

        assert result is None

    def test_get_ancestors_returns_hierarchy(self):
        """_get_ancestors walks a real location chain from the object up to the root."""
        from dcim.models import Location

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("ivs-ancestors")
        grandparent = Location.objects.create(
            name="IVS grandparent", slug="ivs-grandparent", site=device.site, status="active"
        )
        parent = Location.objects.create(
            name="IVS parent", slug="ivs-parent", site=device.site, status="active", parent=grandparent
        )
        child = Location.objects.create(
            name="IVS child", slug="ivs-child", site=device.site, status="active", parent=parent
        )

        ancestors = self._mixin()._get_ancestors(child)

        assert ancestors == [child, parent, grandparent]

    def test_find_vlan_in_group_prefers_specified_group(self):
        """The requested group's VLAN wins over the global VLAN carrying the same VID."""
        from ipam.models import VLAN

        group = self._group("IVS find group")
        in_group = VLAN.objects.create(vid=100, name="IVS-100-GROUP", group=group)
        global_vlan = VLAN.objects.create(vid=100, name="IVS-100-GLOBAL")

        result = self._mixin()._find_vlan_in_group(100, group.pk, {**self._maps([in_group, global_vlan])})

        assert result == in_group
        assert result != global_vlan

    def test_find_vlan_in_group_falls_back_to_global(self):
        """A group holding no such VID falls back to the global VLAN."""
        from ipam.models import VLAN

        empty_group = self._group("IVS empty group")
        global_vlan = VLAN.objects.create(vid=100, name="IVS-100-ONLY-GLOBAL")

        result = self._mixin()._find_vlan_in_group(100, empty_group.pk, self._maps([global_vlan]))

        assert result == global_vlan

    def test_find_vlan_in_group_returns_none_if_not_found(self):
        """A VID absent from NetBox resolves to nothing."""
        result = self._mixin()._find_vlan_in_group(999, None, self._maps([]))

        assert result is None

    @staticmethod
    def _maps(vlans):
        """Build the real lookup maps the production indexer produces for *vlans*."""
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        return VlanAssignmentMixin._index_vlans(vlans)


@pytest.mark.django_db
class TestPortVlanEnrichment:
    """Tests for port VLAN data enrichment."""

    @staticmethod
    def _api(settings):
        """Return a real client bound to a configured server; parse_port_vlan_data sends no request."""
        from copy import deepcopy

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        plugin_config = deepcopy(settings.PLUGINS_CONFIG)
        plugin_settings = plugin_config["netbox_librenms_plugin"]
        plugin_settings["servers"] = {
            "default": {"librenms_url": "http://default.librenms.test", "api_token": "token-default"}
        }
        plugin_settings.pop("librenms_url", None)
        plugin_settings.pop("api_token", None)
        settings.PLUGINS_CONFIG = plugin_config
        return LibreNMSAPI(server_key="default")

    def test_parse_port_vlan_data_access_port(self, settings):
        """An untagged-only port parses as access mode."""
        port_data = {
            "port_id": 1234,
            "ifName": "Gi1/0/1",
            "ifDescr": "GigabitEthernet1/0/1",
            "ifVlan": "100",
            "ifTrunk": None,
        }

        result = self._api(settings).parse_port_vlan_data(port_data, "ifName")

        assert result["port_id"] == 1234
        assert result["interface_name"] == "Gi1/0/1"
        assert result["mode"] == "access"
        assert result["untagged_vlan"] == 100
        assert result["tagged_vlans"] == []

    def test_parse_port_vlan_data_trunk_port(self, settings):
        """A trunk port keeps its untagged VID and lists the tagged ones."""
        port_data = {
            "port_id": 5678,
            "ifName": "Te1/1/1",
            "ifDescr": "TenGigabitEthernet1/1/1",
            "ifVlan": "90",
            "ifTrunk": "dot1Q",
            "vlans": [
                {"vlan": 90, "untagged": 1, "state": "unknown"},
                {"vlan": 50, "untagged": 0, "state": "forwarding"},
                {"vlan": 60, "untagged": 0, "state": "forwarding"},
            ],
        }

        result = self._api(settings).parse_port_vlan_data(port_data, "ifName")

        assert result["port_id"] == 5678
        assert result["interface_name"] == "Te1/1/1"
        assert result["mode"] == "tagged"
        assert result["untagged_vlan"] == 90
        assert sorted(result["tagged_vlans"]) == [50, 60]

    def test_parse_port_vlan_data_uses_interface_name_field(self, settings):
        """The caller's field choice decides which LibreNMS name reaches the row."""
        port_data = {
            "port_id": 1234,
            "ifName": "Gi1/0/1",
            "ifDescr": "GigabitEthernet1/0/1",
            "ifVlan": "100",
            "ifTrunk": None,
        }

        result = self._api(settings).parse_port_vlan_data(port_data, "ifDescr")

        assert result["interface_name"] == "GigabitEthernet1/0/1"


@pytest.mark.django_db
class TestInterfaceVlanSync:
    """Tests for interface VLAN sync action."""

    @staticmethod
    def _fixture(tag, vlans=(), *, group=None):
        """Return a mixin, a real interface and the lookup maps the production indexer builds."""
        from ipam.models import VLAN

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        mixin = VlanAssignmentMixin()
        interface = make_interface(make_device(f"ivs-sync-{tag}"), "eth0")
        created = [VLAN.objects.create(vid=vid, name=name, group=group) for vid, name in vlans]
        return mixin, interface, VlanAssignmentMixin._index_vlans(created), created

    def test_update_interface_vlan_assignment_access_mode(self):
        """An untagged-only port lands in access mode with that VLAN attached."""
        mixin, interface, maps, (vlan,) = self._fixture("access", [(100, "IVS-SYNC-100")])

        mixin._update_interface_vlan_assignment(interface, {"untagged_vlan": 100, "tagged_vlans": []}, None, maps)

        interface.refresh_from_db()
        assert interface.mode == "access"
        assert interface.untagged_vlan == vlan

    @pytest.mark.django_db
    def test_update_interface_vlan_assignment_access_mode_clears_tagged_vlans(self):
        """An access port keeps stale tagged VLANs unless this method clears them itself."""
        from ipam.models import VLAN

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface
        from netbox_librenms_plugin.views.mixins import VlanAssignmentMixin

        device = make_device("vlan-access-clear")
        interface = make_interface(device, "Ethernet1", iface_type="1000base-t")
        untagged_vlan = VLAN.objects.create(vid=100, name="vlan-access-clear-100")
        stale_tagged_vlan = VLAN.objects.create(vid=200, name="vlan-access-clear-200")
        # Mode and untagged VLAN already match the port data, so nothing here re-saves the
        # interface. NetBox clears tagged VLANs on save(), which is why that path is excluded.
        interface.mode = "access"
        interface.untagged_vlan = untagged_vlan
        interface.save()
        interface.tagged_vlans.set([stale_tagged_vlan])
        assert list(interface.tagged_vlans.all()) == [stale_tagged_vlan]

        result = VlanAssignmentMixin()._update_interface_vlan_assignment(
            interface,
            {"untagged_vlan": 100, "tagged_vlans": []},
            None,
            {
                "vid_group_to_vlan": {(100, None): untagged_vlan},
                "vid_to_vlans": {100: [untagged_vlan]},
            },
        )

        interface.refresh_from_db()
        assert interface.mode == "access"
        assert interface.untagged_vlan == untagged_vlan
        assert list(interface.tagged_vlans.all()) == []
        assert result["changed"] is True

    def test_update_interface_vlan_assignment_tagged_mode(self):
        """A trunk port lands in tagged mode with both tagged VLANs attached."""
        mixin, interface, maps, (v100, v200, v300) = self._fixture(
            "tagged", [(100, "IVS-SYNC-T100"), (200, "IVS-SYNC-T200"), (300, "IVS-SYNC-T300")]
        )

        mixin._update_interface_vlan_assignment(
            interface, {"untagged_vlan": 100, "tagged_vlans": [200, 300]}, None, maps
        )

        interface.refresh_from_db()
        assert interface.mode == "tagged"
        assert interface.untagged_vlan == v100
        assert set(interface.tagged_vlans.all()) == {v200, v300}

    def test_update_interface_vlan_assignment_missing_vlans(self):
        """VIDs absent from NetBox are reported and nothing is attached."""
        mixin, interface, maps, _ = self._fixture("missing")

        result = mixin._update_interface_vlan_assignment(
            interface, {"untagged_vlan": 100, "tagged_vlans": [200, 300]}, None, maps
        )

        interface.refresh_from_db()
        assert result["missing_vlans"] == [100, 200, 300]
        assert interface.untagged_vlan is None
        assert list(interface.tagged_vlans.all()) == []

    def test_update_interface_vlan_assignment_respects_group_selection(self):
        """With the same VID in a group and globally, the requested group decides the winner."""
        from ipam.models import VLAN, VLANGroup

        group = VLANGroup.objects.create(name="IVS sync group", slug="ivs-sync-group")
        mixin, interface, _maps, (in_group,) = self._fixture("group-select", [(100, "IVS-SYNC-G100")], group=group)
        global_vlan = VLAN.objects.create(vid=100, name="IVS-SYNC-GLOBAL100")
        maps = mixin._index_vlans([in_group, global_vlan])

        mixin._update_interface_vlan_assignment(interface, {"untagged_vlan": 100, "tagged_vlans": []}, group.pk, maps)

        interface.refresh_from_db()
        assert interface.untagged_vlan == in_group
        assert interface.untagged_vlan != global_vlan


class TestInterfaceCssClassGroupMatching:
    """Verify that VLAN group mismatches use warning CSS instead of success CSS for matching VIDs."""

    # -- get_untagged_vlan_css_class --

    def test_untagged_vid_match_group_match_returns_green(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_untagged_vlan_css_class

        assert get_untagged_vlan_css_class(60, 60, True, [], group_matches=True) == "text-success"

    def test_untagged_vid_match_group_mismatch_returns_orange(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_untagged_vlan_css_class

        assert get_untagged_vlan_css_class(60, 60, True, [], group_matches=False) == "text-warning"

    def test_untagged_vid_differs_group_irrelevant(self, mock_librenms_config):
        """Different VIDs -> text-warning regardless of group_matches."""
        from netbox_librenms_plugin.utils import get_untagged_vlan_css_class

        assert get_untagged_vlan_css_class(60, 100, True, [], group_matches=True) == "text-warning"

    def test_untagged_not_in_netbox_ignores_group(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_untagged_vlan_css_class

        assert get_untagged_vlan_css_class(60, 60, False, [], group_matches=True) == "text-danger"

    def test_untagged_missing_vlan_ignores_group(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_untagged_vlan_css_class

        assert get_untagged_vlan_css_class(60, 60, True, [60], group_matches=True) == "text-danger"

    def test_untagged_no_netbox_vlan_returns_red(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_untagged_vlan_css_class

        assert get_untagged_vlan_css_class(60, None, True, [], group_matches=True) == "text-danger"

    def test_untagged_default_group_matches_is_true(self, mock_librenms_config):
        """Without group_matches param, defaults to True (backward compat)."""
        from netbox_librenms_plugin.utils import get_untagged_vlan_css_class

        assert get_untagged_vlan_css_class(60, 60, True, []) == "text-success"

    # -- get_tagged_vlan_css_class --

    def test_tagged_vid_present_group_match_returns_green(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_tagged_vlan_css_class

        assert get_tagged_vlan_css_class(60, {60, 100}, True, [], group_matches=True) == "text-success"

    def test_tagged_vid_present_group_mismatch_returns_orange(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_tagged_vlan_css_class

        assert get_tagged_vlan_css_class(60, {60, 100}, True, [], group_matches=False) == "text-warning"

    def test_tagged_vid_absent_group_irrelevant(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_tagged_vlan_css_class

        assert get_tagged_vlan_css_class(60, {100}, True, [], group_matches=True) == "text-danger"

    def test_tagged_not_in_netbox_ignores_group(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_tagged_vlan_css_class

        assert get_tagged_vlan_css_class(60, {60}, False, [], group_matches=True) == "text-danger"

    def test_tagged_missing_vlan_ignores_group(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import get_tagged_vlan_css_class

        assert get_tagged_vlan_css_class(60, {60}, True, [60], group_matches=True) == "text-danger"

    def test_tagged_default_group_matches_is_true(self, mock_librenms_config):
        """Without group_matches param, defaults to True (backward compat)."""
        from netbox_librenms_plugin.utils import get_tagged_vlan_css_class

        assert get_tagged_vlan_css_class(60, {60}, True, []) == "text-success"

    # -- check_vlan_group_matches --

    def test_check_group_matches_untagged_same_group(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import check_vlan_group_matches

        assert check_vlan_group_matches("U", 60, 5, 5, {}, 60, set()) is True

    def test_check_group_matches_untagged_different_group(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import check_vlan_group_matches

        assert check_vlan_group_matches("U", 60, 10, 5, {}, 60, set()) is False

    def test_check_group_matches_untagged_vid_differs(self, mock_librenms_config):
        """When VIDs don't match, group comparison is irrelevant -> True."""
        from netbox_librenms_plugin.utils import check_vlan_group_matches

        assert check_vlan_group_matches("U", 60, 10, 5, {}, 100, set()) is True

    def test_check_group_matches_tagged_same_group(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import check_vlan_group_matches

        assert check_vlan_group_matches("T", 60, 5, None, {60: 5}, None, {60}) is True

    def test_check_group_matches_tagged_different_group(self, mock_librenms_config):
        from netbox_librenms_plugin.utils import check_vlan_group_matches

        assert check_vlan_group_matches("T", 60, 10, None, {60: 5}, None, {60}) is False

    def test_check_group_matches_tagged_vid_absent(self, mock_librenms_config):
        """When VID is not tagged in NetBox, group comparison irrelevant -> True."""
        from netbox_librenms_plugin.utils import check_vlan_group_matches

        assert check_vlan_group_matches("T", 60, 10, None, {}, None, set()) is True

    def test_check_group_matches_global_to_global(self, mock_librenms_config):
        """Both NetBox VLAN and selected have no group (global) -> match."""
        from netbox_librenms_plugin.utils import check_vlan_group_matches

        assert check_vlan_group_matches("U", 60, None, None, {}, 60, set()) is True

    def test_check_group_matches_global_vs_group(self, mock_librenms_config):
        """NetBox VLAN is global, selected is a specific group -> mismatch."""
        from netbox_librenms_plugin.utils import check_vlan_group_matches

        assert check_vlan_group_matches("U", 60, 5, None, {}, 60, set()) is False
