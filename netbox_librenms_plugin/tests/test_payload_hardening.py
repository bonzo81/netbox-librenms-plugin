"""Issue #100: base views must fail closed on malformed-but-truthy LibreNMS payloads.

A LibreNMS ``success=True`` response can still carry a non-well-shaped body (a string, a
list of scalars, a dict with a non-list ``ports``). Dereferencing it turned a refresh into a
500 instead of the existing error/fallback path. These tests cover the shared
``is_list_of_dicts`` guard and the three develop-owned sites that adopt it.
"""

from unittest.mock import MagicMock, patch

import pytest


def _make_real_device(tag):
    """Create and return a real NetBox Device (with its required FKs) for DB-backed tests."""
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name=f"Mfr-{tag}", slug=f"mfr-{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"DT-{tag}", slug=f"dt-{tag}")
    role, _ = DeviceRole.objects.get_or_create(name=f"Role-{tag}", slug=f"role-{tag}")
    site, _ = Site.objects.get_or_create(name=f"Site-{tag}", slug=f"site-{tag}")
    return Device.objects.create(name=f"host-{tag}", device_type=dt, role=role, site=site, status="active")


class TestIsListOfDicts:
    """Unit tests for the shared payload-shape guard."""

    def test_list_of_dicts_is_true(self):
        from netbox_librenms_plugin.utils import is_list_of_dicts

        assert is_list_of_dicts([{"a": 1}, {"b": 2}]) is True

    def test_empty_list_is_true(self):
        """A device legitimately with no rows is valid."""
        from netbox_librenms_plugin.utils import is_list_of_dicts

        assert is_list_of_dicts([]) is True

    def test_non_list_is_false(self):
        from netbox_librenms_plugin.utils import is_list_of_dicts

        assert is_list_of_dicts("oops") is False
        assert is_list_of_dicts({"a": 1}) is False
        assert is_list_of_dicts(None) is False

    def test_list_with_a_scalar_row_is_false(self):
        from netbox_librenms_plugin.utils import is_list_of_dicts

        assert is_list_of_dicts([{"a": 1}, "scalar"]) is False


class TestInterfacePostMalformedPorts:
    """Site 1: BaseInterfaceTableView.post must fail closed when get_ports returns a truthy-but-malformed payload, redirecting with an error instead of 500ing."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.cache_timeout = 300
        return view

    def test_non_dict_ports_payload_redirects_without_500(self):
        view = self._make_view()
        view._librenms_api.get_librenms_id.return_value = 1
        # success=True but the body is a bare string (malformed).
        view._librenms_api.get_ports.return_value = (True, "garbage-string")
        view.get_object = MagicMock(return_value=MagicMock())
        view.get_redirect_url = MagicMock(return_value="/redirect")

        with (
            patch(
                "netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch(
                "netbox_librenms_plugin.views.base.interfaces_view.redirect",
                return_value="REDIRECT",
            ) as mock_redirect,
        ):
            result = view.post(MagicMock(), pk=1)

        assert result == "REDIRECT"
        mock_redirect.assert_called_once_with("/redirect")
        assert mock_messages.error.called  # error surfaced, not a 500


class TestSyncViewDeviceInfoMalformed:
    """Site 2: get_librenms_device_info must fall back to default details when get_device_info returns a truthy non-dict payload."""

    def test_non_dict_device_info_falls_back_to_defaults(self):
        from netbox_librenms_plugin.views.base.librenms_sync_view import BaseLibreNMSSyncView

        view = object.__new__(BaseLibreNMSSyncView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.librenms_id = 1
        view._librenms_api.get_device_info.return_value = (True, "garbage-string")

        obj = MagicMock()
        obj.name = "dev1"
        obj.primary_ip = None

        result = view.get_librenms_device_info(obj)

        assert result["found_in_librenms"] is False
        # Default details block (no fields read off the malformed payload).
        assert result["librenms_device_details"]["librenms_device_hardware"] == "-"
        assert result["librenms_device_details"]["librenms_device_serial"] == "-"


class TestVLANFetchMalformedPayload:
    """Site 3: _fetch_and_cache_vlan_data must reject a truthy non-list-of-dicts payload and not cache it (compare_vlans would later 500 on vlan.get(...))."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        view = object.__new__(BaseVLANTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.cache_timeout = 300
        view.librenms_id = 1
        return view

    def test_malformed_vlan_payload_fails_closed_without_caching(self):
        view = self._make_view()
        view._librenms_api.get_device_vlans.return_value = (True, "garbage-string")

        with patch("netbox_librenms_plugin.views.base.vlan_table_view.cache") as mock_cache:
            success, msg = view._fetch_and_cache_vlan_data(MagicMock())

        assert success is False
        assert "malformed" in msg.lower()
        mock_cache.set.assert_not_called()

    def test_valid_vlan_payload_is_cached(self):
        view = self._make_view()
        view._librenms_api.get_device_vlans.return_value = (True, [{"vlan_vlan": 10}])

        with patch("netbox_librenms_plugin.views.base.vlan_table_view.cache") as mock_cache:
            success, msg = view._fetch_and_cache_vlan_data(MagicMock())

        assert success is True
        assert msg is None
        assert mock_cache.set.called

    @pytest.mark.django_db
    def test_malformed_cached_vlans_fails_closed_on_render(self):
        """A stale/corrupt cached VLAN snapshot must be dropped, purged and rendered empty rather than 500 compare_vlans() on a GET render (covers a pre-fix entry still in the cache; real Device, only the cache backend is stubbed)."""
        from django.test import RequestFactory

        device = _make_real_device("vlan-readguard")
        view = self._make_view()
        request = RequestFactory().get("/")
        vlans_key = view.get_cache_key(device, "vlans", "default")

        for bad in ("garbage-string", [42], [{"vlan_vlan": 1}, "scalar"]):
            with patch("netbox_librenms_plugin.views.base.vlan_table_view.cache") as mock_cache:
                mock_cache.get.side_effect = lambda key, bad=bad, vk=vlans_key: bad if key == vk else None
                mock_cache.ttl.return_value = None
                ctx = view.get_vlan_context(request, device)  # must not raise

            assert ctx["vlan_table"] is None  # rendered empty, no 500 from compare_vlans
            assert any(c.args and c.args[0] == vlans_key for c in mock_cache.delete.call_args_list)  # purged

    @pytest.mark.django_db
    def test_empty_cached_vlans_renders_empty_table(self):
        """An empty-but-valid cached VLAN snapshot ([] — a device with no VLANs after a successful refresh) must still render a (empty) table, not be skipped by a truthy check and look like it never loaded."""
        from django.test import RequestFactory

        from django.contrib.auth.models import AnonymousUser

        device = _make_real_device("vlan-empty-render")
        view = self._make_view()
        request = RequestFactory().get("/")
        request.user = AnonymousUser()  # building the table reads request.user for paginate count
        vlans_key = view.get_cache_key(device, "vlans", "default")

        with patch("netbox_librenms_plugin.views.base.vlan_table_view.cache") as mock_cache:
            mock_cache.get.side_effect = lambda key, vk=vlans_key: [] if key == vk else None
            mock_cache.ttl.return_value = None
            ctx = view.get_vlan_context(request, device)

        assert ctx["vlan_table"] is not None  # the empty list still builds a table


@pytest.mark.django_db
class TestInterfaceCachedRenderMalformedPorts:
    """Site 4: the cached interface render degrades a malformed cached ``ports`` to an empty table."""

    def _make_view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        view = DeviceInterfaceTableView()
        # librenms_api is the external boundary this cached-render path never calls (it reads only the
        # already-cached snapshot); stub only the server_key that get_table() reads.
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    @pytest.mark.parametrize("bad_ports", [None, [{"port_id": 1}, "scalar"], "garbage-string"])
    def test_malformed_cached_ports_renders_empty_without_500(self, bad_ports):
        """A dict envelope carrying a non-list-of-dicts ``ports`` renders an empty table, not a 500."""
        from django.contrib.auth.models import AnonymousUser
        from django.core.cache import cache
        from django.test import RequestFactory

        device = _make_real_device("iface-cache-readguard")
        view = self._make_view()
        request = RequestFactory().get("/")
        request.user = AnonymousUser()  # table.configure paginates -> reads request.user
        view.setup(request)  # sets self.request, which get_table() reads for the htmx_url
        ports_key = view.get_cache_key(device, "ports", "default")
        cache.set(ports_key, {"ports": bad_ports})
        try:
            # Pre-fix, `for port in ports_data` (ports_data=None) / `port.get(...)` (scalar row) 500s here.
            ctx = view.get_context_data(request, device, "ifName", server_key="default")
        finally:
            cache.delete(ports_key)

        assert ctx["table"] is not None  # degraded to an empty-but-real table built from []
        assert len(list(ctx["table"].rows)) == 0
        assert ctx["netbox_only_interfaces"] == []
